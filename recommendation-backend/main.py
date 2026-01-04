from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import re
import json
import os
from groq import Groq
from rapidfuzz import fuzz

app = FastAPI()

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- CONFIG ----------
GROQ_API_KEY = "YOUR_GROQ_API_KEY"
client = Groq(api_key=GROQ_API_KEY)

ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True
}

BASE_BAD_KEYWORDS = ["cover", "karaoke", "slowed", "reverb", "short", "instrumental", "8d", "nightcore", "reaction"]
OFFICIAL_LABELS = ["vevo", "topic", "official", "sony", "t-series", "zee", "warner", "universal", "saregama"]

# ---------- Advanced Utils (From your Second Code) ----------

def clean_text(text: str) -> str:
    if not text: return ""
    text = text.lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def get_channel_trust_score(channel_name: str, artist_name: str) -> int:
    channel_norm = channel_name.lower()
    artist_norm = artist_name.lower()
    score = 0
    if fuzz.partial_ratio(artist_norm, channel_norm) > 85: score += 50
    if "topic" in channel_norm: score += 40
    if any(label in channel_norm for label in OFFICIAL_LABELS): score += 25
    return score

def best_youtube_advanced(title, artist, album=None):
    # Construct a strong query
    query = f"{title} {artist} {album if album else ''} Official Audio"
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Search 10 results to find the best official one
            data = ydl.extract_info(f"ytsearch10:{query}", download=False)
        except:
            return None

    candidates = []
    target_title_norm = clean_text(title)
    target_artist_norm = clean_text(artist)

    for e in data.get("entries", []):
        if not e: continue
        
        yt_title = e.get("title") or ""
        yt_channel = e.get("uploader") or ""
        yt_duration = e.get("duration") or 0
        yt_views = e.get("view_count") or 0
        yt_title_norm = clean_text(yt_title)

        # 1. HARD FILTER: No Shorts or Covers
        if any(bad in yt_title_norm for bad in BASE_BAD_KEYWORDS): continue
        if yt_duration < 60: continue # Skip anything under 1 minute (usually shorts/clips)

        # 2. SCORING
        score = fuzz.token_set_ratio(target_title_norm, yt_title_norm)
        score += get_channel_trust_score(yt_channel, artist)
        
        if yt_views > 1000000: score += 15
        
        candidates.append({"data": e, "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    
    if candidates and candidates[0]["score"] > 50:
        best = candidates[0]["data"]
        return {
            "id": best["id"],
            "title": title,
            "artist": artist,
            "duration": best.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
        }
    return None

# ---------- Models ----------
class RecommendReq(BaseModel):
    title: str
    artist: str

@app.post("/recommend")
def recommend(req: RecommendReq):
    # STEP 1: Better Groq Prompt
    # We ask for the Album/Movie name to make YouTube search more accurate
    prompt = f"""
    The user is listening to '{req.title}' by '{req.artist}'. 
    Suggest 7 similar songs (so we have backups if some fail). 
    Provide the 'name', 'artist', and the 'album' or 'movie' it belongs to.
    Avoid recommending the input song itself.
    Return ONLY JSON: {{"tracks": [{{"name": "song", "artist": "art", "album": "alb"}}]}}
    """
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a world-class music discovery engine like Spotify."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        ai_data = json.loads(completion.choices[0].message.content)
        similar = ai_data.get("tracks", [])
    except Exception as e:
        print(f"Groq Error: {e}")
        return {"songs": []}

    results = []
    seen_ids = set() # To prevent duplicates

    # STEP 2: Advanced Resolution
    for t in similar:
        yt = best_youtube_advanced(t.get("name"), t.get("artist"), t.get("album"))
        
        if yt and yt["id"] not in seen_ids:
            # Verify it's not the same song user is already listening to
            if fuzz.ratio(clean_text(req.title), clean_text(yt["title"])) > 90:
                continue
                
            results.append(yt)
            seen_ids.add(yt["id"])

        if len(results) >= 5:
            break

    return {
        "source": "groq-spotify-hybrid",
        "count": len(results),
        "songs": results
    }
