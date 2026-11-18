"""
Telegram HTML Web Hosting Bot (UPI Premium + Daily Claim + Premium Emoji)

Flow:
- Free user: site upload nahi kar sakta (0 free sites).
- Daily claim: har din (IST) ₹DAILY_CLAIM wallet mein milta hai.
- Premium Lo: QR generate hota hai (UPI), user payment karke screenshot / UTR bhejta hai,
  admin Approve karta hai -> user ko unlimited sites ka access.
- Admin UPI ID / naam Admin Panel se set karta hai.
- Bot ke saare messages mein premium emoji (custom emoji) use hote hain, jo admin
  Admin Panel -> Premium Emoji mein premium emoji bhejkar set karta hai.
"""

import asyncio
import html as H
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import segno
from flask import Flask, Response
from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import MessageEntityType, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhost-bot")

# ---------------- CONFIG (sab Render ke ENV se aata hai) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:10000").rstrip("/")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",") if x.strip()}
DEV_NAME = os.environ.get("DEV_NAME", "Developer")
DEV_USERNAME = os.environ.get("DEV_USERNAME", "").lstrip("@")
MAX_BYTES = int(float(os.environ.get("MAX_FILE_MB", "1")) * 1024 * 1024)
PREMIUM_PRICE = int(os.environ.get("PREMIUM_PRICE", "50"))
DAILY_CLAIM = int(os.environ.get("DAILY_CLAIM", "3"))
UPI_ID_DEFAULT = os.environ.get("UPI_ID", "")          # Admin Panel se bhi set ho sakta hai
UPI_NAME_DEFAULT = os.environ.get("UPI_NAME", DEV_NAME)
PORT = int(os.environ.get("PORT", "10000"))
IST = timezone(timedelta(hours=5, minutes=30))

if not ADMIN_IDS:
    log.warning("ADMIN_ID set nahi hai - admin features kaam nahi karenge")

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,29}$")
UPI_RE = re.compile(r"^[A-Za-z0-9.\-_]{2,256}@[A-Za-z]{2,64}$")
RESERVED = {"admin", "api", "health", "static", "login", "bot", "www", "index", "favicon-ico"}
NOPREVIEW = LinkPreviewOptions(is_disabled=True)
PAGE = 8

# ---------------- REDIS KEYS ----------------
# site:<name>        -> html content
# owner:<name>       -> owner user id
# user:<id>:sites    -> set of site names
# users              -> set of all user ids
# uinfo:<id>         -> "naam\tusername"
# banned             -> set of banned user ids
# premium            -> set of premium user ids (unlimited sites)
# wallet:<id>        -> daily claim balance (₹)
# claim:<id>         -> last claim date (IST, YYYY-MM-DD)
# paycounter         -> payment id counter
# pay:<pid>          -> payment json
# pay:pending        -> set of pending payment ids
# ppend:<id>         -> payment id jo user ka QR scan karke proof bhejega
# psub:<id>          -> payment id jo admin approval ke liye pending hai
# cfg:upi_id / cfg:upi_name -> UPI settings
# pemoji             -> json {"standard emoji": "custom_emoji_id"}
# channels / chan:<id> -> force-join channels


# ---------------- BASIC HELPERS ----------------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_banned(uid: int) -> bool:
    return bool(redis.sismember("banned", str(uid)))


def is_premium(uid: int) -> bool:
    return is_admin(uid) or bool(redis.sismember("premium", str(uid)))


def set_premium(uid: int, on: bool):
    if on:
        redis.sadd("premium", str(uid))
    else:
        redis.srem("premium", str(uid))


def site_url(name: str) -> str:
    return f"{BASE_URL}/{name}"


def owner_of(name: str) -> str:
    return str(redis.get(f"owner:{name}") or "")


def can_manage(uid: int, name: str) -> bool:
    return owner_of(name) == str(uid) or is_admin(uid)


def get_wallet(uid: int) -> int:
    return int(redis.get(f"wallet:{uid}") or 0)


def get_upi():
    upi_id = str(redis.get("cfg:upi_id") or UPI_ID_DEFAULT or "")
    name = str(redis.get("cfg:upi_name") or UPI_NAME_DEFAULT or "")
    return upi_id, name


def now_str() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def delete_site(name: str):
    owner = owner_of(name)
    if owner:
        redis.srem(f"user:{owner}:sites", name)
    redis.delete(f"site:{name}", f"owner:{name}")


def user_label(uid):
    raw = redis.get(f"uinfo:{uid}")
    if raw:
        name, _, un = str(raw).partition("\t")
        return (name or str(uid)), un
    return str(uid), ""


def chan_info(cid):
    raw = redis.get(f"chan:{cid}")
    if not raw:
        return str(cid), ""
    title, _, link = str(raw).partition("\t")
    return title or str(cid), link


# ---------------- PREMIUM EMOJI ----------------
_emoji_cache = {"t": 0.0, "map": {}}


def emoji_map() -> dict:
    if time.time() - _emoji_cache["t"] > 30:
        try:
            raw = redis.get("pemoji")
            data = json.loads(raw) if raw else {}
        except Exception as e:  # Redis / json problem par purana map use karo
            log.warning("emoji map load failed: %s", e)
            data = _emoji_cache["map"]
        _emoji_cache.update(t=time.time(), map=data)
    return _emoji_cache["map"]


def save_emoji_map(data: dict):
    redis.set("pemoji", json.dumps(data, ensure_ascii=False))
    _emoji_cache.update(t=time.time(), map=data)


# code / pre / already-premium tags ke andar emoji nahi badalte
_KEEP_RE = re.compile(r"(<code>.*?</code>|<pre>.*?</pre>|<tg-emoji[^>]*>.*?</tg-emoji>)", re.S)


