# v6.1 — LONG & SHORT (EMA+RSI), CoinGecko, RSS+LunarCrush news, Twitter influencers (tweepy), 24/7
import os, io, time, threading, datetime as dt, math
from flask import Flask, request
import requests, xml.etree.ElementTree as ET
import telebot

# ========= ENV / CONFIG =========
TOKEN    = os.getenv("TOKEN")                    # Telegram bot token (Render → Environment)
LUNAR    = os.getenv("LUNARCRUSH_KEY")          # LunarCrush Bearer (Render → Environment)
PORT     = int(os.environ.get("PORT", 10000))

# Twitter API (X) — OAuth1 (user context)
TWITTER_API_KEY       = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET    = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN  = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET")

TRADING_CHAT = -1003166387118   # Аналитика
NEWS_CHAT    = -1002969047835   # Новости

MAX_NEWS_PER_DAY    = 7
MAX_SIGNALS_PER_DAY = 2
BASE_LEVERAGE_MIN   = 2
BASE_LEVERAGE_MAX   = 7
RISK_PER_TRADE      = 0.05       # 5% от депозита

HEADERS = {"User-Agent": "Mozilla/5.0"}
CG_BASE = "https://api.coingecko.com/api/v3"
RSS_FEEDS = [
    "https://www.binance.com/en/blog/rss",
    "https://cointelegraph.com/rss",
]
INFLUENCER_HINTS = [
    "elon", "musk", "trump", "saylor", "buterin", "cz", "gensler",
    "sec", "etf", "listing", "blackrock", "fidelity", "coinbase", "binance",
    "airdrop", "upgrade", "hack", "btc", "eth", "crypto", "bitcoin", "market", "pump", "dump"
]

VERSION = "v6.1-EMA-RSI-TW"

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

