"""Graph model and on-disk JSON format shared by the scanners, the enricher and the viewer."""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA_VERSION = 1

# layout.json grew a `view` block (reading state) and per-shape `anchor` in v2.
# Nothing reads this number to branch — v1 files load unchanged — it is there so
# a human opening the file knows which shape they are looking at.
LAYOUT_VERSION = 2

# Pasted images live as real files in ``.croqui/images/`` and the layout only names
# them. Keeping the bytes out of layout.json is what lets that file stay reviewable
# in a PR — a single pasted screenshot would otherwise be a megabyte of base64 on
# one line — and it means an image survives a re-scan for free, because nothing in
# the scan path ever rewrites it.
IMAGE_DIR = "images"
IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
# The first bytes of the file must agree with the declared Content-Type. A browser
# paste always sends the truth; this is here so a hand-made POST cannot drop an
# arbitrary payload into the project under an image's name.
IMAGE_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}
# Content-addressed, so pasting the same screenshot twice stores one file and the
# name in the diff never churns.
IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(?:png|jpg|gif|webp)$")


def safe_image_name(name: object) -> str | None:
    """``name`` if it is a safe file name under ``.croqui/images/``, else None (blocks path traversal)."""
    if not isinstance(name, str) or not IMAGE_NAME_RE.match(name):
        return None
    return name


def image_names(layout: dict) -> list[str]:
    """Every image the layout actually points at, in first-seen order."""
    out: list[str] = []
    for shape in (layout or {}).get("shapes") or []:
        if not isinstance(shape, dict) or shape.get("type") != "image":
            continue
        name = safe_image_name(shape.get("src"))
        if name and name not in out:
            out.append(name)
    return out


# Node kinds, ordered — the viewer lays them out as columns in this order.
KIND_ORDER = ("route", "service", "repo", "table", "external")

# Edge kinds.
CALLS = "calls"
READS = "read"
WRITES = "write"
SENDS = "send"


class Graph:
    def __init__(self, project: dict) -> None:
        self.project = project
        self.nodes: dict[str, dict] = {}
        self._edges: dict[tuple[str, str, str], dict] = {}

    def add_node(self, node_id: str, kind: str, label: str, group: str | None = None, **meta) -> dict:
        node = self.nodes.get(node_id)
        if node is None:
            node = {"id": node_id, "kind": kind, "label": label, "group": group, "meta": {}}
            self.nodes[node_id] = node
        for key, value in meta.items():
            if value is not None:
                node["meta"][key] = value
        return node

    def add_edge(
        self,
        src: str,
        dst: str,
        kind: str,
        *,
        ops: list[str] | None = None,
        evidence: str | None = None,
        confidence: str = "static",
    ) -> dict:
        key = (src, dst, kind)
        edge = self._edges.get(key)
        if edge is None:
            edge = {"from": src, "to": dst, "kind": kind, "confidence": confidence, "ops": [], "evidence": []}
            self._edges[key] = edge
        # A static finding always outranks an inferred one.
        if edge["confidence"] != "static" and confidence == "static":
            edge["confidence"] = "static"
        for op in ops or []:
            if op not in edge["ops"]:
                edge["ops"].append(op)
        if evidence and evidence not in edge["evidence"]:
            edge["evidence"].append(evidence)
        return edge

    @property
    def edges(self) -> list[dict]:
        return list(self._edges.values())

    def drop_dangling_edges(self) -> int:
        """Remove edges pointing at nodes that never materialised."""
        dangling = [k for k, e in self._edges.items() if e["from"] not in self.nodes or e["to"] not in self.nodes]
        for key in dangling:
            del self._edges[key]
        return len(dangling)

    def drop_isolated_nodes(self, keep_kinds: tuple[str, ...] = ("route",)) -> int:
        """Remove nodes with no edges, except routes (``/health`` is still API surface)."""
        linked: set[str] = set()
        for edge in self._edges.values():
            linked.add(edge["from"])
            linked.add(edge["to"])
        isolated = [nid for nid, n in self.nodes.items() if nid not in linked and n["kind"] not in keep_kinds]
        for nid in isolated:
            del self.nodes[nid]
        return len(isolated)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for node in self.nodes.values():
            out[node["kind"]] = out.get(node["kind"], 0) + 1
        return out

    def to_dict(self) -> dict:
        order = {kind: i for i, kind in enumerate(KIND_ORDER)}
        nodes = sorted(self.nodes.values(), key=lambda n: (order.get(n["kind"], 99), n["label"]))
        edges = sorted(self.edges, key=lambda e: (e["from"], e["to"], e["kind"]))
        return {
            "croqui": SCHEMA_VERSION,
            "project": self.project,
            "stats": self.counts(),
            "nodes": nodes,
            "edges": edges,
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_graph(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- layout


def clean_layout(payload: dict) -> dict:
    """Whitelist what may reach ``layout.json``, sorted and deduplicated for stable diffs."""
    positions = payload.get("positions")
    hidden = payload.get("hidden") or []
    clean = {
        "croqui_layout": LAYOUT_VERSION,
        "positions": {k: positions[k] for k in sorted(positions)} if isinstance(positions, dict) else {},
        "hidden": sorted({h for h in hidden if isinstance(h, str)}) if isinstance(hidden, list) else [],
        "shapes": payload.get("shapes") or [],
        "links": payload.get("links") or [],
    }
    # Reading state: which groups are open, where the camera sits, which filters
    # are on. Committed so a teammate opens the map the way you left it, and kept
    # out of the unsaved-edits comparison so panning never lights the save button.
    view = payload.get("view")
    if isinstance(view, dict):
        camera = view.get("camera")
        filters = view.get("filters")
        clean["view"] = {
            "expanded": view.get("expanded") or [],
            "focus": view.get("focus") or None,
            "camera": camera if isinstance(camera, dict) else None,
            "filters": filters if isinstance(filters, dict) else {},
        }
    # Layouts written before the drawing tools carried `notes`; the viewer reads
    # those back as text shapes, so the key is only kept while something is still
    # sending one.
    if payload.get("notes"):
        clean["notes"] = payload["notes"]
    return clean
