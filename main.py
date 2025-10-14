# === LOTTERIA IMPERIVM – Bot Discord (Render-ready) ============================
# Requisiti (requirements.txt):
# discord.py==2.3.2
# APScheduler==3.10.4
# Flask==3.0.0
# waitress==2.1.2
# Procfile: web: python3 main.py

import os
import json
import random
import asyncio
from datetime import datetime
from threading import Thread

import discord
from discord.ext import commands
from discord import app_commands

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

# --- Piccolo web server per Render (risponde a /) -----------------------------
from flask import Flask
app = Flask(__name__)

@app.get("/")
def index():
    return "IMPERIVM Lottery Bot è vivo 📜"

def start_web_server():
    # waitress è bloccante: lo avvio in un thread parallelo
    port = int(os.getenv("PORT", "8080"))
    def _run():
        from waitress import serve
        serve(app, host="0.0.0.0", port=port)
    Thread(target=_run, daemon=True).start()

# --- CONFIG -------------------------------------------------------------------
TZ = ZoneInfo("Europe/Rome")

# Data di partenza automatica (mercoledì 22 ottobre 2025 alle 00:00)
START_DATE = datetime(2025, 10, 22, 0, 0, 0, tzinfo=TZ)

# ID canale (se 0, cerco per nome)
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_CANDIDATE_NAMES = ["lotteria-imperivm", "lotteria-imperiale"]

# Admin (ID utenti autorizzati ai comandi manuali)
ADMIN_IDS = {
    770034715880194058,
    938135116285349938,
}

# ID ruolo “Scudo di Gilda” (opzionale). Se None, ignora.
SHIELD_ROLE_ID = None

# Colori / stile
COLOR_GOLD = 0xF1C40F
COLOR_NEUTRAL = 0x2B2D31  # embed scuro elegante

# File stato
STATE_FILE = "lotteria_state.json"

# --- STATO --------------------------------------------------------------------
def default_state():
    return {
        "edizione": 1,
        "current_message_id": None,
        "partecipanti_ids": [],     # raccolti alla chiusura
        "wins": {}                  # {user_id (str): num_vittorie}
    }

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        s = default_state()
        save_state(s)
        return s

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

state = load_state()

# --- HELPERS ------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

scheduler = AsyncIOScheduler(timezone=str(TZ))

def is_admin(user: discord.abc.User) -> bool:
    return user.id in ADMIN_IDS

def zws_line():
    # linea vuota “spaziatrice” tra embed
    return "\u200B"

def center_title(text: str) -> str:
    # Discord non centra davvero i titoli degli embed;
    # ma metto il titolo come “cornice” coerente su ogni embed.
    return f"📜 {text} 📜"

def make_embed(title: str, description: str, color=COLOR_GOLD):
    e = discord.Embed(
        title=center_title(title),
        description=description,
        color=color
    )
    e.set_footer(text="Imperivm • Lotteria settimanale")
    return e

