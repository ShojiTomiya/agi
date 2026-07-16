import uuid
import json
import subprocess
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from config import CONFIG
from database import (
    init_db, migrate_db, create_session, save_message,
    get_messages, get_sessions, delete_session, update_session_time,
    get_session_tokens, rename_session, truncate_from_message
)
from files import ALL_SUPPORTED
import context as ctx
from agent import run_agent


# ── helpers ──────────────────────────────────────────────────────────────────

def format_step_for_sse(step: dict) -> str:
    return f"event: step\ndata: {json.dumps(step)}\n\n"


def format_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

# ── setup ────────────────────────────────────────────────────────────────────

def _ensure_sandbox():
    check = subprocess.run(
        ["docker", "image", "inspect", "agi-sandbox:latest"],
        capture_output=True
    )
    if check.returncode != 0:
        print("Building agi-sandbox image (first run)...")
        result = subprocess.run(
            ["docker", "build", "-f", "Dockerfile.sandbox", "-t", "agi-sandbox:latest", "."],
        )
        if result.returncode != 0:
            print("WARNING: failed to build agi-sandbox — execute_python tool will not work. Is Docker running?")

_ensure_sandbox()

app = FastAPI()

Path("uploads").mkdir(exist_ok=True)
Path("db").mkdir(exist_ok=True)

init_db()
migrate_db()

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

ALLOWED_EXTENSIONS = set(ALL_SUPPORTED)
MAX_FILE_SIZE_MB = 20

# ── models ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    file_paths: list[str] = []
    filenames: list[str] = []

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
    for msg in get_messages(session_id):
        for path in msg.get("metadata", {}).get("file_paths", []):
            p = path["path"] if isinstance(path, dict) else path
            Path(p).unlink(missing_ok=True)
    delete_session(session_id)
    try:
        import kernel
        kernel.stop(session_id)
    except Exception:
        pass
    return {"status": "ok"}

@app.get("/sessions/{session_id}/tokens")
def session_tokens(session_id: str):
    return {"total": get_session_tokens(session_id)}

@app.patch("/sessions/{session_id}")
def rename_session_endpoint(session_id: str, body: dict):
    rename_session(session_id, body["title"])
    return {"status": "ok"}

@app.delete("/sessions/{session_id}/messages/{from_id}")
def truncate_messages(session_id: str, from_id: int):
    truncate_from_message(session_id, from_id)
    try:
        import kernel
        kernel.stop(session_id)
    except Exception:
        pass
    return {"status": "ok"}

@app.post("/reload-prompt")
def reload_prompt():
    ctx.SYSTEM_PROMPT = ctx.load_system_prompt()
    return {"status": "ok", "preview": ctx.SYSTEM_PROMPT[:120]}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    # validate extension
    ext = file.filename.split(".")[-1].lower()
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

    # save to disk
    file_id = str(uuid.uuid4())
    path = f"uploads/{file_id}_{file.filename}"
    with open(path, "wb") as f:
        f.write(content)

    return {"file_id": file_id, "filename": file.filename, "path": path}


@app.post("/chat")
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    title = req.message[:60] if req.message else "Attached file"
    create_session(session_id, title)

    # load history (keep metadata for image reconstruction)
    history = get_messages(session_id)

    def generate():
        yield format_event("session", {"session_id": session_id})

        answer = model = None
        usage  = {}

        for event in run_agent(req.message, req.file_paths, req.filenames, history, session_id=session_id):
            if event["type"] == "step":
                yield format_step_for_sse({k: v for k, v in event.items() if k != "type"})
            elif event["type"] == "token":
                yield format_event("token", {"token": event["token"]})
            elif event["type"] == "error":
                yield format_event("error", {"message": event["message"]})
                return
            elif event["type"] == "done":
                answer      = event["answer"]
                usage       = event["usage"] or {}
                model       = event["model"]
                chart_paths = event.get("chart_paths", [])
                file_paths  = event.get("file_paths", [])

        if answer is None:
            return

        prompt_tokens     = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        user_msg_id = save_message(session_id, "user", req.message, {"files": req.filenames, "file_paths": req.file_paths}, prompt_tokens)
        save_message(session_id, "assistant", answer, {
            "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
            "model": model,
            "chart_paths": chart_paths,
            "file_paths": file_paths,
        }, completion_tokens)
        update_session_time(session_id)

        yield format_event("done", {
            "session_id": session_id, "user_msg_id": user_msg_id, "model": model,
            "chart_paths": chart_paths,
            "file_paths": file_paths,
        })

    return StreamingResponse(generate(), media_type="text/event-stream")