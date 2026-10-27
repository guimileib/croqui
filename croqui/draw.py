"""``croqui draw``: a styled Mermaid flowchart straight from the map, with no model in the loop.

A model that draws a diagram by hand spends tokens on the part the map already knows
(who calls whom, which table is touched) and on styling it gets slightly different
every time. This writes that part deterministically: same graph and same arguments
give a byte-identical file, so it diffs well and re-running it is free. What is left
for the model is only what the code cannot tell — a business condition, a better name.

The output stays inside what ``viewer/diagram.js`` draws; ``lint.py`` holds it to that.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .context import _index, _names
from .scaffold import MARKER, MARKER_WINDOW, RELATION_FILE, _label, target_dir

# The line that lets a re-run replace the file. Same contract as ``croqui:gerado``:
# delete it and the diagram is yours. It is a different word on purpose — `scan` owns
# `relacoes.mmd`, `draw` owns only what it wrote itself.
DRAW_MARKER = "croqui:draw"
DEFAULT_NAME = "visao-geral"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SUFFIXES = (".mmd", ".mermaid")
DIRECTIONS = ("LR", "TB")

DEFAULT_DEPTH = 4
DEFAULT_UP = 1
DEFAULT_MAX_NODES = 25

# (id, frame title, kinds). One frame per layer, left to right. An `entry` is a way
# into the system like a route, and a `module` is a unit of logic like a service.
LAYERS = (
    ("api", "API", ("entry", "route")),
    ("dominio", "domínio", ("module", "service")),
    ("repos", "repositórios", ("repo",)),
    ("dados", "dados", ("table",)),
    ("externos", "externos", ("external",)),
)

# kind -> (id prefix, label open, label close, palette class)
SHAPES = {
    "entry": ("e", '(["', '"])', "accent"),
    "route": ("r", '(["', '"])', "route"),
    "module": ("m", '["', '"]', "service"),
    "service": ("s", '["', '"]', "service"),
    "repo": ("p", '[["', '"]]', "repo"),
    "table": ("t", '[("', '")]', "table"),
    "external": ("x", '{{"', '"}}', "external"),
}
OTHER = ("o", '["', '"]', "muted")
VERBS = {"get", "post", "put", "patch", "delete"}


class DrawError(Exception):
    """Something the user can fix by changing the command, said in one line."""


@dataclass
class Drawing:
    text: str
    nodes: int
    omitted: int
    title: str
    name: str                       # the file name `write` picks when none is given
    missing: list[str] = field(default_factory=list)   # targets the map has no node for


# --------------------------------------------------------------------- selection


def _loose(text: object) -> str:
    """`GET /leads/` and `get /leads` are the same endpoint to a person."""
    clean = " ".join(str(text or "").lower().split())
    return clean.rstrip("/") if len(clean) > 1 else clean


def resolve(nodes: dict[str, dict], needle: str) -> list[dict]:
    """Every node a person's name points at: an exact name first, then a group, then a fragment.

    A bare path matches every method on it, which is what `croqui draw /v1/leads` means.
    """
    want = _loose(needle)
    if not want:
        return []
    ordered = sorted(nodes.values(), key=lambda n: (str(n["label"]).lower(), n["id"]))
    exact = [n for n in ordered if want in {_loose(x) for x in _names(n)}]
    if exact:
        return exact
    group = [n for n in ordered if _loose(n.get("group")) == want and n["kind"] in ("entry", "route")]
    if group:
        return group
    if len(want) < 3:
        return []
    return [n for n in ordered if any(want in _loose(x) for x in _names(n))]


def _walk(starts: list[str], edges: dict[str, list[dict]], key: str, depth: int) -> dict[str, int]:
    """Breadth-first distance from ``starts`` along ``key`` ("to" or "from"); cycle-safe."""
    dist = {s: 0 for s in starts}
    queue = deque(starts)
    while queue:
        here = queue.popleft()
        if dist[here] >= depth:
            continue
        for edge in edges.get(here) or []:
            there = edge[key]
            if there not in dist:
                dist[there] = dist[here] + 1
                queue.append(there)
    return dist


def _sides(graph: dict) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Edges by source and by target, keeping only those with both ends in the map."""
    nodes = _index(graph)
    out: dict[str, list[dict]] = {}
    into: dict[str, list[dict]] = {}
    for edge in graph.get("edges") or []:
        if edge.get("from") in nodes and edge.get("to") in nodes and edge["from"] != edge["to"]:
            out.setdefault(edge["from"], []).append({"to": edge["to"], "from": edge["from"]})
            into.setdefault(edge["to"], []).append({"to": edge["to"], "from": edge["from"]})
    return out, into


