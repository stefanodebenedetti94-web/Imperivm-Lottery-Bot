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
    "wins": {},               # {uid(str): livello 1..3 (reset a 1 dopo 3)}
    "victories": {},          # {uid(str): totale vittorie storiche} (serve per Bonus Fedeltà)
    "last_winner_id": None,   # salvato alla chiusura; annunciato alle 08:00

    # Marcatori settimanali anti-duplicato (per cron/watchdog)
    "last_open_week": None,       # "YYYY-WW"
    "last_close_week": None,
    "last_announce_week": None,

    # Flag di stato ciclo corrente
    "current_is_special": False,      # l'edizione in corso è Special?
    "special_info": None              # dict con {"base": int, "bonus": int, "total": int} per l'ultimo esito special
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

def level_from_wins(w: int) -> int:
    if w <= 0: return 0
    return min(w, 3)

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

def edition_is_special(n: int) -> bool:
    # Special ogni 4 edizioni: 4, 8, 12, ...
    return n % 4 == 0

# ---------- Messaggi ----------
def open_text_lines(edition: int, special: bool) -> List[str]:
    if special:
        return [
            "Cittadini dell'Impero 👑",
            "Questa è una **Edizione Speciale**: premio casuale tra **600.000 / 800.000 / 1.000.000 Kama**.",
            "Se il vincitore ha già vinto in passato → **Bonus Fedeltà +200.000 Kama**.",
            "*Nota:* questa edizione **non modifica i livelli**.",
            "",
            "Reagisci con ✅ a questo messaggio per partecipare.",
            "Chiusura: **giovedì 00:00** • Annuncio: **giovedì 08:00**",
            "",
            f"**Edizione n°{edition} (SPECIALE)**"
        ]
    else:
        return [
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
            "🕗 *Annuncio del vincitore giovedì alle 08:00.*"
        ]

def close_text_desc(no_participants: bool, names_preview: Optional[str], special: bool) -> str:
    title = "LOTTERIA IMPERIVM – SPECIALE CHIUSA" if special else "LOTTERIA IMPERIVM – CHIUSA"
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
    return title, desc

def announce_text(member: Optional[discord.Member], special: bool, prize_info: Optional[dict], lvl: int) -> Tuple[str, str]:
    if special:
        title = "ESTRAZIONE UFFICIALE — EDIZIONE SPECIALE"
        if member is None:
            return title, (
                "I sigilli sono stati spezzati, ma stavolta il fato è rimasto muto.\n"
                "Nessun nome scolpito negli annali: riproveremo mercoledì prossimo. 🕯️"
            )
        base = prize_info.get("base", 0) if prize_info else 0
        bonus = prize_info.get("bonus", 0) if prize_info else 0
        total = prize_info.get("total", base + bonus)
        return title, (
            "**Il verdetto è scolpito negli annali!**\n"
            f"Il fato ha scelto: **{member.mention}** ⚖️\n\n"
            f"**Premio estratto:** {base:,} Kama\n"
            f"**Bonus Fedeltà:** {('+' + format(bonus, ',') + ' Kama') if bonus else '—'}\n"
            f"**Totale riconosciuto:** **{total:,} Kama**\n\n"
            "_Questa edizione non modifica i livelli._\n"
            "La prossima chiamata dell’Aquila Imperiale risuonerà **mercoledì a mezzanotte**."
        )
    else:
        title = "ESTRAZIONE UFFICIALE — LOTTERIA IMPERIVM"
        if member is None:
            return title, (
                "I sigilli sono stati spezzati, ma stavolta il fato è rimasto muto.\n"
                "Nessun nome scolpito negli annali: riproveremo mercoledì prossimo. 🕯️"
            )
        if lvl == 1:
            premio = "100.000 Kama"
        elif lvl == 2:
            premio = "Scudo di Gilda *(se già posseduto → 250.000 Kama)*"
        else:
            premio = "500.000 Kama *(reset dei livelli)*"
        return title, (
            "Cittadini dell’Impero, il fato ha parlato e i sigilli sono stati sciolti.\n"
            "Tra pergamene e ceralacca, il nome inciso negli annali è stato scelto.\n\n"
            f"👑 **Vincitore:** {member.mention}\n"
            f"⚔️ **Livello attuale:** {lvl}\n"
            f"📜 **Ricompensa:** {premio}\n\n"
            "Che la fortuna continui a sorriderti. La prossima chiamata dell’Aquila Imperiale\n"
            "risuonerà **mercoledì a mezzanotte**. Presentatevi senza timore."
        )

