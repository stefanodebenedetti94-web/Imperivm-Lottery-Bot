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
    "participants": [],              # non usato ai fini estrazione (leggiamo da reazioni)
    "wins": {},                      # {uid: 1..3 (livello corr.), reset a 1 dopo 3}
    "last_winner_id": None,          # salvato alla chiusura; annunciato alle 08:00

    # Marcatori settimanali anti-duplicato (per cron/watchdog)
    "last_open_week": None,          # "YYYY-WW"
    "last_close_week": None,
    "last_announce_week": None,

    # Dati leaderboard
    "victories": {},                 # {uid: tot vittorie storiche}
    "last_win_iso": {},              # {uid: ISO timestamp ultima vittoria}
    "cycles": {},                    # {uid: volte raggiunto L3 (reset)}
    "leaderboard_message_id": None   # ID messaggio leaderboard pinnato
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
    # backup semplice
    try:
        if os.path.exists(STATE_FILE):
            os.replace(STATE_FILE, STATE_FILE + ".bak")
    except Exception:
        pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

STATE = load_state()

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
    if wins <= 0: return 0
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
        "🕗 *Nota:* il verdetto sarà annunciato **giovedì alle 08:00**."
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

async def post_close_message(channel: discord.TextChannel, no_participants: bool, names_preview: Optional[str]):
    """Messaggio di chiusura con eventuale elenco partecipanti (solo nomi, nessuna mention)."""
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
    # Testo più "imperiale"
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
        "risuonerà **mercoledì a mezzanotte**. Presentatevi senza timore."
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
    # dedup
    return list(dict.fromkeys(ids))

def _bump_win_counters(uid: str):
    """Aggiorna contatori per vincitore: wins (livello), victories, cycles, last_win_iso."""
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
    """Giovedì 00:00 – chiude la raccolta, calcola vincitore e (opz.) annuncia subito."""
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

    # Elenco nomi senza mention (max 50)
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
        uid = str(win_id)
        _bump_win_counters(uid)
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
    """Mercoledì 00:00 – apre la lotteria e incrementa edizione."""
    global STATE
    channel = await find_lottery_channel(guild)
    if not channel:
        return
    # Evita doppie aperture se esiste già un messaggio di apertura “vivo”
    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            # se non lancia eccezione, c'è già un'apertura valida
            return
        except Exception:
            pass
    await post_open_message(channel)
    STATE["edition"] += 1
    save_state(STATE)

# ---------- Leaderboard ----------
def build_leaderboard_lines(guild: discord.Guild, top_n: int = 10):
    victories = STATE.get("victories", {})
    if not victories:
        return ["Nessun vincitore registrato al momento."]
    items = []
    for uid_str, tot in victories.items():
        try:
            uid = int(uid_str)
        except:
            continue
        member = guild.get_member(uid)
        name = member.display_name if member else f"utente {uid}"
        lvl = level_from_wins(STATE["wins"].get(uid_str, 0))
        cycles = STATE["cycles"].get(uid_str, 0)
        last_iso = STATE["last_win_iso"].get(uid_str)
        last_dt = None
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso)
            except:
                last_dt = None
        items.append((tot, last_dt or datetime.min, name, uid_str, lvl, cycles))
    # ordina: vittorie desc, poi last_win desc, poi nome asc
    items.sort(key=lambda x: (-x[0], -(x[1].timestamp()), x[2].lower()))
    items = items[:top_n]
    lines = []
    for i, (tot, last_dt, name, uid_str, lvl, cycles) in enumerate(items, start=1):
        last_str = last_dt.strftime("%d/%m/%Y") if last_dt and last_dt != datetime.min else "-"
        stato = f"{lvl}/3" if lvl else "0/3"
        lines.append(f"{i}) {name} — Livello: {lvl} • Vittorie: {tot} • Ultima vittoria: {last_str} • Cicli: {cycles} • Stato attuale: {stato}")
    return lines

