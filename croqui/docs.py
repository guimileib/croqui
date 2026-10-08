"""Read-only access to the PRD and UML documents that travel with the map.

croqui never writes these files; ``scaffold.py`` seeds them once at scan time.
"""

from __future__ import annotations

import base64
import html
import mimetypes
import re
from pathlib import Path

# Where a tab looks for its documents, in order. ``.croqui/prd`` is the home a fresh
# project gets — next to images/ and layout.json, which is where the rest of croqui's
# output lives. A plain ``prd/`` at the project root, or a ``docs/prd`` the repo
# already had, is read just the same and is never migrated.
SECTIONS = {
    # A PRD may also be a finished HTML page (an export, a prototype write-up):
    # the viewer shows it in a sandboxed frame instead of through markdown.
    "prd": (("prd", "docs/prd", "PRD"), (".md", ".markdown", ".txt", ".html", ".htm")),
    "uml": (("uml", "docs/uml", "UML"), (".mmd", ".mermaid", ".md", ".markdown", ".txt")),
}
SEARCH_ROOTS = (".croqui", "")
MAX_DOC_BYTES = 512 * 1024
MAX_DOCS_PER_SECTION = 60
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache"}

# `# Título` on the first heading line, used as the tab's document name.
H1_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
# `<title>` or the first `<h1>` of an HTML document.
HTML_TITLE_RE = re.compile(r"<title[^>]*>\s*([^<]+?)\s*</title>|<h1[^>]*>\s*([^<]+?)\s*</h1>", re.IGNORECASE)
# `title: X` in mermaid front-matter, or the diagram's own `title` line.
MERMAID_TITLE_RE = re.compile(r"^\s*(?:---\s*\n(?:[\s\S]*?\n)?)?\s*title\s*:\s*(.+)$", re.MULTILINE)


def _pretty(stem: str) -> str:
    """``02-onboarding_flow`` -> ``Onboarding flow``. A filename is a title too."""
    text = re.sub(r"^\d+[-_. ]+", "", stem)
    text = text.replace("_", " ").replace("-", " ").strip()
    return (text[:1].upper() + text[1:]) if text else stem


def _title_of(text: str, path: Path, section: str) -> str:
    if section == "uml":
        found = MERMAID_TITLE_RE.search(text[:400])
        if found:
            return found.group(1).strip().strip("\"'")
    if path.suffix.lower() in (".html", ".htm"):
        found = HTML_TITLE_RE.search(text[:8000])
        if found:
            return html.unescape(found.group(1) or found.group(2)).strip()
        return _pretty(path.stem)
    found = H1_RE.search(text[:2000])
    if found:
        return found.group(1).strip()
    return _pretty(path.stem)


def section_dirs(root: Path, section: str) -> list[Path]:
    """Every folder this project uses for that section, most specific first."""
    names, _ = SECTIONS[section]
    out: list[Path] = []
    for base in SEARCH_ROOTS:
        for name in names:
            path = (root / base / name) if base else (root / name)
            if path.is_dir() and path not in out:
                out.append(path)
    return out


def collect_section(root: Path, section: str) -> list[dict]:
    """Every readable document for one tab, ordered by path."""
    _, suffixes = SECTIONS[section]
    found: list[dict] = []
    seen: set[str] = set()
    for directory in section_dirs(root, section):
        for path in sorted(directory.rglob("*")):
            if len(found) >= MAX_DOCS_PER_SECTION:
                break
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(directory).parts):
                continue
            try:
                if path.stat().st_size > MAX_DOC_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue
            rel = str(path.relative_to(root))
            if rel in seen:
                continue
            seen.add(rel)
            found.append({
                "name": rel,
                "title": _title_of(text, path, section),
                "format": "html" if path.suffix.lower() in (".html", ".htm") else "markdown",
                "text": text,
            })
    return found


def collect_docs(root: Path) -> dict:
    """The whole document set, ready to hand to the viewer."""
    return {section: collect_section(root, section) for section in SECTIONS}


def summarize(docs: dict) -> str:
    """One line for the scan output, or empty when there is nothing to say."""
    parts = [
        f"{len(docs[section])} {section.upper()}"
        for section in SECTIONS
        if docs.get(section)
    ]
    return " · ".join(parts)


# What an HTML PRD may pull in next to itself — pictures, stylesheets, scripts,
# fonts. Served by `croqui serve` and inlined by `croqui build`; nothing else.
ASSET_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".mp4": "video/mp4", ".webm": "video/webm", ".json": "application/json",
}
MAX_ASSET_BYTES = 12 * 1024 * 1024


def prd_asset(root: Path, rel: str) -> Path | None:
    """The file at ``rel`` if it is a known asset type inside a PRD folder, else None."""
    if not rel or "\x00" in rel:
        return None
    try:
        path = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if path.suffix.lower() not in ASSET_TYPES or not path.is_file():
        return None
    for directory in section_dirs(root, "prd"):
        base = directory.resolve()
        if base == path or base in path.parents:
            parts = path.relative_to(base).parts
            if any(part.startswith(".") or part in SKIP_DIRS for part in parts):
                return None
            return path
    return None


# src="…" / href="…" / url(…) pointing at a file next to the page.
_LOCAL_REF = re.compile(r"""(\b(?:src|href)\s*=\s*)(["'])([^"'#?]+)(\2)|(url\(\s*)(["']?)([^"')#?]+)(\6\s*\))""", re.IGNORECASE)


def inline_html_assets(root: Path, doc_rel: str, text: str) -> str:
    """Inline an HTML PRD's local images and stylesheets as data URLs for ``croqui build``."""
    base = Path(doc_rel).parent

    def data_url(ref: str) -> str | None:
        if re.match(r"^[a-z][a-z0-9+.-]*:|^//|^/", ref, re.IGNORECASE):
            return None
        path = prd_asset(root, str(base / ref))
        if path is None or path.suffix.lower() in (".html", ".htm"):
            return None
        try:
            if path.stat().st_size > MAX_ASSET_BYTES:
                return None
            body = path.read_bytes()
        except OSError:
            return None
        mime = ASSET_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return "data:" + mime.split(";")[0] + ";base64," + base64.b64encode(body).decode("ascii")

    def swap(m: re.Match) -> str:
        if m.group(1) is not None:
            url = data_url(m.group(3).strip())
            return m.group(0) if url is None else f"{m.group(1)}{m.group(2)}{url}{m.group(4)}"
        url = data_url(m.group(7).strip())
        return m.group(0) if url is None else f"{m.group(5)}{m.group(6)}{url}{m.group(8)}"

    return _LOCAL_REF.sub(swap, text)
