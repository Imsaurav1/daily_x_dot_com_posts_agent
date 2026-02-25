"""
X.com Daily Auto-Poster Agent
- Posts 2 times/day to X.com
- Uses MongoDB to prevent duplicate posts
- Uses Google Gemini (free) for content generation + verification
- Uses DuckDuckGo search (free, no API key) for fact-checking
- Runs a Flask web server to keep Render alive (auto-pings every 15 min)
"""

import os
import time
import hashlib
import threading
import schedule
import requests
import tweepy
from datetime import datetime, timezone
from flask import Flask, jsonify
from pymongo import MongoClient
import google.generativeai as genai
from duckduckgo_search import DDGS

# ──────────────────────────────────────────────
# ENV VARIABLES (set these in Render dashboard)
# ──────────────────────────────────────────────
# TWITTER_API_KEY
# TWITTER_API_SECRET
# TWITTER_ACCESS_TOKEN
# TWITTER_ACCESS_SECRET
# TWITTER_BEARER_TOKEN
# MONGO_URI              (MongoDB Atlas free tier)
# GEMINI_API_KEY         (Google AI Studio - free, no credit card)
# RENDER_URL             (your Render app URL, e.g. https://yourapp.onrender.com)
# POST_TOPIC             (optional, default: "AI, technology, productivity tips")

TWITTER_API_KEY       = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET    = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN  = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET = os.environ["TWITTER_ACCESS_SECRET"]
TWITTER_BEARER_TOKEN  = os.environ["TWITTER_BEARER_TOKEN"]
MONGO_URI             = os.environ["MONGO_URI"]
GEMINI_API_KEY        = os.environ["GEMINI_API_KEY"]
RENDER_URL            = os.getenv("RENDER_URL", "http://localhost:10000")
POST_TOPIC            = os.getenv("POST_TOPIC", "AI, technology, productivity tips")

# ──────────────────────────────────────────────
# INIT
# ──────────────────────────────────────────────
app = Flask(__name__)

# MongoDB
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["x_poster"]
posts_col = db["posts"]
posts_col.create_index("content_hash", unique=True)

# Gemini
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")  # Free tier model

# Twitter / X
twitter_client = tweepy.Client(
    bearer_token=TWITTER_BEARER_TOKEN,
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_token_secret=TWITTER_ACCESS_SECRET,
    wait_on_rate_limit=True,
)

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Free DuckDuckGo search – no API key needed."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        print(f"[Search Error] {e}")
        return []


