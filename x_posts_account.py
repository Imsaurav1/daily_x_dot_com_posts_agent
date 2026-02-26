"""
Bluesky + Mastodon Daily Auto-Poster Agent
FIXED v3 — Key improvements:
- RSS feeds as PRIMARY news source (no rate limits, always fresh)
- DuckDuckGo as SECONDARY with proper delays + retry
- Topic rotation pool (25 topics, never repeats recently used)
- Post length enforced 200-260 chars with retry loop
- Rich, specific prompts with concrete examples
"""

import os
import sys
import time
import hashlib
import threading
import traceback
import random
import re
import xml.etree.ElementTree as ET
import schedule
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify
from pymongo import MongoClient
from groq import Groq

try:
    from duckduckgo_search import DDGS
    DDG_AVAILABLE = True
except ImportError:
    DDG_AVAILABLE = False

# ----------------------------------------------
# LOGGING
# ----------------------------------------------
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# ----------------------------------------------
# ENV VARIABLES
# ----------------------------------------------
def get_env(key, required=True):
    val = os.environ.get(key)
    if required and not val:
        log(f"Missing required env var: {key}")
        sys.exit(1)
    return val

BLUESKY_HANDLE    = get_env("BLUESKY_HANDLE")
BLUESKY_PASSWORD  = get_env("BLUESKY_PASSWORD")
MASTODON_INSTANCE = get_env("MASTODON_INSTANCE").rstrip("/")
MASTODON_TOKEN    = get_env("MASTODON_TOKEN")
MONGO_URI         = get_env("MONGO_URI")
GROQ_API_KEY      = get_env("GROQ_API_KEY")
RENDER_URL        = get_env("RENDER_URL", required=False) or "http://localhost:10000"
POST_TOPIC        = get_env("POST_TOPIC", required=False) or "AI, tech, science, business, future"

log(f"Env vars loaded. Topic: {POST_TOPIC}")

# ----------------------------------------------
# TOPIC ROTATION POOL — 25 diverse topics
# ----------------------------------------------
TOPIC_POOL = [
    "artificial intelligence new model release",
    "cybersecurity data breach hack",
    "electric vehicles EV battery range",
    "space exploration SpaceX NASA launch",
    "climate change renewable energy solar wind",
    "cryptocurrency bitcoin ethereum market",
    "robotics automation manufacturing",
    "quantum computing breakthrough",
    "big tech antitrust regulation lawsuit",
    "generative AI image video creation tools",
    "remote work hybrid office policy",
    "health tech AI medical diagnosis",
    "semiconductor chip TSMC Intel fab",
    "augmented reality VR mixed reality headset",
    "open source software developer tools",
    "data privacy surveillance government",
    "biotech CRISPR gene therapy trial",
    "fintech neobank digital payment",
    "gaming console cloud subscription",
    "streaming platform content war",
    "social media algorithm censorship",
    "startup unicorn funding Series A B",
    "self-driving autonomous vehicle safety",
    "nuclear fusion energy reactor",
    "drone delivery logistics automation",
]

# ----------------------------------------------
# RSS FEEDS — free, fast, no rate limits
# ----------------------------------------------
RSS_FEEDS = [
    "https://feeds.feedburner.com/TechCrunch",
    "https://www.wired.com/feed/rss",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.theverge.com/rss/index.xml",
    "https://venturebeat.com/feed/",
    "https://techcrunch.com/feed/",
    "https://www.sciencedaily.com/rss/top/technology.xml",
    "https://feeds.newscientist.com/science-news",
    "https://www.nasaspaceflight.com/feed/",
    "https://spaceflightnow.com/feed/",
    "https://rss.cnn.com/rss/cnn_tech.rss",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
]

# ----------------------------------------------
# FLASK
# ----------------------------------------------
app = Flask(__name__)

# ----------------------------------------------
# MONGODB
# ----------------------------------------------
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()
    db = mongo_client["social_poster"]
    posts_col = db["posts"]
    posts_col.create_index("content_hash", unique=True)
    log("MongoDB connected")
except Exception as e:
    log(f"MongoDB failed: {e}")
    sys.exit(1)

# ----------------------------------------------
# GROQ CLIENT
# ----------------------------------------------
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    test = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": "Say OK"}],
        max_tokens=5,
    )
    log(f"Groq connected: {test.choices[0].message.content.strip()}")
