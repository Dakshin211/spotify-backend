from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
from rapidfuzz import fuzz # pip install rapidfuzz
import re
import math

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True, # Ensure we don't accidentally grab playlists
}

# Labels that signify a high-quality/original source
OFFICIAL_CHANNELS = ["vevo", "topic", "official", "records", "music", "audio"]

# ---------- Utils ----------

def clean_text(text: str) -> str:
    """Normalize text for better matching."""
    if not text: return ""
    text = text.lower()
    # Remove common extra text in YouTube titles that messes up similarity
    text = re.sub(r"\(official audio\)|\[official video\]|\(official video\)|lyric video|lyrics", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def calculate_view_score(views: int) -> float:
    """Gives a score boost based on views (Logarithmic)."""
    if not views or views < 1000: return 0
    # Log10 turns 1,000,000 views into a score of 6.0
    return math.log10(views) * 2 

# ---------- API ----------

class SearchReq(BaseModel):
    query: str

@app.post("/search")
def search(req: SearchReq):
    query = req.query.strip()
    clean_query = clean_text(query)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Search for 20 to ensure we have a good pool to rank
        data = ydl.extract_info(f"ytsearch20:{query}", download=False)

    scored_results = []

    for e in data.get("entries", []):
        if not e: continue

        title = e.get("title") or ""
        channel = e.get("uploader") or ""
        duration = e.get("duration") or 0
        views = e.get("view_count") or 0
        
        title_clean = clean_text(title)
        channel_l = channel.lower()

        # ❌ HARD FILTER: Keep out the junk
        # Only filter if the user DIDN'T search for these specific things
        junk_keywords = ["cover", "karaoke", "slowed", "reverb", "reaction", "review", "8d", "nightcore"]
        if any(word in title_clean and word not in clean_query for word in junk_keywords):
            continue

        score = 0

        # 1️⃣ TEXT SIMILARITY (Weight: 60)
        # Using token_set_ratio because it ignores word order
        text_sim = fuzz.token_set_ratio(clean_query, title_clean)
        score += (text_sim * 0.6)

        # 2️⃣ VIEW COUNT BOOST (Weight: 20)
        # Higher views = higher trust
        score += calculate_view_score(views)

        # 3️⃣ CHANNEL TRUST (Weight: 20)
        if any(hint in channel_l for hint in OFFICIAL_CHANNELS):
            score += 15
        if "topic" in channel_l: # 'Topic' channels are the official audio releases
            score += 5

        # 4️⃣ DURATION PENALTY
        # Most songs are between 2 and 5 minutes (120 - 300 seconds)
        if duration < 60 or duration > 600:
            score -= 20 # Penalize shorts or very long compilations

        scored_results.append((score, e))

    # Sort by descending score
    scored_results.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, e in scored_results[:10]:
        results.append({
            "id": e["id"],
            "title": e.get("title"),
            "artist": e.get("uploader"),
            "duration": e.get("duration"),
            "views": e.get("view_count"),
            "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
            "match_score": round(score, 2)
        })

    return {
        "source": "ytdlp_ranked",
        "query": query,
        "count": len(results),
        "results": results
    }
