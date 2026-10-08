"""Ground truth for database objects: DDL in the repo or an ORM ``__tablename__``.

Tables not found here are dropped or marked ``verified: false``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

DDL_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|MATERIALIZED\s+VIEW|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?P<schema>[a-zA-Z_]\w*)\.)?(?P<name>[a-zA-Z_]\w*)",
    re.IGNORECASE,
)
TABLENAME_RE = re.compile(r"""__tablename__\s*=\s*["'](\w+)["']""")

# Column parsing, for the panel that opens when you click a table. Everything here
# is best-effort on purpose: a column list that is missing a row is a smaller loss
# than a scan that dies on a dialect it has never seen.
MAX_COLUMNS = 80
# A line inside CREATE TABLE (...) that declares a constraint, not a column.
CONSTRAINT_RE = re.compile(r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT|EXCLUDE|LIKE|INDEX|KEY)\b", re.I)
# Where a column's type stops and its constraints begin.
COL_TAIL_RE = re.compile(
    r"\b(NOT\s+NULL|NULL|PRIMARY\s+KEY|UNIQUE|DEFAULT|REFERENCES|CHECK|GENERATED|COLLATE|CONSTRAINT)\b",
    re.I,
)
COL_PK_RE = re.compile(r"\bPRIMARY\s+KEY\b", re.I)
COL_NOTNULL_RE = re.compile(r"\bNOT\s+NULL\b", re.I)
COL_REFS_RE = re.compile(r"\bREFERENCES\s+(?:[a-zA-Z_]\w*\.)?([a-zA-Z_]\w*)", re.I)
TABLE_PK_RE = re.compile(r"\bPRIMARY\s+KEY\s*\(([^)]*)\)", re.I)
ORM_COLUMN_CALLS = {"Column", "mapped_column", "deferred"}
SCHEMA_KW_RE = re.compile(r"""["']schema["']\s*:\s*["'](\w+)["']""")

# A qualified reference such as ``crm.tb_products`` or ``analytics.tb_reports``.
QUALIFIED_RE = re.compile(r"\b([a-z_][a-z_0-9]*)\.((?:tb|mv|v|fn)_[a-z_0-9]+)\b")

DDL_GLOBS = ("**/*.sql",)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".croqui", "tmp"}

KIND_BY_DDL = {"TABLE": "table", "VIEW": "view", "MATERIALIZED VIEW": "matview"}


class TableRegistry:
    """Case-insensitive lookup of known database objects."""

    def __init__(self) -> None:
        # bare name -> record
        self._by_name: dict[str, dict] = {}

    # ------------------------------------------------------------------ build

    def add(self, name: str, *, schema: str | None, kind: str, source: str) -> dict:
        key = name.lower()
        rec = self._by_name.get(key)
        if rec is None:
            rec = {"name": name, "schema": schema, "kind": kind, "sources": [], "columns": []}
            self._by_name[key] = rec
        rec.setdefault("columns", [])
        # DDL knows the schema better than a bare ORM ``__tablename__`` does.
        if schema and not rec["schema"]:
            rec["schema"] = schema
        if kind != "table" and rec["kind"] == "table":
            rec["kind"] = kind
        if source not in rec["sources"]:
            rec["sources"].append(source)
        return rec

    def set_columns(self, name: str, columns: list[dict], *, authoritative: bool) -> None:
        """Attach a column list; DDL wins over an ORM model."""
        rec = self._by_name.get(name.lower())
        if rec is None or not columns:
            return
        if rec["columns"] and not (authoritative and not rec.get("columns_from_ddl")):
            return
        rec["columns"] = columns[:MAX_COLUMNS]
        rec["columns_from_ddl"] = authoritative

    # ------------------------------------------------------------------ query

    def resolve(self, name: str) -> dict | None:
        """Look up a bare or qualified name. Returns None if unknown."""
        bare = name.split(".")[-1].strip('"').lower()
        return self._by_name.get(bare)

    def qualified(self, name: str) -> str:
        rec = self.resolve(name)
        if rec is None:
            return name.split(".")[-1]
        return f"{rec['schema']}.{rec['name']}" if rec["schema"] else rec["name"]

    def __len__(self) -> int:
        return len(self._by_name)

    def all(self) -> list[dict]:
        return sorted(self._by_name.values(), key=lambda r: (r["schema"] or "", r["name"]))


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if path.suffix not in suffixes or not path.is_file():
            continue
        # Relative to the root, never absolute: SKIP_DIRS names directories *inside*
        # a project, and matching it against the whole path made every ancestor
        # count too. A checkout under ~/tmp or any path with a `venv` component in
        # it lost its entire database column, silently and with no way to tell.
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        out.append(path)
    return out


def _paren_block(text: str, start: int) -> str | None:
    """The `( … )` that follows a CREATE TABLE, with nesting and quotes respected."""
    i = text.find("(", start)
    if i < 0:
        return None
    # `CREATE TABLE x AS SELECT …` has no column list; neither does a bare view.
    head = text[start:i]
    if ";" in head or re.search(r"\bAS\b", head, re.I):
        return None
    depth, quote, j = 0, "", i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
        j += 1
    return None


