# === IMPERIVM Lottery Bot – main.py (Render / discord.py 2.x) ===
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

    # livelli correnti (1..3) – reset a 1 dopo la 3°
    "wins": {},                       # {uid: 1..3}
    # storico per bonus Special
    "victories": {},                  # {uid: tot vittorie}
    "cycles": {},                     # {uid: quante volte ha superato L3 (reset)}
    "last_win_iso": {},               # {uid: ISO timestamp ultima vittoria}

    "last_winner_id": None,

    # Anti-duplicato cron/watchdog
    "last_open_week": None,           # "YYYY-WW"
    "last_close_week": None,
    "last_announce_week": None,

    # Flag/ausiliari
    "current_is_special": False,
    "restored_from_history": False
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

# ---------- Ricostruzione dallo storico ----------
async def _parse_winner_from_embed(msg: discord.Message) -> Optional[int]:
    """Ritorna l'ID del vincitore leggendo l'embed dell'annuncio ufficiale."""
    if not msg.embeds:
        return None
    emb = msg.embeds[0]
    # Il nostro titolo standard:
    if not emb.title or "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM" not in emb.title:
        return None
    # Nel description cerchiamo la riga "Vincitore: <@123...>"
    if not emb.description:
        return None
    text = emb.description
    # pattern semplice: trova <@...> o <@!...>
    import re
    m = re.search(r"<@!?(\d+)>", text)
    if m:
        try:
            return int(m.group(1))
        except:
            return None
    return None

def _bump_counters_for(uid: str):
    prev = STATE["wins"].get(uid, 0)
    newv = prev + 1
    reset = False
    if newv > 3:
        newv = 1
        reset = True
    STATE["wins"][uid] = newv
    STATE["victories"][uid] = STATE["victories"].get(uid, 0) + 1
    if reset:
        STATE["cycles"][uid] = STATE["cycles"].get(uid, 0) + 1
    STATE["last_win_iso"][uid] = now_tz().isoformat(timespec="seconds")

async def restore_from_history_if_needed(guild: discord.Guild, channel: discord.TextChannel, limit: int = 1200):
    """Se STATE sembra vuoto o è stato resettato, ricostruisce wins/victories leggendo i vecchi annunci."""
    # Heuristic: se non abbiamo vittorie o se è esplicitamente richiesto, prova a ricostruire
    if STATE.get("victories") and STATE.get("wins") and STATE.get("restored_from_history"):
        return

    wins_before = dict(STATE.get("wins", {}))
    vics_before = dict(STATE.get("victories", {}))

    # reset parziale per ricostruire correttamente
    STATE["wins"] = {}
    STATE["victories"] = {}
    STATE["cycles"] = {}
    STATE["last_win_iso"] = {}
    STATE["last_winner_id"] = None

    count = 0
    async for msg in channel.history(limit=limit, oldest_first=True):
        wid = await _parse_winner_from_embed(msg)
        if wid:
            _bump_counters_for(str(wid))
            STATE["last_winner_id"] = wid
            count += 1

    # Se non abbiamo trovato nulla, ripristina lo stato precedente
    if count == 0:
        STATE["wins"] = wins_before
        STATE["victories"] = vics_before
    else:
        STATE["restored_from_history"] = True
    save_state(STATE)

# ---------- Flusso lotteria ----------
async def post_open_message(channel: discord.TextChannel, is_special: bool):
    edition = STATE["edition"]

    if is_special:
        lines = [
            "Cittadini dell’Impero 👑",
            "Edizione **Speciale**: la Borsa dei Premi è aperta!",
            "Da ora fino alle 00:00 di giovedì, partecipa reagendo con ✅ e lascia che gli Dei scelgano il fato.",
            "",
            "🎁 **Premio casuale**: 600.000 / 800.000 / 1.000.000 Kama",
            "➕ **Bonus +200.000 Kama** se il vincitore ha già vinto in passato.",
            "",
            f"**Edizione n°{edition} — SPECIAL EDITION**",
            "",
            "🕗 *Annuncio ufficiale del vincitore giovedì alle **08:00***."
        ]
        title = "LOTTERIA IMPERIVM – EDIZIONE SPECIALE"
    else:
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
            "🕗 *Annuncio ufficiale del vincitore giovedì alle **08:00***."
        ]
        title = "LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE"

    embed = golden_embed(title, "\n".join(lines))
    msg = await channel.send(embed=embed)
    # 🔕 Il bot NON aggiunge reazioni: non deve partecipare.
    STATE["open_message_id"] = msg.id
    STATE["participants"] = []
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

    title = "LOTTERIA IMPERIVM – CHIUSA (SPECIALE)" if STATE.get("current_is_special") else "LOTTERIA IMPERIVM – CHIUSA"
    await channel.send(embed=golden_embed(title, desc))

