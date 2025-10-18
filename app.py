
# v7.0 ProVision — автоновости (≤4ч, перевод RU, sentiment), EMA/RSI сигналы с графиком,
# реальные цены (CoinGecko + Binance fallback), анти-дубли, авто-расписание.

import os, io, json, time, math, threading, datetime, random, urllib.parse
import requests
import schedule
import telebot
from xml.etree import ElementTree as ET

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN      = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "PASTE_TELEGRAM_BOT_TOKEN"
NEWS_CHAT_ID   = int(os.getenv("NEWS_CHAT_ID")   or "-1002969047835")   # Новости
SIGNAL_CHAT_ID = int(os.getenv("SIGNAL_CHAT_ID") or "-1003166387118")   # Сигналы/аналитика

MAX_NEWS_PER_DAY    = 7
MAX_SIGNALS_PER_DAY = 4
NEWS_WINDOW_HOURS   = 4.0  # только свежие новости (≤ 4 часа)
USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; CryptoProVision/7.0)"}

# RSS-источники (дают pubDate)
RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://watcher.guru/news/feed",
    "https://decrypt.co/feed",
    "https://coinmarketcap.com/headlines/news/feed/",
]

# ключевые слова (монеты + события + инфлюенсеры)
TOP20_TICKERS = [
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","TON","DOT",
    "AVAX","LINK","UNI","XLM","ICP","LTC","ATOM","NEAR","APT","ETC",
]
NEWS_KEYWORDS = TOP20_TICKERS + [
    "bitcoin","ethereum","binance","coinbase","sec","etf","listing",
    "fed","powell","rate","rally","dump","hack","airdrop","upgrade",
    "elon","musk","trump","saylor","vitalik","cz"
]

# файлы состояния
HISTORY_FILE = "sent_posts.json"   # заголовки уже отправленных новостей
QUOTA_FILE   = "quota.json"        # лимиты в сутки
STATE_LOCK   = threading.Lock()

# CoinGecko
CG_SIMPLE_PRICE = "https://api.coingecko.com/api/v3/simple/price"
# Binance (без ключей)
BINANCE_KLINES  = "https://api.binance.com/api/v3/klines"
BINANCE_PRICE   = "https://api.binance.com/api/v3/ticker/price"

# соответствие CoinGecko id → тикер Binance (спот)
SYMBOL_MAP = {
    "bitcoin":"BTCUSDT", "ethereum":"ETHUSDT", "bnb":"BNBUSDT", "solana":"SOLUSDT",
    "xrp":"XRPUSDT", "cardano":"ADAUSDT", "dogecoin":"DOGEUSDT", "tron":"TRXUSDT",
    "toncoin":"TONUSDT", "avalanche":"AVAXUSDT", "polkadot":"DOTUSDT", "chainlink":"LINKUSDT",
    "matic-network":"MATICUSDT", "litecoin":"LTCUSDT", "uniswap":"UNIUSDT", "stellar":"XLMUSDT",
    "internet-computer":"ICPUSDT", "aptos":"APTUSDT", "near":"NEARUSDT", "ethereum-classic":"ETCUSDT"
}

# набор популярных id для /price
TOP_COINS_CG = [
    "bitcoin","ethereum","bnb","solana","xrp","cardano","dogecoin","tron","toncoin","avalanche",
    "polkadot","chainlink","matic-network","litecoin","uniswap","stellar","internet-computer",
    "aptos","near","ethereum-classic"
]

bot = telebot.TeleBot(BOT_TOKEN)


# ===================== УТИЛИТЫ СОСТОЯНИЯ =====================
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)

def today_str():
    return datetime.date.today().isoformat()

def get_quota():
    with STATE_LOCK:
        q = load_json(QUOTA_FILE, {"date": today_str(), "news": 0, "signals": 0})
        if q.get("date") != today_str():
            q = {"date": today_str(), "news": 0, "signals": 0}
            save_json(QUOTA_FILE, q)
        return q

def inc_quota(field):
    with STATE_LOCK:
        q = get_quota()
        q[field] = q.get(field, 0) + 1
        save_json(QUOTA_FILE, q)

def add_history(title):
    with STATE_LOCK:
        h = load_json(HISTORY_FILE, [])
        if title not in h:
            h.append(title)
            if len(h) > 1000:
                h = h[-1000:]
            save_json(HISTORY_FILE, h)

def was_sent(title):
    h = load_json(HISTORY_FILE, [])
    return title in h


