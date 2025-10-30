# === IMPERIVM Lottery Bot – main.py (Render / discord.py 2.x) ===

import os
import json
import asyncio
import random
from datetime import datetime, time
from threading import Thread
from typing import Optional, List

import pytz
import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

@app.get("/health")
def health():
    return "OK"

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

# Canale lotteria
LOTTERY_CHANNEL_ID = int(os.getenv("LOTTERY_CHANNEL_ID", "0"))
LOTTERY_CHANNEL_NAME_CANDIDATES = ["lotteria-imperiale", "lotteria-imperivm"]

# Admin extra (facoltativo). Se vuoto valgono i permessi admin Discord.
ADMIN_IDS = set()
_env_admins = os.getenv("ADMINS", "").strip()
if _env_admins:
    try:
        ADMIN_IDS = {int(x) for x in _env_admins.replace(" ", "").split(",") if x}
    except Exception:
        ADMIN_IDS = set()

TZ = pytz.timezone("Europe/Rome")
GOLD = discord.Color.from_str("#DAA520")

STATE_FILE = "lottery_state.json"
DEFAULT_STATE = {
    "edition": 1,
    "open_message_id": None,
    "wins": {},                  # {uid: 1..3} (livello corrente)
    "last_winner_id": None,      # salvato alla chiusura; annunciato alle 08:00
    # Antiduplica per cron/watchdog
    "last_open_week": None,      # "YYYY-WW"
    "last_close_week": None,
    "last_announce_week": None,
    # Stat storiche (opzionali, senza leaderboard)
    "victories": {},             # {uid: tot vittorie storiche}
    "cycles": {},                # {uid: quante volte ha raggiunto L3 (reset)}
    "last_win_iso": {},          # {uid: ISO ultima vittoria}
    # bootstrap per impostazioni iniziali
    "bootstrap_done": False
}

def load_state():
    if not os.path.exists(STATE_FILE):
        return DEFAULT_STATE.copy()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_STATE.items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return DEFAULT_STATE.copy()

def save_state(data):
    try:
        if os.path.exists(STATE_FILE):
            os.replace(STATE_FILE, STATE_FILE + ".bak")
    except Exception:
        pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

STATE = load_state()

# ---------- Bootstrap iniziale richiesto ----------
# Edizione 3, AERIES e SANDOR con una vittoria (livello 1)
AERIES_ID = 277179262534942720
SANDOR_ID = 662291114752999437
if not STATE.get("bootstrap_done", False):
    STATE["edition"] = max(STATE.get("edition", 1), 3)
    for uid in (AERIES_ID, SANDOR_ID):
        s = str(uid)
        STATE["wins"][s] = max(1, STATE["wins"].get(s, 1))
        STATE["victories"][s] = max(1, STATE["victories"].get(s, 1))
        STATE["last_win_iso"][s] = datetime.now(TZ).isoformat(timespec="seconds")
    STATE["bootstrap_done"] = True
    save_state(STATE)

bot = commands.Bot(command_prefix="!", intents=INTENTS)
scheduler = AsyncIOScheduler(timezone=TZ)

# ---------- Utility ----------
def is_admin(ctx_or_member) -> bool:
    m = ctx_or_member.author if hasattr(ctx_or_member, "author") else ctx_or_member
    if ADMIN_IDS and m.id in ADMIN_IDS:
        return True
    perms = getattr(m, "guild_permissions", None)
    return bool(perms and perms.administrator)

async def find_lottery_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    if LOTTERY_CHANNEL_ID:
        ch = guild.get_channel(LOTTERY_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    for name in LOTTERY_CHANNEL_NAME_CANDIDATES:
        for ch in guild.text_channels:
            if ch.name.lower() == name:
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

# ---------- Flusso lotteria ----------
async def post_open_message(channel: discord.TextChannel):
    global STATE
    edition = STATE["edition"]

    lines = [
        "Cittadini dell'Impero 👑",
        "È giunto il momento di sfidare la sorte sotto lo stendardo dorato dell'IMPERIVM!",
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
        f"**Edizione n°{edition}**",
        "",
        "🕗 *Il verdetto sarà annunciato **giovedì alle 08:00**.*"
    ]
    embed = golden_embed("LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE", "\n".join(lines))
    msg = await channel.send(embed=embed)

    # ⚠️ Richiesto: il bot NON deve aggiungere la reazione automaticamente
    # (lasciamo solo le istruzioni nel testo).

    STATE["open_message_id"] = msg.id
    save_state(STATE)
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
            "Tutti i biglietti sono stati raccolti, il fato è in bilico tra le mani degli Dei.\n\n"
        )
        if names_preview:
            desc += names_preview + "\n\n"
        desc += "🕗 *Annuncio del vincitore alle **08:00** di giovedì.*"
    await channel.send(embed=golden_embed("LOTTERIA IMPERIVM – CHIUSA", desc))

