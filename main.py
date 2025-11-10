# === IMPERIVM Lottery Bot — main.py (Render / discord.py 2.x) ===
# Stato persistito su messaggio pinnato (STATE_MESSAGE_ID) per sopravvivere ai reboot.
# Auto-schedule: Wed 00:00 apertura, Thu 00:00 chiusura, Thu 08:00 annuncio.
# Edizione speciale: ogni 4 edizioni (4, 8, 12, …) con premi 600k/800k/1M (+200k se il vincitore ha già vinto in passato).
# Niente leaderboard. Niente reazione automatica del bot sul messaggio di apertura.

import os, json, asyncio, random
from datetime import datetime, time
from threading import Thread
from typing import Optional, List, Tuple

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

@app.get("/health")
def health():
    return "OK"

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
STATE_MESSAGE_ID = int(os.getenv("STATE_MESSAGE_ID", "0"))
APP_URL = os.getenv("APP_URL", "").strip()

TZ = pytz.timezone("Europe/Rome")
GOLD = discord.Color.from_str("#DAA520")

# Seed richiesto (edizione 4 con tre vincitori livello 1)
SEED_WINS = {
    "277179262534942720": 1,  # Aeries
    "662291114752999437": 1,  # Sandor
    "4601937456406200042": 1  # (attenzione: ID corretto sotto, vedi DEFAULT_STATE)
}

SCHEMA = "imperivm.lottery.v1"

DEFAULT_STATE = {
    "schema": SCHEMA,
    "edition": 4,                   # si parte già dall'edizione 4
    "open_message_id": None,
    "wins": {
        "277179262534942720": 1,    # Aeries
        "662291114752999437": 1,    # Sandor
        "460193745640620042": 1     # Klaudrr (ID corretto)
    },
    "victories": {
        "277179262534942720": 1,
        "662291114752999437": 1,
        "460193745640620042": 1
    },
    "cycles": {},                   # contatori dei reset (raggiunto L3)
    "last_win_iso": {},             # ultima vittoria per utente
    "last_winner_id": None,

    # marcatori anti-duplicato (per cron/watchdog)
    "last_open_week": None,         # "YYYY-WW"
    "last_close_week": None,
    "last_announce_week": None,
}

def now_tz() -> datetime:
    return datetime.now(TZ)

def week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-{iso[1]:02d}"

def level_from_wins(w: int) -> int:
    if w <= 0: return 0
    return ((w - 1) % 3) + 1  # livelli 1..3, poi torna a 1

def golden_embed(title: str, desc: str) -> discord.Embed:
    e = discord.Embed(title=f"📜  {title}  📜", description=desc, color=GOLD)
    e.set_footer(text="IMPERIVM • Lotteria settimanale")
    return e

# ---------- Stato su messaggio pinnato ----------
STATE: dict = DEFAULT_STATE.copy()
bot = commands.Bot(command_prefix="!", intents=INTENTS)
scheduler = AsyncIOScheduler(timezone=TZ)

async def _fetch_state_message(guild: discord.Guild) -> Optional[discord.Message]:
    if not STATE_MESSAGE_ID:
        return None
    channel = guild.get_channel(LOTTERY_CHANNEL_ID) if LOTTERY_CHANNEL_ID else None
    if not isinstance(channel, discord.TextChannel):
        return None
    try:
        msg = await channel.fetch_message(STATE_MESSAGE_ID)
        return msg
    except Exception:
        return None

async def load_state_from_pinned(guild: discord.Guild):
    global STATE
    msg = await _fetch_state_message(guild)
    if not msg:
        return
    try:
        raw = msg.content.strip()
        # consenti formato con blocco code
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw.strip("`").split("\n", 1)[1]
        data = json.loads(raw)
        if data.get("schema") == SCHEMA:
            # merge con default per eventuali nuove chiavi
            merged = DEFAULT_STATE.copy()
            merged.update(data)
            STATE = merged
    except Exception:
        # se fallisce, mantieni STATE corrente (seed) e sovrascrivi al prossimo save
        pass

async def save_state_to_pinned(guild: discord.Guild):
    msg = await _fetch_state_message(guild)
    if not msg:
        return
    try:
        payload = json.dumps(STATE, ensure_ascii=False, indent=2)
        content = f"```json\n{payload}\n```"
        await msg.edit(content=content)
    except Exception:
        pass

# ---------- Flusso Lotteria ----------
async def find_lottery_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = guild.get_channel(LOTTERY_CHANNEL_ID) if LOTTERY_CHANNEL_ID else None
    return ch if isinstance(ch, discord.TextChannel) else None