async def find_channel(guild: discord.Guild) -> discord.TextChannel | None:
    # Se hai messo CHANNEL_ID (>0), lo uso. Altrimenti cerco per nome.
    if CHANNEL_ID:
        ch = guild.get_channel(CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    # cerca per nome
    for name in CHANNEL_CANDIDATE_NAMES:
        c = discord.utils.get(guild.text_channels, name=name)
        if c:
            return c
    return None

def prize_text():
    # Blocco premi, riutilizzato
    return (
        "⚔️ **Premi in palio:**\n"
        "🥇 **1ª vittoria** → **100.000 Kama**\n"
        "🥈 **2ª vittoria** → **Scudo di Gilda** *(se già posseduto → **250.000 Kama**)*\n"
        "🥉 **3ª vittoria** → **500.000 Kama** *(reset dei livelli)*\n"
    )

# --- APERTURA / CHIUSURA / ANNUNCIO ------------------------------------------
async def post_apertura(channel: discord.TextChannel):
    global state
    ed = state["edizione"]

    desc = (
        "Cittadini dell’Impero 👑,\n"
        "è giunto il momento di sfidare la sorte sotto lo stendardo dorato dell’**IMPERIVM**!\n"
        "Da **ora** fino alle **00:00 di giovedì**, la lotteria imperiale è ufficialmente **aperta**! 🧾\n\n"
        "Reagite ✅ **a questo messaggio** per prendere parte all’estrazione.\n"
        "Il destino premierà solo i più audaci!\n\n"
        f"{prize_text()}\n"
        f"**Edizione n°{ed}**"
    )
    emb = make_embed("LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE", desc, COLOR_GOLD)
    msg = await channel.send(embed=emb)
    try:
        await msg.add_reaction("✅")
        await msg.add_reaction("🙂")
    except Exception:
        pass

    state["current_message_id"] = msg.id
    save_state(state)

    # riga vuota per spaziatura
    await channel.send(zws_line())

async def post_chiusura(channel: discord.TextChannel):
    global state
    ed = state["edizione"]
    desc = (
        "La sorte ha parlato… ma il verdetto sarà svelato all’alba. 🌅\n"
        "Tutti i biglietti sono stati raccolti, il fato è in bilico tra le mani degli Dei.\n\n"
        "🗒️ Il vincitore verrà **annunciato ufficialmente alle 08:00 di giovedì**.\n"
        "Restate sintonizzati, cittadini dell’Impero 👑"
    )
    emb = make_embed("LOTTERIA IMPERIVM – CHIUSA!", desc, COLOR_GOLD)
    await channel.send(embed=emb)
    await channel.send(zws_line())

async def post_nessun_partecipante(channel: discord.TextChannel):
    desc = (
        "😕 **Nessun partecipante** questa settimana. Riproviamo mercoledì prossimo! 👑"
    )
    emb = make_embed("Nessun partecipante valido", desc, COLOR_NEUTRAL)
    await channel.send(embed=emb)
    await channel.send("✅ Test completo terminato." if channel.guild is None else zws_line())

def premio_per_vittoria(n: int) -> str:
    if n == 1:
        return "Ricompensa: **100.000 Kama**"
    if n == 2:
        return "Ricompensa: **Scudo di Gilda** *(oppure **250.000 Kama** se già posseduto)*"
    return "Ricompensa: **500.000 Kama** *(e reset dei livelli!)*"

async def post_annuncio_vincitore(channel: discord.TextChannel, winner: discord.Member, level_after: int):
    ed = state["edizione"]
    ricompensa = premio_per_vittoria(level_after)

    desc = (
        "Cittadini dell’Impero 👑,\n"
        "il fato ha deciso, e il nome del vincitore è stato scritto negli annali dell’**Imperium**! ⚖️\n\n"
        f"🏆 **Vincitore dell’edizione:** {winner.mention}\n"
        f"🎖️ **Livello:** **{level_after}**\n"
        f"💰 {ricompensa}\n\n"
        "🗓️ La prossima estrazione avverrà **mercoledì prossimo a mezzanotte**!"
    )

    emb = make_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc, COLOR_GOLD)
    await channel.send(embed=emb)
    await channel.send(zws_line())

# --- LOGICA DI GARA -----------------------------------------------------------
async def chiudi_raccogli_partecipanti(guild: discord.Guild):
    """Legge le reazioni sul messaggio di apertura e salva i partecipanti unici (non bot)."""
    global state
    try:
        channel = await find_channel(guild)
        if not channel:
            return
        msg_id = state.get("current_message_id")
        if not msg_id:
            return
        msg = await channel.fetch_message(msg_id)
        partecipanti = set()
        for r in msg.reactions:
            if str(r.emoji) == "✅":
                async for u in r.users():
                    if not u.bot:
                        partecipanti.add(u.id)
        state["partecipanti_ids"] = list(partecipanti)
        save_state(state)
    except Exception:
        # Non fermare il bot se qualcosa va storto
        pass

async def estrai_e_annuncia(guild: discord.Guild):
    """Estrae il vincitore (se ci sono partecipanti), aggiorna i livelli e annuncia."""
    global state
    channel = await find_channel(guild)
    if not channel:
        return

    ids = state.get("partecipanti_ids", [])
    if not ids:
        await post_nessun_partecipante(channel)
        # prepara prossima edizione
        state["edizione"] = max(1, state["edizione"] + 1)
        state["current_message_id"] = None
        state["partecipanti_ids"] = []
        save_state(state)
        return

    winner_id = random.choice(ids)
    member = guild.get_member(winner_id) or await guild.fetch_member(winner_id)

    # aggiorna livelli (wins)
    wins = state.get("wins", {})
    cur = wins.get(str(winner_id), 0) + 1
    if cur >= 3:
        # raggiunta la terza vittoria → premio 3 e reset livelli
        level_after = 3
        wins[str(winner_id)] = 0
    else:
        level_after = cur
        wins[str(winner_id)] = cur
    state["wins"] = wins
    save_state(state)

    await post_annuncio_vincitore(channel, member, level_after)

    # reset per la prossima edizione
    state["edizione"] = state.get("edizione", 1) + 1
    state["current_message_id"] = None
    state["partecipanti_ids"] = []
    save_state(state)

# --- SCHEDULER (APERTURA/CHIUSURA/ANNUNCIO) -----------------------------------
async def job_apertura(guild: discord.Guild):
    ch = await find_channel(guild)
    if ch:
        await post_apertura(ch)

async def job_chiusura(guild: discord.Guild):
    ch = await find_channel(guild)
    if ch:
        await post_chiusura(ch)
        await chiudi_raccogli_partecipanti(guild)

async def job_annuncio(guild: discord.Guild):
    await estrai_e_annuncio(guild)

