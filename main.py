# === IMPERIVM Lottery Bot — main.py (Render / discord.py 2.x) ===
# Stato persistito su GitHub Gist (nessun automatismo settimanale)
# Edizione speciale SOLO quando usi /aperturaspeciale
# Premi speciali: 600k / 800k / 1M (+200k se il vincitore ha già vinto in passato).

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
GOLD = discord.Color.from_str("#DAA520")

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
    # ATTENZIONE: edition = PROSSIMA edizione da aprire
    "edition": 1,
    "open_message_id": None,     # id messaggio di apertura (solo cache)
    "wins": {},                  # {uid: 1..3}, reset a 1 dopo 3
    "victories": {},             # {uid: tot vittorie storiche}
    "cycles": {},                # {uid: volte raggiunto L3 (reset)}
    "last_win_iso": {},          # {uid: ISO timestamp ultima vittoria}
    "last_winner_id": None,      # compat: ultimo vincitore scelto (classica/speciale singola)
    "last_winner_ids": [],       # lista vincitori (Chance -> 2)

    # Modificatori
    "weekly_modifier": None,      # "INT"|"CHA"|"AGI"|"STR" per CLASSICA
    "weekly_modifier_week": None, # week_key quando è stato scelto il modificatore

    # NUOVO: override test (non cambia la casualità settimanale, è solo forzatura manuale)
    "test_override_modifier": None,   # "INT"|"CHA"|"AGI"|"STR"|None
    # NUOVO: modificatore effettivamente usato nell'ultima apertura classica (serve per coerenza close/announce)
    "active_modifier": None,          # "INT"|"CHA"|"AGI"|"STR"|None

    # Cache nomi
    "names": {},                 # {uid: ultimo display_name visto} fallback per bug ID

    # compat
    "last_open_week": None,
    "last_close_week": None,
    "last_announce_week": None,
}

# ---------- Helpers Gist ----------

def load_state_from_gist() -> Dict:
    """Legge lo stato dal Gist. Se non esiste/errore, restituisce DEFAULT_STATE."""
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
        # merge chiavi mancanti
        for k, v in DEFAULT_STATE.items():
            if k not in parsed:
                parsed[k] = v
        # normalizza tipi
        if not isinstance(parsed.get("last_winner_ids"), list):
            parsed["last_winner_ids"] = []
        if not isinstance(parsed.get("names"), dict):
            parsed["names"] = {}
        return parsed
    except Exception:
        return DEFAULT_STATE.copy()

def save_state_to_gist(state: Dict):
    """Scrive lo stato nel file del Gist."""
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

def is_admin(ctx_or_member) -> bool:
    m = ctx_or_member.author if hasattr(ctx_or_member, "author") else ctx_or_member
    if ADMIN_IDS and m.id in ADMIN_IDS:
        return True
    perms = getattr(m, "guild_permissions", None)
    return bool(perms and perms.administrator)

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

def level_from_wins(wins: int) -> int:
    if wins <= 0:
        return 0
    return min(wins, 3)

def golden_embed(title: str, desc: str) -> discord.Embed:
    nice_title = "📜  " + title + "  📜"
    e = discord.Embed(title=nice_title, description=desc, color=GOLD)
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
            "Premio raddoppiato — nessun avanzamento di livello",
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
    """Assicura che per questa settimana ISO ci sia un modificatore classico scelto (CASUALE)."""
    wk = week_key(now_tz())
    if STATE.get("weekly_modifier_week") != wk or STATE.get("weekly_modifier") not in {MOD_INT, MOD_CHA, MOD_AGI, MOD_STR}:
        STATE["weekly_modifier"] = pick_weekly_modifier()
        STATE["weekly_modifier_week"] = wk
        save_state()

