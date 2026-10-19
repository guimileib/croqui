"""Static scanner for Flask routes (@app.route / Blueprint / MethodView) + SQLAlchemy.

Also hosts the Python plumbing shared with the Django scanner: a project-wide module
index (``PyIndex``) and ``PyScanner``, which follows calls from a handler to tables
and wires the service / repository layers. Everything is plain ``ast``; nothing is
imported or executed.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from ..model import CALLS, SENDS, Graph
from ..tables import TableRegistry
from ._common import SKIP_DIRS, TableHits, group_of, iter_files, read, wire_tables
from ._common import rel as rel_of

HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
# How far to follow plain function calls from a handler looking for SQL (same as FastAPI).
MAX_CALL_DEPTH = 3

EXTERNAL_MODULES = {
    "httpx": ("HTTP / Webhook", "outbound HTTP"),
    "requests": ("HTTP / Webhook", "outbound HTTP"),
    "aiohttp": ("HTTP / Webhook", "outbound HTTP"),
    "boto3": ("AWS", "AWS SDK"),
    "redis": ("Redis", "cache"),
}

SQL_WORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN|WITH|CALL)\b", re.I)
BARE_QUALIFIED_RE = re.compile(r"^\s*\"?[a-zA-Z_]\w*\"?\.\"?[a-zA-Z_]\w*\"?\s*$")
DYNAMIC_SQL_CALLS = {"execute", "executemany", "text", "raw", "exec_driver_sql"}

FLASK_DEP_RE = re.compile(r"(?<![\w.-])flask(?!\w)", re.I)
FLASK_IMPORT_RE = re.compile(r"^[ \t]*(?:from[ \t]+flask(?:[ \t]+import|\.\w+[ \t]+import)|import[ \t]+flask\b)", re.M)
FLASK_APP_RE = re.compile(r"^[ \t]*\w+[ \t]*(?::[^=\n]+)?=[ \t]*(?:\w+\.)?Flask\(", re.M)
DEP_FILE_RE = re.compile(r"^(requirements.*\.txt|pyproject\.toml|setup\.py|setup\.cfg|Pipfile|environment\.ya?ml)$", re.I)


# ----------------------------------------------------------------- small helpers


def _tail(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _str_list(node: ast.AST | None) -> list[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _looks_like_sql(text: str) -> bool:
    """True for a query or a bare ``schema.table``, not a message that mentions one."""
    if BARE_QUALIFIED_RE.match(text):
        return True
    return bool(SQL_WORD_RE.search(text)) and not text.rstrip().endswith((".", "!", "?"))


def dep_files(root: Path, max_depth: int = 3) -> list[Path]:
    """Dependency manifests near the project root (monorepos keep them one folder down)."""
    out: list[Path] = []
    base_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if len(Path(current).parts) - base_depth >= max_depth:
            dirs[:] = []
        out.extend(Path(current) / f for f in files if DEP_FILE_RE.match(f))
    return out


def _snake(name: str) -> str:
    """Flask-SQLAlchemy's default table name for a model class."""
    return re.sub(r"((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))", r"_\1", name).lower()


def _type_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _type_name(node.slice)
    if isinstance(node, ast.Call):
        base = _type_name(node.func)
        args = [a for a in (_type_name(x) for x in node.args if not isinstance(x, ast.Constant)) if a]
        return f"{base}({', '.join(args)})" if base and args else base
    return None


# ------------------------------------------------------------------ module index


