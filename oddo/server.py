from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, downloader
from .config import MEDIA_DIR, WEB_DIR

app = FastAPI(title="oddo")

# Simple in-memory job status; good enough until there's a real queue.
jobs: dict[str, dict] = {}


class AddRequest(BaseModel):
    url: str
    mode: str = "audio"


def _run_download(job_id: str, url: str, mode: str) -> None:
    jobs[job_id] = {"url": url, "mode": mode, "status": "downloading"}
    try:
        ep = downloader.download(url, mode)
        jobs[job_id] |= {"status": "done", "title": ep["title"]}
    except Exception as e:  # surface yt-dlp errors to the client
        jobs[job_id] |= {"status": "error", "error": str(e)}


@app.get("/api/library")
def library():
    episodes = db.list_episodes()
    for ep in episodes:
        ep["media_url"] = f"/media/{ep['file']}"
        ep["thumb_url"] = f"/media/{ep['thumb']}" if ep["thumb"] else None
    return episodes


@app.post("/api/add", status_code=202)
def add(req: AddRequest, background: BackgroundTasks):
    if req.mode not in ("audio", "video"):
        raise HTTPException(400, "mode must be 'audio' or 'video'")
    job_id = f"{len(jobs) + 1}"
    jobs[job_id] = {"url": req.url, "mode": req.mode, "status": "queued"}
    background.add_task(_run_download, job_id, req.url, req.mode)
    return {"job_id": job_id}


@app.get("/api/jobs")
def list_jobs():
    return jobs


# StaticFiles serves files via FileResponse, which handles HTTP Range requests
# (needed by Safari for seeking / playing media).
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