def get_effective_modifier_for_open() -> str:
    """
    Ritorna il modificatore da usare in apertura:
    - Se c'è test_override => usa quello
    - Altrimenti => usa weekly casuale
    """
    override = STATE.get("test_override_modifier")
    if override in {MOD_INT, MOD_CHA, MOD_AGI, MOD_STR}:
        return override
    ensure_weekly_modifier()
    return STATE["weekly_modifier"]

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
        "2️⃣ Livello 2 → Scudo di Gilda / 250.000 Kama",
        "3️⃣ Livello 3 → 500.000 Kama *(reset livelli)*",
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
        "✨ **Bonus Fedeltà:** se hai già vinto in passato → **+200.000 Kama** al premio estratto.",
        "",
        f"**Edizione n°{edition} (SPECIALE)**",
        "",
        "🕗 *Annuncio del vincitore alle **08:00** di giovedì.*",
    ]

async def post_open_message(channel: discord.TextChannel, special: bool):
    edition = STATE["edition"]

    if special:
        lines = _special_open_lines(edition)
    else:
        mod = get_effective_modifier_for_open()
        STATE["active_modifier"] = mod  # salva quello effettivo usato
        save_state()
        lines = _classic_open_lines(edition, mod)

    embed = golden_embed("LOTTERIA IMPERIVM – APERTA", "\n".join(lines))
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
    else:
        desc = (
            "I sigilli sono stati chiusi.\n"
            "I nomi sono stati consegnati al Fato.\n\n"
        )
        if names_preview:
            desc += names_preview + "\n\n"
        if not special:
            mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")
            desc += f"⚙️ Modificatore attivo: **{modifier_label(mod)}**\n\n"
        desc += "🕗 **Annuncio del vincitore alle 08:00 di giovedì**."

    await channel.send(embed=golden_embed("LOTTERIA IMPERIVM – CHIUSA", desc))

def _classic_prize_amount(lvl: int) -> int:
    if lvl == 1:
        return 100_000
    elif lvl == 2:
        return 250_000
    else:
        return 500_000

def _classic_prize_text(lvl: int) -> str:
    if lvl == 1:
        return "100.000 Kama"
    elif lvl == 2:
        return "Scudo di Gilda *(se già posseduto → 250.000 Kama)*"
    else:
        return "500.000 Kama *(reset dei livelli)*"

def _special_compute_prize(uid_str: str) -> str:
    base = random.choice([600_000, 800_000, 1_000_000])
    bonus = 200_000 if STATE.get("victories", {}).get(uid_str, 0) > 0 else 0
    total = base + bonus
    parts = [fmt_kama(total)]
    if bonus:
        parts.append("(+200k bonus fedeltà)")
    return " ".join(parts)

def _preview_next_wins(prev_wins: int) -> Tuple[int, bool]:
    new = prev_wins + 1
    reset = False
    if new > 3:
        new = 1
        reset = True
    return new, reset