async def post_open_message(channel: discord.TextChannel):
    global STATE
    edition = STATE["edition"]

    special = (edition % 4 == 0)
    if special:
        title = "LOTTERIA IMPERIVM — EDIZIONE SPECIALE"
        lines = [
            "Cittadini dell'Impero 👑",
            "Questa è un’**Edizione Speciale**: i premi sono maggiorati e il fato è capriccioso!",
            "Da ora fino alle 00:00 di giovedì, la lotteria è **aperta**.",
            "",
            "Reagite con ✅ a questo messaggio per partecipare.",
            "",
            "🎁 Premi casuali: **600.000** / **800.000** / **1.000.000** Kama",
            "💠 *Bonus*: se hai **già vinto** in passato → **+200.000** Kama sul premio estratto.",
            "",
            f"**Edizione n°{edition} (Special)**",
            "📣 Il verdetto sarà annunciato **giovedì alle 08:00**."
        ]
    else:
        title = "LOTTERIA IMPERIVM — EDIZIONE SETTIMANALE"
        lines = [
            "Cittadini dell'Impero 👑",
            "È il momento di sfidare la sorte sotto lo stendardo bianco e rosso dell’IMPERIVM!",
            "Da ora fino alle 00:00 di giovedì, la lotteria è **aperta**.",
            "",
            "Reagite con ✅ a questo messaggio per partecipare.",
            "",
            "⚔️ Premi in palio:",
            "• 1ª vittoria → **100.000** Kama",
            "• 2ª vittoria → **Scudo di Gilda** *(se già posseduto → **250.000** Kama)*",
            "• 3ª vittoria → **500.000** Kama *(reset dei livelli)*",
            "",
            f"**Edizione n°{edition}**",
            "📣 Il verdetto sarà annunciato **giovedì alle 08:00**."
        ]

    embed = golden_embed(title, "\n".join(lines))
    msg = await channel.send(embed=embed)
    # << Non aggiungiamo alcuna reazione automatica >>
    STATE["open_message_id"] = msg.id
    await save_state_to_pinned(channel.guild)
    return msg

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
    # dedup
    return list(dict.fromkeys(ids))

def _bump_win_counters(uid: str):
    prev = int(STATE["wins"].get(uid, 0))
    new = prev + 1
    reset = False
    if new > 3:
        new = 1
        reset = True
    STATE["wins"][uid] = new
    STATE["victories"][uid] = int(STATE["victories"].get(uid, 0)) + 1
    if reset:
        STATE["cycles"][uid] = int(STATE["cycles"].get(uid, 0)) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

def _special_prize_for(uid: str) -> int:
    base = random.choice([600_000, 800_000, 1_000_000])
    if int(STATE["victories"].get(uid, 0)) >= 1:
        base += 200_000
    return base

async def post_close_message(channel: discord.TextChannel, no_participants: bool, names_preview: Optional[str]):
    if no_participants:
        desc = "😕 Nessun partecipante valido questa settimana. Torniamo mercoledì prossimo!"
    else:
        desc = "I biglietti sono stati raccolti: il fato deciderà all’alba.\n"
        if names_preview:
            desc += names_preview + "\n"
        desc += "🕗 Annuncio del vincitore **alle 08:00 di giovedì**."
    await channel.send(embed=golden_embed("LOTTERIA IMPERIVM — CHIUSA", desc))

async def post_winner_announcement(channel: discord.TextChannel, member: Optional[discord.Member], special: bool):
    if member is None:
        await channel.send(embed=golden_embed(
            "ESTRAZIONE UFFICIALE — LOTTERIA IMPERIVM",
            "I sigilli sono stati spezzati ma il fato è rimasto muto. Riproveremo mercoledì prossimo."))
        return

    uid = str(member.id)
    lvl = level_from_wins(int(STATE["wins"].get(uid, 0)))

    if special:
        prize = _special_prize_for(uid)
        body = (
            f"👑 **Vincitore (Special):** {member.mention}\n"
            f"🎁 **Ricompensa:** {prize:,} Kama\n"
            f"⚔️ **Livello attuale:** {lvl}/3"
        )
        title = "ESTRAZIONE UFFICIALE — EDIZIONE SPECIALE"
    else:
        if lvl == 1:
            premio = "100.000 Kama"
        elif lvl == 2:
            premio = "Scudo di Gilda *(se già posseduto → 250.000 Kama)*"
        else:
            premio = "500.000 Kama *(reset dei livelli)*"
        body = (
            f"👑 **Vincitore:** {member.mention}\n"
            f"⚔️ **Livello attuale:** {lvl}/3\n"
            f"📜 **Ricompensa:** {premio}"
        )
        title = "ESTRAZIONE UFFICIALE — LOTTERIA IMPERIVM"

    tail = "\n\nLa prossima chiamata dell’Aquila Imperiale risuonerà **mercoledì a mezzanotte**."
    await channel.send(embed=golden_embed(title, body + tail))