class PyMod:
    """One parsed Python file with its top-level definitions and resolved imports."""

    def __init__(self, dotted: str, path: Path, rel: str, tree: ast.Module, is_pkg: bool) -> None:
        self.dotted = dotted
        self.path = path
        self.rel = rel
        self.tree = tree
        self.is_pkg = is_pkg
        self.imports: dict[str, str] = {}  # local alias -> absolute dotted target
        self.classes: dict[str, ast.ClassDef] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.constants: dict[str, ast.AST] = {}
        self.external: set[str] = set()

    def _absolute(self, module: str | None, level: int) -> str:
        if not level:
            return module or ""
        parts = self.dotted.split(".") if self.is_pkg else self.dotted.split(".")[:-1]
        if level > 1:
            parts = parts[: max(0, len(parts) - (level - 1))]
        base = ".".join(parts)
        return f"{base}.{module}" if module and base else (module or base)

    def scan(self) -> None:
        for node in _toplevel(self.tree.body):
            if isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.constants[node.targets[0].id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.constants[node.target.id] = node.value
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = alias.name
                    else:
                        self.imports[alias.name.split(".")[0]] = alias.name.split(".")[0]
                    if alias.name.split(".")[0] in EXTERNAL_MODULES:
                        self.external.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                base = self._absolute(node.module, node.level)
                for alias in node.names:
                    target = f"{base}.{alias.name}" if base else alias.name
                    self.imports[alias.asname or alias.name] = target
                if not node.level and node.module and node.module.split(".")[0] in EXTERNAL_MODULES:
                    self.external.add(node.module.split(".")[0])


def _toplevel(body: list[ast.stmt]):
    """Top-level statements, looking through ``if`` / ``try`` / ``with`` wrappers."""
    for node in body:
        yield node
        if isinstance(node, ast.If):
            yield from _toplevel(node.body)
            yield from _toplevel(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _toplevel(node.body)
            for handler in node.handlers:
                yield from _toplevel(handler.body)
        elif isinstance(node, ast.With):
            yield from _toplevel(node.body)


class PyIndex:
    """Every Python module of a project, with name/attribute resolution across modules."""

    def __init__(self, root: Path, warnings: list[str] | None = None) -> None:
        self.root = root
        self.mods: dict[str, PyMod] = {}
        self._suffix: dict[str, list[PyMod]] = {}
        for path in iter_files(root, (".py",)):
            rel = rel_of(root, path)
            try:
                tree = ast.parse(read(path), filename=rel)
            except (SyntaxError, ValueError) as exc:
                if warnings is not None:
                    warnings.append(f"skip {rel}: {exc}")
                continue
            dotted = rel[:-3].replace("/", ".")
            is_pkg = dotted.endswith(".__init__") or dotted == "__init__"
            if is_pkg:
                dotted = dotted[: -len("__init__")].rstrip(".")
            mod = PyMod(dotted, path, rel, tree, is_pkg)
            mod.scan()
            self.mods[dotted] = mod
        for mod in self.mods.values():
            parts = mod.dotted.split(".")
            for i in range(len(parts)):
                self._suffix.setdefault(".".join(parts[i:]), []).append(mod)

    def find(self, dotted: str) -> PyMod | None:
        if dotted in self.mods:
            return self.mods[dotted]
        found = self._suffix.get(dotted)
        return found[0] if found and len(found) == 1 else None

    # -- resolution: every result is (module, ast node or None, kind)

    def resolve_target(self, dotted: str, depth: int = 0):
        mod = self.find(dotted)
        if mod:
            return mod, None, "module"
        home, _, attr = dotted.rpartition(".")
        home_mod = self.find(home) if home else None
        return self.resolve_attr(home_mod, attr, depth) if home_mod else None

    def resolve_attr(self, home: PyMod, attr: str, depth: int = 0):
        if depth > 6:
            return None
        if attr in home.classes:
            return home, home.classes[attr], "class"
        if attr in home.functions:
            return home, home.functions[attr], "func"
        if attr in home.constants:
            return home, home.constants[attr], "const"
        if attr in home.imports and home.imports[attr] != f"{home.dotted}.{attr}":
            return self.resolve_target(home.imports[attr], depth + 1)
        sub = self.find(f"{home.dotted}.{attr}") if home.dotted else self.find(attr)
        return (sub, None, "module") if sub else None

    def resolve_name(self, name: str, mod: PyMod):
        if name in mod.classes:
            return mod, mod.classes[name], "class"
        if name in mod.functions:
            return mod, mod.functions[name], "func"
        if name in mod.constants:
            return mod, mod.constants[name], "const"
        target = mod.imports.get(name)
        return self.resolve_target(target) if target else None

    def resolve_expr(self, expr: ast.AST | None, mod: PyMod, depth: int = 0):
        if depth > 6 or expr is None:
            return None
        if isinstance(expr, ast.Name):
            return self.resolve_name(expr.id, mod)
        if isinstance(expr, ast.Attribute):
            base = self.resolve_expr(expr.value, mod, depth + 1)
            if base and base[2] == "module":
                return self.resolve_attr(base[0], expr.attr)
        return None

    def class_of(self, expr: ast.AST, mod: PyMod):
        """The project class an expression names, or the class a module-level singleton is built from."""
        found = self.resolve_expr(expr, mod)
        if not found:
            return None
        if found[2] == "class":
            return found[0], found[1]
        if found[2] == "const" and isinstance(found[1], ast.Call):
            inner = self.resolve_expr(found[1].func, found[0])
            if inner and inner[2] == "class":
                return inner[0], inner[1]
        return None

    def class_chain(self, cls: ast.ClassDef, mod: PyMod):
        """The class and its project bases (depth first), plus the base names we cannot see."""
        chain: list[tuple[PyMod, ast.ClassDef]] = []
        external: set[str] = set()
        seen: set[int] = set()

        def visit(c: ast.ClassDef, m: PyMod) -> None:
            if id(c) in seen:
                return
            seen.add(id(c))
            chain.append((m, c))
            for base in c.bases:
                found = self.resolve_expr(base, m)
                if found and found[2] == "class":
                    visit(found[1], found[0])
                else:
                    tail = _tail(base)
                    if tail:
                        external.add(tail)

        visit(cls, mod)
        return chain, external


# --------------------------------------------------------------- shared scanner


class _Hits(TableHits):
    """Table hits plus the project classes and dynamic-SQL smells met on the way."""

    def __init__(self, registry: TableRegistry) -> None:
        super().__init__(registry)
        self.classes: dict[str, tuple[PyMod, ast.ClassDef]] = {}
        self.dynamic = False


def _layer_of(name: str) -> str | None:
    if name.endswith("Service"):
        return "service"
    if name.endswith("Repository"):
        return "repo"
    return None


class PyScanner:
    """Route -> service -> repository -> table plumbing shared by the Python web stacks."""

    stack = ""

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        self.root = root
        self.graph = graph
        self.registry = registry
        self.openapi_source = openapi
        self.warnings: list[str] = []
        self.index = PyIndex(root, self.warnings)
        self.models: dict[str, dict] = {}  # model class name -> {"table": ...}

    def model_of(self, expr: ast.AST, mod: PyMod) -> str | None:
        """The model class an expression names (``User``, ``models.User``), if it is one of ours."""
        found = self.index.resolve_expr(expr, mod)
        if found and found[2] == "class" and found[1].name in self.models:
            return found[1].name
        return None

    def find_owner(self, expr: ast.AST | None, mod: PyMod, table: dict, depth: int = 0):
        """Key ``(module, variable)`` of the registered object (blueprint, router) an expression names."""
        if expr is None or depth > 5:
            return None
        if isinstance(expr, ast.Name):
            if (mod.dotted, expr.id) in table:
                return mod.dotted, expr.id
            target = mod.imports.get(expr.id)
            return self._owner_by_dotted(target, table, depth) if target else None
        if isinstance(expr, ast.Attribute):
            base = self.index.resolve_expr(expr.value, mod)
            if base and base[2] == "module":
                if (base[0].dotted, expr.attr) in table:
                    return base[0].dotted, expr.attr
                target = base[0].imports.get(expr.attr)
                return self._owner_by_dotted(target, table, depth) if target else None
        return None

    def _owner_by_dotted(self, dotted: str, table: dict, depth: int):
        home, _, attr = dotted.rpartition(".")
        home_mod = self.index.find(home) if home else None
        if home_mod is None:
            return None
        if (home_mod.dotted, attr) in table:
            return home_mod.dotted, attr
        target = home_mod.imports.get(attr)
        if target and depth < 5 and target != dotted:
            return self._owner_by_dotted(target, table, depth + 1)
        return None

    # -- hook: ORM-specific operations in one function body

    def orm_ops(self, func, tree: ast.Module, mod: PyMod, cls: ast.ClassDef | None, ctx: dict | None):
        """(table name, operation, line) tuples the ORM calls in ``tree`` imply."""
        return []

    # ------------------------------------------------------------ table collection

    def collect(self, func, mod: PyMod, cls: ast.ClassDef | None, hits: _Hits, ctx: dict | None = None,
                seen: set | None = None, depth: int = 0) -> None:
        """Tables a function touches, directly or through the helpers it calls."""
        seen = set() if seen is None else seen
        key = (mod.dotted, cls.name if cls else "", func.name, func.lineno)
        if key in seen:
            return
        seen.add(key)
        # Body only: decorators and defaults hold prose, and so does the docstring.
        body = func.body[1:] if ast.get_docstring(func) is not None else func.body
        tree = ast.Module(body=body, type_ignores=[])
        params = {a.arg for a in func.args.posonlyargs + func.args.args + func.args.kwonlyargs}
        self.note_refs([tree, func.args], mod, hits)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if len(node.value) > 12 and _looks_like_sql(node.value):
                    hits.add_sql(node.value, f"{mod.rel}:{getattr(node, 'lineno', func.lineno)}")
            elif isinstance(node, ast.Call):
                self._note_dynamic(node, hits)
                if depth < MAX_CALL_DEPTH:
                    target = self._callee(node.func, mod, cls)
                    if target:
                        self.collect(target[1], target[0], target[2], hits, ctx, seen, depth + 1)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in params:
                found = self.index.resolve_name(node.id, mod)
                if found and found[2] == "const" and ("const", found[0].dotted, node.id) not in seen:
                    seen.add(("const", found[0].dotted, node.id))
                    for sub in ast.walk(found[1]):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            if len(sub.value) > 12 and _looks_like_sql(sub.value):
                                hits.add_sql(sub.value, f"{found[0].rel}:{getattr(sub, 'lineno', 0)}")
        for table, op, line in self.orm_ops(func, tree, mod, cls, ctx):
            hits.add(table, op, f"{mod.rel}:{line}")

    def _callee(self, func_node: ast.AST, mod: PyMod, cls: ast.ClassDef | None):
        """(module, def, class) for a call to a project function or a ``self.method`` of ours."""
        if isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
            if func_node.value.id in ("self", "cls") and cls is not None:
                chain, _ = self.index.class_chain(cls, mod)
                for m, c in chain:
                    for item in c.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == func_node.attr:
                            return m, item, c
                return None
        found = self.index.resolve_expr(func_node, mod)
        if found and found[2] == "func":
            return found[0], found[1], None
        return None

    @staticmethod
    def _note_dynamic(node: ast.Call, hits: _Hits) -> None:
        if _tail(node.func) not in DYNAMIC_SQL_CALLS or not node.args:
            return
        arg = node.args[0]
        if isinstance(arg, (ast.Name, ast.JoinedStr, ast.BinOp)) or (
            isinstance(arg, ast.Call) and _tail(arg.func) == "format"
        ):
            hits.dynamic = True

    def note_refs(self, nodes, mod: PyMod, hits: _Hits) -> None:
        """Record the service / repository classes (or singletons of them) a tree names."""
        for tree in nodes:
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    pass
                elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                    pass
                else:
                    continue
                if _layer_of(_tail(n) or "") is None and not self._maybe_singleton(n, mod):
                    continue
                found = self.index.class_of(n, mod)
                if found and _layer_of(found[1].name):
                    hits.classes.setdefault(found[1].name, found)

    def _maybe_singleton(self, n: ast.AST, mod: PyMod) -> bool:
        found = self.index.resolve_expr(n, mod)
        return bool(found and found[2] == "const" and isinstance(found[1], ast.Call))

    # ------------------------------------------------------------------ route node

    def add_route(self, verb: str, path: str, *, label: str, func, mod: PyMod, cls: ast.ClassDef | None,
                  group: str, extra_refs=(), extra_ops=(), extra_funcs=(), anchor: ast.AST | None = None,
                  ctx: dict | None = None, **meta) -> str:
        """Create a route node and wire it to services, repositories and tables."""
        node_id = f"route:{verb}:{path}"
        hits = _Hits(self.registry)
        if func is not None:
            self.collect(func, mod, cls, hits, ctx)
        for extra_func, extra_mod, extra_cls in extra_funcs:
            self.collect(extra_func, extra_mod, extra_cls, hits, ctx)
        for tree, ref_mod in extra_refs:
            self.note_refs([tree], ref_mod, hits)
        for table, op, evidence in extra_ops:
            hits.add(table, op, evidence)
        services = sorted(n for n in hits.classes if _layer_of(n) == "service")
        repos = sorted(n for n in hits.classes if _layer_of(n) == "repo")
        doc = ast.get_docstring(func) if func is not None else None
        anchor = func if func is not None else anchor
        dynamic_gap = hits.dynamic and not hits and not services and not repos
        self.graph.add_node(
            node_id,
            "route",
            f"{verb} {path}",
            group=group,
            method=verb,
            path=path,
            handler=label,
            summary=doc.strip().splitlines()[0] if doc else None,
            description=doc or None,
            doc_source="docstring" if doc else None,
            source=f"{mod.rel}:{anchor.lineno}" if anchor is not None else None,
            standalone=None if (services or repos) else True,
            unresolved=True if dynamic_gap else None,
            hint="SQL is built dynamically; the table could not be resolved" if dynamic_gap else None,
            **meta,
        )
        evidence = f"{mod.rel}:{anchor.lineno}" if anchor is not None else mod.rel
        for name in services:
            self.graph.add_edge(node_id, f"service:{name}", CALLS, evidence=evidence)
        for name in repos:
            self.graph.add_edge(node_id, f"repo:{name}", CALLS, evidence=evidence)
        wire_tables(self.graph, node_id, hits)
        return node_id

    # ---------------------------------------------------------- services / repos

    def scan_layers(self) -> None:
        """Create service and repository nodes for every *Service / *Repository class."""
        for mod in self.index.mods.values():
            for name, cls in mod.classes.items():
                layer = _layer_of(name)
                if not layer:
                    continue
                node_id = f"{layer}:{name}"
                hits = _Hits(self.registry)
                for item in cls.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.collect(item, mod, cls, hits)
                    else:
                        self.note_refs([item], mod, hits)
                repos = sorted(n for n in hits.classes if _layer_of(n) == "repo" and n != name)
                if layer == "service":
                    missed = any(a.endswith("Repository") for a in mod.imports) and not repos
                    unresolved, hint = (True, "imports a repository that could not be wired") if missed else (None, None)
                else:
                    unresolved, hint = (None, None) if hits else (True, "no table reference resolved")
                self.graph.add_node(
                    node_id,
                    layer,
                    name,
                    group=group_of(mod.rel),
                    source=f"{mod.rel}:{cls.lineno}",
                    description=ast.get_docstring(cls),
                    methods=[
                        f.name
                        for f in cls.body
                        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and not f.name.startswith("_")
                    ] or None,
                    unresolved=unresolved,
                    hint=hint,
                )
                if layer == "service":
                    for repo in repos:
                        self.graph.add_edge(node_id, f"repo:{repo}", CALLS, evidence=f"{mod.rel}:{cls.lineno}")
                wire_tables(self.graph, node_id, hits)
                for ext in sorted(mod.external):
                    label, desc = EXTERNAL_MODULES[ext]
                    ext_id = f"external:{label}"
                    self.graph.add_node(ext_id, "external", label, group="external", via=ext, description=desc)
                    self.graph.add_edge(node_id, ext_id, SENDS, evidence=mod.rel)


# -------------------------------------------------------- Flask-SQLAlchemy models


def _is_db_model_base(base: ast.AST) -> bool:
    """``db.Model`` (but not Django's ``models.Model``)."""
    if isinstance(base, ast.Attribute) and base.attr == "Model":
        return not (isinstance(base.value, ast.Name) and base.value.id in {"models", "pydantic", "orm"})
    return False


def _class_flag(cls: ast.ClassDef, name: str) -> bool:
    for item in cls.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in item.targets):
            return isinstance(item.value, ast.Constant) and item.value.value is True
    return False


def _class_str(cls: ast.ClassDef, name: str) -> str | None:
    for item in cls.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in item.targets):
            return _const_str(item.value)
    return None