async def post_winner_announcement_classic(channel: discord.TextChannel, guild: discord.Guild):
    ids: List[int] = STATE.get("last_winner_ids") or []
    mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")

    if not ids:
        desc = (
            "Cittadini dell’Impero,\n"
            "i sigilli sono stati spezzati… ma nessun nome è stato scelto.\n"
            "Riproveremo mercoledì prossimo. 🕯️"
        )
        await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))
        return

    async def send_one(idx: int, uid: int):
        member = guild.get_member(uid)
        if member:
            remember_name(uid, member.display_name)
            mention = member.mention
        else:
            mention = f"@{name_fallback(uid)}"

        prev_wins = int(STATE.get("wins", {}).get(str(uid), 0))

        if mod == MOD_STR:
            shown_lvl = level_from_wins(prev_wins)
            shown_state = f"{shown_lvl}/3" if shown_lvl else "0/3"

            hypo_wins, _ = _preview_next_wins(prev_wins)
            hypo_lvl = level_from_wins(hypo_wins)

            base_amt = _classic_prize_amount(hypo_lvl)
            final_amt = base_amt * 2

            extra_block = (
                "💪 **Elemento Strength attivo**\n\n"
                f"💰 **Ricompensa finale: {fmt_kama(final_amt)}**\n"
                "Premio raddoppiato — livello **NON incrementato**"
            )
            title = "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM"
            header = (
                "Cittadini dell’Impero,\n"
                "i sigilli sono stati spezzati e il Fato ha pronunciato il suo verdetto.\n\n"
                f"👑 **Vincitore:** {mention}\n"
                f"🎖️ **Livello attuale:** {shown_lvl}\n"
                f"📊 **Stato progressione:** {shown_state}\n\n"
            )
            await channel.send(embed=golden_embed(title, header + extra_block))
            return

        curr_wins = int(STATE.get("wins", {}).get(str(uid), 0))
        lvl = level_from_wins(curr_wins)
        stato = f"{lvl}/3" if lvl else "0/3"
        title = "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM"

        header = (
            "Cittadini dell’Impero,\n"
            "i sigilli sono stati spezzati e il Fato ha pronunciato il suo verdetto.\n\n"
        )

        if mod == MOD_CHA and len(ids) == 2:
            title = f"ESTRAZIONE {'I' if idx == 1 else 'II'} – LOTTERIA IMPERIVM"
            header = (
                f"👑 **Vincitore {'I' if idx == 1 else 'II'}:** {mention}\n"
                f"🎖️ **Livello:** {lvl}\n"
            )
        else:
            header += (
                f"👑 **Vincitore:** {mention}\n"
                f"🎖️ **Livello attuale:** {lvl}\n"
                f"📊 **Stato progressione:** {stato}\n\n"
            )

        base_text = _classic_prize_text(lvl)
        base_amt = _classic_prize_amount(lvl)

        if mod == MOD_INT:
            final_amt = int(round(base_amt * 1.30))
            extra_block = (
                "🧠 **Elemento Intelligence attivo**\n\n"
                f"💰 **Ricompensa finale: {fmt_kama(final_amt)}**\n"
                "(+30% già applicato)"
            )
            await channel.send(embed=golden_embed(title, header + extra_block))
            return

        if mod == MOD_AGI:
            extra_block = (
                "🌪️ **Elemento Agility attivo**\n"
                "Bonus probabilità livello più basso applicato\n\n"
                f"💰 **Ricompensa:** {base_text}"
            )
            await channel.send(embed=golden_embed(title, header + extra_block))
            return

        if mod == MOD_CHA and len(ids) == 2:
            extra_block = (
                "🍀 **Elemento Chance attivo**\n\n"
                f"💰 **Ricompensa:** {base_text}"
            )
            await channel.send(embed=golden_embed(title, header + extra_block))
            return

        await channel.send(embed=golden_embed(title, header + f"💰 **Ricompensa:** {base_text}"))

    if mod == MOD_CHA and len(ids) == 2:
        await send_one(1, ids[0])
        await send_one(2, ids[1])
        return

    await send_one(1, ids[0])

async def post_winner_announcement_special(channel: discord.TextChannel, member: Optional[discord.Member]):
    if member is None:
        desc = (
            "I sigilli sono stati spezzati, ma stavolta il fato è rimasto muto.\n"
            "Nessun nome scolpito negli annali: riproveremo mercoledì prossimo. 🕯️"
        )
        await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))
        return

    uid = str(member.id)
    remember_name(member.id, member.display_name)
    lvl = level_from_wins(int(STATE["wins"].get(uid, 0)))
    stato = f"{lvl}/3" if lvl else "0/3"

    premio = _special_compute_prize(uid)
    title = "ESTRAZIONE UFFICIALE – EDIZIONE SPECIALE"
    desc = (
        "Cittadini dell’Impero, il sigillo dorato è stato infranto.\n"
        "Tra pergamene e ceralacca, il nome inciso negli annali è stato scelto.\n\n"
        f"👑 **Vincitore:** {member.mention}\n"
        f"⚔️ **Stato livello classico:** {stato}\n"
        f"💎 **Ricompensa Speciale:** {premio}\n\n"
        "Che la fortuna continui a sorriderti. La prossima chiamata dell’Aquila Imperiale\n"
        "risuonerà **mercoledì a mezzanotte**. Presentatevi senza timore."
    )
    await channel.send(embed=golden_embed(title, desc))

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