def _special_prize(total_prev_victories: int) -> int:
    base = random.choice([600_000, 800_000, 1_000_000])
    bonus = 200_000 if total_prev_victories > 0 else 0
    return base + bonus

async def post_winner_announcement(channel: discord.TextChannel, member: Optional[discord.Member]):
    is_special = STATE.get("current_is_special", False)
    if member is None:
        desc = (
            "I sigilli sono stati spezzati, ma stavolta il fato è rimasto muto.\n"
            "Nessun nome scolpito negli annali: riproveremo mercoledì prossimo. 🕯️"
        )
        title = "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM (SPECIALE)" if is_special else "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM"
        await channel.send(embed=golden_embed(title, desc))
        return

    uid = str(member.id)

    if is_special:
        tot_prev = STATE["victories"].get(uid, 0)
        prize_amount = _special_prize(tot_prev)
        # anche nelle speciali registriamo la vittoria e aggiorniamo i livelli “ciclici” 1..3
        _bump_counters_for(uid)
        save_state(STATE)

        desc = (
            "L’Aquila Imperiale ha dispiegato le sue ali e la Borsa dei Premi ha decretato il suo verdetto.\n\n"
            f"👑 **Vincitore (Special Edition):** {member.mention}\n"
            f"💎 **Ricompensa:** {prize_amount:,} Kama\n"
            f"📜 *Bonus Special:* {'attivo (+200.000)' if tot_prev>0 else 'non attivo (prima vittoria)'}\n\n"
            "La fortuna favorisce gli audaci. La prossima chiamata dell’Imperium risuonerà **mercoledì a mezzanotte**."
        )
        title = "ESTRAZIONE UFFICIALE – LOTTERIA IMPERIVM (SPECIALE)"
        await channel.send(embed=golden_embed(title, desc))
        return

    # Classica: usa i livelli
    wins = STATE["wins"].get(uid, 0)
    lvl = level_from_wins(wins)
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
        f"⚔️ **Livello attuale:** {lvl}\n"
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

async def close_and_pick(guild: discord.Guild, announce_now: bool = False) -> Optional[discord.Member]:
    channel = await find_lottery_channel(guild)
    if not channel:
        return None

    # Messaggio apertura
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

    # elenco nomi (no mention)
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

    winner_member: Optional[discord.Member] = None
    STATE["last_winner_id"] = None

    if not no_participants:
        win_id = random.choice(participants)
        STATE["last_winner_id"] = win_id

        # Aggiorna contatori subito qui per la classica (la Special li aggiorna in annuncio)
        if not STATE.get("current_is_special", False):
            _bump_counters_for(str(win_id))
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

async def open_lottery(guild: discord.Guild, force_special: bool = False):
    channel = await find_lottery_channel(guild)
    if not channel:
        return

    # evita doppio open
    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            return
        except Exception:
            pass

    # Edizione speciale ogni 4 (4,8,12,...) oppure forzata
    ed = STATE["edition"]
    is_special = force_special or (ed % 4 == 0)
    STATE["current_is_special"] = is_special
    save_state(STATE)

    await post_open_message(channel, is_special)
    STATE["edition"] += 1
    save_state(STATE)

# ---------- Scheduling (cron + watchdog) ----------
def schedule_weekly_jobs():
    trig_open     = CronTrigger(day_of_week="wed", hour=0, minute=0, timezone=TZ)
    trig_close    = CronTrigger(day_of_week="thu", hour=0, minute=0, timezone=TZ)
    trig_announce = CronTrigger(day_of_week="thu", hour=8, minute=0, timezone=TZ)

    async def do_open():
        for g in bot.guilds:
            ch = await find_lottery_channel(g)
            if ch:
                # prima di aprire, prova a ricostruire dallo storico se serve
                await restore_from_history_if_needed(g, ch)
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
            STATE["current_is_special"] = False
            save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(do_open()), trig_open)
    scheduler.add_job(lambda: asyncio.create_task(do_close()), trig_close)
    scheduler.add_job(lambda: asyncio.create_task(do_announce()), trig_announce)

    # WATCHDOG (ogni 5 min) per recuperare eventi mancati
    async def watchdog():
        dt = now_tz()
        wk = week_key(dt)
        wd = dt.weekday()  # Mon=0 ... Sun=6
        t  = dt.time()

        # Apertura mercoledì 00:00
        if wd == 2 and t >= time(0,0) and STATE.get("last_open_week") != wk:
            for g in bot.guilds:
                ch = await find_lottery_channel(g)
                if ch:
                    await restore_from_history_if_needed(g, ch)
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
            STATE["current_is_special"] = False
            save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(watchdog()), "interval", minutes=5, timezone=TZ)

# ---------- Eventi ----------
@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("Lotteria IMPERIVM"))
    except Exception:
        pass

    # tentativo di restore immediato sul canale lotteria
    for g in bot.guilds:
        ch = await find_lottery_channel(g)
        if ch:
            await restore_from_history_if_needed(g, ch)

    print(f"✅ Bot online come {bot.user} — edizione corrente: {STATE['edition']}")
    if not scheduler.running:
        schedule_weekly_jobs()
        scheduler.start()

