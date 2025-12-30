from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from rapidfuzz import fuzz  # MUST INSTALL THIS: pip install rapidfuzz
import os
import re

app = FastAPI()

# ---------- CORS ----------
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
ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True
}

# Bad keywords (but we will apply them smartly)
BASE_BAD_KEYWORDS = [
    "cover", "karaoke", "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore", "bass boosted", "reaction", "review"
]

# Trusted official channel hints
OFFICIAL_LABELS = [
    "vevo", "topic", "official", "sony", "t-series", "zee", 
    "warner", "universal", "monstercat", "record", "music", 
    "entertainment", "audio", "anirudh", "think music" 
]

# ---------- Utils ----------

def clean_text(text: str) -> str:
    """Normalize text for comparison."""
    if not text: return ""
    text = text.lower()
    # Remove brackets ONLY if they don't contain key info like 'remix'
    if "remix" not in text and "live" not in text:
        text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def get_channel_trust_score(channel_name: str, artist_name: str) -> int:
    """Calculates how 'official' a channel looks."""
    channel_norm = channel_name.lower()
    artist_norm = artist_name.lower()
    score = 0

    # 1. Artist name is IN the channel name (High Trust)
    if fuzz.partial_ratio(artist_norm, channel_norm) > 85:
        score += 50

    # 2. "Topic" channels (The Gold Standard for Audio)
    if "topic" in channel_norm:
        score += 40
    
    # 3. VEVO or Official Keywords
    if any(label in channel_norm for label in OFFICIAL_LABELS):
        score += 25
        
    return score

def best_youtube(title, artist, target_duration):
    # Search Query: "Song Artist Official Audio" helps YouTube find the right one
    query = f"{title} {artist} Official Audio"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Fetch top 10 results
            search = ydl.extract_info(f"ytsearch10:{query}", download=False)
        except Exception:
            return None

    candidates = []
    
    target_title_norm = clean_text(title)
    target_artist_norm = clean_text(artist)
    
    # Smart Filtering: Don't ban "Remix" if the user ASKED for a Remix
    current_bad_keywords = [k for k in BASE_BAD_KEYWORDS if k not in target_title_norm]

    for e in search.get("entries", []):
        if not e: continue

        yt_id = e.get("id")
        yt_title = e.get("title") or ""
        yt_channel = e.get("uploader") or ""
        yt_duration = e.get("duration") or 0
        yt_views = e.get("view_count") or 0
        
        yt_title_norm = clean_text(yt_title)
        
        # 1. HARD FILTER: Check for garbage keywords
        if any(bad in yt_title_norm for bad in current_bad_keywords):
            continue
            
        # 2. DURATION CHECK: Must be within 45 seconds
        diff = abs(yt_duration - target_duration)
        if diff > 45: 
            continue

        # 3. SCORING SYSTEM
        score = 0
        
        # A. Title Match (0-100)
        # fuzz.token_set_ratio handles "Song - Artist" vs "Artist - Song"
        score += fuzz.token_set_ratio(target_title_norm, yt_title_norm)
        
        # B. Channel Trust (Crucial for correct Artist)
        score += get_channel_trust_score(yt_channel, artist)
        
        # C. Duration Precision (Topic videos are exact)
        if diff <= 5:
            score += 20
            
        # D. View Count (Prefer popular versions)
        if yt_views > 1000000: score += 15
        elif yt_views > 100000: score += 5

        # E. Penalty if Artist name is missing entirely
        if fuzz.partial_ratio(target_artist_norm, yt_title_norm) < 50 and \
           fuzz.partial_ratio(target_artist_norm, clean_text(yt_channel)) < 50:
            score -= 30

        candidates.append({
            "data": e,
            "score": score
        })

    # Sort by Score (Highest First)
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Return top result if it has a decent score
    if candidates and candidates[0]["score"] > 80:
        best = candidates[0]["data"]
        return {
            "id": best["id"],
            "title": title,
            "artist": best.get("uploader") or artist,
            "sourceArtist": artist,
            "duration": best.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
        }
    
    return None


# ---------- Models ----------
class ImportReq(BaseModel):
    playlistUrl: str
    limit: int | None = None

# =========================================================
# 1️⃣ PREVIEW SPOTIFY PLAYLIST
# =========================================================
@app.post("/preview-spotify")
def preview_spotify(data: ImportReq):
    # Reverted to your original URL logic
    playlist_id = data.playlistUrl.split("/")[-1].split("?")[0]
    playlist = sp.playlist(playlist_id)

    total_duration_ms = 0
    offset = 0

    while True:
        resp = sp.playlist_items(playlist_id, limit=100, offset=offset)
        items = resp["items"]
        if not items:
            break

        for item in items:
            if item["track"]:
                total_duration_ms += item["track"]["duration_ms"]

        offset += 100

    mins = total_duration_ms // 60000
    return {
        "playlist": {
            "id": playlist_id,
            "name": playlist["name"],
            "owner": playlist["owner"]["display_name"],
            "total_tracks": playlist["tracks"]["total"],
            "duration": f"{mins // 60} hr {mins % 60} min"
        }
    }

# =========================================================
# 2️⃣ IMPORT SPOTIFY PLAYLIST
# =========================================================
@app.post("/import-spotify")
def import_spotify(data: ImportReq):
    # Reverted to your original URL logic
    playlist_id = data.playlistUrl.split("/")[-1].split("?")[0]
    
    playlist_meta = sp.playlist(playlist_id)

    tracks = []
    offset = 0

    while True:
        resp = sp.playlist_items(playlist_id, limit=50, offset=offset)
        if not resp["items"]:
            break
        tracks.extend(resp["items"])
        offset += 50
        
        # Optimization: Don't fetch 5000 songs if limit is 10
        if data.limit and len(tracks) >= data.limit:
            break

    if data.limit:
        tracks = tracks[:data.limit]

    songs = []
    skipped = {"spotify_unavailable": 0, "no_youtube_match": 0}

    for item in tracks:
        track = item.get("track")
        if not track:
            skipped["spotify_unavailable"] += 1
            continue

        yt = best_youtube(
            track["name"],
            track["artists"][0]["name"],
            track["duration_ms"] // 1000
        )

        if yt:
            songs.append(yt)
        else:
            skipped["no_youtube_match"] += 1

    return {
        "playlist": {
            "id": playlist_id,
            "name": playlist_meta["name"],
            "total_tracks": playlist_meta["tracks"]["total"]
        },
        "stats": {
            "processed": len(tracks),
            "imported": len(songs),
            "skipped_spotify": skipped["spotify_unavailable"],
            "skipped_youtube": skipped["no_youtube_match"]
        },
        "songs": songs
    }
