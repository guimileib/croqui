"""The map as an Excalidraw diagram that argues, for ``croqui draw --format excalidraw``.

The Mermaid that ``draw`` writes is the cheap view: boxes and arrows in the UML tab.
This one follows the design method of coleam00/excalidraw-diagram-skill — a diagram
should argue, not display — with what the map already knows standing in for the
research that method asks a model to do:

* Three zoom levels. A title over a one-line summary of the flow; one section per
  layer, named in free text instead of boxed; and inside each section the evidence:
  a table's real columns in a code block, a service's real methods, the handler and
  the file behind an endpoint.
* Shape and colour are meaning. An endpoint is where a request starts (an ellipse,
  in its verb's colour); a service or a repository is work (a rectangle); a table
  shows what it holds; an external system is dashed. Every shape is a light fill
  under a darker stroke of the same colour, from Excalidraw's own swatches — the
  ones croqui maps back onto its palette when the drawing is pasted on the map.
* Containers only for what arrows join; titles, summaries and details are text.
* Clean lines (roughness 0), one monospaced font (fontFamily 3), full opacity.

Deterministic, like the Mermaid: the same graph and arguments give the same bytes.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field

from .draw import DRAW_MARKER, LAYERS, VERBS, _arrow, _layer

# kind or verb -> (stroke, fill).
TONES = {
    "get": ("#2f9e44", "#b2f2bb"),
    "post": ("#1971c2", "#a5d8ff"),
    "put": ("#e8590c", "#ffd8a8"),
    "patch": ("#e8590c", "#ffd8a8"),
    "delete": ("#e03131", "#ffc9c9"),
    "route": ("#1971c2", "#a5d8ff"),
    "entry": ("#e8590c", "#ffd8a8"),
    "module": ("#6741d9", "#d0bfff"),
    "service": ("#6741d9", "#d0bfff"),
    "repo": ("#0c8599", "#96f2d7"),
    "table": ("#f08c00", "#ffec99"),
    "external": ("#495057", "#e9ecef"),
}
OTHER_TONE = ("#1e1e1e", "#e9ecef")
# A section's title is in the colour of what it holds.
SECTION_INK = {"api": "#1971c2", "dominio": "#6741d9", "repos": "#0c8599", "dados": "#f08c00",
               "externos": "#495057"}
INK = "#1e1e1e"
BODY = "#495057"
CODE_BG, CODE_INK = "#1e293b", "#22c55e"

# Cascadia is monospaced, so a width is characters times one advance.
FONT = 3
ADVANCE = 0.6
LINE = 1.25
TITLE, SUMMARY, SECTION, LABEL, DETAIL = 28, 16, 20, 16, 13

LABEL_CHARS = 24       # a label wraps past this; shapes stay a readable size
DETAIL_CHARS = 36
MAX_COLUMNS = 8        # columns shown in a table's code block before "+N"
MAX_ENDPOINTS = 6      # past this, endpoints are drawn one block per resource
MAX_LISTED = 10        # endpoints listed in such a block before "+N"
COL_GAP = 170          # between layers: room for an arrow and its label
ROW_GAP = 40           # between blocks in one layer
TOP = 150              # where the sections start, under the title and summary


def _num(value: float) -> int:
    return int(round(value))


def _seed(key: str) -> int:
    return (zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF) or 1


def _width(lines: list[str], size: float) -> float:
    return max((len(line) for line in lines), default=0) * size * ADVANCE


def _height(lines: list[str], size: float) -> float:
    return len(lines) * size * LINE


def _wrap(text: str, limit: int, most: int = 3) -> list[str]:
    """Lines of about ``limit`` characters, broken at spaces or after a slash."""
    lines: list[str] = []
    line, gap = "", False
    for token in re.findall(r"[^\s/]+/?|/|\s+", str(text)):
        if token.isspace():
            gap = bool(line)
            continue
        joined = line + (" " if gap else "") + token
        if line and len(joined) > limit:
            lines.append(line)
            line = token
        else:
            line = joined
        gap = False
    if line:
        lines.append(line)
    if len(lines) > most:
        lines = lines[:most]
        lines[-1] = lines[-1][: max(1, limit - 1)] + "…"
    return lines or [""]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "x"


def _element(eid: str, kind: str, x: float, y: float, w: float, h: float, stroke: str,
             fill: str = "transparent", **extra: object) -> dict:
    out = {
        "id": eid, "type": kind, "x": _num(x), "y": _num(y), "width": _num(w), "height": _num(h),
        "angle": 0, "strokeColor": stroke, "backgroundColor": fill, "fillStyle": "solid",
        "strokeWidth": 2, "strokeStyle": "solid", "roughness": 0, "opacity": 100,
        "groupIds": [], "frameId": None, "roundness": None,
        "seed": _seed(eid), "version": 1, "versionNonce": _seed(eid + "#"),
        "isDeleted": False, "boundElements": [], "updated": 1, "link": None, "locked": False,
    }
    out.update(extra)
    return out


def _text(eid: str, text: str, x: float, y: float, size: float, color: str, *,
          align: str = "left", valign: str = "top", container: str | None = None) -> dict:
    lines = text.split("\n")
    return _element(
        eid, "text", x, y, _width(lines, size), _height(lines, size), color,
        strokeWidth=1, boundElements=None, text=text, originalText=text, fontSize=size,
        fontFamily=FONT, textAlign=align, verticalAlign=valign, containerId=container,
        lineHeight=LINE, autoResize=True,
    )


# ---------------------------------------------------------------------- blocks


@dataclass
class _Block:
    """One node: its shape, plus the evidence that goes with it — under the shape
    when arrows leave by its sides (left to right), beside it when they leave by its
    top and bottom (top to bottom), so an arrow never has to cross its own evidence."""

    node: dict
    eid: str
    shape: str
    label: list[str]
    tone: tuple[str, str]
    dashed: bool
    w: float
    h: float
    code: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    beside: bool = False
    x: float = 0.0           # top-left of the whole block
    y: float = 0.0

    @property
    def code_h(self) -> float:
        # Sized for croqui's line height (1.35), a little taller than Excalidraw's:
        # the same block holds its columns in either.
        return len(self.code) * DETAIL * 1.35 + 22 if self.code else 0.0

    @property
    def code_w(self) -> float:
        return _width(self.code, DETAIL) + 28 if self.code else 0.0

    @property
    def detail_w(self) -> float:
        return _width(self.detail, DETAIL)

    @property
    def detail_h(self) -> float:
        return _height(self.detail, DETAIL)

    @property
    def shape_w(self) -> float:
        # Under the shape, a code block is as wide as the shape and the shape as
        # wide as the code: one column, edges aligned.
        return self.w if self.beside else max(self.w, self.code_w)

    @property
    def side_h(self) -> float:
        return self.code_h + (8 if self.code and self.detail else 0) + self.detail_h

    @property
    def block_w(self) -> float:
        if self.beside:
            side = max(self.code_w, self.detail_w)
            return self.shape_w + (14 + side if side else 0)
        return max(self.shape_w, self.detail_w)

    @property
    def block_h(self) -> float:
        if self.beside:
            return max(self.h, self.side_h)
        h = self.h
        if self.code:
            h += 8 + self.code_h
        if self.detail:
            h += 10 + self.detail_h
        return h

    def box(self) -> tuple[float, float, float, float]:
        """The shape itself: centred over its evidence, or at the left of it."""
        if self.beside:
            return self.x, self.y, self.shape_w, self.h
        return self.x + (self.block_w - self.shape_w) / 2, self.y, self.shape_w, self.h

    def code_box(self) -> tuple[float, float, float, float]:
        x, y, w, h = self.box()
        if self.beside:
            return x + w + 14, y, self.code_w, self.code_h
        return x, y + h + 8, w, self.code_h

    def detail_at(self) -> tuple[float, float, str]:
        """Where the details start, and how they are aligned."""
        x, y, w, h = self.box()
        if self.beside:
            top = y + (self.code_h + 8 if self.code else 0)
            return x + w + 14, top, "left"
        top = y + h + (8 + self.code_h if self.code else 0) + 10
        return x + (w - self.detail_w) / 2, top, "center"

    def extent(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.block_w, self.block_h


def _columns(meta: dict) -> list[str]:
    """A table's columns, as a code block shows them: name, type, and what is special."""
    cols = [c for c in (meta.get("columns") or []) if isinstance(c, dict) and c.get("name")]
    if not cols:
        return []
    pad = min(max(len(str(c["name"])) for c in cols), 18)
    lines = []
    for col in cols[:MAX_COLUMNS]:
        line = f"{str(col['name'])[:18]:<{pad}}  {str(col.get('type') or '')[:12]}".rstrip()
        if col.get("pk"):
            line += "  PK"
        if col.get("references"):
            line += f"  → {str(col['references'])[:14]}"
        lines.append(line)
    if len(cols) > MAX_COLUMNS:
        lines.append(f"… +{len(cols) - MAX_COLUMNS} colunas")
    return lines