def _bump_win_counters(uid: str):
    prev = int(STATE["wins"].get(uid, 0))
    new, reset = _preview_next_wins(prev)
    STATE["wins"][uid] = new
    STATE["victories"][uid] = int(STATE["victories"].get(uid, 0)) + 1
    if reset:
        STATE["cycles"][uid] = int(STATE["cycles"].get(uid, 0)) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

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
    lvl = level_from_wins(int(STATE.get("wins", {}).get(uid, 0)))
    if lvl == min_level:
        return 1.5
    return 1.0

def weighted_pick(participants: List[int], mod: Optional[str]) -> int:
    if not participants:
        raise ValueError("participants empty")

    min_lvl = 99
    for uid_int in participants:
        uid = str(uid_int)
        lvl = level_from_wins(int(STATE.get("wins", {}).get(uid, 0)))
        min_lvl = min(min_lvl, lvl)
    if min_lvl == 99:
        min_lvl = 0

    weights: List[float] = []
    for uid_int in participants:
        uid = str(uid_int)
        w = _time_weight_from_last_win(uid)
        if mod == MOD_AGI:
            w *= _agility_bonus_factor(uid, min_lvl)
        weights.append(max(0.1, float(w)))

    return random.choices(participants, weights=weights, k=1)[0]

async def _close_and_pick_common(guild: discord.Guild, special: bool):
    channel = await get_lottery_channel(guild)
    if not channel:
        return None, [], "", None

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
    mod = STATE.get("active_modifier") or STATE.get("weekly_modifier")  # usa SEMPRE quello dell’apertura

    if participants:
        if special:
            win_id = random.choice(participants)
            winners = [win_id]
            _bump_win_counters(str(win_id))
            save_state()
        else:
            if mod == MOD_CHA and len(participants) >= 2:
                first = weighted_pick(participants, mod=None)
                remaining = [x for x in participants if x != first]
                second = weighted_pick(remaining, mod=None)
                winners = [first, second]
                _bump_win_counters(str(first))
                _bump_win_counters(str(second))
                save_state()
            else:
                win_id = weighted_pick(participants, mod=mod)
                winners = [win_id]

                if mod == MOD_STR:
                    # Strength: NON incrementa livello
                    save_state()
                else:
                    _bump_win_counters(str(win_id))
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
            member = None
            if winners:
                try:
                    member = await guild.fetch_member(winners[0])
                except Exception:
                    member = guild.get_member(winners[0])
            await post_winner_announcement_special(channel, member)
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

# --- TEST MODIFICATORI (NUOVI) ---

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

# --- comandi già esistenti (tutti admin-only) ---

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

@bot.tree.command(name="mostralivelli", description="Mostra i livelli/vittorie registrati (solo admin).")
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
    for uid, w in items:
        uid_int = int(uid)
        w_int = int(w)
        member = inter.guild.get_member(uid_int)
        if member:
            remember_name(uid_int, member.display_name)
            name = member.display_name
        else:
            name = name_fallback(uid_int)
        lines.append(f"• **{name}** — Livello: **{level_from_wins(w_int)}**")

    PAGE_SIZE = 20
    pages = [lines[i:i+PAGE_SIZE] for i in range(0, len(lines), PAGE_SIZE)]
    for idx, page in enumerate(pages, start=1):
        embed = golden_embed(
            f"REGISTRO LIVELLI (corrente) — Pag. {idx}/{len(pages)}",
            "\n".join(page)
        )
        await inter.followup.send(embed=embed, ephemeral=True)

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
    for uid, w in items[:MAX_PUBLIC]:
        uid_int = int(uid)
        w_int = int(w)
        member = inter.guild.get_member(uid_int)
        if member:
            remember_name(uid_int, member.display_name)
            name = member.display_name
        else:
            name = name_fallback(uid_int)
        lvl = level_from_wins(w_int)
        lines.append(f"• **{name}** — Livello **{lvl}**")

    more = max(0, len(items) - MAX_PUBLIC)
    desc = "📜 **Classifica Livelli (Top):**\n" + "\n".join(lines)
    if more > 0:
        desc += f"\n\n…e altri **{more}**."

    embed = golden_embed("ALBO IMPERIALE — LIVELLI", desc)
    await ch.send(embed=embed)
    save_state()
    await inter.followup.send("✅ Classifica pubblicata nel canale lotteria.", ephemeral=True)

