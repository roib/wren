import logging
import mimetypes
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import admin, artwork, db, podcast, subscriptions, worker
from .config import BASE_URL, MEDIA_DIR, WEB_DIR, WORKER_ENABLED

# Slim container images lack /etc/mime.types; without these, media is served as
# application/octet-stream, which iOS Safari may refuse to play.
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("image/jpeg", ".jpg")
mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("application/manifest+json", ".webmanifest")

log = logging.getLogger("wren")
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s wren: %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False

background = worker.Worker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if WORKER_ENABLED:
        background.start()
    yield
    if WORKER_ENABLED:
        background.stop()


app = FastAPI(title="wren", lifespan=lifespan)


@app.middleware("http")
async def revalidate_app_files(request, call_next):
    # Without this, browsers guess the app's HTML/JS are fresh for hours after an
    # update and can pair a new page with an old script. Media can stay cached.
    response = await call_next(request)
    if not request.url.path.startswith("/media/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---- management app ---------------------------------------------------------------

@app.get("/api/overview")
def overview():
    """Everything the home and status screens show, in one request."""
    return {
        **admin.overview(),
        "worker": background.status(),
        "pending": subscriptions.list_pending(),
        "jobs": worker.list_jobs(20),
    }


@app.get("/api/shows/{show_id}/detail")
def show_detail(show_id: int):
    detail = admin.show_detail(show_id)
    if not detail:
        raise HTTPException(404, "no such show")
    return {**detail, "worker": background.status()}


@app.get("/api/singles")
def singles():
    return {**admin.singles_detail(), "worker": background.status()}


class ResolveRequest(BaseModel):
    url: str


@app.post("/api/resolve")
def resolve(req: ResolveRequest):
    try:
        return admin.resolve(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # network / yt-dlp trouble
        msg = str(e).splitlines()[0][:200]
        raise HTTPException(502, f"Couldn't look that up: {msg}")


@app.delete("/api/episodes/{video_id}", status_code=204)
def delete_episode(video_id: str):
    """Delete a downloaded episode, audio and video copies alike, from the server."""
    if not subscriptions.delete_episode(video_id):
        raise HTTPException(404, "no downloaded episode with that id")
    return Response(status_code=204)


@app.post("/api/shows/{show_id}/items/{item_id}/queue", status_code=202)
def requeue(show_id: int, item_id: str):
    if not subscriptions.requeue_item(show_id, item_id):
        raise HTTPException(404, "no skipped or failed episode with that id")
    background.download_now(show_id)
    return {"ok": True}


# ---- manual downloads ---------------------------------------------------------------

class AddRequest(BaseModel):
    url: str
    mode: str = "audio"


@app.post("/api/add", status_code=202)
def add(req: AddRequest):
    if req.mode not in ("audio", "video"):
        raise HTTPException(400, "mode must be 'audio' or 'video'")
    job_id = worker.add_job(req.url, req.mode)
    background.nudge()
    return {"job_id": str(job_id)}


@app.get("/api/jobs")
def list_jobs():
    # Keyed by id, as the app polls jobs[job_id].
    return {str(j["id"]): j for j in worker.list_jobs()}


# ---- subscriptions -----------------------------------------------------------------

class SubscribeRequest(BaseModel):
    url: str
    mode: str = "audio"
    keep_last: int = 10
    backfill: int = 3
    min_duration: int = 300
    title_filter: str | None = None


class ShowUpdate(BaseModel):
    name: str | None = None
    mode: str | None = None
    keep_last: int | None = None
    min_duration: int | None = None
    title_filter: str | None = None
    enabled: bool | None = None
    video_copy: bool | None = None


@app.get("/api/shows")
def list_shows():
    shows = subscriptions.list_shows()
    slugs = podcast.feed_slugs(shows)
    for s in shows:
        s["feed_path"] = f"/feeds/{slugs[s['id']]}.xml"
        s["video_feed_path"] = f"/feeds/{slugs[s['id']]}-video.xml" if s["mode"] == "audio" else None
    return shows


@app.post("/api/shows", status_code=201)
def subscribe(req: SubscribeRequest):
    try:
        show = subscriptions.subscribe(**req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # network / yt-dlp trouble while looking up the show
        raise HTTPException(502, f"couldn't load that show: {e}")
    background.nudge()
    # Make its artwork now, not at the next feed check (which can be hours away);
    # in the background, as fetching the channel picture takes a moment.
    threading.Thread(target=subscriptions.ensure_art, args=(show,), daemon=True).start()
    return show


@app.patch("/api/shows/{show_id}")
def update_show(show_id: int, req: ShowUpdate):
    fields = req.model_dump(exclude_unset=True)
    try:
        show = subscriptions.update_show(show_id, **fields)
    except KeyError:
        raise HTTPException(404, "no such show")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if fields.get("video_copy"):
        background.download_now(show_id)  # start on the existing episodes right away
    return show


@app.delete("/api/shows/{show_id}", status_code=204)
def unsubscribe(show_id: int):
    if not subscriptions.get_show(show_id):
        raise HTTPException(404, "no such show")
    subscriptions.unsubscribe(show_id)
    return Response(status_code=204)


@app.post("/api/shows/{show_id}/refresh", status_code=202)
def refresh_show(show_id: int):
    if not subscriptions.get_show(show_id):
        raise HTTPException(404, "no such show")
    background.refresh(show_id)
    return {"ok": True}


@app.post("/api/shows/{show_id}/download-now", status_code=202)
def download_show_now(show_id: int):
    if not subscriptions.get_show(show_id):
        raise HTTPException(404, "no such show")
    background.download_now(show_id)
    return {"ok": True}


@app.post("/api/refresh", status_code=202)
def refresh_all():
    background.refresh()
    return {"ok": True}


@app.post("/api/download-now", status_code=202)
def download_now():
    background.download_now()
    return {"ok": True}


@app.get("/api/queue")
def queue():
    with db.connect() as conn:
        items = [dict(r) for r in conn.execute(
            """SELECT f.show_id, s.name AS show, f.item_id, f.title, f.attempts, f.error, f.next_try
               FROM feed_items f JOIN shows s ON s.id = f.show_id
               WHERE f.status = 'queued' ORDER BY f.rowid""")]
    jobs = [j for j in worker.list_jobs() if j["status"] in ("queued", "running")]
    return {"worker": background.status(), "jobs": jobs, "items": items}


# ---- podcast feeds ------------------------------------------------------------------
# RSS feeds of the library, for subscribing in a native podcast app (Apple
# Podcasts, Audiobookshelf...), whose lock-screen controls iOS doesn't limit.

def _base_url(request: Request) -> str:
    if BASE_URL:
        return BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _feed_response(xml: str) -> Response:
    return Response(xml, media_type="application/rss+xml; charset=utf-8")


def _show_feed(show: dict, request: Request, video: bool = False) -> Response:
    """The show's episodes in its own format, or (video=True) its video copies."""
    mode = "video" if video else show["mode"]
    eps = [ep for ep in db.list_episodes() if ep["show_id"] == show["id"] and ep["mode"] == mode]
    title = f"{show['name']} (video)" if video and show["mode"] == "audio" else show["name"]
    base = _base_url(request)
    return _feed_response(podcast.render(
        title, f"{show['name']}, downloaded by Wren.", eps, base, f"{base}{request.url.path}",
        art=artwork.feed_ref(artwork.path_for(show["id"]))))


@app.get("/feeds/singles.xml")
def singles_feed(request: Request):
    """Episodes added one at a time, plus those left from removed subscriptions."""
    shows = {s["id"] for s in subscriptions.list_shows()}
    eps = [ep for ep in db.list_episodes() if ep["show_id"] not in shows]
    base = _base_url(request)
    artwork.ensure_singles_art()
    return _feed_response(podcast.render(
        "Wren: single episodes", "Episodes added to Wren one at a time.", eps, base, f"{base}{request.url.path}",
        art=artwork.feed_ref(artwork.singles_path())))


@app.get("/feeds/show/{show_id}.xml")
def show_feed_by_id(show_id: int, request: Request):
    """By id: keeps working if a show is renamed."""
    show = subscriptions.get_show(show_id)
    if not show:
        raise HTTPException(404, "No such show")
    return _show_feed(show, request)


@app.get("/feeds/{name}.xml")
def show_feed(name: str, request: Request):
    shows = subscriptions.list_shows()
    slugs = podcast.feed_slugs(shows)
    show = next((s for s in shows if slugs[s["id"]] == name), None)
    if show:
        return _show_feed(show, request)
    # "<show>-video": the show's video copies.
    if name.endswith("-video"):
        show = next((s for s in shows if slugs[s["id"]] == name[:-6]), None)
        if show:
            return _show_feed(show, request, video=True)
    raise HTTPException(404, "No such show")


# StaticFiles serves files via FileResponse, which handles HTTP Range requests
# (needed by Safari for seeking / playing media).
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