def _split_top_level(body: str) -> list[str]:
    """Split on commas that are not inside `numeric(12,2)` or a quoted string."""
    parts, depth, quote, current = [], 0, "", []
    for ch in body:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _ddl_columns(body: str) -> list[dict]:
    """Column name, type and the three flags worth showing, from a CREATE TABLE."""
    columns: list[dict] = []
    table_pk: set[str] = set()
    for part in _split_top_level(body):
        flat = " ".join(part.split())
        if CONSTRAINT_RE.match(flat):
            found = TABLE_PK_RE.search(flat)
            if found:
                table_pk.update(c.strip().strip('"').lower() for c in found.group(1).split(","))
            continue
        name, _, rest = flat.partition(" ")
        name = name.strip('"').strip("`").strip("[]")
        if not name or not re.match(r"^[A-Za-z_]\w*$", name):
            continue
        cut = COL_TAIL_RE.search(rest)
        column = {"name": name, "type": (rest[: cut.start()] if cut else rest).strip() or None}
        if COL_PK_RE.search(rest):
            column["pk"] = True
        if COL_NOTNULL_RE.search(rest):
            column["required"] = True
        refs = COL_REFS_RE.search(rest)
        if refs:
            column["references"] = refs.group(1)
        columns.append(column)
    for column in columns:
        if column["name"].lower() in table_pk:
            column["pk"] = True
    return columns


def _ast_type(node: ast.AST) -> str | None:
    """`String(50)`, `Integer`, `sa.Numeric` — as written, not as resolved."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):  # Mapped[int]
        return _ast_type(node.slice)
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Call):
        base = _ast_type(node.func)
        args = [a for a in (_ast_type(x) for x in node.args) if a]
        return f"{base}({', '.join(args)})" if base and args else base
    return None


def _orm_columns(text: str) -> dict[str, list[dict]]:
    """Map ``__tablename__`` to its columns, parsed from the class body with ``ast``."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    found: dict[str, list[dict]] = {}
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        table, columns = None, []
        for stmt in cls.body:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else (
                [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
            )
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            attr, value = targets[0].id, stmt.value
            if attr == "__tablename__" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                table = value.value
                continue
            if attr.startswith("_") or not isinstance(value, ast.Call):
                continue
            if _ast_type(value.func) not in ORM_COLUMN_CALLS:
                continue
            column = {"name": attr, "type": None}
            for arg in value.args:
                rendered = _ast_type(arg)
                if isinstance(arg, ast.Call) and _ast_type(arg.func) == "ForeignKey":
                    target = arg.args[0].value if arg.args and isinstance(arg.args[0], ast.Constant) else ""
                    column["references"] = str(target).split(".")[0] or None
                elif column["type"] is None and rendered and not isinstance(arg, ast.Constant):
                    column["type"] = rendered
            if column["type"] is None and isinstance(stmt, ast.AnnAssign):
                column["type"] = _ast_type(stmt.annotation)
            for kw in value.keywords:
                truthy = isinstance(kw.value, ast.Constant) and kw.value.value is True
                falsy = isinstance(kw.value, ast.Constant) and kw.value.value is False
                if kw.arg == "primary_key" and truthy:
                    column["pk"] = True
                elif kw.arg == "nullable" and falsy:
                    column["required"] = True
                elif kw.arg == "index" and truthy:
                    column["indexed"] = True
            columns.append(column)
        if table and columns:
            found[table] = columns
    return found


def build_registry(root: Path, *, code_suffixes: tuple[str, ...] = (".py",)) -> TableRegistry:
    """Scan a project for every database object it defines or references."""
    registry = TableRegistry()

    # 1. DDL is the strongest signal: it carries schema and object kind.
    for sql_file in _iter_files(root, (".sql",)):
        rel = str(sql_file.relative_to(root))
        text = sql_file.read_text(encoding="utf-8", errors="replace")
        for match in DDL_RE.finditer(text):
            kind = KIND_BY_DDL.get(" ".join(match.group(1).upper().split()), "table")
            registry.add(match.group("name"), schema=match.group("schema"), kind=kind, source=rel)
            body = _paren_block(text, match.end()) if kind == "table" else None
            if body:
                registry.set_columns(match.group("name"), _ddl_columns(body), authoritative=True)

    # 2. ORM models. The schema usually sits in __table_args__ a few lines away.
    for code_file in _iter_files(root, code_suffixes):
        rel = str(code_file.relative_to(root))
        text = code_file.read_text(encoding="utf-8", errors="replace")
        if "__tablename__" not in text:
            continue
        by_table = _orm_columns(text)
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            m = TABLENAME_RE.search(line)
            if not m:
                continue
            window = "\n".join(lines[max(0, idx - 12) : idx + 12])
            schema_m = SCHEMA_KW_RE.search(window)
            registry.add(
                m.group(1),
                schema=schema_m.group(1) if schema_m else None,
                kind="table",
                source=f"{rel}:{idx + 1}",
            )
            registry.set_columns(m.group(1), by_table.get(m.group(1), []), authoritative=False)

    # 3. Qualified references in code. These do not *prove* existence, but a
    #    schema-qualified tb_/mv_ token is specific enough to trust as a
    #    reference, and it back-fills the schema for models that omitted it.
    for code_file in _iter_files(root, code_suffixes):
        rel = str(code_file.relative_to(root))
        text = code_file.read_text(encoding="utf-8", errors="replace")
        for schema, name in QUALIFIED_RE.findall(text):
            if name.startswith("fn_"):
                continue  # functions are not nodes in the map
            registry.add(name, schema=schema, kind="table", source=rel)

    return registry
