

# v7.1 Stable — Background worker, no web, webhook auto-delete, polling.
# Auto-news (<=4h, RU translate, no duplicates), auto-signals (EMA/RSI on Binance 15m, with chart),
# real prices (CoinGecko + Binance fallback), daily limits, commands.

import os, io, json, time, math, threading, datetime, urllib.parse
import requests, schedule, telebot

# --- ФИКС ошибки 409: удаляем webhook перед запуском ---
try:
    import requests
    BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
    if BOT_TOKEN:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        print("✅ Webhook удалён:", r.text)
    else:
        print("⚠️ Не найден токен бота, пропускаю удаление webhook.")
except Exception as e:
    print("⚠️ Ошибка при удалении webhook:", e)

from xml.etree import ElementTree as ET

# ============== CONFIG ==============
BOT_TOKEN      = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "PASTE_TELEGRAM_BOT_TOKEN"
NEWS_CHAT_ID   = int(os.getenv("NEWS_CHAT_ID")   or "-1002969047835")   # Новости
SIGNAL_CHAT_ID = int(os.getenv("SIGNAL_CHAT_ID") or "-1003166387118")   # Сигналы/аналитика

MAX_NEWS_PER_DAY    = 7
MAX_SIGNALS_PER_DAY = 4
NEWS_WINDOW_HOURS   = 4.0

USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; CryptoBot/7.1-Stable)"}

# RSS источники (с pubDate)
RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://watcher.guru/news/feed",
    "https://decrypt.co/feed",
    "https://coinmarketcap.com/headlines/news/feed/",
]

TOP20_TICKERS = [
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","TON","DOT",
    "AVAX","LINK","UNI","XLM","ICP","LTC","ATOM","NEAR","APT","ETC",
]
NEWS_KEYWORDS = TOP20_TICKERS + [
    "bitcoin","ethereum","binance","coinbase","sec","etf","listing",
    "fed","powell","rate","rally","dump","hack","airdrop","upgrade",
    "elon","musk","trump","saylor","vitalik","cz"
]

# Файлы состояния (анти-дубли и лимиты)
HISTORY_FILE = "sent_posts.json"   # titles мы уже отправляли
QUOTA_FILE   = "quota.json"        # дневные счётчики
LOCK = threading.Lock()

# Цены
CG_SIMPLE_PRICE = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_PRICE   = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES  = "https://api.binance.com/api/v3/klines"

# Соответствие CoinGecko id → Binance тикер
SYMBOL_MAP = {
    "bitcoin":"BTCUSDT", "ethereum":"ETHUSDT", "bnb":"BNBUSDT", "solana":"SOLUSDT",
    "xrp":"XRPUSDT", "cardano":"ADAUSDT", "dogecoin":"DOGEUSDT", "tron":"TRXUSDT",
    "toncoin":"TONUSDT", "avalanche":"AVAXUSDT", "polkadot":"DOTUSDT", "chainlink":"LINKUSDT",
    "matic-network":"MATICUSDT", "litecoin":"LTCUSDT", "uniswap":"UNIUSDT", "stellar":"XLMUSDT",
    "internet-computer":"ICPUSDT", "aptos":"APTUSDT", "near":"NEARUSDT", "ethereum-classic":"ETCUSDT"
}
TOP_COINS_CG = [
    "bitcoin","ethereum","bnb","solana","xrp","cardano","dogecoin","tron","toncoin","avalanche",
    "polkadot","chainlink","matic-network","litecoin","uniswap","stellar","internet-computer",
    "aptos","near","ethereum-classic"
]

bot = telebot.TeleBot(BOT_TOKEN)

# ============== STATE UTILS ==============
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
    with LOCK:
        q = load_json(QUOTA_FILE, {"date": today_str(), "news": 0, "signals": 0})
        if q.get("date") != today_str():
            q = {"date": today_str(), "news": 0, "signals": 0}
            save_json(QUOTA_FILE, q)
        return q

def inc_quota(key):
    with LOCK:
        q = get_quota()
        q[key] = q.get(key, 0) + 1
        save_json(QUOTA_FILE, q)

def was_sent(title):
    h = load_json(HISTORY_FILE, [])
    return title in h

def add_history(title):
    with LOCK:
        h = load_json(HISTORY_FILE, [])
        if title not in h:
            h.append(title)
            if len(h) > 1500:  # ограничим размер файла
                h = h[-800:]
            save_json(HISTORY_FILE, h)