async def close_and_pick(guild: discord.Guild, announce_now: bool = False):
    global STATE
    channel = await find_lottery_channel(guild)
    if not channel:
        return None

    msg = None
    if STATE.get("open_message_id"):
        try:
            msg = await channel.fetch_message(STATE["open_message_id"])
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
        body = "• " + "\n• ".join(names)
        if more > 0:
            body += f"\n…e altri **{more}**."
        names_preview = f"{header}\n{body}"

    await post_close_message(channel, len(participants) == 0, names_preview)

    winner_member = None
    STATE["last_winner_id"] = None

    if participants:
        win_id = random.choice(participants)
        STATE["last_winner_id"] = win_id
        _bump_win_counters(str(win_id))
        await save_state_to_pinned(guild)
        try:
            winner_member = await guild.fetch_member(win_id)
        except Exception:
            winner_member = guild.get_member(win_id)

    # chiudo edizione
    STATE["open_message_id"] = None
    await save_state_to_pinned(guild)

    if announce_now:
        special = (STATE["edition"] % 4 == 0)
        await post_winner_announcement(channel, winner_member, special)

    return winner_member

async def open_lottery(guild: discord.Guild):
    global STATE
    channel = await find_lottery_channel(guild)
    if not channel:
        return

    # evita doppia apertura se già c'è il messaggio "vivo"
    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            return
        except Exception:
            STATE["open_message_id"] = None

    await post_open_message(channel)
    # incremento edizione SUBITO dopo aver aperto (così il contatore va alla prossima)
    STATE["edition"] += 1
    await save_state_to_pinned(guild)

# ---------- Schedulazione ----------
def schedule_weekly_jobs():
    trig_open     = CronTrigger(day_of_week="wed", hour=0, minute=0, timezone=TZ)
    trig_close    = CronTrigger(day_of_week="thu", hour=0, minute=0, timezone=TZ)
    trig_announce = CronTrigger(day_of_week="thu", hour=8, minute=0, timezone=TZ)

    async def do_open():
        for g in bot.guilds:
            await load_state_from_pinned(g)  # sempre ricaricare prima
            await open_lottery(g)
            STATE["last_open_week"] = week_key(now_tz())
            await save_state_to_pinned(g)

    async def do_close():
        for g in bot.guilds:
            await load_state_from_pinned(g)
            await close_and_pick(g, announce_now=False)
            STATE["last_close_week"] = week_key(now_tz())
            await save_state_to_pinned(g)

    async def do_announce():
        for g in bot.guilds:
            await load_state_from_pinned(g)
            ch = await find_lottery_channel(g)
            if not ch:
                continue
            lw = STATE.get("last_winner_id")
            member = None
            if lw:
                try: member = await g.fetch_member(lw)
                except Exception: member = g.get_member(lw)
            special = ((STATE["edition"] - 1) % 4 == 0)  # l'edizione annunciata è quella appena conclusa
            await post_winner_announcement(ch, member, special)
            STATE["last_announce_week"] = week_key(now_tz())
            STATE["last_winner_id"] = None
            await save_state_to_pinned(g)

    scheduler.add_job(lambda: asyncio.create_task(do_open()), trig_open)
    scheduler.add_job(lambda: asyncio.create_task(do_close()), trig_close)
    scheduler.add_job(lambda: asyncio.create_task(do_announce()), trig_announce)

    # Watchdog ogni 5 minuti per recuperare task mancati
    async def watchdog():
        dt = now_tz()
        wk = week_key(dt)
        wd = dt.weekday()   # Mon=0 ... Sun=6
        t  = dt.time()

        if wd == 2 and t >= time(0,0) and STATE.get("last_open_week") != wk:
            for g in bot.guilds:
                await load_state_from_pinned(g)
                await open_lottery(g)
                STATE["last_open_week"] = wk
                await save_state_to_pinned(g)
            return

        if wd == 3 and t >= time(0,0) and STATE.get("last_close_week") != wk:
            for g in bot.guilds:
                await load_state_from_pinned(g)
                await close_and_pick(g, announce_now=False)
                STATE["last_close_week"] = wk
                await save_state_to_pinned(g)
            return

        if wd == 3 and t >= time(8,0) and STATE.get("last_announce_week") != wk:
            for g in bot.guilds:
                await load_state_from_pinned(g)
                ch = await find_lottery_channel(g)
                if not ch: continue
                lw = STATE.get("last_winner_id")
                member = None
                if lw:
                    try: member = await g.fetch_member(lw)
                    except Exception: member = g.get_member(lw)
                special = ((STATE["edition"] - 1) % 4 == 0)
                await post_winner_announcement(ch, member, special)
                STATE["last_announce_week"] = wk
                STATE["last_winner_id"] = None
                await save_state_to_pinned(g)

    scheduler.add_job(lambda: asyncio.create_task(watchdog()),
                      "interval", minutes=5, timezone=TZ)

