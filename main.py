from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from rapidfuzz import fuzz
import os
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Spotify ----------
sp = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=os.getenv("SPOTIPY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET")
    )
)

# ---------- yt-dlp Configuration ----------
# We fetch slightly more results (20) to ensure the official video is in the pool
ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True
}

# Known "trash" keywords to filter out unless specifically requested
BASE_BAD_KEYWORDS = [
    "cover", "karaoke", "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore", "bass boosted", "reaction"
]

# Major labels that own rights to millions of songs (adds trust score)
OFFICIAL_LABELS = [
    "vevo", "topic", "official", "sony", "t-series", "zee", 
    "warner", "universal", "monstercat", "record", "music", 
    "entertainment", "audio"
]

# ---------- Utils ----------

def clean_text(text: str) -> str:
    """Normalize text for comparison."""
    if not text: return ""
    text = text.lower()
    # Remove things inside brackets like (Official Video) for comparison, 
    # but keep them if they contain 'remix' or 'live' to help identification
    text = re.sub(r"\s+", " ", text).strip()
    return text

def get_channel_trust_score(channel_name: str, artist_name: str) -> int:
    """
    Calculates how 'official' a channel looks.
    """
    channel_norm = channel_name.lower()
    artist_norm = artist_name.lower()
    score = 0

    # 1. Immediate match: Artist name is IN the channel name (e.g., "Anirudh Ravichander")
    if fuzz.partial_ratio(artist_norm, channel_norm) > 85:
        score += 50

    # 2. YouTube Auto-generated "Topic" channels (The Gold Standard)
    # These are created by YouTube automatically from record label files.
    if "topic" in channel_norm:
        score += 40
    
    # 3. VEVO channels
    if "vevo" in channel_norm:
        score += 30

    # 4. Known Record Labels (Sony, T-Series, etc.)
    if any(label in channel_norm for label in OFFICIAL_LABELS):
        score += 20
        
    return score

def best_youtube(title, artist, target_duration):
    # 1. Construct a smarter query
    # Adding "Official Audio" biases YouTube to show the real song first.
    query = f"{title} {artist} Official Audio"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Search for 15 videos
        try:
            search = ydl.extract_info(f"ytsearch15:{query}", download=False)
        except Exception:
            return None

    candidates = []
    
    # Pre-calculate normalized inputs
    target_title_norm = clean_text(title)
    target_artist_norm = clean_text(artist)

    # Dynamic Bad Keywords: 
    # If the Spotify song IS a remix, we allow 'remix' in the YouTube title.
    current_bad_keywords = [k for k in BASE_BAD_KEYWORDS if k not in target_title_norm]

    for e in search.get("entries", []):
        if not e: continue

        yt_id = e.get("id")
        yt_title = e.get("title") or ""
        yt_channel = e.get("uploader") or ""
        yt_duration = e.get("duration") or 0
        yt_views = e.get("view_count") or 0
        
        yt_title_norm = clean_text(yt_title)
        yt_channel_norm = clean_text(yt_channel)

        # ❌ STAGE 1: Hard Filters (Garbage Collection)
        if any(bad in yt_title_norm for bad in current_bad_keywords):
            continue
            
        # ❌ STAGE 2: Duration Sanity Check
        # Official videos might have intros (+/- 20s is safe). 
        # Topic videos are usually exact.
        duration_diff = abs(yt_duration - target_duration)
        if duration_diff > 45: # If duration is off by > 45s, it's likely wrong
            continue

        # ✅ STAGE 3: Scoring Algorithm
        score = 0
        
        # A. Title Similarity (0-100)
        # token_set_ratio handles "Song Name - Artist" vs "Artist - Song Name" very well
        title_score = fuzz.token_set_ratio(target_title_norm, yt_title_norm)
        score += title_score * 2  # Weight: High
        
        # B. Channel / Artist Trust (0-100+)
        channel_score = get_channel_trust_score(yt_channel, artist)
        score += channel_score
        
        # C. Duration Precision
        # If the duration is super close (<= 3s), it's likely the "Topic" audio
        if duration_diff <= 3:
            score += 30
        elif duration_diff <= 10:
            score += 15
            
        # D. View Count Logic (Logarithmic boost)
        # We prefer high views, but 100M views isn't much better than 10M for identification.
        # We just want to avoid the video with 5 views.
        if yt_views > 1000000: score += 20
        elif yt_views > 100000: score += 10
        elif yt_views > 10000: score += 5

        # E. Penalty for missing Artist name in Title/Channel
        # If the artist name is nowhere to be found, punish heavily
        if fuzz.partial_ratio(target_artist_norm, yt_title_norm) < 50 and \
           fuzz.partial_ratio(target_artist_norm, yt_channel_norm) < 50:
            score -= 50

        candidates.append({
            "data": e,
            "score": score,
            "debug": { 
                "title": yt_title, 
                "channel": yt_channel, 
                "diff": duration_diff,
                "score": score 
            }
        })

    # Sort by score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Return the best match if it meets a minimum threshold
    if candidates and candidates[0]["score"] > 140: # Threshold prevents completely wrong songs
        best = candidates[0]["data"]
        return {
            "id": best["id"],
            "title": title,
            "artist": best.get("uploader") or artist,
            "duration": best.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
        }
    
    return None

# ---------- Models ----------
class ImportReq(BaseModel):
    playlistUrl: str
    limit: int | None = None

# =========================================================
# 2️⃣ IMPORT SPOTIFY PLAYLIST
# =========================================================
@app.post("/import-spotify")
def import_spotify(data: ImportReq):
    # Extract ID
    try:
        if "playlist/" in data.playlistUrl:
            playlist_id = data.playlistUrl.split("playlist/")[1].split("?")[0]
        else:
            playlist_id = data.playlistUrl
    except:
        return {"error": "Invalid URL"}

    playlist_meta = sp.playlist(playlist_id)
    
    tracks = []
    offset = 0
    # Fetch all tracks (simple pagination)
    while True:
        resp = sp.playlist_items(playlist_id, limit=50, offset=offset)
        if not resp["items"]: break
        tracks.extend(resp["items"])
        offset += 50
        if data.limit and len(tracks) >= data.limit:
            tracks = tracks[:data.limit]
            break

    songs = []
    
    for item in tracks:
        track = item.get("track")
        if not track: continue

        # --- Search ---
        yt = best_youtube(
            track["name"],
            track["artists"][0]["name"],
            track["duration_ms"] // 1000
        )

        if yt:
            songs.append(yt)

    return {
        "playlist": {
            "name": playlist_meta["name"],
            "total_tracks": playlist_meta["tracks"]["total"]
        },
        "songs": songs
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
