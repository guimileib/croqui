"""Ground truth for database objects: DDL in the repo or an ORM ``__tablename__``.

Tables not found here are dropped or marked ``verified: false``.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

DDL_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|MATERIALIZED\s+VIEW|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:[`\"\[]?(?P<schema>[a-zA-Z_]\w*)[`\"\]]?\.)?[`\"\[]?(?P<name>[a-zA-Z_]\w*)",
    re.IGNORECASE,
)
# Migrations and dumps (pg_dump, Flyway, a docker init script) declare keys after
# the table: `ALTER TABLE ONLY orders ADD CONSTRAINT … FOREIGN KEY … REFERENCES …;`.
ALTER_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    r"(?P<table>(?:[`\"\[]?[a-zA-Z_]\w*[`\"\]]?\.)?[`\"\[]?[a-zA-Z_]\w*[`\"\]]?)(?P<body>[^;]*)",
    re.IGNORECASE,
)
ALTER_ADD_RE = re.compile(r"^ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?", re.I)
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
# The referenced table keeps its schema when the DDL writes one: `REFERENCES users_v3.person`.
COL_REFS_RE = re.compile(r"\bREFERENCES\s+((?:[a-zA-Z_]\w*\.)?[a-zA-Z_]\w*)", re.I)
TABLE_PK_RE = re.compile(r"\bPRIMARY\s+KEY\s*\(([^)]*)\)", re.I)
TABLE_FK_RE = re.compile(
    r"\bFOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+((?:[`\"]?[a-zA-Z_]\w*[`\"]?\.)?[`\"]?[a-zA-Z_]\w*)", re.I
)
ORM_COLUMN_CALLS = {"Column", "mapped_column", "deferred"}
SCHEMA_KW_RE = re.compile(r"""["']schema["']\s*:\s*["'](\w+)["']""")

# A qualified reference such as ``crm.tb_products`` or ``analytics.tb_reports``.
QUALIFIED_RE = re.compile(r"\b([a-z_][a-z_0-9]*)\.((?:tb|mv|v|fn)_[a-z_0-9]+)\b")

DDL_GLOBS = ("**/*.sql",)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".croqui", "tmp", "dist", "build", "target", ".gradle", ".next"}

KIND_BY_DDL = {"TABLE": "table", "VIEW": "view", "MATERIALIZED VIEW": "matview"}


def _key(name: str, schema: str | None) -> str:
    return f"{schema}.{name}".lower() if schema else name.lower()


class TableRegistry:
    """Case-insensitive lookup of known database objects, one per ``schema.name``.

    The same name in two schemas (``access_v3.module``, ``internal_v3.module``) is two
    objects. A name without a schema stands for the one object of that name, if there
    is exactly one.
    """

    def __init__(self) -> None:
        # "schema.name" (bare "name" when no schema is known) -> record
        self._by_key: dict[str, dict] = {}
        # bare name -> every record with that name, whatever the schema
        self._by_bare: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ build

    def add(self, name: str, *, schema: str | None, kind: str, source: str) -> dict:
        same = self._by_bare.setdefault(name.lower(), [])
        if schema:
            rec = self._by_key.get(_key(name, schema))
            if rec is None and len(same) == 1 and not same[0]["schema"]:
                # DDL knows the schema better than a bare ORM ``__tablename__`` does.
                rec = self._by_key.pop(_key(name, None))
                rec["schema"] = schema
                self._by_key[_key(name, schema)] = rec
        else:
            rec = same[0] if len(same) == 1 else self._by_key.get(_key(name, None))
        if rec is None:
            rec = {"name": name, "schema": schema, "kind": kind, "sources": [], "columns": []}
            self._by_key[_key(name, schema)] = rec
            same.append(rec)
        rec.setdefault("columns", [])
        if kind != "table" and rec["kind"] == "table":
            rec["kind"] = kind
        if source not in rec["sources"]:
            rec["sources"].append(source)
        return rec

    def set_columns(self, name: str, columns: list[dict], *, authoritative: bool) -> None:
        """Attach a column list; DDL wins over an ORM model. ``name`` may be ``schema.name``."""
        rec = self.resolve(name)
        if rec is None or not columns:
            return
        if rec["columns"] and not (authoritative and not rec.get("columns_from_ddl")):
            return
        rec["columns"] = columns[:MAX_COLUMNS]
        rec["columns_from_ddl"] = authoritative

    # ------------------------------------------------------------------ query

    def resolve(self, name: str) -> dict | None:
        """Look up a bare or qualified name. Returns None if unknown or ambiguous.

        ``schema.name`` hits that schema; failing that, it falls back to the only object
        of that name. A bare name is the only object of that name, else the one declared
        without a schema, else ``public``'s.
        """
        parts = [p.strip().strip('"').lower() for p in name.split(".")]
        bare, schema = parts[-1], (parts[-2] if len(parts) > 1 else None)
        if schema and _key(bare, schema) in self._by_key:
            return self._by_key[_key(bare, schema)]
        same = self._by_bare.get(bare, [])
        if len(same) == 1:
            return same[0]
        if schema:
            return None
        return self._by_key.get(bare) or self._by_key.get(_key(bare, "public"))

    def qualified(self, name: str) -> str:
        rec = self.resolve(name)
        if rec is None:
            return name.split(".")[-1]
        return f"{rec['schema']}.{rec['name']}" if rec["schema"] else rec["name"]

    def __len__(self) -> int:
        return len(self._by_key)

    def all(self) -> list[dict]:
        return sorted(self._by_key.values(), key=lambda r: (r["schema"] or "", r["name"]))


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    # SKIP_DIRS names directories *inside* the project; pruning while walking keeps
    # an ancestor such as ~/tmp from hiding the whole tree, and never enters
    # node_modules at all.
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        out.extend(Path(dirpath, f) for f in filenames if os.path.splitext(f)[1] in suffixes)
    return sorted(out)


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
    table_fk: dict[str, str] = {}
    for part in _split_top_level(body):
        flat = " ".join(part.split())
        if CONSTRAINT_RE.match(flat):
            found = TABLE_PK_RE.search(flat)
            if found:
                table_pk.update(c.strip().strip('"').lower() for c in found.group(1).split(","))
            # `FOREIGN KEY (a) REFERENCES t(id)`: the DER draws its line from this.
            fk = TABLE_FK_RE.search(flat)
            if fk:
                for col in fk.group(1).split(","):
                    table_fk[col.strip().strip('"`').lower()] = fk.group(2).replace('"', "").replace("`", "")
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
        if column["name"].lower() in table_fk and not column.get("references"):
            column["references"] = table_fk[column["name"].lower()]
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