def setup_scheduler(guild: discord.Guild):
    # Apertura: ogni Mer alle 00:00
    scheduler.add_job(
        job_apertura,
        CronTrigger(day_of_week="wed", hour=0, minute=0, timezone=TZ),
        args=[guild],
        id="lotto_open",
        next_run_time=START_DATE  # prima esecuzione
    )
    # Chiusura: Gio 00:00
    scheduler.add_job(
        job_chiusura,
        CronTrigger(day_of_week="thu", hour=0, minute=0, timezone=TZ),
        args=[guild],
        id="lotto_close",
        next_run_time=datetime(2025, 10, 23, 0, 0, tzinfo=TZ)
    )
    # Annuncio: Gio 08:00
    scheduler.add_job(
        job_annuncio,
        CronTrigger(day_of_week="thu", hour=8, minute=0, timezone=TZ),
        args=[guild],
        id="lotto_announce",
        next_run_time=datetime(2025, 10, 23, 8, 0, tzinfo=TZ)
    )
    scheduler.start()

# --- COMANDI ------------------------------------------------------------------
def admin_only():
    async def predicate(ctx: commands.Context):
        if not is_admin(ctx.author):
            await ctx.reply("❌ Non sei autorizzato a usare questo comando.", mention_author=False)
            return False
        return True
    return commands.check(predicate)

@bot.event
async def on_ready():
    # Prendi la prima guild dove è presente
    guild = bot.guilds[0] if bot.guilds else None
    if guild:
        setup_scheduler(guild)
        ch = await find_channel(guild)
        if ch:
            print(f"✅ Lotteria IMPERIVM bot online. Canale: #{ch.name}. Edizione corrente: {state.get('edizione', 1)}")
        else:
            print("⚠️ Nessun canale trovato (controlla CHANNEL_ID o il nome del canale).")
    else:
        print("⚠️ Il bot non è in nessuna guild.")

@bot.command()
async def whoami(ctx: commands.Context):
    await ctx.reply(
        f"BOT LOTTERIA IMPERIM 📜\nID: `{ctx.author.id}` – sei admin {'✅' if is_admin(ctx.author) else '❌'}",
        mention_author=False
    )

@bot.command()
@admin_only()
async def testcycle(ctx: commands.Context):
    """Apertura → 30s → chiusura → 10s → annuncio (sequenza pulita con embed eleganti)."""
    guild = ctx.guild
    ch = await find_channel(guild)
    if not ch:
        await ctx.reply("⚠️ Non trovo il canale della lotteria.", mention_author=False)
        return

    note = await ctx.reply("🧪 **Avvio ciclo di test:** apertura → chiusura → vincitore.", mention_author=False)

    # Apertura
    await post_apertura(ch)

    # attesa 30s
    await asyncio.sleep(30)

    # Chiusura + raccolta partecipanti
    await post_chiusura(ch)
    await chiudi_raccogli_partecipanti(guild)

    # attesa 10s
    await asyncio.sleep(10)

    # Annuncio
    await estrai_e_annuncio(guild)

    await ctx.reply("✅ **Test completo terminato.**", mention_author=False)

@bot.command()
@admin_only()
async def mostralivelli(ctx: commands.Context):
    wins = state.get("wins", {})
    if not wins:
        await ctx.reply("📜 **Nessun livello** registrato al momento.", mention_author=False)
        return

    lines = []
    for uid, w in sorted(wins.items(), key=lambda x: int(x[0])):
        member = ctx.guild.get_member(int(uid))
        name = member.mention if member else f"`{uid}`"
        lines.append(f"• {name} → **Livello {w if w>0 else 0}**")
    desc = "\n".join(lines)
    emb = make_embed("REGISTRO LIVELLI", desc or "Nessun dato.", COLOR_NEUTRAL)
    await ctx.reply(embed=emb, mention_author=False)

@bot.command()
@admin_only()
async def resetlivelli(ctx: commands.Context):
    state["wins"] = {}
    save_state(state)
    await ctx.reply("Tutti i livelli sono stati azzerati (wins = 0 per tutti).", mention_author=False)

@bot.command()
@admin_only()
async def resetlotteria(ctx: commands.Context):
    state["edizione"] = 1
    state["current_message_id"] = None
    state["partecipanti_ids"] = []
    save_state(state)
    await ctx.reply("🧹 **Lotteria resettata:** edizione=1, partecipanti azzerati.", mention_author=False)

# --- AVVIO --------------------------------------------------------------------
if __name__ == "__main__":
    start_web_server()  # server HTTP per Render
    token = os.getenv("DISCORD_TOKEN", "")
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN mancante nelle Environment Variables.")
    bot.run(token)    await ctx.reply("🔄 Tutti i **livelli** sono stati azzerati (wins = 0 per tutti).")

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
