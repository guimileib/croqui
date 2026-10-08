"""Seed the PRD and UML folders during ``croqui scan``.

Uses the project's existing doc folders, else ``.croqui/prd`` and ``.croqui/uml``.
The PRD is written once and never touched again; ``relacoes.mmd`` is regenerated
on every scan while it keeps its ``croqui:gerado`` line.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

from .docs import collect_section, section_dirs

PRD_FILE = "01-visao-geral.md"
RELATION_FILE = "relacoes.mmd"

# Where the two folders are born when the project has none. `docs.py` looks here
# first anyway, so a seed in `.croqui/` is found by exactly the same search that
# would have found one at the root.
DEFAULT_ROOT = ".croqui"

# The line that makes the diagram croqui's to rewrite. It is in the file, in the
# README and in the scan output, because the whole contract is "delete this line".
MARKER = "croqui:gerado"

# The same contract for prose. A PRD carrying this is a draft croqui put there —
# the blank form, or one `croqui prd` wrote — so `croqui prd` may replace it. The
# moment somebody deletes the paragraph the file is theirs and nothing rewrites it.
# Prose and diagram get the same rule for the same reason: the alternative is a tool
# that decides on your behalf which of your sentences were important.
DRAFT_MARKER = "croqui:rascunho"
MARKER_WINDOW = 800

# A mermaid flowchart is drawn by croqui itself, node by node, so a 400-node graph
# is a slow tab showing an unreadable picture. Past this the diagram is a sample and
# says how much it left out — the map next door still has everything.
MAX_NODES = 150

# kind -> (frame title, id prefix, label open, label close). The shapes match the
# map: a table is a cylinder there too.
FRAMES = (
    ("route", "endpoints", "r", '(["', '"])'),
    ("service", "serviços", "s", '["', '"]'),
    ("repo", "repositórios", "p", '[["', '"]]'),
    ("table", "tabelas", "t", '[("', '")]'),
    ("external", "externos", "x", '{{"', '"}}'),
)

# edge kind -> (mermaid arrow, label). Reading is a dotted line and writing a thick
# one, the same distinction the map draws.
ARROWS = {
    "calls": ("-->", ""),
    "read": ("-.->", "lê"),
    "write": ("==>", "grava"),
    "send": ("-->", "envia"),
}


@dataclass
class Report:
    """What the scan actually did to the two folders, for the CLI to print."""

    dirs: list[str] = field(default_factory=list)      # created, were not there
    seeded: list[str] = field(default_factory=list)    # written for the first time
    refreshed: str | None = None                       # the generated diagram, redone
    kept: str | None = None                            # yours now — marker gone
    omitted: int = 0                                   # nodes left out of the diagram
    errors: list[str] = field(default_factory=list)

    def touched(self) -> bool:
        return bool(self.dirs or self.seeded or self.refreshed)


# --------------------------------------------------------------------- helpers


def _label(text: object, limit: int = 52) -> str:
    """A mermaid label: one line, no quote to close it early, no pipe to split it."""
    clean = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    clean = clean.replace('"', "'").replace("|", "/")
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean or "?"


def target_dir(root: Path, section: str) -> Path:
    """The folder this project already uses for that section, or ``.croqui/<section>``."""
    existing = section_dirs(root, section)
    return existing[0] if existing else root / DEFAULT_ROOT / section


def _pick(nodes: list[dict]) -> tuple[dict[str, list[dict]], int]:
    """Group nodes by kind, capped at ``MAX_NODES`` round-robin so every kind is represented."""
    by_kind: dict[str, list[dict]] = {kind: [] for kind, *_ in FRAMES}
    for node in nodes:
        by_kind.setdefault(node.get("kind") or "external", []).append(node)

    total = sum(len(v) for v in by_kind.values())
    if total <= MAX_NODES:
        return by_kind, 0

    taken: dict[str, list[dict]] = {kind: [] for kind in by_kind}
    left = MAX_NODES
    index = 0
    while left > 0 and any(len(by_kind[k]) > index for k in by_kind):
        for kind in by_kind:
            if left <= 0:
                break
            if len(by_kind[kind]) > index:
                taken[kind].append(by_kind[kind][index])
                left -= 1
        index += 1
    return taken, total - MAX_NODES


# ------------------------------------------------------------------ generation


def relation_mermaid(graph: dict) -> str | None:
    """The scanned graph as a mermaid flowchart, or None when there is nothing yet."""
    return _relation(graph)[0]


def _relation(graph: dict) -> tuple[str | None, int]:
    """The diagram and how many nodes it had to leave out."""
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    if not nodes:
        return None, 0

    by_kind, omitted = _pick(nodes)
    ids: dict[str, str] = {}
    body: list[str] = []

    for kind, title, prefix, open_wrap, close_wrap in FRAMES:
        chosen = by_kind.get(kind) or []
        if not chosen:
            continue
        body.append(f'  subgraph g_{kind}["{title} ({len(chosen)})"]')
        for i, node in enumerate(chosen, 1):
            mid = f"{prefix}{i}"
            ids[node["id"]] = mid
            body.append(f'    {mid}{open_wrap}{_label(node.get("label"))}{close_wrap}')
        body.append("  end")

    # Anything the scanner produced under a kind the map has no column for still has
    # a node id, so it can still be an endpoint of an edge.
    for kind, chosen in by_kind.items():
        if kind in {k for k, *_ in FRAMES} or not chosen:
            continue
        for i, node in enumerate(chosen, 1):
            mid = f"o_{re.sub(r'[^A-Za-z0-9]', '', kind) or 'n'}{i}"
            ids[node["id"]] = mid
            body.append(f'  {mid}["{_label(node.get("label"))}"]')

    seen: set[str] = set()
    links: list[str] = []
    for edge in graph.get("edges") or []:
        src, dst = ids.get(edge.get("from")), ids.get(edge.get("to"))
        if not src or not dst or src == dst:
            continue
        arrow, word = ARROWS.get(edge.get("kind"), ("-->", _label(edge.get("kind"), 16)))
        line = f"  {src} {arrow}{'|' + word + '|' if word else ''} {dst}"
        if line in seen:
            continue
        seen.add(line)
        links.append(line)

    name = _label((graph.get("project") or {}).get("name") or "projeto", 40)
    head = [
        "---",
        f"title: Relações — {name}",
        "---",
        f"%% {MARKER} — reescrito a cada `croqui scan` enquanto esta linha existir.",
        "%% Apague esta linha para o diagrama virar seu; o croqui nunca mais o toca.",
    ]
    if omitted:
        head.append(f"%% {omitted} nós ficaram de fora deste desenho — a aba mapa tem todos.")
    head.append("flowchart LR")
    return "\n".join(head + body + links) + "\n", omitted


def prd_template(graph: dict, today: datetime.date, relation: str | None = None) -> str:
    """A blank PRD pre-filled with this project's numbers, pointing at ``relation``."""
    relation = relation or f"uml/{RELATION_FILE}"
    project = graph.get("project") or {}
    stats = graph.get("stats") or {}
    name = str(project.get("name") or "projeto")
    counted = " · ".join(
        f"{stats[kind]} {one if stats[kind] == 1 else many}"
        for kind, one, many in (
            ("route", "endpoint", "endpoints"),
            ("service", "serviço", "serviços"),
            ("repo", "repositório", "repositórios"),
            ("table", "tabela", "tabelas"),
        )
        if stats.get(kind)
    ) or "nada ainda"
    return f"""# PRD — {name}

> Rascunho criado pelo croqui em {today.isoformat()} (`{DRAFT_MARKER}`). Apague este
> parágrafo e o arquivo passa a ser seu: o croqui só lê esta pasta para desenhar a aba
> **PRD** e nunca reescreve o que você escrever.
>
> Para preencher com o que está no código em vez de na sua memória: `croqui prd`.

## Problema

> _Que dor existe hoje, para quem, e como ela aparece. Não descreva a solução._

## Quem usa

> _Os papéis, e o que cada um precisa conseguir terminar._

## Escopo

> _O que esta versão entrega. Uma lista curta de coisas verificáveis._

## Fora de escopo

> _O que foi decidido não fazer agora, e por quê — isto costuma valer mais que o resto._

## Requisitos

| # | requisito | como se verifica |
|---|---|---|
| 1 |  |  |

## Como isto se relaciona com o código

A varredura de {today.isoformat()} encontrou **{counted}**. O desenho dessa relação
está na aba **UML**, em `{relation}`, refeito a cada `croqui scan`. Quando o
que está escrito aqui e o que está desenhado lá discordarem, um dos dois está errado
— e é essa a pergunta que vale a pena fazer numa revisão.

## Perguntas em aberto

> _O que ainda não foi decidido, e quem decide._
"""