except Exception as e:
    log(f"Groq failed: {e}")
    sys.exit(1)


def groq_call(prompt, retries=3, temperature=0.85):
    for attempt in range(retries):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=temperature,
            )
            class _Resp:
                def __init__(self, text): self.text = text
            return _Resp(response.choices[0].message.content)
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower() or "limit" in err.lower():
                wait = 30 * (attempt + 1)
                log(f"  [Groq] Rate limited, waiting {wait}s (attempt {attempt+1}/{retries})...")
                time.sleep(wait)
                continue
            raise
    raise Exception("Groq rate limit exceeded after all retries.")


# ----------------------------------------------
# BLUESKY CLIENT
# ----------------------------------------------
class BlueskyClient:
    def __init__(self, handle, password):
        self.handle = handle
        self.password = password
        self.base_url = "https://bsky.social/xrpc"
        self.access_token = None
        self.did = None
        self._login()

    def _login(self):
        r = requests.post(
            f"{self.base_url}/com.atproto.server.createSession",
            json={"identifier": self.handle, "password": self.password},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        self.access_token = data["accessJwt"]
        self.did = data["did"]

    def post(self, text):
        self._login()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        r = requests.post(
            f"{self.base_url}/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json={
                "repo": self.did,
                "collection": "app.bsky.feed.post",
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": text,
                    "createdAt": now,
                }
            },
            timeout=15
        )
        r.raise_for_status()
        return r.json()


try:
    bluesky = BlueskyClient(BLUESKY_HANDLE, BLUESKY_PASSWORD)
    log(f"Bluesky connected as: @{BLUESKY_HANDLE}")
except Exception as e:
    log(f"Bluesky init failed: {e}")
    sys.exit(1)


# ----------------------------------------------
# MASTODON CLIENT
# ----------------------------------------------
class MastodonClient:
    def __init__(self, instance, token):
        self.instance = instance
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    def verify(self):
        r = requests.get(
            f"{self.instance}/api/v1/accounts/verify_credentials",
            headers=self.headers, timeout=10
        )
        r.raise_for_status()
        return r.json()

    def post(self, text):
        r = requests.post(
            f"{self.instance}/api/v1/statuses",
            headers=self.headers,
            json={"status": text, "visibility": "public"},
            timeout=15
        )
        r.raise_for_status()
        return r.json()


try:
    mastodon = MastodonClient(MASTODON_INSTANCE, MASTODON_TOKEN)
    me = mastodon.verify()
    log(f"Mastodon connected as: @{me.get('username')}")
except Exception as e:
    log(f"Mastodon init failed: {e}")
    sys.exit(1)


# ==============================================================
# NEWS FETCHING — Layer 1: RSS (fast, free, no rate limits)
# ==============================================================

def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_rss_feed(url, timeout=8):
    """Fetch one RSS/Atom feed. Returns list of {title, body, url}."""
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0; +https://example.com)"
        })
        r.raise_for_status()
        root = ET.fromstring(r.content)

        items = []
        # RSS 2.0
        for item in root.findall(".//item")[:10]:
            title = strip_html(item.findtext("title") or "")
            desc  = strip_html(item.findtext("description") or "")
            link  = (item.findtext("link") or "").strip()
            if title and len(desc) > 40:
                items.append({"title": title, "body": desc[:400], "url": link})

        # Atom fallback
        if not items:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("a:entry", ns)[:10]:
                title   = strip_html(entry.findtext("a:title", namespaces=ns) or "")
                summary = strip_html(entry.findtext("a:summary", namespaces=ns) or "")
                link_el = entry.find("a:link", ns)
                link    = link_el.get("href", "") if link_el is not None else ""
                if title and len(summary) > 40:
                    items.append({"title": title, "body": summary[:400], "url": link})

        return items
    except Exception as e:
        log(f"  [RSS] Error {url[:50]}: {e}")
        return []