def _source(meta: dict) -> str:
    """`app/blueprints/users.py:25` -> `users.py:25`: the file is the evidence, not the tree."""
    where = str(meta.get("source") or "")
    return where.rsplit("/", 1)[-1] if where else ""


def _block(node: dict, eid: str) -> _Block:
    kind = node["kind"]
    meta = node.get("meta") or {}
    tone = TONES.get(kind, OTHER_TONE)
    shape, dashed = "rectangle", False
    code: list[str] = []
    detail: list[str] = []
    if kind == "route":
        verb = str(meta.get("method") or "").lower()
        tone = TONES.get(verb if verb in VERBS else "route", tone)
        label = [verb.upper()] + _wrap(meta.get("path") or node["label"], LABEL_CHARS, 2) if verb \
            else _wrap(node["label"], LABEL_CHARS)
        shape = "ellipse"
        where = " · ".join(x for x in (str(meta.get("handler") or ""), _source(meta)) if x)
        detail = _wrap(where, DETAIL_CHARS, 2) if where else []
    elif kind == "entry":
        label, shape = _wrap(node["label"], LABEL_CHARS), "ellipse"
        detail = _wrap(_source(meta), DETAIL_CHARS, 1) if _source(meta) else []
    elif kind == "external":
        label, shape, dashed = _wrap(node["label"], LABEL_CHARS), "ellipse", True
        how = meta.get("via") or meta.get("description")
        detail = _wrap(f"via {how}" if meta.get("via") else str(how), DETAIL_CHARS, 1) if how else []
    elif kind == "endpoints":
        # A resource's endpoints, listed as the requests they are.
        members = meta.get("members") or []
        label = _wrap(node["label"], LABEL_CHARS)
        tone = TONES["route"]
        width = max((len(str((m.get("meta") or {}).get("method") or "")) for m in members), default=0)
        for m in members[:MAX_LISTED]:
            mm = m.get("meta") or {}
            verb = str(mm.get("method") or "").upper()
            code.append(f"{verb:<{width}}  {str(mm.get('path') or m['label'])[:34]}".strip())
        if len(members) > MAX_LISTED:
            code.append(f"… +{len(members) - MAX_LISTED} endpoints")
        detail = [f"{len(members)} endpoints"]
    elif kind == "table":
        label = _wrap(node["label"], LABEL_CHARS)
        code = _columns(meta)
    else:
        label = _wrap(node["label"], LABEL_CHARS)
        if meta.get("description"):
            detail += _wrap(str(meta["description"]), DETAIL_CHARS, 1)
        methods = [str(m) for m in (meta.get("methods") or [])][:6]
        if methods:
            detail += _wrap(" · ".join(m + "()" for m in methods), DETAIL_CHARS, 2)
    tw, th = _width(label, LABEL), _height(label, LABEL)
    if shape == "ellipse":
        # The writing has to fit the ellipse's inner rectangle, which is 1/√2 of it.
        w, h = max(150, tw * 1.42 + 28), max(64, th * 1.42 + 22)
    else:
        w, h = max(150, tw + 40), max(56, th + 28)
    return _Block(node=node, eid=eid, shape=shape, label=label, tone=tone, dashed=dashed,
                  w=w, h=h, code=code, detail=detail)


