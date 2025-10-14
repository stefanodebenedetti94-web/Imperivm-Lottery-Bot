# === IMPERIVM Lottery Bot – main.py (final) ===
# Render / discord.py 2.x / APScheduler – Europe/Rome

import os
import json
import asyncio
import random
from datetime import datetime
import pytz

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------- Config ----------
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True
INTENTS.reactions = True

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Manca DISCORD_TOKEN nelle Environment Variables di Render.")

# Puoi lasciare LOTTERY_CHANNEL_ID = 0 per far cercare il canale per nome
LOTTERY_CHANNEL_ID = int(os.getenv("LOTTERY_CHANNEL_ID", "0"))
LOTTERY_CHANNEL_NAME_CANDIDATES = ["lotteria-imperiale", "lotteria-imperivm"]

# Admin extra (facoltativo). Se vuoto valgono i permessi amministratore del server.
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
    "wins": {},             # {user_id(str): vittorie accumulate (1..3)}
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
    return getattr(m, "guild_permissions", None) and m.guild_permissions.administrator

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
    # Titolo con “cornice” testuale in stile pergamena
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
        "E' giunto il momento di sfidare la sorte sotto lo stendardo dorato dell'IMPERIVM!",
        "Da ora fino alle 00:00 di giovedi, la lotteria imperiale e' ufficialmente aperta! 🧾",
        "",
        "Reagite con ✅ a questo messaggio per partecipare all'estrazione.",
        "Il destino premierà solo i più audaci!",
        "",
        "⚔ Premi in palio:",
        "  1️⃣ 1a vittoria → 100.000 Kama",
        "  2️⃣ 2a vittoria → Scudo di Gilda (se gia' posseduto → 250.000 Kama)",
        "  3️⃣ 3a vittoria → 500.000 Kama (reset dei livelli)",
        "",
        f"Edizione n°{edition}",
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
            "La sorte ha parlato… 😕  Nessun partecipante valido questa settimana.\n"
            "Torniamo mercoledi prossimo! 👑"
        )
    else:
        desc = (
            "La sorte ha parlato… 🌅  Il verdetto sara' svelato all'alba.\n"
            "Tutti i biglietti sono stati raccolti, il fato e' in bilico tra le mani degli Dei."
        )
    await channel.send(embed=golden_embed("LOTTERIA IMPERIVM – CHIUSA", desc))

async def post_winner_announcement(channel: discord.TextChannel, member: discord.Member | None):
    if member is None:
        desc = "Nessun partecipante questa settimana. Riproviamo mercoledi prossimo! 🙂"
        await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))
        return

    uid = str(member.id)
    wins = STATE["wins"].get(uid, 0)
    lvl = level_from_wins(wins)
    if lvl == 1:
        premio = "100.000 Kama"
    elif lvl == 2:
        premio = "Scudo di Gilda (se gia' posseduto → 250.000 Kama)"
    else:
        premio = "500.000 Kama (reset dei livelli)"

    desc = (
        f"Vincitore: {member.mention} 🎉\n"
        f"Livello attuale: {lvl}\n"
        f"Ricompensa: {premio}\n"
        "La prossima estrazione avverra' mercoledi a mezzanotte. 🕛"
    )
    await channel.send(embed=golden_embed("ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM", desc))

