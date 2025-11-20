# === IMPERIVM Lottery Bot — main.py (Render / discord.py 2.x) ===
# Stato persistito su GitHub Gist (nessun automatismo settimanale)
# Edizione speciale SOLO quando usi /aperturaspeciale
# Premi speciali: 600k / 800k / 1M (+200k se il vincitore ha già vinto in passato).

import os
import json
import asyncio
import random
from datetime import datetime
from threading import Thread
from typing import Optional, List, Dict
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
    "last_winner_id": None,      # id dell'ultimo vincitore scelto da chiusura
    "last_open_week": None,      # non più usati ma lasciati per compatibilità
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
    iso = dt.isocalendar()  # (year, week, weekday)
    return f"{iso[0]}-{iso[1]:02d}"

def now_tz() -> datetime:
    return datetime.now(TZ)

# ---------- Testi & messaggi ----------

def _classic_open_lines(edition: int) -> List[str]:
    return [
        "Cittadini dell'Impero 👑",
        "È giunto il momento di sfidare la sorte sotto lo stendardo bianco e rosso dell'IMPERIVM!",
        "Da ora fino alle 00:00 di giovedì, la lotteria imperiale è ufficialmente **aperta**! 🧾",
        "",
        "Reagite con ✅ a questo messaggio per partecipare all'estrazione.",
        "Il destino premierà solo i più audaci!",
        "",
        "⚔️ Premi in palio:",
        "  1️⃣ 1ª vittoria → 100.000 Kama",
        "  2️⃣ 2ª vittoria → Scudo di Gilda *(se già posseduto → 250.000 Kama)*",
        "  3️⃣ 3ª vittoria → 500.000 Kama *(reset dei livelli)*",
        "",
        f"**Edizione n°{edition} (Classica)**",
        "",
        "🕗 *Nota:* il verdetto sarà annunciato **giovedì alle 08:00**.",
    ]

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
    lines = _special_open_lines(edition) if special else _classic_open_lines(edition)
    embed = golden_embed("LOTTERIA IMPERIVM – APERTA", "\n".join(lines))
    msg = await channel.send(embed=embed)
    STATE["open_message_id"] = msg.id
    save_state()
    print(f"[LOTTERY] Apertura {'SPECIALE' if special else 'classica'} inviata (edizione {edition})")
    return msg

async def post_close_message(channel: discord.TextChannel, no_participants: bool, names_preview: Optional[str]):
    if no_participants:
        desc = (
            "La sorte ha parlato… 😕  **Nessun partecipante valido** questa settimana.\n"
            "Torniamo mercoledì prossimo! 👑"
        )
    else:
        desc = (
            "La sorte ha parlato… 🌅  Il verdetto sarà svelato all'alba.\n"
            "I biglietti sono stati raccolti; il fato è in bilico tra le mani degli Dei.\n\n"
        )
        if names_preview:
            desc += names_preview + "\n\n"
        desc += "🕗 *Annuncio del vincitore alle **08:00** di giovedì.*"
    await channel.send(embed=golden_embed("LOTTERIA IMPERIVM – CHIUSA", desc))

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
    parts = [f"{total:,}".replace(",", ".") + " Kama"]
    if bonus:
        parts.append("(+200k bonus fedeltà)")
    return " ".join(parts)

