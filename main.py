# === IMPERIVM Lottery Bot — main.py (Render / discord.py 2.x) ===
# Stato persistito su GitHub Gist (nessun automatismo settimanale)
# Edizione speciale SOLO quando usi /aperturaspeciale
#
# ✅ MODIFICHE APPLICATE (NUOVE):
# - Premio calcolato SEMPRE sul livello PRIMA della vittoria (prev level), poi aggiornamento livello.
# - Livelli: default = Livello 1 se non esisti in STATE["wins"]
# - Progressione CLASSICA: 1 -> 2 -> 3 -> 1 (reset immediato quando vinci da Livello 3)
# - Livello 2 = SOLO 250.000 Kama (niente scudo)
# - STRENGTH:
#   * premio x2 sul livello PRIMA della vittoria
#   * livello NON avanza per L1/L2
#   * se vinci da L3 => reset a L1 (anti-exploit)
#   * aggiorna last_win_iso sempre
# - SPECIALE:
#   * premio casuale 600k/800k/1M, NESSUN bonus fedeltà
#   * NON cambia livelli/vittorie/cicli
#   * aggiorna last_win_iso (per sorte truccata)
#   * il premio speciale viene "bloccato" in chiusura (non ricalcolato all'annuncio)
# - /mostralivelli e /pubblicalivelli: mostrano solo chi è registrato in wins
#
# ✅ WATERMARK (NUOVO):
# - Allego imperivm_watermark.png a ogni embed e lo imposto come immagine embed
# - Fallback automatico: se il file non esiste/errore, invia solo embed
#
# ✅ COLORI EMBED LOTTERIA (NUOVO):
# - CLASSICA: cornice embed cambia colore in base all'elemento attivo:
#   * INT = rosso, AGI = verde, CHA = blu, STR = marrone
# - SPECIALE e comandi admin: restano GOLD

import os
import json
import random
from datetime import datetime
from threading import Thread
from typing import Optional, List, Dict, Tuple
import urllib.request
import urllib.error

import pytz
import discord
from discord.ext import commands
from discord import app_commands

# --- Mini web server per Render (healthcheck) ---
from flask import Flask
try:
    from waitress import serve
    USE_WAITRESS = True
except Exception:
    USE_WAITRESS = False

app = Flask(__name__)

@app.get("/")
def index():
    return "IMPERIVM Lottery Bot è vivo 📜"

def start_web_server():
    port = int(os.getenv("PORT", "8080"))
    if USE_WAITRESS:
        Thread(target=lambda: serve(app, host="0.0.0.0", port=port), daemon=True).start()
    else:
        Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()

# ---------- Config ----------

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True
INTENTS.reactions = True

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ Manca DISCORD_TOKEN nelle Environment Variables di Render.")

LOTTERY_CHANNEL_ID = int(os.getenv("LOTTERY_CHANNEL_ID", "0"))

# Admin extra (facoltativo). Se vuoto valgono i permessi admin Discord.
ADMIN_IDS = set()
_env_admins = os.getenv("ADMINS", "").strip()
if _env_admins:
    try:
        ADMIN_IDS = {int(x) for x in _env_admins.replace(" ", "").split(",") if x}
    except Exception:
        ADMIN_IDS = set()

TZ = pytz.timezone(os.getenv("TZ", "Europe/Rome"))

# Colore default (admin/speciale)
GOLD = discord.Color.from_str("#DAA520")

# --- Colori elementi (cornice embed lotteria classica) ---
COLOR_INT = discord.Color.from_str("#C0392B")  # rosso
COLOR_AGI = discord.Color.from_str("#27AE60")  # verde
COLOR_CHA = discord.Color.from_str("#2980B9")  # blu
COLOR_STR = discord.Color.from_str("#8E5A2B")  # marrone

# --- Watermark file (repo root) ---
WATERMARK_PATH = os.getenv("WATERMARK_PATH", "imperivm_watermark.png")
WATERMARK_FILENAME = os.path.basename(WATERMARK_PATH)

# --- Gist persistence ---
GIST_ID = os.getenv("GIST_ID") or ""
GIST_FILENAME = os.getenv("GIST_FILENAME") or "imperivm_state.json"
GIST_TOKEN = os.getenv("GIST_TOKEN") or ""

def _gist_api_url() -> str:
    return f"https://api.github.com/gists/{GIST_ID}"

def _gist_headers() -> Dict[str, str]:
    hdr = {"Accept": "application/vnd.github+json"}
    if GIST_TOKEN:
        hdr["Authorization"] = f"Bearer {GIST_TOKEN}"
    return hdr

