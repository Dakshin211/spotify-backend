from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import requests
from difflib import SequenceMatcher
import os
import re

app = FastAPI()

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- CONFIG ----------
LASTFM_API_KEY = "7421c24f0ec3913d4b931779b627845a"

ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True
}

BAD_WORDS = [
    "cover", "karaoke", "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore", "reaction"
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

def best_youtube_match(title, artist):
    query = f"{title} {artist} official audio"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        data = ydl.extract_info(f"ytsearch12:{query}", download=False)

    best = None
    best_score = -1

    for e in data.get("entries", []):
        if not e:
            continue

        yt_title = e.get("title") or ""
        channel = e.get("uploader") or ""
        duration = e.get("duration") or 0
        views = e.get("view_count") or 0

        yt_title_l = yt_title.lower()
        channel_l = channel.lower()

        if any(bad in yt_title_l for bad in BAD_WORDS):
            continue

        score = similarity(title, yt_title) * 50

        if artist.lower() in yt_title_l:
            score += 25
        if artist.lower() in channel_l:
            score += 15
        if any(h in channel_l for h in TRUSTED_HINTS):
            score += 15
        if views:
            score += min(10, views ** 0.25)

        if score > best_score:
            best = e
            best_score = score

    if not best:
        return None

    return {
        "id": best["id"],
        "title": title,
        "artist": artist,
        "duration": best.get("duration"),
        "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
    }

# ---------- Models ----------
class RecommendReq(BaseModel):
    title: str
    artist: str

# =========================================================
# 🎵 RECOMMEND NEXT 5 SONGS
# =========================================================
@app.post("/recommend")
def recommend(req: RecommendReq):
    # --- Step 1: Get similar tracks from Last.fm ---
    lastfm_url = (
        "https://ws.audioscrobbler.com/2.0/"
        f"?method=track.getsimilar"
        f"&track={req.title}"
        f"&artist={req.artist}"
        f"&limit=5"
        f"&api_key={LASTFM_API_KEY}"
        f"&format=json"
    )

    try:
        res = requests.get(lastfm_url, timeout=8)
        data = res.json()
        similar = data.get("similartracks", {}).get("track", [])
    except:
        return {"songs": []}

    results = []

    # --- Step 2: Resolve each song to YouTube ---
    for t in similar:
        title = t["name"]
        artist = t["artist"]["name"]

        yt = best_youtube_match(title, artist)
        if yt:
            results.append(yt)

        if len(results) == 5:
            break

    return {
        "source": "lastfm",
        "count": len(results),
        "songs": results
    }