def _focus(seeds: list[dict], out: dict, into: dict, depth: int, up: int, cap: int) -> list[str]:
    """The seeds, what they reach downhill and who calls them, nearest first, cut at ``cap``."""
    ids = [s["id"] for s in seeds]
    rank: dict[str, float] = {}
    for node_id, d in _walk(ids, out, "to", depth).items():
        rank[node_id] = float(d)
    for node_id, d in _walk(ids, into, "from", up).items():
        rank[node_id] = min(rank.get(node_id, 99.0), d + 0.5 if d else 0.0)
    ordered = sorted(rank, key=lambda i: (rank[i], i))
    return ordered[:cap]


def _overview(nodes: dict[str, dict], out: dict, into: dict, cap: int) -> list[str]:
    """Endpoints with the chain behind each, one group at a time, until the next chain would not fit.

    Round-robin over groups so a large API is sampled, not just its first resource —
    the same idea as ``scaffold._pick``, but it keeps a chain whole or leaves it out.
    """
    ordered = sorted(nodes.values(), key=lambda n: (str(n["label"]).lower(), n["id"]))
    roots = [n for n in ordered if n["kind"] in ("entry", "route")]
    if not roots:
        roots = [n for n in ordered if n["id"] not in into]
    groups: dict[str, deque] = {}
    for node in roots:
        groups.setdefault(str(node.get("group") or ""), deque()).append(node)
    queues = [groups[g] for g in sorted(groups)]

    chosen: dict[str, None] = {}
    added = True
    while added:
        added = False
        for queue in queues:
            while queue:
                chain = _walk([queue.popleft()["id"]], out, "to", DEFAULT_DEPTH + 2)
                new = [i for i in chain if i not in chosen]
                if len(chosen) + len(new) <= cap:
                    chosen.update(dict.fromkeys(sorted(chain, key=lambda i: (chain[i], i))))
                    added = True
                    break
    if not chosen and roots:
        # One chain alone is bigger than the cap: its nearest nodes are still a picture.
        chain = _walk([roots[0]["id"]], out, "to", DEFAULT_DEPTH + 2)
        return sorted(chain, key=lambda i: (chain[i], i))[:cap]
    return list(chosen)


# ------------------------------------------------------------------- rendering


def _text(value: object, limit: int = 52) -> str:
    """A label that survives diagram.js: no quote, pipe, `%%` comment or `--` arrow start."""
    return _label(value, limit).replace("%%", "%").replace("--", "–")


def _layer(kind: str) -> int:
    for i, (_, _, kinds) in enumerate(LAYERS):
        if kind in kinds:
            return i
    return len(LAYERS)


def _shape(node: dict) -> tuple[str, str, str, str]:
    prefix, left, right, tone = SHAPES.get(node["kind"], OTHER)
    if node["kind"] == "route":
        verb = str((node.get("meta") or {}).get("method") or "").lower()
        tone = verb if verb in VERBS else tone
    return prefix, left, right, tone


def _arrow(edge: dict) -> tuple[str, str]:
    """(arrow, label). Reading is dotted and writing thick, like the map; ops say how."""
    kind = edge.get("kind")
    ops = [str(op).lower() for op in edge.get("ops") or []]
    if kind == "write":
        ops = [op for op in ops if op != "select"]
        return "==>", _text("grava: " + "/".join(ops), 30) if ops else "grava"
    if kind == "read":
        extra = [op for op in ops if op != "select"]
        return "-.->", _text("lê: " + "/".join(extra), 30) if extra else "lê"
    if kind == "send":
        return "-->", "envia"
    if kind == "calls":
        return "-->", ""
    return "-->", _text(kind, 16) if kind else ""


def _compose(graph: dict, picked: list[str], omitted: int, title: str, direction: str) -> str:
    nodes = _index(graph)
    chosen = sorted((nodes[i] for i in picked), key=lambda n: (_layer(n["kind"]), str(n["label"]).lower(), n["id"]))

    ids: dict[str, str] = {}
    counters: dict[str, int] = {}
    for node in chosen:
        prefix = _shape(node)[0]
        counters[prefix] = counters.get(prefix, 0) + 1
        ids[node["id"]] = f"{prefix}{counters[prefix]}"

    body: list[str] = []
    for i, (frame, label, _) in enumerate(LAYERS):
        members = [n for n in chosen if _layer(n["kind"]) == i]
        if not members:
            continue
        body.append(f'  subgraph g_{frame}["{label}"]')
        for node in members:
            _, left, right, tone = _shape(node)
            body.append(f'    {ids[node["id"]]}{left}{_text(node["label"])}{right}:::{tone}')
        body.append("  end")
    for node in (n for n in chosen if _layer(n["kind"]) == len(LAYERS)):
        _, left, right, tone = _shape(node)
        body.append(f'  {ids[node["id"]]}{left}{_text(node["label"])}{right}:::{tone}')

    links: dict[tuple[str, str, str, str], None] = {}
    for edge in graph.get("edges") or []:
        src, dst = ids.get(edge.get("from")), ids.get(edge.get("to"))
        if src and dst and src != dst:
            arrow, word = _arrow(edge)
            links[(src, dst, arrow, word)] = None
    order = {mid: n for n, mid in enumerate(ids.values())}
    lines = [
        f"  {s} {a}{'|' + w + '|' if w else ''} {d}"
        for s, d, a, w in sorted(links, key=lambda k: (order[k[0]], order[k[1]], k[2], k[3]))
    ]

    head = [
        "---",
        f"title: {_text(title, 70)}",
        "---",
        f"%% {DRAW_MARKER} — refeito por `croqui draw`. Apague esta linha para o diagrama virar seu.",
    ]
    if omitted:
        head.append(f"%% {omitted} nós ficaram de fora — nomeie um alvo ou suba --max-nodes.")
    head.append(f"flowchart {direction}")
    return "\n".join(head + body + lines) + "\n"