def fetch_news_rss(topic_keyword, max_items=20):
    """
    Fetch from all RSS feeds in parallel threads.
    Score results by how well they match the topic keywords.
    """
    keyword_words = [w.lower() for w in re.split(r"[\s,]+", topic_keyword) if len(w) > 3]

    collected = []
    lock = threading.Lock()

    def fetch_one(url):
        items = fetch_rss_feed(url)
        with lock:
            collected.extend(items)

    feeds = RSS_FEEDS.copy()
    random.shuffle(feeds)
    threads = [threading.Thread(target=fetch_one, args=(url,)) for url in feeds[:10]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)

    # Score by keyword match
    scored = []
    for item in collected:
        text  = (item["title"] + " " + item["body"]).lower()
        score = sum(1 for kw in keyword_words if kw in text)
        scored.append((score, item))

    scored.sort(key=lambda x: -x[0])

    # Deduplicate by title
    seen  = set()
    final = []
    for score, item in scored:
        if item["title"] not in seen:
            seen.add(item["title"])
            final.append(item)
        if len(final) >= max_items:
            break

    # If < 5 on-topic items, pad with general items
    if len(final) < 5:
        for score, item in scored:
            if item["title"] not in seen:
                seen.add(item["title"])
                final.append(item)
            if len(final) >= max_items:
                break

    log(f"  [RSS] {len(final)} items (topic: '{topic_keyword[:40]}')")
    return final


# ==============================================================
# NEWS FETCHING — Layer 2: DuckDuckGo (rate-limit aware)
# ==============================================================

def fetch_news_ddg(topic_keyword, max_results=5):
    """DDG search with mandatory delays to avoid 202 rate limits."""
    if not DDG_AVAILABLE:
        return []
    today = datetime.now().strftime("%B %Y")
    query = f"{topic_keyword} news {today}"
    for attempt in range(3):
        delay = 5 + attempt * 10  # 5s, 15s, 25s between attempts
        log(f"  [DDG] Waiting {delay}s before search (attempt {attempt+1}/3)...")
        time.sleep(delay)
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            items = []
            for r in results:
                title = (r.get("title") or "").strip()
                body  = (r.get("body") or "").strip()
                href  = r.get("href", "")
                if title and len(body) > 50:
                    items.append({"title": title, "body": body[:400], "url": href})
            log(f"  [DDG] {len(items)} results for: {query[:50]}")
            return items
        except Exception as e:
            err = str(e)
            if "Ratelimit" in err or "202" in err:
                log(f"  [DDG] Rate limited on attempt {attempt+1}, backing off...")
                continue
            log(f"  [DDG] Error: {e}")
            return []
    log("  [DDG] All retries exhausted.")
    return []


def fetch_news_for_topic(topic):
    """Master fetcher: RSS first, DDG supplement if needed."""
    items = fetch_news_rss(topic)

    if len(items) < 3:
        log(f"  [News] Only {len(items)} RSS items, trying DDG...")
        ddg = fetch_news_ddg(topic)
        existing = {i["title"] for i in items}
        for d in ddg:
            if d["title"] not in existing:
                items.append(d)
                existing.add(d["title"])

    log(f"  [News] {len(items)} total items for: {topic}")
    return items


# ==============================================================
# TOPIC ROTATION
# ==============================================================

def get_used_topics(limit=22):
    recent = posts_col.find(
        {"topic": {"$exists": True, "$ne": ""}},
        {"topic": 1}
    ).sort("posted_at", -1).limit(limit)
    return [p.get("topic", "") for p in recent]


def pick_fresh_topic():
    used = set(get_used_topics(limit=22))
    available = [t for t in TOPIC_POOL if t not in used]
    if not available:
        available = TOPIC_POOL
    chosen = random.choice(available)
    log(f"  [Topic] Selected: {chosen}")
    return chosen


# ==============================================================
# AI: PICK BEST NEWS ITEM
# ==============================================================

def pick_best_news(news_items, past_posts):
    if not news_items:
        return {}

    past_texts = [p.get("text", "")[:80] for p in past_posts]
    news_list  = "\n\n".join(
        f"[{i+1}] {n['title']}\n    {n['body'][:250]}"
        for i, n in enumerate(news_items[:15])
    )
    past_str = "\n".join(f"- {p}" for p in past_texts) if past_texts else "None."

    prompt = f"""You are a social media editor. Pick the SINGLE BEST news item below.

NEWS ITEMS:
{news_list}

ALREADY POSTED (avoid similar topics):
{past_str}

Choose the item that:
1. Contains a specific concrete fact — a company name, country, number, or named person
2. Would surprise or engage a general tech/science audience
3. Has NOT been covered in past posts
4. Is recent and newsworthy

Reply with ONLY the item number. Example: 3"""

    response = groq_call(prompt, temperature=0.3)
    pick = response.text.strip().strip(".")
    try:
        idx = int(pick) - 1
        if 0 <= idx < len(news_items):
            log(f"  [Pick] #{idx+1}: {news_items[idx]['title'][:65]}")
            return news_items[idx]
    except Exception:
        pass
    log("  [Pick] Fallback to item 0")
    return news_items[0]


