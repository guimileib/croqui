"""croqui as an MCP server: JSON-RPC 2.0 over stdio, stdlib only.

Exposes the map and documents as tools and lets a model write back into ``prd/``
and ``uml/``. stdout carries the protocol; diagnostics go to stderr.

    claude mcp add croqui -- croqui mcp
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import __version__
from .context import as_json, build, render, render_node
from .docs import SECTIONS, collect_section
from .model import load_graph
from .scaffold import MARKER, RELATION_FILE, target_dir

OUT_DIR = ".croqui"
GRAPH_FILE = "graph.json"

# The version we speak. A client that asks for another gets told what we have, which
# is what the specification says to do.
PROTOCOL = "2024-11-05"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

MAX_DOC_BYTES = 512 * 1024
# A document name a model may write. No directories, no dotfiles, no surprises.
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

TOOLS = [
    {
        "name": "croqui_map",
        "description": (
            "The map croqui extracted from this project's source: endpoints, the service "
            "and repository chain behind each one, and the database tables they touch, with "
            "columns. Start here — it is the cheapest way to learn what this API is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
                "full": {"type": "boolean", "default": False,
                         "description": "include each endpoint's full documentation"},
            },
        },
    },
    {
        "name": "croqui_node",
        "description": (
            "Everything about one node: an endpoint's parameters, responses and documentation, "
            "or a table's columns and who reads and writes it. Accepts a croqui id "
            "(route:GET:/v1/orders), a bare path (/v1/orders), or a table name (tb_orders)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "croqui_docs",
        "description": (
            "The PRD and UML documents that travel with the map — the *why* and the *how we "
            "meant it to work*, which the code cannot tell you. Without arguments it lists "
            "them; with `name` it returns one in full."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["prd", "uml"]},
                "name": {"type": "string", "description": "a path from the listing, e.g. prd/01-visao-geral.md"},
            },
        },
    },
    {
        "name": "croqui_write_doc",
        "description": (
            "Write a document into prd/ (markdown) or uml/ (mermaid). This is how you leave "
            "something behind that the map cannot derive: a requirement, a decision, a diagram "
            "of how it is *meant* to work. Refuses to overwrite an existing document unless "
            "`overwrite` is true, and never touches croqui's own generated diagram."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["prd", "uml"]},
                "name": {"type": "string", "description": "file name, e.g. 02-checkout.md or fluxo-do-pedido.mmd"},
                "text": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["section", "name", "text"],
        },
    },
    {
        "name": "croqui_scan",
        "description": (
            "Re-read the source and rebuild the map, the way `croqui scan` does. Run it after "
            "changing the code; the generated diagram in uml/ is redrawn with it. Returns what "
            "the scan reported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"stack": {"type": "string", "description": "force an adapter"}},
        },
    },
]


class Refused(Exception):
    """A guard-rail refusal, reported as a tool error so the model does not read it as success."""


# ----------------------------------------------------------------------- helpers


def _graph(root: Path) -> dict | None:
    path = root / OUT_DIR / GRAPH_FILE
    if not path.is_file():
        return None
    try:
        return load_graph(path)
    except (json.JSONDecodeError, OSError):
        return None


def _no_graph() -> str:
    return ("Este projeto ainda não foi mapeado. Rode a ferramenta croqui_scan "
            "(ou `croqui scan` no terminal) e tente de novo.")


# ------------------------------------------------------------------------- tools


def _tool_map(root: Path, args: dict) -> str:
    graph = _graph(root)
    if graph is None:
        return _no_graph()
    full = bool(args.get("full"))
    context = build(root, graph, full=full)
    return as_json(context) if args.get("format") == "json" else render(context, full=full)


def _tool_node(root: Path, args: dict) -> str:
    graph = _graph(root)
    if graph is None:
        return _no_graph()
    node = str(args.get("id") or "").strip()
    if not node:
        raise Refused("Faltou `id`.")
    text = render_node(graph, root, node)
    if text is None:
        return (f"Nada no mapa com o nome '{node}'. Chame croqui_map para ver os ids "
                f"— eles se parecem com route:GET:/v1/orders ou table:crm.tb_orders.")
    return text


def _tool_docs(root: Path, args: dict) -> str:
    wanted = args.get("section")
    sections = [wanted] if wanted in SECTIONS else list(SECTIONS)
    name = args.get("name")
    lines: list[str] = []
    for section in sections:
        found = collect_section(root, section)
        if name:
            for doc in found:
                if doc["name"] == name or Path(doc["name"]).name == name:
                    return f"# {doc['title']}\n`{doc['name']}`\n\n{doc['text']}"
            continue
        lines.append(f"## {section.upper()} ({len(found)})")
        for doc in found:
            mark = "  (gerado pelo croqui, reescrito a cada scan)" if MARKER in doc["text"][:800] else ""
            lines.append(f"- `{doc['name']}` — {doc['title']}{mark}")
        if not found:
            lines.append(f"- (nenhum ainda; escreva o primeiro com croqui_write_doc em {section}/)")
        lines.append("")
    if name:
        return f"Não existe documento chamado '{name}'. Chame croqui_docs sem argumentos para ver a lista."
    return "\n".join(lines)


def _tool_write_doc(root: Path, args: dict) -> str:
    section = args.get("section")
    if section not in SECTIONS:
        raise Refused("`section` tem de ser 'prd' ou 'uml'.")
    name = str(args.get("name") or "").strip()
    if not SAFE_NAME_RE.match(name):
        raise Refused("Nome inválido. Só um nome de arquivo simples, sem barras nem ponto "
                      "inicial — por exemplo `02-checkout.md` ou `fluxo-do-pedido.mmd`.")
    suffixes = SECTIONS[section][1]
    if Path(name).suffix.lower() not in suffixes:
        raise Refused(f"Em {section}/ a extensão tem de ser uma de: {', '.join(suffixes)}.")
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refused("`text` está vazio.")
    if len(text.encode("utf-8")) > MAX_DOC_BYTES:
        raise Refused(f"Documento acima de {MAX_DOC_BYTES // 1024} KB.")

    directory = target_dir(root, section)
    target = directory / name
    if name == RELATION_FILE and section == "uml":
        raise Refused(f"`{RELATION_FILE}` é o diagrama que o croqui gera do código e reescreve a "
                      f"cada scan — o que você escrever ali se perde. Escreva um arquivo novo ao "
                      f"lado, ou peça ao usuário para apagar a linha `%% {MARKER}` se quiser "
                      f"tomar posse dele.")
    if target.exists() and not args.get("overwrite"):
        raise Refused(f"`{name}` já existe. Passe overwrite: true se a intenção é substituir "
                      f"o que está lá — leia antes com croqui_docs.")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    except OSError as exc:
        raise Refused(f"Não deu para escrever: {exc.strerror or exc}") from exc
    rel = target.relative_to(root) if target.is_relative_to(root) else target
    return (f"Escrito em `{rel}`. A aba {section.upper()} mostra na próxima vez que a página "
            f"for recarregada — nada de re-scan.")


def _tool_scan(root: Path, args: dict) -> str:
    from .cli import build_parser, cmd_scan

    argv = ["scan", str(root)]
    if args.get("stack"):
        argv += ["--stack", str(args["stack"])]
    parsed = build_parser().parse_args(argv)
    # cmd_scan reports to stderr, which is exactly where it must stay: stdout is
    # the protocol. The client gets the same lines through the tool result.
    captured = _Captured()
    import contextlib

    with contextlib.redirect_stderr(captured):
        code = cmd_scan(parsed)
    body = _strip_ansi(captured.text())
    return body + ("\n" if code == 0 else f"\n(scan terminou com código {code})\n")


class _Captured:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return "".join(self.chunks)


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


HANDLERS = {
    "croqui_map": _tool_map,
    "croqui_node": _tool_node,
    "croqui_docs": _tool_docs,
    "croqui_write_doc": _tool_write_doc,
    "croqui_scan": _tool_scan,
}


# ------------------------------------------------------------------ the protocol


def handle(root: Path, message: dict) -> dict | None:
    """One JSON-RPC request in, one response out. None for a notification."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(request_id, {
            "protocolVersion": asked if asked in KNOWN_PROTOCOLS else PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "croqui", "version": __version__},
            "instructions": (
                "croqui maps this project's API and database from its source. Call croqui_map "
                "before answering questions about how the system fits together, croqui_node for "
                "one endpoint or table, and croqui_write_doc to leave a PRD or a diagram behind."
            ),
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(request_id, -32602, f"unknown tool: {name}")
        try:
            text = handler(root, params.get("arguments") or {})
            failed = False
        except Refused as refusal:
            text, failed = str(refusal), True
        except Exception as exc:  # noqa: BLE001 - a tool must not take the server down
            text, failed = f"{type(exc).__name__}: {exc}", True
        return _ok(request_id, {"content": [{"type": "text", "text": text}], "isError": failed})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def _ok(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve_stdio(root: Path) -> int:
    """Read newline-delimited JSON-RPC on stdin, answer on stdout, until EOF."""
    print(f"croqui mcp — {root}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _emit(_error(None, -32700, "parse error"))
            continue
        if isinstance(message, list):
            # A batch: answer each, drop the notifications, stay silent if all were.
            answers = [a for a in (handle(root, m) for m in message if isinstance(m, dict)) if a]
            if answers:
                _emit(answers)
            continue
        if not isinstance(message, dict):
            _emit(_error(None, -32600, "invalid request"))
            continue
        answer = handle(root, message)
        if answer is not None:
            _emit(answer)
    return 0


def _emit(payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