def _sa_columns(cls: ast.ClassDef) -> list[dict]:
    """Column name / type / pk / FK of a declarative class body."""
    columns: list[dict] = []
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            attr, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            attr, value = stmt.target.id, stmt.value
        else:
            continue
        if attr.startswith("_") or not isinstance(value, ast.Call) or _tail(value.func) not in ("Column", "mapped_column"):
            continue
        column: dict = {"name": attr, "type": None}
        for arg in value.args:
            if isinstance(arg, ast.Call) and _tail(arg.func) == "ForeignKey":
                target = _const_str(arg.args[0]) if arg.args else None
                if target:
                    column["references"] = target.split(".")[0]
            elif column["type"] is None and not isinstance(arg, ast.Constant):
                column["type"] = _type_name(arg)
        if column["type"] is None and isinstance(stmt, ast.AnnAssign):
            column["type"] = _type_name(stmt.annotation)
        for kw in value.keywords:
            truthy = isinstance(kw.value, ast.Constant) and kw.value.value is True
            if kw.arg == "primary_key" and truthy:
                column["pk"] = True
            elif kw.arg == "nullable" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                column["required"] = True
            elif kw.arg == "index" and truthy:
                column["indexed"] = True
        columns.append(column)
    return columns


def flask_models(index: PyIndex) -> dict[str, dict]:
    """Model class name -> {table, rel, line, columns, own} for declarative / Flask-SQLAlchemy models."""
    cache: dict[tuple[str, str], dict] = {}

    def info_of(mod: PyMod, cls: ast.ClassDef, stack: tuple) -> dict:
        key = (mod.dotted, cls.name)
        if key in cache:
            return cache[key]
        if key in stack:
            return {"model": False, "table": None, "columns": [], "own": False}
        own_table = _class_str(cls, "__tablename__")
        direct = any(_is_db_model_base(b) for b in cls.bases)
        parents = []
        for base in cls.bases:
            found = index.resolve_expr(base, mod)
            if found and found[2] == "class":
                parents.append(info_of(found[0], found[1], stack + (key,)))
        model = direct or bool(own_table) or any(p["model"] for p in parents)
        abstract = _class_flag(cls, "__abstract__")
        columns: list[dict] = []
        for parent in parents:
            if parent["table"] is None:  # mixin or abstract base: its columns become ours
                columns.extend(parent["columns"])
        columns.extend(_sa_columns(cls))
        table, own = None, False
        if model and not abstract:
            if own_table:
                table, own = own_table, True
            else:
                concrete = next((p for p in parents if p["model"] and p["table"]), None)
                table = concrete["table"] if concrete else _snake(cls.name)
                own = concrete is None  # single-table inheritance shares the parent's table
        info = {"model": model and not abstract, "table": table, "columns": columns, "own": own,
                "rel": mod.rel, "line": cls.lineno}
        if abstract:
            info["model"] = model  # still a model base for subclasses
        cache[key] = info
        return info

    out: dict[str, dict] = {}
    for mod in index.mods.values():
        for name, cls in mod.classes.items():
            info = info_of(mod, cls, ())
            if info["model"] and info["table"] and not _class_flag(cls, "__abstract__"):
                out.setdefault(name, info)
    return out


