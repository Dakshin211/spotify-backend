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


sp = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=os.getenv("SPOTIPY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET")
    )
)


ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": True,
    "ignoreerrors": True,
    "noplaylist": True
}

BASE_BAD_KEYWORDS = [
    "cover", "karaoke", "slowed", "reverb", "short",
    "instrumental", "8d", "nightcore", "bass boosted", "reaction", "review"
]


OFFICIAL_LABELS = [
    "vevo", "topic", "official", "sony", "t-series", "zee", 
    "warner", "universal", "monstercat", "record", "music", 
    "entertainment", "audio", "anirudh", "think music", "saregama", "lahari"
]

def clean_text(text: str) -> str:
    """Normalize text for comparison."""
    if not text: return ""
    text = text.lower()

    if not any(x in text for x in ["remix", "slowed", "reverb", "live"]):
        text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def get_channel_trust_score(channel_name: str, artist_name: str) -> int:
    """Calculates how 'official' a channel looks."""
    channel_norm = channel_name.lower()
    artist_norm = artist_name.lower()
    score = 0


    if fuzz.partial_ratio(artist_norm, channel_norm) > 85:
        score += 50
    if "topic" in channel_norm:
        score += 40

    if any(label in channel_norm for label in OFFICIAL_LABELS):
        score += 25
        
    return score

def best_youtube(title, artist, album, target_duration):

    search_query_parts = [title, artist]
    
    is_generic_album = any(x in album.lower() for x in ["greatest hits", "best of", "single", "compilation"])
    if album and not is_generic_album:
        search_query_parts.append(album)
    
    search_query_parts.append("Official Audio")
    query = " ".join(search_query_parts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:

            search = ydl.extract_info(f"ytsearch15:{query}", download=False)
        except Exception:
            return None

    candidates = []
    
    target_title_norm = clean_text(title)
    target_artist_norm = clean_text(artist)
    target_album_norm = clean_text(album)
    
    current_bad_keywords = []
    for bad in BASE_BAD_KEYWORDS:
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

        if any(bad in yt_title_norm for bad in current_bad_keywords):
            continue

        diff = abs(yt_duration - target_duration)
        if diff > 60: 
            continue

        score = 0
        
        score += fuzz.token_set_ratio(target_title_norm, yt_title_norm)
        
        if target_album_norm and fuzz.partial_ratio(target_album_norm, yt_title_norm) > 80:
            score += 20

        score += get_channel_trust_score(yt_channel, artist)
        if diff <= 5: score += 25
        elif diff <= 15: score += 15

        if yt_views > 1000000: score += 15
        elif yt_views > 100000: score += 10
        elif yt_views > 10000: score += 5

        if fuzz.partial_ratio(target_artist_norm, yt_title_norm) < 50 and \
           fuzz.partial_ratio(target_artist_norm, clean_text(yt_channel)) < 50:
            score -= 40
            
        candidates.append({"data": e, "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)

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

class ImportReq(BaseModel):
    playlistUrl: str
    limit: int | None = None


@app.post("/preview-spotify")
def preview_spotify(data: ImportReq):
    try:
        playlist_id = data.playlistUrl.split("/")[-1].split("?")[0]
        playlist = sp.playlist(playlist_id)
    except Exception as e:
        return {"error": str(e)}

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

        album_name = track["album"]["name"] if track.get("album") else ""

        yt = best_youtube(
            track["name"],
            track["artists"][0]["name"],
            album_name, 
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