STATE: Dict = {}

DEFAULT_STATE = {
    "schema": "imperivm.lottery.v1",
    "edition": 1,
    "open_message_id": None,

    # Livelli: salviamo il LIVELLO (1..3). Se mancante => default = 1.
    "wins": {},

    # Statistiche (solo CLASSICA, non speciale)
    "victories": {},
    "cycles": {},

    # Per sorte truccata (aggiornato anche da Strength e Speciale)
    "last_win_iso": {},

    "last_winner_id": None,
    "last_winner_ids": [],

    # ✅ livello "prima della vittoria" usato per calcolare premio (per annuncio corretto)
    "last_winner_prev_levels": {},   # {uid: prev_level}
    # ✅ flag reset per Strength (solo informativo in annuncio)
    "last_winner_reset_flags": {},   # {uid: bool}

    # ✅ premio speciale bloccato in chiusura
    "last_special_prize": None,      # int (kama)

    # Modificatori
    "weekly_modifier": None,
    "weekly_modifier_week": None,

    # Override test
    "test_override_modifier": None,

    # Modificatore effettivamente usato nell'ultima apertura classica
    "active_modifier": None,

    # Cache nomi
    "names": {},

    # compat
    "last_open_week": None,
    "last_close_week": None,
    "last_announce_week": None,
}

# ---------- Helpers Gist ----------

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

        # normalizza tipi
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

        return parsed
    except Exception:
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
    save_state_to_gist(STATE)

def save_state():
    save_state_to_gist(STATE)

# ---------- Bot ----------
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------- Utility ----------

def _slash_admin_guard(inter: discord.Interaction) -> bool:
    if ADMIN_IDS and inter.user.id in ADMIN_IDS:
        return True
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

async def get_lottery_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    if LOTTERY_CHANNEL_ID:
        ch = guild.get_channel(LOTTERY_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    return guild.text_channels[0] if guild.text_channels else None

def imperial_embed(title: str, desc: str, color: discord.Color = GOLD) -> discord.Embed:
    nice_title = "📜  " + title + "  📜"
    e = discord.Embed(title=nice_title, description=desc, color=color)
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
    if not display_name:
        return
    STATE.setdefault("names", {})
    STATE["names"][str(uid)] = display_name

def name_fallback(uid: int) -> str:
    return STATE.get("names", {}).get(str(uid), f"utente {uid}")

# ---------- WATERMARK SENDERS ----------

def _attach_watermark(embed: discord.Embed) -> Tuple[discord.Embed, Optional[discord.File]]:
    """
    Ritorna (embed, file) pronto per send.
    Se watermark mancante/errore -> (embed, None).
    """
    try:
        if WATERMARK_PATH and os.path.exists(WATERMARK_PATH):
            f = discord.File(WATERMARK_PATH, filename=WATERMARK_FILENAME)
            embed.set_image(url=f"attachment://{WATERMARK_FILENAME}")
            return embed, f
    except Exception as e:
        print("Errore watermark:", e)
    return embed, None

async def send_imperial(channel: discord.abc.Messageable, embed: discord.Embed):
    """
    Invia embed con watermark allegato (se disponibile).
    """
    embed, f = _attach_watermark(embed)
    if f:
        return await channel.send(embed=embed, file=f)
    return await channel.send(embed=embed)

async def followup_send_imperial(inter: discord.Interaction, embed: discord.Embed, ephemeral: bool = False):
    """
    Invia embed con watermark in followup.
    """
    embed, f = _attach_watermark(embed)
    if f:
        return await inter.followup.send(embed=embed, file=f, ephemeral=ephemeral)
    return await inter.followup.send(embed=embed, ephemeral=ephemeral)

# ---------- Modificatori settimanali (SOLO CLASSICA) ----------

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
    }.get(mod or "", "NESSUNO")

def lottery_color_for_modifier(mod: Optional[str]) -> discord.Color:
    """
    Colore cornice embed per la LOTTERIA CLASSICA.
    """
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
    return ["⚙️ **MODIFICATORE ATTIVO:** NESSUNO"]

def pick_weekly_modifier() -> str:
    """
    Chance più rara: -20% rispetto alle altre.
    Pesi:
    - INT 25
    - AGI 25
    - STR 25
    - CHA 20
    """
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

# ---------- Livelli (LOGICA CORRETTA) ----------

