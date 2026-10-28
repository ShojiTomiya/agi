"""Where a session's files live.

Each session has one directory, uploads/<session_id>/, mounted into its sandbox
container as /workspace. User uploads, files the model writes and charts all sit
there under their real names, so the app, the sandbox and the browser all refer
to a file by the same name.
"""
import re
from pathlib import Path

UPLOADS_DIR = Path("uploads").resolve()
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}

_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def valid_session_id(session_id: str) -> bool:
    return bool(session_id and _SESSION_ID.match(session_id))


def session_dir(session_id: str) -> Path:
    if not valid_session_id(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    path = UPLOADS_DIR / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_path(session_id: str, name: str) -> Path:
    """Path of a file in the session directory; refuses names that point outside it."""
    base = session_dir(session_id)
    path = (base / name).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError(f"Invalid file name: {name!r}")
    return path


def unique_name(directory: Path, name: str) -> str:
    """`name`, or `stem (2).ext`, `stem (3).ext`… if it's already taken."""
    if not (directory / name).exists():
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = f"{stem} ({n})" + (f".{ext}" if dot else "")
        if not (directory / candidate).exists():
            return candidate
        n += 1


def is_image(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in IMAGE_EXTS
