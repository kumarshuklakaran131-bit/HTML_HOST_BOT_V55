# 🌐 Telegram HTML Web Hosting Bot (UPI Premium)

Telegram bot jisme user `.html` file bhejta hai, site ka naam deta hai, aur site live ho jaati hai.
Free upload nahi hai. **Premium (₹50, UPI)** lene ke baad admin approve karta hai, phir **unlimited sites**.

**Stack:** Python, python-telegram-bot, Flask, Upstash Redis, segno (UPI QR), Render

---

## ✨ Features
- 💎 **Premium ₹50** — UPI QR auto-generate, user screenshot / 12-digit UTR bhejta hai, admin **Approve / Reject** karta hai
- 🚫 Free user ke liye **0 sites** (upload ke liye premium zaroori)
- ♾ Premium user ke liye **unlimited sites**
- 🎁 **Daily Claim** — roz ₹3 wallet mein (IST din ke hisaab se, din mein ek baar)
- 🏦 Admin Panel se **UPI ID / naam** set karo (ENV mein bhi de sakte ho)
- ⭐ **Premium emoji** — admin premium emoji wala message bhejta hai, bot wo save karke sabhi messages mein use karta hai
- 🔗 Instant link: `https://yourapp.onrender.com/sitename`
- 📂 My Sites (Open / Update / Delete), Naam validation, duplicate block
- 🛡 Admin Panel: stats, users, premium on/off, ban/unban, payments, channels, broadcast, delete any site
- 📡 Force-join channels

---

## 📁 Files
| File | Kaam |
|---|---|
| `main.py` | Poora bot + web server |
| `requirements.txt` | Python libraries |
| `.env.example` | ENV variables ka sample |
| `.gitignore` | `.env` ko GitHub se bachata hai |
| `.python-version` | Render ko Python 3.12.8 use karne ke liye batata hai |

---

## 🔑 Environment Variables

| Key | Value |
|---|---|
| `BOT_TOKEN` | @BotFather se mila token |
| `UPSTASH_REDIS_REST_URL` | Upstash REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash REST Token |
| `BASE_URL` | Render ka link (bina `/` ke) |
| `ADMIN_ID` | Admin ka Telegram ID (kai ho to comma se) |
| `DEV_NAME` | Developer ka naam |
| `DEV_USERNAME` | Telegram username (bina `@` ke) |
| `PYTHON_VERSION` | `3.12.8` (Render par zaroori) |
| `MAX_FILE_MB` | Max file size MB mein (default `1`) |
| `PREMIUM_PRICE` | Premium ki price ₹ mein (default `50`) |
| `DAILY_CLAIM` | Daily claim ₹ (default `3`) |
| `UPI_ID` | (optional) UPI ID — Admin Panel se bhi set ho sakti hai |
| `UPI_NAME` | (optional) QR par dikhne wala naam |

---

## 🚀 Setup (Step by Step)

1. **Bot token:** @BotFather → `/newbot` → token copy karo. Apna ID @userinfobot se lo.
2. **Upstash Redis:** https://upstash.com → Create Database → **REST API** se URL aur TOKEN copy karo.
3. **GitHub:** Naya repo banao, is folder ki files upload karo. `.env` kabhi upload mat karna.
4. **Render:** New → Web Service → repo connect karo
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
   - Plan: Free, upar wale ENV variables daalo, `PYTHON_VERSION=3.12.8`
   - Deploy ke baad link `BASE_URL` mein daalo aur **Manual Deploy** karo
5. **24/7:** Uptime monitor mein `https://yourapp.onrender.com/health` add karo (har 5 min).

---

## 🏦 Admin: UPI aur Premium setup
1. Bot mein `/start` → **🛡 Admin Panel** → **🏦 UPI Settings** → UPI ID aur naam set karo.
2. User **💎 Premium Lo** dabata hai → bot QR bhejta hai (UPI ID + ₹50 pre-filled).
3. User payment ka screenshot ya UTR bhejta hai → admin ko notification aata hai.
4. **✅ Approve** dabao → user premium ho jaata hai (unlimited sites). **❌ Reject** se payment reject.

Premium manually dene/hatane ke liye: `/premium <user_id>` aur `/unpremium <user_id>` (ya Users list se).

## ⭐ Premium emoji setup
1. Admin Panel → **⭐ Premium Emoji** → **➕ Premium Emoji Add Karo**
2. Premium emoji wala message bhejo (kai baar bhej sakte ho)
3. **✅ Done** dabao. Ab bot ke messages mein wo emoji premium style mein dikhenge.

Note: Telegram inline buttons ke text mein premium emoji allow nahi karta. Jin users ke paas Telegram Premium nahi hai, unhe fallback (normal) emoji dikhega.

---

## 🤖 Bot Commands

**Users:** `/start` (baaki sab buttons se)

**Admin only:**
```
/premium <user_id>
/unpremium <user_id>
/ban <user_id>
/unban <user_id>
/delsite <name>
```

---

## 🗄 Redis Data Structure
```
site:<name>        -> HTML content
owner:<name>       -> owner user id
user:<id>:sites    -> user ki sites ka set
users              -> saare users ka set
uinfo:<id>         -> user ka naam aur username
premium            -> premium users ka set
wallet:<id>        -> daily claim balance (₹)
claim:<id>         -> last daily claim date (IST)
pay:<id>           -> payment record (json)
pay:pending        -> approval ke intezaar mein payments
pemoji             -> premium emoji map (json)
cfg:upi_id / cfg:upi_name -> UPI settings
channels / chan:<id> -> force-join channels
banned             -> banned users ka set
```

---

## 💻 Local par chalana
```bash
pip install -r requirements.txt
cp .env.example .env     # values bharo
export $(grep -v '^#' .env | xargs)
python main.py
```

---

## ⚠️ Dhyan Rakho
- Saari sites ek hi domain par chalti hain. Malicious/phishing HTML ho sakti hai, isliye `/delsite` aur `/ban` use karte raho.
- Payment ko UPI app mein apne aap verify nahi kiya jaata, admin screenshot / UTR dekh kar hi approve kare.
- Render Free par service 15 min inactive rehne par sleep ho jaati hai, uptime monitor zaroor lagao.
- Bot token aur Redis token kisi ke saath share mat karo.

---

Developed by **Aditya**