async def collect_participants(msg: discord.Message) -> list[int]:
    ids = []
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

    participants = []
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
            w = 1  # reset dopo il 3
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
    # preparo l'etichetta “edizione n°X” per la prossima settimana
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
       Se adesso è prima del 22/10/2025 00:00, la primissima apertura usa exactly START_DATE."""
    trig_open     = CronTrigger(day_of_week="wed", hour=0,  minute=0, timezone=TZ)
    trig_close    = CronTrigger(day_of_week="thu", hour=0,  minute=0, timezone=TZ)
    trig_announce = CronTrigger(day_of_week="thu", hour=8,  minute=0, timezone=TZ)

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
    print(f"[OK] Bot online come {bot.user} – edizione corrente: {STATE['edition']}")
    if not scheduler.running:
        schedule_weekly_jobs()
        scheduler.start()

# ---------- Comandi ----------
@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    adm = "si" if is_admin(ctx) else "no"
    await ctx.reply(f"ID: {ctx.author.id} — sei admin: {adm}", mention_author=False)

@bot.command(name="mostralivelli")
async def mostralivelli(ctx: commands.Context):
    wins = STATE.get("wins", {})
    if not wins:
        await ctx.reply("Nessun livello registrato al momento. 🧾", mention_author=False)
        return
    lines = []
    for uid, w in wins.items():
        member = ctx.guild.get_member(int(uid))
        tag = member.mention if member else f"<@{uid}>"
        lines.append(f"{tag}: vittorie={w}, livello={level_from_wins(w)}")
    embed = golden_embed("LIVELLI E VITTORIE", "\n".join(lines))
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="resetlivelli")
async def resetlivelli(ctx: commands.Context):
    if not is_admin(ctx):
        return
    STATE["wins"] = {}
    save_state(STATE)
    await ctx.reply("Tutti i livelli sono stati azzerati (wins = 0 per tutti). 🔄", mention_author=False)

@bot.command(name="resetlotteria")
async def resetlotteria(ctx: commands.Context):
    if not is_admin(ctx):
        return
    STATE["edition"] = 1
    STATE["open_message_id"] = None
    STATE["participants"] = []
    save_state(STATE)
    await ctx.reply("Lotteria resettata: edizione=1, partecipanti azzerati. ♻️", mention_author=False)

@bot.command(name="testcycle")
async def testcycle(ctx: commands.Context):
    """Apertura -> 5s -> Chiusura -> 5s -> Annuncio (con embed)."""
    if not is_admin(ctx):
        return
    guild = ctx.guild
    channel = await find_lottery_channel(guild)
    if not channel:
        await ctx.reply("Canale lotteria non trovato.", mention_author=False)
        return

    await ctx.reply("Avvio ciclo di test: apertura → chiusura → vincitore. 🧪", mention_author=False)

    # Apertura
    await post_open_message(channel)
    await asyncio.sleep(5)

    # Chiusura e pick
    winner = await close_and_pick(guild, announce_now=False)
    await asyncio.sleep(5)

    # Annuncio
    await post_winner_announcement(channel, winner)
    await ctx.reply("Test completo terminato. ✅", mention_author=False)

# ---------- Run ----------
if __name__ == "__main__":
    bot.run(TOKEN)_jobs()
        scheduler.start()

# ---------- Comandi ----------
@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    adm = "si" if is_admin(ctx) else "no"
    await ctx.reply(f"ID: {ctx.author.id} — sei admin: {adm}", mention_author=False)

@bot.command(name="mostralivelli")
async def mostralivelli(ctx: commands.Context):
    wins = STATE.get("wins", {})
    if not wins:
        await ctx.reply("Nessun livello registrato al momento. 🧾", mention_author=False)
        return
    lines = []
    for uid, w in wins.items():
        member = ctx.guild.get_member(int(uid))
        tag = member.mention if member else f"<@{uid}>"
        lines.append(f"{tag}: vittorie={w}, livello={level_from_wins(w)}")
    embed = golden_embed("LIVELLI E VITTORIE", "\n".join(lines))
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="resetlivelli")
async def resetlivelli(ctx: commands.Context):
    if not is_admin(ctx):
        return
    STATE["wins"] = {}
    save_state(STATE)
    await ctx.reply("Tutti i livelli sono stati azzerati (wins = 0 per tutti). 🔄", mention_author=False)

@bot.command(name="resetlotteria")
async def resetlotteria(ctx: commands.Context):
    if not is_admin(ctx):
        return
    STATE["edition"] = 1
    STATE["open_message_id"] = None
    STATE["participants"] = []
    save_state(STATE)
    await ctx.reply("Lotteria resettata: edizione=1, partecipanti azzerati. ♻️", mention_author=False)

@bot.command(name="testcycle")
async def testcycle(ctx: commands.Context):
    """Apertura -> 5s -> Chiusura -> 5s -> Annuncio (con embed)."""
    if not is_admin(ctx):
        return
    guild = ctx.guild
    channel = await find_lottery_channel(guild)
    if not channel:
        await ctx.reply("Canale lotteria non trovato.", mention_author=False)
        return

    await ctx.reply("Avvio ciclo di test: apertura → chiusura → vincitore. 🧪", mention_author=False)

    # Apertura
    await post_open_message(channel)
    await asyncio.sleep(5)

    # Chiusura e pick
    winner = await close_and_pick(guild, announce_now=False)
    await asyncio.sleep(5)

    # Annuncio
    await post_winner_announcement(channel, winner)
    await ctx.reply("Test completo terminato. ✅", mention_author=False)

# ---------- Run ----------
if __name__ == "__main__":
    bot.run(TOKEN)ler.add_job(
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
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass
    print(f"✅ {bot.user} online. Edizione corrente: {lottery_data['edizione']}")

# ---- Avvio ----
if __name__ == "__main__":
    # Avvia il web server per Render
    start_web_server()

    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN mancante")

    # ⛔ IMPORTANTISSIMO: niente altro dopo questa riga
    bot.run(TOKEN)
