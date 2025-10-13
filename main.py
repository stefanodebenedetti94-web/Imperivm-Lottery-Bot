# ===== IMPERIVM LOTTERY BOT — Render-ready (Flask keepalive) =====
import os, json, random, asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Thread

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- Web server keepalive (Render health check) ---
from flask import Flask
from waitress import serve

app = Flask(__name__)
@app.get("/")
def index():
    return "IMPERIVM Lottery Bot è vivo 👑"

def start_web_server():
    port = int(os.getenv("PORT", "8080"))
    # waitress è bloccante: lo avvio in un thread separato
    t = Thread(target=serve, args=(app,), kwargs={"host":"0.0.0.0","port":port}, daemon=True)
    t.start()

# ================= CONFIG =================
TOKEN = os.getenv("DISCORD_TOKEN")            # ⚠️ Su Render: Environment → DISCORD_TOKEN
CHANNEL_ID = 1426994508347867286              # 🏁 canale #lotteria-imperiale
TZ = ZoneInfo("Europe/Rome")

# Prima partenza automatica
START_DATE = datetime(2025, 10, 22, 0, 0, tzinfo=TZ)

# Admin (utenti) autorizzati ai comandi manuali
ADMIN_IDS = {770034715880194058, 938135116285349938}
ADMIN_ROLE_ID = None  # opzionale: se vuoi consentire a chi ha un certo ruolo

# Ruolo che indica chi possiede già lo "Scudo di Gilda" (opzionale)
SHIELD_ROLE_ID = None

# Opzionale: mini icona per gli embed
LOGO_URL = None

REACTION_EMOJI = "✅"
DATA_FILE = "lotteria_data.json"

# ================= MESSAGGISTICA =================
def banner_embed(title: str, desc: str, color=0xB30000):
    e = discord.Embed(title=title, description=desc, color=color)
    if LOGO_URL:
        e.set_thumbnail(url=LOGO_URL)
    e.set_footer(text="IMPERIVM • Lotteria settimanale")
    return e

MSG_START = (
    "📜 **[ LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE ]** 📜\n\n"
    "Cittadini dell’Impero 👑,\n"
    "è giunto il momento di sfidare la sorte sotto lo stendardo dorato dell’IMPERIVM!\n"
    "Da **ora** fino alle **00:00 di giovedì**, la lotteria imperiale è ufficialmente **aperta**! 🎟️\n\n"
    f"Reagite con {REACTION_EMOJI} a questo messaggio per prendere parte all’estrazione.\n"
    "Il destino premierà solo i più audaci!\n\n"
    "⚔️ **Premi in palio:**\n"
    "🏅 1ª vittoria → 100.000 Kama\n"
    "🥈 2ª vittoria → Scudo di Gilda *(se già posseduto → 250.000 Kama)*\n"
    "🥇 3ª vittoria → 500.000 Kama *(reset dei livelli)*\n\n"
    "Buona fortuna, e che la fortuna serva l’Impero! 👑"
)

MSG_CLOSED = (
    "⚔️ **LOTTERIA IMPERIVM – CHIUSA!** ⚔️\n\n"
    "La sorte ha parlato… ma il verdetto sarà svelato all’alba. 🌅\n"
    "Tutti i biglietti sono stati raccolti, il fato è in bilico tra le mani degli Dei.\n\n"
    "📜 Il vincitore verrà **annunciato ufficialmente alle 08:00 di giovedì**.\n"
    "Restate sintonizzati, cittadini dell’Impero 👑"
)

def msg_winner(user_mention: str, level: int, prize: str) -> str:
    return (
        "🎉 **[ ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM ]** 🎉\n\n"
        "Cittadini dell’Impero 👑,\n"
        "il fato ha deciso, e il nome del vincitore è stato scritto negli annali dell’Imperium! ⚖️\n\n"
        f"🏆 Vincitore dell’edizione: **{user_mention}**\n"
        f"🎖️ Livello: **{level}**\n"
        f"💰 Ricompensa: **{prize}**\n"
        "📜 La prossima estrazione avverrà **mercoledì prossimo a mezzanotte!**\n\n"
        "Gloria all’Imperium e buona fortuna ai futuri partecipanti! ⚔️"
    )