# ---------- Flusso lotteria ----------
async def post_open_message(channel: discord.TextChannel):
    edition = STATE["edition"]
    special = edition_is_special(edition) or STATE.get("current_is_special", False)
    lines = open_text_lines(edition, special)
    embed = golden_embed(
        "LOTTERIA IMPERIVM — EDIZIONE SPECIALE" if special else "LOTTERIA IMPERIVM – EDIZIONE SETTIMANALE",
        "\n".join(lines)
    )
    msg = await channel.send(embed=embed)
    # Il bot NON reagisce con ✅ (decidono i player)
    STATE["open_message_id"] = msg.id
    save_state(STATE)
    return msg

async def post_close_message(channel: discord.TextChannel, no_participants: bool, names_preview: Optional[str]):
    special = STATE.get("current_is_special", False) or edition_is_special(STATE["edition"])
    title, desc = close_text_desc(no_participants, names_preview, special)
    await channel.send(embed=golden_embed(title, desc))

async def post_winner_announcement(channel: discord.TextChannel, member: Optional[discord.Member]):
    special = STATE.get("current_is_special", False) or edition_is_special(STATE["edition"])
    uid = str(member.id) if member else None
    lvl = level_from_wins(STATE["wins"].get(uid, 0)) if uid else 0
    title, desc = announce_text(member, special, STATE.get("special_info"), lvl)
    await channel.send(embed=golden_embed(title, desc))

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

def _bump_classic_win(uid: str):
    prev = STATE["wins"].get(uid, 0)
    new = prev + 1
    if new > 3:
        new = 1  # reset dopo la 3ª vittoria
    STATE["wins"][uid] = new
    STATE["victories"][uid] = STATE["victories"].get(uid, 0) + 1

def _compute_special_prize(uid: str) -> dict:
    base = random.choice([600_000, 800_000, 1_000_000])
    bonus = 200_000 if STATE["victories"].get(uid, 0) >= 1 else 0
    total = base + bonus
    return {"base": base, "bonus": bonus, "total": total}

async def close_and_pick(guild: discord.Guild, announce_now: bool = False):
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
    STATE["special_info"] = None

    if not no_participants:
        win_id = random.choice(participants)
        STATE["last_winner_id"] = win_id
        uid = str(win_id)

        is_special = STATE.get("current_is_special", False) or edition_is_special(STATE["edition"])
        if is_special:
            # Non toccare i livelli; solo calcolo premio special (per annuncio)
            # Aggiorniamo comunque il contatore “victories” per memoria storica
            STATE["victories"][uid] = STATE["victories"].get(uid, 0) + 1
            STATE["special_info"] = _compute_special_prize(uid)
        else:
            _bump_classic_win(uid)

        save_state(STATE)
        try:
            winner_member = await guild.fetch_member(win_id)
        except Exception:
            winner_member = guild.get_member(win_id)

    if announce_now:
        await post_winner_announcement(channel, winner_member)

    # chiudo messaggio corrente
    STATE["open_message_id"] = None
    save_state(STATE)
    return winner_member

async def open_lottery(guild: discord.Guild):
    channel = await find_lottery_channel(guild)
    if not channel:
        return
    # Evita doppie aperture se il messaggio c'è ancora
    if STATE.get("open_message_id"):
        try:
            await channel.fetch_message(STATE["open_message_id"])
            return
        except Exception:
            pass
    # flag special
    STATE["current_is_special"] = edition_is_special(STATE["edition"])
    await post_open_message(channel)
    # incrementa edizione (vale dall’apertura successiva)
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
            STATE["special_info"] = None
            # reset flag special per sicurezza
            STATE["current_is_special"] = False
            save_state(STATE)

    scheduler.add_job(lambda: asyncio.create_task(do_open()), trig_open)
    scheduler.add_job(lambda: asyncio.create_task(do_close()), trig_close)
    scheduler.add_job(lambda: asyncio.create_task(do_announce()), trig_announce)

    # WATCHDOG ogni 5 minuti per recupero eventi mancati
    async def watchdog():
        dt = now_tz()
        wk = week_key(dt)
        wd = dt.weekday()   # Mon=0 ... Sun=6
        t  = dt.time()

        # Apertura Mer 00:00
        if wd == 2 and t >= time(0,0) and STATE.get("last_open_week") != wk:
            for g in bot.guilds:
                await open_lottery(g)
            STATE["last_open_week"] = wk; save_state(STATE); return

        # Chiusura Gio 00:00
        if wd == 3 and t >= time(0,0) and STATE.get("last_close_week") != wk:
            for g in bot.guilds:
                await close_and_pick(g, announce_now=False)
            STATE["last_close_week"] = wk; save_state(STATE); return

        # Annuncio Gio 08:00
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
            STATE["special_info"] = None
            STATE["current_is_special"] = False
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

# ---------- Slash commands ----------
def _slash_admin_guard(inter: discord.Interaction) -> bool:
    if ADMIN_IDS and inter.user.id in ADMIN_IDS:
        return True
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

