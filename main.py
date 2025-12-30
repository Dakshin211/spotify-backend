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

# Standard junk keywords
BASE_BAD_KEYWORDS = [
    "cover", "karaoke", "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore", "bass boosted", "reaction", "review"
]

# Trusted official channel hints
OFFICIAL_LABELS = [
    "vevo", "topic", "official", "sony", "t-series", "zee", 
    "warner", "universal", "monstercat", "record", "music", 
    "entertainment", "audio", "anirudh", "think music", "saregama", "lahari"
]

# ---------- Utils ----------

def clean_text(text: str) -> str:
    """Normalize text for comparison."""
    if not text: return ""
    text = text.lower()
    # Remove brackets ONLY if they don't contain key info like 'remix' or 'slowed'
    if not any(x in text for x in ["remix", "slowed", "reverb", "live"]):
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

    # 2. "Topic" channels (The Gold Standard)
    if "topic" in channel_norm:
        score += 40
    
    # 3. VEVO or Official Keywords
    if any(label in channel_norm for label in OFFICIAL_LABELS):
        score += 25
        
    return score

def best_youtube(title, artist, album, target_duration):
    # ---------------------------------------------------------
    # STRATEGY 1: QUERY CONSTRUCTION
    # ---------------------------------------------------------
    # We include the ALBUM name in the search query. 
    # For movies/soundtracks, Album = Movie Name, which solves duplicates.
    # We filter out generic album names like "Greatest Hits" or "Singles"
    search_query_parts = [title, artist]
    
    is_generic_album = any(x in album.lower() for x in ["greatest hits", "best of", "single", "compilation"])
    if album and not is_generic_album:
        search_query_parts.append(album)
    
    search_query_parts.append("Official Audio")
    query = " ".join(search_query_parts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # INCREASED SEARCH LIMIT: 10 -> 15 (Reduces skips)
            search = ydl.extract_info(f"ytsearch15:{query}", download=False)
        except Exception:
            return None

    candidates = []
    
    target_title_norm = clean_text(title)
    target_artist_norm = clean_text(artist)
    target_album_norm = clean_text(album)
    
    # ---------------------------------------------------------
    # STRATEGY 2: DYNAMIC BAD KEYWORDS
    # ---------------------------------------------------------
    # If the Spotify song IS "Slowed", we REMOVE "slowed" from the ban list.
    current_bad_keywords = []
    for bad in BASE_BAD_KEYWORDS:
        # If the bad keyword is present in the OFFICIAL Spotify title, allow it.
        if bad in target_title_norm:
            continue 
        current_bad_keywords.append(bad)

    for e in search.get("entries", []):
        if not e: continue

        yt_id = e.get("id")
        yt_title = e.get("title") or ""
        yt_channel = e.get("uploader") or ""
        yt_duration = e.get("duration") or 0
        yt_views = e.get("view_count") or 0
        
        yt_title_norm = clean_text(yt_title)
        
        # 1. HARD FILTER: Garbage Collection
        if any(bad in yt_title_norm for bad in current_bad_keywords):
            continue
            
        # 2. DURATION CHECK (Slightly Relaxed)
        # We allow a larger gap (+/- 60s) for "Official Videos" that have long intros
        # But we punish the score later if the gap is big.
        diff = abs(yt_duration - target_duration)
        if diff > 60: 
            continue

        # 3. SCORING SYSTEM
        score = 0
        
        # A. Title Match (Base Score)
        score += fuzz.token_set_ratio(target_title_norm, yt_title_norm)
        
        # B. Album/Movie Match (Fixes "Kadhal Aasai" issue)
        # If the YouTube title contains the Album/Movie name, huge bonus.
        if target_album_norm and fuzz.partial_ratio(target_album_norm, yt_title_norm) > 80:
            score += 20
        
        # C. Channel Trust
        score += get_channel_trust_score(yt_channel, artist)
        
        # D. Duration Precision
        if diff <= 5: score += 25
        elif diff <= 15: score += 15
        
        # E. View Count (Logarithmic Boost)
        if yt_views > 1000000: score += 15
        elif yt_views > 100000: score += 10
        elif yt_views > 10000: score += 5

        # F. Penalties
        # Penalty if Artist is missing from both Title and Channel
        if fuzz.partial_ratio(target_artist_norm, yt_title_norm) < 50 and \
           fuzz.partial_ratio(target_artist_norm, clean_text(yt_channel)) < 50:
            score -= 40
            
        candidates.append({"data": e, "score": score})

    # Sort by Score
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # ---------------------------------------------------------
    # STRATEGY 3: LOWERED THRESHOLD
    # ---------------------------------------------------------
    # Was 80, now 50. 
    # If the top result is "okay" (50), we take it rather than skipping.
    if candidates and candidates[0]["score"] > 50:
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
    try:
        playlist_id = data.playlistUrl.split("/")[-1].split("?")[0]
        playlist = sp.playlist(playlist_id)
    except:
        return {"error": "Invalid Spotify URL"}

    total_duration_ms = 0
    offset = 0

    while True:
        resp = sp.playlist_items(playlist_id, limit=100, offset=offset)
        items = resp["items"]
        if not items: break

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
    try:
        playlist_id = data.playlistUrl.split("/")[-1].split("?")[0]
        playlist_meta = sp.playlist(playlist_id)
    except:
        return {"error": "Invalid Spotify URL"}

    tracks = []
    offset = 0

    while True:
        resp = sp.playlist_items(playlist_id, limit=50, offset=offset)
        if not resp["items"]: break
        tracks.extend(resp["items"])
        offset += 50
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

        # Extract Album Name safely
        album_name = track["album"]["name"] if track.get("album") else ""

        yt = best_youtube(
            track["name"],
            track["artists"][0]["name"],
            album_name, # <-- PASSING ALBUM NAME HERE
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