# ---------- Eventi ----------
@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass
    # all'avvio, carica stato dal messaggio pinnato del primo server
    for g in bot.guilds:
        await load_state_from_pinned(g)
        break
    if not scheduler.running:
        schedule_weekly_jobs()
        scheduler.start()
    print(f"✅ Bot online come {bot.user} — edizione corrente: {STATE['edition']}")

# ---------- Comandi testo (debug/utility) ----------
@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    await ctx.reply(f"ID: {ctx.author.id}", mention_author=False)

@bot.command(name="mostralivelli")
async def mostralivelli(ctx: commands.Context):
    wins = STATE.get("wins", {})
    if not wins:
        await ctx.reply("📜 Nessun livello registrato al momento.", mention_author=False); return
    lines = []
    for uid, w in wins.items():
        member = ctx.guild.get_member(int(uid))
        tag = member.mention if member else f"<@{uid}>"
        lines.append(f"{tag}: livello attuale = {level_from_wins(int(w))}/3 • vittorie totali = {STATE['victories'].get(uid,0)}")
    await ctx.reply(embed=golden_embed("REGISTRO LIVELLI (corrente)", "\n".join(lines)), mention_author=False)

# ---------- Slash (admin) ----------
def _admin(inter: discord.Interaction) -> bool:
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

@bot.tree.command(name="setwin", description="Registra manualmente una vittoria per un utente.")
@app_commands.describe(utente="Seleziona l'utente vincitore da registrare")
async def slash_setwin(inter: discord.Interaction, utente: discord.Member):
    if not _admin(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    _bump_win_counters(str(utente.id))
    await save_state_to_pinned(inter.guild)
    lvl = level_from_wins(int(STATE["wins"].get(str(utente.id), 0)))
    await inter.followup.send(f"✅ Registrata vittoria per **{utente.display_name}** — livello attuale {lvl}/3")

@bot.tree.command(name="setedition", description="Imposta manualmente il numero di edizione.")
@app_commands.describe(numero="Numero di edizione (es. 4)")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if not _admin(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    STATE["edition"] = int(numero)
    await save_state_to_pinned(inter.guild)
    await inter.followup.send(f"📌 Edizione impostata a **{numero}**.")

@bot.tree.command(name="apertura", description="Forza l'apertura della lotteria (classica/speciale in base all'edizione corrente).")
async def slash_apertura(inter: discord.Interaction):
    if not _admin(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    await load_state_from_pinned(inter.guild)
    await open_lottery(inter.guild)
    STATE["last_open_week"] = week_key(now_tz())
    await save_state_to_pinned(inter.guild)
    await inter.followup.send("📜 Apertura forzata eseguita.")

@bot.tree.command(name="chiusura", description="Forza la chiusura e l’estrazione.")
async def slash_chiusura(inter: discord.Interaction):
    if not _admin(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    await load_state_from_pinned(inter.guild)
    await close_and_pick(inter.guild, announce_now=False)
    STATE["last_close_week"] = week_key(now_tz())
    await save_state_to_pinned(inter.guild)
    await inter.followup.send("🗝️ Chiusura forzata eseguita.")

@bot.tree.command(name="annuncio", description="Forza l’annuncio del vincitore.")
async def slash_annuncio(inter: discord.Interaction):
    if not _admin(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    await load_state_from_pinned(inter.guild)
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
    lw = STATE.get("last_winner_id")
    member = None
    if lw:
        try: member = await inter.guild.fetch_member(lw)
        except Exception: member = inter.guild.get_member(lw)
    # l’edizione “annunciata” è quella appena conclusa (edition-1)
    special = ((STATE["edition"] - 1) % 4 == 0)
    await post_winner_announcement(ch, member, special)
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    await save_state_to_pinned(inter.guild)
    await inter.followup.send("📣 Annuncio forzato eseguito.")

# Alias “speciali” espliciti (stesse azioni ma messaggi seguiranno l’edizione corrente)
@bot.tree.command(name="apertura_speciale", description="Forza apertura come speciale (usa l’edizione corrente).")
async def slash_apertura_speciale(inter: discord.Interaction):
    await slash_apertura(inter)

@bot.tree.command(name="chiusura_speciale", description="Forza chiusura come speciale.")
async def slash_chiusura_speciale(inter: discord.Interaction):
    await slash_chiusura(inter)

@bot.tree.command(name="annuncio_speciale", description="Forza annuncio come speciale.")
async def slash_annuncio_speciale(inter: discord.Interaction):
    await slash_annuncio(inter)

# ---------- Avvio ----------
@bot.event
async def setup_hook():
    try:
        await bot.tree.sync()
    except Exception:
        pass

if __name__ == "__main__":
    start_web_server()
    bot.run(TOKEN)