def pe(text: str) -> str:
    """Standard emoji ko premium (custom) emoji se badalta hai, agar uski ID set hai."""
    m = emoji_map()
    if not m:
        return text
    pat = re.compile("|".join(re.escape(k) for k in sorted(m, key=len, reverse=True)))

    def repl(mo):
        ch = mo.group(0)
        return f'<tg-emoji emoji-id="{m[ch]}">{ch}</tg-emoji>'

    parts = _KEEP_RE.split(text)
    for i in range(0, len(parts), 2):  # even index = normal text, odd = protected
        parts[i] = pat.sub(repl, parts[i])
    return "".join(parts)


# ---------------- SEND HELPERS (sab messages yahin se jaate hain) ----------------
async def send(msg, text, markup=None):
    return await msg.reply_text(
        pe(text), parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NOPREVIEW,
    )


async def edit(q, text, markup=None):
    try:
        await q.edit_message_text(
            pe(text), parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NOPREVIEW,
        )
    except Exception as e:  # same content ho to Telegram error deta hai, ignore
        log.debug("edit skipped: %s", e)


async def notify(bot, chat_id, text, markup=None):
    try:
        await bot.send_message(
            chat_id, pe(text), parse_mode=ParseMode.HTML, reply_markup=markup,
            link_preview_options=NOPREVIEW,
        )
    except Exception as e:
        log.warning("notify %s failed: %s", chat_id, e)


# ---------------- FLASK (sites serve karta hai) ----------------
web = Flask(__name__)

HOME_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Web Hosting Bot</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center;padding:16px}}
h1{{font-size:2rem}}p{{color:#94a3b8}}</style></head>
<body><div><h1>🌐 HTML Hosting</h1>
<p>Telegram bot se apni HTML site live karo.</p>
<p>Developed by {H.escape(DEV_NAME)}</p></div></body></html>"""

NOT_FOUND = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>404</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center;padding:16px}</style></head>
<body><div><h1>404</h1><p>Ye site exist nahi karti.</p></div></body></html>"""


@web.route("/")
def home():
    return Response(HOME_HTML, mimetype="text/html")


@web.route("/health")
def health():
    return "ok"


@web.route("/<name>")
def serve_site(name):
    content = redis.get(f"site:{name.lower()}")
    if not content:
        return Response(NOT_FOUND, status=404, mimetype="text/html")
    return Response(str(content), mimetype="text/html")


def run_web():
    web.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)


# ---------------- TEXTS & KEYBOARDS ----------------
def home_btn() -> Markup:
    return Markup([[Btn("🏠 Home", callback_data="home")]])


def main_menu(uid: int) -> Markup:
    rows = [
        [Btn("📤 Site Upload Karo", callback_data="upload"), Btn("📂 Meri Sites", callback_data="mysites")],
        [Btn(f"🎁 Daily Claim (+₹{DAILY_CLAIM})", callback_data="claim"),
         Btn(f"💎 Premium Lo (₹{PREMIUM_PRICE})", callback_data="buy")],
        [Btn("ℹ️ Help", callback_data="help")],
    ]
    dev_row = []
    if DEV_USERNAME:
        dev_row.append(Btn("👨‍💻 Developer", url=f"https://t.me/{DEV_USERNAME}"))
    if is_admin(uid):
        dev_row.append(Btn("🛡 Admin Panel", callback_data="admin"))
    if dev_row:
        rows.append(dev_row)
    return Markup(rows)


def premium_markup() -> Markup:
    return Markup([
        [Btn(f"💎 Premium Lo (₹{PREMIUM_PRICE})", callback_data="buy")],
        [Btn(f"🎁 Daily Claim (+₹{DAILY_CLAIM})", callback_data="claim")],
        [Btn("🏠 Home", callback_data="home")],
    ])


def premium_text() -> str:
    return (
        "🔒 <b>Site upload ke liye Premium chahiye</b>\n\n"
        f"💎 Premium: <b>₹{PREMIUM_PRICE}</b> (ek baar)\n"
        "✅ Premium ke baad <b>unlimited sites</b>\n\n"
        "UPI se pay karo, admin approve karega, phir upload chalu! 🚀"
    )


def status_text(uid: int) -> str:
    if is_admin(uid):
        return "👑 Admin"
    if is_premium(uid):
        return "💎 Premium — Unlimited sites"
    return "🔒 Free — upload ke liye Premium chahiye"


def welcome_text(user) -> str:
    uid = user.id
    return (
        f"👋 <b>Welcome, {H.escape(user.first_name or 'dost')}!</b>\n\n"
        "Main ek <b>HTML Hosting Bot</b> hu.\n"
        "Apni <code>.html</code> file bhejo, naam do, aur turant tumhari site live! 🚀\n\n"
        f"🏷 Status: <b>{status_text(uid)}</b>\n"
        f"💰 Wallet: <b>₹{get_wallet(uid)}</b>\n\n"
        "👇 Neeche button se shuru karo"
    )


HELP = (
    "ℹ️ <b>Kaise use karein</b>\n\n"
    "1️⃣ <b>💎 Premium Lo</b> dabao aur ₹" + str(PREMIUM_PRICE) + " UPI se pay karo\n"
    "2️⃣ QR scan karo ya UPI ID par payment karo\n"
    "3️⃣ Screenshot ya 12-digit UTR bhejo\n"
    "4️⃣ Admin approve karega → <b>Unlimited sites</b> ✅\n"
    "5️⃣ <b>📤 Site Upload Karo</b> → <code>.html</code> file → naam → link ready 🎉\n\n"
    f"🎁 Roz <b>Daily Claim</b> karo: +₹{DAILY_CLAIM}\n"
    f"📏 Max file size: <b>{MAX_BYTES // (1024 * 1024)} MB</b>\n"
    "✏️ Same naam se dobara upload karoge to site <b>update</b> ho jayegi."
)


JOIN_TEXT = (
    "🔒 <b>Bot use karne ke liye pehle ye channels join karo</b>\n\n"
    "Join karne ke baad neeche <b>✅ Maine Join Kar Liya</b> dabao."
)


# ---------------- FORCE JOIN ----------------
async def missing_channels(bot, uid: int):
    out = []
    for cid in sorted(redis.smembers("channels") or []):
        try:
            m = await bot.get_chat_member(int(cid), uid)
            if m.status in ("left", "kicked"):
                out.append(cid)
        except Exception as e:
            log.warning("channel check failed %s: %s", cid, e)
    return out


def join_markup(missing) -> Markup:
    rows = []
    for cid in missing:
        title, link = chan_info(cid)
        if link:
            rows.append([Btn(f"📢 {title[:30]}", url=link)])
    rows.append([Btn("✅ Maine Join Kar Liya", callback_data="chk")])
    return Markup(rows)


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """True = aage badho. False = banned ya channel join nahi kiya."""
    u = update.effective_user
    if u is None:
        return False
    uid = u.id
    if is_banned(uid):
        if update.callback_query:
            await update.callback_query.answer("🚫 Tum banned ho.", show_alert=True)
        elif update.effective_message:
            await send(update.effective_message, "🚫 Tumhe is bot se ban kar diya gaya hai.")
        return False
    redis.sadd("users", str(uid))
    redis.set(f"uinfo:{uid}", f"{(u.full_name or '').replace(chr(9), ' ')}\t{u.username or ''}")

    if not is_admin(uid):
        missing = await missing_channels(ctx.bot, uid)
        if missing:
            markup = join_markup(missing)
            q = update.callback_query
            if q:
                await q.answer("❌ Pehle saare channel join karo", show_alert=True)
                if q.data != "chk":
                    await edit(q, JOIN_TEXT, markup)
            elif update.effective_message:
                await send(update.effective_message, JOIN_TEXT, markup)
            return False
    return True


# ---------------- DAILY CLAIM ----------------
def try_claim(uid: int):
    today = datetime.now(IST).date().isoformat()
    if redis.get(f"claim:{uid}") == today:
        return False, get_wallet(uid)
    balance = int(redis.incrby(f"wallet:{uid}", DAILY_CLAIM))
    redis.set(f"claim:{uid}", today)
    return True, balance


# ---------------- PREMIUM PAYMENT ----------------
def pay_get(pid):
    raw = redis.get(f"pay:{pid}")
    return json.loads(str(raw)) if raw else None


def pay_put(pid, data: dict):
    redis.set(f"pay:{pid}", json.dumps(data, ensure_ascii=False))


def upi_link(upi_id: str, name: str, amount: int, note: str, ref: str) -> str:
    query = urlencode(
        {"pa": upi_id, "pn": name, "am": f"{amount:.2f}", "cu": "INR", "tn": note, "tr": ref},
        quote_via=quote,
    )
    return "upi://pay?" + query


def qr_png(data: str) -> io.BytesIO:
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=3)
    buf.seek(0)
    buf.name = "upi-qr.png"
    return buf