def _call_column(name: str, call: ast.Call) -> dict:
    """One column from `Column(Integer, ForeignKey("t.id"), nullable=False)`."""
    column = {"name": name, "type": None}
    for arg in call.args:
        rendered = _ast_type(arg)
        if isinstance(arg, ast.Call) and _ast_type(arg.func) == "ForeignKey":
            target = arg.args[0].value if arg.args and isinstance(arg.args[0], ast.Constant) else ""
            # `ForeignKey("schema.table.column")`: everything but the column.
            column["references"] = str(target).rsplit(".", 1)[0] or None
        elif column["type"] is None and rendered and not isinstance(arg, ast.Constant):
            column["type"] = rendered
    for kw in call.keywords:
        truthy = isinstance(kw.value, ast.Constant) and kw.value.value is True
        falsy = isinstance(kw.value, ast.Constant) and kw.value.value is False
        if kw.arg == "primary_key" and truthy:
            column["pk"] = True
        elif kw.arg == "nullable" and falsy:
            column["required"] = True
        elif kw.arg == "index" and truthy:
            column["indexed"] = True
    return column


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
            column = _call_column(attr, value)
            if column["type"] is None and isinstance(stmt, ast.AnnAssign):
                column["type"] = _ast_type(stmt.annotation)
            columns.append(column)
        if table and columns:
            found[table] = columns
    return found


def _strs(node: ast.AST | None) -> list[str]:
    """`"a"` or `["a", "b"]` as a list of strings."""
    items = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
    return [i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)]


