"""
X.com Daily Auto-Poster Agent
- Posts 2 times/day to X.com
- Uses MongoDB to prevent duplicate posts
- Uses Google Gemini (free) for content generation + verification
- Uses DuckDuckGo search (free, no API key) for fact-checking
- Runs a Flask web server to keep Render alive (auto-pings every 14 min)
"""

import os
import sys
import time
import hashlib
import threading
import traceback
import schedule
import requests
import tweepy
from datetime import datetime, timezone
from flask import Flask, jsonify
from pymongo import MongoClient
import google.generativeai as genai
from duckduckgo_search import DDGS

# ──────────────────────────────────────────────
# LOGGING HELPER
# ──────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# ──────────────────────────────────────────────
# ENV VARIABLES
# ──────────────────────────────────────────────
def get_env(key, required=True):
    val = os.environ.get(key)
    if required and not val:
        log(f"❌ Missing required env var: {key}")
        sys.exit(1)
    return val

TWITTER_API_KEY       = get_env("TWITTER_API_KEY")
TWITTER_API_SECRET    = get_env("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN  = get_env("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = get_env("TWITTER_ACCESS_SECRET")
TWITTER_BEARER_TOKEN  = get_env("TWITTER_BEARER_TOKEN")
MONGO_URI             = get_env("MONGO_URI")
GEMINI_API_KEY        = get_env("GEMINI_API_KEY")
RENDER_URL            = get_env("RENDER_URL", required=False) or "http://localhost:10000"
POST_TOPIC            = get_env("POST_TOPIC", required=False) or "AI, technology, productivity tips"

log(f"✅ All env vars loaded. Topic: {POST_TOPIC}")

# ──────────────────────────────────────────────
# INIT FLASK
# ──────────────────────────────────────────────
app = Flask(__name__)

# ──────────────────────────────────────────────
# INIT MONGODB
# ──────────────────────────────────────────────
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()
    db = mongo_client["x_poster"]
    posts_col = db["posts"]
    posts_col.create_index("content_hash", unique=True)
    log("✅ MongoDB connected")
except Exception as e:
    log(f"❌ MongoDB connection failed: {e}")
    sys.exit(1)

# ──────────────────────────────────────────────
# INIT GEMINI
# ──────────────────────────────────────────────
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini = genai.GenerativeModel("gemini-1.5-flash")
    test = gemini.generate_content("Say OK")
    log(f"✅ Gemini connected: {test.text.strip()[:20]}")
except Exception as e:
    log(f"❌ Gemini init failed: {e}")
    sys.exit(1)

# ──────────────────────────────────────────────
# INIT TWITTER
# ──────────────────────────────────────────────
try:
    twitter_client = tweepy.Client(
        bearer_token=TWITTER_BEARER_TOKEN,
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_SECRET,
        wait_on_rate_limit=True,
    )
    me = twitter_client.get_me()
    log(f"✅ Twitter connected as: @{me.data.username}")
except Exception as e:
    log(f"❌ Twitter init failed: {e}")
    log("   → Check your API keys. Access Token must have Read+Write permission!")
    sys.exit(1)

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def search_web(query: str, max_results: int = 5) -> list:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        log(f"  [Search] {len(results)} results for: {query[:50]}")
        return results
    except Exception as e:
        log(f"  [Search Error] {e}")
        return []


def hash_content(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def is_duplicate(text: str) -> bool:
    h = hash_content(text)
    return posts_col.find_one({"content_hash": h}) is not None


def save_post(text: str, verified: bool, posted: bool, error: str = ""):
    h = hash_content(text)
    try:
        posts_col.insert_one({
            "content_hash": h,
            "text": text,
            "verified": verified,
            "posted": posted,
            "error": error,
            "posted_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        log(f"  [DB Save Error] {e}")


def get_past_post_snippets(limit: int = 20) -> list:
    recent = posts_col.find({}, {"text": 1}).sort("posted_at", -1).limit(limit)
    return [p["text"][:80] for p in recent]


# ──────────────────────────────────────────────
# CONTENT GENERATION
# ──────────────────────────────────────────────

def generate_post() -> str:
    log("  [Generate] Searching for context...")
    search_query = f"latest {POST_TOPIC} news {datetime.now().strftime('%B %Y')}"
    results = search_web(search_query, max_results=4)
    context_snippets = "\n".join(
        f"- {r.get('title', '')}: {r.get('body', '')[:120]}" for r in results
    )
    past_posts = get_past_post_snippets()
    past_str = "\n".join(f"- {p}" for p in past_posts) if past_posts else "None yet."

    prompt = f"""You are a social media expert creating engaging X (Twitter) posts.

Topic: {POST_TOPIC}

Recent web context:
{context_snippets if context_snippets else 'No context available, use your knowledge.'}

Previously posted (DO NOT repeat):
{past_str}

Write ONE tweet that:
- Is 240 characters or less
- Is engaging, informative, adds genuine value
- Includes 2-3 relevant hashtags
- Does NOT repeat any previously posted idea
- Is factually accurate

Return ONLY the tweet text. No quotes around it."""

    log("  [Generate] Calling Gemini...")
    response = gemini.generate_content(prompt)
    tweet = response.text.strip().strip('"').strip("'")
    if len(tweet) > 280:
        tweet = tweet[:277] + "..."
    log(f"  [Generate] Result: {tweet[:80]}...")
    return tweet


# ──────────────────────────────────────────────
# AI VERIFICATION
# ──────────────────────────────────────────────

def verify_post(text: str) -> tuple:
    log("  [Verify] Fact-checking...")
    search_results = search_web(text[:60], max_results=3)
    evidence = "\n".join(
        f"- {r.get('title', '')}: {r.get('body', '')[:100]}" for r in search_results
    )
    prompt = f"""You are a fact-checking AI. Evaluate this tweet:

        TWEET: "{text}"
        
        Web evidence:
        {evidence if evidence else 'No specific evidence found.'}
        
        Reply with ONLY one of:
        VALID: <one sentence reason>
        INVALID: <one sentence reason>"""

    response = gemini.generate_content(prompt)
    verdict = response.text.strip()
    is_valid = verdict.upper().startswith("VALID")
    log(f"  [Verify] {verdict[:80]}")
    return is_valid, verdict


# ──────────────────────────────────────────────
# MAIN POST FLOW
# ──────────────────────────────────────────────

def create_and_post():
    log("=" * 50)
    log("Starting post creation flow...")
    result = {"success": False, "error": "", "tweet": "", "step": "init"}

    try:
        for attempt in range(1, 6):
            log(f"Attempt {attempt}/5")
            result["step"] = f"attempt_{attempt}"

            # Generate
            try:
                tweet_text = generate_post()
            except Exception as e:
                err = f"Generation failed: {str(e)}"
                log(f"  {err}\n{traceback.format_exc()}")
                result["error"] = err
                time.sleep(3)
                continue

            result["tweet"] = tweet_text

            # Duplicate check
            if is_duplicate(tweet_text):
                log("  Duplicate, regenerating...")
                result["error"] = "duplicate"
                continue

            # Verify
            try:
                is_valid, reason = verify_post(tweet_text)
            except Exception as e:
                err = f"Verification failed: {str(e)}"
                log(f"  {err}\n{traceback.format_exc()}")
                result["error"] = err
                continue

            if not is_valid:
                log(f"  Rejected by AI: {reason}")
                result["error"] = f"AI rejected: {reason}"
                continue

            # Post
            log("  Posting to X...")
            result["step"] = "posting"
            try:
                resp = twitter_client.create_tweet(text=tweet_text)
                tweet_id = resp.data["id"]
                log(f"  ✅ Posted! Tweet ID: {tweet_id}")
                save_post(tweet_text, verified=True, posted=True)
                result.update({"success": True, "tweet_id": str(tweet_id), "error": ""})
                return result
            except tweepy.TweepyException as e:
                err = f"Twitter API error: {str(e)}"
                log(f"  ❌ {err}")
                save_post(tweet_text, verified=True, posted=False, error=err)
                result["error"] = err
                return result

        log("❌ All attempts exhausted.")
        return result

    except Exception as e:
        err = f"Unexpected: {str(e)}"
        log(f"❌ {err}\n{traceback.format_exc()}")
        result["error"] = err
        return result


# ──────────────────────────────────────────────
# SCHEDULER
# ──────────────────────────────────────────────

def run_scheduler():
    schedule.every().day.at("09:00").do(create_and_post)
    schedule.every().day.at("18:00").do(create_and_post)
    log("[Scheduler] Posts at 09:00 UTC and 18:00 UTC daily.")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ──────────────────────────────────────────────
# SELF-PING
# ──────────────────────────────────────────────

def self_ping():
    time.sleep(90)
    while True:
        try:
            r = requests.get(f"{RENDER_URL}/health", timeout=10)
            log(f"[Ping] {r.status_code} → {RENDER_URL}/health")
        except Exception as e:
            log(f"[Ping] Failed: {e}")
        time.sleep(14 * 60)


# ──────────────────────────────────────────────
# FLASK ROUTES
# ──────────────────────────────────────────────

@app.route("/")
def index():
    total = posts_col.count_documents({})
    verified = posts_col.count_documents({"verified": True})
    posted = posts_col.count_documents({"posted": True})
    latest = list(posts_col.find({}, {"text": 1, "posted_at": 1, "posted": 1, "_id": 0})
                  .sort("posted_at", -1).limit(5))
    return jsonify({
        "status": "running",
        "total_posts": total,
        "verified_posts": verified,
        "successfully_posted": posted,
        "topic": POST_TOPIC,
        "schedule": "09:00 UTC and 18:00 UTC daily",
        "render_url": RENDER_URL,
        "latest_posts": latest,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/post-now")
def post_now():
    log("[/post-now] Manual trigger received")
    result = create_and_post()
    return jsonify(result)


@app.route("/posts")
def list_posts():
    posts = list(posts_col.find({}, {"_id": 0}).sort("posted_at", -1).limit(50))
    return jsonify(posts)


@app.route("/test-twitter")
def test_twitter():
    """Test Twitter connection without posting."""
    try:
        me = twitter_client.get_me()
        return jsonify({"ok": True, "username": me.data.username, "id": str(me.data.id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/test-gemini")
def test_gemini():
    """Test Gemini without posting."""
    try:
        r = gemini.generate_content("Write a 10-word tweet about AI.")
        return jsonify({"ok": True, "response": r.text.strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    log(f"[Server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
