"""Helpers shared by every stack scanner: file walking, SQL references, table nodes."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..model import READS, WRITES, Graph
from ..tables import QUALIFIED_RE, TableRegistry

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".croqui", "tmp",
    "tests", "test", "__tests__", "dist", "build", "target", ".gradle", ".next", "coverage",
}

TABLE_REF_RE = re.compile(
    r"\b(INSERT\s+INTO|DELETE\s+FROM|UPDATE|FROM|JOIN)\s+"
    r"([\"`]?[a-zA-Z_]\w*[\"`]?(?:\.[\"`]?[a-zA-Z_]\w*[\"`]?)?)",
    re.IGNORECASE,
)
OP_BY_KEYWORD = {
    "INSERT INTO": "INSERT",
    "DELETE FROM": "DELETE",
    "UPDATE": "UPDATE",
    "FROM": "SELECT",
    "JOIN": "SELECT",
}
WRITE_OPS = {"INSERT", "UPDATE", "DELETE"}
HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Source files under ``root`` with one of ``suffixes``, skipping vendored/build dirs."""
    out: list[Path] = []
    # Prune while walking: filtering after rglob would still crawl node_modules.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        out.extend(Path(dirpath, f) for f in filenames if os.path.splitext(f)[1] in suffixes)
    return sorted(out)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def sql_refs(sql: str) -> list[tuple[str, str]]:
    """(table_name, operation) pairs found in a SQL string."""
    out: list[tuple[str, str]] = []
    for keyword, ref in TABLE_REF_RE.findall(sql):
        op = OP_BY_KEYWORD[" ".join(keyword.upper().split())]
        out.append((ref.replace('"', "").replace("`", ""), op))
    for schema, name in QUALIFIED_RE.findall(sql):
        out.append((f"{schema}.{name}", "SELECT"))
    return out


def group_of(relpath: str) -> str:
    parts = Path(relpath).parts
    return parts[-2] if len(parts) > 1 else "app"


def join_path(*parts: str) -> str:
    """Join URL fragments into ``/a/b`` with no doubled or trailing slashes."""
    joined = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return "/" + joined


class TableHits:
    """Tables one node touches: qualified name -> {record, ops, evidence}."""

    def __init__(self, registry: TableRegistry) -> None:
        self.registry = registry
        self.hits: dict[str, dict] = {}

    def add(self, name: str, op: str, evidence: str) -> bool:
        """Record ``op`` on ``name`` if the registry knows it. Unknown names are ignored."""
        rec = self.registry.resolve(name)
        if rec is None:
            return False
        qualified = self.registry.qualified(name)
        hit = self.hits.setdefault(qualified, {"record": rec, "ops": set(), "evidence": []})
        hit["ops"].add(op)
        if evidence not in hit["evidence"]:
            hit["evidence"].append(evidence)
        return True

    def add_sql(self, sql: str, evidence: str) -> None:
        for name, op in sql_refs(sql):
            self.add(name, op, evidence)

    def __bool__(self) -> bool:
        return bool(self.hits)


def add_table_node(graph: Graph, qualified: str, rec: dict) -> str:
    node_id = f"table:{qualified}"
    graph.add_node(
        node_id,
        "table",
        rec["name"],
        group=rec["schema"] or "public",
        schema=rec["schema"],
        object_kind=rec["kind"],
        verified=True,
        defined_in=rec["sources"][:4],
        columns=rec.get("columns") or None,
    )
    return node_id


def wire_tables(graph: Graph, node_id: str, hits: TableHits) -> None:
    """Add a table node and a read/write edge from ``node_id`` for every hit."""
    for qualified, info in hits.hits.items():
        table_id = add_table_node(graph, qualified, info["record"])
        ops = sorted(info["ops"])
        graph.add_edge(
            node_id,
            table_id,
            WRITES if WRITE_OPS & set(ops) else READS,
            ops=ops,
            evidence=info["evidence"][0] if info["evidence"] else None,
        )
