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

# 🚫 Garbage Filters (Standard + "Shorts" keywords)
BASE_BAD_KEYWORDS = [
    "cover", "karaoke", "slowed", "reverb", "short", 
    "instrumental", "8d", "nightcore", "reaction", "review", "status"
]

# ✅ Trusted Sources (The "Gold Standard" for Official Audio)
OFFICIAL_LABELS = [
    "vevo", "topic", "official", "sony", "t-series", "zee", 
    "warner", "universal", "monstercat", "record", "music", 
    "entertainment", "audio", "saregama", "lahari", "think music"
]

# ---------- ADVANCED UTILS (From your Import Code) ----------

def clean_text(text: str) -> str:
    """Normalize text for consistent comparison."""
    if not text: return ""
    text = text.lower()
    # Remove brackets unless they contain important descriptors
    if not any(x in text for x in ["remix", "live", "acoustic"]):
        text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def get_channel_trust_score(channel_name: str, artist_name: str) -> int:
    """Calculates how 'official' a YouTube channel looks."""
    channel_norm = channel_name.lower()
    artist_norm = artist_name.lower()
    score = 0

    # 1. Artist name is IN the channel name (High Trust)
    if fuzz.partial_ratio(artist_norm, channel_norm) > 85:
        score += 50

    # 2. "Topic" channels (YouTube's auto-generated official channels)
    if "topic" in channel_norm:
        score += 40
    
    # 3. VEVO or Major Label keywords
    if any(label in channel_norm for label in OFFICIAL_LABELS):
        score += 25
        
    return score

def best_youtube_search(title, artist, album=None):
    """
    The 'Spotify-Level' Search Logic.
    Uses Album/Movie names and Channel Trust Scores to find the REAL song.
    """
    # --- STRATEGY 1: Smart Query Construction ---
    # Searching "Ranjha Shershaah Official Audio" is better than just "Ranjha"
    query_parts = [title, artist]
    if album and "greatest hits" not in album.lower():
        query_parts.append(album)
    query_parts.append("Official Audio")
    
    query = " ".join(query_parts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Fetch 10 candidates to ensure we find the official one
            data = ydl.extract_info(f"ytsearch10:{query}", download=False)
        except:
            return None

    candidates = []
    target_title_norm = clean_text(title)
    target_artist_norm = clean_text(artist)
    target_album_norm = clean_text(album) if album else ""

    for e in data.get("entries", []):
        if not e: continue

        yt_id = e.get("id")
        yt_title = e.get("title") or ""
        yt_channel = e.get("uploader") or ""
        yt_duration = e.get("duration") or 0
        yt_views = e.get("view_count") or 0
        
        yt_title_norm = clean_text(yt_title)

        # --- STRATEGY 2: Aggressive Filtering ---
        
        # A. Filter Shorts & Clips (Must be > 60 seconds)
        if yt_duration < 60: 
            continue

        # B. Filter Junk (Covers, Slowed, etc.)
        if any(bad in yt_title_norm for bad in BASE_BAD_KEYWORDS):
            # Exception: If the user ASKED for a Remix, don't filter it out
            if "remix" not in target_title_norm:
                continue

        # --- STRATEGY 3: Weighted Scoring ---
        score = 0
        
        # A. Title Similarity
        score += fuzz.token_set_ratio(target_title_norm, yt_title_norm)
        
        # B. Channel Trust (Crucial for avoiding fan uploads)
        score += get_channel_trust_score(yt_channel, artist)
        
        # C. Album/Movie Match (Bonus points)
        if target_album_norm and fuzz.partial_ratio(target_album_norm, yt_title_norm) > 80:
            score += 20
            
        # D. View Count Boost (Official songs usually have more views)
        if yt_views > 1000000: score += 15
        elif yt_views > 100000: score += 10

        candidates.append({"data": e, "score": score})

    # Sort by Score (Highest First)
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # --- STRATEGY 4: Final Threshold ---
    # We only accept results that have a decent match score (>50)
    if candidates and candidates[0]["score"] > 50:
        best = candidates[0]["data"]
        return {
            "id": best["id"],
            "title": title, # Return clean AI title
            "artist": artist, # Return clean AI artist
            "album": album,
            "duration": best.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
        }
    
    return None

# ---------- Models ----------
class RecommendReq(BaseModel):
    title: str
    artist: str

# =========================================================
# 🎵 RECOMMENDATION ENGINE (Groq Brain + Robust Search)
# =========================================================
@app.post("/recommend")
def recommend(req: RecommendReq):
    # --- Step 1: The "Brain" (Groq AI) ---
    # We explicitly ask for the Album/Movie name to help our searcher
    prompt = f"""
    The user is listening to "{req.title}" by "{req.artist}". 
    Suggest 6 similar songs with the same mood, genre, or from the same movie/industry.
    
    IMPORTANT: Provide the specific Album or Movie name for each song to help find the official video.
    
    Return JSON format:
    {{
        "tracks": [
            {{"name": "Song Title", "artist": "Artist Name", "album": "Album/Movie Name"}}
        ]
    }}
    """
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a music recommendation engine. Output valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        
        ai_data = json.loads(completion.choices[0].message.content)
        similar_tracks = ai_data.get("tracks", [])
    except Exception as e:
        print(f"Groq Error: {e}")
        return {"songs": []}

    results = []
    seen_ids = set() # Prevent duplicates

    # --- Step 2: The "Muscle" (Advanced YouTube Search) ---
    for t in similar_tracks:
        # Stop if we have 5 good results
        if len(results) >= 5:
            break

        name = t.get("name")
        artist = t.get("artist")
        album = t.get("album")
        
        if not name or not artist: continue

        # Run the advanced search
        yt = best_youtube_search(name, artist, album)
        
        if yt:
            # prevent duplicate songs
            if yt["id"] in seen_ids: continue
            
            # prevent recommending the song the user is already listening to
            if fuzz.ratio(clean_text(req.title), clean_text(yt["title"])) > 90: continue
            
            results.append(yt)
            seen_ids.add(yt["id"])

    return {
        "source": "groq-advanced",
        "count": len(results),
        "songs": results
    }
