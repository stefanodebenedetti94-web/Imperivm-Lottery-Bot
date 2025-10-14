# === IMPERIVM Lottery Bot – main.py (Render / discord.py 2.x) ===

import os
import json
import asyncio
import random
from datetime import datetime
from threading import Thread

import pytz
import discord
from discord.ext import commands
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

# Canale lotteria: puoi lasciare 0 per cercarlo per nome
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
    "participants": [],
    "wins": {},             # {user_id(str): vittorie (1..3, reset a 1 dopo 3)}
    "last_winner_id": None  # salvato alla chiusura; annunciato alle 08:00
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

STATE = load_state()

bot = commands.Bot(command_prefix="!", intents=INTENTS)
scheduler = AsyncIOScheduler(timezone=TZ)


# ---------- Utility ----------
def is_admin(ctx_or_member):
    m = ctx_or_member.author if hasattr(ctx_or_member, "author") else ctx_or_member
    if ADMIN_IDS and m.id in ADMIN_IDS:
        return True
    perms = getattr(m, "guild_permissions", None)
    return bool(perms and perms.administrator)

async def find_lottery_channel(guild: discord.Guild):
    if LOTTERY_CHANNEL_ID:
        ch = guild.get_channel(LOTTERY_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    # per nome (fallback)
    for name in LOTTERY_CHANNEL_NAME_CANDIDATES:
        for ch in guild.text_channels:
            if ch.name.lower() == name:
                return ch
    # ultima spiaggia: primo canale testuale disponibile
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
    ]
    embed = golden_embed("LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE", "\n".join(lines))
    msg = await channel.send(embed=embed)
    try:
        await msg.add_reaction("✅")
    except Exception:
        pass

    STATE["open_message_id"] = msg.id
    STATE["participants"] = []
    save_state(STATE)
    return msg

async def post_close_message(channel: discord.TextChannel, no_participants: bool):
    if no_participants:
        desc = (
            "La sorte ha parlato… 😕  **Nessun partecipante valido** questa settimana.\n"
            "Torniamo mercoledì prossimo! 👑"
        )
    else:
        desc = (
            "La sorte ha parlato… 🌅  Il verdetto sarà svelato all'alba.\n"
            "Tutti i biglietti sono stati raccolti, il fato è in bilico tra le mani degli Dei."
        )
    await channel.send(embed=golden_embed("LOTTERIA IMPERIVM – CHIUSA", desc))

async def post_winner_announcement(channel: discord.TextChannel, member: discord.Member | None):
    if member is None:
        desc = "Nessun partecipante questa settimana. Riproviamo mercoledì prossimo! 🙂"
        await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))
        return

    uid = str(member.id)
    wins = STATE["wins"].get(uid, 0)
    lvl = level_from_wins(wins)

    if lvl == 1:
        premio = "100.000 Kama"
    elif lvl == 2:
        premio = "Scudo di Gilda *(se già posseduto → 250.000 Kama)*"
    else:
        premio = "500.000 Kama *(reset dei livelli)*"

    desc = (
        f"**Vincitore:** {member.mention} 🎉\n"
        f"**Livello attuale:** {lvl}\n"
        f"**Ricompensa:** {premio}\n"
        "La prossima estrazione avverrà mercoledì a mezzanotte. 🕛"
    )
    await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))

async def collect_participants(msg: discord.Message) -> list[int]:
    ids: list[int] = []
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
    # dedup
    return list(dict.fromkeys(ids))

