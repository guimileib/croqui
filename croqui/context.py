"""The map as context for generative AI: API, tables, documents, and where to write.

``render`` produces a compact text digest; ``build`` returns the same facts as data
(used by ``--json`` and ``mcp.py``). The digest is trimmed on purpose to stay small.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from .docs import collect_docs
from .scaffold import MARKER, RELATION_FILE, target_dir

# Budgets. Generous enough for a real API, small enough that the whole thing lands
# in a few thousand tokens.
SUMMARY_CHARS = 160
DESC_CHARS = 400
MAX_CHAIN = 6
MAX_COLUMNS = 40
MAX_DOC_CHARS = 20000

KIND_LABEL = {
    "route": "endpoint",
    "service": "serviço",
    "repo": "repositório",
    "table": "tabela",
    "external": "externo",
}
OP_WORD = {"read": "lê", "write": "grava", "calls": "chama", "send": "envia"}


# ----------------------------------------------------------------------- helpers


def _clip(text: object, limit: int) -> str:
    """One paragraph's worth, on one line. Markdown survives; the token count does not."""
    clean = " ".join(str(text if text is not None else "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _index(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}


def _adjacency(graph: dict) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    out: dict[str, list[dict]] = {}
    into: dict[str, list[dict]] = {}
    for edge in graph.get("edges") or []:
        out.setdefault(edge.get("from"), []).append(edge)
        into.setdefault(edge.get("to"), []).append(edge)
    return out, into


def _chain(start: str, nodes: dict, out: dict, depth: int = 0, seen: frozenset = frozenset()) -> list[list[dict]]:
    """Every path from a node down to the database, as lists of ``{label, kind, ops}``.

    Cycle-guarded and depth-capped.
    """
    if depth >= MAX_CHAIN or start in seen:
        return [[]]
    edges = out.get(start) or []
    if not edges:
        return [[]]
    paths: list[list[dict]] = []
    for edge in edges:
        target = nodes.get(edge.get("to"))
        if target is None:
            continue
        step = {
            "id": target["id"],
            "label": target["label"],
            "kind": target["kind"],
            "how": edge.get("kind"),
            "ops": edge.get("ops") or [],
        }
        for tail in _chain(target["id"], nodes, out, depth + 1, seen | {start}):
            paths.append([step] + tail)
    return paths or [[]]


def _chain_text(path: list[dict]) -> str:
    parts = []
    for step in path:
        word = OP_WORD.get(step["how"], step["how"] or "")
        ops = ("/".join(op.lower() for op in step["ops"])) if step["ops"] else ""
        arrow = f" -[{word}{': ' + ops if ops else ''}]-> " if step["kind"] == "table" else " → "
        parts.append(arrow + step["label"])
    return "".join(parts).lstrip()


# ------------------------------------------------------------------------- build


def build(root: Path, graph: dict, *, full: bool = False) -> dict:
    """Everything a model needs about this project, as plain data."""
    nodes = _index(graph)
    out, into = _adjacency(graph)
    docs = collect_docs(root)

    endpoints = []
    for node in nodes.values():
        if node["kind"] != "route":
            continue
        meta = node.get("meta") or {}
        chains = [_chain_text(p) for p in _chain(node["id"], nodes, out) if p]
        entry = {
            "id": node["id"],
            "method": meta.get("method"),
            "path": meta.get("path"),
            "group": node.get("group"),
            "summary": _clip(meta.get("summary"), SUMMARY_CHARS) or None,
            "source": meta.get("source"),
            "chains": chains,
        }
        if meta.get("deprecated"):
            entry["deprecated"] = True
        if full and meta.get("description"):
            entry["description"] = _clip(meta["description"], MAX_DOC_CHARS)
        elif meta.get("description"):
            entry["description"] = _clip(meta["description"], DESC_CHARS)
        endpoints.append(entry)
    endpoints.sort(key=lambda e: (e.get("group") or "", e.get("path") or "", e.get("method") or ""))

    tables = []
    for node in nodes.values():
        if node["kind"] != "table":
            continue
        meta = node.get("meta") or {}
        touched: dict[str, set[str]] = {}
        for edge in into.get(node["id"]) or []:
            source = nodes.get(edge.get("from"))
            if source:
                touched.setdefault(OP_WORD.get(edge.get("kind"), "usa"), set()).add(source["label"])
        tables.append({
            "id": node["id"],
            "name": node["label"],
            "schema": meta.get("schema"),
            "object_kind": meta.get("object_kind") or "table",
            "defined_in": meta.get("defined_in") or [],
            "columns": (meta.get("columns") or [])[:MAX_COLUMNS],
            "touched_by": {k: sorted(v) for k, v in touched.items()},
        })
    tables.sort(key=lambda t: (t.get("schema") or "", t["name"]))

    def _plain(kind: str) -> list[dict]:
        return sorted(
            (
                {
                    "id": n["id"],
                    "label": n["label"],
                    "source": (n.get("meta") or {}).get("source"),
                    "uses": [nodes[e["to"]]["label"] for e in (out.get(n["id"]) or []) if e["to"] in nodes],
                }
                for n in nodes.values()
                if n["kind"] == kind
            ),
            key=lambda n: n["label"],
        )

    def _docs(section: str) -> list[dict]:
        listed = []
        for doc in docs.get(section) or []:
            entry = {"name": doc["name"], "title": doc["title"]}
            if MARKER in doc["text"][:800]:
                entry["generated"] = True
            if full:
                entry["text"] = doc["text"][:MAX_DOC_CHARS]
            listed.append(entry)
        return listed

    return {
        "croqui_context": 1,
        "project": graph.get("project") or {},
        "stats": graph.get("stats") or {},
        "endpoints": endpoints,
        "services": _plain("service"),
        "repositories": _plain("repo"),
        "tables": tables,
        "external": _plain("external"),
        "docs": {"prd": _docs("prd"), "uml": _docs("uml")},
        "writing": writing_guide(root),
    }


def _rel_dir(path: Path, root: Path) -> str:
    """A folder as the model should type it: relative to the project, forward slashes."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def writing_guide(root: Path) -> dict:
    """Where a model may write (this project's own doc folders) and which file to leave alone."""
    prd_dir = _rel_dir(target_dir(root, "prd"), root)
    uml_dir = _rel_dir(target_dir(root, "uml"), root)
    return {
        "prd": {
            "where": f"{prd_dir}/*.md",
            "how": "Markdown. O primeiro título vira o nome do documento na aba PRD. "
                   "Um bloco ```mermaid dentro do PRD é desenhado ali mesmo.",
        },
        "uml": {
            "where": f"{uml_dir}/*.mmd",
            "how": "Mermaid. flowchart/graph e sequenceDiagram são desenhados; "
                   "qualquer outro tipo aparece com o código fonte visível.",
        },
        "do_not_touch": [
            f"{uml_dir}/{RELATION_FILE} — gerado pelo croqui e reescrito a cada `croqui scan`. "
            f"Escreva um arquivo novo ao lado, ou apague a linha `%% {MARKER}` para tomar posse dele.",
            ".croqui/graph.json — derivado do código. Mude o código e rode `croqui scan`.",
        ],
        "after_writing": "Nada a fazer: a aba relê o disco a cada reload. "
                         "Rode `croqui scan` só quando o código mudar.",
    }


# ------------------------------------------------------------------------ render


def render(ctx: dict, *, full: bool = False) -> str:
    """The digest as markdown — what you paste into a context window."""
    project = ctx.get("project") or {}
    stats = ctx.get("stats") or {}
    lines: list[str] = []
    add = lines.append

    add(f"# croqui — {project.get('name', 'projeto')}")
    counted = " · ".join(
        f"{stats.get(kind, 0)} {word}"
        for kind, word in (("route", "endpoints"), ("service", "serviços"),
                           ("repo", "repositórios"), ("table", "tabelas"))
        if stats.get(kind)
    )
    add(f"{counted or 'mapa vazio'} · stack `{project.get('stack', '?')}`")
    add("")
    add("Este é o mapa que o croqui extraiu do código: quem chama quem, e qual endpoint")
    add("toca qual tabela. É derivado do código, não escrito à mão — quando ele e a")
    add("prosa discordarem, a prosa está velha ou o código está errado.")
    add("")

    if ctx.get("endpoints"):
        add("## endpoints")
        group = object()
        for endpoint in ctx["endpoints"]:
            if endpoint.get("group") != group:
                group = endpoint.get("group")
                add("")
                add(f"### {group or 'sem grupo'}")
            head = f"- **{endpoint.get('method', '?')} {endpoint.get('path', endpoint['id'])}**"
            if endpoint.get("deprecated"):
                head += " _(deprecated)_"
            if endpoint.get("summary"):
                head += f" — {endpoint['summary']}"
            add(head)
            if endpoint.get("source"):
                add(f"  - `{endpoint['source']}`")
            for chain in endpoint.get("chains") or []:
                add(f"  - {chain}")
            if full and endpoint.get("description"):
                add(f"  - doc: {endpoint['description']}")
        add("")

    if ctx.get("tables"):
        add("## tabelas")
        for table in ctx["tables"]:
            name = f"{table['schema']}.{table['name']}" if table.get("schema") else table["name"]
            add(f"- **{name}** ({table.get('object_kind', 'table')})")
            for column in table.get("columns") or []:
                flags = "".join([
                    " PK" if column.get("pk") else "",
                    " not null" if column.get("required") else "",
                    f" → {column['references']}" if column.get("references") else "",
                ])
                add(f"  - `{column['name']}` {column.get('type') or ''}{flags}".rstrip())
            for how, who in sorted((table.get("touched_by") or {}).items()):
                add(f"  - {how}: {', '.join(who)}")
        add("")

    for key, title in (("services", "serviços"), ("repositories", "repositórios"), ("external", "externos")):
        if ctx.get(key):
            add(f"## {title}")
            for node in ctx[key]:
                tail = f" → {', '.join(node['uses'])}" if node.get("uses") else ""
                where = f"  `{node['source']}`" if node.get("source") else ""
                add(f"- **{node['label']}**{tail}{where}")
            add("")

    docs = ctx.get("docs") or {}
    if docs.get("prd") or docs.get("uml"):
        add("## documentos")
        for section in ("prd", "uml"):
            for doc in docs.get(section) or []:
                mark = " _(gerado pelo croqui)_" if doc.get("generated") else ""
                add(f"- `{doc['name']}` — {doc['title']}{mark}")
                if full and doc.get("text"):
                    add("")
                    add("  " + doc["text"].replace("\n", "\n  "))
                    add("")
        add("")

    guide = ctx.get("writing") or {}
    add("## como escrever aqui")
    add("")
    for section in ("prd", "uml"):
        item = guide.get(section) or {}
        add(f"- **{section.upper()}** → `{item.get('where', '')}`. {item.get('how', '')}")
    for warning in guide.get("do_not_touch") or []:
        add(f"- ⚠ {warning}")
    if guide.get("after_writing"):
        add(f"- {guide['after_writing']}")
    add("")
    return "\n".join(lines)


def render_node(ctx_graph: dict, root: Path, node_id: str) -> str | None:
    """Everything about one node — the answer to "tell me about this endpoint"."""
    nodes = _index(ctx_graph)
    node = nodes.get(node_id) or _find(nodes, node_id)
    if node is None:
        return None
    out, into = _adjacency(ctx_graph)
    meta = node.get("meta") or {}
    lines = [f"# {node['label']}", f"{KIND_LABEL.get(node['kind'], node['kind'])} · `{node['id']}`", ""]
    if meta.get("source"):
        lines.append(f"definido em `{meta['source']}`")
    if meta.get("defined_in"):
        lines.append("definido em " + ", ".join(f"`{s}`" for s in meta["defined_in"]))
    if meta.get("summary"):
        lines += ["", meta["summary"]]
    if meta.get("description"):
        lines += ["", "## documentação", "", str(meta["description"])[:MAX_DOC_CHARS]]

    if meta.get("columns"):
        lines += ["", "## colunas", ""]
        for column in meta["columns"]:
            flags = "".join([
                " PK" if column.get("pk") else "",
                " not null" if column.get("required") else "",
                f" → {column['references']}" if column.get("references") else "",
            ])
            lines.append(f"- `{column['name']}` {column.get('type') or ''}{flags}".rstrip())

    for title, params in (("parâmetros", meta.get("params")), ):
        if params:
            lines += ["", f"## {title}", ""]
            for param in params:
                required = " (obrigatório)" if param.get("required") else ""
                lines.append(f"- `{param.get('name')}` {param.get('type') or ''} em {param.get('in')}{required}"
                             + (f" — {_clip(param.get('description'), DESC_CHARS)}" if param.get("description") else ""))
    if meta.get("responses"):
        lines += ["", "## respostas", ""]
        for response in meta["responses"]:
            lines.append(f"- `{response.get('code')}` {response.get('description') or ''}".rstrip())

    for title, edges, other in (("usa", out.get(node["id"]) or [], "to"), ("usado por", into.get(node["id"]) or [], "from")):
        if edges:
            lines += ["", f"## {title}", ""]
            for edge in edges:
                target = nodes.get(edge[other])
                ops = f" ({'/'.join(edge.get('ops') or [])})" if edge.get("ops") else ""
                evidence = f" — {edge['evidence'][0]}" if edge.get("evidence") else ""
                lines.append(f"- {OP_WORD.get(edge.get('kind'), edge.get('kind'))} "
                             f"{target['label'] if target else edge[other]}{ops}{evidence}")
    return "\n".join(lines) + "\n"


def _find(nodes: dict, needle: str) -> dict | None:
    """Match a node the way a person names one: `/v1/orders`, `GET /v1/orders`, `tb_orders`."""
    want = needle.strip().lower()
    for node in nodes.values():
        meta = node.get("meta") or {}
        candidates = {
            str(node["id"]).lower(),
            str(node["label"]).lower(),
            str(meta.get("path") or "").lower(),
            f"{meta.get('method', '')} {meta.get('path', '')}".strip().lower(),
        }
        if want in candidates:
            return node
    return None


def as_json(ctx: dict) -> str:
    return json.dumps(ctx, indent=2, ensure_ascii=False) + "\n"


def stamp(path: Path) -> str:
    """When the graph was written, so a model knows how stale the map might be."""
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "?"