async def post_winner_announcement(channel: discord.TextChannel, member: Optional[discord.Member]):
    if member is None:
        desc = (
            "I sigilli sono stati spezzati, ma stavolta il fato è rimasto muto.\n"
            "Nessun nome scolpito negli annali: riproveremo mercoledì prossimo. 🕯️"
        )
        await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))
        return

    uid = str(member.id)
    wins = STATE["wins"].get(uid, 0)
    lvl = level_from_wins(wins)
    stato = f"{lvl}/3" if lvl else "0/3"

    if lvl == 1:
        premio = "100.000 Kama"
    elif lvl == 2:
        premio = "Scudo di Gilda *(se già posseduto → 250.000 Kama)*"
    else:
        premio = "500.000 Kama *(reset dei livelli)*"

    desc = (
        "Cittadini dell’Impero, il fato ha parlato e i sigilli sono stati sciolti.\n"
        "Tra pergamene e ceralacca, il nome inciso negli annali è stato scelto.\n\n"
        f"👑 **Vincitore:** {member.mention}\n"
        f"⚔️ **Livello attuale:** {lvl}  —  **Stato:** {stato}\n"
        f"📜 **Ricompensa:** {premio}\n\n"
        "Che la fortuna continui a sorriderti. La prossima chiamata dell’Aquila Imperiale\n"
        "risuonerà **mercoledì a mezzanotte**."
    )
    await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))

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

async def close_and_pick(guild: discord.Guild, announce_now: bool = False):
    global STATE
    channel = await find_lottery_channel(guild)
    if not channel:
        return None

    # recupera messaggio di apertura
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

    # anteprima nomi (no mention)
    names_preview = None
    if participants:
        names = []
        for uid_int in participants[:50]:
            m = guild.get_member(uid_int)
            names.append(m.display_name if m else f"utente {uid_int}")
        more = len(participants) - 50
        header = f"📜 **Partecipanti ({len(participants)}):**"
        body = "• " + "\n• ".join(names)
        if more > 0:
            body += f"\n…e altri **{more}**."
        names_preview = f"{header}\n{body}"

    no_participants = len(participants) == 0
    await post_close_message(channel, no_participants, names_preview)

    winner_member = None
    STATE["last_winner_id"] = None

    if not no_participants:
        win_id = random.choice(participants)
        STATE["last_winner_id"] = win_id
        _bump_win_counters(str(win_id))
        save_state(STATE)
        try:
            winner_member = await guild.fetch_member(win_id)
        except Exception:
            winner_member = guild.get_member(win_id)

    if announce_now:
        await post_winner_announcement(channel, winner_member)

    STATE["open_message_id"] = None
    save_state(STATE)
    return winner_member

async def open_lottery(guild: discord.Guild):
    """Mercoledì 00:00 – apre la lotteria e incrementa edizione."""
    global STATE
    channel = await find_lottery_channel(guild)
    if not channel:
        return
    # Evita doppie aperture se esiste già un messaggio di apertura vivo
    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            return
        except Exception:
            pass
    await post_open_message(channel)
    STATE["edition"] += 1
    save_state(STATE)

# ---------- Scheduling (cron + watchdog) ----------
def schedule_weekly_jobs():
    trig_open     = CronTrigger(day_of_week="wed", hour=0, minute=0, timezone=TZ)
    trig_close    = CronTrigger(day_of_week="thu", hour=0, minute=0, timezone=TZ)
    trig_announce = CronTrigger(day_of_week="thu", hour=8, minute=0, timezone=TZ)

    async def do_open():
        for g in bot.guilds:
            await open_lottery(g)
        STATE["last_open_week"] = week_key(now_tz()); save_state(STATE)

    async def do_close():
        for g in bot.guilds:
            await close_and_pick(g, announce_now=False)
        STATE["last_close_week"] = week_key(now_tz()); save_state(STATE)

    async def do_announce():
        for g in bot.guilds:
            ch = await find_lottery_channel(g)
            if not ch:
                continue
            lw = STATE.get("last_winner_id")
            member = None
            if lw:
                try:
                    member = await g.fetch_member(lw)
                except Exception:
                    member = g.get_member(lw)
            await post_winner_announcement(ch, member)
        STATE["last_announce_week"] = week_key(now_tz())
        STATE["last_winner_id"] = None
        save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(do_open()), trig_open)
    scheduler.add_job(lambda: asyncio.create_task(do_close()), trig_close)
    scheduler.add_job(lambda: asyncio.create_task(do_announce()), trig_announce)

    # WATCHDOG ogni 5 minuti: recupera eventuali esecuzioni mancate
    async def watchdog():
        dt = now_tz()
        wk = week_key(dt)
        wd = dt.weekday()  # Mon=0 ... Sun=6
        t  = dt.time()

        # Apertura mercoledì 00:00
        if wd == 2 and t >= time(0,0) and STATE.get("last_open_week") != wk:
            for g in bot.guilds:
                await open_lottery(g)
            STATE["last_open_week"] = wk; save_state(STATE); return

        # Chiusura giovedì 00:00
        if wd == 3 and t >= time(0,0) and STATE.get("last_close_week") != wk:
            for g in bot.guilds:
                await close_and_pick(g, announce_now=False)
            STATE["last_close_week"] = wk; save_state(STATE); return

        # Annuncio giovedì 08:00
        if wd == 3 and t >= time(8,0) and STATE.get("last_announce_week") != wk:
            for g in bot.guilds:
                ch = await find_lottery_channel(g)
                if not ch:
                    continue
                lw = STATE.get("last_winner_id")
                member = None
                if lw:
                    try:
                        member = await g.fetch_member(lw)
                    except Exception:
                        member = g.get_member(lw)
                await post_winner_announcement(ch, member)
            STATE["last_announce_week"] = wk
            STATE["last_winner_id"] = None
            save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(watchdog()), "interval", minutes=5, timezone=TZ)