def jget(url, params=None, headers=None, timeout=25):
    try:
        r = requests.get(url, params=params or {}, headers=headers or HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

def tget(url, timeout=25):
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
    state["top20"] = syms or ["BTC","ETH","SOL","BNB","XRP","ADA","DOGE","TON","TRX","DOT",
                              "AVAX","LINK","UNI","XLM","ICP","LTC","ATOM","NEAR","APT","ETC"]
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
    return data.get("prices") or []  # [[ts_ms, price], ...]

# ========= TECHNICALS =========
def ema(values, span):
    if not values: return []
    k = 2 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(values, period=14):
    if len(values) < period + 1: return []
    gains, losses = [], []
    for i in range(1, len(values)):
        ch = values[i] - values[i-1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = []
    rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
    rsis.append(100 - (100 / (1 + rs)))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
        rsis.append(100 - (100 / (1 + rs)))
    pad = len(values) - len(rsis)
    return [None]*pad + rsis

def infer_direction(ys):
    if len(ys) < 40:
        return "LONG", 0.4
    ema10 = ema(ys, 10)
    ema30 = ema(ys, 30)
    rsi14 = rsi(ys, 14)
    e10, e30 = ema10[-1], ema30[-1]
    r = rsi14[-1] if rsi14 and rsi14[-1] is not None else 50.0
    dir_ema = 1 if e10 >= e30 else -1
    dir_rsi = 1 if r >= 55 else (-1 if r <= 45 else 0)
    total = dir_ema + dir_rsi
    direction = "LONG" if total >= 0 else "SHORT"
    rsi_conf = min(abs(r - 50) / 30.0, 1.0)
    ema_conf = min(abs(e10 - e30) / (0.01 * ys[-1] + 1e-9), 1.0)
    confidence = max(0.15, min(1.0, 0.5*rsi_conf + 0.5*ema_conf))
    return direction, confidence

def leverage_from_conf(conf):
    lvl = BASE_LEVERAGE_MIN + int(round(conf * (BASE_LEVERAGE_MAX - BASE_LEVERAGE_MIN)))
    return max(BASE_LEVERAGE_MIN, min(BASE_LEVERAGE_MAX, lvl))

# ========= PICK SYMBOL =========
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
    best, best_div = "BTC", -1
    for s in top20_symbols():
        data = market_chart(s, days=2)
        ys = [p[1] for p in data][-60:]
        if len(ys) < 30: continue
        e10, e30 = ema(ys, 10)[-1], ema(ys, 30)[-1]
        div = abs(e10 - e30) / (ys[-1] + 1e-9)
        if div > best_div:
            best_div, best = div, s
    return best

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
    ys = [v[1] for v in hist]
    direction, conf = infer_direction(ys)
    lev = leverage_from_conf(conf)

    entry = float(p)
    if direction == "LONG":
        tp, sl = entry * 1.025, entry * 0.985
    else:
        tp, sl = entry * 0.975, entry * 1.015

    stop_dist = abs(entry - sl)
    qty = (equity * RISK_PER_TRADE) / (stop_dist if stop_dist > 0 else (0.001*entry))
    notional, cap = qty * entry, equity * lev
    if notional > cap:
        qty *= cap / (notional + 1e-9)

    img = chart_image(sym, entry, tp, sl)
    text = (
        f"📊 Сигнал по {sym}\n\n"
        f"🎯 Вход: {entry:,.4f} $\n"
        f"💰 Тейк: {tp:,.4f} $\n"
        f"🛑 Стоп: {sl:,.4f} $\n\n"
        f"⚖ Плечо: x{lev}\n"
        f"💵 Риск: {int(RISK_PER_TRADE*100)}% от депозита\n"
        f"Размер позиции ≈ {qty:,.6f} {sym}\n"
        f"Направление: {'🟢 LONG' if direction=='LONG' else '🔴 SHORT'}\n"
        f"Уверенность: {int(conf*100)}%\n"
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
        print(f"[signal] {sym} posted")
    except Exception as e:
        bot.send_message(TRADING_CHAT, f"⚠️ Ошибка сигнала: {e}")

# ========= NEWS: RSS + LunarCrush =========
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
        important = any(k in t for k in ["bitcoin","btc","ethereum","eth","binance","coinbase","sec","etf","listing","spot","hack","airdrop","upgrade","trump","musk"])
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

# ========= TWITTER / INFLUENCERS =========
def twitter_enabled():
    return all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET])

influencers = {
    "elonmusk": "Илон Маск",
    "realDonaldTrump": "Дональд Трамп",
    "cz_binance": "CZ (Binance)",
    "saylor": "Майкл Сэйлор",
    "VitalikButerin": "Виталик Бутерин"
}

_last_tweet_id = {}  # username -> last_id (чтобы не постить одно и то же)

def check_influencers():
    if not twitter_enabled():
        return []
    try:
        import tweepy
        auth = tweepy.OAuth1UserHandler(
            TWITTER_API_KEY, TWITTER_API_SECRET,
            TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET
        )
        api = tweepy.API(auth, wait_on_rate_limit=True)
    except Exception as e:
        print(f"[twitter auth error] {e}")
        return []

    alerts = []
    for username, pretty in influencers.items():
        try:
            tweets = api.user_timeline(screen_name=username, count=5, tweet_mode="extended", since_id=_last_tweet_id.get(username))
            max_id = _last_tweet_id.get(username, 0)
            for t in tweets:
                text = (t.full_text or "").strip()
                t_low = text.lower()
                if any(k in t_low for k in INFLUENCER_HINTS):
                    url = f"https://twitter.com/{username}/status/{t.id}"
                    msg = f"💬 {pretty} написал(а):\n{text}\nСсылка: {url}\n#influencer #{username}"
                    alerts.append(msg)
                if t.id > max_id:
                    max_id = t.id
            if max_id:
                _last_tweet_id[username] = max_id
        except Exception as e:
            print(f"[twitter fetch error] {username}: {e}")
    return alerts

# ========= SCHEDULER =========
def scheduler_loop():
    last_news, last_sig, last_tw = 0, 0, 0
    while True:
        now = time.time()
        # Новости (каждые 30 мин, до лимита)
        if now - last_news >= 30*60:
            post_news_once(); last_news = now
        # Сигналы (каждые 3 часа, до 2/сутки)
        if now - last_sig >= 3*60*60:
            post_signal_once(); last_sig = now
        # Инфлюенсеры (каждые 20 мин)
        if now - last_tw >= 20*60:
            infl = check_influencers()
            for m in infl:
                reset_counters_if_new_day()
                if state["news"] < MAX_NEWS_PER_DAY:
                    bot.send_message(NEWS_CHAT, m)
                    state["news"] += 1
                    time.sleep(2)
            last_tw = now
        time.sleep(12)

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
    bot.send_message(m.chat.id, "Привет! Публикую новости (до 7/день) и сигналы (до 2/день), LONG/SHORT по EMA+RSI. Отслеживаю инфлюенсеров (Маск/Трамп/и др.). Это не финсовет.")

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
    tw = "ON" if twitter_enabled() else "OFF"
    bot.send_message(m.chat.id, f"Version: {VERSION}\nPrices: CoinGecko\nLunarCrush: {lunar}\nTwitter: {tw}\nWebhooks: ON")

# ========= RUN =========
def run_bg():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    print("✅ bot starting…", VERSION)
    run_bg()
    app.run(host="0.0.0.0", port=PORT)