def draw(
    graph: dict,
    targets: list[str] | tuple[str, ...] = (),
    *,
    depth: int = DEFAULT_DEPTH,
    up: int = DEFAULT_UP,
    max_nodes: int = DEFAULT_MAX_NODES,
    direction: str = "LR",
    title: str | None = None,
) -> Drawing:
    """The diagram for ``targets`` (or an overview of the whole map when there are none)."""
    nodes = _index(graph)
    if not nodes:
        raise DrawError("O mapa está vazio. Rode `croqui scan` antes.")
    direction = direction.upper()
    if direction not in DIRECTIONS:
        raise DrawError(f"--direction tem de ser {' ou '.join(DIRECTIONS)}.")
    if max_nodes < 1 or depth < 0 or up < 0:
        raise DrawError("--max-nodes tem de ser >= 1, e --depth >= 0.")

    targets = [t for t in (str(t).strip() for t in targets) if t]
    out, into = _sides(graph)
    missing: list[str] = []
    if targets:
        seeds: dict[str, dict] = {}
        for target in targets:
            found = resolve(nodes, target)
            if not found:
                missing.append(target)
            for node in found:
                seeds.setdefault(node["id"], node)
        if not seeds:
            raise DrawError(f"Nada no mapa com o nome {', '.join(repr(t) for t in missing)}. "
                            f"Tente um caminho (/v1/leads), uma tabela ou o nome de um serviço.")
        picked = _focus(list(seeds.values()), out, into, depth, up, max_nodes)
        reached = len(_focus(list(seeds.values()), out, into, depth, up, len(nodes)))
        shown = ", ".join(str(s["label"]) for s in list(seeds.values())[:3])
        default_title = f"Fluxo — {shown}" + ("…" if len(seeds) > 3 else "")
    else:
        picked = _overview(nodes, out, into, max_nodes)
        reached = len(nodes)
        default_title = f"Visão geral — {(graph.get('project') or {}).get('name') or 'projeto'}"

    omitted = max(reached - len(picked), 0)
    final_title = title or default_title
    return Drawing(
        text=_compose(graph, picked, omitted, final_title, direction),
        nodes=len(picked),
        omitted=omitted,
        title=final_title,
        name=_slug(targets),
        missing=missing,
    )


# --------------------------------------------------------------------- writing


def _slug(targets: list[str]) -> str:
    """`POST /v1/leads` -> `post-v1-leads`; no targets -> `visao-geral`."""
    if not targets:
        return DEFAULT_NAME
    ascii_ = unicodedata.normalize("NFKD", " ".join(targets)).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:60].strip("-")
    return slug or "diagrama"


def file_name(name: str | None, default: str) -> str:
    """A safe `.mmd` file name: no directories, no dotfiles, and an extension the UML tab reads."""
    chosen = Path(name).name if name else default
    if Path(chosen).suffix.lower() not in SUFFIXES:
        chosen += ".mmd"
    if not NAME_RE.match(chosen):
        raise DrawError("Nome inválido para --name: só letras, números, `.`, `-` e `_`.")
    return chosen


def write(root: Path, drawing: Drawing, *, name: str | None = None, force: bool = False) -> tuple[Path, str]:
    """Put the diagram in the UML folder; returns (path, "criado" | "atualizado" | "igual")."""
    target = target_dir(root, "uml") / file_name(name, drawing.name)
    if target.name == RELATION_FILE:
        raise DrawError(f"`{RELATION_FILE}` é o diagrama que o croqui gera a cada scan. "
                        f"Use outro --name.")
    status = "criado"
    if target.exists():
        try:
            current = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise DrawError(f"Não deu para ler {target.name}: {exc.strerror or exc}") from exc
        head = current[:MARKER_WINDOW]
        if MARKER in head:
            raise DrawError(f"`{target.name}` ainda tem a linha `%% {MARKER}`: o croqui a reescreve "
                            f"no scan. Use outro --name.")
        if DRAW_MARKER not in head and not force:
            raise DrawError(f"`{target.name}` já existe e não foi escrito por `croqui draw`. "
                            f"Use outro --name, ou --force para substituir.")
        if current == drawing.text:
            return target, "igual"
        status = "atualizado"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(drawing.text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise DrawError(f"Não deu para escrever {target.name}: {exc.strerror or exc}") from exc
    return target, status
