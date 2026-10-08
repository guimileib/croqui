"""Per-stack scanners. Each adapter turns source code into graph nodes/edges."""

from __future__ import annotations

from pathlib import Path

from .fastapi_py import FastApiScanner

ADAPTERS = {"python/fastapi": FastApiScanner}


def detect_stack(root: Path) -> str | None:
    """Best-effort stack detection, cheapest signal first."""
    for candidate in ("requirements.txt", "pyproject.toml", "requirements-worker.txt"):
        path = root / candidate
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if "fastapi" in text:
                return "python/fastapi"
    if list(root.glob("**/main.py")) and list(root.rglob("*.py")):
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
            if "APIRouter" in head or "from fastapi" in head:
                return "python/fastapi"
    return None


__all__ = ["ADAPTERS", "FastApiScanner", "detect_stack"]
