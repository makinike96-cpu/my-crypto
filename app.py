# v5.2 — auto news + auto signals + real CoinGecko prices (top-20 only)
import os, io, time, threading, datetime as dt
from flask import Flask, request
import requests, xml.etree.ElementTree as ET
import telebot

# ========= ENV / CONFIG =========
TOKEN    = os.getenv("TOKEN")                   # Telegram bot token (Render → Environment)
LUNAR    = os.getenv("LUNARCRUSH_KEY")          # LunarCrush Bearer (Render → Environment)
PORT     = int(os.environ.get("PORT", 10000))

TRADING_CHAT = -1003166387118   # Аналитика
NEWS_CHAT    = -1002969047835   # Новости

MAX_NEWS_PER_DAY    = 7
MAX_SIGNALS_PER_DAY = 2
DEFAULT_LEVERAGE    = 2
RISK_PER_TRADE      = 0.05

HEADERS = {"User-Agent": "Mozilla/5.0"}
CG_BASE = "https://api.coingecko.com/api/v3"
RSS_FEEDS = [
    "https://www.binance.com/en/blog/rss",
    "https://cointelegraph.com/rss",
]
INFLUENCER_HINTS = [
    "elon", "musk", "trump", "saylor", "buterin", "cz", "gensler",
    "sec", "etf", "listing", "blackrock", "fidelity", "coinbase", "binance",
    "airdrop", "upgrade", "hack"
]

VERSION = "v5.2-CG"

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
state = {"date": dt.date.today(), "news": 0, "signals": 0, "top20": []}

# ========= HELPERS =========
def reset_counters_if_new_day():
    today = dt.date.today()
    if state["date"] != today:
        state["date"] = today
        state["news"] = 0
        state["signals"] = 0

def jget(url, params=None, headers=None, timeout=20):
    try:
        r = requests.get(url, params=params or {}, headers=headers or HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

def tget(url, timeout=20):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""

def top20_symbols():
    if state["top20"]:
        return state["top20"]
    data = jget(f"{CG_BASE}/coins/markets",
                {"vs_currency":"usd","order":"market_cap_desc","per_page":20,"page":1})
    syms = []
    for d in data or []:
        s = (d.get("symbol") or "").upper()
        if s: syms.append(s)
    # fallback на случай недоступности API
    state["top20"] = syms or ["BTC","ETH","SOL","BNB","XRP","ADA","DOGE","TON","TRX","DOT",
                              "AVAX","LINK","UNI","XLM","ICP","LTC","ATOM","NEAR","APT"]
    return state["top20"]

FAST_MAP = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin","XRP":"ripple",
    "ADA":"cardano","DOGE":"dogecoin","TON":"the-open-network","TRX":"tron","DOT":"polkadot",
    "AVAX":"avalanche-2","LINK":"chainlink","UNI":"uniswap","XLM":"stellar","ICP":"internet-computer",
    "LTC":"litecoin","ATOM":"cosmos","NEAR":"near","APT":"aptos","ETC":"ethereum-classic"
}
def cg_id(symbol):
    s = symbol.upper()
    if s in FAST_MAP: return FAST_MAP[s]
    # запасной путь — поиск по списку монет
    lst = jget(f"{CG_BASE}/coins/list")
    for it in lst or []:
        if (it.get("symbol") or "").upper() == s:
            return it.get("id")
    return "bitcoin"

def price_usd(sym):
    _id = cg_id(sym)
    data = jget(f"{CG_BASE}/simple/price", {"ids": _id, "vs_currencies":"usd"})
    return (data.get(_id) or {}).get("usd")

def market_chart(sym, days=2):
    _id = cg_id(sym)
    data = jget(f"{CG_BASE}/coins/{_id}/market_chart", {"vs_currency":"usd","days":days})
    return data.get("prices") or []

def ma(values, window):
    if len(values) < window: return []
    out, s = [], sum(values[:window])
    out.append(s/window)
    for i in range(window, len(values)):
        s += values[i] - values[i-window]
        out.append(s/window)
    return out

# ========= PICK SYMBOL (LunarCrush if available, else BTC) =========
def pick_symbol():
    try:
        if LUNAR:
            syms = ",".join(top20_symbols())
            j = jget("https://api.lunarcrush.com/v2",
                     {"data":"assets","symbol":syms},
                     headers={"Authorization": f"Bearer {LUNAR}"})
            best, gmax = None, -1
            for row in (j.get("data") or []):
                g = row.get("galaxy_score") or 0
                if g > gmax:
                    gmax, best = g, row.get("symbol")
            if best: return best
    except Exception:
        pass
    return "BTC"

