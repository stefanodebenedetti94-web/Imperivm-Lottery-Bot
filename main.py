# === IMPERIVM Lottery Bot — main.py (VPS / discord.py 2.x) ===
# Stato persistito su GitHub Gist
# Lotteria classica + speciale
# Automazione VPS:
# - Mercoledì 00:00 apertura
# - Giovedì 00:00 chiusura
# - Giovedì 08:00 annuncio
# La modalità automatica segue STATE["lottery_mode"]:
# - "classic"  -> apertura/chiusura/annuncio classici
# - "special"  -> apertura/chiusura/annuncio speciali
# Dopo un annuncio SPECIALE automatico, la modalità torna automaticamente a CLASSICA.

import os
import json
import random
import time
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Tuple

import urllib.request
import urllib.error

import pytz
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ---------- Config ----------

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.reactions = True

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ Manca DISCORD_TOKEN nelle variabili ambiente.")

LOTTERY_CHANNEL_ID = int(os.getenv("LOTTERY_CHANNEL_ID", "0"))

ADMIN_IDS = set()
_env_admins = os.getenv("ADMINS", "").strip()
if _env_admins:
    try:
        ADMIN_IDS = {int(x) for x in _env_admins.replace(" ", "").split(",") if x}
    except Exception:
        ADMIN_IDS = set()

TZ = pytz.timezone(os.getenv("TZ", "Europe/Rome"))
AUTO_WINDOW_MINUTES = int(os.getenv("AUTO_WINDOW_MINUTES", "10"))

# Sync slash opzionale (solo se vuoi forzare sync al boot)
SYNC_ON_START = os.getenv("SYNC_ON_START", "false").strip().lower() in {"1", "true", "yes", "on"}
SYNC_GUILD_ID = int(os.getenv("SYNC_GUILD_ID", "0"))

# Debounce salvataggi Gist
GIST_SAVE_MIN_INTERVAL = float(os.getenv("GIST_SAVE_MIN_INTERVAL", "2.0"))

# Colori
GOLD = discord.Color.from_str("#DAA520")
COLOR_INT = discord.Color.from_str("#C0392B")
COLOR_AGI = discord.Color.from_str("#27AE60")
COLOR_CHA = discord.Color.from_str("#2980B9")
COLOR_STR = discord.Color.from_str("#8E5A2B")

# Gist
GIST_ID = os.getenv("GIST_ID") or ""
GIST_FILENAME = os.getenv("GIST_FILENAME") or "imperivm_state.json"
GIST_TOKEN = os.getenv("GIST_TOKEN") or ""

# ---------- Gist / State ----------

def _gist_api_url() -> str:
    return f"https://api.github.com/gists/{GIST_ID}"

def _gist_headers() -> Dict[str, str]:
    hdr = {"Accept": "application/vnd.github+json"}
    if GIST_TOKEN:
        hdr["Authorization"] = f"Bearer {GIST_TOKEN}"
    return hdr

STATE: Dict = {}
_LAST_GIST_SAVE_TS = 0.0
TEST_TASKS: Dict[str, asyncio.Task] = {}

DEFAULT_STATE = {
    "schema": "imperivm.lottery.v2",
    "edition": 1,
    "open_message_id": None,

    "wins": {},
    "victories": {},
    "cycles": {},
    "last_win_iso": {},

    "last_winner_id": None,
    "last_winner_ids": [],
    "last_winner_prev_levels": {},
    "last_winner_reset_flags": {},

    "last_special_prize": None,

    "weekly_modifier": None,
    "weekly_modifier_week": None,
    "test_override_modifier": None,
    "active_modifier": None,

    "names": {},

    "last_open_week": None,
    "last_close_week": None,
    "last_announce_week": None,

    # Nuove chiavi
    "automation_enabled": True,      # True = automazione attiva
    "lottery_mode": "classic",       # "classic" oppure "special"
}

