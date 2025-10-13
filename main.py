# === IMPERIVM Lottery Bot - main.py ===
# Render Web Service: Flask per ping e Discord bot in esecuzione costante.

import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ---- Flask (endpoint di salute) ----
from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.get("/")
def index():
    return "IMPERIVM Lottery Bot è vivo 👑"

def start_web_server():
    # Render espone già la porta in PORT
    port = int(os.getenv("PORT", "8080"))
    # waitress è bloccante: lo avvio in un thread separato
    from waitress import serve
    t = Thread(target=serve, args=(app,), kwargs={"host":"0.0.0.0", "port":port}, daemon=True)
    t.start()

# ---- Configurazione ----

# Timezone
TZ = ZoneInfo("Europe/Rome")

# Data di inizio AUTOMAZIONE: mer 22/10/2025 h00:00 Europe/Rome
START_DATE = datetime(2025, 10, 22, 0, 0, 0, tzinfo=TZ)

# Admin: user IDs autorizzati ai comandi manuali
ADMIN_IDS = {
    770034715880194058,
    938135116285349938,
}

# (opzionale) ID ruolo admin se vuoi autorizzare un ruolo intero
ADMIN_ROLE_ID = None  # es. 112233445566778899

# Nome canale fallback
FALLBACK_CHANNEL_NAME = "lotteria-imperivm"

# File dati
DATA_FILE = "lotteria_data.json"

# ---- Stato persistente ----

def _empty_state():
    return {
        "edition": 1,
        "open_message_id": None,
        "participants": [],        # user_id che hanno reagito ✅
        "wins": {},                # user_id -> quante vittorie totali (storico)
        "levels": {}               # user_id -> livello (1..3, si azzera con resetlivelli)
    }

def load_state():
    if not os.path.exists(DATA_FILE):
        return _empty_state()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _empty_state()

def save_state(state):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()

# ---- Utility ----

def is_admin(ctx_or_member):
    # accetta ctx o Member
    user = ctx_or_member.author if hasattr(ctx_or_member, "author") else ctx_or_member
    if user.id in ADMIN_IDS:
        return True
    if ADMIN_ROLE_ID and any(r.id == ADMIN_ROLE_ID for r in getattr(user, "roles", [])):
        return True
    return False

def fmt_user(guild, user_id):
    m = guild.get_member(user_id)
    return m.mention if m else f"<@{user_id}>"

async def get_lottery_channel(guild: discord.Guild) -> discord.TextChannel | None:
    # se c'è CHANNEL_ID in env, usa quello
    cid = os.getenv("CHANNEL_ID")
    if cid:
        try:
            ch = guild.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                return ch
        except:
            pass
    # fallback per nome
    ch = discord.utils.get(guild.text_channels, name=FALLBACK_CHANNEL_NAME)
    return ch

# ---- Messaggi/Embed ----

def embed_title(title: str) -> discord.Embed:
    return discord.Embed(title=title, color=0x2F3136)

def open_lottery_embed(edition: int) -> discord.Embed:
    e = embed_title("LOTTERIA IMPERIVM – Edizione n°{}".format(edition))
    e.description = (
        "La lotteria è **aperta**!\n"
        "Reagisci con ✅ per partecipare.\n\n"
        "IMPERIVM • Lotteria settimanale"
    )
    return e

def info_open_embed() -> discord.Embed:
    e = embed_title("[ 📜 LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE ]")
    e.description = (
        "Cittadini dell’Impero 👑,\n"
        "è giunto il momento di sfidare la sorte sotto lo stendardo dorato dell’IMPERIVM!\n"
        "Da **ora** fino alle **00:00 di giovedì**, la lotteria imperiale è ufficialmente **aperta**! 🧾\n\n"
        "Reagite **✅** a questo messaggio per prendere parte all’estrazione.\n"
        "Il destino premierà solo i più audaci!\n\n"
        "⚔️ Premi in palio:\n"
        "🥇 1ª vittoria → 100.000 Kama\n"
        "🥈 2ª vittoria → Scudo di Gilda *(se già posseduto → 250.000 Kama)*\n"
        "🥉 3ª vittoria → 500.000 Kama *(reset dei livelli)*\n\n"
        "Buona fortuna, e che la fortuna serva l’Impero! 👑\n\n"
        "**Edizione n°{}**".format(state["edition"])
    )
    return e