# ---------- Eventi ----------
@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass
    print(f"✅ Bot online come {bot.user} — edizione corrente: {STATE['edition']}")
    if not scheduler.running:
        schedule_weekly_jobs()
        scheduler.start()

# ---------- Comandi testuali ----------
@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    adm = "sì" if is_admin(ctx) else "no"
    await ctx.reply(f"ID: {ctx.author.id} — sei admin: {adm}", mention_author=False)

@bot.command(name="mostralivelli")
async def mostralivelli(ctx: commands.Context):
    wins = STATE.get("wins", {})
    if not wins:
        await ctx.reply("📜 Nessun livello registrato al momento.", mention_author=False); return
    lines = []
    for uid, w in wins.items():
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"utente {uid}"
        lines.append(f"{name}: vittorie (livello) = {w} → Livello attuale: {level_from_wins(w)}")
    embed = golden_embed("REGISTRO LIVELLI (corrente)", "\n".join(lines))
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="resetlivelli")
async def resetlivelli(ctx: commands.Context):
    if not is_admin(ctx): return
    STATE["wins"] = {}
    save_state(STATE)
    await ctx.reply("🔄 Tutti i livelli sono stati azzerati (wins = 0 per tutti).", mention_author=False)

@bot.command(name="resetlotteria")
async def resetlotteria(ctx: commands.Context):
    if not is_admin(ctx): return
    STATE["edition"] = 1
    STATE["open_message_id"] = None
    save_state(STATE)
    await ctx.reply("🧹 Lotteria resettata: edizione=1.", mention_author=False)

# ---------- Slash commands ----------
def _slash_admin_guard(inter: discord.Interaction) -> bool:
    if ADMIN_IDS and inter.user.id in ADMIN_IDS:
        return True
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

@bot.tree.command(name="apertura", description="Forza l'apertura della lotteria (solo admin).")
async def slash_apertura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    await open_lottery(inter.guild)
    STATE["last_open_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("📜 Apertura forzata eseguita.")

@bot.tree.command(name="chiusura", description="Forza la chiusura e la selezione del vincitore (solo admin).")
async def slash_chiusura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    await close_and_pick(inter.guild, announce_now=False)
    STATE["last_close_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("🗝️ Chiusura forzata eseguita.")

@bot.tree.command(name="annuncio", description="Forza l'annuncio del vincitore (solo admin).")
async def slash_annuncio(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
    lw = STATE.get("last_winner_id")
    member = None
    if lw:
        try:
            member = await inter.guild.fetch_member(lw)
        except Exception:
            member = inter.guild.get_member(lw)
    await post_winner_announcement(ch, member)
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    save_state(STATE)
    await inter.followup.send("📣 Annuncio forzato eseguito.")

# --- NUOVO: registra manualmente una vittoria (manteniamo il comando) ---
@bot.tree.command(name="setwin", description="Registra manualmente una vittoria per un utente (solo admin).")
@app_commands.describe(utente="Seleziona l'utente vincitore da registrare")
async def slash_setwin(inter: discord.Interaction, utente: discord.Member):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    _bump_win_counters(str(utente.id))
    save_state(STATE)
    lvl = level_from_wins(STATE["wins"].get(str(utente.id), 0))
    await inter.followup.send(f"✅ Registrata vittoria per **{utente.display_name}** — Livello attuale: {lvl}")

# --- NUOVO: imposta manualmente l'edizione corrente ---
@bot.tree.command(name="setedition", description="Imposta manualmente l'edizione corrente (solo admin).")
@app_commands.describe(numero="Nuovo numero di edizione (>=1)")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    if numero < 1:
        await inter.response.send_message("⚠️ L'edizione deve essere >= 1.", ephemeral=True); return
    STATE["edition"] = numero
    save_state(STATE)
    await inter.response.send_message(f"📌 Edizione impostata a **n°{numero}**.", ephemeral=True)

# ---------- Avvio ----------
@bot.event
async def setup_hook():
    # sincronizza i comandi slash ad ogni boot (tollerante agli errori)
    try:
        await bot.tree.sync()
    except Exception:
        pass

if __name__ == "__main__":
    start_web_server()
    bot.run(TOKEN)