# ================= STATO PERSISTENTE =================
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

state = {
    "edition": 1,
    "lottery_message_id": None,
    "accepting": False,
    "participants": [],          # [user_id, ...]
    "wins": {}                   # {user_id: 0|1|2}
}

def load_state():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            state.update(data)
    except FileNotFoundError:
        save_state()

def save_state():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ================= UTILITY =================
def is_admin(ctx: commands.Context) -> bool:
    if ctx.author.id in ADMIN_IDS:
        return True
    if ADMIN_ROLE_ID:
        r = discord.utils.get(getattr(ctx.author, "roles", []), id=ADMIN_ROLE_ID)
        if r:
            return True
    return False

async def spacer(channel: discord.TextChannel):
    # Messaggio invisibile per “distanziare” i post su Discord
    await channel.send("‎")

# ================= SCHEDULER =================
scheduler = AsyncIOScheduler(timezone=TZ)

@bot.event
async def on_ready():
    start_web_server()   # avvia il server HTTP per Render
    load_state()
    print(f"✅ {bot.user} online. Edizione corrente: {state['edition']}")
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        next_run = START_DATE.astimezone(TZ).strftime("%d/%m %H:%M")
        await channel.send(
            embed=banner_embed(
                "IMPERIVM Lottery Bot – Online",
                f"Il servizio imperiale è attivo.\n"
                f"📅 Prossima apertura prevista: **{next_run}** (ora italiana).\n"
                f"Canale: <#{CHANNEL_ID}>"
            )
        )

    # Pianifica da START_DATE in poi
    scheduler.add_job(start_lottery,  "cron", day_of_week="wed", hour=0, minute=0, start_date=START_DATE)
    scheduler.add_job(close_lottery,  "cron", day_of_week="thu", hour=0, minute=0, start_date=START_DATE)
    scheduler.add_job(announce_winner,"cron", day_of_week="thu", hour=8, minute=0, start_date=START_DATE)
    scheduler.start()

# ================= ROUTINE LOTTERIA =================
async def start_lottery():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("❌ CHANNEL_ID non valido o permessi insufficienti.")
        return

    state["participants"] = []
    state["accepting"] = True

    await channel.send(embed=banner_embed(
        f"LOTTERIA IMPERIVM – Edizione n°{state['edition']}",
        "La lotteria è **aperta**! Reagisci con ✅ per partecipare."
    ))
    await spacer(channel)
    m = await channel.send(MSG_START + f"\n\n**Edizione n°{state['edition']}**")
    await m.add_reaction(REACTION_EMOJI)

    state["lottery_message_id"] = m.id
    save_state()
    print(f"📝 Lotteria aperta – messaggio {m.id}, edizione {state['edition']}")

async def close_lottery():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return
    state["accepting"] = False
    save_state()
    await spacer(channel)
    await channel.send(MSG_CLOSED)
    print("🔒 Lotteria chiusa – in attesa di annuncio 08:00")