def pay_caption(pid, d: dict) -> str:
    proof = ""
    if d.get("proof_type") == "text":
        proof = f"\n🧾 UTR / Text: <code>{H.escape(str(d.get('proof', '')))}</code>"
    elif d.get("proof_type") == "photo":
        proof = "\n🖼 Screenshot: <b>niche dekho</b>"
    uname = f" (@{H.escape(d['username'])})" if d.get("username") else ""
    return (
        f"💳 <b>Payment #{pid}</b>\n\n"
        f"👤 {H.escape(d.get('name', ''))}{uname}\n"
        f"🆔 <code>{d.get('uid')}</code>\n"
        f"💰 Amount: <b>₹{d.get('amount')}</b>\n"
        f"📌 Status: <b>{H.escape(str(d.get('status', '')))}</b>"
        f"{proof}\n"
        f"🕒 {H.escape(str(d.get('submitted') or d.get('created', '')))}"
    )


def pay_buttons(pid) -> Markup:
    return Markup([
        [Btn("✅ Approve", callback_data=f"pa:{pid}"), Btn("❌ Reject", callback_data=f"pr:{pid}")],
    ])


async def start_buy(msg, tg_user, bot):
    uid = tg_user.id
    if is_premium(uid):
        return await send(msg, "💎 Tum already <b>Premium</b> ho! Unlimited sites ready ✅", home_btn())
    if redis.get(f"psub:{uid}"):
        return await send(
            msg, "⏳ Tumhara payment admin ke approval mein hai. Approve hote hi premium active ho jayega.",
            home_btn(),
        )
    upi_id, upi_name = get_upi()
    if not upi_id:
        return await send(msg, "⚠️ Admin ne abhi UPI ID set nahi ki hai. Thodi der baad try karo.", home_btn())

    pid = redis.get(f"ppend:{uid}")
    data = pay_get(pid) if pid else None
    if not data or data.get("status") != "created":
        pid = str(redis.incr("paycounter"))
        data = {
            "uid": uid,
            "name": tg_user.full_name or str(uid),
            "username": tg_user.username or "",
            "amount": PREMIUM_PRICE,
            "status": "created",
            "created": now_str(),
        }
        pay_put(pid, data)
        redis.set(f"ppend:{uid}", pid)

    link = upi_link(upi_id, upi_name or "Admin", PREMIUM_PRICE, f"WebHost Premium {pid}", f"WHP{pid}")
    caption = (
        f"💎 <b>Premium — ₹{PREMIUM_PRICE}</b>\n"
        f"🏦 UPI ID: <code>{H.escape(upi_id)}</code>\n"
        f"👤 Name: {H.escape(upi_name)}\n\n"
        f"1️⃣ QR scan karo ya UPI ID par <b>₹{PREMIUM_PRICE}</b> bhejo\n"
        "2️⃣ Payment ka <b>screenshot</b> bhejo, ya <b>12-digit UTR</b> likho\n"
        "3️⃣ Admin approve karega → <b>Unlimited sites</b> ✅\n\n"
        f"🧾 Ref: <code>WHP{pid}</code>"
    )
    try:
        await msg.reply_photo(
            photo=qr_png(link), caption=pe(caption), parse_mode=ParseMode.HTML, reply_markup=home_btn(),
        )
    except Exception as e:
        log.warning("QR send failed: %s", e)
        await send(msg, caption + f"\n\n⚠️ QR nahi ban paya, upar wali UPI ID par pay karo.", home_btn())