def get_level(uid: str) -> int:
    """
    Livello attuale:
    - se non esiste in wins => 1
    - se esiste => clamp 1..3
    """
    try:
        v = int(STATE.get("wins", {}).get(uid, 1))
    except Exception:
        v = 1
    if v < 1:
        v = 1
    if v > 3:
        v = 3
    return v

def set_level(uid: str, lvl: int):
    lvl = int(lvl)
    if lvl < 1:
        lvl = 1
    if lvl > 3:
        lvl = 3
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
    """
    Dopo aver pagato il premio sul prev_lvl:
    1->2, 2->3, 3->1 (cycle reset su 3->1)
    Ritorna (new_lvl, did_cycle_reset)
    """
    if prev_lvl == 1:
        new = 2
        cycle = False
    elif prev_lvl == 2:
        new = 3
        cycle = False
    else:
        new = 1
        cycle = True
    set_level(uid, new)
    return new, cycle

def apply_classic_win_after_prize(uid: str, prev_lvl: int):
    """
    CLASSICA (normale/int/agi/chance):
    - premio calcolato su prev_lvl (gestito in annuncio)
    - qui aggiorniamo SOLO lo stato post-vittoria
    """
    _, cycle = advance_level_after_classic_win(uid, prev_lvl)

    STATE.setdefault("victories", {})
    STATE.setdefault("cycles", {})
    STATE.setdefault("last_win_iso", {})

    STATE["victories"][uid] = int(STATE["victories"].get(uid, 0)) + 1
    if cycle:
        STATE["cycles"][uid] = int(STATE["cycles"].get(uid, 0)) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

def apply_strength_win_after_prize(uid: str, prev_lvl: int) -> Tuple[int, bool]:
    """
    STRENGTH:
    - premio x2 calcolato su prev_lvl (gestito in annuncio)
    - livello non avanza per L1/L2
    - se prev_lvl == 3 => reset a L1
    Ritorna (new_lvl, did_reset)
    """
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

# ---------- Testi & messaggi ----------

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
        "💎 **Borsa dei Premi Speciale (casuali):** 600.000 / 800.000 / 1.000.000 Kama",
        "",
        f"**Edizione n°{edition} (SPECIALE)**",
        "",
        "🕗 *Annuncio del vincitore alle **08:00** di giovedì.*",
    ]

async def post_open_message(channel: discord.TextChannel, special: bool):
    edition = STATE["edition"]

    if special:
        lines = _special_open_lines(edition)
        embed = imperial_embed("LOTTERIA IMPERIVM – APERTA", "\n".join(lines), color=GOLD)
    else:
        mod = get_effective_modifier_for_open()
        STATE["active_modifier"] = mod
        save_state()
        lines = _classic_open_lines(edition, mod)
        embed = imperial_embed(
            "LOTTERIA IMPERIVM – APERTA",
            "\n".join(lines),
            color=lottery_color_for_modifier(mod),
        )

    msg = await send_imperial(channel, embed)

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
        await send_imperial(channel, imperial_embed("LOTTERIA IMPERIVM – CHIUSA", desc, color=color))
        return

    desc = (
        "I sigilli sono stati chiusi.\n"
        "I nomi sono stati consegnati al Fato.\n\n"
    )
    if names_preview:
        desc += names_preview + "\n\n"

    if special:
        desc += "🕗 **Annuncio del vincitore alle 08:00 di giovedì**."
        await send_imperial(channel, imperial_embed("LOTTERIA IMPERIVM – CHIUSA", desc, color=GOLD))
        return

    mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")
    desc += f"⚙️ Modificatore attivo: **{modifier_label(mod)}**\n\n"
    desc += "🕗 **Annuncio del vincitore alle 08:00 di giovedì**."
    await send_imperial(
        channel,
        imperial_embed("LOTTERIA IMPERIVM – CHIUSA", desc, color=lottery_color_for_modifier(mod)),
    )

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
        await send_imperial(channel, imperial_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc, color=color))
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
        if prev_lvl < 1: prev_lvl = 1
        if prev_lvl > 3: prev_lvl = 3

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
            await send_imperial(channel, imperial_embed(title, header + body, color=color))
            return

        if mod == MOD_AGI:
            body = (
                "🌪️ **Elemento Agility attivo**\n"
                "Bonus probabilità livello più basso applicato\n\n"
                f"💰 **Ricompensa:** {base_txt}"
            )
            await send_imperial(channel, imperial_embed(title, header + body, color=color))
            return

        if mod == MOD_CHA and len(ids) == 2:
            body = (
                "🍀 **Elemento Chance attivo**\n\n"
                f"💰 **Ricompensa:** {base_txt}"
            )
            await send_imperial(channel, imperial_embed(title, header + body, color=color))
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
            await send_imperial(channel, imperial_embed(title, header + body, color=color))
            return

        await send_imperial(channel, imperial_embed(title, header + f"💰 **Ricompensa:** {base_txt}", color=color))

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
        await send_imperial(channel, imperial_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc, color=GOLD))
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
        f"💎 **Ricompensa Speciale:** {fmt_kama(premio)}\n\n"
        "Questa edizione **non modifica** i livelli.\n"
        "Che la fortuna continui a sorriderti."
    )
    await send_imperial(channel, imperial_embed("ESTRAZIONE UFFICIALE – EDIZIONE SPECIALE", desc, color=GOLD))

