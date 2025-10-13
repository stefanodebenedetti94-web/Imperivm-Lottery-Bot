# === IMPERIVM LOTTERY BOT — EPIC EDITION (Render-ready) ===
import os, json, random, asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Thread

import discord
from discord.ext import commands, tasks
from flask import Flask

# ---------------- CONFIG ----------------
TZ = ZoneInfo("Europe/Rome")

# Prima esecuzione ufficiale: NON parte nulla prima di questa data/ora
START_DATE = datetime(2025, 10, 22, 0, 0, tzinfo=TZ)  # 22/10/2025 00:00 (ora italiana)

# Canale: usa l'ID se lo imposti come ENV su Render, altrimenti cerca per nome
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_NAME_FALLBACK = os.getenv("CHANNEL_NAME", "lotteria-imperivm")

STATE_FILE = "lotteria_data.json"
REACTION_EMOJI = "✅"

# ---------------- KEEP-ALIVE WEB (Render) ----------------
app = Flask(__name__)

@app.get("/")
def index():
    return "IMPERIVM Lottery Bot è vivo 👑"

def start_web_server():
    port = int(os.getenv("PORT", "8080"))
    Thread(target=lambda: app.run(host="0.0.0.0", port=port)).start()

# ---------------- STATO PERSISTENTE ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "edition": 1,
        "participants": [],   # elenco user_id (stringhe) della EDIZIONE CORRENTE
        "wins": {}            # mappa user_id (string) -> 0|1|2 (vittorie cumulate)
    }

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()

# ---------------- DISCORD ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)

def is_admin(ctx: commands.Context) -> bool:
    return getattr(ctx.author.guild_permissions, "administrator", False)

