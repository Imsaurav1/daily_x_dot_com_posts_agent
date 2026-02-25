"""
Bluesky + Mastodon Daily Auto-Poster Agent
- Posts 2 times/day to BOTH Bluesky and Mastodon
- Uses MongoDB to prevent duplicate posts
- Uses Google Gemini (free) for content generation + verification
- Uses DuckDuckGo search (free, no API key) for fact-checking
- Runs Flask web server to keep Render alive (auto-pings every 14 min)
- 100% FREE - no credit card needed
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
import google.generativeai as genai
from duckduckgo_search import DDGS

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# ──────────────────────────────────────────────
# ENV VARIABLES
# Set these in Render dashboard:
#
# BLUESKY_HANDLE       → your handle e.g. yourname.bsky.social
# BLUESKY_PASSWORD     → your Bluesky APP password (not login password)
#                        Create at: bsky.social → Settings → Privacy → App Passwords
#
# MASTODON_INSTANCE    → your instance e.g. https://mastodon.social
# MASTODON_TOKEN       → access token
#                        Create at: Settings → Development → New Application
#
# MONGO_URI            → MongoDB Atlas connection string
# GEMINI_API_KEY       → Google AI Studio free API key
# RENDER_URL           → https://your-app.onrender.com
# POST_TOPIC           → optional, default: "AI, technology, productivity tips"
# ──────────────────────────────────────────────

def get_env(key, required=True):
    val = os.environ.get(key)
    if required and not val:
        log(f"❌ Missing required env var: {key}")
        sys.exit(1)
    return val

BLUESKY_HANDLE    = get_env("BLUESKY_HANDLE")
BLUESKY_PASSWORD  = get_env("BLUESKY_PASSWORD")
MASTODON_INSTANCE = get_env("MASTODON_INSTANCE").rstrip("/")
MASTODON_TOKEN    = get_env("MASTODON_TOKEN")
MONGO_URI         = get_env("MONGO_URI")
GEMINI_API_KEY    = get_env("GEMINI_API_KEY")
RENDER_URL        = get_env("RENDER_URL", required=False) or "http://localhost:10000"
POST_TOPIC        = get_env("POST_TOPIC", required=False) or "AI, technology, productivity tips"

log(f"✅ Env vars loaded. Topic: {POST_TOPIC}")

# ──────────────────────────────────────────────
# FLASK
# ──────────────────────────────────────────────
app = Flask(__name__)

# ──────────────────────────────────────────────
# MONGODB
# ──────────────────────────────────────────────
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()
    db = mongo_client["social_poster"]
    posts_col = db["posts"]
    posts_col.create_index("content_hash", unique=True)
    log("✅ MongoDB connected")
except Exception as e:
    log(f"❌ MongoDB failed: {e}")
    sys.exit(1)

# ──────────────────────────────────────────────
# GEMINI
# ──────────────────────────────────────────────
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini = genai.GenerativeModel("gemini-2.5-flash")
    test = gemini.generate_content("Say OK")
    log(f"✅ Gemini connected: {test.text.strip()[:20]}")
except Exception as e:
    log(f"❌ Gemini failed: {e}")
    sys.exit(1)

# ──────────────────────────────────────────────
# BLUESKY CLIENT
# ──────────────────────────────────────────────
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

    def post(self, text: str) -> dict:
        self._login()  # Refresh token before each post
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
    log(f"✅ Bluesky connected as: @{BLUESKY_HANDLE}")
except Exception as e:
    log(f"❌ Bluesky init failed: {e}")
    log("   → Check BLUESKY_HANDLE (e.g. you.bsky.social) and BLUESKY_PASSWORD (App Password)")
    sys.exit(1)


# ──────────────────────────────────────────────
# MASTODON CLIENT
# ──────────────────────────────────────────────
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

    def post(self, text: str) -> dict:
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
    log(f"✅ Mastodon connected as: @{me.get('username')}@{MASTODON_INSTANCE.replace('https://','')}")
except Exception as e:
    log(f"❌ Mastodon init failed: {e}")
    log("   → Check MASTODON_INSTANCE (e.g. https://mastodon.social) and MASTODON_TOKEN")
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
    return posts_col.find_one({"content_hash": hash_content(text)}) is not None


def save_post(text: str, verified: bool, results: dict):
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


def get_past_snippets(limit: int = 20) -> list:
    recent = posts_col.find({}, {"text": 1}).sort("posted_at", -1).limit(limit)
    return [p["text"][:80] for p in recent]


# ──────────────────────────────────────────────
# GENERATE POST
# ──────────────────────────────────────────────

def generate_post() -> str:
    log("  [Generate] Fetching context...")
    results = search_web(f"latest {POST_TOPIC} {datetime.now().strftime('%B %Y')}", 4)
    context = "\n".join(
        f"- {r.get('title','')}: {r.get('body','')[:120]}" for r in results
    )
    past = get_past_snippets()
    past_str = "\n".join(f"- {p}" for p in past) if past else "None yet."

    prompt = f"""You are a social media expert creating engaging posts.