async def announce_winner():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return

    # ricava partecipanti dalla reazione
    try:
        if state["lottery_message_id"]:
            msg = await channel.fetch_message(state["lottery_message_id"])
            for r in msg.reactions:
                if str(r.emoji) == REACTION_EMOJI:
                    users = [u async for u in r.users() if not u.bot]
                    ids = set(state["participants"]) | {u.id for u in users}
                    state["participants"] = list(ids)
                    break
    except discord.NotFound:
        pass

    if not state["participants"]:
        await spacer(channel)
        await channel.send("📜 **Nessun partecipante valido questa settimana.** Torniamo mercoledì! 👑")
        print("⚠️ Nessun partecipante – nessun vincitore.")
        # passa comunque all’edizione successiva
        state["edition"] += 1
        state["lottery_message_id"] = None
        state["participants"] = []
        state["accepting"] = False
        save_state()
        return

    winner_id = random.choice(state["participants"])
    try:
        winner = await bot.fetch_user(winner_id)
        mention = winner.mention
    except Exception:
        mention = f"<@{winner_id}>"

    wins = state["wins"].get(str(winner_id), 0)  # 0,1,2
    level = wins + 1

    # premio in base al livello
    if level == 1:
        prize = "100.000 Kama"
        state["wins"][str(winner_id)] = 1
    elif level == 2:
        has_shield = False
        if SHIELD_ROLE_ID:
            for g in bot.guilds:
                member = g.get_member(winner_id)
                if member:
                    has_shield = any(r.id == SHIELD_ROLE_ID for r in member.roles)
                    break
        prize = "250.000 Kama (già in possesso dello Scudo di Gilda)" if has_shield else "Scudo di Gilda"
        state["wins"][str(winner_id)] = 2
    else:  # level == 3
        prize = "500.000 Kama (reset livelli)"
        state["wins"][str(winner_id)] = 0  # reset

    save_state()

    await spacer(channel)
    await channel.send(msg_winner(mention, level, prize))
    await spacer(channel)
    await channel.send(
        f"🗒️ **Registro rapido** – Ed. n°{state['edition']} | Vincitore: {mention} | Livello {level} → {prize}"
    )

    state["edition"] += 1
    state["lottery_message_id"] = None
    state["participants"] = []
    state["accepting"] = False
    save_state()
    print(f"👑 Vincitore Ed.{state['edition']-1}: {mention} (Livello {level}) – {prize}")

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or not state["accepting"]:
        return
    if reaction.message.id != state.get("lottery_message_id"):
        return
    if str(reaction.emoji) != REACTION_EMOJI:
        return
    if user.id not in state["participants"]:
        state["participants"].append(user.id)
        save_state()

# ================= COMANDI ADMIN =================
@bot.command(name="whoami")
async def whoami(ctx):
    if not is_admin(ctx): return
    await ctx.reply(f"ID: `{ctx.author.id}` – sei admin ✅")

@bot.command(name="startlotteria")
async def cmd_start(ctx):
    if not is_admin(ctx): return
    await ctx.send("🧪 Avvio *manuale*: apertura.")
    await start_lottery()

@bot.command(name="chiudilotteria")
async def cmd_close(ctx):
    if not is_admin(ctx): return
    await ctx.send("🧪 Chiusura *manuale* in corso.")
    await close_lottery()

@bot.command(name="annunciavincitore")
async def cmd_winner(ctx):
    if not is_admin(ctx): return
    await ctx.send("🧪 Annuncio vincitore *manuale* in corso.")
    await announce_winner()

@bot.command(name="testcycle")
async def testcycle(ctx):
    if not is_admin(ctx): return
    await ctx.send("🧪 Avvio ciclo di test: apertura 30s → chiusura 30s → annuncio 30s.")
    await start_lottery()
    await asyncio.sleep(30)
    await close_lottery()
    await asyncio.sleep(30)
    await announce_winner()
    await asyncio.sleep(2)
    await ctx.send("✅ Test completo terminato.")

@bot.command(name="resetlotteria")
async def resetlotteria(ctx, mode: str = "soft"):
    """!resetlotteria [soft|hard|edizione N]
       - soft: resetta stato e edizione=1 ma conserva storico vittorie
       - hard: reset completo (vince/storico cancellati)
       - edizione N: imposta il contatore edizioni a N
    """
    if not is_admin(ctx): return

    if mode.lower() == "hard":
        state.update({"edition":1, "lottery_message_id":None, "accepting":False, "participants":[], "wins":{}})
        save_state()
        await ctx.reply("♻️ **Reset HARD** eseguito: stato e storico azzerati; edizione riportata a **1**.")
        return

    if mode.lower() == "soft":
        state.update({"edition":1, "lottery_message_id":None, "accepting":False, "participants":[]})
        save_state()
        await ctx.reply("♻️ **Reset SOFT** eseguito: stato azzerato; edizione riportata a **1** (storico vittorie conservato).")
        return

    # edizione N
    try:
        n = int(mode)
        if n < 1: raise ValueError
        state["edition"] = n
        save_state()
        await ctx.reply(f"🧮 Edizione impostata a **{n}**.")
    except Exception:
        await ctx.reply("Uso: `!resetlotteria [soft|hard|edizione N]`")

# ================= AVVIO =================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN mancante nelle Environment Variables.")

    bot.run(TOKEN)
