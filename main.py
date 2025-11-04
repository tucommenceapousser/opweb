# app.py
# Flask app + OpenAI summarization + Telegram polling integration
import os
import time
import threading
import sqlite3
from datetime import datetime
from urllib.parse import urlparse
from io import StringIO
import csv

import requests
import feedparser
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response
from dotenv import load_dotenv
import openai

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_POLLING = os.getenv("TELEGRAM_POLLING", "1") == "1"  # default polling enabled
TELEGRAM_POLL_INTERVAL = float(os.getenv("TELEGRAM_POLL_INTERVAL", "5.0"))

if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in .env")
if not TELEGRAM_BOT_TOKEN:
    print("Warning: TELEGRAM_BOT_TOKEN not set — Telegram integration disabled.")

openai.api_key = OPENAI_API_KEY

DB_PATH = "data/feeds.db"
os.makedirs("data", exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change_this_secret_for_prod")

USER_AGENT = "HackAwarenessBot/1.0 (+https://example.local/)"

# KEYWORDS ciblés (tu peux éditer)
KEYWORDS = [
    "anonymous", "op unite", "opunite", "op unity", "opunity",
    "op israel", "op russia", "op paris", "opwiki", "ophack",
    "operation anonymous", "claimed operation", "hacktivism", "hacktiviste",
]

DEFAULT_FEEDS = [
    "https://www.wired.com/feed/category/security/latest/rss",
    "https://krebsonsecurity.com/feed/",
    "https://nakedsecurity.sophos.com/feed/",
    "https://www.zdnet.com/topic/security/rss.xml",
    # ajoute d'autres flux fiables
]

# --- DB helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY,
        url TEXT UNIQUE,
        source TEXT,
        title TEXT,
        published_at TEXT,
        content TEXT,
        summary TEXT,
        category TEXT,
        confidence REAL,
        fetched_at TEXT
    )""")
    conn.commit()
    conn.close()

def save_article(item):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO articles (url, source, title, published_at, content, summary, category, confidence, fetched_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item["url"],
        item.get("source"),
        item.get("title"),
        item.get("published_at"),
        item.get("content"),
        item.get("summary"),
        item.get("category"),
        item.get("confidence"),
        datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()

# --- Fetch / parse web ---
def fetch_rss(url):
    parsed = feedparser.parse(url)
    items = []
    for e in parsed.entries:
        link = e.get("link") or e.get("id")
        title = e.get("title", "")
        published = e.get("published") or e.get("updated") or ""
        items.append({"url": link, "title": title, "published_at": published, "raw": e})
    return items

def fetch_page_text(url, timeout=10):
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
    except Exception as ex:
        app.logger.debug(f"fetch_page_text error for {url}: {ex}")
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for s in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        s.decompose()
    article = soup.find("article")
    if article:
        text = " ".join(p.get_text(separator=" ", strip=True) for p in article.find_all(["p","h1","h2","h3","li"]))
    else:
        body = soup.find("body") or soup
        text = " ".join(p.get_text(separator=" ", strip=True) for p in body.find_all(["p","h1","h2","h3","li"]))
    return text[:25000]

# --- OpenAI summarization/classification ---
def summarize_and_classify(text, url):
    prompt = f"""
You are a security/OSINT summarizer. Given the following publicly available article text and its URL,
produce a JSON object with these fields:
- summary: one short paragraph (max 70 words) describing key facts.
- category: one of ["news","analysis","claimed_operation","historical","opinion","other"] where "claimed_operation" indicates the article explicitly reports an ongoing hacktivist operation by name.
- confidence: a float between 0.0 and 1.0 indicating confidence the article actually describes an ongoing publicly-declared hacktivist operation.
Return ONLY valid JSON.

URL: {url}

