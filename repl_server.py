#!/usr/bin/env python3
"""Persistent HTTP REPL running inside the Docker sandbox.

/workspace is the session's directory on the host, mounted in, so files the code
writes are already where the app serves them from — only their names are returned.
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import sys, io, json, ast, os, re, signal, linecache, traceback

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as _plt

WORKSPACE = os.environ.get('WORKSPACE', '/workspace')
IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
MAX_OUTPUT = 5000   # characters of printed output kept per cell
DEFAULT_TIMEOUT = 30   # seconds; the app sends its own with each request

_ns = {'plt': _plt}


class ExecutionTimeout(BaseException):
    """Raised inside the running cell when its time is up, like Ctrl+C in Jupyter: the
    cell stops where it is and the namespace survives. A BaseException, so the cell's
    own `except Exception:` can't swallow it."""


def _on_alarm(signum, frame):
    raise ExecutionTimeout()

# requests are handled one at a time in the main thread, where signals are delivered
signal.signal(signal.SIGALRM, _on_alarm)


class _CappedOutput(io.StringIO):
    """Keeps the first MAX_OUTPUT characters: a cell printing in an endless loop would
    otherwise fill the container's memory, and the model's request after it."""
    def __init__(self):
        super().__init__()
        self.dropped = 0

    def write(self, s):
        room = max(MAX_OUTPUT - self.tell(), 0)
        super().write(s[:room])
        self.dropped += len(s) - min(len(s), room)
        return len(s)

    def text(self):
        out = self.getvalue()
        return out + f"\n…[{self.dropped} more characters of output cut]\n" if self.dropped else out


def _next_chart_path() -> str:
    """chart_1.png, chart_2.png… numbered across the whole session, so a name never repeats."""
    taken = [int(m.group(1)) for f in os.listdir(WORKSPACE) if (m := re.fullmatch(r'chart_(\d+)\.png', f))]
    return os.path.join(WORKSPACE, f'chart_{max(taken, default=0) + 1}.png')


def _patched_show(*a, **kw):
    _plt.savefig(_next_chart_path(), bbox_inches='tight', dpi=150)
    _plt.close()

_plt.show = _patched_show


def _workspace_files() -> dict:
    """Map of path (relative to /workspace) -> mtime, for every file in the workspace."""
    files = {}
    for root, dirs, names in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        for name in names:
            if name.startswith('.'):
                continue
            path = os.path.join(root, name)
            files[os.path.relpath(path, WORKSPACE)] = os.stat(path).st_mtime_ns
    return files


def _run(code: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    # uploads were written before this cell, so they only count as output
    # if the code itself changes them
    before = _workspace_files()

    # tracebacks show the cell's lines, not just "line 28"
    linecache.cache['<cell>'] = (len(code), None, code.splitlines(True), '<cell>')

    buf = _CappedOutput()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = buf
    sys.stderr = buf
    error = None

    try:
        tree = ast.parse(code)
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            # Like Jupyter: show the last expression's value, but not None —
            # otherwise a cell ending in print(...) also printed "None".
            last = tree.body[-1]
            store = ast.Assign(targets=[ast.Name(id='_', ctx=ast.Store())], value=last.value)
            show = ast.If(
                test=ast.Compare(left=ast.Name(id='_', ctx=ast.Load()), ops=[ast.IsNot()],
                                 comparators=[ast.Constant(value=None)]),
                body=[ast.Expr(value=ast.Call(func=ast.Name(id='print', ctx=ast.Load()),
                                              args=[ast.Name(id='_', ctx=ast.Load())], keywords=[]))],
                orelse=[],
            )
            for node in (store, show):
                ast.copy_location(node, last)
            tree.body[-1:] = [store, show]
            ast.fix_missing_locations(tree)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            exec(compile(tree, '<cell>', 'exec'), _ns)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except ExecutionTimeout as e:
        # where it was stuck is what tells an endless loop from slow code
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == '<cell>']
        where = f", still running line {frames[-1].lineno}:\n    {frames[-1].line}\n" if frames else ". "
        error = (f"Stopped after {timeout:g}s{where}If that line is inside a loop, the loop most likely "
                 "never ends. Variables and files are kept.")
    except Exception:
        error = traceback.format_exc()
    finally:
        sys.stdout = old_out
        sys.stderr = old_err

    # files created or modified by this cell; images are shown inline, the rest
    # become download buttons
    changed = sorted(p for p, mtime in _workspace_files().items() if before.get(p) != mtime)
    images = [p for p in changed if p.rsplit('.', 1)[-1].lower() in IMAGE_EXTS]

    return {
        'stdout': buf.text(),
        'error': error,
        'images': images,
        'files': [p for p in changed if p not in images],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != '/exec':
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length).decode())
        result = _run(body.get('code', ''), float(body.get('timeout', DEFAULT_TIMEOUT)))
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