# ==============================================================
# AI: WRITE FULL-LENGTH POST
# ==============================================================

def write_post(news_context, topic, past_posts):
    past_texts = [p.get("text", "")[:80] for p in past_posts]
    past_str   = "\n".join(f"- {t}" for t in past_texts) if past_texts else "None yet."

    prompt = f"""You are a viral social media writer for a tech/science/innovation account.

Write ONE post based on this news:
{news_context}

PREVIOUSLY POSTED — do NOT repeat these topics or phrasings:
{past_str}

══════════════════════════════════════
STRICT RULES (violating any = failure):
══════════════════════════════════════

RULE 1 — LENGTH: Post MUST be between 230 and 260 characters total.
Count every character: letters, spaces, punctuation, hashtags.
If it is shorter than 230, you FAILED. If longer than 260, you FAILED.

RULE 2 — STRUCTURE:
• Line 1: A HOOK — one bold, specific, surprising sentence. Name a company, country, person, or real number.
• Lines 2–3: 2 sentences expanding the story. Answer "why does this matter?"
• Final line: 2–3 hashtags separated by spaces.

RULE 3 — SPECIFICITY: You must reference at least one real named entity (company/person/place/technology).
BAD: "AI is changing the future of work."
GOOD: "Google's DeepMind just trained an AI that solved protein folding in 90 seconds."

RULE 4 — NO URLS.

RULE 5 — For uncertain stats use: "reportedly", "could", "may", "is said to".

══════════════════════════════════════
GOOD EXAMPLES — all 230-260 chars:
══════════════════════════════════════

"OpenAI's GPT-5 reportedly scores in the top 5% of humans on bar exams. Lawyers aren't replaced yet — but their tools are changing fast. Is your industry ready for this? #AI #FutureOfWork #GPT5"
(195 chars — TOO SHORT, don't do this)

"SpaceX just nailed Starship's first full orbital test flight, making it the most powerful rocket ever flown — twice the thrust of the Saturn V. If reusable heavy lift becomes routine, the cost of reaching orbit could drop by 90%. Mars is getting closer. #SpaceX #Starship #Space"
(280 chars — TOO LONG, don't do this)

PERFECT EXAMPLE (247 chars):
"NVIDIA quietly surpassed Apple as the world's most valuable company this week. A chip designer — not a phone or software giant — now runs the global economy. The AI hardware gold rush is real, and NVIDIA is sitting on the motherlode. #NVIDIA #AI #Chips"

PERFECT EXAMPLE (241 chars):
"South Korea announced a 4-day work week for all public sector employees starting 2026. Early pilots show productivity holds steady or improves. Over 40 countries are watching closely — your employer might be next. #WorkLife #FutureOfWork #Korea"

══════════════════════════════════════

Now write ONE post. Return ONLY the post text. No quotes around it. No labels. No explanation. Just the post."""

    log("  [Write] Calling Groq...")
    response = groq_call(prompt, temperature=0.88)
    post = response.text.strip().strip('"').strip("'")

    # Hard cap
    if len(post) > 280:
        post = post[:277] + "..."

    log(f"  [Write] {len(post)} chars: {post[:100]}...")
    return post


# ==============================================================
# GENERATE POST (full pipeline)
# ==============================================================