# ========= SIGNALS =========
def chart_image(sym, entry, tp, sl):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import datetime as pdt
    data = market_chart(sym, days=2)
    if not data: return None
    xs = [pdt.datetime.utcfromtimestamp(p[0]/1000.0) for p in data]
    ys = [p[1] for p in data]
    fig = plt.figure(figsize=(7,3.4), dpi=160)
    ax = plt.gca()
    ax.plot(xs, ys, linewidth=1.8, label=f"{sym} • ~48ч")
    ax.axhline(entry, linestyle="--", linewidth=1.2, label=f"Вход {entry:,.2f}")
    ax.axhline(tp,    linestyle="--", linewidth=1.2, label=f"Тейк {tp:,.2f}")
    ax.axhline(sl,    linestyle="--", linewidth=1.2, label=f"Стоп {sl:,.2f}")
    ax.legend(loc="upper left"); ax.set_ylabel("USD"); ax.grid(True, alpha=0.25)
    buf = io.BytesIO(); fig.tight_layout(); plt.savefig(buf, format="png"); plt.close(fig); buf.seek(0)
    return buf

def build_signal(sym="BTC", equity=1000.0):
    p = price_usd(sym)
    if p is None: return None
    hist = market_chart(sym, days=2)
    direction = "LONG"
    if len(hist) >= 30:
        ys = [v[1] for v in hist]
        fast, slow = ma(ys, 12), ma(ys, 26)
        try:
            direction = "LONG" if fast[-1] >= slow[-1] else "SHORT"
        except:
            direction = "LONG"
    entry = float(p)
    if direction == "LONG":
        tp, sl = entry*1.025, entry*0.985
    else:
        tp, sl = entry*0.975, entry*1.015

    stop_dist = abs(entry - sl)
    qty = (equity * RISK_PER_TRADE) / stop_dist
    notional, cap = qty*entry, equity*DEFAULT_LEVERAGE
    if notional > cap:
        qty *= cap/notional

    img = chart_image(sym, entry, tp, sl)
    text = (
        f"📊 Сигнал по {sym}\n\n"
        f"🎯 Вход: {entry:,.2f} $\n"
        f"💰 Тейк: {tp:,.2f} $\n"
        f"🛑 Стоп: {sl:,.2f} $\n\n"
        f"⚖ Плечо: x{DEFAULT_LEVERAGE}\n"
        f"💵 Риск: {int(RISK_PER_TRADE*100)}% от депозита\n"
        f"Размер позиции ≈ {qty:,.4f} {sym}\n"
        f"Направление: {'🟢 LONG' if direction=='LONG' else '🔴 SHORT'}\n"
        f"#signal #{sym.lower()} #crypto"
    )
    return text, img

def post_signal_once():
    reset_counters_if_new_day()
    if state["signals"] >= MAX_SIGNALS_PER_DAY: return
    try:
        sym = pick_symbol()
        payload = build_signal(sym, equity=1000.0)
        if not payload: return
        text, img = payload
        if img: bot.send_photo(TRADING_CHAT, img, caption=text)
        else:   bot.send_message(TRADING_CHAT, text)
        state["signals"] += 1
        print(f"[signal] posted {sym}")
    except Exception as e:
        bot.send_message(TRADING_CHAT, f"⚠️ Ошибка сигнала: {e}")