# ---------- Comandi testuali (facoltativi) ----------
@bot.command(name="whoami")
async def whoami(ctx: commands.Context):
    adm = "sì" if is_admin(ctx) else "no"
    await ctx.reply(f"ID: {ctx.author.id} — sei admin: {adm}", mention_author=False)

# ---------- Slash commands ----------
def _slash_admin_guard(inter: discord.Interaction) -> bool:
    if ADMIN_IDS and inter.user.id in ADMIN_IDS:
        return True
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

@bot.tree.command(name="mostralivelli", description="Mostra i livelli correnti (1..3) dei vincitori registrati.")
async def slash_mostralivelli(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    wins = STATE.get("wins", {})
    if not wins:
        await inter.response.send_message("📜 Nessun livello registrato al momento.", ephemeral=True); return
    lines = []
    for uid, w in wins.items():
        try:
            m = inter.guild.get_member(int(uid))
            name = m.display_name if m else f"utente {uid}"
        except:
            name = f"utente {uid}"
        lines.append(f"• {name}: **Livello {level_from_wins(w)}** (vittorie cicliche)")
    emb = golden_embed("REGISTRO LIVELLI (corrente)", "\n".join(lines))
    await inter.response.send_message(embed=emb)

@bot.tree.command(name="setwin", description="Registra manualmente una vittoria (aggiorna livelli/bonus interni).")
@app_commands.describe(utente="Seleziona l'utente vincitore da registrare")
async def slash_setwin(inter: discord.Interaction, utente: discord.Member):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    uid = str(utente.id)
    _bump_counters_for(uid)
    save_state(STATE)
    lvl = level_from_wins(STATE["wins"].get(uid, 0))
    tot = STATE["victories"].get(uid, 0)
    cyc = STATE["cycles"].get(uid, 0)
    await inter.response.send_message(
        f"✅ Registrata vittoria per **{utente.display_name}** — Livello attuale: {lvl} • Vittorie totali: {tot} • Cicli: {cyc}"
    )

@bot.tree.command(name="setedition", description="Imposta manualmente il numero dell'edizione corrente.")
@app_commands.describe(numero="Numero edizione da impostare (>=1)")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    if numero < 1:
        await inter.response.send_message("⚠️ L'edizione deve essere ≥ 1.", ephemeral=True); return
    STATE["edition"] = numero
    save_state(STATE)
    await inter.response.send_message(f"🔧 Edizione impostata a **n°{numero}**.")

@bot.tree.command(name="apertura", description="Forza l'apertura della lotteria (classica).")
async def slash_apertura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
    await restore_from_history_if_needed(inter.guild, ch)
    STATE["current_is_special"] = False
    await open_lottery(inter.guild, force_special=False)
    STATE["last_open_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("📜 Apertura (classica) forzata eseguita.")

@bot.tree.command(name="chiusura", description="Forza la chiusura + selezione vincitore (classica/speciale).")
async def slash_chiusura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    await close_and_pick(inter.guild, announce_now=False)
    STATE["last_close_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("🗝️ Chiusura forzata eseguita.")

@bot.tree.command(name="annuncio", description="Forza l'annuncio del vincitore (classica/speciale).")
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
    STATE["current_is_special"] = False
    save_state(STATE)
    await inter.followup.send("📣 Annuncio forzato eseguito.")

# --- Speciale: forzati ---
@bot.tree.command(name="aperturaspeciale", description="Forza l'apertura dell'Edizione Speciale.")
async def slash_apertura_speciale(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
    await restore_from_history_if_needed(inter.guild, ch)
    await open_lottery(inter.guild, force_special=True)
    STATE["last_open_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("📜 Apertura **Special Edition** forzata eseguita.")

@bot.tree.command(name="chiusuraspeciale", description="Forza la chiusura dell'Edizione Speciale.")
async def slash_chiusura_speciale(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    STATE["current_is_special"] = True
    await close_and_pick(inter.guild, announce_now=False)
    STATE["last_close_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("🗝️ Chiusura **Special Edition** forzata eseguita.")

@bot.tree.command(name="annunciospeciale", description="Forza l'annuncio dell'Edizione Speciale.")
async def slash_annuncio_speciale(inter: discord.Interaction):
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
    STATE["current_is_special"] = True
    await post_winner_announcement(ch, member)
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    STATE["current_is_special"] = False
    save_state(STATE)
    await inter.followup.send("📣 Annuncio **Special Edition** forzato eseguito.")

# ---------- Avvio ----------
@bot.event
async def setup_hook():
    try:
        await bot.tree.sync()
    except Exception:
        pass

if __name__ == "__main__":
    start_web_server()  # server HTTP per Render
    bot.run(TOKEN)
