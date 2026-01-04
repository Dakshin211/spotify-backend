from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import requests
from difflib import SequenceMatcher
import os
import re
import json
from groq import Groq

app = FastAPI()

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- CONFIG ----------
# Replace with your actual Groq API Key
GROQ_API_KEY = "YOUR_GROQ_API_KEY_HERE"
client = Groq(api_key=GROQ_API_KEY)

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
        # Reduced search to 5 for better speed with LLM
        data = ydl.extract_info(f"ytsearch5:{query}", download=False)

    best = None
    best_score = -1

    for e in data.get("entries", []):
        if not e: continue
        yt_title = e.get("title") or ""
        channel = e.get("uploader") or ""
        views = e.get("view_count") or 0

        yt_title_l = yt_title.lower()
        channel_l = channel.lower()

        if any(bad in yt_title_l for bad in BAD_WORDS): continue

        score = similarity(title, yt_title) * 50
        if artist.lower() in yt_title_l: score += 25
        if artist.lower() in channel_l: score += 15
        if any(h in channel_l for h in TRUSTED_HINTS): score += 15
        if views: score += min(10, views ** 0.25)

        if score > best_score:
            best = e
            best_score = score

    if not best: return None

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
# 🎵 RECOMMEND NEXT 5 SONGS (Groq Implementation)
# =========================================================
@app.post("/recommend")
def recommend(req: RecommendReq):
    # --- Step 1: Get recommendations from Groq ---
    prompt = f"The user is listening to '{req.title}' by '{req.artist}'. Suggest 5 similar songs. Return a JSON object with a 'tracks' key containing a list of objects with 'name' and 'artist' keys."
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a music expert. Always respond in valid JSON format."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        
        # Parse AI response
        ai_data = json.loads(completion.choices[0].message.content)
        similar = ai_data.get("tracks", [])
    except Exception as e:
        print(f"Groq Error: {e}")
        return {"songs": []}

    results = []

    # --- Step 2: Resolve each AI-suggested song to YouTube ---
    for t in similar:
        title = t.get("name")
        artist = t.get("artist")
        
        if not title or not artist: continue

        yt = best_youtube_match(title, artist)
        if yt:
            results.append(yt)

        if len(results) >= 5:
            break

    return {
        "source": "groq-ai", # Changed source name for clarity
        "count": len(results),
        "songs": results
    }
