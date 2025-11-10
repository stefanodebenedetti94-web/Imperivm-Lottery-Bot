# === IMPERIVM Lottery Bot — main.py (Render / discord.py 2.x) ===
# Stato persistito su messaggio pinnato (niente reset tra i redeploy)
# Edizione speciale: ogni 4 edizioni (4, 8, 12, …) con premi 600k/800k/1M
# (+200k se il vincitore ha già vinto in passato).
# Nessuna reazione automatica del bot al messaggio di apertura.

import os
import json
import asyncio
import random
from datetime import datetime, time
from threading import Thread
from typing import Optional, List, Tuple

import pytz
import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --- tiny web server (Render healthcheck) ---
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
        Thread(target=lambda: serve(app, host="0.0.0.0", port=port),
               daemon=True).start()
    else:
        Thread(target=lambda: app.run(host="0.0.0.0", port=port),
               daemon=True).start()

# ---------- CONFIG ----------
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True
INTENTS.reactions = True

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ Manca DISCORD_TOKEN nelle Environment Variables di Render.")

LOTTERY_CHANNEL_ID = int(os.getenv("LOTTERY_CHANNEL_ID", "0"))  # es. 1426994508347867286
STATE_MESSAGE_ID   = int(os.getenv("STATE_MESSAGE_ID", "0"))    # ID messaggio pinnato con lo stato
APP_URL            = os.getenv("APP_URL", "").strip()           # usato da uptime monitor (ping)

TZ = pytz.timezone("Europe/Rome")
GOLD = discord.Color.from_str("#DAA520")

# Stato di default
DEFAULT_STATE = {
    "schema": "imperivm.lottery.v1",
    "edition": 4,                     # seed iniziale (come concordato)
    "open_message_id": None,
    "wins": {},                       # {uid: 1..3} (livello corrente)
    "victories": {},                  # {uid: tot vittorie storiche}
    "cycles": {},                     # {uid: cicli L3 completati}
    "last_win_iso": {},               # {uid: ISO datetime ultima vittoria}
    "last_winner_id": None,
    "last_open_week": None,
    "last_close_week": None,
    "last_announce_week": None
}

# Cache in RAM
STATE = DEFAULT_STATE.copy()

bot = commands.Bot(command_prefix="!", intents=INTENTS)
scheduler = AsyncIOScheduler(timezone=TZ)

# ---------- UTILS ----------
def now_tz() -> datetime:
    return datetime.now(TZ)

