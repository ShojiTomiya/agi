#!/usr/bin/env python3
"""Persistent HTTP REPL running inside the Docker sandbox."""
from http.server import HTTPServer, BaseHTTPRequestHandler
import sys, io, json, ast, base64, glob, os, traceback

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as _plt

_ns = {'plt': _plt}
_chart_n = [0]


def _patched_show(*a, **kw):
    _chart_n[0] += 1
    n = '' if _chart_n[0] == 1 else str(_chart_n[0])
    _plt.savefig(f'/tmp/_chart{n}.png', bbox_inches='tight', dpi=150)
    _plt.close()

_plt.show = _patched_show


def _workspace_files():
    return set(f for f in glob.glob('/workspace/*') if os.path.isfile(f))


def _run(code: str) -> dict:
    _chart_n[0] = 0

    before = _workspace_files()

    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = buf
    sys.stderr = buf
    error = None

    try:
        tree = ast.parse(code)
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body[-1]
            pnode = ast.Expr(value=ast.Call(
                func=ast.Name(id='print', ctx=ast.Load()),
                args=[last.value], keywords=[]
            ))
            ast.copy_location(pnode, last)
            ast.fix_missing_locations(pnode)
            tree.body[-1] = pnode
        exec(compile(tree, '<cell>', 'exec'), _ns)
    except Exception:
        error = traceback.format_exc()
    finally:
        sys.stdout = old_out
        sys.stderr = old_err

    images = []
    for i in range(1, _chart_n[0] + 1):
        n = '' if i == 1 else str(i)
        path = f'/tmp/_chart{n}.png'
        if os.path.exists(path):
            with open(path, 'rb') as f:
                images.append({'name': f'chart{n}.png', 'data': base64.b64encode(f.read()).decode()})
            os.remove(path)

    output_files = []

    def _collect(fpath):
        fname = os.path.basename(fpath)
        ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
        with open(fpath, 'rb') as f:
            data = base64.b64encode(f.read()).decode()
        output_files.append({'name': fname, 'ext': ext, 'data': data})
        os.remove(fpath)

    # Files explicitly placed in /workspace/output/
    os.makedirs('/workspace/output', exist_ok=True)
    for fpath in sorted(glob.glob('/workspace/output/*')):
        if os.path.isfile(fpath):
            _collect(fpath)

    # New files created in /workspace root during this execution
    for fpath in sorted(_workspace_files() - before):
        _collect(fpath)

    return {
        'stdout': buf.getvalue(),
        'error': error,
        'images': images,
        'output_files': output_files,
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
        result = _run(body.get('code', ''))
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