# ---------- Partecipanti ----------

async def collect_participants(msg: discord.Message) -> List[int]:
    ids: List[int] = []
    try:
        await msg.fetch()
    except Exception:
        pass
    for r in msg.reactions:
        if str(r.emoji) == "✅":
            users = [u async for u in r.users()]
            for u in users:
                if not u.bot:
                    ids.append(u.id)
    return list(dict.fromkeys(ids))

# ---------- Sorte truccata / Agility weights ----------

def _time_weight_from_last_win(uid: str) -> float:
    iso = STATE.get("last_win_iso", {}).get(uid)
    if not iso:
        return 2.0
    try:
        last = datetime.fromisoformat(iso)
        delta_days = max(0.0, (now_tz() - last).total_seconds() / 86400.0)
        return 1.0 + min(delta_days / 7.0, 2.0)  # 1..3
    except Exception:
        return 1.5

def _agility_bonus_factor(uid: str, min_level: int) -> float:
    lvl = get_level(uid)
    if lvl == min_level:
        return 1.5
    return 1.0

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

# ---------- Close / Pick ----------

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
    mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")

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

async def open_lottery(guild: discord.Guild, special: bool = False):
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

# ---------- Eventi ----------

@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass
    print(f"✅ Bot online come {bot.user} — prossima edizione: {STATE.get('edition')}")

# ---------- Slash commands (tutti admin-only e invisibili ai non-admin) ----------

def admin_only_command():
    def deco(func):
        func = app_commands.guild_only()(func)
        func = app_commands.default_permissions(administrator=True)(func)
        return func
    return deco

# --- TEST MODIFICATORI ---

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
    save_state()
    await inter.response.send_message(
        f"🧪 TEST attivo: prossimo /apertura userà **{modifier_label(elemento.value)}**.\n"
        f"Per tornare casuale: **/testoff**",
        ephemeral=True
    )

@bot.tree.command(name="testoff", description="Disattiva il test override e torna al casuale settimanale (solo admin).")
@admin_only_command()
async def slash_testoff(inter: discord.Interaction):
    STATE["test_override_modifier"] = None
    save_state()
    await inter.response.send_message("✅ Test disattivato. Da ora /apertura usa il modificatore **casuale settimanale**.", ephemeral=True)

# --- Utility ---

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

# --- Livelli (admin-only) ---

@bot.tree.command(name="mostralivelli", description="Mostra i livelli registrati (solo admin).")
@admin_only_command()
async def slash_mostralivelli(inter: discord.Interaction):
    if not inter.guild:
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
        lvl_int = int(lvl)
        member = inter.guild.get_member(uid_int)
        if member:
            remember_name(uid_int, member.display_name)
            name = member.display_name
        else:
            name = name_fallback(uid_int)
        if lvl_int < 1: lvl_int = 1
        if lvl_int > 3: lvl_int = 3
        lines.append(f"• **{name}** — Livello: **{lvl_int}**")

    PAGE_SIZE = 20
    pages = [lines[i:i+PAGE_SIZE] for i in range(0, len(lines), PAGE_SIZE)]
    for idx, page in enumerate(pages, start=1):
        embed = imperial_embed(
            f"REGISTRO LIVELLI (corrente) — Pag. {idx}/{len(pages)}",
            "\n".join(page),
            color=GOLD
        )
        await followup_send_imperial(inter, embed, ephemeral=True)