# ============== TRANSLATE (RU) ==============
def translate_ru(text, max_len=350):
    """Бесплатный перевод через публичный Google endpoint (без ключей).
       Если не удалось — вернём исходный текст."""
    try:
        if not text:
            return text
        cut = text[:max_len]
        q = urllib.parse.quote(cut)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ru&dt=t&q={q}"
        r = requests.get(url, headers=USER_AGENT, timeout=12)
        r.raise_for_status()
        data = r.json()
        parts = [seg[0] for seg in (data[0] or []) if seg and seg[0]]
        tr = "".join(parts).strip()
        return tr or text
    except Exception:
        return text

def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ============== PRICES (CoinGecko + Binance fallback) ==============

def get_price(symbol):
    symbol = symbol.strip().upper()
    coingecko_map = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "XRP": "ripple",
        "BNB": "binancecoin",
        "SOL": "solana",
        "DOGE": "dogecoin",
        "ADA": "cardano"
    }

    try:
        # CoinGecko
        name = coingecko_map.get(symbol, symbol.lower())
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={name}&vs_currencies=usd"
        r = requests.get(url, headers=USER_AGENT, timeout=10)
        data = r.json()
        if name in data:
            return data[name]["usd"]
    except Exception as e:
        print("CoinGecko error:", e)

    try:
        # Binance fallback
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT"
        r = requests.get(url, headers=USER_AGENT, timeout=10)
        data = r.json()
        if "price" in data:
            return float(data["price"])
    except Exception as e:
        print("Binance error:", e)

    return None

def cg_price_one(sym_or_id):
    cgid = cg_id_guess(sym_or_id)
    mp = cg_price_map([cgid])
    p = mp.get(cgid)
    if p is not None:
        return p
    # fallback на Binance тикер
    ticker = SYMBOL_MAP.get(cgid)
    if not ticker:
        guess = cgid.replace("-","").upper()
        ticker = guess + "USDT" if guess.isalpha() else "BTCUSDT"
    return binance_price(ticker)

# ============== KLINES + INDICATORS ==============
def binance_klines(symbol="BTCUSDT", interval="15m", limit=200):
    try:
        r = requests.get(BINANCE_KLINES,
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         headers=USER_AGENT, timeout=15)
        r.raise_for_status()
        raw = r.json()
        times  = [int(x[0]) for x in raw]
        closes = [float(x[4]) for x in raw]
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

# ============== SIGNAL BUILD (with chart) ==============
def render_signal_chart(symbol, times, closes, entry, tp, sl):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import datetime as dt

    xs = [dt.utcfromtimestamp(t/1000.0) for t in times]
    e10 = ema(closes, 10)
    e30 = ema(closes, 30)
    fig = plt.figure(figsize=(8,4), dpi=140)
    ax = plt.gca()
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

def build_signal_for_cgid(cg_id="bitcoin", equity=1000.0):
    symbol = SYMBOL_MAP.get(cg_id, "BTCUSDT")
    times, closes = binance_klines(symbol, "15m", 200)
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
    img = render_signal_chart(symbol, times[-120:], closes[-120:], entry, tp, sl)
    return text, img

def pick_symbols_for_signals(n=2):
    # можно усложнить логикой выбора — пока берём популярные
    universe = ["bitcoin","ethereum","solana","bnb","xrp","cardano","dogecoin","tron","toncoin","avalanche"]
    return universe[:n]

# ============== NEWS (<=4h, translate RU, no dupes) ==============
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
            # формат типа "Sat, 18 Oct 2025 00:24:00 +0000"
            try:
                dt = datetime.datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S")
            except Exception:
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
    items = [it for it in items if is_crypto_related(it["title"])]
    items = [it for it in items if not was_sent(it["title"])]
    items.sort(key=lambda x: x["age_h"])  # свежие вперёд
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
        # Переводим заголовок без префикса "Перевод:"
        tr = translate_ru(title)
        msg_html = f"📰 {html_escape(tr)}\n🔗 <a href=\"{html_escape(link)}\">Источник</a>\n#CryptoNews"
        try:
           comment = generate_thought(msg)
           analysis = generate_analysis(msg)
           bot.send_message(NEWS_CHAT_ID, msg + "\n\n" + comment + "\n\n" + analysis)
           add_history(title)
           inc_quota("news")
           time.sleep(3)
           except Exception as e:
           print("[tg news send error]:", e)

     # ============ ADVANCED ANALYSIS ============
