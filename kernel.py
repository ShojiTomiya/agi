"""Per-session Docker container management for the persistent REPL."""
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

_sessions: dict = {}
_lock = threading.Lock()


def _name(session_id: str) -> str:
    return f"agi-kernel-{session_id}"


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
        'docker', 'run', '-d',
        '--name', name,
        '-p', '127.0.0.1::8080',
        '--memory', '512m',
        '--pids-limit', '64',
        'agi-sandbox:latest',
        'python', '/repl_server.py',
    ], check=True, capture_output=True)
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


def execute(session_id: str, code: str, context_files: list = None) -> dict:
    entry = _get_or_create(session_id)
    name, port = entry['name'], entry['port']

    with entry['lock']:
        for att in (context_files or []):
            if not att.get('name'):
                continue
            src = att.get('original_path')
            if src and os.path.exists(src):
                subprocess.run(['docker', 'cp', src, f'{name}:/workspace/{att["name"]}'], capture_output=True)
            elif att.get('content'):
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=att['name']) as tf:
                    tf.write(att['content'])
                    tf_path = tf.name
                subprocess.run(['docker', 'cp', tf_path, f'{name}:/workspace/{att["name"]}'], capture_output=True)
                os.unlink(tf_path)

        payload = json.dumps({'code': code}).encode()
        req = urllib.request.Request(
            f'http://127.0.0.1:{port}/exec',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as e:
            return {'stdout': '', 'error': str(e), 'images': [], 'output_files': []}
