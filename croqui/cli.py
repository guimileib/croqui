"""croqui command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .adapters import ADAPTERS, detect_stack
from .docs import collect_docs, summarize
from .model import Graph, load_graph
from .scaffold import PRD_FILE
from .tables import build_registry

OUT_DIR = ".croqui"
GRAPH_FILE = "graph.json"
# The graph the layout was last reconciled against. Following a rename means
# diffing two graphs, and using whatever `graph.json` happened to hold would make
# that depend on when the user ran `scan` — run it twice before opening the
# viewer, or once with --no-migrate, and the rename is unrecoverable. This file is
# croqui's own baseline, so the migration is possible whenever the viewer is next
# opened. Derived, gitignored.
PREV_FILE = "graph.prev.json"
LAYOUT_FILE = "layout.json"
BUNDLE_FILE = "croqui.html"

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def _out_dir(root: Path) -> Path:
    path = root / OUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------- commands


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        _say(f"{RED}not a directory: {root}{RESET}")
        return 2

    stack = args.stack or detect_stack(root)
    if stack is None:
        _say(f"{RED}could not detect the stack.{RESET} Pass --stack (one of: {', '.join(ADAPTERS)})")
        return 2
    if stack not in ADAPTERS:
        _say(f"{RED}no adapter for '{stack}'.{RESET} Available: {', '.join(ADAPTERS)}")
        return 2

    _say(f"{BOLD}croqui{RESET} scanning {root.name} {DIM}({stack}){RESET}")

    registry = build_registry(root)
    _say(f"  {GREEN}✓{RESET} {len(registry)} database objects in ground truth {DIM}(DDL + ORM models){RESET}")

    graph = Graph({"name": root.name, "stack": stack, "root": str(root), "croqui_version": __version__})
    scanner = ADAPTERS[stack](root, graph, registry, openapi=args.openapi)
    scanner.scan()

    if graph.project.get("openapi"):
        _say(f"  {GREEN}✓{RESET} spec: {graph.project['openapi']}")
    else:
        _say(f"  {YELLOW}!{RESET} no OpenAPI spec found — routes come from code only, without docs")

    graph.drop_dangling_edges()
    dropped = graph.drop_isolated_nodes()

    counts = graph.counts()
    _say(
        f"  {GREEN}✓{RESET} {counts.get('route', 0)} routes · {counts.get('service', 0)} services · "
        f"{counts.get('repo', 0)} repositories · {counts.get('table', 0)} tables · {len(graph.edges)} edges"
    )
    if dropped:
        _say(f"  {DIM}· {dropped} isolated nodes dropped{RESET}")

    unresolved = [n for n in graph.nodes.values() if n["meta"].get("unresolved")]
    if unresolved:
        _say(f"  {YELLOW}!{RESET} {len(unresolved)} nodes unresolved — run {BOLD}croqui enrich{RESET} to let an LLM finish them")
        for node in unresolved[:5]:
            _say(f"    {DIM}{node['label']} — {node['meta'].get('hint', '')}{RESET}")
        if len(unresolved) > 5:
            _say(f"    {DIM}… and {len(unresolved) - 5} more{RESET}")

    for warning in getattr(scanner, "warnings", [])[:5]:
        _say(f"  {DIM}· {warning}{RESET}")

    out = _out_dir(root)
    target = out / GRAPH_FILE
    previous = _baseline(out)
    current = graph.to_dict()
    graph.write(target)
    _say(f"  {GREEN}✓{RESET} wrote {target.relative_to(root)}")

    _report_docs(root, current, seed=not args.no_docs)

    report = _report_layout(out / LAYOUT_FILE, previous, current, migrate=not args.no_migrate)
    # Hold the older baseline whenever a migration is still owed — reported but not
    # applied, or impossible because the layout could not be read — so the next
    # scan can still follow the rename.
    pending = report is not None and (
        bool(report.broken) or (not report.changed and bool(report.matches or report.groups))
    )
    _write_baseline(out, previous if pending and previous else current)

    _say(f"\nnext: {BOLD}croqui serve{RESET}" + ("" if args.path == "." else f" {args.path}"))
    return 0


def _baseline(out: Path) -> dict | None:
    """The graph the layout is currently in step with, if croqui knows of one."""
    for name in (PREV_FILE, GRAPH_FILE):
        path = out / name
        if not path.is_file():
            continue
        try:
            return load_graph(path)
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _write_baseline(out: Path, graph: dict) -> None:
    try:
        (out / PREV_FILE).write_text(
            json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        # A read-only .croqui is not a reason to fail a scan; it only costs the
        # next re-scan its ability to follow a rename.
        pass


def _report_layout(layout_path: Path, previous: dict | None, current: dict, *, migrate: bool):
    """Re-point the saved map at the new graph, and say exactly what moved."""
    from .reconcile import reconcile_file

    report = reconcile_file(layout_path, previous, current, migrate=migrate)
    if not report:
        return report

    if report.broken:
        _say(f"  {RED}✗{RESET} {layout_path.name} is unreadable — {report.broken}")
        _say(f"    {DIM}left untouched; the viewer will open read-only rather than overwrite it{RESET}")
        return report

    renames = report.matches + [(f"group {o}", f"group {n}", why) for o, n, why in report.groups]
    if renames and report.changed:
        refs = _plural(report.rewritten, "reference")
        _say(f"  {GREEN}✓{RESET} layout.json follows the rename — {refs} re-pointed")
    elif renames:
        nodes = _plural(len(renames), "node")
        _say(f"  {YELLOW}!{RESET} {nodes} renamed — drop --no-migrate to re-point the saved map")
    for old, new, why in renames[:6]:
        _say(f"    {DIM}· {old} → {new}  ({why}){RESET}")
    if len(renames) > 6:
        _say(f"    {DIM}… and {len(renames) - 6} more{RESET}")

    if report.orphans:
        refs = _plural(len(report.orphans), "layout reference")
        _say(f"  {YELLOW}!{RESET} {refs} with no matching node {DIM}(kept in the file, invisible on the map){RESET}")
        for node_id, pointers in list(report.orphans.items())[:5]:
            _say(f"    {DIM}· {node_id} — {', '.join(sorted(set(pointers)))}{RESET}")
        if len(report.orphans) > 5:
            _say(f"    {DIM}… and {len(report.orphans) - 5} more{RESET}")
    return report


def _report_docs(root: Path, graph: dict, *, seed: bool) -> None:
    """Seed the PRD and UML folders, refresh the generated diagram, and say what happened."""
    if seed:
        from .scaffold import scaffold

        made = scaffold(root, graph)
        for name in made.dirs:
            _say(f"  {GREEN}✓{RESET} criou {name}/ {DIM}(aba de documentação){RESET}")
        for name in made.seeded:
            _say(f"  {GREEN}✓{RESET} escreveu {name}")
        if made.refreshed:
            _say(f"  {GREEN}✓{RESET} {made.refreshed} refeito a partir do código")
        if made.omitted:
            _say(f"    {DIM}· {_plural(made.omitted, 'nó')} fora do desenho — a aba mapa tem todos{RESET}")
        if made.kept:
            _say(f"  {DIM}· {made.kept} é seu (sem a linha croqui:gerado) — não foi tocado{RESET}")
        for problem in made.errors[:3]:
            _say(f"  {YELLOW}!{RESET} não deu para escrever {problem}")

    tally = summarize(collect_docs(root))
    if tally:
        _say(f"  {GREEN}✓{RESET} {tally} {DIM}(abas de documentação){RESET}")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import serve

    root = Path(args.path).resolve()
    graph_path = root / OUT_DIR / GRAPH_FILE
    if not graph_path.is_file():
        _say(f"{RED}no graph yet.{RESET} Run {BOLD}croqui scan {args.path}{RESET} first.")
        return 2
    return serve(root, host=args.host, port=args.port, open_browser=not args.no_open)


def cmd_build(args: argparse.Namespace) -> int:
    from .serve import build_bundle

    root = Path(args.path).resolve()
    graph_path = root / OUT_DIR / GRAPH_FILE
    if not graph_path.is_file():
        _say(f"{RED}no graph yet.{RESET} Run {BOLD}croqui scan {args.path}{RESET} first.")
        return 2
    target = Path(args.output).resolve() if args.output else _out_dir(root) / BUNDLE_FILE
    html = build_bundle(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    size = len(html.encode("utf-8")) / 1024
    _say(f"{GREEN}✓{RESET} {target} {DIM}({size:.0f} KB, self-contained){RESET}")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    from .enrich import enrich

    root = Path(args.path).resolve()
    graph_path = root / OUT_DIR / GRAPH_FILE
    if not graph_path.is_file():
        _say(f"{RED}no graph yet.{RESET} Run {BOLD}croqui scan {args.path}{RESET} first.")
        return 2
    return enrich(root, graph_path, engine=args.engine, dry_run=args.dry_run, limit=args.limit)


def cmd_prd(args: argparse.Namespace) -> int:
    """The PRD, written from the map by whatever model this machine has."""
    from .prd import draft

    root = Path(args.path).resolve()
    graph_path = root / OUT_DIR / GRAPH_FILE
    if not graph_path.is_file():
        _say(f"{RED}no graph yet.{RESET} Run {BOLD}croqui scan {args.path}{RESET} first.")
        return 2
    return draft(
        root, graph_path,
        engine=args.engine, name=args.name, about=args.about or "",
        force=args.force, dry_run=args.dry_run, say=_say,
    )


def cmd_engines(args: argparse.Namespace) -> int:
    """What this machine can talk to — the answer to "which LLM does croqui use"."""
    from .llm import available

    found = available()
    if not found:
        from .llm import _nothing_available

        _say(_nothing_available())
        return 1
    _say(f"{BOLD}modelos conectados{RESET} {DIM}(o primeiro é o que `--engine auto` usa){RESET}")
    for i, engine in enumerate(found):
        mark = f"{GREEN}→{RESET}" if i == 0 else " "
        _say(f"  {mark} {BOLD}{engine.name}{RESET} {DIM}— {engine.how}{RESET}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """The map as text, for a model's context window."""
    from .context import as_json, build, render, render_node, stamp

    root = Path(args.path).resolve()
    graph_path = root / OUT_DIR / GRAPH_FILE
    if not graph_path.is_file():
        _say(f"{RED}no graph yet.{RESET} Run {BOLD}croqui scan {args.path}{RESET} first.")
        return 2
    graph = load_graph(graph_path)

    if args.node:
        text = render_node(graph, root, args.node)
        if text is None:
            _say(f"{RED}no node matching '{args.node}'.{RESET} Try an id, a path, or a table name.")
            return 2
        print(text, end="")
        return 0

    context = build(root, graph, full=args.full)
    if args.json:
        print(as_json(context), end="")
        return 0
    print(render(context, full=args.full), end="")
    _say(f"{DIM}mapa de {stamp(graph_path)} · {BOLD}croqui scan{RESET}{DIM} para atualizar{RESET}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import serve_stdio

    return serve_stdio(Path(args.path).resolve())


def cmd_update(args: argparse.Namespace) -> int:
    from .update import update

    return update(check_only=args.check)


def cmd_stats(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    graph_path = root / OUT_DIR / GRAPH_FILE
    if not graph_path.is_file():
        _say(f"{RED}no graph yet.{RESET}")
        return 2
    data = load_graph(graph_path)
    print(json.dumps({"project": data["project"], "stats": data["stats"], "edges": len(data["edges"])}, indent=2))
    return 0


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="croqui",
        description="Sketch-style interactive maps of an API and the database it touches.",
    )
    parser.add_argument("--version", action="version", version=f"croqui {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    p_scan = subs.add_parser("scan", help="analyse the source and write .croqui/graph.json")
    p_scan.add_argument("path", nargs="?", default=".")
    p_scan.add_argument("--stack", help=f"force an adapter ({', '.join(ADAPTERS)})")
    p_scan.add_argument(
        "--openapi",
        help="path or http(s) URL of the OpenAPI JSON (default: search the usual locations)",
    )
    p_scan.add_argument(
        "--no-docs",
        action="store_true",
        help="do not create prd/ and uml/, nor refresh the generated uml/relacoes.mmd",
    )
    p_scan.add_argument(
        "--no-migrate",
        action="store_true",
        help="do not re-point layout.json at renamed nodes — only report what moved",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_serve = subs.add_parser("serve", help="open the editable map on localhost")
    p_serve.add_argument("path", nargs="?", default=".")
    p_serve.add_argument("--port", type=int, default=7777)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--no-open", action="store_true", help="do not launch a browser")
    p_serve.set_defaults(func=cmd_serve)

    p_build = subs.add_parser("build", help="write a single self-contained HTML file")
    p_build.add_argument("path", nargs="?", default=".")
    p_build.add_argument("-o", "--output", help="target .html path")
    p_build.set_defaults(func=cmd_build)

    p_enrich = subs.add_parser("enrich", help="let an LLM resolve only what static analysis could not")
    p_enrich.add_argument("path", nargs="?", default=".")
    p_enrich.add_argument("--engine", default="auto",
                          help="auto (default), or a name from `croqui engines`")
    p_enrich.add_argument("--dry-run", action="store_true", help="show the worklist and token estimate, call nothing")
    p_enrich.add_argument("--limit", type=int, default=0, help="max nodes to enrich (0 = all)")
    p_enrich.set_defaults(func=cmd_enrich)

    p_prd = subs.add_parser(
        "prd",
        help="write the PRD from the map, using whatever model this machine has",
    )
    p_prd.add_argument("path", nargs="?", default=".")
    p_prd.add_argument("--engine", default="auto",
                       help="auto (default), or a name from `croqui engines`")
    p_prd.add_argument("--name", default=PRD_FILE, help=f"file to write (default: {PRD_FILE})")
    p_prd.add_argument("--about", default="", help="what the PRD should emphasise")
    p_prd.add_argument("--force", action="store_true",
                       help="overwrite a PRD somebody wrote by hand")
    p_prd.add_argument("--dry-run", action="store_true",
                       help="say which engine and which file, call nothing")
    p_prd.set_defaults(func=cmd_prd)

    p_engines = subs.add_parser("engines", help="list the models this machine can talk to")
    p_engines.set_defaults(func=cmd_engines)

    p_context = subs.add_parser(
        "context",
        help="print the map as text for an LLM's context window (see also: croqui mcp)",
    )
    p_context.add_argument("path", nargs="?", default=".")
    p_context.add_argument("--json", action="store_true", help="structured output instead of markdown")
    p_context.add_argument("--full", action="store_true", help="include full endpoint docs and document text")
    p_context.add_argument("--node", help="everything about one node: an id, a path, or a table name")
    p_context.set_defaults(func=cmd_context)

    p_mcp = subs.add_parser(
        "mcp",
        help="run as an MCP server on stdio, so Claude can read the map and write documents",
    )
    p_mcp.add_argument("path", nargs="?", default=".")
    p_mcp.set_defaults(func=cmd_mcp)

    p_update = subs.add_parser("update", help="update croqui itself, wherever it was installed from")
    p_update.add_argument("--check", action="store_true", help="say what would happen, change nothing")
    p_update.set_defaults(func=cmd_update)

    p_stats = subs.add_parser("stats", help="print graph counts as JSON")
    p_stats.add_argument("path", nargs="?", default=".")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _say("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