def closed_embed() -> discord.Embed:
    e = embed_title("⚔️ LOTTERIA IMPERIVM – CHIUSA! ⚔️")
    e.description = (
        "La sorte ha parlato… ma il verdetto sarà svelato all’alba. 🌅\n"
        "Tutti i biglietti sono stati raccolti, il fato è in bilico tra le mani degli Dei.\n\n"
        "📝 Il vincitore verrà **annunciato ufficialmente alle 08:00 di giovedì**."
    )
    return e

def winner_embed(guild: discord.Guild, winner_id: int, new_level: int) -> discord.Embed:
    e = embed_title("🎉 [ ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM ] 🎉")
    e.description = (
        "Cittadini dell’Impero 👑,\n"
        "il fato ha deciso, e il nome del vincitore è stato scritto negli annali dell’Imperium! ⚖️\n\n"
        f"🏆 Vincitore dell’edizione: {fmt_user(guild, winner_id)}\n"
        f"🏅 Livello: **{new_level}**\n"
        "💰 Ricompensa: **100.000 Kama** *(alla 1ª vittoria; vedi regole soprà)*\n\n"
        "La prossima estrazione avverrà **mercoledì prossimo a mezzanotte**!\n\n"
        "Gloria all’Imperium e buona fortuna ai futuri partecipanti! ⚔️"
    )
    return e

# ---- Discord Bot ----

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---- Funzioni core ----

async def start_lottery(guild: discord.Guild):
    ch = await get_lottery_channel(guild)
    if ch is None:
        return

    # reset partecipanti dell'edizione corrente
    state["participants"] = []
    state["open_message_id"] = None
    save_state(state)

    e1 = open_lottery_embed(state["edition"])
    msg = await ch.send(embed=e1)
    await msg.add_reaction("✅")

    e2 = info_open_embed()
    await ch.send(embed=e2)
    # riga vuota per “aria”
    await ch.send("\u200b")

    # ricorda l'ID del messaggio per leggere le reazioni in chiusura
    state["open_message_id"] = msg.id
    save_state(state)

async def close_lottery(guild: discord.Guild):
    ch = await get_lottery_channel(guild)
    if ch is None:
        return

    # raccogli partecipanti dalla reazione ✅ del messaggio aperto
    if state["open_message_id"]:
        try:
            msg = await ch.fetch_message(state["open_message_id"])
            for r in msg.reactions:
                if str(r.emoji) == "✅":
                    users = [u async for u in r.users() if not u.bot]
                    state["participants"] = sorted({u.id for u in users})
                    break
        except discord.NotFound:
            pass

    await ch.send(embed=closed_embed())
    await ch.send("\u200b")
    save_state(state)

async def announce_winner(guild: discord.Guild):
    ch = await get_lottery_channel(guild)
    if ch is None:
        return

    participants = state.get("participants", [])
    if not participants:
        await ch.send("😕 Nessun partecipante questa settimana. Riproviamo mercoledì prossimo!")
        # prepara comunque alla prossima edizione
        state["edition"] += 1
        state["open_message_id"] = None
        state["participants"] = []
        save_state(state)
        return

    # estrazione casuale equa
    import random
    winner_id = random.choice(participants)

    # aggiorna storico vittorie + livello
    wins = state["wins"].get(str(winner_id), 0) + 1
    state["wins"][str(winner_id)] = wins

    # livello: 1 alla prima, 2 alla seconda, 3 alla terza poi reset a 0 (riparte da 1 la volta successiva)
    prev_level = state["levels"].get(str(winner_id), 0)
    new_level = prev_level + 1
    if new_level > 3:
        new_level = 1
    state["levels"][str(winner_id)] = new_level

    await ch.send(embed=winner_embed(guild, winner_id, new_level))

    # registro rapido
    await ch.send(
        f"🗒️ **Registro rapido** – Ed. n°{state['edition']} | "
        f"Vincitore: {fmt_user(guild, winner_id)} | Livello {new_level}"
    )
    await ch.send("\u200b")

    # prepara prossima edizione
    state["edition"] += 1
    state["open_message_id"] = None
    state["participants"] = []
    save_state(state)