# ===================== ПЕРЕВОД RU =====================
def translate_ru(text: str, max_len=350) -> str:
    """Бесплатный перевод через публичный endpoint Google (без ключей).
       Если не удаётся — возвращает исходный текст."""
    try:
        if not text:
            return text
        cut = text[:max_len]
        q = urllib.parse.quote(cut)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ru&dt=t&q={q}"
        r = requests.get(url, headers=USER_AGENT, timeout=12)
        r.raise_for_status()
        data = r.json()
        # формат: [[["Перевод","Original",...],...],...]
        chunks = [seg[0] for seg in (data[0] or []) if seg and seg[0]]
        tr = "".join(chunks).strip()
        return tr or text
    except Exception:
        return text


# ===================== ЦЕНЫ (CG + fallback Binance) =====================
def cg_price_map(ids):
    try:
        url = f"{CG_SIMPLE_PRICE}?ids={','.join(ids)}&vs_currencies=usd"
        r = requests.get(url, headers=USER_AGENT, timeout=15)
        r.raise_for_status()
        data = r.json()
        return {k: v["usd"] for k, v in data.items() if isinstance(v, dict) and "usd" in v}
    except Exception as e:
        print("[cg] error:", e)
        return {}

def cg_id_guess(sym_or_id: str) -> str:
    s = sym_or_id.lower().strip()
    # быстрые сопоставления по началу/совпадению
    for cg in TOP_COINS_CG:
        if s == cg or s == cg.replace("-","") or cg.startswith(s):
            return cg
    # простые символы
    quick = {
        "btc":"bitcoin","eth":"ethereum","bnb":"bnb","sol":"solana","xrp":"xrp","ada":"cardano",
        "doge":"dogecoin","trx":"tron","ton":"toncoin","dot":"polkadot","avax":"avalanche",
        "link":"chainlink","matic":"matic-network","ltc":"litecoin","uni":"uniswap",
        "xlm":"stellar","icp":"internet-computer","apt":"aptos","near":"near","etc":"ethereum-classic"
    }
    return quick.get(s, s)