def generate_post():
    topic     = pick_fresh_topic()
    log(f"  [Generate] Topic: {topic}")

    news_items = fetch_news_for_topic(topic)
    past_posts = list(
        posts_col.find({}, {"text": 1, "topic": 1}).sort("posted_at", -1).limit(30)
    )

    if news_items:
        best         = pick_best_news(news_items, past_posts)
        news_context = (
            f"HEADLINE: {best.get('title', '')}\n"
            f"DETAILS: {best.get('body', '')[:400]}"
        )
    else:
        log(f"  [Generate] No news. Using AI knowledge for: {topic}")
        news_context = (
            f"Write about the single most important or surprising recent development "
            f"in: {topic}. Reference a specific real company, country, number, or "
            f"named technology. Do not invent statistics."
        )

    post_text = write_post(news_context, topic, past_posts)
    return post_text, topic


# ==============================================================
# VERIFY POST
# ==============================================================

def verify_post(text):
    prompt = f"""Evaluate this social media post for content policy:

POST: "{text}"

Mark VALID if:
- It is a general opinion, trend observation, or factual commentary
- It uses hedging language like "reportedly", "may", "could"
- It references real companies, technologies, or events plausibly

Mark INVALID ONLY if:
- It contains demonstrably false facts stated with absolute certainty
- It contains hate speech, harassment, or harmful content
- It is completely incoherent or nonsensical

Reply with ONLY:
VALID: <one sentence reason>
INVALID: <one sentence reason>"""

    response = groq_call(prompt, temperature=0.2)
    verdict  = response.text.strip()
    is_valid = verdict.upper().startswith("VALID")
    log(f"  [Verify] {verdict[:80]}")
    return is_valid, verdict


# ==============================================================
# HELPERS
# ==============================================================

def hash_content(text):
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()

def is_duplicate(text):
    return posts_col.find_one({"content_hash": hash_content(text)}) is not None

def save_post(text, verified, results, topic=""):
    try:
        posts_col.insert_one({
            "content_hash": hash_content(text),
            "text":         text,
            "topic":        topic,
            "char_count":   len(text),
            "verified":     verified,
            "results":      results,
            "posted_at":    datetime.now(timezone.utc),
        })
    except Exception as e:
        log(f"  [DB Error] {e}")


# ==============================================================
# MAIN POST FLOW
# ==============================================================

def create_and_post():
    log("=" * 55)
    log("Starting post cycle...")
    result = {
        "success": False, "error": "", "post": "", "topic": "",
        "char_count": 0,
        "bluesky":  {"posted": False, "error": "", "url": ""},
        "mastodon": {"posted": False, "error": "", "url": ""},
    }

    try:
        for attempt in range(1, 7):
            log(f"--- Attempt {attempt}/6 ---")

            try:
                post_text, topic = generate_post()
            except Exception as e:
                result["error"] = f"Generation failed: {e}"
                log(f"  {result['error']}\n{traceback.format_exc()}")
                time.sleep(5)
                continue

            char_count         = len(post_text)
            result["post"]      = post_text
            result["topic"]     = topic
            result["char_count"] = char_count

            # Reject short posts — always retry
            if char_count < 200:
                result["error"] = f"Post too short ({char_count} chars) — retrying"
                log(f"  {result['error']}")
                time.sleep(2)
                continue

            if is_duplicate(post_text):
                result["error"] = "Duplicate — regenerating"
                log(f"  {result['error']}")
                continue

            try:
                is_valid, reason = verify_post(post_text)
            except Exception as e:
                result["error"] = f"Verify error: {e}"
                log(f"  {result['error']}")
                continue

            if not is_valid:
                result["error"] = f"Rejected: {reason}"
                log(f"  {result['error']}")
                continue

            # Post to Bluesky
            log(f"  Posting to Bluesky ({char_count} chars)...")
            try:
                bs_resp = bluesky.post(post_text)
                result["bluesky"]["posted"] = True
                result["bluesky"]["url"]    = bs_resp.get("uri", "")
                log(f"  Bluesky OK: {bs_resp.get('uri','')}")
            except Exception as e:
                result["bluesky"]["error"] = str(e)
                log(f"  Bluesky failed: {e}")

            # Post to Mastodon
            log("  Posting to Mastodon...")
            try:
                mt_resp = mastodon.post(post_text)
                result["mastodon"]["posted"] = True
                result["mastodon"]["url"]    = mt_resp.get("url", "")
                log(f"  Mastodon OK: {mt_resp.get('url','')}")
            except Exception as e:
                result["mastodon"]["error"] = str(e)
                log(f"  Mastodon failed: {e}")

            if result["bluesky"]["posted"] or result["mastodon"]["posted"]:
                save_post(post_text, verified=True, results={
                    "bluesky":  result["bluesky"],
                    "mastodon": result["mastodon"],
                }, topic=topic)
                result["success"] = True
                result["error"]   = ""
                log(f"Post cycle complete! Topic: {topic} | {char_count} chars")
            else:
                result["error"] = (
                    f"Both platforms failed — "
                    f"BS: {result['bluesky']['error']} | "
                    f"MT: {result['mastodon']['error']}"
                )
                log(result["error"])

            return result

        log("All 6 attempts exhausted.")
        return result

    except Exception as e:
        result["error"] = f"Unexpected: {str(e)}"
        log(f"{result['error']}\n{traceback.format_exc()}")
        return result


