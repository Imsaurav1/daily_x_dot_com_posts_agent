"""
Bluesky + Mastodon Daily Auto-Poster Agent
- Fetches TODAY's real news from the web
- AI picks the best news item
- Writes an engaging post based on that news
- Posts to Bluesky AND Mastodon
- MongoDB prevents duplicate posts
- Flask server keeps Render alive via self-ping
- 100% FREE
"""

import os
import sys
import time
import hashlib
import threading
import traceback
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
# ENV VARIABLES - Set in Render dashboard:
#
# BLUESKY_HANDLE      e.g. yourname.bsky.social
# BLUESKY_PASSWORD    App Password from bsky.social Settings
# MASTODON_INSTANCE   e.g. https://mastodon.social
# MASTODON_TOKEN      Access token from Mastodon Settings > Development
# MONGO_URI           MongoDB Atlas connection string
# GROQ_API_KEY        Get free key at console.groq.com
# RENDER_URL          https://your-app.onrender.com
# POST_TOPIC          e.g. "AI, tech startups, productivity"
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
POST_TOPIC        = get_env("POST_TOPIC", required=False) or "AI, technology, productivity tips"

log(f"Env vars loaded. Topic: {POST_TOPIC}")

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
# GROQ CALL HELPER (handles rate limits)
# ----------------------------------------------

def gemini_call(prompt, retries=3):
    """Call Groq with automatic retry on rate limit."""
    for attempt in range(retries):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.7,
            )
            # Return object with .text for compatibility
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
    raise Exception("Groq rate limit exceeded after all retries. Try again later.")

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

def save_post(text, verified, results):
    try:
        posts_col.insert_one({
            "content_hash": hash_content(text),
            "text": text,
            "verified": verified,
            "results": results,
            "posted_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        log(f"  [DB Error] {e}")

def get_past_snippets(limit=20):
    recent = posts_col.find({}, {"text": 1}).sort("posted_at", -1).limit(limit)
    return [p["text"][:80] for p in recent]

# ----------------------------------------------
# STEP 1: FETCH TODAY'S REAL NEWS
# ----------------------------------------------

def fetch_latest_news():
    today = datetime.now().strftime("%B %d %Y")
    queries = [
        f"AI artificial intelligence news today {today}",
        f"tech startup news today {today}",
        f"technology innovation latest {today}",
        f"AI tools productivity update {today}",
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
                    "body": body[:300],
                    "url": href,
                })

    # Deduplicate by title
    seen = set()
    unique = []
    for n in all_news:
        if n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)

    log(f"  [News] {len(unique)} unique news items fetched")
    return unique

# ----------------------------------------------
# STEP 2: AI PICKS BEST NEWS
# ----------------------------------------------

def pick_best_news(news_items, past_snippets):
    if not news_items:
        return {}

    news_list = "\n\n".join(
        f"[{i+1}] {n['title']}\n    {n['body'][:150]}"
        for i, n in enumerate(news_items[:15])
    )
    past_str = "\n".join(f"- {p}" for p in past_snippets) if past_snippets else "None."

    prompt = f"""You are a social media news curator. Pick the BEST news to post about today.

NEWS ITEMS:
{news_list}

ALREADY POSTED (avoid these topics):
{past_str}

Choose the ONE news item that:
- Is most interesting, surprising, or impactful
- Has NOT been covered in past posts
- Would get most engagement on social media
- Is about AI, tech startups, or productivity

Reply with ONLY the number. Example: 4"""

    log("  [Pick] AI selecting best news...")
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
# STEP 3: WRITE POST FROM NEWS
# ----------------------------------------------

def generate_post():
    log("  [Generate] Fetching today's news...")
    news_items = fetch_latest_news()
    past = get_past_snippets()

    if not news_items:
        log("  [Generate] No news found, using topic knowledge")
        news_context = f"Write about a recent trend in {POST_TOPIC}"
    else:
        best = pick_best_news(news_items, past)
        if not best:
            news_context = f"Write about a recent trend in {POST_TOPIC}"
        else:
            news_context = f"HEADLINE: {best.get('title', '')}\nDETAILS: {best.get('body', '')}"
            log(f"  [Generate] Writing post for: {best.get('title','')[:60]}...")

    past_str = "\n".join(f"- {p}" for p in past) if past else "None yet."

    prompt = f"""You are a viral social media writer. Write an engaging post based on this real news:

{news_context}

PREVIOUSLY POSTED (do NOT repeat these topics):
{past_str}

Write ONE post that:
- Opens with a hook (shocking fact, bold statement, or question)
- Explains the news in simple, exciting language
- Adds a "why this matters" insight
- Has 2-3 hashtags at the end
- Is under 260 characters total
- Contains NO URLs

Good example:
"Google's new AI just outscored human doctors in medical diagnosis tests. Healthcare is about to change forever. Are we ready? #AI #HealthTech #FutureOfMedicine"

Return ONLY the post text. Nothing else."""

    log("  [Generate] Writing with Gemini...")
    response = gemini_call(prompt)
    post = response.text.strip().strip('"').strip("'")
    if len(post) > 280:
        post = post[:277] + "..."
    log(f"  [Generate] Done ({len(post)} chars): {post[:80]}...")
    return post

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

    prompt = f"""You are a fact-checking AI. Evaluate this social media post:

POST: "{text}"

Web evidence:
{evidence_str}

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
        "bluesky": {"posted": False, "error": "", "url": ""},
        "mastodon": {"posted": False, "error": "", "url": ""},
    }

    try:
        for attempt in range(1, 6):
            log(f"Attempt {attempt}/5")

            # Generate
            try:
                post_text = generate_post()
            except Exception as e:
                result["error"] = f"Generation failed: {e}"
                log(f"  {result['error']}\n{traceback.format_exc()}")
                time.sleep(3)
                continue

            result["post"] = post_text

            # Duplicate check
            if is_duplicate(post_text):
                log("  Duplicate, regenerating...")
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
                })
                result["success"] = True
                result["error"] = ""
                log("Post cycle complete!")
            else:
                result["error"] = f"Both failed | BS: {result['bluesky']['error']} | MT: {result['mastodon']['error']}"
                log(result["error"])

            return result

        log("All attempts exhausted.")
        return result

    except Exception as e:
        result["error"] = f"Unexpected: {str(e)}"
        log(f"{result['error']}\n{traceback.format_exc()}")
        return result

# ----------------------------------------------
# SCHEDULER
# ----------------------------------------------

def run_scheduler():
    schedule.every().day.at("08:00").do(create_and_post)
    schedule.every().day.at("12:00").do(create_and_post)
    schedule.every().day.at("17:00").do(create_and_post)
    schedule.every().day.at("21:00").do(create_and_post)
    log("[Scheduler] Posts at 08:00, 12:00, 17:00, 21:00 UTC daily.")
    while True:
        schedule.run_pending()
        time.sleep(30)

# ----------------------------------------------
# SELF-PING (keeps Render alive)
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
        posts_col.find({}, {"text": 1, "posted_at": 1, "results": 1, "_id": 0})
        .sort("posted_at", -1).limit(5)
    )
    return jsonify({
        "status": "running",
        "platforms": ["Bluesky", "Mastodon"],
        "topic": POST_TOPIC,
        "schedule": "08:00, 12:00, 17:00, 21:00 UTC daily (4 posts/day)",
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