def generate_analysis(text):
    text_low = text.lower()
    coin = "рынок"
    for sym in ["btc", "bitcoin", "eth", "ethereum", "xrp", "solana", "bnb", "layer", "doge"]:
        if sym in text_low:
            coin = sym.upper()
            break

    if any(word in text_low for word in ["rise", "surge", "up", "bullish", "growth", "increase", "gain"]):
        return f"🚀 Позитивно для {coin} — возможно движение вверх."
    elif any(word in text_low for word in ["drop", "fall", "bearish", "decline", "down", "risk", "crash"]):
        return f"📉 Негативно для {coin} — может снизиться в ближайшее время."
    elif any(word in text_low for word in ["etf", "approval", "law", "decision", "update", "adoption"]):
        return f"⚖️ Новость по {coin} может вызвать краткосрочную волатильность."
    else:
        return f"🤔 {coin}: ситуация неоднозначная, наблюдаем за динамикой."
           
            comment = generate_thought(msg)
            analysis = generate_analysis(msg)
            bot.send_message(NEWS_CHAT_ID, msg + "\n\n" + comment + "\n" + analysis)
            add_history(title)      # дедуп
            inc_quota("news")       # квота
            time.sleep(3)
        except Exception as e:
            print("[tg] news send error:", e)

# ============ AUTO COMMENT GENERATOR ============
def generate_thought(text):
    text = text.lower()
    if any(word in text for word in ["rise", "surge", "up", "growth", "bullish", "increase", "profit", "gain"]):
        return "🚀 Новости выглядят позитивно — рынок может показать рост."
    elif any(word in text for word in ["drop", "fall", "bearish", "decline", "risk", "down", "crash", "recession"]):
        return "📉 Похоже, аналитики ожидают снижение — стоит быть осторожнее."
    elif any(word in text for word in ["etf", "approval", "decision", "update", "law", "adoption"]):
        return "⚖️ Интересное событие — может повлиять на рынок в ближайшие дни."
    else:
        return "🤔 Ситуация неясна, наблюдаем за динамикой."

# ============== AUTORUN TASKS ==============
def post_signals_batch():
    q = get_quota()
    if q["signals"] >= MAX_SIGNALS_PER_DAY:
        return
    can = MAX_SIGNALS_PER_DAY - q["signals"]
    per_run = min(4, can)  # за один прогон максимум 4 поста
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

# ============== SCHEDULER ==============
def scheduler_loop():
    # каждые 3 часа — новости
    schedule.every(3).hours.do(post_news_batch)
    # каждые 4 часов — сигналы
    schedule.every(4).hours.do(post_signals_batch)
    # мягкий ежедневный ресет квот на всякий случай (файл и так по дате)
    schedule.every().day.at("00:05").do(lambda: save_json(QUOTA_FILE, {"date": today_str(), "news": 0, "signals": 0}))
    # моментальный старт
    time.sleep(5)
    try:
        post_news_batch()
        post_signals_batch()
    except Exception as e:
        print("[init run] error:", e)
    # цикл
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print("[schedule] error:", e)
        time.sleep(5)

# ============== COMMANDS ==============
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.reply_to(m, "👋 Я CryptoBot v7.1 Stable: новости (≤4ч, RU перевод) и сигналы EMA/RSI с графиком. Это не финсовет.")

@bot.message_handler(commands=["version"])
def cmd_version(m):
    q = get_quota()
    bot.reply_to(m, f"Version: v7.1 Stable\nNews today: {q['news']}/{MAX_NEWS_PER_DAY}\nSignals today: {q['signals']}/{MAX_SIGNALS_PER_DAY}\nSources: CoinGecko+Binance+RSS\nTranslate: ON")

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

# ============ START ============

def run_scheduler_thread():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

def delete_webhook_if_any():
    """Жестко гасим вебхук перед запуском polling, чтобы не ловить 409."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
        r = requests.get(url, timeout=10)
        print("deleteWebhook status:", r.status_code, r.text[:200])
    except Exception as e:
        print("deleteWebhook error:", e)

def print_webhook_info():
    """(не обязательно) Просто для логов — посмотреть текущее состояние."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
        r = requests.get(url, timeout=10)
        print("getWebhookInfo:", r.status_code, r.text[:200])
    except Exception as e:
        print("getWebhookInfo error:", e)

if __name__ == "__main__":
    print("✅ CryptoBot v7.1 Stable starting…")
    delete_webhook_if_any()
    print_webhook_info()
    run_scheduler_thread()

    # ВАЖНО: только один способ опроса!
    # Ниже — единый бесконечный polling.
    bot.infinity_polling(timeout=60, long_polling_timeout=50, skip_pending=True)
