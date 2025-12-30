from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from difflib import SequenceMatcher
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

# ---------- yt-dlp ----------
ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True
}

BAD_KEYWORDS = [
    "live", "remix", "cover", "karaoke", "slowed",
    "reverb", "short", "instrumental", "8d", "nightcore"
]

TRUSTED_CHANNEL_HINTS = ["topic", "vevo", "official"]

# ---------- Utils ----------
def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()

def best_youtube(title, artist, target_duration):
    query = f"{title} {artist}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        search = ydl.extract_info(
            f"ytsearch12:{query}",
            download=False
        )

    best = None
    best_score = -1

    for e in search.get("entries", []):
        if not e:
            continue

        yt_title = e.get("title") or ""
        channel = e.get("uploader") or ""
        duration = e.get("duration") or 0
        views = e.get("view_count") or 0

        yt_title_l = yt_title.lower()
        channel_l = channel.lower()

        # ❌ Hard reject junk
        if any(bad in yt_title_l for bad in BAD_KEYWORDS):
            continue

        score = 0

        # 1️⃣ Title similarity (MAIN)
        title_sim = similarity(title, yt_title)
        score += title_sim * 45

        # 2️⃣ Artist presence
        if artist.lower() in yt_title_l:
            score += 25
        if artist.lower() in channel_l:
            score += 20

        # 3️⃣ Trusted channels
        if any(hint in channel_l for hint in TRUSTED_CHANNEL_HINTS):
            score += 15

        # 4️⃣ Duration penalty (important)
        diff = abs(duration - target_duration)
        if diff <= 3:
            score += 15
        elif diff <= 7:
            score += 8
        elif diff <= 12:
            score += 3
        else:
            score -= 20  # HARD penalty for wrong length

        # 5️⃣ Popularity (small influence)
        if views:
            score += min(10, views ** 0.25)

        # ❌ Reject weak matches
        if title_sim < 0.45:
            continue

        if score > best_score:
            best = e
            best_score = score

    # No confident match
    if not best or best_score < 55:
        return None

    return {
        "id": best["id"],
        "title": title,
        "artist": best.get("uploader") or artist,
        "sourceArtist": artist,
        "duration": best.get("duration"),
        "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
    }

# ---------- Models ----------
class ImportReq(BaseModel):
    playlistUrl: str
    limit: int | None = None

# =========================================================
# 1️⃣ PREVIEW SPOTIFY PLAYLIST
# =========================================================
@app.post("/preview-spotify")
def preview_spotify(data: ImportReq):
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

    if data.limit:
        tracks = tracks[:data.limit]

    songs = []
    skipped = {
        "spotify_unavailable": 0,
        "no_youtube_match": 0
    }

    for item in tracks:
        track = item["track"]
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