@bot.tree.command(name="setedition", description="Imposta manualmente il numero di edizione (solo admin).")
@admin_only_command()
@app_commands.describe(numero="Numero edizione da impostare (>=1). È la PROSSIMA edizione che aprirai.")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if numero < 1:
        await inter.response.send_message("❌ L'edizione deve essere ≥ 1.", ephemeral=True)
        return
    STATE["edition"] = numero
    save_state()
    await inter.response.send_message(f"✅ Prossima edizione impostata a **{numero}**.", ephemeral=True)

@bot.tree.command(name="setwin", description="Registra manualmente una vittoria e aggiorna i dati (solo admin).")
@admin_only_command()
@app_commands.describe(utente="Seleziona l'utente vincitore da registrare")
async def slash_setwin(inter: discord.Interaction, utente: discord.Member):
    uid = str(utente.id)
    remember_name(utente.id, utente.display_name)
    _bump_win_counters(uid)
    save_state()
    lvl = level_from_wins(int(STATE["wins"].get(uid, 0)))
    tot = int(STATE["victories"].get(uid, 0))
    cyc = int(STATE["cycles"].get(uid, 0))
    await inter.response.send_message(
        f"✅ Registrata vittoria per **{utente.display_name}** — Livello attuale: {lvl} • Vittorie totali: {tot} • Cicli: {cyc}",
        ephemeral=True
    )

@bot.tree.command(name="setlivello", description="Imposta manualmente il livello (1–3) di un vincitore (solo admin).")
@admin_only_command()
@app_commands.describe(utente="Utente di cui modificare il livello", livello="Livello da impostare (1, 2 o 3)")
async def slash_setlivello(inter: discord.Interaction, utente: discord.Member, livello: int):
    if livello < 1 or livello > 3:
        await inter.response.send_message("❌ Il livello deve essere tra 1 e 3.", ephemeral=True)
        return
    uid = str(utente.id)
    remember_name(utente.id, utente.display_name)
    STATE["wins"][uid] = int(livello)
    save_state()
    tot = int(STATE["victories"].get(uid, 0))
    cyc = int(STATE["cycles"].get(uid, 0))
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
    STATE["wins"].pop(uid, None)
    STATE["victories"].pop(uid, None)
    STATE["cycles"].pop(uid, None)
    STATE["last_win_iso"].pop(uid, None)
    STATE.get("names", {}).pop(uid, None)
    save_state()
    await inter.response.send_message(
        f"🧹 **{utente.display_name}** rimosso dalla memoria dei vincitori.",
        ephemeral=True
    )

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
    save_state()
    await inter.followup.send("📣 Annuncio **classico** eseguito.", ephemeral=True)

# --- Speciale: comandi forzati (lasciata IDENTICA nella logica premi) ---
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
    member = None
    if lw:
        try:
            member = await inter.guild.fetch_member(int(lw))
        except Exception:
            member = inter.guild.get_member(int(lw))

    await post_winner_announcement_special(ch, member)

    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    STATE["last_winner_ids"] = []
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