async def post_or_update_leaderboard(guild: discord.Guild, channel: discord.TextChannel, pin: bool = True):
    lines = build_leaderboard_lines(guild, top_n=10)
    edition = STATE.get("edition", 1)
    updated = now_tz().strftime("%d/%m/%Y — %H:%M")

    desc = "🏆 **Classifica (storico)**\n" + "\n".join(lines) + \
           f"\n\n📊 **Statistiche**\n• Edizione corrente: n°{edition}\n• Aggiornamento: {updated}"

    emb = golden_embed("LEADERBOARD — Top Vincitori", desc)

    msg_id = STATE.get("leaderboard_message_id")
    msg = None
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
        except Exception:
            msg = None

    if msg:
        await msg.edit(embed=emb)
    else:
        msg = await channel.send(embed=emb)
        STATE["leaderboard_message_id"] = msg.id
        save_state(STATE)
        if pin:
            try:
                await msg.pin(reason="Leaderboard IMPERIVM")
            except Exception:
                pass
    return msg

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
            if not ch: continue
            lw = STATE.get("last_winner_id")
            member = None
            if lw:
                try:
                    member = await g.fetch_member(lw)
                except Exception:
                    member = g.get_member(lw)
            await post_winner_announcement(ch, member)
            # leaderboard auto
            try:
                await post_or_update_leaderboard(g, ch, pin=True)
            except Exception:
                pass
            STATE["last_announce_week"] = week_key(now_tz())
            STATE["last_winner_id"] = None
            save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(do_open()), trig_open)
    scheduler.add_job(lambda: asyncio.create_task(do_close()), trig_close)
    scheduler.add_job(lambda: asyncio.create_task(do_announce()), trig_announce)

    # WATCHDOG ogni 5 minuti per recupero eventi mancati
    async def watchdog():
        dt = now_tz()
        wk = week_key(dt)
        wd = dt.weekday()  # Mon=0 ... Sun=6
        t  = dt.time()

        if wd == 2 and t >= time(0,0) and STATE.get("last_open_week") != wk:
            for g in bot.guilds:
                await open_lottery(g)
            STATE["last_open_week"] = wk; save_state(STATE); return

        if wd == 3 and t >= time(0,0) and STATE.get("last_close_week") != wk:
            for g in bot.guilds:
                await close_and_pick(g, announce_now=False)
            STATE["last_close_week"] = wk; save_state(STATE); return

        if wd == 3 and t >= time(8,0) and STATE.get("last_announce_week") != wk:
            for g in bot.guilds:
                ch = await find_lottery_channel(g)
                if not ch: continue
                lw = STATE.get("last_winner_id")
                member = None
                if lw:
                    try:
                        member = await g.fetch_member(lw)
                    except Exception:
                        member = g.get_member(lw)
                await post_winner_announcement(ch, member)
                try:
                    await post_or_update_leaderboard(g, ch, pin=True)
                except Exception:
                    pass
            STATE["last_announce_week"] = wk
            STATE["last_winner_id"] = None
            save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(watchdog()),
                      "interval", minutes=5, timezone=TZ)

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

# ---------- Comandi testuali (già presenti) ----------
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
        tag = member.mention if member else f"<@{uid}>"
        lines.append(f"{tag}: vittorie (livello) = {w} → Livello attuale: {level_from_wins(w)}")
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
    STATE["participants"] = []
    save_state(STATE)
    await ctx.reply("🧹 Lotteria resettata: edizione=1, partecipanti azzerati.", mention_author=False)

@bot.command(name="testcycle")
async def testcycle(ctx: commands.Context):
    """Simula un ciclo completo con messaggi reali (apertura → 20s → chiusura → 20s → annuncio → leaderboard)."""
    if not is_admin(ctx): return
    guild = ctx.guild
    channel = await find_lottery_channel(guild)
    if not channel:
        await ctx.reply("⚠️ Canale lotteria non trovato.", mention_author=False); return

    await ctx.reply(
        "🧪 **Avvio ciclo di test:** Apertura → (20s) → Chiusura → (20s) → Annuncio → Leaderboard.",
        mention_author=False
    )
    await post_open_message(channel)
    await asyncio.sleep(20)
    await close_and_pick(guild, announce_now=False)
    await asyncio.sleep(20)

    winner_id = STATE.get("last_winner_id")
    winner = None
    if winner_id:
        try:
            winner = await guild.fetch_member(winner_id)
        except Exception:
            winner = ctx.guild.get_member(winner_id)

    await post_winner_announcement(channel, winner)
    try:
        await post_or_update_leaderboard(guild, channel, pin=True)
    except Exception:
        pass
    await asyncio.sleep(2)
    await ctx.reply("✅ **Test completo terminato.**", mention_author=False)

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
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
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
    try:
        await post_or_update_leaderboard(inter.guild, ch, pin=True)
    except Exception:
        pass
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    save_state(STATE)
    await inter.followup.send("📣 Annuncio forzato eseguito.")

# --- NUOVO: registra manualmente una vittoria e aggiorna la leaderboard ---
@bot.tree.command(name="setwin", description="Registra manualmente una vittoria per un utente e aggiorna i dati (solo admin).")
@app_commands.describe(utente="Seleziona l'utente vincitore da registrare")
async def slash_setwin(inter: discord.Interaction, utente: discord.Member):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    uid = str(utente.id)
    _bump_win_counters(uid)
    save_state(STATE)

    ch = await find_lottery_channel(inter.guild)
    if ch:
        try:
            await post_or_update_leaderboard(inter.guild, ch, pin=True)
        except Exception:
            pass

    lvl = level_from_wins(STATE["wins"].get(uid, 0))
    tot = STATE["victories"].get(uid, 0)
    cyc = STATE["cycles"].get(uid, 0)
    await inter.followup.send(f"✅ Registrata vittoria per **{utente.display_name}** — Livello attuale: {lvl} • Vittorie totali: {tot} • Cicli: {cyc}")

# --- NUOVO: aggiorna/mostra la leaderboard (subito) ---
@bot.tree.command(name="leaderboard", description="Genera o aggiorna la leaderboard (opz. pin).")
@app_commands.describe(pin="Se attivo, pinna il messaggio della leaderboard")
async def slash_leaderboard(inter: discord.Interaction, pin: bool = True):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
    msg = await post_or_update_leaderboard(inter.guild, ch, pin=pin)
    await inter.followup.send(f"🏛️ Leaderboard aggiornata. (ID: {msg.id})")

# ---------- Avvio ----------
@bot.event
async def setup_hook():
    # sync comandi slash
    try:
        await bot.tree.sync()
    except Exception:
        pass

if __name__ == "__main__":
    start_web_server()  # server HTTP per Render
    bot.run(TOKEN)