def hash_content(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def is_duplicate(text: str) -> bool:
    h = hash_content(text)
    return posts_col.find_one({"content_hash": h}) is not None


def save_post(text: str, verified: bool):
    h = hash_content(text)
    try:
        posts_col.insert_one({
            "content_hash": h,
            "text": text,
            "verified": verified,
            "posted_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        print(f"[DB Save Error] {e}")


def get_past_post_snippets(limit: int = 20) -> list[str]:
    recent = posts_col.find({}, {"text": 1}).sort("posted_at", -1).limit(limit)
    return [p["text"][:80] for p in recent]


# ──────────────────────────────────────────────
# CONTENT GENERATION
# ──────────────────────────────────────────────

def generate_post() -> str | None:
    """Generate a tweet using Gemini + DuckDuckGo for fresh context."""
    # 1. Search for fresh angle
    search_query = f"latest news {POST_TOPIC} {datetime.now().strftime('%B %Y')}"
    results = search_web(search_query, max_results=4)
    context_snippets = "\n".join(
        f"- {r.get('title', '')}: {r.get('body', '')[:120]}" for r in results
    )

    # 2. Get past posts to avoid repetition
    past_posts = get_past_post_snippets()
    past_str = "\n".join(f"- {p}" for p in past_posts) if past_posts else "None yet."

    prompt = f"""
You are a social media expert creating engaging X (Twitter) posts.

Topic: {POST_TOPIC}

Recent web context (use for inspiration, keep content fresh):
{context_snippets}

Previously posted (DO NOT repeat these ideas):
{past_str}

Write ONE tweet that:
- Is 240 characters or less
- Is engaging, informative, and adds genuine value
- Includes 2-3 relevant hashtags
- Does NOT repeat any previously posted idea
- Is factually accurate
- Has a unique angle or insight

Return ONLY the tweet text, nothing else.
"""
    try:
        response = gemini.generate_content(prompt)
        tweet = response.text.strip().strip('"').strip("'")
        # Enforce character limit
        if len(tweet) > 280:
            tweet = tweet[:277] + "..."
        return tweet
    except Exception as e:
        print(f"[Generation Error] {e}")
        return None


# ──────────────────────────────────────────────
# AI VERIFICATION
# ──────────────────────────────────────────────

def verify_post(text: str) -> tuple[bool, str]:
    """
    Use Gemini + web search to verify the post is:
    - Factually accurate
    - Not harmful / misleading
    - Appropriate for public posting
    Returns (is_valid, reason)
    """
    # Quick web check on main claim
    search_results = search_web(text[:60], max_results=3)
    evidence = "\n".join(
        f"- {r.get('title', '')}: {r.get('body', '')[:100]}" for r in search_results
    )

    prompt = f"""
You are a fact-checking AI. Evaluate this tweet for public posting:

TWEET: "{text}"

Web evidence found:
{evidence if evidence else 'No specific evidence found.'}

Evaluate:
1. Is this factually accurate or at least not verifiably false?
2. Is it appropriate (no hate speech, misinformation, harmful content)?
3. Is it authentic and adds value?

Reply with ONLY:
VALID: <one sentence reason>
or
INVALID: <one sentence reason>
"""
    try:
        response = gemini.generate_content(prompt)
        verdict = response.text.strip()
        if verdict.upper().startswith("VALID"):
            return True, verdict
        else:
            return False, verdict
    except Exception as e:
        print(f"[Verification Error] {e}")
        return False, str(e)


# ──────────────────────────────────────────────
# MAIN POST FLOW
# ──────────────────────────────────────────────

def create_and_post():
    print(f"\n[{datetime.now()}] Starting post creation...")

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        print(f"  Attempt {attempt}/{max_attempts}")

        # Generate
        tweet_text = generate_post()
        if not tweet_text:
            print("  Generation failed, retrying...")
            time.sleep(5)
            continue

        print(f"  Generated: {tweet_text[:80]}...")

        # Check duplicate
        if is_duplicate(tweet_text):
            print("  Duplicate detected, regenerating...")
            continue

        # Verify
        is_valid, reason = verify_post(tweet_text)
        print(f"  Verification: {reason[:80]}")

        if not is_valid:
            print("  Failed verification, regenerating...")
            continue

        # Post to X
        try:
            response = twitter_client.create_tweet(text=tweet_text)
            tweet_id = response.data["id"]
            print(f"  ✅ Posted! Tweet ID: {tweet_id}")
            save_post(tweet_text, verified=True)
            return True
        except tweepy.TweepyException as e:
            print(f"  ❌ Twitter error: {e}")
            save_post(tweet_text, verified=True)  # Save to avoid retrying same content
            return False

    print("  ❌ Max attempts reached, skipping this slot.")
    return False


# ──────────────────────────────────────────────
# SCHEDULER
# ──────────────────────────────────────────────

def run_scheduler():
    """Run 2 posts/day: 9 AM and 6 PM UTC"""
    schedule.every().day.at("09:00").do(create_and_post)
    schedule.every().day.at("18:00").do(create_and_post)

    print("[Scheduler] Running. Posts scheduled at 09:00 UTC and 18:00 UTC daily.")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ──────────────────────────────────────────────
# SELF-PING (keeps Render free tier alive)
# ──────────────────────────────────────────────

def self_ping():
    """Ping own URL every 14 minutes to prevent Render sleep."""
    time.sleep(60)  # Wait for server to start
    while True:
        try:
            r = requests.get(f"{RENDER_URL}/health", timeout=10)
            print(f"[Ping] Self-ping: {r.status_code}")
        except Exception as e:
            print(f"[Ping] Failed: {e}")
        time.sleep(14 * 60)  # 14 minutes


# ──────────────────────────────────────────────
# FLASK ROUTES
# ──────────────────────────────────────────────

@app.route("/")
def index():
    total = posts_col.count_documents({})
    verified = posts_col.count_documents({"verified": True})
    latest = list(posts_col.find({}, {"text": 1, "posted_at": 1, "_id": 0})
                  .sort("posted_at", -1).limit(5))
    return jsonify({
        "status": "running",
        "total_posts": total,
        "verified_posts": verified,
        "topic": POST_TOPIC,
        "schedule": "09:00 UTC and 18:00 UTC daily",
        "latest_posts": latest,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/post-now")
def post_now():
    """Manual trigger endpoint."""
    success = create_and_post()
    return jsonify({"success": success})


@app.route("/posts")
def list_posts():
    posts = list(posts_col.find({}, {"_id": 0}).sort("posted_at", -1).limit(50))
    return jsonify(posts)


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    # Start scheduler in background
    threading.Thread(target=run_scheduler, daemon=True).start()

    # Start self-ping in background
    threading.Thread(target=self_ping, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    print(f"[Server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
