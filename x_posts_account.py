"""
Bluesky + Mastodon Daily Auto-Poster Agent
FIXED VERSION:
- Diverse topic rotation (not just AI/startups)
- Full character usage (240-260 chars target)
- Strong deduplication by topic keyword
- Better news search with varied queries
- Richer, more engaging post prompts
"""

import os
import sys
import time
import hashlib
import threading
import traceback
import random
import schedule
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify
from pymongo import MongoClient
from groq import Groq
from duckduckgo_search import DDGS

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
# TOPIC ROTATION POOL
# These ensure every post covers a DIFFERENT angle
# ----------------------------------------------
TOPIC_POOL = [
    "artificial intelligence breakthroughs",
    "tech startup funding raises",
    "cybersecurity data breach",
    "electric vehicles EV news",
    "space exploration NASA SpaceX",
    "climate change renewable energy",
    "cryptocurrency bitcoin blockchain",
    "social media platform update",
    "robotics automation jobs",
    "quantum computing research",
    "big tech regulation antitrust",
    "generative AI tools new release",
    "remote work future of work",
    "health tech medical AI",
    "chip semiconductor shortage",
    "augmented reality VR Apple Vision",
    "open source software release",
    "data privacy surveillance",
    "biotech gene editing CRISPR",
    "fintech banking disruption",
    "developer tools programming",
    "gaming industry news",
    "streaming platform wars",
    "supply chain logistics tech",
    "edtech online learning AI",
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
# GROQ
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

# ----------------------------------------------
# GROQ CALL HELPER
# ----------------------------------------------
def gemini_call(prompt, retries=3):
    for attempt in range(retries):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.85,  # Higher = more creative/varied
            )
            class _Resp:
                def __init__(self, text):
                    self.text = text
            return _Resp(response.choices[0].message.content)
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower() or "limit" in err.lower():
                wait = 30 * (attempt + 1)
                log(f"  [Groq] Rate limited, waiting {wait}s before retry {attempt+1}/{retries}...")
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
                    "createdAt": now
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

