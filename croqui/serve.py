"""Local viewer server (loopback only) and single-file bundler."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from urllib.parse import unquote

from .docs import ASSET_TYPES, collect_docs, inline_html_assets, prd_asset
from .model import IMAGE_DIR, IMAGE_MAGIC, IMAGE_TYPES, clean_layout, image_names, safe_image_name

VIEWER = Path(__file__).parent / "viewer"
OUT_DIR = ".croqui"
GRAPH_FILE = "graph.json"
LAYOUT_FILE = "layout.json"
MAX_LAYOUT_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _make_handler(root: Path):
    out = root / OUT_DIR

    class Handler(BaseHTTPRequestHandler):
        server_version = "croqui"

        def log_message(self, fmt, *args):  # noqa: A003 - silence per-request noise
            pass

        # ------------------------------------------------------------- helpers

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(json.dumps(payload).encode("utf-8"), CONTENT_TYPES[".json"], status)

        def _static(self, name: str) -> None:
            path = (VIEWER / name).resolve()
            if VIEWER.resolve() not in path.parents or not path.is_file():
                self._send(b"not found", "text/plain; charset=utf-8", 404)
                return
            self._send(path.read_bytes(), CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

        def _image(self, name: str) -> None:
            safe = safe_image_name(name)
            if safe is None:
                self._send(b"not found", "text/plain; charset=utf-8", 404)
                return
            path = out / IMAGE_DIR / safe
            if not path.is_file():
                self._send(b"not found", "text/plain; charset=utf-8", 404)
                return
            # Content-addressed, so the bytes behind a name never change and this
            # is the one response in croqui worth letting the browser keep.
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)

        def _store_image(self) -> None:
            """Take a pasted image and write it into ``.croqui/images/``."""
            declared = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            suffix = IMAGE_TYPES.get(declared)
            if suffix is None:
                self._json({"error": f"unsupported image type: {declared or 'none'}"}, 415)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._json({"error": "empty body"}, 400)
                return
            if length > MAX_IMAGE_BYTES:
                self._json({"error": f"image over {MAX_IMAGE_BYTES // (1024 * 1024)} MB"}, 413)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._json({"error": "short read"}, 400)
                return
            if not any(body.startswith(magic) for magic in IMAGE_MAGIC[suffix]):
                self._json({"error": f"body is not a {suffix.lstrip('.')} file"}, 400)
                return
            name = hashlib.sha256(body).hexdigest()[:16] + suffix
            target = out / IMAGE_DIR / name
            # Same bytes, same name: pasting one screenshot into five places costs
            # one file, and the name in the diff never churns.
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            self._json({"name": name})

        # ---------------------------------------------------------------- verbs

        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?")[0]
            if route in ("/", "/index.html"):
                self._static("index.html")
            elif route == "/api/graph":
                graph = out / GRAPH_FILE
                if not graph.is_file():
                    self._json({"error": "no graph"}, 404)
                    return
                self._send(graph.read_bytes(), CONTENT_TYPES[".json"])
            elif route == "/api/docs":
                # Read from disk per request, not cached: editing a PRD in your
                # editor and pressing reload is the entire workflow.
                self._json(collect_docs(root))
            elif route == "/api/layout":
                layout = out / LAYOUT_FILE
                self._send(layout.read_bytes() if layout.is_file() else b"{}", CONTENT_TYPES[".json"])
            elif route in ("/app.js", "/diagram.js", "/style.css"):
                self._static(route.lstrip("/"))
            elif route.startswith("/images/"):
                self._image(route[len("/images/"):])
            elif route.startswith("/prd/"):
                # An HTML PRD and whatever it links next to itself, by URL, so its
                # relative <img>, <link> and <script> resolve like on any web server.
                path = prd_asset(root, unquote(route[len("/prd/"):]))
                if path is None:
                    self._send(b"not found", "text/plain; charset=utf-8", 404)
                    return
                self._send(path.read_bytes(), ASSET_TYPES[path.suffix.lower()])
            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)

        def do_POST(self) -> None:  # noqa: N802
            route = self.path.split("?")[0]
            if route == "/api/image":
                self._store_image()
                return
            if route != "/api/layout":
                self._send(b"not found", "text/plain; charset=utf-8", 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_LAYOUT_BYTES:
                self._json({"error": "bad length"}, 400)
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._json({"error": "invalid json"}, 400)
                return
            if not isinstance(payload, dict):
                self._json({"error": "expected an object"}, 400)
                return
            clean = clean_layout(payload)
            out.mkdir(parents=True, exist_ok=True)
            (out / LAYOUT_FILE).write_text(
                json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            self._json({"ok": True})

    return Handler


def serve(root: Path, *, host: str = "127.0.0.1", port: int = 7777, open_browser: bool = True) -> int:
    handler = _make_handler(root)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        print(f"cannot bind {host}:{port} — {exc}", file=sys.stderr)
        return 1
    url = f"http://{host}:{port}/"
    print(f"croqui serving {root.name} at \033[1m{url}\033[0m  (ctrl-c to stop)", file=sys.stderr)
    print("  edits are saved to .croqui/layout.json", file=sys.stderr)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


def build_bundle(root: Path) -> str:
    """Inline everything into one portable HTML file (read-only view)."""
    out = root / OUT_DIR
    graph = json.loads((out / GRAPH_FILE).read_text(encoding="utf-8"))
    layout_path = out / LAYOUT_FILE
    layout = json.loads(layout_path.read_text(encoding="utf-8")) if layout_path.is_file() else {}

    html = (VIEWER / "index.html").read_text(encoding="utf-8")
    css = (VIEWER / "style.css").read_text(encoding="utf-8")
    js = (VIEWER / "app.js").read_text(encoding="utf-8")
    diagram = (VIEWER / "diagram.js").read_text(encoding="utf-8")

    # A bundle is one file you can mail to someone, so pasted images have to travel
    # inside it. Only what the layout actually points at, and only through
    # `safe_image_name` — a `src` is user-editable text and this reads from disk.
    images = {}
    for name in image_names(layout):
        path = out / IMAGE_DIR / name
        if not path.is_file():
            continue
        mime = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        images[name] = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    docs = collect_docs(root)
    # No server behind a bundle, so an HTML PRD brings its pictures and styles along.
    for doc in docs.get("prd", []):
        if doc.get("format") == "html":
            doc["text"] = inline_html_assets(root, doc["name"], doc["text"])
    payload = json.dumps(
        {"graph": graph, "layout": layout, "images": images, "docs": docs},
        ensure_ascii=False,
    )
    # </script> inside JSON would close the tag early.
    payload = payload.replace("</", "<\\/")

    html = html.replace('<link rel="stylesheet" href="style.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<script src="diagram.js"></script>', f"<script>\n{diagram}\n</script>")
    html = html.replace(
        '<script src="app.js"></script>',
        f"<script>window.__CROQUI__ = {payload};</script>\n<script>\n{js}\n</script>",
    )
    html = html.replace("<title>croqui</title>", f"<title>croqui — {graph['project']['name']}</title>")
    return html
