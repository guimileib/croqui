"""Static scanner for FastAPI + SQLAlchemy projects.

Traces route -> DI service -> repository -> table, or handler -> inline SQL /
helper -> table, without running the app or an LLM. Whatever it cannot prove is
flagged ``unresolved`` for the optional LLM pass.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from ..model import CALLS, READS, SENDS, WRITES, Graph
from ..tables import QUALIFIED_RE, TableRegistry

HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".croqui", "tmp", "tests", "test"}

TABLE_REF_RE = re.compile(
    r"\b(INSERT\s+INTO|DELETE\s+FROM|UPDATE|FROM|JOIN)\s+"
    r"(\"?[a-zA-Z_]\w*\"?(?:\.\"?[a-zA-Z_]\w*\"?)?)",
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
# How far to follow plain function calls from a handler looking for SQL. Deep
# enough for handler -> service function -> query helper, shallow enough that a
# utility everyone calls does not drag every table into every route.
MAX_CALL_DEPTH = 3

# ORM call name -> SQL operation it implies.
ORM_OPS = {
    "select": "SELECT",
    "insert": "INSERT",
    "update": "UPDATE",
    "sa_update": "UPDATE",
    "delete": "DELETE",
    "sa_delete": "DELETE",
    "add": "INSERT",
    "add_all": "INSERT",
    "merge": "UPDATE",
    "bulk_insert_mappings": "INSERT",
    "bulk_update_mappings": "UPDATE",
}

EXTERNAL_MODULES = {
    "httpx": ("HTTP / Webhook", "outbound HTTP"),
    "requests": ("HTTP / Webhook", "outbound HTTP"),
    "aiohttp": ("HTTP / Webhook", "outbound HTTP"),
    "boto3": ("AWS", "AWS SDK"),
    "redis": ("Redis", "cache"),
}


def _sql_refs(sql: str) -> list[tuple[str, str]]:
    """Return (table_name, operation) pairs found in a SQL string."""
    out: list[tuple[str, str]] = []
    for keyword, ref in TABLE_REF_RE.findall(sql):
        op = OP_BY_KEYWORD[" ".join(keyword.upper().split())]
        out.append((ref.replace('"', ""), op))
    for schema, name in QUALIFIED_RE.findall(sql):
        out.append((f"{schema}.{name}", "SELECT"))
    return out


class _Module:
    """One parsed Python file plus its resolved import aliases."""

    def __init__(self, dotted: str, path: Path, rel: str, tree: ast.Module) -> None:
        self.dotted = dotted
        self.path = path
        self.rel = rel
        self.tree = tree
        self.imports: dict[str, str] = {}  # local alias -> dotted target
        self.classes: dict[str, ast.ClassDef] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        # Module-level `NAME = <expr>`: where SQL often lives, away from the
        # function that runs it (`_BASE_QUERY = """…"""`, then `db.fetch(_BASE_QUERY)`).
        self.constants: dict[str, ast.AST] = {}
        self.external: set[str] = set()

    def scan_toplevel(self) -> None:
        for node in self.tree.body:
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
                    self.imports[alias.asname or alias.name.split(".")[0]] = alias.name
                    root = alias.name.split(".")[0]
                    if root in EXTERNAL_MODULES:
                        self.external.add(root)
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                root = node.module.split(".")[0]
                if root in EXTERNAL_MODULES:
                    self.external.add(root)


class FastApiScanner:
    stack = "python/fastapi"

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        self.root = root
        self.graph = graph
        self.registry = registry
        self.openapi_source = openapi
        self.modules: dict[str, _Module] = {}
        self.class_home: dict[str, _Module] = {}  # class name -> module that defines it
        self.factories: dict[str, dict] = {}  # DI factory name -> {returns, deps, module}
        self.openapi: dict[tuple[str, str], dict] = {}
        self.warnings: list[str] = []

    # ------------------------------------------------------------------- entry

    def scan(self) -> None:
        self._index_modules()
        self._load_openapi()
        self._index_factories()
        prefixes = self._router_prefixes()
        self._scan_routes(prefixes)
        self._scan_services()
        self._scan_repositories()

    # ------------------------------------------------------------------ indexes

    def _index_modules(self) -> None:
        for path in sorted(self.root.rglob("*.py")):
            if any(part in SKIP_DIRS for part in path.relative_to(self.root).parts):
                continue
            rel = str(path.relative_to(self.root))
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
            except SyntaxError as exc:
                self.warnings.append(f"skip {rel}: {exc}")
                continue
            dotted = rel[:-3].replace("/", ".").removesuffix(".__init__")
            module = _Module(dotted, path, rel, tree)
            module.scan_toplevel()
            self.modules[dotted] = module
            for name in module.classes:
                self.class_home.setdefault(name, module)

    def _load_openapi(self) -> None:
        """Load the spec — explicit path/URL first, then the usual locations."""
        candidates = [self.openapi_source] if self.openapi_source else [
            "contracts/openapi.json",
            "openapi.json",
            "docs/openapi.json",
            "openapi/openapi.json",
            "api/openapi.json",
            "static/openapi.json",
            "swagger.json",
        ]
        for candidate in candidates:
            spec = self._read_spec(candidate)
            if spec is None:
                continue
            for route_path, methods in (spec.get("paths") or {}).items():
                if not isinstance(methods, dict):
                    continue
                for method, op in methods.items():
                    if method.lower() in HTTP_VERBS and isinstance(op, dict):
                        self.openapi[(method.upper(), route_path)] = op
            self.graph.project["openapi"] = candidate
            return
        if self.openapi_source:
            self.warnings.append(f"could not read spec: {self.openapi_source}")

    def _read_spec(self, candidate: str) -> dict | None:
        try:
            if candidate.startswith(("http://", "https://")):
                # A running app can serve its own spec (FastAPI, Flask-smorest, …).
                from urllib.request import urlopen

                with urlopen(candidate, timeout=15) as response:  # noqa: S310 - user-supplied URL
                    spec = json.loads(response.read().decode("utf-8"))
            else:
                path = Path(candidate)
                if not path.is_absolute():
                    path = self.root / candidate
                if not path.is_file():
                    return None
                spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.warnings.append(f"spec {candidate}: {exc}")
            return None
        return spec if isinstance(spec, dict) else None

    def _index_factories(self) -> None:
        """Map DI factory functions to the class they construct and the deps they take."""
        for module in self.modules.values():
            for name, func in module.functions.items():
                returns: str | None = None
                for node in ast.walk(func):
                    if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
                        callee = node.value.func
                        if isinstance(callee, ast.Name):
                            returns = callee.id
                        elif isinstance(callee, ast.Attribute):
                            returns = callee.attr
                        break
                if not returns:
                    continue
                deps = [d for d in self._depends_of(func)]
                self.factories[name] = {"returns": returns, "deps": deps, "module": module}

    @staticmethod
    def _depends_of(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        """Names passed to ``Depends(...)`` anywhere in a signature."""
        found: list[str] = []
        for node in ast.walk(func.args):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Depends"
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                found.append(node.args[0].id)
        return found

    # ------------------------------------------------------------------ routing

    def _router_prefixes(self) -> dict[str, str]:
        """Resolve each controller module's full URL prefix (``APIRouter`` + mount prefix)."""
        local: dict[str, str] = {}
        for dotted, module in self.modules.items():
            for node in ast.walk(module.tree):
                # APIRouter carries a prefix; the FastAPI app itself mounts at root.
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("APIRouter", "FastAPI")
                ):
                    local[dotted] = _kwarg_str(node, "prefix") or ""
                    tags = _kwarg_list(node, "tags")
                    if tags:
                        module.tags = tags  # type: ignore[attr-defined]

        mounted: dict[str, str] = {}
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                if not (isinstance(node, ast.Call) and _callee_name(node.func) == "include_router"):
                    continue
                prefix = _kwarg_str(node, "prefix") or ""
                if not node.args:
                    continue
                target = node.args[0]
                alias = None
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    alias = target.value.id  # shipment.router
                elif isinstance(target, ast.Name):
                    alias = target.id  # auth_router
                if not alias:
                    continue
                dotted = module.imports.get(alias)
                if dotted and dotted in self.modules:
                    mounted[dotted] = prefix
                else:
                    # ``from app.controllers import shipment`` style
                    for cand in self.modules:
                        if cand.endswith(f".{alias}"):
                            mounted[cand] = prefix
                            break

        return {dotted: mounted.get(dotted, "") + prefix for dotted, prefix in local.items()}

    def _scan_routes(self, prefixes: dict[str, str]) -> None:
        for dotted, prefix in prefixes.items():
            module = self.modules[dotted]
            group = getattr(module, "tags", None)
            group = group[0] if group else dotted.rsplit(".", 1)[-1]
            for node in ast.walk(module.tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for deco in node.decorator_list:
                    verb, sub_path, meta = _route_decorator(deco)
                    if not verb:
                        continue
                    full = (prefix + sub_path) or "/"
                    self._add_route(module, node, verb, full, group, meta)

    def _add_route(self, module: _Module, func, verb: str, path: str, group: str, meta: dict) -> None:
        node_id = f"route:{verb}:{path}"
        spec = self.openapi.get((verb, path), {})
        services = self._services_of(func)
        # Tables the handler reaches without a service class in between: inline
        # SQL, a query constant, a plain helper function. In a layered project this
        # is empty and the tables arrive through service -> repository instead.
        tables = self._tables_reached(func, module)
        self.graph.add_node(
            node_id,
            "route",
            f"{verb} {path}",
            group=spec.get("tags", [group])[0] if spec.get("tags") else group,
            method=verb,
            path=path,
            handler=func.name,
            summary=meta.get("summary") or spec.get("summary"),
            # The full text, never truncated: an endpoint's rules live here.
            description=(spec.get("description") or ast.get_docstring(func) or None),
            doc_source=("openapi" if spec.get("description") else ("docstring" if ast.get_docstring(func) else None)),
            deprecated=spec.get("deprecated"),
            operation_id=spec.get("operationId"),
            tags=spec.get("tags") or None,
            params=_spec_params(spec),
            responses=_spec_responses(spec),
            request_schema=_spec_request(spec),
            response_model=meta.get("response_model"),
            request_body=_request_dto(func),
            source=f"{module.rel}:{func.lineno}",
            in_spec=bool(spec),
            # Docs/health plumbing: real routes, but not part of the public contract.
            internal=meta.get("internal") or (not spec and path in ("/", "/docs", "/redoc", "/openapi.json")) or None,
            # A handler with no service is normal (health checks, inline logic) —
            # openapi.json already gives the full route truth, so there is nothing
            # for an LLM to add here.
            standalone=None if services else True,
        )
        for qualified, info in tables.items():
            self._add_table(qualified, info)
            ops = sorted(info["ops"])
            self.graph.add_edge(
                node_id,
                f"table:{qualified}",
                WRITES if WRITE_OPS & set(ops) else READS,
                ops=ops,
                evidence=info["evidence"][0] if info["evidence"] else None,
            )
        for service in services:
            self.graph.add_edge(
                node_id,
                f"service:{service}",
                CALLS,
                evidence=f"{module.rel}:{func.lineno}",
            )

    def _services_of(self, func) -> list[str]:
        """Service classes a handler depends on, via annotation or Depends()."""
        out: list[str] = []
        for arg in list(func.args.args) + list(func.args.kwonlyargs):
            if arg.annotation is not None:
                name = _annotation_tail(arg.annotation)
                if name and name.endswith("Service"):
                    out.append(name)
        for factory in self._depends_of(func):
            info = self.factories.get(factory)
            if info and info["returns"].endswith("Service"):
                out.append(info["returns"])
        return sorted(set(out))

    # ----------------------------------------------------------------- services

    def _scan_services(self) -> None:
        """Create service nodes and wire them to their repositories."""
        for name, module in self.class_home.items():
            if not name.endswith("Service"):
                continue
            cls = module.classes[name]
            node_id = f"service:{name}"
            repos = self._repos_of_service(name, cls)
            # Only a service that imports a repository we failed to wire is a real
            # gap. A service with no repository at all (cache, auth, webhooks) is
            # simply a service with no database access.
            missed = [a for a in module.imports if a.endswith("Repository")] and not repos
            self.graph.add_node(
                node_id,
                "service",
                name,
                group=_group_of(module.rel),
                source=f"{module.rel}:{cls.lineno}",
                description=ast.get_docstring(cls),
                methods=[
                    f.name
                    for f in cls.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and not f.name.startswith("_")
                ],
                unresolved=True if missed else None,
                hint="imports a repository that could not be wired" if missed else None,
            )
            for repo in repos:
                self.graph.add_edge(node_id, f"repo:{repo}", CALLS, evidence=f"{module.rel}:{cls.lineno}")
            self._wire_external(node_id, module)

    def _repos_of_service(self, service: str, cls: ast.ClassDef) -> list[str]:
        repos: set[str] = set()
        # a) constructor annotations: __init__(self, repo: XRepository)
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
                for arg in list(item.args.args) + list(item.args.kwonlyargs):
                    if arg.annotation is not None:
                        tail = _annotation_tail(arg.annotation)
                        if tail and tail.endswith("Repository"):
                            repos.add(tail)
        # b) DI factory that builds this service: its Depends() are the repos
        for info in self.factories.values():
            if info["returns"] != service:
                continue
            for dep in info["deps"]:
                dep_info = self.factories.get(dep)
                if dep_info and dep_info["returns"].endswith("Repository"):
                    repos.add(dep_info["returns"])
        return sorted(repos)

    # ------------------------------------------------------------- repositories

    def _scan_repositories(self) -> None:
        for name, module in self.class_home.items():
            if not name.endswith("Repository"):
                continue
            cls = module.classes[name]
            node_id = f"repo:{name}"
            hits = self._tables_of(cls, module)
            self.graph.add_node(
                node_id,
                "repo",
                name,
                group=_group_of(module.rel),
                source=f"{module.rel}:{cls.lineno}",
                description=ast.get_docstring(cls),
                unresolved=None if hits else True,
                hint=None if hits else "no table reference resolved",
            )
            for qualified, info in hits.items():
                self._add_table(qualified, info)
                ops = sorted(info["ops"])
                kind = WRITES if WRITE_OPS & set(ops) else READS
                self.graph.add_edge(
                    node_id,
                    f"table:{qualified}",
                    kind,
                    ops=ops,
                    evidence=info["evidence"][0] if info["evidence"] else None,
                )
            self._wire_external(node_id, module)

    def _tables_of(self, cls: ast.ClassDef, module: _Module) -> dict[str, dict]:
        """Resolve every table a repository class touches, with operations."""
        hits: dict[str, dict] = {}
        seen: set[tuple[str, str]] = set()
        for method in cls.body:
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_tables(method, module, hits, seen, depth=0)
        return hits

    def _tables_reached(self, func, module: _Module) -> dict[str, dict]:
        """Every table a function touches, directly or through the helpers it calls.

        Following calls finds SQL wherever it lives (handler, constant, helper), not only
        in layered repositories.
        """
        hits: dict[str, dict] = {}
        self._collect_tables(func, module, hits, set(), depth=0)
        return hits

    def _collect_tables(self, func, module: _Module, hits: dict, seen: set, depth: int) -> None:
        key = (module.dotted, f"{func.name}:{func.lineno}")
        if key in seen:
            return
        seen.add(key)

        def record(raw: str, op: str, where: _Module, line: int) -> None:
            rec = self.registry.resolve(raw)
            if rec is None:
                return  # unknown object: the ground-truth gate rejects it
            qualified = self.registry.qualified(raw)
            entry = hits.setdefault(qualified, {"ops": set(), "evidence": [], "record": rec})
            entry["ops"].add(op)
            marker = f"{where.rel}:{line}"
            if marker not in entry["evidence"]:
                entry["evidence"].append(marker)

        def strings(tree: ast.AST, where: _Module, fallback: int) -> None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 12:
                    if not _looks_like_sql(node.value):
                        continue
                    for raw, op in _sql_refs(node.value):
                        record(raw, op, where, getattr(node, "lineno", fallback))

        # The body only. Decorators and parameter defaults are where FastAPI keeps
        # its prose — `description=`, `Query(description=…)` — and prose names
        # tables all the time without touching them. So does a docstring.
        body = func.body[1:] if ast.get_docstring(func) is not None else func.body
        body_tree = ast.Module(body=body, type_ignores=[])
        model_tables = self._model_tables(module)
        ops_here: set[str] = set()
        models_here: set[str] = set()
        local_names = {a.arg for a in func.args.args + func.args.kwonlyargs}
        strings(body_tree, module, func.lineno)
        for node in ast.walk(body_tree):
            if isinstance(node, ast.Call):
                callee = _callee_name(node.func)
                if callee in ORM_OPS:
                    ops_here.add(ORM_OPS[callee])
                if depth < MAX_CALL_DEPTH:
                    target = self._project_function(node.func, module)
                    if target:
                        self._collect_tables(target[1], target[0], hits, seen, depth + 1)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in model_tables:
                    models_here.add(node.id)
                elif node.id not in local_names:
                    found = self._project_constant(node.id, module)
                    if found and ("const", f"{found[0].dotted}.{node.id}") not in seen:
                        seen.add(("const", f"{found[0].dotted}.{node.id}"))
                        strings(found[1], found[0], getattr(found[1], "lineno", func.lineno))
        for model in models_here:
            for op in ops_here or {"SELECT"}:
                record(model_tables[model], op, module, func.lineno)

    def _model_tables(self, module: _Module) -> dict[str, str]:
        """Model classes imported into a module, mapped to their table name."""
        out: dict[str, str] = {}
        for alias in list(module.imports) + list(module.classes):
            home = self.class_home.get(alias)
            tablename = _tablename_of(home.classes.get(alias)) if home else None
            if tablename:
                out[alias] = tablename
        return out

    def _project_function(self, func_node: ast.AST, module: _Module):
        """`helper(...)`, `imported(...)` or `mod.helper(...)` -> (module, def), if it is ours."""
        if isinstance(func_node, ast.Name):
            name = func_node.id
            if name in module.functions:
                return module, module.functions[name]
            dotted = module.imports.get(name)
            if dotted and "." in dotted:
                home, _, attr = dotted.rpartition(".")
                target = self.modules.get(home)
                if target and attr in target.functions:
                    return target, target.functions[attr]
        elif isinstance(func_node, ast.Attribute) and isinstance(func_node.value, ast.Name):
            target = self.modules.get(module.imports.get(func_node.value.id, ""))
            if target and func_node.attr in target.functions:
                return target, target.functions[func_node.attr]
        return None

    def _project_constant(self, name: str, module: _Module):
        """A module-level constant, here or imported from another project module."""
        if name in module.constants:
            return module, module.constants[name]
        dotted = module.imports.get(name)
        if dotted and "." in dotted:
            home, _, attr = dotted.rpartition(".")
            target = self.modules.get(home)
            if target and attr in target.constants:
                return target, target.constants[attr]
        return None

    def _add_table(self, qualified: str, info: dict) -> None:
        rec = info["record"]
        self.graph.add_node(
            f"table:{qualified}",
            "table",
            rec["name"],
            group=rec["schema"] or "public",
            schema=rec["schema"],
            object_kind=rec["kind"],
            verified=True,
            defined_in=rec["sources"][:4],
            # What the DDL or the model says this table holds. The panel shows it
            # on click, so "which column is that endpoint writing" stops being a
            # trip to the schema file.
            columns=rec.get("columns") or None,
        )

    # ------------------------------------------------------------------ external

    def _wire_external(self, node_id: str, module: _Module) -> None:
        for root in sorted(module.external):
            label, desc = EXTERNAL_MODULES[root]
            ext_id = f"external:{label}"
            self.graph.add_node(ext_id, "external", label, group="external", via=root, description=desc)
            self.graph.add_edge(node_id, ext_id, SENDS, evidence=module.rel)


# ----------------------------------------------------------------- ast helpers


SQL_WORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN|WITH|CALL)\b", re.I)
BARE_QUALIFIED_RE = re.compile(r"^\s*\"?[a-zA-Z_]\w*\"?\.\"?[a-zA-Z_]\w*\"?\s*$")