def week_key(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-{w:02d}"

def level_from_wins(wins: int) -> int:
    if wins <= 0:
        return 0
    return min(wins, 3)

def is_special_edition(edition: int) -> bool:
    return edition % 4 == 0

def golden_embed(title: str, desc: str) -> discord.Embed:
    e = discord.Embed(title=f"📜  {title}  📜", description=desc, color=GOLD)
    e.set_footer(text="IMPERIVM • Lotteria settimanale")
    return e

async def get_lottery_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    if LOTTERY_CHANNEL_ID:
        ch = guild.get_channel(LOTTERY_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    # fallback: primo text channel
    return guild.text_channels[0] if guild.text_channels else None

# --- Stato su messaggio pinnato ---
def _extract_json_from_message(content: str) -> Optional[dict]:
    # prova a prendere il blocco JSON (con o senza ```json)
    start = content.find("{")
    end   = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(content[start:end+1])
    except Exception:
        return None

async def load_state_from_pinned(guild: discord.Guild) -> dict:
    """Legge il messaggio pinnato (STATE_MESSAGE_ID) e aggiorna STATE."""
    global STATE
    try:
        ch = await get_lottery_channel(guild)
        if not ch:
            return STATE
        msg = await ch.fetch_message(STATE_MESSAGE_ID)
        data = _extract_json_from_message(msg.content or "")
        if not isinstance(data, dict):
            return STATE
        # merge con default per eventuali nuove chiavi
        merged = DEFAULT_STATE.copy()
        merged.update(data)
        STATE = merged
        return STATE
    except Exception:
        return STATE

async def save_state_to_pinned(guild: discord.Guild) -> None:
    """Scrive lo stato corrente nel messaggio pinnato (in JSON formattato)."""
    try:
        ch = await get_lottery_channel(guild)
        if not ch:
            return
        msg = await ch.fetch_message(STATE_MESSAGE_ID)
        payload = json.dumps(STATE, ensure_ascii=False, indent=2)
        content = f"IMPERIVM_STATE\n```json\n{payload}\n```"
        await msg.edit(content=content)
    except Exception:
        pass

def _bump_win_counters(uid: str):
    prev = STATE["wins"].get(uid, 0)
    new  = prev + 1
    reset = False
    if new > 3:
        new = 1
        reset = True
    STATE["wins"][uid] = new
    STATE["victories"][uid] = STATE["victories"].get(uid, 0) + 1
    if reset:
        STATE["cycles"][uid] = STATE["cycles"].get(uid, 0) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

# ---------- MESSAGGI ----------
async def post_open_message(channel: discord.TextChannel, edition: int, special: bool):
    if special:
        lines = [
            "Cittadini dell’Impero 👑",
            "Questa è una **Special Edition**: il fato brandisce borse più pesanti!",
            "Premi casuali: **600.000 / 800.000 / 1.000.000 Kama**.",
            "Se il vincitore ha già vinto in passato → **+200.000 Kama bonus**.",
            "",
            "Per partecipare reagite con ✅ a **questo** messaggio.",
            "",
            f"**Edizione n°{edition} — SPECIALE**",
            "Chiusura alle **00:00 di giovedì** • Annuncio alle **08:00 di giovedì**."
        ]
        title = "LOTTERIA IMPERIVM — SPECIAL EDITION"
    else:
        lines = [
            "Cittadini dell’Impero 👑",
            "La **Lotteria Classica** è ufficialmente aperta.",
            "Reagite con ✅ per entrare nell’urna dell’Aquila Imperiale.",
            "",
            "⚔️ Premi:",
            "• 1ª vittoria → **100.000 Kama**",
            "• 2ª vittoria → **Scudo di Gilda** *(se già posseduto → 250.000 Kama)*",
            "• 3ª vittoria → **500.000 Kama** *(reset livelli)*",
            "",
            f"**Edizione n°{edition}**",
            "Chiusura alle **00:00 di giovedì** • Annuncio alle **08:00 di giovedì**."
        ]
        title = "LOTTERIA IMPERIVM — EDIZIONE SETTIMANALE"

    emb = golden_embed(title, "\n".join(lines))
    msg = await channel.send(embed=emb)
    # Importante: **NON** aggiungiamo reazione automatica del bot
    STATE["open_message_id"] = msg.id

async def post_close_message(channel: discord.TextChannel, participants: List[int]):
    if not participants:
        desc = ("La sorte ha parlato… 😕 **Nessun partecipante valido** questa settimana.\n"
                "Riproveremo sotto lo stendardo dell’Impero.")
    else:
        # mostriamo solo nomi (no mention)
        preview = []
        for uid in participants[:50]:
            m = channel.guild.get_member(uid)
            preview.append(m.display_name if m else f"utente {uid}")
        more = len(participants) - len(preview)
        body = "• " + "\n• ".join(preview)
        if more > 0:
            body += f"\n…e altri **{more}**."
        desc = ("La raccolta è chiusa: i biglietti sono stati sigillati. 🌙\n\n"
                f"📜 **Partecipanti ({len(participants)}):**\n{body}\n\n"
                "Il verdetto sarà proclamato alle **08:00**.")
    await channel.send(embed=golden_embed("LOTTERIA IMPERIVM — CHIUSA", desc))

async def post_winner_announcement(channel: discord.TextChannel, winner_id: Optional[int], special: bool):
    if not winner_id:
        await channel.send(embed=golden_embed(
            "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM",
            "I sigilli sono stati spezzati, ma nessun nome è emerso dagli annali. 🕯️"
        ))
        return

    g = channel.guild
    member = g.get_member(winner_id) or await g.fetch_member(winner_id)
    uid = str(winner_id)
    lvl = level_from_wins(STATE["wins"].get(uid, 0))
    stato = f"{lvl}/3" if lvl else "0/3"

    if special:
        base = random.choice([600_000, 800_000, 1_000_000])
        bonus = 200_000 if STATE["victories"].get(uid, 0) > 1 else 0  # >1 perché _bump_win_counters ha già sommato
        prize = base + bonus
        prize_txt = f"{prize:,}".replace(",", ".") + " Kama"
        subtitle = "— **EDIZIONE SPECIALE** —"
    else:
        if lvl == 1:
            prize_txt = "100.000 Kama"
        elif lvl == 2:
            prize_txt = "Scudo di Gilda *(se già posseduto → 250.000 Kama)*"
        else:
            prize_txt = "500.000 Kama *(reset livelli)*"
        subtitle = ""

    desc = (
        f"{subtitle}\n"
        f"👑 **Vincitore:** {member.mention}\n"
        f"⚔️ **Livello attuale:** {lvl}  —  **Stato:** {stato}\n"
        f"📜 **Ricompensa:** {prize_txt}\n\n"
        "La prossima chiamata dell’Aquila Imperiale risuonerà **mercoledì a mezzanotte**."
    )
    await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))