def _association_tables(index: PyIndex) -> list[tuple[str, str, int]]:
    """``db.Table("name", ...)`` association tables: (name, rel, line)."""
    out = []
    for mod in index.mods.values():
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call) and _tail(node.func) == "Table" and node.args:
                name = _const_str(node.args[0])
                if name and any(_tail(a.func) in ("Column", "ForeignKey") for a in node.args[1:] if isinstance(a, ast.Call)):
                    out.append((name, mod.rel, node.lineno))
    return out


# ----------------------------------------------------------------- ORM visitor

_STARTERS = {"select": "SELECT", "insert": "INSERT", "update": "UPDATE", "delete": "DELETE"}
_GETTERS = {"get", "get_or_404", "first_or_404", "one_or_404"}
_STARTER_OWNERS = {"db", "sa", "sqlalchemy"}


class _SaVisitor(ast.NodeVisitor):
    """Operations SQLAlchemy / Flask-SQLAlchemy calls imply, per model, in source order."""

    def __init__(self, scanner: FlaskScanner, mod: PyMod) -> None:
        self.scanner = scanner
        self.mod = mod
        self.ops: list[tuple[str, str, int]] = []
        self.env: dict[str, tuple[str, str]] = {}  # variable -> (model, "new" | "loaded")
        self.handled: set[int] = set()
        self.consumed: set[int] = set()

    def model(self, expr: ast.AST) -> str | None:
        return self.scanner.model_of(expr, self.mod)

    def emit(self, model: str, op: str, node: ast.AST) -> None:
        self.ops.append((self.scanner.models[model]["table"], op, getattr(node, "lineno", 0)))

    def models_in(self, nodes) -> list[tuple[str, ast.AST]]:
        found: list[tuple[str, ast.AST]] = []
        for n in nodes:
            for sub in ast.walk(n):
                if isinstance(sub, (ast.Name, ast.Attribute)):
                    m = self.model(sub)
                    if m:
                        found.append((m, sub))
        return found

    @staticmethod
    def chain(node: ast.AST) -> list[ast.AST]:
        out: list[ast.AST] = []
        cur = node
        while isinstance(cur, (ast.Call, ast.Attribute)):
            out.append(cur)
            cur = cur.func if isinstance(cur, ast.Call) else cur.value
        return out

    def state(self, expr: ast.AST | None):
        """(model, "new" | "loaded") for an expression that yields a model instance."""
        while isinstance(expr, ast.Await):
            expr = expr.value
        if isinstance(expr, ast.Name):
            return self.env.get(expr.id)
        if isinstance(expr, ast.ListComp):
            return self.state(expr.elt)
        if isinstance(expr, ast.Call):
            m = self.model(expr.func)
            if m:
                self.consumed.add(id(expr.func))
                return m, "new"
        if expr is None:
            return None
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Attribute) and sub.attr == "query":
                m = self.model(sub.value)
                if m:
                    return m, "loaded"
            elif isinstance(sub, ast.Call) and sub.args:
                name = _tail(sub.func)
                if name in ("query", "select") or name in _GETTERS:
                    found = self.models_in(sub.args[:1])
                    if found:
                        return found[0][0], "loaded"
        return None

    # -- visiting

    def visit_Assign(self, node: ast.Assign) -> None:
        st = self.state(node.value)
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if st:
                    self.env[target.id] = st
                else:
                    self.env.pop(target.id, None)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(ast.Assign(targets=[node.target], value=node.value, lineno=node.lineno))

    def visit_Call(self, node: ast.Call) -> None:
        if id(node) not in self.handled:
            self._call(node)
        self.generic_visit(node)

    def _call(self, node: ast.Call) -> None:
        chain = self.chain(node)
        names = [n.attr for n in chain if isinstance(n, ast.Attribute)]
        starter = None
        for n in chain:
            if isinstance(n, ast.Attribute) and n.attr == "query":
                m = self.model(n.value)
                if m:
                    starter = ("query", [(m, n.value)])
                    break
            if isinstance(n, ast.Call) and n.args:
                name = _tail(n.func)
                bare = isinstance(n.func, ast.Name)
                owner = n.func.value.id if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) else None
                if name in _STARTERS and (bare or owner in _STARTER_OWNERS):
                    found = self.models_in(n.args)
                    if found:
                        starter = (_STARTERS[name], found)
                        break
                elif name == "query":
                    found = self.models_in(n.args)
                    if found:
                        starter = ("query", found)
                        break
                elif name in _GETTERS:
                    found = self.models_in(n.args[:1])
                    if found:
                        starter = ("get", found)
                        break
        if starter:
            kind, found = starter
            if kind == "query":
                op = "DELETE" if "delete" in names else "UPDATE" if "update" in names else "SELECT"
            elif kind == "get":
                op = "SELECT"
            else:
                op = kind
            for model, ref in found:
                self.emit(model, op, node)
                self.consumed.add(id(ref))
            for n in chain:
                if isinstance(n, ast.Call):
                    self.handled.add(id(n))
            return
        func, name = node.func, _tail(node.func)
        ctor = self.model(func)
        if ctor:
            self.consumed.add(id(func))
        elif isinstance(func, ast.Attribute) and name in ("add", "add_all", "merge") and node.args:
            arg = node.args[0]
            targets = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
            for target in targets:
                st = self.state(target)
                if st:
                    self.emit(st[0], "INSERT" if (st[1] == "new" and name != "merge") else "UPDATE", node)
        elif isinstance(func, ast.Attribute) and name == "delete" and node.args:
            st = self.state(node.args[0])
            if st:
                self.emit(st[0], "DELETE", node)
        elif name in ("bulk_insert_mappings", "bulk_update_mappings") and node.args:
            for model, ref in self.models_in(node.args[:1]):
                self.emit(model, "INSERT" if "insert" in name else "UPDATE", node)
                self.consumed.add(id(ref))
        elif name in ("isinstance", "issubclass"):
            for _, ref in self.models_in(node.args):
                self.consumed.add(id(ref))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and id(node) not in self.consumed:
            m = self.model(node)
            if m:
                self.emit(m, "SELECT", node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and id(node) not in self.consumed and self.model(node):
            self.emit(self.model(node), "SELECT", node)
            return
        self.generic_visit(node)


# ------------------------------------------------------------------ the scanner


class FlaskScanner(PyScanner):
    stack = "python/flask"

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        super().__init__(root, graph, registry, openapi)
        # (module dotted, variable) -> {"kind": app|bp|api, "name", "prefix", "target"}
        self.owners: dict[tuple[str, str], dict] = {}
        self.mounts: dict[tuple[str, str], list[tuple[tuple[str, str] | None, str | None]]] = {}

    # ----------------------------------------------------------------- detection

    @staticmethod
    def detect(root: Path) -> bool:
        """True when this project uses Flask."""
        for path in dep_files(root):
            if FLASK_DEP_RE.search(read(path)):
                return True
        for path in iter_files(root, (".py",)):
            text = read(path)
            if FLASK_IMPORT_RE.search(text) or FLASK_APP_RE.search(text):
                return True
        return False

    @staticmethod
    def register_tables(root: Path, registry: TableRegistry) -> None:
        """Register Flask-SQLAlchemy models (default snake_case table names) and association tables."""
        index = PyIndex(root)
        seen: set[str] = set()
        for name, info in flask_models(index).items():
            if not info["own"] or info["table"] in seen:
                continue
            seen.add(info["table"])
            registry.add(info["table"], schema=None, kind="table", source=f"{info['rel']}:{info['line']}")
            registry.set_columns(info["table"], info["columns"], authoritative=False)
        for table, rel, line in _association_tables(index):
            registry.add(table, schema=None, kind="table", source=f"{rel}:{line}")

    # -------------------------------------------------------------------- models

    def orm_ops(self, func, tree, mod, cls, ctx):
        if not self.models:
            return []
        visitor = _SaVisitor(self, mod)
        visitor.visit(tree)
        return visitor.ops

    # --------------------------------------------------------------------- scan

    def scan(self) -> None:
        self.models = flask_models(self.index)
        self._index_owners()
        self._scan_decorators()
        self._scan_rules()
        self.scan_layers()

    # -- blueprints, apps and their prefixes

    def _index_owners(self) -> None:
        for mod in self.index.mods.values():
            for node in ast.walk(mod.tree):
                if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1):
                    continue
                target, call = node.targets[0], node.value
                if not isinstance(target, ast.Name):
                    continue
                tail = _tail(call.func)
                if tail == "Blueprint":
                    self.owners[(mod.dotted, target.id)] = {
                        "kind": "bp", "name": (_const_str(call.args[0]) if call.args else None) or target.id,
                        "prefix": _const_str(_kwarg(call, "url_prefix")),
                    }
                elif tail == "Flask":
                    self.owners[(mod.dotted, target.id)] = {"kind": "app", "name": target.id, "prefix": None}
                elif tail == "Api":
                    self.owners[(mod.dotted, target.id)] = {
                        "kind": "api", "name": target.id, "prefix": _const_str(_kwarg(call, "prefix")),
                        "target_expr": (call.args[0] if call.args else _kwarg(call, "app")), "mod": mod,
                    }
        for mod in self.index.mods.values():
            for node in ast.walk(mod.tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register_blueprint"
                    and node.args
                ):
                    parent = self.owner_key(node.func.value, mod)
                    child = self.owner_key(node.args[0], mod)
                    if child:
                        prefix = _const_str(_kwarg(node, "url_prefix"))
                        self.mounts.setdefault(child, []).append((parent, prefix))

    def owner_key(self, expr: ast.AST | None, mod: PyMod) -> tuple[str, str] | None:
        return self.find_owner(expr, mod, self.owners)

    def mount_prefixes(self, key: tuple[str, str], seen: tuple = ()) -> list[str]:
        """Every URL prefix an owner is reachable under (a blueprint can be registered twice).

        Flask semantics: a ``url_prefix`` given at registration replaces the blueprint's own;
        a blueprint nested in another blueprint stacks under the parent's prefix.
        """
        if key in seen or key not in self.owners:
            return [""]
        owner = self.owners[key]
        if owner["kind"] == "app":
            return [""]
        if owner["kind"] == "api":
            target = self.owner_key(owner.get("target_expr"), owner["mod"])
            bases = self.mount_prefixes(target, seen + (key,)) if target else [""]
            return [_join(b, owner["prefix"] or "") for b in bases]
        own = owner["prefix"] or ""
        regs = self.mounts.get(key)
        if not regs:
            return [own]
        out: list[str] = []
        for parent, override in regs:
            effective = override if override is not None else own
            bases = self.mount_prefixes(parent, seen + (key,)) if parent else [""]
            for base in bases:
                prefix = _join(base, effective)
                if prefix not in out:
                    out.append(prefix)
        return out

    def _group(self, key: tuple[str, str], mod: PyMod) -> str:
        owner = self.owners.get(key)
        if owner and owner["kind"] == "bp":
            return owner["name"]
        if owner and owner["kind"] == "api":
            target = self.owner_key(owner.get("target_expr"), owner["mod"])
            return self._group(target, mod) if target else mod.dotted.rsplit(".", 1)[-1]
        return mod.dotted.rsplit(".", 1)[-1] or "app"

    # -- routes

    def _scan_decorators(self) -> None:
        for mod in self.index.mods.values():
            for node in ast.walk(mod.tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for deco in node.decorator_list:
                    if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)):
                        continue
                    attr = deco.func.attr
                    if attr != "route" and attr not in HTTP_VERBS:
                        continue
                    key = self.owner_key(deco.func.value, mod)
                    if key is None:
                        continue
                    rule = _const_str(deco.args[0]) if deco.args else _const_str(_kwarg(deco, "rule"))
                    if rule is None:
                        continue
                    methods = [m.upper() for m in _str_list(_kwarg(deco, "methods"))]
                    verbs = [attr.upper()] if attr != "route" else (methods or ["GET"])
                    if isinstance(node, ast.ClassDef):
                        self._class_routes(key, rule, node, mod, restrict=verbs if methods else None)
                    else:
                        for verb in verbs:
                            self._emit(key, rule, verb, node, mod, None, node.name)

    def _scan_rules(self) -> None:
        for mod in self.index.mods.values():
            for node in ast.walk(mod.tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                key = self.owner_key(node.func.value, mod)
                if key is None:
                    continue
                if node.func.attr == "add_url_rule" and node.args:
                    rule = _const_str(node.args[0])
                    view = _kwarg(node, "view_func") or (node.args[2] if len(node.args) > 2 else None)
                    methods = [m.upper() for m in _str_list(_kwarg(node, "methods"))] or None
                    if rule is None or view is None:
                        continue
                    if isinstance(view, ast.Call) and _tail(view.func) == "as_view":
                        found = self.index.resolve_expr(view.func.value, mod) if isinstance(view.func, ast.Attribute) else None
                        if found and found[2] == "class":
                            extra = [(a, mod) for a in list(view.args) + [k.value for k in view.keywords]]
                            self._class_routes(key, rule, found[1], found[0], restrict=methods, extra=extra)
                    else:
                        found = self.index.resolve_expr(view, mod)
                        if found and found[2] == "func":
                            for verb in methods or ["GET"]:
                                self._emit(key, rule, verb, found[1], found[0], None, found[1].name)
                elif node.func.attr == "add_resource" and node.args:
                    found = self.index.resolve_expr(node.args[0], mod)
                    if found and found[2] == "class":
                        for url in node.args[1:]:
                            rule = _const_str(url)
                            if rule:
                                self._class_routes(key, rule, found[1], found[0])

    def _class_routes(self, key, rule: str, cls: ast.ClassDef, cmod: PyMod, restrict=None, extra=()) -> None:
        chain, _ = self.index.class_chain(cls, cmod)
        methods: dict[str, tuple[PyMod, ast.AST, ast.ClassDef]] = {}
        class_refs: list[tuple[ast.AST, PyMod]] = list(extra)
        for m, c in chain:
            for item in c.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(item.name, (m, item, c))
                    if item.name == "__init__":
                        class_refs.append((item, m))
                else:
                    class_refs.append((item, m))
        verbs = [v for v in HTTP_VERBS if v in methods]
        handlers = {v.upper(): methods[v] for v in verbs}
        if not handlers and "dispatch_request" in methods:
            attr = next((a for a in cls.body if isinstance(a, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "methods" for t in a.targets)), None)
            allowed = [m.upper() for m in _str_list(attr.value)] if attr else ["GET"]
            handlers = {v: methods["dispatch_request"] for v in allowed}
        for verb, (hmod, func, hcls) in handlers.items():
            if restrict and verb not in restrict:
                continue
            label = f"{cls.name}.{func.name}" if func.name != "dispatch_request" else f"{cls.name}.dispatch_request"
            self._emit(key, rule, verb, func, hmod, hcls, label, extra_refs=class_refs)

    def _emit(self, key, rule: str, verb: str, func, mod: PyMod, cls, label: str, extra_refs=()) -> None:
        group = self._group(key, mod)
        for prefix in self.mount_prefixes(key):
            self.add_route(verb, _flask_path(_join(prefix, rule)), label=label, func=func, mod=mod, cls=cls,
                           group=group, extra_refs=extra_refs)


def _join(prefix: str, rule: str) -> str:
    """Flask URL joining: ``/api`` + ``/`` -> ``/api/``; ``/api`` + ``''`` -> ``/api``."""
    path = prefix.rstrip("/") + (("/" + rule.lstrip("/")) if rule else "")
    if not path.startswith("/"):
        path = "/" + path
    return path


def _flask_path(rule: str) -> str:
    """``/u/<int:id>`` -> ``/u/{id}``."""
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", rule)