# ---------------------------------------------------------------------- writing


def _write(path: Path, text: str, report: Report) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        # A read-only checkout is not a reason to fail a scan; it costs the tabs,
        # not the map.
        report.errors.append(f"{path.name}: {exc.strerror or exc}")
        return False


def scaffold(root: Path, graph: dict, *, today: datetime.date | None = None) -> Report:
    """Create ``prd/`` and ``uml/`` and put the first document in each."""
    report = Report()
    today = today or datetime.date.today()

    for section in ("prd", "uml"):
        directory = target_dir(root, section)
        if not directory.is_dir():
            try:
                directory.mkdir(parents=True, exist_ok=True)
                report.dirs.append(_rel(directory, root))
            except OSError as exc:
                report.errors.append(f"{section}/: {exc.strerror or exc}")
                continue

    # --- PRD: only when this project has no PRD anywhere croqui looks.
    prd_dir = target_dir(root, "prd")
    if prd_dir.is_dir() and not collect_section(root, "prd"):
        target = prd_dir / PRD_FILE
        relation = _rel(target_dir(root, "uml") / RELATION_FILE, root)
        if not target.exists() and _write(target, prd_template(graph, today, relation), report):
            report.seeded.append(_rel(target, root))

    # --- UML: the generated relation, refreshed while it still carries the marker.
    uml_dir = target_dir(root, "uml")
    diagram, omitted = _relation(graph)
    if uml_dir.is_dir() and diagram:
        target = uml_dir / RELATION_FILE
        rel = _rel(target, root)
        if not target.exists():
            if _write(target, diagram, report):
                report.seeded.append(rel)
                report.omitted = omitted
        else:
            try:
                current = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                report.errors.append(f"{target.name}: {exc.strerror or exc}")
                current = ""
            if MARKER not in current[:MARKER_WINDOW]:
                report.kept = rel
            elif current != diagram and _write(target, diagram, report):
                report.refreshed = rel
                report.omitted = omitted
    return report


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