def load_state_from_gist() -> Dict:
    if not GIST_ID:
        return DEFAULT_STATE.copy()

    try:
        req = urllib.request.Request(_gist_api_url(), headers=_gist_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        files = data.get("files", {})
        file_obj = files.get(GIST_FILENAME)
        if not file_obj:
            return DEFAULT_STATE.copy()

        content = file_obj.get("content", "")
        if not content:
            return DEFAULT_STATE.copy()

        parsed = json.loads(content)

        for k, v in DEFAULT_STATE.items():
            if k not in parsed:
                parsed[k] = v

        if not isinstance(parsed.get("last_winner_ids"), list):
            parsed["last_winner_ids"] = []
        if not isinstance(parsed.get("names"), dict):
            parsed["names"] = {}
        if not isinstance(parsed.get("wins"), dict):
            parsed["wins"] = {}
        if not isinstance(parsed.get("victories"), dict):
            parsed["victories"] = {}
        if not isinstance(parsed.get("cycles"), dict):
            parsed["cycles"] = {}
        if not isinstance(parsed.get("last_win_iso"), dict):
            parsed["last_win_iso"] = {}
        if not isinstance(parsed.get("last_winner_prev_levels"), dict):
            parsed["last_winner_prev_levels"] = {}
        if not isinstance(parsed.get("last_winner_reset_flags"), dict):
            parsed["last_winner_reset_flags"] = {}

        if parsed.get("lottery_mode") not in {"classic", "special"}:
            parsed["lottery_mode"] = "classic"

        parsed["automation_enabled"] = bool(parsed.get("automation_enabled", True))
        return parsed

    except Exception as e:
        print("Errore load Gist:", e)
        return DEFAULT_STATE.copy()

def save_state_to_gist(state: Dict):
    if not GIST_ID:
        return

    payload = json.dumps({
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(state, ensure_ascii=False, indent=2)
            }
        }
    }).encode("utf-8")

    try:
        req = urllib.request.Request(_gist_api_url(), headers=_gist_headers(), data=payload, method="PATCH")
        with urllib.request.urlopen(req, timeout=20) as _:
            pass
    except urllib.error.HTTPError as e:
        print("Errore salvataggio Gist:", e.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        print("Errore salvataggio Gist:", e)

def load_state():
    global STATE
    STATE = load_state_from_gist()
    if STATE.get("schema") != DEFAULT_STATE["schema"]:
        STATE["schema"] = DEFAULT_STATE["schema"]
    save_state(force=True)

def save_state(force: bool = False):
    global _LAST_GIST_SAVE_TS
    now_mono = time.monotonic()

    if not force and (now_mono - _LAST_GIST_SAVE_TS) < GIST_SAVE_MIN_INTERVAL:
        return

    _LAST_GIST_SAVE_TS = now_mono
    save_state_to_gist(STATE)

# ---------- Bot ----------

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------- Utility ----------

def _slash_admin_guard(inter: discord.Interaction) -> bool:
    if ADMIN_IDS and inter.user.id in ADMIN_IDS:
        return True
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

def admin_only_command():
    def deco(func):
        func = app_commands.guild_only()(func)
        func = app_commands.default_permissions(administrator=True)(func)
        return func
    return deco

async def get_lottery_channel(guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
    if guild is None:
        return None

    if LOTTERY_CHANNEL_ID:
        ch = guild.get_channel(LOTTERY_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch

    return guild.text_channels[0] if guild.text_channels else None

def imperial_embed(title: str, desc: str, color: discord.Color = GOLD) -> discord.Embed:
    e = discord.Embed(title=f"📜  {title}  📜", description=desc, color=color)
    e.set_footer(text="IMPERIVM • Lotteria settimanale")
    return e

def week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-{iso[1]:02d}"

def now_tz() -> datetime:
    return datetime.now(TZ)

def fmt_kama(n: int) -> str:
    return f"{n:,}".replace(",", ".") + " Kama"

def remember_name(uid: int, display_name: str):
    if display_name:
        STATE.setdefault("names", {})
        STATE["names"][str(uid)] = display_name

def name_fallback(uid: int) -> str:
    return STATE.get("names", {}).get(str(uid), f"utente {uid}")

def require_guild(inter: discord.Interaction) -> bool:
    return inter.guild is not None

def clear_user_state(uid: str):
    STATE.get("wins", {}).pop(uid, None)
    STATE.get("victories", {}).pop(uid, None)
    STATE.get("cycles", {}).pop(uid, None)
    STATE.get("last_win_iso", {}).pop(uid, None)
    STATE.get("names", {}).pop(uid, None)

def mode_label(mode: Optional[str]) -> str:
    return "SPECIALE" if mode == "special" else "CLASSICA"

def automation_label() -> str:
    return "ON" if STATE.get("automation_enabled", True) else "OFF"

def get_future_effective_modifier() -> Optional[str]:
    """Modificatore che verrebbe usato alla prossima apertura classica."""
    if STATE.get("lottery_mode", "classic") != "classic":
        return None

    override = STATE.get("test_override_modifier")
    if override in {MOD_INT, MOD_CHA, MOD_AGI, MOD_STR}:
        return override

    ensure_weekly_modifier()
    return STATE.get("weekly_modifier")

def in_time_window(now: datetime, target_weekday: int, target_hour: int, window_minutes: int = AUTO_WINDOW_MINUTES) -> bool:
    """
    True se siamo nel giorno/ora target e il minuto è dentro la finestra utile.
    Esempio: 00:00 -> 00:09 se AUTO_WINDOW_MINUTES=10
    """
    return (
        now.weekday() == target_weekday
        and now.hour == target_hour
        and 0 <= now.minute < window_minutes
    )

async def catch_up_automation_now():
    """
    Recupera eventi automatici se il bot si avvia/riavvia dentro la finestra utile.
    Non duplica grazie ai last_*_week.
    """
    if not STATE.get("automation_enabled", True):
        return

    now = now_tz()

    # Mercoledì 00:00-00:09 -> apertura
    if in_time_window(now, target_weekday=2, target_hour=0):
        await run_auto_open()

    # Giovedì 00:00-00:09 -> chiusura
    if in_time_window(now, target_weekday=3, target_hour=0):
        await run_auto_close()

    # Giovedì 08:00-08:09 -> annuncio
    if in_time_window(now, target_weekday=3, target_hour=8):
        await run_auto_announce()

# ---------- Modificatori ----------

MOD_INT = "INT"
MOD_CHA = "CHA"
MOD_AGI = "AGI"
MOD_STR = "STR"

def modifier_label(mod: Optional[str]) -> str:
    return {
        MOD_INT: "ELEMENTO INTELLIGENCE",
        MOD_CHA: "ELEMENTO CHANCE",
        MOD_AGI: "ELEMENTO AGILITY",
        MOD_STR: "ELEMENTO STRENGTH",
    }.get(mod or "", "OFF")

def lottery_color_for_modifier(mod: Optional[str]) -> discord.Color:
    if mod == MOD_INT:
        return COLOR_INT
    if mod == MOD_AGI:
        return COLOR_AGI
    if mod == MOD_CHA:
        return COLOR_CHA
    if mod == MOD_STR:
        return COLOR_STR
    return GOLD

def modifier_open_block(mod: str) -> List[str]:
    if mod == MOD_INT:
        return [
            "🧠 **MODIFICATORE ATTIVO — ELEMENTO INTELLIGENCE**",
            "+30% Kama sul premio finale del vincitore",
        ]
    if mod == MOD_CHA:
        return [
            "🍀 **MODIFICATORE ATTIVO — ELEMENTO CHANCE**",
            "Doppia estrazione: verranno scelti **DUE vincitori**",
            "Entrambi ricevono il premio completo in base al livello",
        ]
    if mod == MOD_AGI:
        return [
            "🌪️ **MODIFICATORE ATTIVO — ELEMENTO AGILITY**",
            "+50% probabilità ai partecipanti con livello più basso",
        ]
    if mod == MOD_STR:
        return [
            "💪 **MODIFICATORE ATTIVO — ELEMENTO STRENGTH**",
            "Premio raddoppiato — livello non avanza (L1/L2)",
            "Se vinci da Livello 3 → reset immediato a Livello 1",
        ]
    return ["⚙️ **MODIFICATORE ATTIVO:** OFF"]

def pick_weekly_modifier() -> str:
    population = [MOD_INT, MOD_AGI, MOD_STR, MOD_CHA]
    weights = [25, 25, 25, 20]
    return random.choices(population, weights=weights, k=1)[0]

def ensure_weekly_modifier():
    wk = week_key(now_tz())
    if STATE.get("weekly_modifier_week") != wk or STATE.get("weekly_modifier") not in {MOD_INT, MOD_CHA, MOD_AGI, MOD_STR}:
        STATE["weekly_modifier"] = pick_weekly_modifier()
        STATE["weekly_modifier_week"] = wk
        save_state()

def get_effective_modifier_for_open() -> str:
    override = STATE.get("test_override_modifier")
    if override in {MOD_INT, MOD_CHA, MOD_AGI, MOD_STR}:
        return override
    ensure_weekly_modifier()
    return STATE["weekly_modifier"]

# ---------- Livelli ----------

def get_level(uid: str) -> int:
    try:
        v = int(STATE.get("wins", {}).get(uid, 1))
    except Exception:
        v = 1
    return max(1, min(3, v))

def set_level(uid: str, lvl: int):
    lvl = max(1, min(3, int(lvl)))
    STATE.setdefault("wins", {})
    STATE["wins"][uid] = lvl

def base_prize_amount_for_level(lvl: int) -> int:
    if lvl == 1:
        return 100_000
    if lvl == 2:
        return 250_000
    return 500_000

def base_prize_text_for_level(lvl: int) -> str:
    if lvl == 1:
        return "100.000 Kama"
    if lvl == 2:
        return "250.000 Kama"
    return "500.000 Kama *(reset a Livello 1)*"

def advance_level_after_classic_win(uid: str, prev_lvl: int) -> Tuple[int, bool]:
    if prev_lvl == 1:
        new, cycle = 2, False
    elif prev_lvl == 2:
        new, cycle = 3, False
    else:
        new, cycle = 1, True
    set_level(uid, new)
    return new, cycle

def apply_classic_win_after_prize(uid: str, prev_lvl: int):
    _, cycle = advance_level_after_classic_win(uid, prev_lvl)

    STATE.setdefault("victories", {})
    STATE.setdefault("cycles", {})
    STATE.setdefault("last_win_iso", {})

    STATE["victories"][uid] = int(STATE["victories"].get(uid, 0)) + 1
    if cycle:
        STATE["cycles"][uid] = int(STATE["cycles"].get(uid, 0)) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

def apply_strength_win_after_prize(uid: str, prev_lvl: int) -> Tuple[int, bool]:
    did_reset = False
    if prev_lvl == 3:
        set_level(uid, 1)
        did_reset = True
        new = 1
    else:
        set_level(uid, prev_lvl)
        new = prev_lvl

    STATE.setdefault("victories", {})
    STATE.setdefault("cycles", {})
    STATE.setdefault("last_win_iso", {})

    STATE["victories"][uid] = int(STATE["victories"].get(uid, 0)) + 1
    if did_reset:
        STATE["cycles"][uid] = int(STATE["cycles"].get(uid, 0)) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")
    return new, did_reset

def update_last_win_iso_only(uid: str):
    STATE.setdefault("last_win_iso", {})
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

# ---------- Testi ----------

def _classic_open_lines(edition: int, mod: str) -> List[str]:
    base = [
        "Cittadini dell'Impero 👑",
        "La Lotteria Imperiale è ufficialmente **aperta**.",
        "",
        "Reagite con ✅ per partecipare all’estrazione.",
        "",
        "⚔️ Premi base per livello:",
        "1️⃣ Livello 1 → 100.000 Kama",
        "2️⃣ Livello 2 → 250.000 Kama",
        "3️⃣ Livello 3 → 500.000 Kama *(reset a Livello 1)*",
        "",
    ]
    base += modifier_open_block(mod)
    base += [
        "",
        f"**Edizione n°{edition} (Classica)**",
        "",
        "⏳ La lotteria chiuderà alle **00:00 di giovedì**",
    ]
    return base

def _special_open_lines(edition: int) -> List[str]:
    return [
        "Cittadini dell'Impero 👑",
        "**EDIZIONE SPECIALE** sotto gli stendardi bianco e rosso dell’IMPERIVM!",
        "Da ora fino alle 00:00 di giovedì, la sorte si farà capricciosa e magnanima. 🧾",
        "",
        "Reagite con ✅ a questo messaggio per partecipare.",
        "",
        "⚙️ **Modificatori:** OFF",
        "💎 **Borsa dei Premi Speciale (casuali):** 600.000 / 800.000 / 1.000.000 Kama",
        "",
        f"**Edizione n°{edition} (SPECIALE)**",
        "",
        "🕗 *Annuncio del vincitore alle **08:00** di giovedì.*",
    ]

# ---------- Messaggi ----------

async def post_open_message(channel: discord.TextChannel, special: bool):
    edition = STATE["edition"]

    if special:
        STATE["active_modifier"] = None
        save_state()
        lines = _special_open_lines(edition)
        embed = imperial_embed("LOTTERIA IMPERIVM – APERTA", "\n".join(lines), color=GOLD)
    else:
        mod = get_effective_modifier_for_open()
        STATE["active_modifier"] = mod
        save_state()
        lines = _classic_open_lines(edition, mod)
        embed = imperial_embed("LOTTERIA IMPERIVM – APERTA", "\n".join(lines), color=lottery_color_for_modifier(mod))

    msg = await channel.send(embed=embed)
    STATE["open_message_id"] = msg.id
    save_state()
    print(f"[LOTTERY] Apertura {'SPECIALE' if special else 'classica'} inviata (edizione {edition})")
    return msg

async def post_close_message(channel: discord.TextChannel, no_participants: bool, names_preview: Optional[str], special: bool):
    if no_participants:
        desc = (
            "La sorte ha parlato… 😕  **Nessun partecipante valido** questa settimana.\n"
            "Torniamo mercoledì prossimo! 👑"
        )
        color = GOLD
        if not special:
            mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")
            color = lottery_color_for_modifier(mod)
        await channel.send(embed=imperial_embed("LOTTERIA IMPERIVM – CHIUSA", desc, color=color))
        return

    desc = "I sigilli sono stati chiusi.\nI nomi sono stati consegnati al Fato.\n\n"
    if names_preview:
        desc += names_preview + "\n\n"

    if special:
        desc += "⚙️ Modificatori: **OFF**\n\n"
        desc += "🕗 **Annuncio del vincitore alle 08:00 di giovedì**."
        await channel.send(embed=imperial_embed("LOTTERIA IMPERIVM – CHIUSA", desc, color=GOLD))
        return

    mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")
    desc += f"⚙️ Modificatore attivo: **{modifier_label(mod)}**\n\n"
    desc += "🕗 **Annuncio del vincitore alle 08:00 di giovedì**."
    await channel.send(embed=imperial_embed("LOTTERIA IMPERIVM – CHIUSA", desc, color=lottery_color_for_modifier(mod)))

def _special_compute_prize() -> int:
    return random.choice([600_000, 800_000, 1_000_000])

# ---------- Annunci ----------

async def post_winner_announcement_classic(channel: discord.TextChannel, guild: discord.Guild):
    ids: List[int] = STATE.get("last_winner_ids") or []
    mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")
    color = lottery_color_for_modifier(mod)

    prev_levels: Dict[str, int] = STATE.get("last_winner_prev_levels", {}) or {}
    reset_flags: Dict[str, bool] = STATE.get("last_winner_reset_flags", {}) or {}

    if not ids:
        desc = (
            "Cittadini dell’Impero,\n"
            "i sigilli sono stati spezzati… ma nessun nome è stato scelto.\n"
            "Riproveremo mercoledì prossimo. 🕯️"
        )
        await channel.send(embed=imperial_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc, color=color))
        return

    async def send_one(idx: int, uid_int: int):
        uid = str(uid_int)

        member = guild.get_member(uid_int)
        if member:
            remember_name(uid_int, member.display_name)
            mention = member.mention
        else:
            mention = f"@{name_fallback(uid_int)}"

        prev_lvl = int(prev_levels.get(uid, 1))
        prev_lvl = max(1, min(3, prev_lvl))
        lvl_now = get_level(uid)

        if mod == MOD_CHA and len(ids) == 2:
            title = f"ESTRAZIONE {'I' if idx == 1 else 'II'} – LOTTERIA IMPERIVM"
            header = (
                f"👑 **Vincitore {'I' if idx == 1 else 'II'}:** {mention}\n"
                f"🎖️ **Livello (prima della vittoria):** {prev_lvl}\n"
                f"📌 **Nuovo livello:** {lvl_now}\n\n"
            )
        else:
            title = "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM"
            header = (
                "Cittadini dell’Impero,\n"
                "i sigilli sono stati spezzati e il Fato ha pronunciato il suo verdetto.\n\n"
                f"👑 **Vincitore:** {mention}\n"
                f"🎖️ **Livello (prima della vittoria):** {prev_lvl}\n"
                f"📌 **Nuovo livello:** {lvl_now}\n\n"
            )

        base_amt = base_prize_amount_for_level(prev_lvl)
        base_txt = base_prize_text_for_level(prev_lvl)

        if mod == MOD_INT:
            final_amt = int(round(base_amt * 1.30))
            body = (
                "🧠 **Elemento Intelligence attivo**\n\n"
                f"💰 **Ricompensa finale: {fmt_kama(final_amt)}**\n"
                "(+30% già applicato)"
            )
            await channel.send(embed=imperial_embed(title, header + body, color=color))
            return

        if mod == MOD_AGI:
            body = (
                "🌪️ **Elemento Agility attivo**\n"
                "Bonus probabilità livello più basso applicato\n\n"
                f"💰 **Ricompensa:** {base_txt}"
            )
            await channel.send(embed=imperial_embed(title, header + body, color=color))
            return

        if mod == MOD_CHA and len(ids) == 2:
            body = (
                "🍀 **Elemento Chance attivo**\n\n"
                f"💰 **Ricompensa:** {base_txt}"
            )
            await channel.send(embed=imperial_embed(title, header + body, color=color))
            return

        if mod == MOD_STR:
            final_amt = base_amt * 2
            did_reset = bool(reset_flags.get(uid, False))
            extra = "✅ Reset a Livello 1 applicato" if did_reset else "Livello fermo (non avanza)"
            body = (
                "💪 **Elemento Strength attivo**\n\n"
                f"💰 **Ricompensa finale: {fmt_kama(final_amt)}**\n"
                f"📌 {extra}"
            )
            await channel.send(embed=imperial_embed(title, header + body, color=color))
            return

        await channel.send(embed=imperial_embed(title, header + f"💰 **Ricompensa:** {base_txt}", color=color))

    if mod == MOD_CHA and len(ids) == 2:
        await send_one(1, ids[0])
        await send_one(2, ids[1])
        return

    await send_one(1, ids[0])

async def post_winner_announcement_special(channel: discord.TextChannel, guild: discord.Guild, winner_id: Optional[int]):
    if not winner_id:
        desc = (
            "I sigilli sono stati spezzati, ma stavolta il fato è rimasto muto.\n"
            "Nessun nome scolpito negli annali: riproveremo mercoledì prossimo. 🕯️"
        )
        await channel.send(embed=imperial_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc, color=GOLD))
        return

    member = guild.get_member(winner_id)
    if member:
        remember_name(winner_id, member.display_name)
        mention = member.mention
    else:
        mention = f"@{name_fallback(winner_id)}"

    premio = STATE.get("last_special_prize")
    if not isinstance(premio, int) or premio <= 0:
        premio = _special_compute_prize()
        STATE["last_special_prize"] = premio
        save_state()

    lvl = get_level(str(winner_id))
    desc = (
        "Cittadini dell’Impero, il sigillo dorato è stato infranto.\n"
        "Tra pergamene e ceralacca, il nome inciso negli annali è stato scelto.\n\n"
        f"👑 **Vincitore:** {mention}\n"
        f"🎖️ **Livello classico (solo informativo):** {lvl}\n"
        f"⚙️ **Modificatori:** OFF\n"
        f"💎 **Ricompensa Speciale:** {fmt_kama(premio)}\n\n"
        "Questa edizione **non modifica** i livelli.\n"
        "Che la fortuna continui a sorriderti."
    )
    await channel.send(embed=imperial_embed("ESTRAZIONE UFFICIALE – EDIZIONE SPECIALE", desc, color=GOLD))

# ---------- Partecipanti ----------

async def collect_participants(msg: discord.Message) -> List[int]:
    ids: List[int] = []
    for r in msg.reactions:
        if str(r.emoji) == "✅":
            users = [u async for u in r.users()]
            for u in users:
                if not u.bot:
                    ids.append(u.id)
    return list(dict.fromkeys(ids))

# ---------- Weighted pick ----------

def _time_weight_from_last_win(uid: str) -> float:
    iso = STATE.get("last_win_iso", {}).get(uid)
    if not iso:
        return 2.0
    try:
        last = datetime.fromisoformat(iso)
        delta_days = max(0.0, (now_tz() - last).total_seconds() / 86400.0)
        return 1.0 + min(delta_days / 7.0, 2.0)
    except Exception:
        return 1.5

def _agility_bonus_factor(uid: str, min_level: int) -> float:
    lvl = get_level(uid)
    return 1.5 if lvl == min_level else 1.0

def weighted_pick(participants: List[int], mod: Optional[str]) -> int:
    if not participants:
        raise ValueError("participants empty")

    min_lvl = 99
    for uid_int in participants:
        uid = str(uid_int)
        min_lvl = min(min_lvl, get_level(uid))
    if min_lvl == 99:
        min_lvl = 1

    weights: List[float] = []
    for uid_int in participants:
        uid = str(uid_int)
        w = _time_weight_from_last_win(uid)
        if mod == MOD_AGI:
            w *= _agility_bonus_factor(uid, min_lvl)
        weights.append(max(0.1, float(w)))

    return random.choices(participants, weights=weights, k=1)[0]

# ---------- Core lottery ----------

async def _close_and_pick_common(guild: discord.Guild, special: bool):
    channel = await get_lottery_channel(guild)
    if not channel:
        return None, [], "", []

    msg = None
    msg_id = STATE.get("open_message_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
        except Exception:
            msg = None

    participants: List[int] = []
    if msg:
        participants = await collect_participants(msg)

    names_preview = None
    if participants:
        names = []
        for uid_int in participants[:50]:
            m = guild.get_member(uid_int)
            if m:
                remember_name(uid_int, m.display_name)
                names.append(m.display_name)
            else:
                names.append(name_fallback(uid_int))
        more = len(participants) - 50
        header = f"📜 **Partecipanti ({len(participants)}):**"
        body = "• " + "\n".join(f"• {n}" for n in names)
        if more > 0:
            body += f"\n…e altri **{more}**."
        names_preview = f"{header}\n{body}"

    winners: List[int] = []
    mod = None if special else (STATE.get("active_modifier") or STATE.get("weekly_modifier"))

    STATE["last_winner_prev_levels"] = {}
    STATE["last_winner_reset_flags"] = {}
    STATE["last_special_prize"] = None

    if participants:
        if special:
            win_id = random.choice(participants)
            winners = [win_id]
            update_last_win_iso_only(str(win_id))
            STATE["last_special_prize"] = _special_compute_prize()
            save_state()

        else:
            if mod == MOD_CHA and len(participants) >= 2:
                first = weighted_pick(participants, mod=None)
                remaining = [x for x in participants if x != first]
                second = weighted_pick(remaining, mod=None)
                winners = [first, second]

                for wid in winners:
                    uid = str(wid)
                    prev_lvl = get_level(uid)
                    STATE["last_winner_prev_levels"][uid] = prev_lvl
                    apply_classic_win_after_prize(uid, prev_lvl)
                save_state()

            else:
                win_id = weighted_pick(participants, mod=mod)
                winners = [win_id]
                uid = str(win_id)

                prev_lvl = get_level(uid)
                STATE["last_winner_prev_levels"][uid] = prev_lvl

                if mod == MOD_STR:
                    _, did_reset = apply_strength_win_after_prize(uid, prev_lvl)
                    STATE["last_winner_reset_flags"][uid] = did_reset
                    save_state()
                else:
                    apply_classic_win_after_prize(uid, prev_lvl)
                    save_state()

    STATE["last_winner_ids"] = winners[:] if winners else []
    STATE["last_winner_id"] = winners[0] if winners else None
    save_state()

    return channel, winners, (names_preview or ""), participants

async def close_and_pick(guild: discord.Guild, announce_now: bool = False, special: bool = False):
    channel, winners, names_preview, participants = await _close_and_pick_common(guild, special=special)
    if not channel:
        return None

    await post_close_message(channel, len(participants) == 0, names_preview, special=special)

    if announce_now:
        if special:
            await post_winner_announcement_special(channel, guild, winners[0] if winners else None)
        else:
            await post_winner_announcement_classic(channel, guild)

    STATE["open_message_id"] = None
    save_state()
    print(f"[LOTTERY] Chiusura eseguita ({'SPECIALE' if special else 'classica'}) (partecipanti: {len(participants)})")
    return winners[0] if winners else None

async def open_lottery(guild: Optional[discord.Guild], special: bool = False):
    if guild is None:
        return

    channel = await get_lottery_channel(guild)
    if not channel:
        return

    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            print("[LOTTERY] Apertura saltata: messaggio già presente.")
            return
        except Exception:
            pass

    await post_open_message(channel, special=special)

# ---------- Automazione ----------

def auto_schedule_status_lines() -> List[str]:
    mode = STATE.get("lottery_mode", "classic")
    if not STATE.get("automation_enabled", True):
        return [
            "⏰ **Automazione:** OFF",
            f"📌 **Modalità automatica impostata:** {mode_label(mode)}",
        ]

    if mode == "special":
        return [
            "⏰ **Automazione:** ON",
            "• Mercoledì 00:00 → apertura SPECIALE",
            "• Giovedì 00:00 → chiusura SPECIALE",
            "• Giovedì 08:00 → annuncio SPECIALE",
            "• Dopo l'annuncio speciale → reset automatico a CLASSICA",
        ]

    return [
        "⏰ **Automazione:** ON",
        "• Mercoledì 00:00 → apertura CLASSICA",
        "• Giovedì 00:00 → chiusura CLASSICA",
        "• Giovedì 08:00 → annuncio CLASSICO",
    ]

async def run_auto_open():
    if not STATE.get("automation_enabled", True):
        return

    guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return

    wk = week_key(now_tz())
    if STATE.get("last_open_week") == wk:
        return

    mode = STATE.get("lottery_mode", "classic")
    special = (mode == "special")

    ed = int(STATE.get("edition", 1))
    await open_lottery(guild, special=special)
    STATE["edition"] = ed + 1
    STATE["last_open_week"] = wk
    save_state(force=True)
    print(f"[AUTO] Apertura automatica eseguita per settimana {wk} — modalità {mode_label(mode)}")

async def run_auto_close():
    if not STATE.get("automation_enabled", True):
        return

    guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return

    wk = week_key(now_tz())
    if STATE.get("last_close_week") == wk:
        return

    mode = STATE.get("lottery_mode", "classic")
    special = (mode == "special")

    await close_and_pick(guild, announce_now=False, special=special)
    STATE["last_close_week"] = wk
    save_state(force=True)
    print(f"[AUTO] Chiusura automatica eseguita per settimana {wk} — modalità {mode_label(mode)}")

async def run_auto_announce():
    if not STATE.get("automation_enabled", True):
        return

    guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return

    wk = week_key(now_tz())
    if STATE.get("last_announce_week") == wk:
        return

    ch = await get_lottery_channel(guild)
    if not ch:
        return

    mode = STATE.get("lottery_mode", "classic")
    special = (mode == "special")

    if special:
        lw = STATE.get("last_winner_id")
        winner_id = int(lw) if lw else None
        await post_winner_announcement_special(ch, guild, winner_id)

        STATE["last_announce_week"] = wk
        STATE["last_winner_id"] = None
        STATE["last_winner_ids"] = []
        STATE["last_special_prize"] = None

        # reset automatico a classica dopo una speciale automatica
        STATE["lottery_mode"] = "classic"
        save_state(force=True)
        print(f"[AUTO] Annuncio automatico SPECIALE eseguito per settimana {wk} — reset modalità a CLASSICA")
        return

    await post_winner_announcement_classic(ch, guild)

    STATE["last_announce_week"] = wk
    STATE["last_winner_id"] = None
    STATE["last_winner_ids"] = []
    STATE["last_winner_prev_levels"] = {}
    STATE["last_winner_reset_flags"] = {}
    save_state(force=True)
    print(f"[AUTO] Annuncio automatico CLASSICO eseguito per settimana {wk}")

@tasks.loop(minutes=1)
async def automation_loop():
    if not STATE.get("automation_enabled", True):
        return

    now = now_tz()

    try:
        # Mercoledì 00:00-00:09
        if in_time_window(now, target_weekday=2, target_hour=0):
            await run_auto_open()

        # Giovedì 00:00-00:09
        if in_time_window(now, target_weekday=3, target_hour=0):
            await run_auto_close()

        # Giovedì 08:00-08:09
        if in_time_window(now, target_weekday=3, target_hour=8):
            await run_auto_announce()

    except Exception as e:
        print("[AUTO] Errore automation_loop:", e)

@automation_loop.before_loop
async def before_automation_loop():
    await bot.wait_until_ready()

def schedule_test_task(name: str, minutes: int, coro_func):
    async def runner():
        try:
            await asyncio.sleep(minutes * 60)
            await coro_func()
        except asyncio.CancelledError:
            print(f"[TEST-AUTO] Task {name} annullato.")
        except Exception as e:
            print(f"[TEST-AUTO] Errore task {name}:", e)

    old = TEST_TASKS.get(name)
    if old and not old.done():
        old.cancel()

    TEST_TASKS[name] = asyncio.create_task(runner())

def cancel_all_test_tasks() -> int:
    count = 0
    for task in TEST_TASKS.values():
        if task and not task.done():
            task.cancel()
            count += 1
    TEST_TASKS.clear()
    return count

# ---------- Eventi ----------

@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass

    if not automation_loop.is_running():
        automation_loop.start()

    print(f"✅ Bot online come {bot.user} — prossima edizione: {STATE.get('edition')}")
    print(f"✅ Gist attivo: {'sì' if bool(GIST_ID) else 'no'}")
    print(f"✅ Canale lotteria ID: {LOTTERY_CHANNEL_ID or 'auto-primo-canale'}")
    print(f"✅ Admin extra caricati: {len(ADMIN_IDS)}")
    print(f"✅ Automazione: {automation_label()} — Modalità: {mode_label(STATE.get('lottery_mode', 'classic'))}")
    print(f"✅ Finestra automazione: primi {AUTO_WINDOW_MINUTES} minuti utili dell'ora target")

    try:
        await catch_up_automation_now()
    except Exception as e:
        print("[AUTO] Errore catch-up on_ready:", e)

# ---------- Comandi test modificatori ----------

@bot.tree.command(name="testmodificatore", description="TEST: forza il modificatore classico per le prossime aperture (solo admin).")
@admin_only_command()
@app_commands.choices(elemento=[
    app_commands.Choice(name="Intelligence (INT)", value=MOD_INT),
    app_commands.Choice(name="Chance (CHA)", value=MOD_CHA),
    app_commands.Choice(name="Agility (AGI)", value=MOD_AGI),
    app_commands.Choice(name="Strength (STR)", value=MOD_STR),
])
async def slash_testmodificatore(inter: discord.Interaction, elemento: app_commands.Choice[str]):
    STATE["test_override_modifier"] = elemento.value
    save_state(force=True)
    await inter.response.send_message(
        f"🧪 TEST attivo: la prossima apertura **classica** userà **{modifier_label(elemento.value)}**.\n"
        f"Per tornare casuale: **/testoff**",
        ephemeral=True
    )

@bot.tree.command(name="testoff", description="Disattiva il test override e torna al casuale settimanale (solo admin).")
@admin_only_command()
async def slash_testoff(inter: discord.Interaction):
    STATE["test_override_modifier"] = None
    save_state(force=True)
    await inter.response.send_message(
        "✅ Test disattivato. Da ora la prossima apertura **classica** usa il modificatore **casuale settimanale**.",
        ephemeral=True
    )

# ---------- Comandi utilità ----------

@bot.tree.command(name="whoami", description="Mostra il tuo ID e se sei admin.")
@admin_only_command()
async def slash_whoami(inter: discord.Interaction):
    adm = "sì" if _slash_admin_guard(inter) else "no"
    await inter.response.send_message(f"ID: {inter.user.id} — sei admin: {adm}", ephemeral=True)

@bot.tree.command(name="mostraedizione", description="Mostra la PROSSIMA edizione che verrà aperta.")
@admin_only_command()
async def slash_mostraedizione(inter: discord.Interaction):
    ed = STATE.get("edition", 1)
    await inter.response.send_message(f"🧾 **Prossima edizione da aprire:** n°{ed}", ephemeral=True)

@bot.tree.command(name="stato", description="Mostra lo stato attuale della lotteria e dell'automazione (solo admin).")
@admin_only_command()
async def slash_stato(inter: discord.Interaction):
    open_msg = STATE.get("open_message_id")
    current_week = week_key(now_tz())
    mode = STATE.get("lottery_mode", "classic")
    auto_enabled = STATE.get("automation_enabled", True)
    current_active_modifier = STATE.get("active_modifier")
    future_modifier = get_future_effective_modifier()

    desc = [
        f"🧾 **Prossima edizione:** {STATE.get('edition', 1)}",
        f"📬 **Lotteria aperta:** {'Sì' if open_msg else 'No'}",
        f"🗓️ **Settimana corrente:** {current_week}",
        f"🎭 **Modalità attiva:** {mode_label(mode)}",
        f"🤖 **Automazione:** {automation_label()}",
    ]

    if mode == "special":
        desc += [
            "⚙️ **Modificatori:** OFF",
            "🧪 **Override test:** ignorato in modalità SPECIALE",
            "🎯 **Modifier apertura corrente:** OFF",
            "🔮 **Prossima apertura automatica:** SPECIALE",
        ]
    else:
        desc += [
            f"⚙️ **Weekly modifier:** {modifier_label(STATE.get('weekly_modifier'))}",
            f"🧪 **Override test:** {modifier_label(STATE.get('test_override_modifier')) if STATE.get('test_override_modifier') else 'Nessuno'}",
            f"🎯 **Modifier apertura corrente:** {modifier_label(current_active_modifier)}",
            f"🔮 **Prossimo modificatore effettivo:** {modifier_label(future_modifier)}",
            "🔮 **Prossima apertura automatica:** CLASSICA" if auto_enabled else "🔮 **Prossima apertura automatica:** disattivata",
        ]

    desc += [
        f"📤 **Ultima apertura auto:** {STATE.get('last_open_week') or 'mai'}",
        f"🔒 **Ultima chiusura auto:** {STATE.get('last_close_week') or 'mai'}",
        f"📣 **Ultimo annuncio auto:** {STATE.get('last_announce_week') or 'mai'}",
        f"👥 **Utenti registrati:** {len(STATE.get('wins', {}))}",
        "",
    ]
    desc += auto_schedule_status_lines()

    await inter.response.send_message(
        embed=imperial_embed("STATO LOTTERIA IMPERIVM", "\n".join(desc), color=GOLD),
        ephemeral=True
    )

@bot.tree.command(name="automazione", description="Attiva o disattiva l'automazione settimanale (solo admin).")
@admin_only_command()
@app_commands.describe(stato="on oppure off")
@app_commands.choices(stato=[
    app_commands.Choice(name="ON", value="on"),
    app_commands.Choice(name="OFF", value="off"),
])
async def slash_automazione(inter: discord.Interaction, stato: app_commands.Choice[str]):
    new_state = (stato.value == "on")
    STATE["automation_enabled"] = new_state
    save_state(force=True)
    await inter.response.send_message(
        f"🤖 Automazione impostata su **{automation_label()}**.",
        ephemeral=True
    )

@bot.tree.command(name="modalita", description="Imposta la modalità settimanale automatica: classica o speciale (solo admin).")
@admin_only_command()
@app_commands.describe(tipo="classica oppure speciale")
@app_commands.choices(tipo=[
    app_commands.Choice(name="CLASSICA", value="classic"),
    app_commands.Choice(name="SPECIALE", value="special"),
])
async def slash_modalita(inter: discord.Interaction, tipo: app_commands.Choice[str]):
    if STATE.get("open_message_id"):
        await inter.response.send_message(
            "❌ Non puoi cambiare modalità mentre una lotteria è già aperta.",
            ephemeral=True
        )
        return

    STATE["lottery_mode"] = tipo.value
    save_state(force=True)

    if tipo.value == "special":
        msg = (
            "🎭 Modalità automatica impostata su **SPECIALE**.\n"
            "⚙️ Modificatori: **OFF**\n"
            "Dopo l'annuncio speciale automatico, la modalità tornerà da sola a **CLASSICA**."
        )
    else:
        msg = "🎭 Modalità automatica impostata su **CLASSICA**."

    await inter.response.send_message(msg, ephemeral=True)

# ---------- Comandi livelli ----------

@bot.tree.command(name="mostralivelli", description="Mostra i livelli registrati (solo admin).")
@admin_only_command()
async def slash_mostralivelli(inter: discord.Interaction):
    if not require_guild(inter):
        await inter.response.send_message("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    await inter.response.defer(ephemeral=True, thinking=True)

    wins = STATE.get("wins", {})
    if not wins:
        await inter.followup.send("📜 Nessun livello registrato al momento.", ephemeral=True)
        return

    items = sorted(wins.items(), key=lambda x: (-int(x[1]), int(x[0])))

    lines = []
    for uid, lvl in items:
        uid_int = int(uid)
        lvl_int = max(1, min(3, int(lvl)))
        member = inter.guild.get_member(uid_int)
        if member:
            remember_name(uid_int, member.display_name)
            name = member.display_name
        else:
            name = name_fallback(uid_int)
        lines.append(f"• **{name}** — Livello: **{lvl_int}**")

    page_size = 20
    pages = [lines[i:i + page_size] for i in range(0, len(lines), page_size)]
    for idx, page in enumerate(pages, start=1):
        embed = imperial_embed(
            f"REGISTRO LIVELLI (corrente) — Pag. {idx}/{len(pages)}",
            "\n".join(page),
            color=GOLD
        )
        await inter.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="pubblicalivelli", description="Pubblica nel canale lotteria la classifica livelli (solo admin).")
@admin_only_command()
async def slash_pubblicalivelli(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not require_guild(inter):
        await inter.followup.send("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    ch = await get_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato.", ephemeral=True)
        return

    wins = STATE.get("wins", {})
    if not wins:
        await inter.followup.send("📜 Nessun livello registrato al momento.", ephemeral=True)
        return

    items = sorted(wins.items(), key=lambda x: (-int(x[1]), int(x[0])))

    lines = []
    max_public = 30
    for uid, lvl in items[:max_public]:
        uid_int = int(uid)
        lvl_int = max(1, min(3, int(lvl)))
        member = inter.guild.get_member(uid_int)
        if member:
            remember_name(uid_int, member.display_name)
            name = member.display_name
        else:
            name = name_fallback(uid_int)
        lines.append(f"• **{name}** — Livello **{lvl_int}**")

    more = max(0, len(items) - max_public)
    desc = "📜 **Classifica Livelli (Top):**\n" + "\n".join(lines)
    if more > 0:
        desc += f"\n\n…e altri **{more}**."

    await ch.send(embed=imperial_embed("ALBO IMPERIALE — LIVELLI", desc, color=GOLD))
    save_state()
    await inter.followup.send("✅ Classifica pubblicata nel canale lotteria.", ephemeral=True)

# ---------- Comandi gestione stato ----------

@bot.tree.command(name="setedition", description="Imposta manualmente il numero di edizione (solo admin).")
@admin_only_command()
@app_commands.describe(numero="Numero edizione da impostare (>=1). È la PROSSIMA edizione che aprirai.")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if numero < 1:
        await inter.response.send_message("❌ L'edizione deve essere ≥ 1.", ephemeral=True)
        return
    STATE["edition"] = int(numero)
    save_state(force=True)
    await inter.response.send_message(f"✅ Prossima edizione impostata a **{numero}**.", ephemeral=True)

@bot.tree.command(name="setlivello", description="Imposta manualmente il livello (1–3) di un utente (solo admin).")
@admin_only_command()
@app_commands.describe(utente="Utente di cui modificare il livello", livello="Livello da impostare (1, 2 o 3)")
async def slash_setlivello(inter: discord.Interaction, utente: discord.Member, livello: int):
    if livello < 1 or livello > 3:
        await inter.response.send_message("❌ Il livello deve essere tra 1 e 3.", ephemeral=True)
        return

    uid = str(utente.id)
    remember_name(utente.id, utente.display_name)
    set_level(uid, livello)
    save_state(force=True)

    tot = int(STATE.get("victories", {}).get(uid, 0))
    cyc = int(STATE.get("cycles", {}).get(uid, 0))
    await inter.response.send_message(
        f"✅ Impostato **livello {livello}** per **{utente.display_name}** "
        f"(vittorie totali: {tot}, cicli: {cyc}).",
        ephemeral=True
    )

@bot.tree.command(name="rimuoviwinner", description="Rimuove un utente dalla memoria dei vincitori (solo admin).")
@admin_only_command()
@app_commands.describe(utente="Utente da cancellare dalla memoria vincitori")
async def slash_rimuoviwinner(inter: discord.Interaction, utente: discord.Member):
    clear_user_state(str(utente.id))
    save_state(force=True)
    await inter.response.send_message(
        f"🧹 **{utente.display_name}** rimosso dalla memoria dei vincitori.",
        ephemeral=True
    )

@bot.tree.command(name="removewinner", description="Alias di /rimuoviwinner (solo admin).")
@admin_only_command()
@app_commands.describe(utente="Utente da cancellare dalla memoria vincitori")
async def slash_removewinner(inter: discord.Interaction, utente: discord.Member):
    clear_user_state(str(utente.id))
    save_state(force=True)
    await inter.response.send_message(
        f"🧹 **{utente.display_name}** rimosso dalla memoria dei vincitori.",
        ephemeral=True
    )

# ---------- Comandi lotteria classica ----------

@bot.tree.command(name="apertura", description="Apre la lotteria CLASSICA (solo admin).")
@admin_only_command()
async def slash_apertura(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not require_guild(inter):
        await inter.followup.send("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    ed = int(STATE.get("edition", 1))
    await open_lottery(inter.guild, special=False)

    STATE["edition"] = ed + 1
    save_state(force=True)
    await inter.followup.send(f"📜 Apertura **classica** eseguita (edizione n°{ed}).", ephemeral=True)

@bot.tree.command(name="chiusura", description="Chiude e seleziona il vincitore (lotteria CLASSICA, solo admin).")
@admin_only_command()
async def slash_chiusura(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not require_guild(inter):
        await inter.followup.send("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    await close_and_pick(inter.guild, announce_now=False, special=False)
    STATE["last_close_week"] = week_key(now_tz())
    save_state(force=True)
    await inter.followup.send("🗝️ Chiusura **classica** eseguita.", ephemeral=True)

@bot.tree.command(name="annuncio", description="Annuncia il vincitore (lotteria CLASSICA, solo admin).")
@admin_only_command()
async def slash_annuncio(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not require_guild(inter):
        await inter.followup.send("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    ch = await get_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato.", ephemeral=True)
        return

    await post_winner_announcement_classic(ch, inter.guild)

    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    STATE["last_winner_ids"] = []
    STATE["last_winner_prev_levels"] = {}
    STATE["last_winner_reset_flags"] = {}
    save_state(force=True)
    await inter.followup.send("📣 Annuncio **classico** eseguito.", ephemeral=True)

# ---------- Comandi lotteria speciale ----------

@bot.tree.command(name="aperturaspeciale", description="Apre la lotteria SPECIALE (solo admin).")
@admin_only_command()
async def slash_aperturaspeciale(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not require_guild(inter):
        await inter.followup.send("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    ed = int(STATE.get("edition", 1))
    await open_lottery(inter.guild, special=True)

    STATE["edition"] = ed + 1
    save_state(force=True)
    await inter.followup.send(f"💎 Apertura **SPECIALE** eseguita (edizione n°{ed}).", ephemeral=True)

@bot.tree.command(name="chiusuraspeciale", description="Chiude e seleziona il vincitore (EDIZIONE SPECIALE, solo admin).")
@admin_only_command()
async def slash_chiusuraspeciale(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not require_guild(inter):
        await inter.followup.send("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    await close_and_pick(inter.guild, announce_now=False, special=True)
    STATE["last_close_week"] = week_key(now_tz())
    save_state(force=True)
    await inter.followup.send("🗝️ Chiusura **SPECIALE** eseguita.", ephemeral=True)

@bot.tree.command(name="annunciospeciale", description="Annuncia il vincitore (EDIZIONE SPECIALE, solo admin).")
@admin_only_command()
async def slash_annunciospeciale(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not require_guild(inter):
        await inter.followup.send("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return

    ch = await get_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato.", ephemeral=True)
        return

    lw = STATE.get("last_winner_id")
    winner_id = int(lw) if lw else None
    await post_winner_announcement_special(ch, inter.guild, winner_id)

    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    STATE["last_winner_ids"] = []
    STATE["last_special_prize"] = None
    save_state(force=True)
    await inter.followup.send("📣 Annuncio **SPECIALE** eseguito.", ephemeral=True)

# ---------- Comandi test automazione ----------

@bot.tree.command(name="testautoapertura", description="Testa l'apertura automatica tra X minuti (solo admin).")
@admin_only_command()
@app_commands.describe(minuti="Numero di minuti dopo cui eseguire l'apertura automatica secondo la modalità attiva")
async def slash_testautoapertura(inter: discord.Interaction, minuti: int):
    if not require_guild(inter):
        await inter.response.send_message("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return
    if minuti < 1:
        await inter.response.send_message("❌ I minuti devono essere almeno 1.", ephemeral=True)
        return

    async def _runner():
        special = (STATE.get("lottery_mode", "classic") == "special")
        ed = int(STATE.get("edition", 1))
        await open_lottery(inter.guild, special=special)
        STATE["edition"] = ed + 1
        save_state(force=True)

    schedule_test_task("open", minuti, _runner)
    await inter.response.send_message(
        f"⏰ Test apertura automatica programmato tra **{minuti} minuti** "
        f"(modalità {mode_label(STATE.get('lottery_mode', 'classic'))}).",
        ephemeral=True
    )

@bot.tree.command(name="testautochiusura", description="Testa la chiusura automatica tra X minuti (solo admin).")
@admin_only_command()
@app_commands.describe(minuti="Numero di minuti dopo cui eseguire la chiusura automatica secondo la modalità attiva")
async def slash_testautochiusura(inter: discord.Interaction, minuti: int):
    if not require_guild(inter):
        await inter.response.send_message("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return
    if minuti < 1:
        await inter.response.send_message("❌ I minuti devono essere almeno 1.", ephemeral=True)
        return

    async def _runner():
        special = (STATE.get("lottery_mode", "classic") == "special")
        await close_and_pick(inter.guild, announce_now=False, special=special)

    schedule_test_task("close", minuti, _runner)
    await inter.response.send_message(
        f"⏰ Test chiusura automatica programmato tra **{minuti} minuti** "
        f"(modalità {mode_label(STATE.get('lottery_mode', 'classic'))}).",
        ephemeral=True
    )

@bot.tree.command(name="testautoannuncio", description="Testa l'annuncio automatico tra X minuti (solo admin).")
@admin_only_command()
@app_commands.describe(minuti="Numero di minuti dopo cui eseguire l'annuncio automatico secondo la modalità attiva")
async def slash_testautoannuncio(inter: discord.Interaction, minuti: int):
    if not require_guild(inter):
        await inter.response.send_message("❌ Questo comando funziona solo dentro un server.", ephemeral=True)
        return
    if minuti < 1:
        await inter.response.send_message("❌ I minuti devono essere almeno 1.", ephemeral=True)
        return

    async def _runner():
        ch = await get_lottery_channel(inter.guild)
        if not ch:
            return

        special = (STATE.get("lottery_mode", "classic") == "special")
        if special:
            lw = STATE.get("last_winner_id")
            winner_id = int(lw) if lw else None
            await post_winner_announcement_special(ch, inter.guild, winner_id)
        else:
            await post_winner_announcement_classic(ch, inter.guild)

    schedule_test_task("announce", minuti, _runner)
    await inter.response.send_message(
        f"⏰ Test annuncio automatico programmato tra **{minuti} minuti** "
        f"(modalità {mode_label(STATE.get('lottery_mode', 'classic'))}).",
        ephemeral=True
    )

@bot.tree.command(name="annullatestauto", description="Annulla tutti i test automatici programmati (solo admin).")
@admin_only_command()
async def slash_annullatestauto(inter: discord.Interaction):
    count = cancel_all_test_tasks()
    await inter.response.send_message(
        f"🛑 Test automatici annullati: **{count}**.",
        ephemeral=True
    )

# ---------- Avvio ----------

@bot.event
async def setup_hook():
    try:
        if not SYNC_ON_START:
            print("ℹ️ Sync slash automatico DISATTIVATO (SYNC_ON_START=false).")
            return

        if SYNC_GUILD_ID:
            guild_obj = discord.Object(id=SYNC_GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"✅ Slash sync su guild {SYNC_GUILD_ID}: {len(synced)} comandi.")
        else:
            synced = await bot.tree.sync()
            print(f"✅ Slash sync globale: {len(synced)} comandi.")
    except Exception as e:
        print("Errore sync comandi:", e)

if __name__ == "__main__":
    load_state()
    bot.run(TOKEN)
    