def _alembic_migration(text: str, rel: str, registry: TableRegistry) -> None:
    """`op.create_table`, `op.add_column` and `op.create_foreign_key` in an Alembic migration."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "op"
    ]
    # Every table first: a later revision adds a column or a key to an earlier one.
    for call in sorted(calls, key=lambda c: c.func.attr != "create_table"):
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        names = _strs(call.args[0]) if call.args else []
        if call.func.attr == "create_table" and names:
            schema = (_strs(kwargs.get("schema")) or [None])[0]
            columns, by_name = [], {}
            for arg in call.args[1:]:
                kind = _ast_type(arg.func) if isinstance(arg, ast.Call) else None
                if kind == "Column" and arg.args and _strs(arg.args[0]):
                    column = _call_column(_strs(arg.args[0])[0], arg)
                    columns.append(column)
                    by_name[column["name"]] = column
                elif kind == "PrimaryKeyConstraint":
                    for name in (s for a in arg.args for s in _strs(a)):
                        by_name.get(name, {})["pk"] = True
                elif kind == "ForeignKeyConstraint" and len(arg.args) >= 2:
                    for local, remote in zip(_strs(arg.args[0]), _strs(arg.args[1])):
                        by_name.get(local, {}).setdefault("references", remote.rsplit(".", 1)[0])
            registry.add(names[0], schema=schema, kind="table", source=f"{rel}:{call.lineno}")
            registry.set_columns(_key(names[0], schema), columns, authoritative=False)
            continue
        if call.func.attr == "add_column" and len(call.args) > 1 and isinstance(call.args[1], ast.Call):
            rec = registry.resolve(names[0]) if names else None
            column = call.args[1]
            name = (_strs(column.args[0]) if column.args else [])[:1]
            if rec and name and all(c["name"] != name[0] for c in rec["columns"]):
                rec["columns"].append(_call_column(name[0], column))
        elif call.func.attr == "create_foreign_key" and len(call.args) >= 4:
            # op.create_foreign_key(name, source, referent, local_cols, remote_cols)
            source, target = _strs(call.args[1]), _strs(call.args[2])
            rec = registry.resolve(source[0]) if source and target else None
            for col in rec["columns"] if rec else []:
                if col["name"] in _strs(call.args[3]):
                    col.setdefault("references", target[0])


def _alter_tables(text: str, registry: TableRegistry) -> None:
    """Columns and foreign keys an `ALTER TABLE` adds to a table already declared."""
    for match in ALTER_RE.finditer(text):
        rec = registry.resolve(re.sub(r'[`"\[\]]', "", match.group("table")))
        if rec is None:
            continue
        by_name = {c["name"].lower(): c for c in rec["columns"]}
        for part in _split_top_level(match.group("body")):
            flat = " ".join(part.split())
            fk = TABLE_FK_RE.search(flat)
            if fk:
                target = fk.group(2).replace('"', "").replace("`", "")
                for col in fk.group(1).split(","):
                    by_name.get(col.strip().strip('"`').lower(), {}).setdefault("references", target)
                continue
            pk = TABLE_PK_RE.search(flat)
            for col in pk.group(1).split(",") if pk else []:
                by_name.get(col.strip().strip('"`').lower(), {})["pk"] = True
            added = ALTER_ADD_RE.match(flat)
            for column in _ddl_columns(flat[added.end():]) if added else []:
                if column["name"].lower() not in by_name and len(rec["columns"]) < MAX_COLUMNS:
                    rec["columns"].append(column)
                    by_name[column["name"].lower()] = column


def build_registry(root: Path, *, code_suffixes: tuple[str, ...] = (".py",)) -> TableRegistry:
    """Scan a project for every database object it defines or references."""
    registry = TableRegistry()

    # 1. DDL is the strongest signal: it carries schema and object kind.
    ddl: list[str] = []
    for sql_file in _iter_files(root, (".sql",)):
        rel = sql_file.relative_to(root).as_posix()
        text = sql_file.read_text(encoding="utf-8", errors="replace")
        ddl.append(text)
        for match in DDL_RE.finditer(text):
            kind = KIND_BY_DDL.get(" ".join(match.group(1).upper().split()), "table")
            registry.add(match.group("name"), schema=match.group("schema"), kind=kind, source=rel)
            body = _paren_block(text, match.end()) if kind == "table" else None
            if body:
                registry.set_columns(
                    _key(match.group("name"), match.group("schema")), _ddl_columns(body), authoritative=True
                )

    # 2. ORM models. The schema usually sits in __table_args__ a few lines away.
    for code_file in _iter_files(root, code_suffixes):
        rel = code_file.relative_to(root).as_posix()
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
            schema = schema_m.group(1) if schema_m else None
            registry.add(m.group(1), schema=schema, kind="table", source=f"{rel}:{idx + 1}")
            registry.set_columns(_key(m.group(1), schema), by_table.get(m.group(1), []), authoritative=False)

    # 2b. Migrations: Alembic revisions, then what any .sql alters after creating.
    for code_file in _iter_files(root, tuple(s for s in code_suffixes if s == ".py")):
        text = code_file.read_text(encoding="utf-8", errors="replace")
        if "op.create_table" in text or "op.add_column" in text or "op.create_foreign_key" in text:
            _alembic_migration(text, code_file.relative_to(root).as_posix(), registry)
    for text in ddl:
        _alter_tables(text, registry)

    # 3. Qualified references in code. These do not *prove* existence, but a
    #    schema-qualified tb_/mv_ token is specific enough to trust as a
    #    reference, and it back-fills the schema for models that omitted it.
    for code_file in _iter_files(root, code_suffixes):
        rel = code_file.relative_to(root).as_posix()
        text = code_file.read_text(encoding="utf-8", errors="replace")
        for schema, name in QUALIFIED_RE.findall(text):
            if name.startswith("fn_"):
                continue  # functions are not nodes in the map
            registry.add(name, schema=schema, kind="table", source=rel)

    return registry