# ---------- LOGICA ----------
async def collect_participants(msg: discord.Message) -> List[int]:
    ids: List[int] = []
    try:
        await msg.fetch()
    except Exception:
        pass
    for r in msg.reactions:
        if str(r.emoji) == "✅":
            async for u in r.users():
                if not u.bot:
                    ids.append(u.id)
    # dedup
    return list(dict.fromkeys(ids))

async def open_lottery(guild: discord.Guild, force_special: Optional[bool] = None):
    """Apre la lotteria: decide se Special/Classic in base all'edizione corrente."""
    await load_state_from_pinned(guild)
    channel = await get_lottery_channel(guild)
    if not channel:
        return

    # se esiste già un messaggio di apertura valido, non riapriamo
    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            return
        except Exception:
            STATE["open_message_id"] = None

    edition = STATE["edition"]
    special = is_special_edition(edition) if force_special is None else force_special
    await post_open_message(channel, edition, special)
    STATE["last_open_week"] = week_key(now_tz())
    # incrementiamo edizione subito dopo apertura
    STATE["edition"] = edition + 1
    await save_state_to_pinned(guild)

async def close_and_pick(guild: discord.Guild, announce_now: bool = False, force_special: Optional[bool] = None) -> Tuple[Optional[int], bool]:
    """Chiude e sceglie il vincitore. Ritorna (winner_id, special)."""
    await load_state_from_pinned(guild)
    channel = await get_lottery_channel(guild)
    if not channel:
        return (None, False)

    msg = None
    if STATE.get("open_message_id"):
        try:
            msg = await channel.fetch_message(STATE["open_message_id"])
        except Exception:
            msg = None

    participants: List[int] = []
    if msg:
        participants = await collect_participants(msg)

    await post_close_message(channel, participants)

    winner_id = None
    if participants:
        winner_id = random.choice(participants)
        STATE["last_winner_id"] = winner_id
        _bump_win_counters(str(winner_id))

    STATE["open_message_id"] = None
    STATE["last_close_week"] = week_key(now_tz())
    await save_state_to_pinned(guild)

    # annuncio immediato opzionale
    special = is_special_edition(STATE["edition"] - 1) if force_special is None else force_special
    if announce_now:
        await post_winner_announcement(channel, winner_id, special)
        STATE["last_announce_week"] = week_key(now_tz())
        STATE["last_winner_id"] = None
        await save_state_to_pinned(guild)
    return (winner_id, special)

async def announce_winner(guild: discord.Guild, force_special: Optional[bool] = None):
    await load_state_from_pinned(guild)
    channel = await get_lottery_channel(guild)
    if not channel:
        return
    winner_id = STATE.get("last_winner_id")
    special = is_special_edition(STATE["edition"] - 1) if force_special is None else force_special
    await post_winner_announcement(channel, winner_id, special)
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    await save_state_to_pinned(guild)

# ---------- SCHEDULING ----------
def schedule_jobs():
    trig_open     = CronTrigger(day_of_week="wed", hour=0, minute=0, timezone=TZ)
    trig_close    = CronTrigger(day_of_week="thu", hour=0, minute=0, timezone=TZ)
    trig_announce = CronTrigger(day_of_week="thu", hour=8, minute=0, timezone=TZ)

    async def do_open():
        for g in bot.guilds:
            await open_lottery(g)
    async def do_close():
        for g in bot.guilds:
            await close_and_pick(g, announce_now=False)
    async def do_announce():
        for g in bot.guilds:
            await announce_winner(g)

    scheduler.add_job(lambda: asyncio.create_task(do_open()), trig_open)
    scheduler.add_job(lambda: asyncio.create_task(do_close()), trig_close)
    scheduler.add_job(lambda: asyncio.create_task(do_announce()), trig_announce)

    # WATCHDOG ogni 5' per recuperare eventuali eventi saltati
    async def watchdog():
        dt = now_tz()
        wk = week_key(dt)
        wd = dt.weekday()  # Mon=0 … Sun=6
        t  = dt.time()

        for g in bot.guilds:
            await load_state_from_pinned(g)

        if wd == 2 and t >= time(0,0):  # Wed 00:00+
            for g in bot.guilds:
                if STATE.get("last_open_week") != wk:
                    await open_lottery(g)
        if wd == 3 and t >= time(0,0):  # Thu 00:00+
            for g in bot.guilds:
                if STATE.get("last_close_week") != wk:
                    await close_and_pick(g, announce_now=False)
        if wd == 3 and t >= time(8,0):  # Thu 08:00+
            for g in bot.guilds:
                if STATE.get("last_announce_week") != wk:
                    await announce_winner(g)

    scheduler.add_job(lambda: asyncio.create_task(watchdog()),
                      "interval", minutes=5, timezone=TZ)

