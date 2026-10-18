import logging
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, subscriptions, worker
from .config import MEDIA_DIR, WEB_DIR, WORKER_ENABLED

# Slim container images lack /etc/mime.types; without these, media is served as
# application/octet-stream, which iOS Safari may refuse to play.
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("image/jpeg", ".jpg")
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


# ---- library ----------------------------------------------------------------------

@app.get("/api/library")
def library():
    episodes = db.list_episodes()
    for ep in episodes:
        ep["media_url"] = f"/media/{ep['file']}"
        ep["thumb_url"] = f"/media/{ep['thumb']}" if ep["thumb"] else None
    return episodes


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


@app.get("/api/shows")
def list_shows():
    return subscriptions.list_shows()


@app.post("/api/shows", status_code=201)
def subscribe(req: SubscribeRequest):
    try:
        show = subscriptions.subscribe(**req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # network / yt-dlp trouble while looking up the show
        raise HTTPException(502, f"couldn't load that show: {e}")
    background.nudge()
    return show


@app.patch("/api/shows/{show_id}")
def update_show(show_id: int, req: ShowUpdate):
    try:
        return subscriptions.update_show(show_id, **req.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(404, "no such show")
    except ValueError as e:
        raise HTTPException(400, str(e))


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


@app.get("/api/pending")
def pending():
    """Episodes on their way: what the app shows greyed out until they arrive."""
    return {"worker": background.status(), "items": subscriptions.list_pending()}


@app.get("/api/queue")
def queue():
    with db.connect() as conn:
        items = [dict(r) for r in conn.execute(
            """SELECT f.show_id, s.name AS show, f.item_id, f.title, f.attempts, f.error, f.next_try
               FROM feed_items f JOIN shows s ON s.id = f.show_id
               WHERE f.status = 'queued' ORDER BY f.rowid""")]
    jobs = [j for j in worker.list_jobs() if j["status"] in ("queued", "running")]
    return {"worker": background.status(), "jobs": jobs, "items": items}


# StaticFiles serves files via FileResponse, which handles HTTP Range requests
# (needed by Safari for seeking / playing media).
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