@bot.tree.command(name="pubblicalivelli", description="Pubblica nel canale lotteria la classifica livelli (solo admin).")
@admin_only_command()
async def slash_pubblicalivelli(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    if not inter.guild:
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
    MAX_PUBLIC = 30
    for uid, lvl in items[:MAX_PUBLIC]:
        uid_int = int(uid)
        lvl_int = int(lvl)
        member = inter.guild.get_member(uid_int)
        if member:
            remember_name(uid_int, member.display_name)
            name = member.display_name
        else:
            name = name_fallback(uid_int)
        if lvl_int < 1: lvl_int = 1
        if lvl_int > 3: lvl_int = 3
        lines.append(f"• **{name}** — Livello **{lvl_int}**")

    more = max(0, len(items) - MAX_PUBLIC)
    desc = "📜 **Classifica Livelli (Top):**\n" + "\n".join(lines)
    if more > 0:
        desc += f"\n\n…e altri **{more}**."

    embed = imperial_embed("ALBO IMPERIALE — LIVELLI", desc, color=GOLD)
    await send_imperial(ch, embed)
    save_state()
    await inter.followup.send("✅ Classifica pubblicata nel canale lotteria.", ephemeral=True)

# --- Admin gestione stato ---

@bot.tree.command(name="setedition", description="Imposta manualmente il numero di edizione (solo admin).")
@admin_only_command()
@app_commands.describe(numero="Numero edizione da impostare (>=1). È la PROSSIMA edizione che aprirai.")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if numero < 1:
        await inter.response.send_message("❌ L'edizione deve essere ≥ 1.", ephemeral=True)
        return
    STATE["edition"] = int(numero)
    save_state()
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
    save_state()
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
    uid = str(utente.id)
    STATE.get("wins", {}).pop(uid, None)
    STATE.get("victories", {}).pop(uid, None)
    STATE.get("cycles", {}).pop(uid, None)
    STATE.get("last_win_iso", {}).pop(uid, None)
    STATE.get("names", {}).pop(uid, None)
    save_state()
    await inter.response.send_message(
        f"🧹 **{utente.display_name}** rimosso dalla memoria dei vincitori.",
        ephemeral=True
    )

# --- Lotteria CLASSICA ---

@bot.tree.command(name="apertura", description="Apre la lotteria CLASSICA (solo admin).")
@admin_only_command()
async def slash_apertura(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    ed = int(STATE.get("edition", 1))

    await open_lottery(inter.guild, special=False)

    STATE["edition"] = ed + 1
    save_state()
    await inter.followup.send(f"📜 Apertura **classica** eseguita (edizione n°{ed}).", ephemeral=True)

@bot.tree.command(name="chiusura", description="Chiude e seleziona il vincitore (lotteria CLASSICA, solo admin).")
@admin_only_command()
async def slash_chiusura(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    await close_and_pick(inter.guild, announce_now=False, special=False)
    STATE["last_close_week"] = week_key(now_tz())
    save_state()
    await inter.followup.send("🗝️ Chiusura **classica** eseguita.", ephemeral=True)

@bot.tree.command(name="annuncio", description="Annuncia il vincitore (lotteria CLASSICA, solo admin).")
@admin_only_command()
async def slash_annuncio(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
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
    save_state()
    await inter.followup.send("📣 Annuncio **classico** eseguito.", ephemeral=True)

# --- SPECIALE ---

@bot.tree.command(name="aperturaspeciale", description="Apre la lotteria **Special Edition** (solo admin).")
@admin_only_command()
async def slash_aperturaspeciale(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    ed = int(STATE.get("edition", 1))

    await open_lottery(inter.guild, special=True)

    STATE["edition"] = ed + 1
    save_state()
    await inter.followup.send(f"💎 Apertura **SPECIALE** eseguita (edizione n°{ed}).", ephemeral=True)

@bot.tree.command(name="chiusuraspeciale", description="Chiude e seleziona il vincitore (EDIZIONE SPECIALE, solo admin).")
@admin_only_command()
async def slash_chiusuraspeciale(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    await close_and_pick(inter.guild, announce_now=False, special=True)
    STATE["last_close_week"] = week_key(now_tz())
    save_state()
    await inter.followup.send("🗝️ Chiusura **SPECIALE** eseguita.", ephemeral=True)

@bot.tree.command(name="annunciospeciale", description="Annuncia il vincitore (EDIZIONE SPECIALE, solo admin).")
@admin_only_command()
async def slash_annunciospeciale(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
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
    save_state()
    await inter.followup.send("📣 Annuncio **SPECIALE** eseguito.", ephemeral=True)

# ---------- Avvio ----------

@bot.event
async def setup_hook():
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Errore sync comandi:", e)

if __name__ == "__main__":
    load_state()
    start_web_server()
    bot.run(TOKEN)