@bot.tree.command(name="apertura", description="Forza l'apertura della lotteria (classica) — solo admin.")
async def slash_apertura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    # forza classica per questa apertura
    STATE["current_is_special"] = False
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
    await open_lottery(inter.guild)
    STATE["last_open_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("📜 Apertura **classica** forzata eseguita.")

@bot.tree.command(name="chiusura", description="Forza la chiusura e la selezione del vincitore (classica) — solo admin.")
async def slash_chiusura(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    # non annunciare subito
    await close_and_pick(inter.guild, announce_now=False)
    STATE["last_close_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("🗝️ Chiusura **classica** forzata eseguita.")

@bot.tree.command(name="annuncio", description="Forza l'annuncio del vincitore (classica) — solo admin.")
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
    STATE["special_info"] = None
    STATE["current_is_special"] = False
    save_state(STATE)
    await inter.followup.send("📣 Annuncio **classico** forzato eseguito.")

# ---- Speciale (forzato) ----
@bot.tree.command(name="aperturaspeciale", description="Forza l'apertura della Edizione Speciale — solo admin.")
async def slash_aperturaspeciale(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    STATE["current_is_special"] = True
    ch = await find_lottery_channel(inter.guild)
    if not ch:
        await inter.followup.send("⚠️ Canale lotteria non trovato."); return
    await post_open_message(ch)
    # incremento edizione come in open_lottery
    STATE["edition"] += 1
    STATE["last_open_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("🎴 Apertura **SPECIALE** forzata eseguita.")

@bot.tree.command(name="chiusuraspeciale", description="Forza la chiusura della Edizione Speciale — solo admin.")
async def slash_chiusuraspeciale(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    # assicura special
    STATE["current_is_special"] = True
    await close_and_pick(inter.guild, announce_now=False)
    STATE["last_close_week"] = week_key(now_tz()); save_state(STATE)
    await inter.followup.send("🗝️ Chiusura **SPECIALE** forzata eseguita.")

@bot.tree.command(name="annunciospeciale", description="Forza l'annuncio della Edizione Speciale — solo admin.")
async def slash_annunciospeciale(inter: discord.Interaction):
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
    # assicura special
    STATE["current_is_special"] = True
    await post_winner_announcement(ch, member)
    STATE["last_announce_week"] = week_key(now_tz())
    STATE["last_winner_id"] = None
    STATE["special_info"] = None
    STATE["current_is_special"] = False
    save_state(STATE)
    await inter.followup.send("📣 Annuncio **SPECIALE** forzato eseguito.")

# ---- Utility admin: setwin / mostralivelli / setedition ----
@bot.tree.command(name="setwin", description="Registra manualmente una **vittoria classica** per un utente (avanza livello).")
@app_commands.describe(utente="Seleziona l'utente vincitore da registrare")
async def slash_setwin(inter: discord.Interaction, utente: discord.Member):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    await inter.response.defer(thinking=True)
    uid = str(utente.id)
    _bump_classic_win(uid)
    save_state(STATE)
    lvl = level_from_wins(STATE["wins"].get(uid, 0))
    tot = STATE["victories"].get(uid, 0)
    await inter.followup.send(f"✅ Registrata **vittoria classica** per **{utente.display_name}** — Livello attuale: {lvl} • Vittorie totali: {tot}")

@bot.tree.command(name="mostralivelli", description="Mostra i livelli attuali dei vincitori registrati.")
async def slash_mostralivelli(inter: discord.Interaction):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    wins = STATE.get("wins", {})
    if not wins:
        await inter.response.send_message("📜 Nessun livello registrato al momento.", ephemeral=True); return
    lines = []
    for uid, w in wins.items():
        try:
            member = await inter.guild.fetch_member(int(uid))
        except Exception:
            member = inter.guild.get_member(int(uid))
        tag = member.mention if member else f"<@{uid}>"
        lines.append(f"{tag}: **Livello {level_from_wins(w)}**")
    embed = golden_embed("REGISTRO LIVELLI (corrente)", "\n".join(lines))
    await inter.response.send_message(embed=embed)

@bot.tree.command(name="setedition", description="Imposta manualmente il numero di edizione (solo admin).")
@app_commands.describe(numero="Numero di edizione da impostare (es. 3, 4, 5...)")
async def slash_setedition(inter: discord.Interaction, numero: int):
    if not _slash_admin_guard(inter):
        await inter.response.send_message("❌ Non sei autorizzato.", ephemeral=True); return
    if numero < 1:
        await inter.response.send_message("⚠️ L'edizione deve essere >= 1.", ephemeral=True); return
    STATE["edition"] = numero
    save_state(STATE)
    await inter.response.send_message(f"🔧 Edizione impostata a **n°{numero}**.", ephemeral=True)

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