# ========= NEWS =========
def parse_rss(url, limit=10):
    out = []
    xml = tget(url)
    if not xml: return out
    try:
        root = ET.fromstring(xml)
        for item in root.iterfind(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            out.append((title, link))
    except Exception:
        pass
    return out

def lunar_spikes():
    if not LUNAR: return []
    syms = ",".join(top20_symbols())
    j = jget("https://api.lunarcrush.com/v2",
             {"data":"assets","symbol":syms},
             headers={"Authorization": f"Bearer {LUNAR}"})
    out = []
    for row in (j.get("data") or []):
        sym = row.get("symbol"); g = row.get("galaxy_score") or 0; vol = row.get("social_volume") or 0
        if g >= 60 or vol >= 500:
            out.append((f"🔥 Соц-всплеск по {sym}: GalaxyScore {g}, обсуждения {vol}.", None, sym))
    return out

def select_news_messages():
    reset_counters_if_new_day()
    if state["news"] >= MAX_NEWS_PER_DAY: return []
    candidates = []
    for feed in RSS_FEEDS:
        candidates += parse_rss(feed, limit=8)
    spikes = lunar_spikes()

    selected = []
    t20 = set(s.lower() for s in top20_symbols())
    for (title, link) in candidates:
        t = (title or "").lower()
        by_symbol = any(f" {s} " in f" {t} " for s in t20)
        influencer = any(k in t for k in INFLUENCER_HINTS)
        important = any(k in t for k in ["bitcoin","btc","ethereum","eth","binance","coinbase","sec","etf","listing","spot","hack","airdrop","upgrade"])
        if important or by_symbol or influencer:
            msg = f"📰 {title}"
            if link: msg += f"\nПодробнее: {link}"
            msg += "\n#CryptoNews #Market"
            selected.append(msg)
        if len(selected) >= MAX_NEWS_PER_DAY: break

    for (txt, link, sym) in spikes:
        if len(selected) >= MAX_NEWS_PER_DAY: break
        msg = f"📰 {txt}"
        if link: msg += f"\nПодробнее: {link}"
        if sym:  msg += f"\n#{sym.lower()} #Social"
        msg += "\n#CryptoNews #Market"
        selected.append(msg)
    return selected

def post_news_once():
    reset_counters_if_new_day()
    if state["news"] >= MAX_NEWS_PER_DAY: return
    try:
        msgs = select_news_messages()
        for m in msgs:
            if state["news"] >= MAX_NEWS_PER_DAY: break
            bot.send_message(NEWS_CHAT, m)
            state["news"] += 1
            print("[news] posted")
            time.sleep(2)
    except Exception as e:
        bot.send_message(NEWS_CHAT, f"⚠️ Ошибка новостей: {e}")

# ========= SCHEDULER (без внешних библиотек) =========
def scheduler_loop():
    last_news, last_sig = 0, 0
    while True:
        now = time.time()
        if now - last_news >= 30*60:      # новости ~каждые 30 минут (до лимита)
            post_news_once(); last_news = now
        if now - last_sig >= 4*60*60:     # сигналы ~каждые 4 часа (до лимита)
            post_signal_once(); last_sig = now
        time.sleep(15)

# ========= WEBHOOK =========
WEBHOOK_PATH = f"/{TOKEN}"

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    js = request.stream.read().decode("utf-8")
    upd = telebot.types.Update.de_json(js)
    bot.process_new_updates([upd])
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "OK", 200

# ========= COMMANDS =========
@bot.message_handler(commands=["start"])
def start_cmd(m):
    bot.send_message(m.chat.id, "Привет! Я публикую новости (до 7/день) и сигналы (до 2/день). Всё на русском. Это не финсовет.")

@bot.message_handler(commands=["price"])
def price_cmd(m):
    try:
        parts = m.text.split()
        sym = parts[1].upper() if len(parts) > 1 else "BTC"
        p = price_usd(sym)
        if p is None:
            bot.send_message(m.chat.id, f"Не смог получить цену для {sym}")
            return
        bot.send_message(m.chat.id, f"{sym}: ${p:,.6f}")
    except Exception as e:
        bot.send_message(m.chat.id, f"Ошибка /price: {e}")

@bot.message_handler(commands=["btc"])
def btc_cmd(m):
    p = price_usd("BTC")
    if p is None: bot.send_message(m.chat.id, "⚠️ Не удалось получить цену BTC"); return
    bot.send_message(m.chat.id, f"💰 BTC сейчас: ${p:,.2f}")

@bot.message_handler(commands=["signal"])
def manual_signal(m):
    post_signal_once()
    bot.send_message(m.chat.id, "✅ Сигнал отправлен (если дневной лимит не исчерпан).")

@bot.message_handler(commands=["news"])
def manual_news(m):
    post_news_once()
    bot.send_message(m.chat.id, "✅ Новости отправлены (если дневной лимит не исчерпан).")

@bot.message_handler(commands=["version"])
def version_cmd(m):
    lunar = "ON" if LUNAR else "OFF"
    bot.send_message(m.chat.id, f"Version: {VERSION}\nPrices: CoinGecko\nLunarCrush: {lunar}\nWebhooks: ON")

# ========= RUN =========
def run_bg():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    print("✅ bot starting…", VERSION)
    run_bg()
    app.run(host="0.0.0.0", port=PORT)

