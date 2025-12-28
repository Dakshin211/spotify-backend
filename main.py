from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from difflib import SequenceMatcher
import os

app = FastAPI()

# ---------- CORS (IMPORTANT) ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # later restrict to your frontend domain
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

BAD = ["live", "remix", "cover", "karaoke", "slowed", "reverb", "short"]

def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def best_youtube(title, artist, target_duration=None):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        search = ydl.extract_info(
            f"ytsearch10:{title} {artist}",
            download=False
        )

    best = None
    best_score = -1

    for e in search.get("entries", []):
        if not e:
            continue

        yt_title = (e.get("title") or "").lower()
        channel = (e.get("uploader") or "").lower()
        duration = e.get("duration") or 0
        views = e.get("view_count") or 0

        if any(x in yt_title for x in BAD):
            continue

        score = 0
        score += similarity(title, yt_title) * 40

        if artist.lower() in yt_title:
            score += 25
        if artist.lower() in channel:
            score += 20

        if "topic" in channel or "vevo" in channel:
            score += 15

        if target_duration and duration:
            diff = abs(duration - target_duration)
            if diff < 3:
                score += 15
            elif diff < 7:
                score += 10
            elif diff < 12:
                score += 5

        if views > 0:
            score += min(15, (views ** 0.25))

        if score > best_score:
            best = e
            best_score = score

    if not best and search.get("entries"):
        best = search["entries"][0]

    if not best:
        return None

    return {
        "id": best["id"],
        "title": title,
        "artist": best.get("uploader") or artist,
        "sourceArtist": artist,
        "duration": best.get("duration"),
        "thumbnail": f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg"
    }

# ---------- API ----------
class ImportReq(BaseModel):
    playlistUrl: str

@app.post("/import-spotify")
def import_playlist(data: ImportReq):
    playlist_id = data.playlistUrl.split("/")[-1].split("?")[0]
    tracks = sp.playlist_items(playlist_id, limit=50)["items"]

    songs = []
    for item in tracks:
        t = item["track"]
        yt = best_youtube(
            t["name"],
            t["artists"][0]["name"],
            t["duration_ms"] // 1000
        )
        if yt:
            songs.append(yt)

    return {"songs": songs}