ARTICLE TEXT:
\"\"\"{text}\"\"\"
"""
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role":"system","content":"You are accurate and concise."},
                      {"role":"user","content":prompt}],
            max_tokens=400,
            temperature=0.0
        )
        out = resp.choices[0].message.content.strip()
        import json
        parsed = json.loads(out)
        summary = parsed.get("summary", "")[:1000]
        category = parsed.get("category", "other")
        confidence = float(parsed.get("confidence", 0.0))
        return summary, category, confidence
    except Exception as e:
        app.logger.error(f"OpenAI error: {e}")
        return ("(summary failed)", "other", 0.0)

# --- Fetch action (RSS + keywords) ---
@app.route("/", methods=["GET"])
def index():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, title, source, published_at, summary, category, confidence FROM articles ORDER BY fetched_at DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    return render_template("index.html", articles=rows)

@app.route("/fetch", methods=["POST"])
def fetch_action():
    feeds = list(DEFAULT_FEEDS)
    custom = request.form.get("custom_feed", "").strip()
    if custom:
        feeds.append(custom)
    fetched = 0
    saved = 0
    for f in feeds:
        app.logger.info(f"Parsing feed {f}")
        try:
            items = fetch_rss(f)
        except Exception:
            items = []
        for it in items[:10]:
            url = it["url"]
            content = fetch_page_text(url)
            if not content:
                continue
            combined = (it.get("title","") + " " + content).lower()
            if not any(k.lower() in combined for k in KEYWORDS):
                continue
            summary, category, confidence = summarize_and_classify(content, url)
            save_article({
                "url": url,
                "source": urlparse(f).netloc,
                "title": it.get("title", "") or "",
                "published_at": it.get("published_at") or "",
                "content": content,
                "summary": summary,
                "category": category,
                "confidence": confidence
            })
            saved += 1
            fetched += 1
            time.sleep(1.0)
    flash(f"Parcouru {fetched} items, sauvegardé {saved} mentions pertinentes.", "success")
    return redirect(url_for("index"))

@app.route("/article/<int:aid>", methods=["GET"])
def article_view(aid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, url, source, title, published_at, content, summary, category, confidence FROM articles WHERE id=?",(aid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        flash("Article not found", "danger")
        return redirect(url_for("index"))
    obj = {
        "id": row[0], "url": row[1], "source": row[2], "title": row[3],
        "published_at": row[4], "content": row[5], "summary": row[6],
        "category": row[7], "confidence": row[8]
    }
    return render_template("article.html", a=obj)

# --- Advanced search + CSV export (comme fourni précédemment) ---
@app.route("/advanced", methods=["GET"])
def advanced_search_page():
    q = request.args.get("q","")
    source = request.args.get("source","")
    cat = request.args.get("category","")
    try:
        min_conf = float(request.args.get("min_conf", "0") or 0)
    except:
        min_conf = 0.0
    date_from = request.args.get("date_from","")
    date_to = request.args.get("date_to","")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    sql = "SELECT id, title, source, published_at, summary, category, confidence, url FROM articles WHERE 1=1"
    params = []
    if q:
        sql += " AND (lower(title) LIKE ? OR lower(content) LIKE ? OR lower(summary) LIKE ?)"
        qq = f"%{q.lower()}%"
        params += [qq, qq, qq]
    if source:
        sql += " AND lower(source)=?"
        params.append(source.lower())
    if cat:
        sql += " AND lower(category)=?"
        params.append(cat.lower())
    if min_conf:
        sql += " AND confidence>=?"
        params.append(min_conf)
    if date_from:
        sql += " AND published_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND published_at <= ?"
        params.append(date_to)
    sql += " ORDER BY published_at DESC LIMIT 500"
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return render_template("advanced.html", rows=rows, q=q, source=source, category=cat, min_conf=min_conf, date_from=date_from, date_to=date_to)

@app.route("/export_csv", methods=["GET"])
def export_csv():
    q = request.args.get("q","")
    source = request.args.get("source","")
    cat = request.args.get("category","")
    try:
        min_conf = float(request.args.get("min_conf", "0") or 0)
    except:
        min_conf = 0.0
    date_from = request.args.get("date_from","")
    date_to = request.args.get("date_to","")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    sql = "SELECT title, source, published_at, summary, category, confidence, url FROM articles WHERE 1=1"
    params = []
    if q:
        sql += " AND (lower(title) LIKE ? OR lower(content) LIKE ? OR lower(summary) LIKE ?)"
        qq = f"%{q.lower()}%"
        params += [qq, qq, qq]
    if source:
        sql += " AND lower(source)=?"
        params.append(source.lower())
    if cat:
        sql += " AND lower(category)=?"
        params.append(cat.lower())
    if min_conf:
        sql += " AND confidence>=?"
        params.append(min_conf)
    if date_from:
        sql += " AND published_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND published_at <= ?"
        params.append(date_to)
    sql += " ORDER BY published_at DESC LIMIT 5000"
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(["title","source","published_at","summary","category","confidence","url"])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6]])

    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = f"attachment; filename=export_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv"
    output.headers["Content-type"] = "text/csv; charset=utf-8"
    return output

# --- Telegram integration (polling based) ---
def process_telegram_message(chat_id, chat_title, username, message_id, text, date_ts):
    """
    Called when a Telegram message is received by the bot.
    We check keywords and if matched, summarize + save to DB.
    """
    if not text:
        return False
    combined = (chat_title or "") + " " + (username or "") + " " + text
    lower = combined.lower()
    if not any(k.lower() in lower for k in KEYWORDS):
        return False

    # create a pseudo-URL to reference message (not a public url unless channel has username)
    if username:
        msg_url = f"https://t.me/{username}/{message_id}"
    else:
        msg_url = f"tg://{chat_id}/{message_id}"

    # optionally summarize via OpenAI
    summary, category, confidence = summarize_and_classify(text, msg_url)

    save_article({
        "url": msg_url,
        "source": "telegram",
        "title": f"{chat_title or username or 'Telegram'}",
        "published_at": datetime.utcfromtimestamp(date_ts).isoformat() if date_ts else datetime.utcnow().isoformat(),
        "content": text,
        "summary": summary,
        "category": category,
        "confidence": confidence
    })
    app.logger.info(f"Saved telegram msg {msg_url} (conf={confidence})")
    return True

def telegram_poller_loop(token, poll_interval=5.0):
    if not token:
        return
    offset = None
    base = f"https://api.telegram.org/bot{token}"
    while True:
        try:
            params = {"timeout": 20, "limit": 50}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"{base}/getUpdates", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                app.logger.warning("Telegram getUpdates returned not ok")
                time.sleep(poll_interval)
                continue
            updates = data.get("result", [])
            for u in updates:
                offset = u["update_id"] + 1
                # message types: message, channel_post, edited_message, etc.
                msg = u.get("message") or u.get("channel_post") or u.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                chat_title = chat.get("title") or chat.get("first_name") or chat.get("username")
                username = chat.get("username")
                message_id = msg.get("message_id")
                date_ts = msg.get("date")
                text = msg.get("text") or msg.get("caption") or ""
                # handle entities with text parts concatenated if needed
                process_telegram_message(chat_id, chat_title, username, message_id, text, date_ts)
            time.sleep(poll_interval)
        except Exception as e:
            app.logger.error(f"Telegram poller error: {e}")
            time.sleep(poll_interval)

# Start poller thread on app start
def start_telegram_thread():
    if TELEGRAM_BOT_TOKEN and TELEGRAM_POLLING:
        t = threading.Thread(target=telegram_poller_loop, args=(TELEGRAM_BOT_TOKEN, TELEGRAM_POLL_INTERVAL), daemon=True)
        t.start()
        app.logger.info("Started Telegram poller thread")

# --- minimal templates served inline if missing, else use files ---
# It's expected you create templates/index.html, article.html, advanced.html as in previous messages.

if __name__ == "__main__":
    init_db()
    start_telegram_thread()
    app.run(debug=True, host="0.0.0.0", port=5000)
