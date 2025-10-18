# v6.5 — AutoTrader: авто-новости и сигналы, EMA/RSI по Binance, цены CoinGecko
import os, json, time, math, random, threading, datetime
import requests
import telebot
import schedule
from xml.etree import ElementTree as ET

# ================== НАСТРОЙКИ ==================
BOT_TOKEN      = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "PASTE_TELEGRAM_BOT_TOKEN"
NEWS_CHAT_ID   = int(os.getenv("NEWS_CHAT_ID") or "-1002969047835")     # Новости
SIGNAL_CHAT_ID = int(os.getenv("SIGNAL_CHAT_ID") or "-1003166387118")   # Аналитика/сигналы

# лимиты в сутки
MAX_NEWS_PER_DAY    = 7
MAX_SIGNALS_PER_DAY = 4

# RSS-источники (дают pubDate → можем фильтровать по времени)
RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://watcher.guru/news/feed",
    "https://decrypt.co/feed",
    "https://coinmarketcap.com/headlines/news/feed/",
]

# ключевые слова для отбора «крипто»-новостей и влияющих персон
TOP20_TICKERS = [
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TRX","TON","DOT",
    "AVAX","LINK","UNI","XLM","ICP","LTC","ATOM","NEAR","APT","ETC",
]
NEWS_KEYWORDS = TOP20_TICKERS + [
    "bitcoin","ethereum","binance","coinbase","sec","etf","listing",
    "fed","powell","rate","rally","dump","hack","airdrop","upgrade",
    "elon","musk","trump","saylor","vitalik","cz"
]

# хранение состояний (в файлах, чтобы не спамил при перезапусках)
HISTORY_FILE = "sent_posts.json"   # заголовки уже отправленных новостей
QUOTA_FILE   = "quota.json"        # счётчики за сутки
STATE_LOCK   = threading.Lock()

# CoinGecko
CG_SIMPLE_PRICE = "https://api.coingecko.com/api/v3/simple/price"

# Binance (без ключей)
BINANCE_KLINES  = "https://api.binance.com/api/v3/klines"

# соответствие CoinGecko id → тикер Binance (спот)
SYMBOL_MAP = {
    "bitcoin":"BTCUSDT", "ethereum":"ETHUSDT", "bnb":"BNBUSDT", "solana":"SOLUSDT",
    "xrp":"XRPUSDT", "cardano":"ADAUSDT", "dogecoin":"DOGEUSDT", "tron":"TRXUSDT",
    "toncoin":"TONUSDT", "avalanche":"AVAXUSDT", "polkadot":"DOTUSDT", "chainlink":"LINKUSDT",
    "matic-network":"MATICUSDT", "litecoin":"LTCUSDT", "uniswap":"UNIUSDT", "stellar":"XLMUSDT",
    "internet-computer":"ICPUSDT", "aptos":"APTUSDT", "ethereum-classic":"ETCUSDT", "near":"NEARUSDT"
}

# =============== ТЕЛЕГРАМ-БОТ ===============
bot = telebot.TeleBot(BOT_TOKEN)

# =============== УТИЛИТЫ СОСТОЯНИЯ ===============
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
            # ограничим память до последних 500 заголовков
            if len(h) > 500:
                h = h[-500:]
            save_json(HISTORY_FILE, h)

def was_sent(title):
    h = load_json(HISTORY_FILE, [])
    return title in h

# ================= ЦЕНЫ (CoinGecko) =================
# TOP_COINS — список id CoinGecko для /price и т.п.
TOP_COINS_CG = [
    "bitcoin","ethereum","bnb","solana","xrp","cardano","dogecoin","tron","toncoin","avalanche",
    "polkadot","chainlink","matic-network","litecoin","uniswap","stellar","internet-computer",
    "aptos","near","ethereum-classic"
]

def cg_price_map(ids):
    url = f"{CG_SIMPLE_PRICE}?ids={','.join(ids)}&vs_currencies=usd"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        # вернуть только где есть usd
        return {k: v["usd"] for k, v in data.items() if isinstance(v, dict) and "usd" in v}
    except Exception as e:
        print("[cg] error:", e)
        return {}