async def submit_proof(msg, tg_user, ctx, kind: str, value: str) -> bool:
    uid = tg_user.id
    pid = redis.get(f"ppend:{uid}")
    data = pay_get(pid) if pid else None
    if not data or data.get("status") != "created":
        redis.delete(f"ppend:{uid}")
        return False
    data.update(status="submitted", proof_type=kind, proof=value, submitted=now_str())
    pay_put(pid, data)
    redis.delete(f"ppend:{uid}")
    redis.set(f"psub:{uid}", pid)
    redis.sadd("pay:pending", pid)

    await send(
        msg, "✅ Payment proof mil gaya! Admin approve karega, phir premium active ho jayega 🙏", home_btn(),
    )
    cap = pay_caption(pid, data)
    for aid in ADMIN_IDS:
        try:
            if kind == "photo":
                await ctx.bot.send_photo(
                    aid, value, caption=pe(cap), parse_mode=ParseMode.HTML, reply_markup=pay_buttons(pid),
                )
            else:
                await ctx.bot.send_message(
                    aid, pe(cap), parse_mode=ParseMode.HTML, reply_markup=pay_buttons(pid),
                    link_preview_options=NOPREVIEW,
                )
        except Exception as e:
            log.warning("admin notify failed %s: %s", aid, e)
    return True


async def decide_payment(pid: str, approve: bool, admin_name: str):
    data = pay_get(pid)
    if not data or data.get("status") != "submitted":
        return None
    uid = int(data["uid"])
    data.update(
        status="approved" if approve else "rejected",
        decided_by=admin_name,
        decided=now_str(),
    )
    pay_put(pid, data)
    redis.srem("pay:pending", pid)
    redis.delete(f"psub:{uid}")
    if approve:
        set_premium(uid, True)
    return uid, data["status"]


async def notify_user_decision(bot, uid: int, approved: bool):
    if approved:
        text = (
            "🎉 <b>Payment approve ho gayi!</b>\n\n"
            "💎 Tum ab <b>Premium</b> ho — <b>unlimited sites</b> ✅\n"
            "📤 Ab site upload karo! 🚀"
        )
    else:
        text = (
            "❌ <b>Payment reject ho gayi.</b>\n\n"
            "Sahi screenshot ya UTR ke saath dobara <b>💎 Premium Lo</b> karo."
        )
    await notify(bot, uid, text, home_btn())


# ---------------- ADMIN SCREENS ----------------
def admin_markup() -> Markup:
    return Markup([
        [Btn("👥 Users & Sites", callback_data="users:0"), Btn("💳 Payments", callback_data="payments")],
        [Btn("📡 Channels", callback_data="channels"), Btn("📢 Broadcast", callback_data="broadcast")],
        [Btn("🏦 UPI Settings", callback_data="upi"), Btn("⭐ Premium Emoji", callback_data="pemoji")],
        [Btn("🔄 Refresh", callback_data="admin"), Btn("🏠 Home", callback_data="home")],
    ])


async def show_admin(q):
    users = redis.scard("users")
    banned = redis.scard("banned")
    premium = redis.scard("premium")
    pending = redis.scard("pay:pending")
    sites = len(redis.keys("site:*") or [])
    chans = redis.scard("channels")
    await edit(
        q,
        "🛡 <b>Admin Panel</b>\n\n"
        f"👥 Total users: <b>{users}</b>\n"
        f"💎 Premium users: <b>{premium}</b>\n"
        f"💳 Pending payments: <b>{pending}</b>\n"
        f"🌐 Total sites: <b>{sites}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n"
        f"📡 Force-join channels: <b>{chans}</b>\n\n"
        "<b>Commands:</b>\n"
        "<code>/premium user_id</code> · <code>/unpremium user_id</code>\n"
        "<code>/ban user_id</code> · <code>/unban user_id</code>\n"
        "<code>/delsite name</code>",
        admin_markup(),
    )


async def show_payments(q):
    pids = sorted(redis.smembers("pay:pending") or [], key=lambda x: int(x))
    rows = []
    for pid in pids[:20]:
        d = pay_get(pid) or {}
        if d.get("status") != "submitted":
            continue
        rows.append([Btn(f"💳 #{pid} • {str(d.get('name', ''))[:16]} • ₹{d.get('amount')}", callback_data=f"pay:{pid}")])
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    text = (
        "💳 <b>Pending Payments</b>\n\n"
        + ("Kisi payment par tap karo, proof dekhkar approve/reject karo." if rows[:-1] else "Abhi koi pending payment nahi hai ✅")
    )
    await edit(q, text, Markup(rows))