def get_lottery_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if CHANNEL_ID:
        ch = guild.get_channel(CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    # fallback per nome
    for ch in guild.text_channels:
        if ch.name == CHANNEL_NAME_FALLBACK:
            return ch
    return None

async def spacer(channel: discord.TextChannel):
    # Messaggio invisibile per separare visivamente i post (no “ammucchiata”)
    await channel.send("‎")

# ---------------- TESTI EPICI ----------------
MSG_START = (
    "📜 **[ LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE ]** 📜\n\n"
    "Cittadini dell’Impero 👑,\n"
    "è giunto il momento di sfidare la sorte sotto lo stendardo dorato dell’IMPERIVM!\n"
    "Da **ora** fino alle **00:00 di giovedì**, la lotteria imperiale è ufficialmente **aperta**! 🎟️\n\n"
    f"Reagite con {REACTION_EMOJI} a **questo messaggio** per prendere parte all’estrazione.\n"
    "Il destino premierà solo i più audaci!\n\n"
    "⚔️ **Premi in palio:**\n"
    "🏅 1ª vittoria → **100.000 Kama**\n"
    "🥈 2ª vittoria → **Scudo di Gilda** *(se già posseduto → **250.000 Kama**)*\n"
    "🥇 3ª vittoria → **500.000 Kama** *(reset dei livelli)*\n\n"
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

# ---------------- ROUTINE LOTTERIA ----------------
async def start_lottery(guild: discord.Guild):
    ch = get_lottery_channel(guild)
    if not ch:
        return
    # Messaggio di apertura + reazione
    await ch.send(f"🕛 **Edizione n°{state['edition']} — Apertura ufficiale**")
    await spacer(ch)
    m = await ch.send(MSG_START + f"\n\n**Edizione n°{state['edition']}**")
    try:
        await m.add_reaction(REACTION_EMOJI)
    except Exception:
        pass
    # Pulisci partecipanti per la nuova edizione
    state["participants"] = []
    save_state()
    await spacer(ch)

async def close_lottery(guild: discord.Guild):
    ch = get_lottery_channel(guild)
    if not ch:
        return
    await ch.send(MSG_CLOSED)
    await spacer(ch)

async def announce_winner(guild: discord.Guild):
    ch = get_lottery_channel(guild)
    if not ch:
        return

    # Se non ci sono partecipanti registrati, prova comunque a leggerli dal messaggio di apertura (se servisse).
    # Nota: in questa versione si registrano con on_reaction_add, quindi normalmente state["participants"] è già pieno.
    participants = state.get("participants", [])
    if not participants:
        await ch.send("📜 **Nessun partecipante valido questa settimana.** Torniamo mercoledì! 👑")
        await spacer(ch)
        # passa comunque all’edizione successiva
        state["edition"] += 1
        save_state()
        return

    # Estrazione casuale
    winner_id = random.choice(participants)
    try:
        user = await bot.fetch_user(int(winner_id))
        mention = user.mention
    except Exception:
        mention = f"<@{winner_id}>"

    # Calcolo livello/premio
    prev = state["wins"].get(str(winner_id), 0)  # 0,1,2
    level = prev + 1
    if level == 1:
        prize = "100.000 Kama"
        state["wins"][str(winner_id)] = 1
    elif level == 2:
        # Non verifichiamo il ruolo scudo su questa versione: comunichiamo l'alternativa
        prize = "Scudo di Gilda (se già posseduto → 250.000 Kama)"
        state["wins"][str(winner_id)] = 2
    else:  # level == 3
        prize = "500.000 Kama (reset livelli)"
        state["wins"][str(winner_id)] = 0  # reset

    save_state()

    # Annuncio epico + registro rapido
    await ch.send(msg_winner(mention, level, prize))
    await spacer(ch)
    await ch.send(
        f"🗒️ **Registro rapido** – Ed. n°{state['edition']} | "
        f"Vincitore: {mention} | Livello {level} → {prize}"
    )

    # Avanza edizione
    state["edition"] += 1
    state["participants"] = []
    save_state()
    await spacer(ch)

# ---------------- SCHEDULER AUTOMATICO ----------------
@tasks.loop(minutes=1)
async def scheduler():
    now = datetime.now(TZ)
    if now < START_DATE:
        return
    for guild in bot.guilds:
        # Mercoledì 00:00 – apertura
        if now.weekday() == 2 and now.hour == 0 and now.minute == 0:
            await start_lottery(guild)
        # Giovedì 00:00 – chiusura
        if now.weekday() == 3 and now.hour == 0 and now.minute == 0:
            await close_lottery(guild)
        # Giovedì 08:00 – annuncio vincitore
        if now.weekday() == 3 and now.hour == 8 and now.minute == 0:
            await announce_winner(guild)

# ---------------- REAZIONI (partecipazione) ----------------
@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot or str(reaction.emoji) != REACTION_EMOJI:
        return
    guild = reaction.message.guild
    if not guild:
        return
    ch = get_lottery_channel(guild)
    if not ch or reaction.message.channel.id != ch.id:
        return
    uid = str(user.id)
    if uid not in state["participants"]:
        state["participants"].append(uid)
        save_state()

# ---------------- COMANDI ADMIN ----------------
@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    await ctx.reply(f"ID: `{ctx.author.id}` | Admin: {'✅' if is_admin(ctx) else '❌'}")

@bot.command(name="testcycle")
async def testcycle(ctx: commands.Context):
    if not is_admin(ctx):
        return
    await ctx.reply("🧪 Avvio ciclo di test: apertura → chiusura → vincitore.")
    await start_lottery(ctx.guild)
    await asyncio.sleep(2)
    await close_lottery(ctx.guild)
    await asyncio.sleep(2)
    await announce_winner(ctx.guild)
    await ctx.send("✅ Test completo terminato.")

@bot.command(name="resetlotteria")
async def resetlotteria(ctx: commands.Context):
    if not is_admin(ctx):
        return
    state["edition"] = 1
    state["participants"] = []
    save_state()
    await ctx.reply("♻️ **Lotteria resettata**: edizione=1, partecipanti azzerati.")

@bot.command(name="resetlivelli")
async def resetlivelli(ctx: commands.Context):
    if not is_admin(ctx):
        return
    state["wins"] = {}
    save_state()
    await ctx.reply("🔄 Tutti i **livelli** sono stati azzerati (wins = 0 per tutti).")

@bot.command(name="mostralivelli")
async def mostralivelli(ctx: commands.Context):
    if not state["wins"]:
        await ctx.reply("📜 Nessun livello registrato al momento.")
        return
    ordered = sorted(state["wins"].items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"<@{uid}> → Livello {lvl}" for uid, lvl in ordered]
    await ctx.reply("🏅 **Classifica livelli attuale:**\n" + "\n".join(lines))

# ---------------- AVVIO ----------------
@bot.event
async def on_ready():
    print(f"✅ Online come {bot.user} | Edizione corrente: {state['edition']}")
    if not scheduler.is_running():
        scheduler.start()
    # Messaggio di servizio (opzionale): annuncia prossima apertura
    for guild in bot.guilds:
        ch = get_lottery_channel(guild)
        if ch:
            nxt = START_DATE.astimezone(TZ).strftime("%d/%m %H:%M")
            await ch.send(f"🛰️ **IMPERIVM Lottery Bot – Online.** Prossima apertura prevista: **{nxt}** (ora italiana).")
            await spacer(ch)

if __name__ == "__main__":
    start_web_server()  # keep-alive per Render
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN mancante nelle Environment Variables di Render.")
    bot.run(token)
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