def cg_price_one(sym):  # sym может быть и символом (BTC), и id (bitcoin)
    sym_low = sym.lower()
    # если это тикер типа BTC — попытка найти id
    guess = None
    for cg in TOP_COINS_CG:
        if cg.startswith(sym_low) or sym_low in [cg, cg.replace("-","")]:
            guess = cg
            break
    if guess is None:
        guess = sym_low
    mp = cg_price_map([guess])
    return mp.get(guess)

# ================= СВЕЧИ/ИНДИКАТОРЫ (Binance) =================
def binance_klines(symbol="BTCUSDT", interval="15m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(BINANCE_KLINES, params=params, timeout=15)
        r.raise_for_status()
        raw = r.json()
        closes = [float(x[4]) for x in raw]
        return closes
    except Exception as e:
        print("[binance] klines error:", e)
        return []

def ema(series, span):
    if not series: return []
    k = 2/(span+1.0)
    out = [series[0]]
    for v in series[1:]:
        out.append(v*k + out[-1]*(1-k))
    return out

def rsi(series, period=14):
    if len(series) < period+1: return []
    gains, losses = [], []
    for i in range(1, len(series)):
        ch = series[i] - series[i-1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    rsis = []
    rs = (avg_gain/avg_loss) if avg_loss != 0 else math.inf
    rsis.append(100 - 100/(1+rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1)+gains[i]) / period
        avg_loss = (avg_loss*(period-1)+losses[i]) / period
        rs = (avg_gain/avg_loss) if avg_loss != 0 else math.inf
        rsis.append(100 - 100/(1+rs))
    pad = len(series) - len(rsis)
    return [None]*pad + rsis

def infer_direction_from_closes(closes):
    if len(closes) < 40:
        return "LONG", 0.4
    e10 = ema(closes, 10)[-1]
    e30 = ema(closes, 30)[-1]
    rsi14 = rsi(closes, 14)[-1] or 50.0
    dir_ema = 1 if e10 >= e30 else -1
    dir_rsi = 1 if rsi14 >= 55 else (-1 if rsi14 <= 45 else 0)
    total = dir_ema + dir_rsi
    direction = "LONG" if total >= 0 else "SHORT"
    rsi_conf = min(abs(rsi14 - 50) / 30.0, 1.0)
    ema_conf = min(abs(e10 - e30) / (0.01 * closes[-1] + 1e-9), 1.0)
    conf = max(0.15, min(1.0, 0.5*rsi_conf + 0.5*ema_conf))
    return direction, conf

def leverage_from_conf(conf):
    base_min, base_max = 2, 7
    lvl = base_min + int(round(conf*(base_max-base_min)))
    return max(base_min, min(base_max, lvl))

def build_signal_for_cgid(cg_id="bitcoin", equity=1000.0):
    symbol = SYMBOL_MAP.get(cg_id, "BTCUSDT")
    closes = binance_klines(symbol, "15m", 200)
    if len(closes) < 40:
        return None
    entry = float(closes[-1])
    direction, conf = infer_direction_from_closes(closes)
    lev = leverage_from_conf(conf)
    if direction == "LONG":
        tp, sl = entry * 1.025, entry * 0.985
    else:
        tp, sl = entry * 0.975, entry * 1.015
    stop_dist = abs(entry - sl)
    qty = (equity * 0.05) / (stop_dist if stop_dist > 0 else (0.001*entry))
    notional, cap = qty * entry, equity * lev
    if notional > cap:
        qty *= cap / (notional + 1e-9)
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
    return text

# ================ НОВОСТИ (RSS ≤ 4 часов, без повторов) ================
def parse_rss(url, max_items=20):
    out = []
    try:
        raw = requests.get(url, timeout=20).text
        root = ET.fromstring(raw)
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if not title or not link or not pub:
                continue
            try:
                # пример формата: "Sat, 18 Oct 2025 00:24:00 +0000"
                dt = datetime.datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S")
            except Exception:
                # попробуем ISO
                try:
                    dt = datetime.datetime.fromisoformat(pub.replace("Z","+00:00").split("+")[0])
                except Exception:
                    continue
            age_h = (datetime.datetime.utcnow() - dt).total_seconds() / 3600.0
            if age_h <= 4.0:  # только свежие
                out.append({"title": title, "link": link, "age_h": age_h})
    except Exception as e:
        print("[rss] parse error:", e)
    return out

def is_crypto_related(title):
    t = title.upper()
    return any(k in t for k in NEWS_KEYWORDS)

def collect_fresh_news():
    items = []
    for src in RSS_SOURCES:
        items.extend(parse_rss(src, max_items=30))
    # фильтр по крипто/монетам/инфлюенсерам
    items = [it for it in items if is_crypto_related(it["title"])]
    # анти-дубли по заголовку
    items = [it for it in items if not was_sent(it["title"])]
    # сортировка: самые свежие вперёд
    items.sort(key=lambda x: x["age_h"])
    # ограничим итоговый набор до 20 кандидатов (перед квотой)
    return items[:20]

def post_news_batch():
    q = get_quota()
    if q["news"] >= MAX_NEWS_PER_DAY:
        return
    # берём свежие
    items = collect_fresh_news()
    if not items:
        return
    # сколько можем отправить
    can = MAX_NEWS_PER_DAY - q["news"]
    batch = items[:can]
    for it in batch:
        title, link = it["title"], it["link"]
        try:
            bot.send_message(NEWS_CHAT_ID, f"📰 {title}\n🔗 {link}\n#CryptoNews")
            add_history(title)
            inc_quota("news")
            time.sleep(2)
        except Exception as e:
            print("[tg] news send error:", e)

# ================== АВТО-СИГНАЛЫ ==================
def pick_symbols_for_signals(n=2):
    # простой выбор: два верхних CG-ид среди списка (можешь заменить на «наибольшая дивергенция EMA»)
    universe = ["bitcoin","ethereum","solana","bnb","xrp","cardano","dogecoin","tron","toncoin","avalanche"]
    return universe[:n]

def post_signals_batch():
    q = get_quota()
    if q["signals"] >= MAX_SIGNALS_PER_DAY:
        return
    can = MAX_SIGNALS_PER_DAY - q["signals"]
    # один батч — максимум 2 за раз, чтобы не спамить
    per_run = min(2, can)
    picked = pick_symbols_for_signals(per_run)
    for cid in picked:
        try:
            s = build_signal_for_cgid(cg_id=cid, equity=1000.0)
            if s:
                bot.send_message(SIGNAL_CHAT_ID, s)
                inc_quota("signals")
                time.sleep(2)
        except Exception as e:
            print("[tg] signal send error:", e)

# ================== РАСПИСАНИЕ (schedule) ==================
def scheduler_thread():
    # каждые 3 часа — новости
    schedule.every(3).hours.do(post_news_batch)
    # каждые 6 часов — сигналы (в сумме до 4/день, контролирует квота)
    schedule.every(6).hours.do(post_signals_batch)
    # ежедневный сброс квот (на случай, если файл не сменился)
    schedule.every().day.at("00:05").do(lambda: save_json(QUOTA_FILE, {"date": today_str(), "news": 0, "signals": 0}))
    # первый запуск сразу после старта (чтобы не ждать часа)
    time.sleep(5)
    try:
        post_news_batch()
        post_signals_batch()
    except Exception as e:
        print("[init run] error:", e)
    # основной цикл
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print("[scheduler] error:", e)
        time.sleep(5)

# ================== КОМАНДЫ ==================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.reply_to(m, "👋 Привет! Я CryptoBot v6.5 AutoTrader — публикую свежие новости (≤4ч) и сигналы (EMA/RSI). Это не финсовет.")

@bot.message_handler(commands=["version"])
def cmd_version(m):
    q = get_quota()
    bot.reply_to(m, f"Version: v6.5 AutoTrader\nNews today: {q['news']}/{MAX_NEWS_PER_DAY}\nSignals today: {q['signals']}/{MAX_SIGNALS_PER_DAY}\nSources: CoinGecko + Binance + RSS")

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
    bot.reply_to(m, "✅ Проверил и опубликовал свежие новости (если были и лимит не исчерпан).")

@bot.message_handler(commands=["signal"])
def cmd_signal(m):
    post_signals_batch()
    bot.reply_to(m, "✅ Сигналы сгенерированы (если лимит не исчерпан).")

# ================== ЗАПУСК ==================
def run_scheduler():
    t = threading.Thread(target=scheduler_thread, daemon=True)
    t.start()

if __name__ == "__main__":
    print("✅ CryptoBot v6.5 AutoTrader starting...")
    run_scheduler()
    # на Render/Termux — запуск через polling
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