async def post_winner_announcement(channel: discord.TextChannel, member: Optional[discord.Member], special: bool):
    if member is None:
        desc = (
            "I sigilli sono stati spezzati, ma stavolta il fato è rimasto muto.\n"
            "Nessun nome scolpito negli annali: riproveremo mercoledì prossimo. 🕯️"
        )
        await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))
        return

    uid = str(member.id)
    lvl = level_from_wins(STATE["wins"].get(uid, 0))
    stato = f"{lvl}/3" if lvl else "0/3"

    if special:
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
    else:
        premio = _classic_prize_text(lvl)
        title = "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM"
        desc = (
            "Cittadini dell’Impero, il fato ha parlato e i sigilli sono stati sciolti.\n"
            "Tra pergamene e ceralacca, il nome inciso negli annali è stato scelto.\n\n"
            f"👑 **Vincitore:** {member.mention}\n"
            f"⚔️ **Livello attuale:** {lvl}  —  **Stato:** {stato}\n"
            f"📜 **Ricompensa:** {premio}\n\n"
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
    prev = STATE["wins"].get(uid, 0)
    new = prev + 1
    reset = False
    if new > 3:
        new = 1
        reset = True
    STATE["wins"][uid] = new
    STATE["victories"][uid] = STATE["victories"].get(uid, 0) + 1
    if reset:
        STATE["cycles"][uid] = STATE["cycles"].get(uid, 0) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

async def _close_and_pick_common(guild: discord.Guild):
    channel = await get_lottery_channel(guild)
    if not channel:
        return None, None, [], ""

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
            names.append(m.display_name if m else f"utente {uid_int}")
        more = len(participants) - 50
        header = f"📜 **Partecipanti ({len(participants)}):**"
        body = "• " + "\n".join(f"• {n}" for n in names)
        if more > 0:
            body += f"\n…e altri **{more}**."
        names_preview = f"{header}\n{body}"

    winner_member = None
    STATE["last_winner_id"] = None

    if participants:
        win_id = random.choice(participants)
        STATE["last_winner_id"] = win_id
        uid = str(win_id)
        _bump_win_counters(uid)
        save_state()
        try:
            winner_member = await guild.fetch_member(win_id)
        except Exception:
            winner_member = guild.get_member(win_id)

    return channel, winner_member, participants, names_preview or ""

async def close_and_pick(guild: discord.Guild, announce_now: bool = False, special: bool = False):
    channel, winner_member, participants, names_preview = await _close_and_pick_common(guild)
    if not channel:
        return None
    await post_close_message(channel, len(participants) == 0, names_preview)
    if announce_now:
        await post_winner_announcement(channel, winner_member, special=special)
    STATE["open_message_id"] = None
    save_state()
    print(f"[LOTTERY] Chiusura eseguita (partecipanti: {len(participants)})")
    return winner_member

async def open_lottery(guild: discord.Guild, special: bool = False):
    channel = await get_lottery_channel(guild)
    if not channel:
        return
    # Evita doppie aperture se c'è già un messaggio valido
    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            print("[LOTTERY] Apertura saltata: messaggio già presente.")
            return
        except Exception:
            pass

    await post_open_message(channel, special=special)
    # L'incremento di edition viene fatto nei comandi /apertura e /aperturaspeciale

# ---------- Eventi ----------

@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass
    print(f"✅ Bot online come {bot.user} — prossima edizione: {STATE.get('edition')}")

# ---------- Slash commands ----------

def _slash_admin_guard(inter: discord.Interaction) -> bool:
    if ADMIN_IDS and inter.user.id in ADMIN_IDS:
        return True
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

@bot.tree.command(name="whoami", description="Mostra il tuo ID e se sei admin.")
async def slash_whoami(inter: discord.Interaction):
    adm = "sì" if _slash_admin_guard(inter) else "no"
    await inter.response.send_message(f"ID: {inter.user.id} — sei admin: {adm}", ephemeral=True)

@bot.tree.command(name="mostraedizione", description="Mostra la PROSSIMA edizione che verrà aperta.")
async def slash_mostraedizione(inter: discord.Interaction):
    ed = STATE.get("edition", 1)
    await inter.response.send_message(f"🧾 **Prossima edizione da aprire:** n°{ed}", ephemeral=True)

@bot.tree.command(name="mostralivelli", description="Mostra i livelli/vittorie registrati.")
async def slash_mostralivelli(inter: discord.Interaction):
    wins = STATE.get("wins", {})
    if not wins:
        await inter.response.send_message("📜 Nessun livello registrato al momento.", ephemeral=True)
        return
    lines = []
    for uid, w in wins.items():
        try:
            member = await inter.guild.fetch_member(int(uid))
        except Exception:
            member = inter.guild.get_member(int(uid))
        tag = member.mention if member else f"<@{uid}>"
        lines.append(f"{tag}: vittorie (livello) = {w} → Livello attuale: {level_from_wins(w)}")
    embed = golden_embed("REGISTRO LIVELLI (corrente)", "\n".join(lines))
    await inter.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setedition", description="Imposta manualmente il numero di edizione (solo admin).")
@app_commands.describe(numero="Numero edizione da impostare (>=1). È la PROSSIMA edizione che aprirai.")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    if numero < 1:
        await inter.response.send_message("❌ L'edizione deve essere ≥ 1.", ephemeral=True)
        return
    STATE["edition"] = numero
    save_state()
    await inter.response.send_message(f"✅ Prossima edizione impostata a **{numero}**.", ephemeral=True)

@bot.tree.command(name="setwin", description="Registra manualmente una vittoria e aggiorna i dati (solo admin).")
@app_commands.describe(utente="Seleziona l'utente vincitore da registrare")
async def slash_setwin(inter: discord.Interaction, utente: discord.Member):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    uid = str(utente.id)
    _bump_win_counters(uid)
    save_state()
    lvl = level_from_wins(STATE["wins"].get(uid, 0))
    tot = STATE["victories"].get(uid, 0)
    cyc = STATE["cycles"].get(uid, 0)
    await inter.response.send_message(
        f"✅ Registrata vittoria per **{utente.display_name}** — Livello attuale: {lvl} • Vittorie totali: {tot} • Cicli: {cyc}",
        ephemeral=True
    )

@bot.tree.command(name="setlivello", description="Imposta manualmente il livello (1–3) di un vincitore (solo admin).")
@app_commands.describe(utente="Utente di cui modificare il livello", livello="Livello da impostare (1, 2 o 3)")
async def slash_setlivello(inter: discord.Interaction, utente: discord.Member, livello: int):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    if livello < 1 or livello > 3:
        await inter.response.send_message("❌ Il livello deve essere tra 1 e 3.", ephemeral=True)
        return
    uid = str(utente.id)
    STATE["wins"][uid] = livello
    save_state()
    tot = STATE["victories"].get(uid, 0)
    cyc = STATE["cycles"].get(uid, 0)
    await inter.response.send_message(
        f"✅ Impostato **livello {livello}** per **{utente.display_name}** "
        f"(vittorie totali: {tot}, cicli: {cyc}).",
        ephemeral=True
    )

@bot.tree.command(name="rimuoviwinner", description="Rimuove un utente dalla memoria dei vincitori (solo admin).")
@app_commands.describe(utente="Utente da cancellare dalla memoria vincitori")
async def slash_rimuoviwinner(inter: discord.Interaction, utente: discord.Member):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    uid = str(utente.id)
    STATE["wins"].pop(uid, None)
    STATE["victories"].pop(uid, None)
    STATE["cycles"].pop(uid, None)
    STATE["last_win_iso"].pop(uid, None)
    save_state()
    await inter.response.send_message(
        f"🧹 **{utente.display_name}** rimosso dalla memoria dei vincitori.",
        ephemeral=True
    )

@bot.tree.command(name="apertura", description="Apre la lotteria CLASSICA (solo admin).")
async def slash_apertura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    ed = STATE.get("edition", 1)
    await open_lottery(inter.guild, special=False)
    STATE["edition"] = ed + 1
    save_state()
    await inter.followup.send(f"📜 Apertura **classica** eseguita (edizione n°{ed}).", ephemeral=True)

@bot.tree.command(name="chiusura", description="Chiude e seleziona il vincitore (lotteria CLASSICA, solo admin).")
async def slash_chiusura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    await close_and_pick(inter.guild, announce_now=False, special=False)
    STATE["last_close_week"] = week_key(now_tz()); save_state()
    await inter.followup.send("🗝️ Chiusura **classica** eseguita.", ephemeral=True)

@bot.tree.command(name="annuncio", description="Annuncia il vincitore (lotteria CLASSICA, solo admin).")
async def slash_annuncio(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    ch = await get_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato.", ephemeral=True)
        return
    lw = STATE.get("last_winner_id")
    member = None
    if lw:
        try:
            member = await inter.guild.fetch_member(lw)
        except Exception:
            member = inter.guild.get_member(lw)
    await post_winner_announcement(ch, member, special=False)
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    save_state()
    await inter.followup.send("📣 Annuncio **classico** eseguito.", ephemeral=True)

# --- Speciale: comandi forzati ---
@bot.tree.command(name="aperturaspeciale", description="Apre la lotteria **Special Edition** (solo admin).")
async def slash_aperturaspeciale(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    ed = STATE.get("edition", 1)
    await open_lottery(inter.guild, special=True)
    STATE["edition"] = ed + 1
    save_state()
    await inter.followup.send(f"💎 Apertura **SPECIALE** eseguita (edizione n°{ed}).", ephemeral=True)

@bot.tree.command(name="chiusuraspeciale", description="Chiude e seleziona il vincitore (EDIZIONE SPECIALE, solo admin).")
async def slash_chiusuraspeciale(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    await close_and_pick(inter.guild, announce_now=False, special=True)
    STATE["last_close_week"] = week_key(now_tz()); save_state()
    await inter.followup.send("🗝️ Chiusura **SPECIALE** eseguita.", ephemeral=True)

@bot.tree.command(name="annunciospeciale", description="Annuncia il vincitore (EDIZIONE SPECIALE, solo admin).")
async def slash_annunciospeciale(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    ch = await get_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato.", ephemeral=True)
        return
    lw = STATE.get("last_winner_id")
    member = None
    if lw:
        try:
            member = await inter.guild.fetch_member(lw)
        except Exception:
            member = inter.guild.get_member(lw)
    await post_winner_announcement(ch, member, special=True)
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    save_state()
    await inter.followup.send("📣 Annuncio **SPECIALE** eseguito.", ephemeral=True)

# ---------- Avvio ----------

@bot.event
async def setup_hook():
    try:
        await bot.tree.sync()
    except Exception:
        pass

if __name__ == "__main__":
    load_state()
    start_web_server()  # server HTTP per Render (healthcheck)
    bot.run(TOKEN)