def _layer_of(kind: str) -> int:
    return 0 if kind == "endpoints" else _layer(kind)


def _group_endpoints(chosen: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Past a handful, endpoints are one block per resource.

    An overview is the summary level: twelve endpoints stacked in a column are a
    list, and the arrows out of them cross the whole picture. One block per group
    still names every request (as evidence, inside the block) and keeps one arrow
    per thing the group touches. A diagram of one flow keeps its endpoints whole.
    """
    routes = [n for n in chosen if n["kind"] in ("route", "entry")]
    if len(routes) <= MAX_ENDPOINTS:
        return chosen, edges
    groups: dict[str, list[dict]] = {}
    for node in routes:
        groups.setdefault(str(node.get("group") or "endpoints"), []).append(node)
    owner: dict[str, str] = {}
    out = [n for n in chosen if n["kind"] not in ("route", "entry")]
    for name in sorted(groups):
        members = sorted(groups[name], key=lambda n: (str((n.get("meta") or {}).get("path") or n["label"]),
                                                      str((n.get("meta") or {}).get("method") or "")))
        gid = "endpoints:" + name
        out.append({"id": gid, "kind": "endpoints", "label": name, "meta": {"members": members}})
        for member in members:
            owner[member["id"]] = gid
    return out, [{**e, "from": owner.get(e.get("from"), e.get("from")),
                  "to": owner.get(e.get("to"), e.get("to"))} for e in edges]


# ---------------------------------------------------------------------- layout


def _order(layers: list[list[_Block]], links: list[tuple[str, str]]) -> None:
    """Sort each layer by where its neighbours sit, so arrows cross less: two sweeps
    of the barycentre heuristic, down the layers and back up."""
    near: dict[str, list[str]] = {}
    for a, b in links:
        near.setdefault(a, []).append(b)
        near.setdefault(b, []).append(a)
    for sweep in (range(1, len(layers)), range(len(layers) - 2, -1, -1)):
        for i in sweep:
            pos = {}
            for j in (i - 1, i + 1):
                if 0 <= j < len(layers):
                    pos.update({b.eid: k for k, b in enumerate(layers[j])})
            def key(b: _Block, here: int) -> tuple[float, int]:
                seen = [pos[n] for n in near.get(b.eid, []) if n in pos]
                return (sum(seen) / len(seen) if seen else float(here), here)
            layers[i] = [b for _, b in sorted(((key(b, k), b) for k, b in enumerate(layers[i])),
                                              key=lambda t: t[0])]


def _place(layers: list[list[_Block]], direction: str, gaps: list[float]) -> None:
    """Left to right, one column per layer; or top to bottom, one row per layer.
    Each line of blocks is centred on the longest one; ``gaps[k]`` is the room
    after line ``k``, wide enough for the labels of the arrows that cross it."""
    if direction == "TB":
        rows = [(sum(b.block_w for b in row) + ROW_GAP * 1.25 * (len(row) - 1), max(b.block_h for b in row))
                for row in layers]
        widest = max(w for w, _ in rows)
        y = TOP
        for row, (w, h) in zip(layers, rows):
            x = 190 + (widest - w) / 2
            for b in row:
                b.x, b.y = x, y
                x += b.block_w + ROW_GAP * 1.25
            y += h + COL_GAP * 0.75
        return
    cols = [(max(b.block_w for b in col), sum(b.block_h for b in col) + ROW_GAP * (len(col) - 1))
            for col in layers]
    tallest = max(h for _, h in cols)
    x = 0.0
    for k, (col, (w, h)) in enumerate(zip(layers, cols)):
        y = TOP + 40 + (tallest - h) / 2
        for b in col:
            b.x, b.y = x + (w - b.block_w) / 2, y
            y += b.block_h + ROW_GAP
        x += w + (gaps[k] if k < len(gaps) else COL_GAP)


# ---------------------------------------------------------------------- arrows


def _blocked(points: list[tuple[float, float]], boxes: list[tuple[float, float, float, float]]) -> int:
    """How many boxes a path through ``points`` runs into."""
    hit = set()
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        for k in range(1, 30):
            t = k / 30
            px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            for i, (bx, by, bw, bh) in enumerate(boxes):
                if bx - 10 < px < bx + bw + 10 and by - 10 < py < by + bh + 10:
                    hit.add(i)
    return len(hit)


def _around(p: tuple[float, float], q: tuple[float, float], la: int, lb: int, direction: str,
            lanes: list[tuple[float, float, float, float]], used: dict) -> list[tuple[float, float]]:
    """An arrow that skips a layer: out into the gap beside its own layer, along the
    outside of the layers it skips — above or below them, whichever is nearer —
    and down the gap into its target. Straight, it would cut through every block
    in the layers between. Arrows that take the same way round run side by side
    (``used`` counts them), never on top of one another: Excalidraw writes an
    arrow's label at its middle, and two arrows sharing a run share that middle."""
    step = 1 if lb > la else -1
    between = lanes[min(la, lb) + 1:max(la, lb)]

    def facing(k: int, toward: int) -> float:
        # The edge of line k that faces the direction ``toward``.
        x, y, w, h = lanes[k]
        if direction == "TB":
            return y + h if toward > 0 else y
        return x + w if toward > 0 else x

    # The runs in the gaps hug the layers they leave and enter: the middle of a gap
    # is where the labels of the straight arrows across it are written.
    first, last = facing(la, step) + step * 24, facing(lb, -step) - step * 24
    if direction == "TB":
        low_edge = min(x for x, _, _, _ in between) - 46
        high_edge = max(x + w for x, _, w, _ in between) + 46
        middle = (p[0] + q[0]) / 2
    else:
        # Above the section titles too, not through them.
        low_edge = min(min(y for _, y, _, _ in between) - 46, TOP - 24)
        high_edge = max(y + h for _, y, _, h in between) + 46
        middle = (p[1] + q[1]) / 2
    outward = -1 if abs(middle - low_edge) <= abs(middle - high_edge) else 1
    key = (min(la, lb), max(la, lb), outward)
    k = used.get(key, 0)
    used[key] = k + 1
    side = (low_edge if outward < 0 else high_edge) + outward * 26 * k
    first += step * 14 * k
    last -= step * 14 * k
    # Square corners: straight out of the shape into the gap, along the outside,
    # and straight into the target — the evidence under either block is never
    # crossed on the way.
    if direction == "TB":
        return [p, (p[0], first), (side, first), (side, last), (q[0], last), q]
    return [p, (first, p[1]), (first, side), (last, side), (last, q[1]), q]


def _point_at(points: list[tuple[float, float]], frac: float) -> tuple[float, float]:
    """The point ``frac`` of the way along a path, by length."""
    legs = [((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 for (x1, y1), (x2, y2) in zip(points, points[1:])]
    goal = sum(legs) * frac
    for ((x1, y1), (x2, y2)), leg in zip(zip(points, points[1:]), legs):
        if goal <= leg and leg:
            t = goal / leg
            return x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        goal -= leg
    return points[-1]


def _route(a: _Block, b: _Block, la: int, lb: int, direction: str,
           others: list[tuple[float, float, float, float]],
           lanes: list[tuple[float, float, float, float]], used: dict) -> list[tuple[float, float]]:
    """From the side of ``a`` that faces ``b`` to the side of ``b`` that faces ``a``:
    straight when nothing is in the way, round the outside of a skipped layer, and
    otherwise with one bend that misses the blocks."""
    ax, ay, aw, ah = a.box()
    bx, by, bw, bh = b.box()
    if direction == "TB":
        if la < lb:
            p, q = (ax + aw / 2, ay + ah + 4), (bx + bw / 2, by - 4)
        elif la > lb:
            p, q = (ax + aw / 2, ay - 4), (bx + bw / 2, by + bh + 4)
        else:
            p, q = (ax + aw / 2, ay + ah + 4), (bx + bw / 2, by + bh + 4)
    else:
        if la < lb:
            p, q = (ax + aw + 4, ay + ah / 2), (bx - 4, by + bh / 2)
        elif la > lb:
            p, q = (ax - 4, ay + ah / 2), (bx + bw + 4, by + bh / 2)
        else:
            p, q = (ax + aw + 4, ay + ah / 2), (bx + bw + 4, by + bh / 2)
    mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
    if la == lb:
        # Two blocks of one layer: out to the side and back, round the ones between.
        far = max(ax + aw, bx + bw) + 70 if direction != "TB" else None
        low = max(ay + ah, by + bh) + 60 if direction == "TB" else None
        return [p, (far, mid[1]) if far is not None else (mid[0], low), q]
    straight = [p, q]
    if not _blocked(straight, others):
        return straight
    if abs(la - lb) > 1:
        return _around(p, q, la, lb, direction, lanes, used)
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    nx, ny = -dy / length, dx / length
    best, fewest = straight, _blocked(straight, others)
    for off in (60, -60, 120, -120, 190, -190, 280, -280):
        bent = [p, (mid[0] + nx * off, mid[1] + ny * off), q]
        n = _blocked(bent, others)
        if n < fewest:
            best, fewest = bent, n
        if not n:
            break
    return best


# ---------------------------------------------------------------------- compose


def compose(graph: dict, picked: list[str], omitted: int, title: str, direction: str) -> str:
    """The .excalidraw file for ``picked`` nodes of ``graph``."""
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    picked_nodes = [nodes[i] for i in picked if i in nodes]
    counts: dict[int, int] = {}
    for node in picked_nodes:
        counts[_layer(node["kind"])] = counts.get(_layer(node["kind"]), 0) + 1
    picked_nodes, graph_edges = _group_endpoints(picked_nodes, list(graph.get("edges") or []))
    chosen = sorted(picked_nodes, key=lambda n: (_layer_of(n["kind"]), str(n["label"]).lower(), n["id"]))

    eids: dict[str, str] = {}
    taken: set[str] = set()
    for node in chosen:
        base = "node-" + _slug(node["label"])
        eid, k = base, 2
        while eid in taken:
            eid, k = f"{base}-{k}", k + 1
        taken.add(eid)
        eids[node["id"]] = eid

    blocks = {node["id"]: _block(node, eids[node["id"]]) for node in chosen}
    for b in blocks.values():
        b.beside = direction == "TB"
    layer_keys = [frame for frame, _, _ in LAYERS] + ["outros"]
    layer_names = [label for _, label, _ in LAYERS] + ["outros"]
    grouped: dict[int, list[_Block]] = {}
    for node in chosen:
        grouped.setdefault(_layer_of(node["kind"]), []).append(blocks[node["id"]])
    used = sorted(grouped)
    layers = [grouped[i] for i in used]
    line_of = {b.eid: k for k, i in enumerate(used) for b in grouped[i]}

    edges: dict[tuple[str, str], dict] = {}
    for edge in graph_edges:
        a, b = edge.get("from"), edge.get("to")
        if a in blocks and b in blocks and a != b:
            # Writing outranks reading outranks calling, when two edges join a pair.
            rank = {"write": 3, "read": 2, "send": 1}.get(str(edge.get("kind")), 0)
            seen = edges.get((a, b))
            if seen is None or rank > {"write": 3, "read": 2, "send": 1}.get(str(seen.get("kind")), 0):
                edges[(a, b)] = edge
    _order(layers, [(blocks[a].eid, blocks[b].eid) for a, b in edges])
    # Between two neighbouring layers, room for the widest label written there.
    gaps = [float(COL_GAP)] * max(0, len(layers) - 1)
    for (a, b), edge in edges.items():
        la, lb = line_of[blocks[a].eid], line_of[blocks[b].eid]
        word = _arrow(edge)[1]
        if word and abs(la - lb) == 1:
            k = min(la, lb)
            gaps[k] = max(gaps[k], _width([word], DETAIL) + 90)
    _place(layers, direction, gaps if direction != "TB" else [])

    out: list[dict] = []
    by_id: dict[str, dict] = {}

    def add(element: dict) -> dict:
        out.append(element)
        by_id[element["id"]] = element
        return element

    # Level 1: what this is, and the whole flow in one line.
    add(_text("title", title, 0, 0, TITLE, INK))
    summary = "  →  ".join(f"{layer_names[i]} ({counts.get(i, len(grouped[i]))})" for i in used)
    add(_text("summary", summary, 0, TITLE * LINE + 12, SUMMARY, BODY))

    # Level 2: a free-standing title per section.
    for i, layer in zip(used, layers):
        ink = SECTION_INK.get(layer_keys[i], BODY)
        if direction == "TB":
            top = min(b.y for b in layer)
            add(_text(f"section-{layer_keys[i]}", layer_names[i], 0, top + 4, SECTION, ink))
        else:
            left = min(b.x for b in layer)
            add(_text(f"section-{layer_keys[i]}", layer_names[i], left, TOP, SECTION, ink))

    # Level 3: the blocks, each with its evidence.
    for layer in layers:
        for b in layer:
            x, y, w, h = b.box()
            stroke, fill = b.tone
            shape = add(_element(
                b.eid, b.shape, x, y, w, h, stroke, fill,
                strokeStyle="dashed" if b.dashed else "solid",
                roundness={"type": 3} if b.shape == "rectangle" else {"type": 2},
            ))
            label = "\n".join(b.label)
            lines = b.label
            tid = b.eid + "-label"
            add(_text(tid, label, x + (w - _width(lines, LABEL)) / 2, y + (h - _height(lines, LABEL)) / 2,
                      LABEL, INK, align="center", valign="middle", container=b.eid))
            shape["boundElements"].append({"type": "text", "id": tid})
            if b.code:
                cx, cy, cw, ch = b.code_box()
                add(_element(b.eid + "-code", "rectangle", cx, cy, cw, ch, CODE_BG, CODE_BG,
                             strokeWidth=1, roundness={"type": 3}))
                add(_text(b.eid + "-columns", "\n".join(b.code), cx + 14, cy + 11, DETAIL, CODE_INK))
            if b.detail:
                dx, dy, align = b.detail_at()
                add(_text(b.eid + "-detail", "\n".join(b.detail), dx, dy, DETAIL, BODY, align=align))

    # The relations, with the verb of each written on it.
    extents = {b.eid: b.extent() for b in blocks.values()}
    lanes = []
    for layer in layers:
        x1 = min(b.x for b in layer)
        y1 = min(b.y for b in layer)
        lanes.append((x1, y1, max(b.x + b.block_w for b in layer) - x1, max(b.y + b.block_h for b in layer) - y1))
    written: list[tuple[float, float, float, float]] = []
    runs: dict = {}
    for (a_id, b_id), edge in sorted(edges.items(), key=lambda kv: (blocks[kv[0][0]].eid, blocks[kv[0][1]].eid)):
        a, b = blocks[a_id], blocks[b_id]
        others = [box for eid, box in extents.items() if eid not in (a.eid, b.eid)]
        points = _route(a, b, line_of[a.eid], line_of[b.eid], direction, others, lanes, runs)
        style, word = _arrow(edge)
        sx, sy = points[0]
        rel = [[_num(px - sx), _num(py - sy)] for px, py in points]
        xs, ys = [p[0] for p in rel], [p[1] for p in rel]
        aid = f"arrow-{a.eid[5:]}-to-{b.eid[5:]}"
        stroke = a.tone[0]
        arrow = add(_element(
            aid, "arrow", sx, sy, max(xs) - min(xs), max(ys) - min(ys), stroke,
            strokeWidth=1 if style == "-.->" else 2,
            strokeStyle="dashed" if style == "-.->" else "solid",
            # A bend round a block is a curve; a route round a layer keeps its corners.
            roundness={"type": 2} if len(rel) == 3 else None,
            points=rel, lastCommittedPoint=None,
            startBinding={"elementId": a.eid, "focus": 0, "gap": 4},
            endBinding={"elementId": b.eid, "focus": 0, "gap": 4},
            startArrowhead=None, endArrowhead="arrow",
        ))
        by_id[a.eid]["boundElements"].append({"type": "arrow", "id": aid})
        by_id[b.eid]["boundElements"].append({"type": "arrow", "id": aid})
        if word:
            # Half way along, unless another label or a block is already there:
            # where two arrows cross, their midpoints often coincide.
            wl, hl = _width([word], DETAIL), DETAIL * LINE
            spot = None
            for frac in (0.5, 0.4, 0.6, 0.3, 0.7, 0.22, 0.78):
                mx, my = _point_at(points, frac)
                box = (mx - wl / 2, my - hl / 2, wl, hl)
                if not _blocked([(mx, my), (mx, my)], written + others) and not any(
                        box[0] < o[0] + o[2] and o[0] < box[0] + box[2] and
                        box[1] < o[1] + o[3] and o[1] < box[1] + box[3] for o in written):
                    spot = box
                    break
            if spot is None:
                mx, my = _point_at(points, 0.5)
                spot = (mx - wl / 2, my - hl / 2, wl, hl)
            written.append(spot)
            lid = aid + "-label"
            add(_text(lid, word, spot[0], spot[1], DETAIL, stroke,
                      align="center", valign="middle", container=aid))
            arrow["boundElements"].append({"type": "text", "id": lid})

    if omitted:
        bottom = max(b.y + b.block_h for b in blocks.values()) if blocks else TOP
        add(_text("omitted", f"+{omitted} nós ficaram de fora — nomeie um alvo ou suba --max-nodes",
                  0, bottom + 50, DETAIL, BODY))

    doc = {
        "type": "excalidraw",
        "version": 2,
        "source": f"{DRAW_MARKER} — refeito por `croqui draw`; tire esta marca para o diagrama virar seu",
        "elements": out,
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20},
        "files": {},
    }
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