# ----------------------------------------------
# HELPERS
# ----------------------------------------------
def search_web(query, max_results=5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        log(f"  [Search] {len(results)} results for: {query[:60]}")
        return results
    except Exception as e:
        log(f"  [Search Error] {e}")
        return []

def hash_content(text):
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()

def is_duplicate(text):
    return posts_col.find_one({"content_hash": hash_content(text)}) is not None

def save_post(text, verified, results, topic=""):
    try:
        posts_col.insert_one({
            "content_hash": hash_content(text),
            "text": text,
            "topic": topic,
            "verified": verified,
            "results": results,
            "posted_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        log(f"  [DB Error] {e}")

def get_past_snippets(limit=30):
    recent = posts_col.find({}, {"text": 1, "topic": 1}).sort("posted_at", -1).limit(limit)
    return list(recent)

def get_used_topics(limit=20):
    """Return recently used topic strings to avoid repetition."""
    recent = posts_col.find({"topic": {"$exists": True, "$ne": ""}}, {"topic": 1}).sort("posted_at", -1).limit(limit)
    return [p.get("topic", "") for p in recent]

def pick_fresh_topic():
    """Pick a topic from TOPIC_POOL that hasn't been used recently."""
    used = get_used_topics(limit=len(TOPIC_POOL) - 1)
    available = [t for t in TOPIC_POOL if t not in used]
    if not available:
        # All topics used — reset and pick randomly
        available = TOPIC_POOL
    chosen = random.choice(available)
    log(f"  [Topic] Selected: {chosen}")
    return chosen

# ----------------------------------------------
# STEP 1: FETCH TODAY'S REAL NEWS FOR A TOPIC
# ----------------------------------------------
def fetch_news_for_topic(topic):
    today = datetime.now().strftime("%B %d %Y")
    queries = [
        f"{topic} news {today}",
        f"{topic} latest update 2025",
        f"{topic} breaking news today",
    ]
    all_news = []
    for q in queries:
        results = search_web(q, max_results=5)
        for r in results:
            title = r.get("title", "").strip()
            body = r.get("body", "").strip()
            href = r.get("href", "")
            if title and body and len(body) > 50:
                all_news.append({
                    "title": title,
                    "body": body[:400],
                    "url": href,
                })

    # Deduplicate by title
    seen = set()
    unique = []
    for n in all_news:
        if n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)

    log(f"  [News] {len(unique)} unique news items for topic: {topic}")
    return unique

# ----------------------------------------------
# STEP 2: AI PICKS BEST NEWS
# ----------------------------------------------
def pick_best_news(news_items, past_snippets):
    if not news_items:
        return {}

    past_texts = [p.get("text", "")[:80] for p in past_snippets]
    news_list = "\n\n".join(
        f"[{i+1}] {n['title']}\n    {n['body'][:300]}"
        for i, n in enumerate(news_items[:15])
    )
    past_str = "\n".join(f"- {p}" for p in past_texts) if past_texts else "None."

    prompt = f"""You are a social media news curator. Pick the BEST news to post about today.

NEWS ITEMS:
{news_list}

ALREADY POSTED (avoid similar topics):
{past_str}

Choose the ONE news item that:
- Is most interesting, surprising, or impactful for a general tech/science audience
- Has NOT been covered in past posts
- Would generate curiosity, debate, or shares on social media
- Contains a concrete fact, number, name, or development — not just a vague trend

Reply with ONLY the number. Example: 4"""

    log("  [Pick] AI selecting best news item...")
    response = gemini_call(prompt)
    pick = response.text.strip().strip(".")
    try:
        idx = int(pick) - 1
        if 0 <= idx < len(news_items):
            chosen = news_items[idx]
            log(f"  [Pick] Selected: {chosen['title'][:70]}...")
            return chosen
    except Exception:
        pass
    log("  [Pick] Fallback to first item")
    return news_items[0] if news_items else {}

# ----------------------------------------------
# STEP 3: WRITE A RICH, FULL-LENGTH POST
# ----------------------------------------------
def generate_post():
    # Pick a fresh topic each time
    topic = pick_fresh_topic()

    log(f"  [Generate] Fetching news for: {topic}")
    news_items = fetch_news_for_topic(topic)
    past = get_past_snippets()

    if not news_items:
        log("  [Generate] No news found, generating from topic knowledge")
        news_context = f"Write about the latest important development in: {topic}"
        headline_title = topic
    else:
        best = pick_best_news(news_items, past)
        if not best:
            news_context = f"Write about the latest important development in: {topic}"
            headline_title = topic
        else:
            headline_title = best.get("title", topic)
            news_context = f"HEADLINE: {best.get('title', '')}\nDETAILS: {best.get('body', '')}"
            log(f"  [Generate] Writing post for: {headline_title[:70]}...")

    past_texts = [p.get("text", "")[:80] for p in past]
    past_str = "\n".join(f"- {t}" for t in past_texts) if past_texts else "None yet."

    prompt = f"""You are a viral social media writer for a tech/science/innovation account.

Write a post based on this real news:
{news_context}

PREVIOUSLY POSTED (do NOT repeat these topics or phrasings):
{past_str}

STRICT RULES:
1. LENGTH: The post MUST be between 230 and 260 characters total (including hashtags). Count carefully.
2. STRUCTURE: 
   - Line 1: A punchy HOOK — a surprising fact, bold statement, or provocative question from the news
   - Line 2-3: 2-3 sentences expanding the story with specific details (who, what, why it matters)
   - Last line: 2-3 relevant hashtags
3. SPECIFICITY: Name the actual company, country, technology, or number from the news. NO vague language like "AI is changing things."
4. TONE: Confident, curious, slightly opinionated. Think tech journalist meets Twitter power user.
5. NO URLS in the post.
6. Use soft language for unverified numbers: "reportedly", "could", "may", "is said to".

GOOD EXAMPLES (notice they are long, specific, and use the full character budget):
"OpenAI just released GPT-5 — and it reportedly scores higher than 95% of humans on bar exams. Lawyers, doctors, engineers: the race to stay ahead just got real. The question isn't if AI replaces knowledge workers, but when. #AI #FutureOfWork #GPT5"

"SpaceX's Starship finally completed its first full orbital test flight. It's the most powerful rocket ever flown — 2x the thrust of the Saturn V. If this succeeds commercially, Mars stops being a dream and starts being a timeline. #SpaceX #Starship #Space"

"South Korea just announced a 4-day work week for all public sector workers starting 2026. Productivity studies say output stays the same or improves. 40 countries are watching. The 5-day week may finally be ending. #FutureOfWork #WorkLifeBalance #Innovation"

Now write ONE post using the news provided. Return ONLY the post text. Nothing else. No quotes around it."""

    log("  [Generate] Writing post with Groq...")
    response = gemini_call(prompt)
    post = response.text.strip().strip('"').strip("'")

    # Enforce hard character limit
    if len(post) > 280:
        post = post[:277] + "..."

    # Warn if too short
    if len(post) < 180:
        log(f"  [Generate] WARNING: Post too short ({len(post)} chars). Will retry on next cycle.")

    log(f"  [Generate] Done ({len(post)} chars): {post[:100]}...")
    return post, topic

# ----------------------------------------------
# STEP 4: VERIFY POST
# ----------------------------------------------
def verify_post(text):
    log("  [Verify] Fact-checking...")
    search_results = search_web(text[:60], max_results=3)
    evidence = "\n".join(
        f"- {r.get('title','')}: {r.get('body','')[:100]}"
        for r in search_results
    )
    evidence_str = evidence if evidence else "No specific evidence found."

    prompt = f"""You are a social media content moderator. Evaluate this post:

POST: "{text}"

Web evidence:
{evidence_str}

Rules:
- Mark VALID if the post is a general opinion, trend observation, or commentary (even without hard proof)
- Mark VALID if the post is based on real news or widely known facts
- Mark VALID if the post uses soft language like "may", "could", "reportedly"
- Mark INVALID ONLY if the post contains clearly false facts, hate speech, or harmful content
- Do NOT reject posts just because a specific statistic cannot be immediately verified

Reply with ONLY one of:
VALID: <one sentence reason>
INVALID: <one sentence reason>"""

    response = gemini_call(prompt)
    verdict = response.text.strip()
    is_valid = verdict.upper().startswith("VALID")
    log(f"  [Verify] {verdict[:80]}")
    return is_valid, verdict

# ----------------------------------------------
# MAIN POST FLOW
# ----------------------------------------------
def create_and_post():
    log("=" * 50)
    log("Starting post creation...")
    result = {
        "success": False,
        "error": "",
        "post": "",
        "topic": "",
        "bluesky": {"posted": False, "error": "", "url": ""},
        "mastodon": {"posted": False, "error": "", "url": ""},
    }

    try:
        for attempt in range(1, 6):
            log(f"Attempt {attempt}/5")

            # Generate
            try:
                post_text, topic = generate_post()
            except Exception as e:
                result["error"] = f"Generation failed: {e}"
                log(f"  {result['error']}\n{traceback.format_exc()}")
                time.sleep(3)
                continue

            result["post"] = post_text
            result["topic"] = topic

            # Reject if too short — force retry
            if len(post_text) < 180:
                result["error"] = f"Post too short ({len(post_text)} chars), retrying..."
                log(f"  {result['error']}")
                continue

            # Duplicate check
            if is_duplicate(post_text):
                log("  Duplicate post, regenerating...")
                result["error"] = "duplicate"
                continue

            # Verify
            try:
                is_valid, reason = verify_post(post_text)
            except Exception as e:
                result["error"] = f"Verification error: {e}"
                log(f"  {result['error']}")
                continue

            if not is_valid:
                result["error"] = f"AI rejected: {reason}"
                log(f"  {result['error']}")
                continue

            # Post to Bluesky
            log("  Posting to Bluesky...")
            try:
                bs_resp = bluesky.post(post_text)
                result["bluesky"]["posted"] = True
                result["bluesky"]["url"] = bs_resp.get("uri", "")
                log(f"  Bluesky OK: {bs_resp.get('uri','')}")
            except Exception as e:
                result["bluesky"]["error"] = str(e)
                log(f"  Bluesky failed: {e}")

            # Post to Mastodon
            log("  Posting to Mastodon...")
            try:
                mt_resp = mastodon.post(post_text)
                result["mastodon"]["posted"] = True
                result["mastodon"]["url"] = mt_resp.get("url", "")
                log(f"  Mastodon OK: {mt_resp.get('url','')}")
            except Exception as e:
                result["mastodon"]["error"] = str(e)
                log(f"  Mastodon failed: {e}")

            # Save if at least one succeeded
            if result["bluesky"]["posted"] or result["mastodon"]["posted"]:
                save_post(post_text, verified=True, results={
                    "bluesky": result["bluesky"],
                    "mastodon": result["mastodon"],
                }, topic=topic)
                result["success"] = True
                result["error"] = ""
                log(f"Post cycle complete! Topic: {topic}")
            else:
                result["error"] = f"Both platforms failed | BS: {result['bluesky']['error']} | MT: {result['mastodon']['error']}"
                log(result["error"])

            return result

        log("All 5 attempts exhausted.")
        return result

    except Exception as e:
        result["error"] = f"Unexpected: {str(e)}"
        log(f"{result['error']}\n{traceback.format_exc()}")
        return result

# ----------------------------------------------
# SCHEDULER
# ----------------------------------------------
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
    log("[Scheduler] Running at scheduled times daily.")
    while True:
        schedule.run_pending()
        time.sleep(30)

# ----------------------------------------------
# SELF-PING
# ----------------------------------------------
def self_ping():
    time.sleep(90)
    while True:
        try:
            r = requests.get(f"{RENDER_URL}/health", timeout=10)
            log(f"[Ping] {r.status_code} -> {RENDER_URL}/health")
        except Exception as e:
            log(f"[Ping] Failed: {e}")
        time.sleep(14 * 60)

# ----------------------------------------------
# FLASK ROUTES
# ----------------------------------------------
@app.route("/")
def index():
    total = posts_col.count_documents({})
    bs_posted = posts_col.count_documents({"results.bluesky.posted": True})
    mt_posted = posts_col.count_documents({"results.mastodon.posted": True})
    latest = list(
        posts_col.find({}, {"text": 1, "topic": 1, "posted_at": 1, "results": 1, "_id": 0})
        .sort("posted_at", -1).limit(5)
    )
    return jsonify({
        "status": "running",
        "platforms": ["Bluesky", "Mastodon"],
        "topic": POST_TOPIC,
        "topic_pool_size": len(TOPIC_POOL),
        "schedule": "11 posts/day at scheduled UTC times",
        "stats": {
            "total_attempts": total,
            "bluesky_posted": bs_posted,
            "mastodon_posted": mt_posted,
        },
        "latest_posts": latest,
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
def list_topics():
    used = get_used_topics(limit=30)
    available = [t for t in TOPIC_POOL if t not in used]
    return jsonify({
        "total_topics": len(TOPIC_POOL),
        "recently_used": used,
        "available_now": available,
    })

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
        return jsonify({"ok": True, "username": data.get("username"), "instance": MASTODON_INSTANCE})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/test-groq")
def test_groq():
    try:
        r = gemini_call("Write a 10-word post about AI.")
        return jsonify({"ok": True, "model": "llama-3.3-70b-versatile", "response": r.text.strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ----------------------------------------------
# ENTRY POINT
# ----------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    log(f"[Server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