# ---------- PERMESSI ----------
def _is_admin(user: discord.abc.User) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.administrator)

# ---------- EVENTI ----------
@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass
    for g in bot.guilds:
        await load_state_from_pinned(g)
    if not scheduler.running:
        schedule_jobs()
        scheduler.start()
    print(f"✅ Bot online come {bot.user} — edizione corrente (prima dell'apertura): {STATE['edition']}")

# ---------- SLASH COMMANDS ----------
@bot.tree.command(name="apertura", description="Forza l'apertura (classica/speciale auto).")
async def apertura(inter: discord.Interaction):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(thinking=True, ephemeral=True)
    await open_lottery(inter.guild, force_special=None)
    await inter.followup.send("📜 Apertura forzata eseguita.", ephemeral=True)

@bot.tree.command(name="chiusura", description="Forza la chiusura e la scelta del vincitore.")
async def chiusura(inter: discord.Interaction):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(thinking=True, ephemeral=True)
    await close_and_pick(inter.guild, announce_now=False, force_special=None)
    await inter.followup.send("🗝️ Chiusura forzata eseguita.", ephemeral=True)

@bot.tree.command(name="annuncio", description="Forza l'annuncio del vincitore.")
async def annuncio(inter: discord.Interaction):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(thinking=True, ephemeral=True)
    await announce_winner(inter.guild, force_special=None)
    await inter.followup.send("📣 Annuncio forzato eseguito.", ephemeral=True)

# — Varianti SPECIALE (forzate) —
@bot.tree.command(name="apertura_speciale", description="Forza apertura in modalità Special Edition.")
async def apertura_speciale(inter: discord.Interaction):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(thinking=True, ephemeral=True)
    await open_lottery(inter.guild, force_special=True)
    await inter.followup.send("✨ Apertura *Special Edition* forzata.", ephemeral=True)

@bot.tree.command(name="chiusura_speciale", description="Forza chiusura e sorteggio (Special).")
async def chiusura_speciale(inter: discord.Interaction):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(thinking=True, ephemeral=True)
    await close_and_pick(inter.guild, announce_now=False, force_special=True)
    await inter.followup.send("✨ Chiusura *Special Edition* forzata.", ephemeral=True)

@bot.tree.command(name="annuncio_speciale", description="Forza annuncio (Special).")
async def annuncio_speciale(inter: discord.Interaction):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(thinking=True, ephemeral=True)
    await announce_winner(inter.guild, force_special=True)
    await inter.followup.send("✨ Annuncio *Special Edition* forzato.", ephemeral=True)

# — Utility amministrazione —
@bot.tree.command(name="setedition", description="Imposta manualmente il numero di edizione.")
@app_commands.describe(numero="Numero di edizione da impostare (intero).")
async def setedition(inter: discord.Interaction, numero: int):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    STATE["edition"] = int(numero)
    await save_state_to_pinned(inter.guild)
    await inter.followup.send(f"✅ Edizione impostata a **{numero}**.", ephemeral=True)

@bot.tree.command(name="setwin", description="Registra manualmente una vittoria per un utente.")
@app_commands.describe(utente="Seleziona il vincitore da registrare.")
async def setwin(inter: discord.Interaction, utente: discord.Member):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    _bump_win_counters(str(utente.id))
    await save_state_to_pinned(inter.guild)
    lvl = level_from_wins(STATE["wins"].get(str(utente.id), 0))
    tot = STATE["victories"].get(str(utente.id), 0)
    cyc = STATE["cycles"].get(str(utente.id), 0)
    await inter.followup.send(
        f"✅ Registrata vittoria per **{utente.display_name}** — Livello: {lvl} • Vittorie totali: {tot} • Cicli: {cyc}",
        ephemeral=True
    )

@bot.tree.command(name="mostralivelli", description="Mostra i livelli correnti di tutti i vincitori.")
async def mostralivelli(inter: discord.Interaction):
    if not _is_admin(inter.user):
        return await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    wins = STATE.get("wins", {})
    if not wins:
        return await inter.followup.send("📜 Nessun livello registrato al momento.", ephemeral=True)
    lines = []
    for uid, w in wins.items():
        m = inter.guild.get_member(int(uid))
        tag = m.mention if m else f"<@{uid}>"
        lines.append(f"{tag}: vittorie (livello) = {w} → Livello attuale: {level_from_wins(w)}")
    await inter.followup.send(embed=golden_embed("REGISTRO LIVELLI (corrente)", "\n".join(lines)), ephemeral=True)

# ---------- BOOT ----------
@bot.event
async def setup_hook():
    try:
        await bot.tree.sync()
    except Exception:
        pass

if __name__ == "__main__":
    start_web_server()
    bot.run(TOKEN)
