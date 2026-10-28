"""Per-session Docker container management for the persistent REPL."""
import json
import os
import subprocess
import threading
import time
import urllib.request

import storage

# A cell is interrupted inside the sandbox after EXEC_TIMEOUT, like Ctrl+C in Jupyter:
# variables survive and the model is told the line it was stuck on. Cells here take
# seconds; what ran into the old 60 s limit were loops that never end.
EXEC_TIMEOUT = 30
# Code stuck inside a C call can't be interrupted; after this much longer the whole
# container is replaced.
HARD_TIMEOUT_GRACE = 15
REPL_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repl_server.py")

_sessions: dict = {}
_lock = threading.Lock()


def _name(session_id: str) -> str:
    return f"agi-kernel-{session_id}"


def cleanup_orphans():
    """Remove kernel containers left running by a previous server process."""
    r = subprocess.run(
        ['docker', 'ps', '-aq', '--filter', 'name=agi-kernel-'],
        capture_output=True, text=True,
    )
    ids = r.stdout.split()
    if ids:
        subprocess.run(['docker', 'rm', '-f', *ids], capture_output=True)


def _get_port(name: str) -> int:
    r = subprocess.run(['docker', 'port', name, '8080'], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Cannot get port for {name}: {r.stderr.strip()}")
    return int(r.stdout.strip().split(':')[-1])


def _wait_ready(port: int, retries: int = 30):
    for _ in range(retries):
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("REPL server did not become ready in time")


def _start(session_id: str) -> dict:
    name = _name(session_id)
    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
    subprocess.run([
        'docker', 'create', '--rm',
        '--name', name,
        '-p', '127.0.0.1::8080',
        # the session's directory is the workspace, so files survive a restarted
        # container and nothing has to be copied in or out
        '-v', f'{storage.session_dir(session_id)}:/workspace',
        '--memory', '512m',
        '--pids-limit', '64',
        'agi-sandbox:latest',
        'python', '/repl_server.py',
    ], check=True, capture_output=True)
    # copy the current repl_server.py in, so edits to it apply without rebuilding the image
    subprocess.run(['docker', 'cp', REPL_SERVER, f'{name}:/repl_server.py'], check=True, capture_output=True)
    subprocess.run(['docker', 'start', name], check=True, capture_output=True)
    port = _get_port(name)
    _wait_ready(port)
    entry = {'name': name, 'port': port, 'lock': threading.Lock()}
    _sessions[session_id] = entry
    return entry


def _get_or_create(session_id: str) -> dict:
    with _lock:
        entry = _sessions.get(session_id)
        if entry:
            r = subprocess.run(
                ['docker', 'inspect', '--format', '{{.State.Running}}', entry['name']],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and 'true' in r.stdout:
                return entry
        return _start(session_id)


def stop(session_id: str):
    with _lock:
        entry = _sessions.pop(session_id, None)
    if entry:
        subprocess.run(['docker', 'rm', '-f', entry['name']], capture_output=True)


def execute(session_id: str, code: str) -> dict:
    entry = _get_or_create(session_id)
    port = entry['port']

    with entry['lock']:
        payload = json.dumps({'code': code, 'timeout': EXEC_TIMEOUT}).encode()
        req = urllib.request.Request(
            f'http://127.0.0.1:{port}/exec',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=EXEC_TIMEOUT + HARD_TIMEOUT_GRACE) as resp:
                return json.loads(resp.read())
        except Exception as e:
            timed_out = isinstance(e, TimeoutError) or isinstance(getattr(e, 'reason', None), TimeoutError)
            if not timed_out:
                return {'stdout': '', 'error': str(e), 'images': [], 'files': []}

    # The interrupt didn't work and the single-threaded REPL server is still busy, so
    # every later call in this session would time out too — replace the container.
    stop(session_id)
    return {
        'stdout': '',
        'error': (f"Execution didn't stop within {EXEC_TIMEOUT}s, not even when interrupted (stuck inside "
                  "a library call), so the sandbox was restarted: files in /workspace are kept, "
                  "variables from earlier calls are gone."),
        'images': [], 'files': [],
    }