async def show_users(q, page: int):
    ids = sorted(redis.smembers("users") or [], key=lambda x: int(x))
    total = len(ids)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * PAGE:(page + 1) * PAGE]
    rows = []
    for x in chunk:
        name, _ = user_label(x)
        cnt = redis.scard(f"user:{x}:sites")
        flag = "🚫 " if is_banned(int(x)) else ("💎 " if is_premium(int(x)) else "")
        rows.append([Btn(f"{flag}{name[:18]} • 🌐 {cnt}", callback_data=f"u:{x}:{page}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"users:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn("Next ➡️", callback_data=f"users:{page + 1}"))
    rows.append(nav)
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    await edit(
        q, f"👥 <b>Users ({total})</b>\n\nKisi user par tap karo, uski sites dikhengi.\n"
           "Format: naam • 🌐 sites ki sankhya", Markup(rows))


async def show_user(q, x: str, page: int):
    name, un = user_label(x)
    sites = sorted(redis.smembers(f"user:{x}:sites") or [])
    banned = is_banned(int(x))
    prem = is_premium(int(x))
    text = (
        f"👤 <b>{H.escape(name)}</b>\n"
        f"🆔 <code>{x}</code>\n"
        + (f"🔗 @{H.escape(un)}\n" if un else "")
        + f"💰 Wallet: <b>₹{get_wallet(int(x))}</b>\n"
        + f"💎 Premium: <b>{'Haan ✅' if prem else 'Nahi'}</b>\n"
        + f"🌐 Sites: <b>{len(sites)}</b>\n"
        + f"🚦 Status: <b>{'Banned 🚫' if banned else 'Active ✅'}</b>\n"
    )
    if sites:
        text += "\n" + "\n".join(f'• <a href="{site_url(n)}">{H.escape(n)}</a>' for n in sites)
    ban_btn = (Btn("✅ Unban", callback_data=f"unban:{x}:{page}") if banned
               else Btn("🚫 Ban", callback_data=f"ban:{x}:{page}"))
    prem_btn = (Btn("💎 Premium Hatao", callback_data=f"prem:{x}:{page}:0") if prem
                else Btn("💎 Premium Do", callback_data=f"prem:{x}:{page}:1"))
    await edit(q, text, Markup([[prem_btn, ban_btn], [Btn("⬅️ Back", callback_data=f"users:{page}")]]))


async def show_channels(q):
    chans = sorted(redis.smembers("channels") or [])
    rows = []
    for c in chans:
        title, _ = chan_info(c)
        rows.append([Btn(f"❌ {title[:28]}", callback_data=f"chdel:{c}")])
    rows.append([Btn("➕ Channel Add Karo", callback_data="addch")])
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    text = (
        "📡 <b>Force-Join Channels</b>\n\n"
        "Users ko bot use karne se pehle ye channels join karne honge.\n"
        "Hatane ke liye channel par tap karo.\n\n"
        + (f"Total: <b>{len(chans)}</b>" if chans else "Abhi koi channel add nahi hai.")
    )
    await edit(q, text, Markup(rows))


def upi_markup() -> Markup:
    return Markup([
        [Btn("✏️ UPI ID Set Karo", callback_data="upiid")],
        [Btn("✏️ Naam Set Karo", callback_data="upiname")],
        [Btn("🛡 Admin Panel", callback_data="admin")],
    ])


async def show_upi(q):
    upi_id, name = get_upi()
    await edit(
        q,
        "🏦 <b>UPI Settings</b>\n\n"
        f"UPI ID: <code>{H.escape(upi_id) or 'set nahi hai'}</code>\n"
        f"Naam: <b>{H.escape(name) or 'set nahi hai'}</b>\n\n"
        "Ye UPI ID premium QR code mein jaayegi.",
        upi_markup(),
    )


def pemoji_markup(m: dict) -> Markup:
    rows = [[Btn(f"🗑 {k}", callback_data=f"pedel:{k}")] for k in list(m)[:20]]
    rows.append([Btn("➕ Premium Emoji Add Karo", callback_data="pemadd")])
    if m:
        rows.append([Btn("🧹 Sab Clear Karo", callback_data="pemclr")])
    rows.append([Btn("🛡 Admin Panel", callback_data="admin")])
    return Markup(rows)


async def show_pemoji(q):
    m = emoji_map()
    lines = "\n".join(f"{k} → <code>{v}</code>" for k, v in m.items())
    text = (
        "⭐ <b>Premium Emoji</b>\n\n"
        "Admin premium emoji wala message bhejega, bot wo emoji save karega.\n"
        "Phir bot ke saare messages mein wahi premium emoji dikhenge.\n\n"
        + (lines if lines else "Abhi koi premium emoji set nahi hai.")
        + "\n\n<i>Note: buttons ke text mein premium emoji Telegram allow nahi karta.</i>"
    )
    await edit(q, text, pemoji_markup(m))


# ---------------- COMMANDS ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    ctx.user_data.clear()
    user = update.effective_user
    await send(update.effective_message, welcome_text(user), main_menu(user.id))


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await send(msg, "Use: <code>/ban user_id</code>")
    redis.sadd("banned", ctx.args[0])
    await send(msg, f"🚫 {ctx.args[0]} ban ho gaya.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await send(msg, "Use: <code>/unban user_id</code>")
    redis.srem("banned", ctx.args[0])
    await send(msg, f"✅ {ctx.args[0]} unban ho gaya.")


async def cmd_delsite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        return await send(msg, "Use: <code>/delsite name</code>")
    name = ctx.args[0].lower()
    if not redis.exists(f"site:{name}"):
        return await send(msg, "❌ Aisi site nahi mili.")
    delete_site(name)
    await send(msg, f"🗑 <code>{H.escape(name)}</code> delete ho gayi.")


async def _premium_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, on: bool):
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await send(msg, f"Use: <code>/{'premium' if on else 'unpremium'} user_id</code>")
    uid = int(ctx.args[0])
    set_premium(uid, on)
    await send(msg, f"💎 {uid} ab {'Premium hai ✅' if on else 'Premium nahi hai'}.")
    await notify_user_decision(ctx.bot, uid, on)


async def cmd_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _premium_cmd(update, ctx, True)


async def cmd_unpremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _premium_cmd(update, ctx, False)


# ---------------- BUTTONS ----------------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    q = update.callback_query
    user = q.from_user
    uid = user.id
    data = q.data or ""
    await q.answer()
    admin = is_admin(uid)

    # ----- USER -----
    if data in ("home", "chk"):
        ctx.user_data.clear()
        return await edit(q, welcome_text(user), main_menu(uid))

    if data == "help":
        return await edit(q, HELP, home_btn())

    if data == "claim":
        ok, balance = try_claim(uid)
        if ok:
            text = (f"🎁 <b>+₹{DAILY_CLAIM} mil gaye!</b>\n\n"
                    f"💰 Wallet: <b>₹{balance}</b>\n\nKal phir aana 😉")
        else:
            text = (f"⏳ <b>Aaj ka claim ho chuka hai.</b>\n\n"
                    f"💰 Wallet: <b>₹{balance}</b>\n\nKal phir aana 😉")
        return await edit(q, text, premium_markup())

    if data == "buy":
        return await start_buy(q.message, user, ctx.bot)

    if data == "upload":
        if not is_premium(uid):
            return await edit(q, premium_text(), premium_markup())
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        return await edit(
            q, "📤 <b>Ab apni .html file bhejo</b>\n\n(File ko document ki tarah attach karke bhejo)",
            Markup([[Btn("❌ Cancel", callback_data="home")]]),
        )

    if data == "mysites":
        names = sorted(redis.smembers(f"user:{uid}:sites") or [])
        if not names:
            return await edit(
                q, "📂 Tumhari koi site nahi hai abhi.",
                Markup([[Btn("📤 Upload Karo", callback_data="upload")], [Btn("🏠 Home", callback_data="home")]]),
            )
        rows = [[Btn(f"🌐 {n}", callback_data=f"site:{n}")] for n in names]
        rows.append([Btn("🏠 Home", callback_data="home")])
        return await edit(q, f"📂 <b>Tumhari Sites ({len(names)})</b>\n\nKisi par tap karo:", Markup(rows))

    if data.startswith("site:"):
        name = data[5:]
        if not can_manage(uid, name):
            return await edit(q, "❌ Ye tumhari site nahi hai.", home_btn())
        return await edit(
            q, f"🌐 <b>{H.escape(name)}</b>\n\n🔗 {site_url(name)}",
            Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("♻️ Update", callback_data=f"upd:{name}"), Btn("🗑 Delete", callback_data=f"del:{name}")],
                [Btn("⬅️ Back", callback_data="mysites")],
            ]),
        )

    if data.startswith("upd:"):
        name = data[4:]
        if not can_manage(uid, name):
            return await edit(q, "❌ Ye tumhari site nahi hai.", home_btn())
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        ctx.user_data["update_name"] = name
        return await edit(
            q, f"♻️ <b>{H.escape(name)}</b> update karne ke liye nayi .html file bhejo.",
            Markup([[Btn("❌ Cancel", callback_data=f"site:{name}")]]),
        )

    if data.startswith("del:"):
        name = data[4:]
        return await edit(
            q, f"⚠️ Pakka <b>{H.escape(name)}</b> delete karni hai?",
            Markup([[Btn("✅ Haan, Delete", callback_data=f"delok:{name}"),
                     Btn("❌ Nahi", callback_data=f"site:{name}")]]),
        )

    if data.startswith("delok:"):
        name = data[6:]
        if not can_manage(uid, name):
            return await edit(q, "❌ Ye tumhari site nahi hai.", home_btn())
        delete_site(name)
        return await edit(
            q, f"🗑 <b>{H.escape(name)}</b> delete ho gayi.",
            Markup([[Btn("📂 Meri Sites", callback_data="mysites")], [Btn("🏠 Home", callback_data="home")]]),
        )

    if data == "noop":
        return

    # ----- ADMIN -----
    if not admin:
        return

    if data == "admin":
        return await show_admin(q)

    if data.startswith("users:"):
        return await show_users(q, int(data.split(":")[1]))

    if data.startswith("u:"):
        _, x, pg = data.split(":")
        return await show_user(q, x, int(pg))

    if data.startswith(("ban:", "unban:")):
        act, x, pg = data.split(":")
        if act == "ban":
            redis.sadd("banned", x)
        else:
            redis.srem("banned", x)
        return await show_user(q, x, int(pg))

    if data.startswith("prem:"):
        _, x, pg, on = data.split(":")
        set_premium(int(x), on == "1")
        await show_user(q, x, int(pg))
        return await notify_user_decision(ctx.bot, int(x), on == "1")

    if data == "payments":
        return await show_payments(q)

    if data.startswith("pay:"):
        pid = data[4:]
        d = pay_get(pid)
        if not d:
            return await edit(q, "❌ Ye payment nahi mili.", admin_markup())
        markup = pay_buttons(pid) if d.get("status") == "submitted" else None
        chat_id = q.message.chat_id
        if d.get("proof_type") == "photo":
            await ctx.bot.send_photo(
                chat_id, d["proof"], caption=pe(pay_caption(pid, d)),
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
        else:
            await notify(ctx.bot, chat_id, pay_caption(pid, d), markup)
        return

    if data.startswith(("pa:", "pr:")):
        approve = data.startswith("pa:")
        pid = data[3:]
        result = await decide_payment(pid, approve, user.full_name or str(uid))
        if result is None:
            return await q.message.reply_text("⚠️ Ye payment pehle hi process ho chuki hai.")
        buyer, status = result
        await notify_user_decision(ctx.bot, buyer, approve)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            log.debug("markup clear skipped: %s", e)
        label = "✅ APPROVED" if approve else "❌ REJECTED"
        return await notify(ctx.bot, q.message.chat_id, f"{label} — Payment #{pid} (user {buyer}) by {H.escape(user.full_name or str(uid))}")

    if data == "upi":
        return await show_upi(q)

    if data == "upiid":
        ctx.user_data.clear()
        ctx.user_data["state"] = "upi_id"
        return await edit(
            q, "🏦 Ab <b>UPI ID</b> bhejo.\nExample: <code>name@okaxis</code>",
            Markup([[Btn("❌ Cancel", callback_data="upi")]]),
        )

    if data == "upiname":
        ctx.user_data.clear()
        ctx.user_data["state"] = "upi_name"
        return await edit(
            q, "🏦 Ab <b>account naam</b> bhejo (QR par ye naam dikhega).",
            Markup([[Btn("❌ Cancel", callback_data="upi")]]),
        )

    if data == "pemoji":
        return await show_pemoji(q)

    if data == "pemadd":
        ctx.user_data.clear()
        ctx.user_data["state"] = "add_pemoji"
        return await edit(
            q,
            "⭐ <b>Premium emoji add karo</b>\n\n"
            "Ab wo message bhejo jisme premium emoji ho (ek ya zyada).\n"
            "Bot har premium emoji save karega. Kai baar bhej sakte ho.",
            Markup([[Btn("✅ Done", callback_data="pemoji")]]),
        )

    if data.startswith("pedel:"):
        key = data[6:]
        m = dict(emoji_map())
        m.pop(key, None)
        save_emoji_map(m)
        return await show_pemoji(q)

    if data == "pemclr":
        save_emoji_map({})
        return await show_pemoji(q)

    if data == "channels":
        return await show_channels(q)

    if data == "addch":
        ctx.user_data.clear()
        ctx.user_data["state"] = "add_channel"
        return await edit(
            q,
            "📡 <b>Channel add karo</b>\n\n"
            "1️⃣ Pehle bot ko us channel mein <b>Admin</b> banao\n"
            "2️⃣ Phir channel ka <code>@username</code> ya ID bhejo,\n"
            "    ya channel ka koi message yahan <b>forward</b> karo.",
            Markup([[Btn("❌ Cancel", callback_data="channels")]]),
        )

    if data.startswith("chdel:"):
        cid = data[6:]
        redis.srem("channels", cid)
        redis.delete(f"chan:{cid}")
        return await show_channels(q)

    if data == "broadcast":
        ctx.user_data.clear()
        ctx.user_data["state"] = "broadcast"
        return await edit(
            q, "📢 Jo message sabko bhejna hai wo ab likh kar bhejo (premium emoji bhi chalenge).",
            Markup([[Btn("❌ Cancel", callback_data="admin")]]),
        )


# ---------------- FILE RECEIVE ----------------
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    msg = update.effective_message
    uid = update.effective_user.id
    doc = msg.document
    fname = (doc.file_name or "").lower()

    if not fname.endswith((".html", ".htm")):
        return await send(msg, "❌ Sirf <code>.html</code> file bhejo.")
    if doc.file_size and doc.file_size > MAX_BYTES:
        return await send(msg, f"❌ File bahut badi hai. Max {MAX_BYTES // (1024 * 1024)} MB.")

    update_name = ctx.user_data.get("update_name") if ctx.user_data.get("state") == "await_file" else None
    if update_name:
        if not can_manage(uid, update_name):
            ctx.user_data.clear()
            return await send(msg, "❌ Ye tumhari site nahi hai.", home_btn())
    elif not is_premium(uid):
        return await send(msg, premium_text(), premium_markup())

    tg_file = await doc.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")

    if update_name:
        redis.set(f"site:{update_name}", content)
        ctx.user_data.clear()
        return await send(
            msg,
            f"✅ <b>{H.escape(update_name)}</b> update ho gayi!\n\n🔗 {site_url(update_name)}",
            Markup([[Btn("🔗 Open Site", url=site_url(update_name))], [Btn("🏠 Home", callback_data="home")]]),
        )

    ctx.user_data["html"] = content
    ctx.user_data["state"] = "await_name"
    await send(
        msg,
        "✅ File mil gayi!\n\n✍️ Ab <b>site ka naam</b> bhejo.\n"
        "Rules: 3-30 characters, sirf <code>a-z 0-9 -</code>\n"
        "Example: <code>my-portfolio</code>",
        Markup([[Btn("❌ Cancel", callback_data="home")]]),
    )


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    msg = update.effective_message
    user = update.effective_user
    if redis.get(f"ppend:{user.id}"):
        file_id = msg.photo[-1].file_id
        if await submit_proof(msg, user, ctx, "photo", file_id):
            return
    await send(msg, "👇 Menu se option chuno", main_menu(user.id))


# ---------------- TEXT ----------------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    msg = update.effective_message
    user = update.effective_user
    uid = user.id
    text = (msg.text or "").strip()
    state = ctx.user_data.get("state")
    admin = is_admin(uid)

    # ----- ADMIN STATES -----
    if admin and state == "add_channel":
        ref = None
        fo = getattr(msg, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            ref = fo.chat.id
        elif text.startswith("@"):
            ref = text
        elif text.lstrip("-").isdigit():
            ref = int(text)
        if ref is None:
            return await send(msg, "❌ @username, ID ya forwarded message bhejo.")
        try:
            chat = await ctx.bot.get_chat(ref)
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                raise ValueError("bot admin nahi hai")
        except Exception as e:
            log.warning("add channel failed: %s", e)
            return await send(
                msg, "❌ Channel nahi mila ya bot us channel mein admin nahi hai.\n"
                     "Bot ko admin banao aur dobara bhejo.")
        link = f"https://t.me/{chat.username}" if chat.username else ""
        if not link:
            try:
                link = chat.invite_link or await ctx.bot.export_chat_invite_link(chat.id)
            except Exception:
                link = ""
        title = (chat.title or str(chat.id)).replace("\t", " ")
        redis.sadd("channels", str(chat.id))
        redis.set(f"chan:{chat.id}", f"{title}\t{link}")
        ctx.user_data.clear()
        warn = "" if link else ("\n\n⚠️ Invite link nahi mila. Bot ko 'Invite users' permission do, "
                                "warna users ko join button nahi dikhega.")
        return await send(
            msg, f"✅ <b>{H.escape(title)}</b> add ho gaya!{warn}",
            Markup([[Btn("📡 Channels", callback_data="channels")], [Btn("🛡 Admin Panel", callback_data="admin")]]),
        )

    if admin and state == "broadcast":
        body = pe(msg.text_html or "")
        users = list(redis.smembers("users") or [])
        ok = fail = 0
        await send(msg, f"📢 Bhej raha hu... ({len(users)} users)")
        for u in users:
            try:
                await ctx.bot.send_message(
                    int(u), body, parse_mode=ParseMode.HTML, link_preview_options=NOPREVIEW,
                )
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)  # Telegram rate limit se bachne ke liye
        ctx.user_data.clear()
        return await send(msg, f"✅ Done\nSent: {ok}\nFailed: {fail}", home_btn())

    if admin and state == "upi_id":
        if not UPI_RE.match(text):
            return await send(msg, "❌ Galat UPI ID. Example: <code>name@okaxis</code>")
        redis.set("cfg:upi_id", text)
        ctx.user_data.clear()
        return await send(msg, f"✅ UPI ID save ho gaya: <code>{H.escape(text)}</code>", upi_markup())

    if admin and state == "upi_name":
        if not text or len(text) > 60:
            return await send(msg, "❌ Naam 1-60 characters ka hona chahiye.")
        redis.set("cfg:upi_name", text)
        ctx.user_data.clear()
        return await send(msg, f"✅ Naam save ho gaya: <b>{H.escape(text)}</b>", upi_markup())

    if admin and state == "add_pemoji":
        ents = [e for e in (msg.entities or []) if e.type == MessageEntityType.CUSTOM_EMOJI]
        if not ents:
            return await send(msg, "❌ Is message mein premium emoji nahi mila. Premium emoji wala message bhejo.")
        mapping = dict(emoji_map())
        added = []
        for e in ents:
            fallback = msg.parse_entity(e)
            cid = str(e.custom_emoji_id or "")
            if fallback and fallback.strip() and cid.isdigit():
                mapping[fallback] = cid
                added.append(fallback)
        if not added:
            return await send(msg, "❌ Premium emoji ki ID nahi mili, dobara bhejo.")
        save_emoji_map(mapping)
        return await send(
            msg, f"✅ {len(added)} premium emoji save ho gaye: {''.join(added)}\n"
                 "Aur bhejo ya ✅ Done dabao.",
            Markup([[Btn("✅ Done", callback_data="pemoji")]]),
        )

    # ----- USER STATES -----
    if state == "await_name":
        name = text.lower()
        if not is_premium(uid):
            ctx.user_data.clear()
            return await send(msg, premium_text(), premium_markup())
        if not NAME_RE.match(name):
            return await send(
                msg, "❌ Galat naam. 3-30 characters, sirf <code>a-z 0-9 -</code>, shuru letter/number se.")
        if name in RESERVED:
            return await send(msg, "❌ Ye naam reserved hai, koi aur naam do.")
        if redis.exists(f"site:{name}"):
            return await send(msg, "❌ Ye naam pehle se le liya gaya hai. Koi aur naam do.")
        html_content = ctx.user_data.get("html")
        if not html_content:
            ctx.user_data.clear()
            return await send(msg, "⚠️ File nahi mili, dobara upload karo.", main_menu(uid))

        redis.set(f"site:{name}", html_content)
        redis.set(f"owner:{name}", str(uid))
        redis.sadd(f"user:{uid}:sites", name)
        ctx.user_data.clear()
        return await send(
            msg,
            f"🎉 <b>Site live ho gayi!</b>\n\n🌐 Naam: <code>{name}</code>\n🔗 {site_url(name)}",
            Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("📂 Meri Sites", callback_data="mysites"), Btn("🏠 Home", callback_data="home")],
            ]),
        )

    # ----- PAYMENT PROOF (UTR text) -----
    if redis.get(f"ppend:{uid}") and 4 <= len(text) <= 40:
        if await submit_proof(msg, user, ctx, "text", text):
            return

    await send(msg, "👇 Menu se option chuno", main_menu(uid))


# ---------------- ERRORS ----------------
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error: %s", ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                pe("⚠️ Kuch gadbad ho gayi, thodi der baad dobara try karo."),
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        pass


# ---------------- RUN ----------------
def main():
    threading.Thread(target=run_web, daemon=True).start()
    # Python 3.14 / naye versions mein event loop khud banana padta hai
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("delsite", cmd_delsite))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("unpremium", cmd_unpremium))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