def _looks_like_sql(text: str) -> bool:
    """True for a query or a bare ``schema.table``, not a message that mentions one."""
    if BARE_QUALIFIED_RE.match(text):
        return True
    return bool(SQL_WORD_RE.search(text)) and not text.rstrip().endswith((".", "!", "?"))


def _callee_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _kwarg_str(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _kwarg_list(call: ast.Call, name: str) -> list[str]:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [e.value for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _schema_hint(schema: dict | None) -> str | None:
    """Collapse a JSON Schema into a short type label for the docs panel."""
    if not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            parts = [_schema_hint(s) for s in schema[key]]
            parts = [p for p in parts if p and p != "null"]
            joined = " | ".join(dict.fromkeys(parts))
            return joined or None
    if schema.get("enum"):
        return " | ".join(str(v) for v in schema["enum"])
    kind = schema.get("type")
    if kind == "array":
        inner = _schema_hint(schema.get("items")) or "any"
        return f"{inner}[]"
    if kind and schema.get("format"):
        return f"{kind}<{schema['format']}>"
    return str(kind) if kind else None


def _spec_params(spec: dict) -> list[dict] | None:
    """Query/path/header parameters, with the rules authors wrote on them."""
    out: list[dict] = []
    for param in spec.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        schema = param.get("schema") or {}
        entry = {
            "name": param.get("name"),
            "in": param.get("in"),
            "required": bool(param.get("required")),
            "description": param.get("description") or None,
            "type": _schema_hint(schema),
        }
        if isinstance(schema, dict) and schema.get("default") is not None:
            entry["default"] = schema["default"]
        out.append({k: v for k, v in entry.items() if v is not None})
    return out or None


def _spec_responses(spec: dict) -> list[dict] | None:
    out = []
    for code, body in (spec.get("responses") or {}).items():
        if not isinstance(body, dict):
            continue
        schema = None
        content = body.get("content") or {}
        for media in content.values():
            schema = _schema_hint((media or {}).get("schema"))
            if schema:
                break
        entry = {"code": str(code), "description": body.get("description") or None, "schema": schema}
        out.append({k: v for k, v in entry.items() if v is not None})
    return out or None


def _spec_request(spec: dict) -> dict | None:
    body = spec.get("requestBody")
    if not isinstance(body, dict):
        return None
    schema = None
    for media in (body.get("content") or {}).values():
        schema = _schema_hint((media or {}).get("schema"))
        if schema:
            break
    entry = {
        "required": bool(body.get("required")),
        "description": body.get("description") or None,
        "schema": schema,
    }
    entry = {k: v for k, v in entry.items() if v not in (None, False)}
    return entry or None


def _is_route_owner(name: str) -> bool:
    """True for the objects FastAPI routes hang off: a router or the app itself."""
    lowered = name.lower()
    return "router" in lowered or lowered in {"app", "application"} or lowered.endswith("_app")


def _route_decorator(deco: ast.AST) -> tuple[str | None, str, dict]:
    """Return (VERB, path, meta) for a ``@router.get(...)`` style decorator."""
    if not isinstance(deco, ast.Call):
        return None, "", {}
    callee = deco.func
    if not (isinstance(callee, ast.Attribute) and callee.attr in HTTP_VERBS):
        return None, "", {}
    owner = callee.value
    if not isinstance(owner, ast.Name) or not _is_route_owner(owner.id):
        return None, "", {}
    path = ""
    if deco.args and isinstance(deco.args[0], ast.Constant) and isinstance(deco.args[0].value, str):
        path = deco.args[0].value
    meta = {"summary": _kwarg_str(deco, "summary")}
    for kw in deco.keywords:
        if kw.arg == "include_in_schema" and isinstance(kw.value, ast.Constant):
            meta["internal"] = kw.value.value is False
        if kw.arg == "response_model":
            try:
                meta["response_model"] = ast.unparse(kw.value)
            except Exception:  # pragma: no cover - unparse is best effort
                pass
    return callee.attr.upper(), path, meta


def _annotation_tail(node: ast.AST) -> str | None:
    """Innermost identifier of an annotation: Annotated[X, ...] -> X."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Name) and node.value.id == "Annotated":
            inner = node.slice
            if isinstance(inner, ast.Tuple) and inner.elts:
                return _annotation_tail(inner.elts[0])
            return _annotation_tail(inner)
        return _annotation_tail(node.value)
    if isinstance(node, ast.BinOp):  # X | None
        return _annotation_tail(node.left)
    return None


def _request_dto(func) -> str | None:
    for arg in list(func.args.args) + list(func.args.kwonlyargs):
        if arg.annotation is None:
            continue
        tail = _annotation_tail(arg.annotation)
        if tail and (tail.endswith("DTO") or tail.endswith("Request") or tail.endswith("Payload")):
            return tail
    return None


def _tablename_of(cls: ast.ClassDef | None) -> str | None:
    if cls is None:
        return None
    for item in cls.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__tablename__"
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                ):
                    return item.value.value
    return None


def _group_of(rel: str) -> str:
    parts = Path(rel).parts
    return parts[-2] if len(parts) > 1 else "app"