def binance_price(symbol="BTCUSDT"):
    try:
        r = requests.get(BINANCE_PRICE, params={"symbol": symbol}, headers=USER_AGENT, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        print("[binance] price error:", e)
        return None

def cg_price_one(sym_or_id: str):
    cgid = cg_id_guess(sym_or_id)
    mp = cg_price_map([cgid])
    p = mp.get(cgid)
    if p is not None:
        return p
    # fallback по Binance тикеру
    ticker = SYMBOL_MAP.get(cgid)
    if not ticker:
        # попытка построить тикер вида XXXUSDT
        guess = cgid.replace("-","").upper()
        ticker = guess + "USDT" if guess.isalpha() and len(guess) in (2,3,4,5) else "BTCUSDT"
    return binance_price(ticker)


# ===================== СВЕЧИ/ИНДИКАТОРЫ (Binance) =====================
def binance_klines(symbol="BTCUSDT", interval="15m", limit=200):
    try:
        r = requests.get(BINANCE_KLINES, params={"symbol":symbol,"interval":interval,"limit":limit},
                         headers=USER_AGENT, timeout=15)
        r.raise_for_status()
        raw = r.json()
        closes = [float(x[4]) for x in raw]
        times  = [int(x[0]) for x in raw]
        return times, closes
    except Exception as e:
        print("[binance] klines error:", e)
        return [], []

def ema(series, span):
    if not series: return []
    k = 2/(span+1.0); out=[series[0]]
    for v in series[1:]:
        out.append(v*k + out[-1]*(1-k))
    return out

def rsi(series, period=14):
    if len(series) < period+1: return []
    gains, losses = [], []
    for i in range(1, len(series)):
        ch = series[i]-series[i-1]
        gains.append(max(ch,0.0)); losses.append(max(-ch,0.0))
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    rsis=[]
    rs = (avg_gain/avg_loss) if avg_loss!=0 else math.inf
    rsis.append(100 - 100/(1+rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1)+gains[i])/period
        avg_loss = (avg_loss*(period-1)+losses[i])/period
        rs = (avg_gain/avg_loss) if avg_loss!=0 else math.inf
        rsis.append(100 - 100/(1+rs))
    pad = len(series)-len(rsis)
    return [None]*pad + rsis

def infer_direction(closes):
    if len(closes) < 40:
        return "LONG", 0.4
    e10 = ema(closes, 10)[-1]
    e30 = ema(closes, 30)[-1]
    r  = rsi(closes, 14)[-1] or 50.0
    dir_ema = 1 if e10>=e30 else -1
    dir_rsi = 1 if r>=55 else (-1 if r<=45 else 0)
    total = dir_ema + dir_rsi
    direction = "LONG" if total>=0 else "SHORT"
    rsi_conf = min(abs(r-50)/30.0, 1.0)
    ema_conf = min(abs(e10-e30)/(0.01*closes[-1]+1e-9), 1.0)
    conf = max(0.15, min(1.0, 0.5*rsi_conf + 0.5*ema_conf))
    return direction, conf

def leverage_from_conf(conf):
    base_min, base_max = 2, 7
    lvl = base_min + int(round(conf*(base_max-base_min)))
    return max(base_min, min(base_max, lvl))


# ===================== ГРАФИК СИГНАЛА =====================
def render_signal_chart(symbol, times, closes, entry, tp, sl):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import datetime as dt

    # последние ~2 суток
    xs = [dt.utcfromtimestamp(t/1000.0) for t in times]
    e10 = ema(closes, 10)
    e30 = ema(closes, 30)
    fig = plt.figure(figsize=(8,4), dpi=140)
    ax  = plt.gca()
    ax.plot(xs, closes, linewidth=1.6, label=f"{symbol} • 15m")
    if len(e10)==len(closes): ax.plot(xs, e10, linewidth=1.0, label="EMA10")
    if len(e30)==len(closes): ax.plot(xs, e30, linewidth=1.0, label="EMA30")
    ax.axhline(entry, linestyle="--", linewidth=1.0, label=f"Entry {entry:,.2f}")
    ax.axhline(tp,    linestyle="--", linewidth=1.0, label=f"TP {tp:,.2f}")
    ax.axhline(sl,    linestyle="--", linewidth=1.0, label=f"SL {sl:,.2f}")
    ax.grid(True, alpha=0.25); ax.legend(loc="upper left"); ax.set_ylabel("USD")
    fig.tight_layout()
    buf = io.BytesIO(); plt.savefig(buf, format="png"); plt.close(fig); buf.seek(0)
    return buf


# ===================== СБОРКА СИГНАЛА =====================
def build_signal_for_cgid(cg_id="bitcoin", equity=1000.0):
    symbol = SYMBOL_MAP.get(cg_id, "BTCUSDT")
    ts, closes = binance_klines(symbol, "15m", 200)
    if len(closes) < 40:
        return None
    entry = float(closes[-1])
    direction, conf = infer_direction(closes)
    lev = leverage_from_conf(conf)
    if direction == "LONG":
        tp, sl = entry*1.025, entry*0.985
    else:
        tp, sl = entry*0.975, entry*1.015
    stop_dist = abs(entry - sl)
    qty = (equity * 0.05) / (stop_dist if stop_dist>0 else (0.001*entry))
    notional, cap = qty*entry, equity*lev
    if notional > cap:
        qty *= cap/(notional+1e-9)
    coin = symbol.replace("USDT","")
    text = (
        f"📊 Сигнал по {coin}\n\n"
        f"🎯 Вход: {entry:,.4f} $\n"
        f"💰 Тейк: {tp:,.4f} $\n"
        f"🛑 Стоп: {sl:,.4f} $\n\n"
        f"⚖ Плечо: x{lev}\n"
        f"💵 Риск: 5% от депозита\n"
        f"Размер позиции ≈ {qty:,.6f} {coin}\n"
        f"Направление: {'🟢 LONG' if direction=='LONG' else '🔴 SHORT'}\n"
        f"Уверенность: {int(conf*100)}%\n"
        f"#signal #{coin.lower()} #crypto"
    )
    img = render_signal_chart(symbol, ts[-120:], closes[-120:], entry, tp, sl)
    return text, img

def pick_symbols_for_signals(n=2):
    universe = ["bitcoin","ethereum","solana","bnb","xrp","cardano","dogecoin","tron","toncoin","avalanche"]
    return universe[:n]


# ===================== SENTIMENT (очень простой) =====================
POS_WORDS = ["bull", "bullish", "pump", "rally", "up", "soared", "surge", "gain"]
NEG_WORDS = ["bear", "bearish", "dump", "down", "crash", "fall", "selloff", "fear"]

def sentiment(text: str) -> str:
    t = text.lower()
    pos = sum(w in t for w in POS_WORDS)
    neg = sum(w in t for w in NEG_WORDS)
    if pos>neg: return "😁 Позитивно"
    if neg>pos: return "😟 Негативно"
    return "😐 Нейтрально"


# ===================== НОВОСТИ (RSS ≤ 4ч, перевод, анти-дубли) =====================
def parse_rss(url, max_items=30):
    out = []
    try:
        raw = requests.get(url, headers=USER_AGENT, timeout=20).text
        root = ET.fromstring(raw)
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if not title or not link or not pub:
                continue
            # формат: "Sat, 18 Oct 2025 00:24:00 +0000"
            try:
                dt = datetime.datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S")
            except Exception:
                # иногда ISO
                try:
                    dt = datetime.datetime.fromisoformat(pub.replace("Z","+00:00").split("+")[0])
                except Exception:
                    continue
            age_h = (datetime.datetime.utcnow() - dt).total_seconds()/3600.0
            if age_h <= NEWS_WINDOW_HOURS:
                out.append({"title": title, "link": link, "age_h": age_h})
    except Exception as e:
        print("[rss] parse error:", e)
    return out

def is_crypto_related(title):
    t = title.upper()
    return any(k in t for k in NEWS_KEYWORDS)

def collect_fresh_news():
    items=[]
    for src in RSS_SOURCES:
        items.extend(parse_rss(src, max_items=30))
    # фильтр по крипто/инфлюенсерам/монетам
    items = [it for it in items if is_crypto_related(it["title"])]
    # анти-дубли по заголовку
    items = [it for it in items if not was_sent(it["title"])]
    # свежие вперёд
    items.sort(key=lambda x: x["age_h"])
    return items[:30]

def post_news_batch():
    q = get_quota()
    if q["news"] >= MAX_NEWS_PER_DAY:
        return
    items = collect_fresh_news()
    if not items:
        return
    can = MAX_NEWS_PER_DAY - q["news"]
    batch = items[:can]
    for it in batch:
        title, link = it["title"], it["link"]
        tr = translate_ru(title)
        sent = sentiment(title)
        msg = f"📰 {title}\n**Перевод:** {tr}\n{sent}\n🔗 {link}\n#CryptoNews"
        try:
            bot.send_message(NEWS_CHAT_ID, msg, parse_mode="Markdown")
            add_history(title)
            inc_quota("news")
            time.sleep(3)
        except Exception as e:
            print("[tg] news send error:", e)


# ===================== АВТО-СИГНАЛЫ =====================
def post_signals_batch():
    q = get_quota()
    if q["signals"] >= MAX_SIGNALS_PER_DAY:
        return
    can = MAX_SIGNALS_PER_DAY - q["signals"]
    per_run = min(2, can)  # за один прогон не больше 2
    picked = pick_symbols_for_signals(per_run)
    for cid in picked:
        try:
            payload = build_signal_for_cgid(cg_id=cid, equity=1000.0)
            if not payload:
                continue
            text, img = payload
            bot.send_photo(SIGNAL_CHAT_ID, img, caption=text)
            inc_quota("signals")
            time.sleep(4)
        except Exception as e:
            print("[tg] signal send error:", e)


# ===================== РАСПИСАНИЕ =====================
def scheduler_thread():
    # каждые 3 часа — новости
    schedule.every(3).hours.do(post_news_batch)
    # каждые 6 часов — сигналы (в сумме до 4/день)
    schedule.every(6).hours.do(post_signals_batch)
    # ежедневный мягкий сброс квот
    schedule.every().day.at("00:05").do(lambda: save_json(QUOTA_FILE, {"date": today_str(), "news": 0, "signals": 0}))
    # начальный запуск
    time.sleep(5)
    try:
        post_news_batch()
        post_signals_batch()
    except Exception as e:
        print("[init] error:", e)
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print("[schedule] error:", e)
        time.sleep(5)


# ===================== КОМАНДЫ =====================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.reply_to(m, "👋 Я CryptoBot v7.0 ProVision: новости (≤4ч, перевод RU) и сигналы EMA/RSI с графиком. Это не финсовет.")

@bot.message_handler(commands=["version"])
def cmd_version(m):
    q = get_quota()
    bot.reply_to(m, f"Version: v7.0 ProVision\nNews today: {q['news']}/{MAX_NEWS_PER_DAY}\nSignals today: {q['signals']}/{MAX_SIGNALS_PER_DAY}\nSources: CoinGecko+Binance+RSS\nTranslate: ON")

@bot.message_handler(commands=["price"])
def cmd_price(m):
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Использование: /price BTC")
        return
    coin = parts[1]
    p = cg_price_one(coin)
    if p is None:
        bot.reply_to(m, f"❌ Не смог получить цену для {coin.upper()}")
    else:
        bot.reply_to(m, f"💰 {coin.upper()}: ${p:,.6f}")

@bot.message_handler(commands=["news"])
def cmd_news(m):
    post_news_batch()
    bot.reply_to(m, "✅ Свежие новости опубликованы (если были и лимит не исчерпан).")

@bot.message_handler(commands=["signal"])
def cmd_signal(m):
    post_signals_batch()
    bot.reply_to(m, "✅ Сигналы сгенерированы (если лимит не исчерпан).")


# ===================== ЗАПУСК =====================
def run_scheduler():
    t = threading.Thread(target=scheduler_thread, daemon=True)
    t.start()

if __name__ == "__main__":
    print("✅ CryptoBot v7.0 ProVision starting…")
    run_scheduler()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
