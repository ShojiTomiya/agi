import uuid
import json
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from config import CONFIG
from database import (
    init_db, migrate_db, create_session, save_message,
    get_messages, get_sessions, delete_session, update_session_time,
    get_session_tokens, rename_session, truncate_from_message,
    save_step, get_turn_steps
)
from files import ALL_SUPPORTED
import context as ctx
import kernel
import storage
from agent import run_agent


# ── helpers ──────────────────────────────────────────────────────────────────

def format_step_for_sse(step: dict) -> str:
    return f"event: step\ndata: {json.dumps(step)}\n\n"


def format_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

# ── setup ────────────────────────────────────────────────────────────────────

def _ensure_sandbox():
    try:
        check = subprocess.run(
            ["docker", "image", "inspect", "agi-sandbox:latest"],
            capture_output=True
        )
    except FileNotFoundError:
        print("WARNING: docker not found — execute_python tool will not work.")
        return
    if check.returncode != 0:
        print("Building agi-sandbox image (first run)...")
        result = subprocess.run(
            ["docker", "build", "-f", "Dockerfile.sandbox", "-t", "agi-sandbox:latest", "."],
        )
        if result.returncode != 0:
            print("WARNING: failed to build agi-sandbox — execute_python tool will not work. Is Docker running?")
            return
    kernel.cleanup_orphans()

_ensure_sandbox()

app = FastAPI()

storage.UPLOADS_DIR.mkdir(exist_ok=True)
Path("db").mkdir(exist_ok=True)

init_db()
migrate_db()

@app.middleware("http")
async def revalidate(request, call_next):
    # The frontend changes with the backend, and the model can rewrite a session
    # file under the same name: the browser must check for a newer version (a cheap
    # 304 when nothing changed) instead of reusing a stale copy.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith(("/static/", "/uploads/")):
        response.headers["Cache-Control"] = "no-cache"
    return response

app.mount("/static", StaticFiles(directory="static"), name="static")
# uploads/<session_id>/<name> — the same files the session's sandbox sees as /workspace
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

ALLOWED_EXTENSIONS = set(ALL_SUPPORTED)
MAX_FILE_SIZE_MB = 20

# ── models ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    files: list[str] = []      # names of files already uploaded to this session

# ── routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/sessions")
def list_sessions():
    return get_sessions()


@app.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str):
    return get_messages(session_id)


@app.delete("/sessions/{session_id}")
def remove_session(session_id: str):
    delete_session(session_id)
    try:
        kernel.stop(session_id)
    except Exception:
        pass
    if storage.valid_session_id(session_id):
        shutil.rmtree(storage.UPLOADS_DIR / session_id, ignore_errors=True)
    return {"status": "ok"}

@app.get("/turns/{user_msg_id}/steps")
def turn_steps(user_msg_id: int):
    return get_turn_steps(user_msg_id)

@app.get("/sessions/{session_id}/tokens")
def session_tokens(session_id: str):
    return {"total": get_session_tokens(session_id)}

@app.patch("/sessions/{session_id}")
def rename_session_endpoint(session_id: str, body: dict):
    rename_session(session_id, body["title"])
    return {"status": "ok"}

@app.delete("/sessions/{session_id}/messages/{from_id}")
def truncate_messages(session_id: str, from_id: int):
    # The sandbox keeps running: its files are the session's files (an edited
    # message keeps its attachments) and variables from earlier turns stay usable.
    truncate_from_message(session_id, from_id)
    return {"status": "ok"}

@app.post("/reload-prompt")
def reload_prompt():
    ctx.SYSTEM_PROMPT = ctx.load_system_prompt()
    return {"status": "ok", "preview": ctx.SYSTEM_PROMPT[:120]}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), session_id: str = Form(...)):
    if not storage.valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")

    # drop any directory part the client sent
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    # validate extension
    ext = filename.split(".")[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '.{ext}'. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # validate size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail=f"File too large (max {MAX_FILE_SIZE_MB}MB)"
        )

    # a second file with the same name becomes "name (2).ext" instead of
    # replacing the one an earlier message refers to
    directory = storage.session_dir(session_id)
    name = storage.unique_name(directory, filename)
    (directory / name).write_bytes(content)

    return {"name": name}


@app.post("/chat")
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    if not storage.valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    # names come from the browser and are read from disk: only files in this session
    try:
        if not all(storage.session_path(session_id, n).is_file() for n in req.files):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown file")

    title = req.message[:60] if req.message else "Attached file"
    create_session(session_id, title)

    # load history (keep metadata for image reconstruction)
    history = get_messages(session_id)
    last_user = next((m for m in reversed(history) if m["role"] == "user"), None)
    previous_steps = get_turn_steps(last_user["id"]) if last_user else []

    def generate():
        # saved before the agent runs, so a failed turn is still there to edit or retry
        user_msg_id = save_message(session_id, "user", req.message, {"files": req.files})
        update_session_time(session_id)
        yield format_event("session", {"session_id": session_id, "user_msg_id": user_msg_id})

        answer = model = None
        usage  = {}
        seq    = 0

        for event in run_agent(req.message, req.files, history,
                               session_id=session_id, previous_steps=previous_steps):
            if event["type"] == "step":
                step = {k: v for k, v in event.items() if k != "type"}
                if step["step"] != "reasoning_start":   # UI cue only, no content
                    seq += 1
                    save_step(session_id, user_msg_id, seq, step["step"], step)
                yield format_step_for_sse(step)
            elif event["type"] == "token":
                yield format_event("token", {"token": event["token"]})
            elif event["type"] == "error":
                seq += 1
                save_step(session_id, user_msg_id, seq, "error", {"step": "error", "message": event["message"]})
                save_message(session_id, "assistant", event["message"], {"error": True})
                yield format_event("error", {"message": event["message"]})
                return
            elif event["type"] == "done":
                answer = event["answer"]
                usage  = event["usage"] or {}
                model  = event["model"]
                charts = event.get("charts", [])
                files  = event.get("files", [])

        if answer is None:
            return

        prompt_tokens     = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        save_message(session_id, "assistant", answer, {
            "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
            "model": model,
            "charts": charts,
            "files": files,
        }, prompt_tokens + completion_tokens)
        update_session_time(session_id)

        yield format_event("done", {
            "session_id": session_id, "user_msg_id": user_msg_id, "model": model,
            "charts": charts,
            "files": files,
        })

    return StreamingResponse(generate(), media_type="text/event-stream")