# ---- Scheduler settimanale ----

scheduler = AsyncIOScheduler(timezone=str(TZ))

@scheduler.scheduled_job(CronTrigger(day_of_week="wed", hour=0, minute=0))
async def job_open():
    now = datetime.now(tz=TZ)
    if now < START_DATE:
        return
    for guild in bot.guilds:
        await start_lottery(guild)

@scheduler.scheduled_job(CronTrigger(day_of_week="thu", hour=0, minute=0))
async def job_close():
    now = datetime.now(tz=TZ)
    if now < START_DATE:
        return
    for guild in bot.guilds:
        await close_lottery(guild)

@scheduler.scheduled_job(CronTrigger(day_of_week="thu", hour=8, minute=0))
async def job_announce():
    now = datetime.now(tz=TZ)
    if now < START_DATE:
        return
    for guild in bot.guilds:
        await announce_winner(guild)

# ---- Comandi ----

@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    if is_admin(ctx):
        await ctx.reply(f"**BOT LOTTERIA IMPERIVM 👑**\nID: `{ctx.author.id}` – sei **admin** ✅")
    else:
        await ctx.reply(f"**BOT LOTTERIA IMPERIVM 👑**\nID: `{ctx.author.id}` – non sei admin.")

@bot.command(name="resetlotteria")
async def resetlotteria(ctx: commands.Context):
    if not is_admin(ctx):
        return
    state["edition"] = 1
    state["open_message_id"] = None
    state["participants"] = []
    save_state(state)
    await ctx.reply("🧼 **Lotteria resettata**: edizione=1, partecipanti azzerati.")

@bot.command(name="resetlivelli")
async def resetlivelli(ctx: commands.Context):
    if not is_admin(ctx):
        return
    state["levels"] = {}
    save_state(state)
    await ctx.reply("🔄 Tutti i **livelli** sono stati azzerati (wins = 0 per tutti).")

@bot.command(name="mostralivelli")
async def mostralivelli(ctx: commands.Context):
    if not is_admin(ctx):
        return
    guild = ctx.guild
    levels = state.get("levels", {})
    if not levels:
        await ctx.reply("📜 Nessun livello registrato al momento.")
        return

    # ordina per livello desc poi per nome
    items = []
    for uid, lvl in levels.items():
        try:
            uid_i = int(uid)
        except:
            continue
        member = guild.get_member(uid_i)
        name = member.display_name if member else f"utente {uid}"
        items.append((lvl, name, uid_i))
    items.sort(key=lambda x: (-x[0], x[1].lower()))

    lines = [f"{i+1}. **{name}** ({fmt_user(guild, uid)}) → Livello **{lvl}**"
             for i, (lvl, name, uid) in enumerate(items)]
    text = "🏆 **Livelli attuali**\n" + "\n".join(lines)
    await ctx.reply(text)

@bot.command(name="testcycle")
async def testcycle(ctx: commands.Context):
    # Solo admin
    if not is_admin(ctx):
        return

    guild = ctx.guild
    chan = await get_lottery_channel(guild)
    if chan is None:
        await ctx.reply("❌ Non trovo il canale della lotteria. Imposta `CHANNEL_ID` o rinomina un canale in `#lotteria-imperivm`.")
        return

    await ctx.reply(
        f"🧪 Avvio ciclo di test **in** {chan.mention}: **Apertura → (30s) → Chiusura → (10s) → Annuncio**.\n"
        "Reagisci con ✅ al messaggio di apertura!"
    )

    # Apertura
    await start_lottery(guild)
    await asyncio.sleep(30)  # tempo per reagire

    # Chiusura
    await close_lottery(guild)
    await asyncio.sleep(10)

    # Annuncio
    await announce_winner(guild)

    await ctx.send("✅ Test completo terminato.")

# ---- Eventi ----

@bot.event
async def on_ready():
    scheduler.start()
    # stato bot
    try:
        await bot.change_presence(activity=discord.Game(name="Lotteria IMPERIVM"))
    except:
        pass
    # log su console
    print(f"✅ {bot.user} online. Edizione corrente: {state['edition']}")

# ---- Avvio ----

if __name__ == "__main__":
    # Avvia il web server (Render)
    start_web_server()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN mancante nelle Environment Variables.")

    bot.run(token)
