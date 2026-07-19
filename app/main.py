import os
import urllib.request
import xml.etree.ElementTree as ET
import re
import logging
import traceback
import secrets

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel

from .database import Base, engine, get_db
from . import models


Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="MyTube Sync Backend",
    # If you're deploying behind a reverse proxy under a path prefix
    # (e.g. https://your-server.example.com/mytube-sync), set
    # MYTUBE_ROOT_PATH to that prefix. Leave unset for a bare deployment.
    root_path=os.environ.get("MYTUBE_ROOT_PATH", ""),
    docs_url=None,
    redoc_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("mytube")

# =========================
# Pydantic Schemas
# =========================

class SubCreate(BaseModel):
    channel_id: str


class HistoryUpdate(BaseModel):
    video_id: str
    progress_seconds: int
    completed: bool = False


class PlaylistCreate(BaseModel):
    name: str


class PlaylistItemCreate(BaseModel):
    video_id: str


class UserCreate(BaseModel):
    name: str


# =========================
# Authentication
# =========================

def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization format")

    token = authorization.replace("Bearer ", "").strip()

    user = db.query(models.User).filter_by(api_token=token).first()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    return user


# =========================
# Subscriptions
# =========================

@app.get("/subscriptions")
def get_subscriptions(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    return db.query(models.Subscription).filter_by(user_id=user.id).all()


@app.post("/subscriptions")
def add_subscription(
    subscription: SubCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    existing = db.query(models.Subscription).filter_by(
        user_id=user.id,
        channel_id=subscription.channel_id
    ).first()

    if existing:
        return existing

    new_subscription = models.Subscription(
        user_id=user.id,
        channel_id=subscription.channel_id
    )

    db.add(new_subscription)
    db.commit()
    db.refresh(new_subscription)

    logger.info(
        f"Subscription added user={user.name} channel={subscription.channel_id}"
    )

    return {
        "success": True,
        "message": f"Subscription added: {subscription.channel_id}",
        "data": {
            "id": new_subscription.id,
            "channel_id": new_subscription.channel_id
        }
    }


@app.delete("/subscriptions/{channel_id}")
def delete_subscription(
    channel_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    subscription = db.query(models.Subscription).filter_by(
        user_id=user.id,
        channel_id=channel_id
    ).first()

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    db.delete(subscription)
    db.commit()

    logger.info(
        f"Subscription removed user={user.name} channel={channel_id}"
    )

    return {
    "success": True,
    "message": f"Subscription removed: {channel_id}"
    }


def fetch_youtube_rss_videos(channel_id: str, limit: int = 5):
    channel_id = channel_id.strip()
    
    if channel_id.startswith("@"):
        try:
            profile_url = f"https://www.youtube.com/{channel_id}"
            req = urllib.request.Request(
                profile_url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                html_content = response.read().decode('utf-8', errors='ignore')
                
                match = re.search(r'meta itemprop="channelId" content="(UC[^"]+)"', html_content)
                if match:
                    resolved_id = match.group(1)
                    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={resolved_id}"
                else:
                    match_alt = re.search(r'href="https://www.youtube.com/channel/(UC[^"]+)"', html_content)
                    if match_alt:
                        resolved_id = match_alt.group(1)
                        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={resolved_id}"
                    else:
                        url = f"https://www.youtube.com/feeds/videos.xml?user={channel_id.replace('@', '')}"
        except Exception as err:
            logger.warning(
                f"Fallback warning: Could not map channel source metadata directly for {channel_id}: {err}"
            )
            logger.warning(traceback.format_exc())
            url = f"https://www.youtube.com/feeds/videos.xml?user={channel_id.replace('@', '')}"
    else:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            xml_data = response.read()
            
        root = ET.fromstring(xml_data)
        namespaces = {
            'atom': 'http://www.w3.org/2005/Atom',
            'yt': 'http://www.youtube.com/xml/schemas/2015'
        }
        
        videos = []
        for entry in root.findall('atom:entry', namespaces)[:limit]:
            video_id = entry.find('yt:videoId', namespaces)
            published = entry.find('atom:published', namespaces)

            if video_id is not None:
                videos.append({
                    "video_id": video_id.text,
                    "link": f"https://www.youtube.com/watch?v={video_id.text}",
                    "published": published.text if published is not None else None
                })
        return videos
    except Exception as e:
        logger.error(
            f"RSS fetch failed for {channel_id}: {e}"
        )
        logger.error(traceback.format_exc())
        return []


@app.get("/subscriptions/feed")
async def get_subscription_feed(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    subscriptions = db.query(models.Subscription).filter_by(user_id=user.id).all()
    
    feed_data = []
    for subscription in subscriptions:
        latest_videos = fetch_youtube_rss_videos(subscription.channel_id, limit=15)
        feed_data.append({
            "channel_id": subscription.channel_id,
            "videos": latest_videos
        })
        
    return feed_data


# =========================
# History
# =========================

@app.get("/history")
def get_history(
    limit: int = None,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    q = db.query(models.History).filter_by(
        user_id=user.id
    ).order_by(
        models.History.last_watched_at.desc()
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return q.all()


@app.post("/history/update")
def update_history(
    item: HistoryUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    history_entry = db.query(models.History).filter_by(
        user_id=user.id,
        video_id=item.video_id
    ).first()

    if history_entry:
        history_entry.progress_seconds = item.progress_seconds
        history_entry.completed = item.completed
    else:
        history_entry = models.History(
            user_id=user.id,
            video_id=item.video_id,
            progress_seconds=item.progress_seconds,
            completed=item.completed
        )

        db.add(history_entry)

    db.commit()
    db.refresh(history_entry)

    logger.info(
        f"History updated user={user.name} video={item.video_id} progress={item.progress_seconds}"
    )

    return {
        "success": True,
        "message": f"History updated for {item.video_id}",
        "data": {
            "id": history_entry.id,
            "video_id": history_entry.video_id,
            "progress_seconds": history_entry.progress_seconds
        }
    }
    

# =========================
# Playlists
# =========================

@app.get("/playlists")
def get_playlists(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    return db.query(models.Playlist).filter_by(
        user_id=user.id
    ).all()


@app.post("/playlists")
def create_playlist(
    pl: PlaylistCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    playlist = models.Playlist(
        user_id=user.id,
        name=pl.name
    )

    db.add(playlist)
    db.commit()
    db.refresh(playlist)

    logger.info(
        f"Playlist created user={user.name} name={pl.name}"
    )

    return {
        "success": True,
        "message": f"Playlist created {pl.name}",
        "data": {
            "id": playlist.id,
            "name": playlist.name
        }
    }


@app.delete("/playlists/{playlist_id}")
def delete_playlist(
    playlist_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    playlist = db.query(models.Playlist).filter_by(
        id=playlist_id,
        user_id=user.id
    ).first()

    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    db.delete(playlist)
    db.commit()

    logger.info(
        f"Playlist removed user={user.name} name={playlist.name}"
    )

    return {
        "success": True,
        "message": f"Playlist removed: {playlist.name}"
    }


@app.get("/playlists/{playlist_id}")
def get_playlist_items(
    playlist_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    playlist = db.query(models.Playlist).filter_by(
        id=playlist_id,
        user_id=user.id
    ).first()

    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    return db.query(models.PlaylistItem).filter_by(
        playlist_id=playlist_id
    ).order_by(
        models.PlaylistItem.position.asc()
    ).all()


@app.post("/playlists/{playlist_id}/add")
def add_to_playlist(
    playlist_id: int,
    item: PlaylistItemCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    playlist = db.query(models.Playlist).filter_by(
        id=playlist_id,
        user_id=user.id
    ).first()

    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    existing = db.query(models.PlaylistItem).filter_by(
        playlist_id=playlist_id,
        video_id=item.video_id
    ).first()

    if existing:
        return existing

    current_count = db.query(models.PlaylistItem).filter_by(
        playlist_id=playlist_id
    ).count()

    new_item = models.PlaylistItem(
        playlist_id=playlist_id,
        video_id=item.video_id,
        position=current_count + 1
    )

    db.add(new_item)

    db.commit()
    db.refresh(new_item)

    logger.info(
        f"Playlist item added user={user.name} playlist={playlist_id} video={item.video_id}"
    )

    return {
        "success": True,
        "message": f"Playlist item added {item.video_id}",
        "data": {
            "playlist_id": new_item.playlist_id,
            "video_id": new_item.video_id
        }
    }


@app.delete("/playlists/{playlist_id}/item/{video_id}")
def delete_playlist_item(
    playlist_id: int,
    video_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    playlist = db.query(models.Playlist).filter_by(
        id=playlist_id,
        user_id=user.id
    ).first()

    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    item = db.query(models.PlaylistItem).filter_by(
        playlist_id=playlist_id,
        video_id=video_id
    ).first()

    if not item:
        raise HTTPException(status_code=404, detail="Playlist item not found")

    db.delete(item)
    db.commit()

    logger.info(
        f"Playlist item removed user={user.name} playlist={playlist_id} video={item.video_id}"
    )

    return {
        "success": True,
        "message": f"Playlist item removed: {item.video_id}"
    }


# =========================
# Profiles
# =========================

@app.get("/users")
def get_profiles(
    db: Session = Depends(get_db),
):
    return [
        {
            "id": user.id,
            "name": user.name
        }
        for user in db.query(models.User).all()
    ]

@app.get("/current_user")
def get_current_user_info(
    user: models.User = Depends(get_current_user)
):
    return{
            "id": user.id,
            "name": user.name
        }
    
@app.post("/users")
def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db)
):
    existing = db.query(models.User).filter_by(
        name=user_data.name
    ).first()

    if existing:
        # Existing users keep their original token; we don't have it here
        # (it's not re-derivable), so we signal this explicitly rather than
        # returning a different response shape than the "created" case.
        return {
            "success": False,
            "message": f"User '{existing.name}' already exists. Use their existing API token to connect.",
            "user": {
                "id": existing.id,
                "name": existing.name
            },
            "token": None
        }

    token = secrets.token_urlsafe(32)

    user = models.User(
        name=user_data.name,
        api_token=token
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    liked = models.Playlist(
        user_id=user.id,
        name="Liked Videos"
    )

    later = models.Playlist(
        user_id=user.id,
        name="Watch Later"
    )

    db.add(liked)
    db.add(later)

    db.commit()

    logger.info(
        f"User created name={user.name}"
    )

    return {
        "success": True,
        "message": "User created",
        "user": {
            "id": user.id,
            "name": user.name
        },
        "token": token
    }

@app.delete("/users/current_user")
def delete_current_user(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user)
):
    username = user.name

    db.delete(user)
    db.commit()

    logger.info(
        f"User deleted name={username}"
    )

    return {
        "success": True,
        "message": f"User deleted: {username}"
    }

# =========================
# Frontend
# =========================

_FRONTEND_HTML = None

def _get_frontend_html():
    global _FRONTEND_HTML
    if _FRONTEND_HTML is None:
        here = os.path.dirname(__file__)
        path = os.path.join(here, "..", "frontend", "index.html")
        try:
            with open(path, encoding="utf-8") as f:
                _FRONTEND_HTML = f.read()
        except FileNotFoundError:
            _FRONTEND_HTML = "<h1>MyTube Sync</h1><p>Frontend not found.</p>"
    return _FRONTEND_HTML

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def frontend():
    return _get_frontend_html()


# =========================
# Healthcheck
# =========================

@app.get("/health")
def health():
    return {
        "status": "ok"
    }