Topic: {POST_TOPIC}

Recent context:
{context if context else 'Use your knowledge.'}

Previously posted (DO NOT repeat):
{past_str}

Write ONE post that:
- Is 280 characters or less
- Is engaging, informative, adds genuine value
- Includes 2-3 relevant hashtags
- Has a unique angle or fresh insight
- Is factually accurate

Return ONLY the post text. No quotes. No preamble."""

    log("  [Generate] Calling Gemini...")
    response = gemini.generate_content(prompt)
    post = response.text.strip().strip('"').strip("'")
    if len(post) > 280:
        post = post[:277] + "..."
    log(f"  [Generate] Got ({len(post)} chars): {post[:80]}...")
    return post


# ──────────────────────────────────────────────
# VERIFY POST
# ──────────────────────────────────────────────

def verify_post(text: str) -> tuple:
    log("  [Verify] Fact-checking...")
    results = search_web(text[:60], max_results=3)
    evidence = "\n".join(
        f"- {r.get('title','')}: {r.get('body','')[:100]}" for r in results
    )
    prompt = f"""You are a fact-checking AI. Evaluate this social media post:

POST: "{text}"

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
                log(f"  ❌ {result['error']}\n{traceback.format_exc()}")
                time.sleep(3)
                continue

            result["post"] = post_text

            # Duplicate check
            if is_duplicate(post_text):
                log("  Duplicate detected, regenerating...")
                result["error"] = "duplicate"
                continue

            # Verify
            try:
                is_valid, reason = verify_post(post_text)
            except Exception as e:
                result["error"] = f"Verification error: {e}"
                log(f"  ❌ {result['error']}")
                continue

            if not is_valid:
                result["error"] = f"AI rejected: {reason}"
                log(f"  ❌ {result['error']}")
                continue

            # ── Post to Bluesky ──
            log("  Posting to Bluesky...")
            try:
                bs_resp = bluesky.post(post_text)
                uri = bs_resp.get("uri", "")
                result["bluesky"]["posted"] = True
                result["bluesky"]["url"] = uri
                log(f"  ✅ Bluesky: {uri}")
            except Exception as e:
                result["bluesky"]["error"] = str(e)
                log(f"  ❌ Bluesky failed: {e}")

            # ── Post to Mastodon ──
            log("  Posting to Mastodon...")
            try:
                mt_resp = mastodon.post(post_text)
                url = mt_resp.get("url", "")
                result["mastodon"]["posted"] = True
                result["mastodon"]["url"] = url
                log(f"  ✅ Mastodon: {url}")
            except Exception as e:
                result["mastodon"]["error"] = str(e)
                log(f"  ❌ Mastodon failed: {e}")

            # Save if at least one succeeded
            if result["bluesky"]["posted"] or result["mastodon"]["posted"]:
                save_post(post_text, verified=True, results={
                    "bluesky": result["bluesky"],
                    "mastodon": result["mastodon"],
                })
                result["success"] = True
                result["error"] = ""
                log("✅ Post cycle complete!")
            else:
                result["error"] = (
                    f"Both failed | Bluesky: {result['bluesky']['error']} "
                    f"| Mastodon: {result['mastodon']['error']}"
                )
                log(f"❌ {result['error']}")

            return result

        log("❌ All 5 attempts exhausted.")
        return result

    except Exception as e:
        result["error"] = f"Unexpected error: {str(e)}"
        log(f"❌ {result['error']}\n{traceback.format_exc()}")
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
# SELF-PING (keeps Render free tier alive)
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
        "schedule": "09:00 UTC and 18:00 UTC daily",
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


@app.route("/test-gemini")
def test_gemini():
    try:
        r = gemini.generate_content("Write a 10-word post about AI.")
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