# ==============================================================
# SCHEDULER
# ==============================================================

def run_scheduler():
    schedule.every().day.at("02:00").do(create_and_post)
    schedule.every().day.at("05:00").do(create_and_post)
    schedule.every().day.at("08:00").do(create_and_post)
    schedule.every().day.at("11:00").do(create_and_post)
    schedule.every().day.at("14:00").do(create_and_post)
    schedule.every().day.at("16:00").do(create_and_post)
    schedule.every().day.at("17:00").do(create_and_post)
    schedule.every().day.at("18:00").do(create_and_post)
    schedule.every().day.at("20:00").do(create_and_post)
    schedule.every().day.at("21:00").do(create_and_post)
    schedule.every().day.at("23:00").do(create_and_post)
    log("[Scheduler] 11 posts/day scheduled.")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ==============================================================
# SELF-PING
# ==============================================================

def self_ping():
    time.sleep(90)
    while True:
        try:
            r = requests.get(f"{RENDER_URL}/health", timeout=10)
            log(f"[Ping] {r.status_code}")
        except Exception as e:
            log(f"[Ping] Failed: {e}")
        time.sleep(14 * 60)


# ==============================================================
# FLASK ROUTES
# ==============================================================

@app.route("/")
def index():
    total    = posts_col.count_documents({})
    bs_count = posts_col.count_documents({"results.bluesky.posted": True})
    mt_count = posts_col.count_documents({"results.mastodon.posted": True})
    latest   = list(
        posts_col.find({}, {"text": 1, "topic": 1, "char_count": 1, "posted_at": 1, "_id": 0})
        .sort("posted_at", -1).limit(5)
    )
    return jsonify({
        "status":          "running",
        "topic_pool_size": len(TOPIC_POOL),
        "rss_feeds":       len(RSS_FEEDS),
        "stats":           {"total": total, "bluesky": bs_count, "mastodon": mt_count},
        "latest_posts":    latest,
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

@app.route("/post-now")
def post_now():
    log("[/post-now] Manual trigger")
    result = create_and_post()
    return jsonify(result)

@app.route("/posts")
def list_posts():
    posts = list(posts_col.find({}, {"_id": 0}).sort("posted_at", -1).limit(50))
    return jsonify(posts)

@app.route("/topics")
def show_topics():
    used      = get_used_topics(limit=25)
    available = [t for t in TOPIC_POOL if t not in used]
    return jsonify({"used_recently": used, "available_now": available})

@app.route("/test-news")
def test_news():
    topic = pick_fresh_topic()
    items = fetch_news_for_topic(topic)
    return jsonify({"topic": topic, "count": len(items), "sample": items[:3]})

@app.route("/test-rss")
def test_rss():
    results = {}
    for url in RSS_FEEDS[:5]:
        items = fetch_rss_feed(url, timeout=6)
        results[url] = {"count": len(items), "first_title": items[0]["title"] if items else "—"}
    return jsonify(results)

@app.route("/test-bluesky")
def test_bluesky():
    try:
        bluesky._login()
        return jsonify({"ok": True, "handle": BLUESKY_HANDLE, "did": bluesky.did})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/test-mastodon")
def test_mastodon():
    try:
        data = mastodon.verify()
        return jsonify({"ok": True, "username": data.get("username")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/test-groq")
def test_groq():
    try:
        r = groq_call("Say: Groq is working fine.")
        return jsonify({"ok": True, "response": r.text.strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ==============================================================
# ENTRY POINT
# ==============================================================

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    log(f"[Server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