async def close_and_pick(guild: discord.Guild, announce_now: bool = False):
    """Chiude la lotteria, calcola vincitore e (se announce_now=True) annuncia subito."""
    global STATE
    channel = await find_lottery_channel(guild)
    if not channel:
        return None

    # carica messaggio apertura
    msg = None
    msg_id = STATE.get("open_message_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
        except Exception:
            msg = None

    participants: list[int] = []
    if msg:
        participants = await collect_participants(msg)

    no_participants = len(participants) == 0
    await post_close_message(channel, no_participants)

    winner_member = None
    STATE["last_winner_id"] = None

    if not no_participants:
        win_id = random.choice(participants)
        STATE["last_winner_id"] = win_id
        uid = str(win_id)
        w = STATE["wins"].get(uid, 0) + 1
        if w > 3:
            w = 1  # reset dopo la 3ª vittoria
        STATE["wins"][uid] = w
        save_state(STATE)
        try:
            winner_member = await guild.fetch_member(win_id)
        except Exception:
            winner_member = guild.get_member(win_id)

    if announce_now:
        await post_winner_announcement(channel, winner_member)

    # chiudo edizione
    STATE["open_message_id"] = None
    save_state(STATE)
    return winner_member

async def open_lottery(guild: discord.Guild):
    global STATE
    channel = await find_lottery_channel(guild)
    if not channel:
        return
    await post_open_message(channel)
    # prepara l'etichetta “edizione n°X” per la prossima settimana
    STATE["edition"] += 1
    save_state(STATE)


# ---------- Scheduling settimanale ----------
# Prima apertura automatica: 22/10/2025 00:00 (Europe/Rome)
START_DATE = TZ.localize(datetime(2025, 10, 22, 0, 0, 0))

def schedule_weekly_jobs():
    """Pianifica:
       - mer 00:00  → apertura
       - gio 00:00  → chiusura + calcolo vincitore (salvato)
       - gio 08:00  → annuncio ufficiale
       Se adesso è prima del 22/10/2025 00:00, la primissima apertura usa START_DATE."""
    trig_open     = CronTrigger(day_of_week="wed", hour=0, minute=0, timezone=TZ)
    trig_close    = CronTrigger(day_of_week="thu", hour=0, minute=0, timezone=TZ)
    trig_announce = CronTrigger(day_of_week="thu", hour=8, minute=0, timezone=TZ)

    now = datetime.now(TZ)
    next_open = START_DATE if now < START_DATE else None

    async def do_open():
        for g in bot.guilds:
            await open_lottery(g)

    async def do_close():
        for g in bot.guilds:
            await close_and_pick(g, announce_now=False)

    async def do_announce():
        for g in bot.guilds:
            channel = await find_lottery_channel(g)
            if not channel:
                continue
            lw = STATE.get("last_winner_id")
            member = None
            if lw:
                try:
                    member = await g.fetch_member(lw)
                except Exception:
                    member = g.get_member(lw)
            await post_winner_announcement(channel, member)
            STATE["last_winner_id"] = None
            save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(do_open()), trig_open, next_run_time=next_open)
    scheduler.add_job(lambda: asyncio.create_task(do_close()), trig_close)
    scheduler.add_job(lambda: asyncio.create_task(do_announce()), trig_announce)


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


# ---------- Comandi ----------
@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    adm = "sì" if is_admin(ctx) else "no"
    await ctx.reply(f"ID: {ctx.author.id} — sei admin: {adm}", mention_author=False)

@bot.command(name="mostralivelli")
async def mostralivelli(ctx: commands.Context):
    wins = STATE.get("wins", {})
    if not wins:
        await ctx.reply("📜 Nessun livello registrato al momento.", mention_author=False)
        return
    lines = []
    for uid, w in wins.items():
        member = ctx.guild.get_member(int(uid))
        tag = member.mention if member else f"<@{uid}>"
        lines.append(f"{tag}: vittorie = {w}, livello = {level_from_wins(w)}")
    embed = golden_embed("REGISTRO LIVELLI", "\n".join(lines))
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="resetlivelli")
async def resetlivelli(ctx: commands.Context):
    if not is_admin(ctx):
        return
    STATE["wins"] = {}
    save_state(STATE)
    await ctx.reply("🔄 Tutti i livelli sono stati azzerati (wins = 0 per tutti).", mention_author=False)

@bot.command(name="resetlotteria")
async def resetlotteria(ctx: commands.Context):
    if not is_admin(ctx):
        return
    STATE["edition"] = 1
    STATE["open_message_id"] = None
    STATE["participants"] = []
    save_state(STATE)
    await ctx.reply("🧹 Lotteria resettata: edizione=1, partecipanti azzerati.", mention_author=False)

@bot.command(name="testcycle")
async def testcycle(ctx: commands.Context):
    """Simula un ciclo completo con messaggi reali (apertura → chiusura → annuncio)."""
    if not is_admin(ctx):
        return

    guild = ctx.guild
    channel = await find_lottery_channel(guild)
    if not channel:
        await ctx.reply("⚠️ Canale lotteria non trovato.", mention_author=False)
        return

    await ctx.reply(
        "🧪 **Avvio ciclo di test completo:**\n"
        "📜 Apertura → (20s) → Chiusura → (20s) → Annuncio (10s dopo).",
        mention_author=False
    )

    # --- Apertura ---
    await post_open_message(channel)
    await asyncio.sleep(20)  # tempo per reagire

    # --- Chiusura e selezione vincitore (senza annuncio immediato) ---
    await close_and_pick(guild, announce_now=False)
    await asyncio.sleep(20)

    # --- Annuncio del vincitore ---
    winner_id = STATE.get("last_winner_id")
    winner = None
    if winner_id:
        try:
            winner = await guild.fetch_member(winner_id)
        except Exception:
            winner = ctx.guild.get_member(winner_id)

    await post_winner_announcement(channel, winner)
    await asyncio.sleep(10)

    await ctx.reply("✅ **Test completo terminato.**", mention_author=False)


# ---------- Avvio ----------
if __name__ == "__main__":
    start_web_server()  # server HTTP per Render
    bot.run(TOKEN)
