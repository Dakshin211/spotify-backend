from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
from rapidfuzz import fuzz  # HIGH ACCURACY MATCHING
import re
import math

app = FastAPI()

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- yt-dlp ----------
ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True,
}

# Keywords to filter out unless user specifically asks for them
BAD_WORDS = [
    "live", "remix", "cover", "karaoke",
    "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore", "reaction", "review"
]

# Trusted indicators
TRUSTED_HINTS = ["topic", "vevo", "official", "records", "music", "audio", "saregama", "lahari", "think music"]

# ---------- Utils ----------
def normalize(text: str) -> str:
    """Cleans text for accurate comparison."""
    if not text: return ""
    text = text.lower()
    # Remove clutter like (Official Video) to match pure song titles
    text = re.sub(r"\(official audio\)|\[official video\]|\(official video\)|lyric video|lyrics", "", text)
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def calculate_view_score(views: int) -> float:
    """Logarithmic score: 1M views = 12 pts, 100M views = 16 pts"""
    if not views or views < 1000: return 0
    return math.log10(views) * 2

# ---------- API ----------
class SearchReq(BaseModel):
    query: str

@app.post("/search")
def search(req: SearchReq):
    query = req.query.strip()
    clean_query = normalize(query)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Fetch 20 results to ensure we have enough candidates to rank
        data = ydl.extract_info(
            f"ytsearch20:{query}",
            download=False
        )

    scored = []

    for e in data.get("entries", []):
        if not e:
            continue

        title = e.get("title") or ""
        channel = e.get("uploader") or ""
        duration = e.get("duration") or 0
        views = e.get("view_count") or 0

        title_l = normalize(title)
        channel_l = channel.lower()

        # ❌ Filter junk (unless user searched for it)
        if any(b in title_l for b in BAD_WORDS) and not any(b in clean_query for b in BAD_WORDS):
            continue

        # 🛑 Sanity check: Skip shorts (<60s) or long compilations (>10m)
        # This prevents 30-second WhatsApp status videos from appearing
        if duration < 60 or duration > 600:
            continue

        score = 0

        # 1️⃣ RAPIDFUZZ SIMILARITY (Weight: 60)
        # token_set_ratio matches "Song - Artist" even if user typed "Artist Song"
        score += fuzz.token_set_ratio(clean_query, title_l) * 0.6

        # 2️⃣ TRUSTED CHANNEL BOOST (Weight: 20)
        if any(h in channel_l for h in TRUSTED_HINTS):
            score += 15
        if "topic" in channel_l:
            score += 10 # Extra boost for "Topic" channels (Official Audio)

        # 3️⃣ POPULARITY BOOST (Weight: 20)
        # This pushes real songs (millions of views) above covers (thousands of views)
        score += calculate_view_score(views)

        scored.append((score, e))

    # Sort by score (Highest first)
    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    # Return exactly the format you used before
    for _, e in scored[:10]:
        results.append({
            "id": e["id"],
            "title": e.get("title"),
            "artist": e.get("uploader"),
            "duration": e.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg"
        })

    return {
        "source": "ytdlp",
        "count": len(results),
        "results": results
    }
