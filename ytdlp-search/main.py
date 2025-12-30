from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
from difflib import SequenceMatcher
import re

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
}

BAD_WORDS = [
    "live", "remix", "cover", "karaoke",
    "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore"
]

TRUSTED_HINTS = ["topic", "vevo", "official"]

# ---------- Utils ----------
def normalize(text: str):
    text = text.lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()

# ---------- API ----------
class SearchReq(BaseModel):
    query: str   # user typed text (song / artist / both)

@app.post("/search")
def search(req: SearchReq):
    query = req.query.strip()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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

        title_l = title.lower()
        channel_l = channel.lower()

        # ❌ Remove obvious junk
        if any(b in title_l for b in BAD_WORDS):
            continue

        score = 0

        # 1️⃣ Query similarity (MOST IMPORTANT)
        score += similarity(query, title) * 50

        # 2️⃣ Trusted channels boost
        if any(h in channel_l for h in TRUSTED_HINTS):
            score += 20

        # 3️⃣ Popularity (light)
        if views:
            score += min(10, views ** 0.25)

        scored.append((score, e))

    # Sort by score
    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for score, e in scored[:10]:
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
