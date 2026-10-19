"""Static scanner for Express routers (app.get / router.post) + Prisma / TypeORM / Sequelize / Knex / raw SQL.

Resolves ``express.Router()`` instances mounted with ``app.use("/prefix", router)`` across
files (``require`` / ``import``, ``module.exports = router`` / ``export default router``),
follows each route's handler (inline, ``controller.method``, wrapped, imported) and finds the
services, repositories and tables it reaches. Nothing is executed.

Conventions (documented because they are choices, not facts):
* A *service* is a class named ``*Service`` or, when a file lives in a ``services/`` directory
  (or is named ``*.service.js`` / ``fooService.js``) and defines no such class, the module
  itself: node ``service:<Basename in PascalCase>Service``. Repositories are the same with
  ``*Repository`` / ``*Repo`` / ``*Dao`` and ``repositories/`` / ``repos/`` / ``dao/``.
* Routes that cannot be traced to a service are ``standalone`` (health checks, inline SQL).
* A router that no scanned file mounts is still emitted, flagged ``unresolved``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..model import CALLS, SENDS, Graph
from ..tables import TableRegistry
from ._common import TableHits, group_of, join_path, wire_tables
from ._js import (
    EXTERNALS,
    JsFile,
    JsProject,
    OrmIndex,
    Unit,
    doc_before,
    externals_in,
    find_function,
    match_close,
    package_dirs,
    reach_tables,
    split_top,
    str_list,
    str_value,
)

VERBS = ("get", "post", "put", "patch", "delete", "all", "head", "options")
ROUTER_DECL = re.compile(r"(?:const|let|var)\s+([\w$]+)\s*(?::[^=;]+)?=\s*(?:await\s+)?(?:[\w$]+\s*\.\s*)?Router\s*\(")
APP_DECL = re.compile(r"(?:const|let|var)\s+([\w$]+)\s*(?::[^=;]+)?=\s*(?:await\s+)?express\s*\(\s*\)")
CALL_RE = re.compile(r"(?<![\w$])([\w$]+)\s*\.\s*(" + "|".join(VERBS) + r"|use|route)\s*\(")
# Owners we accept without a visible declaration (``module.exports = (app) => { app.get(...) }``).
FREE_OWNER_RE = re.compile(r"^(app|router|routes?|api|server|application|\w*Router|\w*Routes|\w*App)$", re.I)
INLINE_RE = re.compile(r"^(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[\w$]+\s*=>)")
REF_RE = re.compile(r"^(?:this\s*\.\s*)?[\w$]+(?:\s*\.\s*[\w$]+)*$")
PARAM_RE = re.compile(r":([A-Za-z_]\w*)(?:\([^)]*\))?\??")
SERVICE_META_KINDS = {"service", "repo"}

Key = tuple[Path, str]  # (file, router/app variable)


@dataclass
class Route:
    js: JsFile
    owner: str
    verb: str
    paths: list[str]
    handlers: list[tuple[int, int]]
    pos: int


@dataclass
class Mount:
    js: JsFile
    prefixes: list[str]
    target: tuple[int, int]


@dataclass
class FileData:
    routers: dict[str, int] = field(default_factory=dict)
    apps: set[str] = field(default_factory=set)
    routes: list[Route] = field(default_factory=list)
    mounts: dict[str, list[Mount]] = field(default_factory=dict)  # owner -> mounts


class ExpressScanner:
    stack = "node/express"

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        self.root = root
        self.graph = graph
        self.registry = registry
        self.openapi_source = openapi
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ hooks

    @staticmethod
    def detect(root: Path) -> bool:
        """True when a package.json depends on express and not on @nestjs/core (Nest owns that)."""
        return bool(package_dirs(root.resolve(), "express", "@nestjs/core"))

    @staticmethod
    def register_tables(root: Path, registry: TableRegistry) -> None:
        """Register Prisma models, TypeORM entities, Sequelize models and Knex createTable calls."""
        OrmIndex.build(root.resolve()).register(registry)

    # ------------------------------------------------------------------- entry

    def scan(self) -> None:
        root = self.root.resolve()
        dirs = package_dirs(root, "express", "@nestjs/core") or [root]
        self.project = JsProject(root, dirs)
        self.orm = OrmIndex.build(root)
        self.data: dict[Path, FileData] = {}
        for js in self.project.files():
            self.data[js.path] = self._collect(js)
        self._emit_routes()
        self._emit_units()

    # --------------------------------------------------------------- discovery

    def _collect(self, js: JsFile) -> FileData:
        fd = FileData()
        skel = js.skel
        for m in ROUTER_DECL.finditer(skel):
            fd.routers[m.group(1)] = m.start()
        for m in APP_DECL.finditer(skel):
            fd.apps.add(m.group(1))
        for m in CALL_RE.finditer(skel):
            owner, verb = m.group(1), m.group(2)
            if not (owner in fd.routers or owner in fd.apps or FREE_OWNER_RE.match(owner)):
                continue
            close = match_close(skel, m.end() - 1)
            if close < 0:
                continue
            spans = split_top(skel, m.end(), close)
            if verb == "route":
                self._collect_chain(js, fd, owner, spans, close, m.start())
            elif verb == "use":
                self._collect_use(js, fd, owner, spans)
            elif len(spans) >= 2:
                paths = self._paths(js, js.code[spans[0][0] : spans[0][1]])
                if paths is None:
                    self.warnings.append(f"skip {js.rel}:{js.line(m.start())}: path is not a literal")
                    continue
                fd.routes.append(Route(js, owner, verb.upper(), paths, spans[1:], m.start()))
        return fd

    def _collect_chain(self, js: JsFile, fd: FileData, owner: str, spans, close: int, pos: int) -> None:
        if not spans:
            return
        paths = self._paths(js, js.code[spans[0][0] : spans[0][1]])
        if paths is None:
            return
        i = close + 1
        verb_re = re.compile(r"\s*\.\s*(" + "|".join(VERBS) + r")\s*\(")
        while True:
            m = verb_re.match(js.skel, i)
            if not m:
                break
            c = match_close(js.skel, m.end() - 1)
            if c < 0:
                break
            fd.routes.append(Route(js, owner, m.group(1).upper(), paths, split_top(js.skel, m.end(), c), pos))
            i = c + 1

    def _collect_use(self, js: JsFile, fd: FileData, owner: str, spans) -> None:
        if not spans:
            return
        first = js.code[spans[0][0] : spans[0][1]]
        prefixes = self._paths(js, first)
        if prefixes is not None and len(spans) >= 2:
            target = spans[-1]
        elif prefixes is None:
            prefixes, target = [""], spans[-1]
        else:
            return
        fd.mounts.setdefault(owner, []).append(Mount(js, prefixes, target))

    def _paths(self, js: JsFile, expr: str) -> list[str] | None:
        """Literal path(s) of a route/mount argument: strings, arrays, constants, ``A + "/x"``."""
        expr = expr.strip()
        if not expr:
            return None
        if expr.startswith("["):
            out = str_list(expr)
            return out or None
        parts = _split_plus(expr)
        if len(parts) > 1:
            pieces = [self._paths(js, p) for p in parts]
            if any(p is None or len(p) != 1 for p in pieces):
                return None
            return ["".join(p[0] for p in pieces)]  # type: ignore[index]
        v = str_value(expr)
        if v is not None:
            return [v]
        if expr.startswith("`"):
            ok = True

            def sub(m: re.Match) -> str:
                nonlocal ok
                val = self._const(js, m.group(1))
                if val is None:
                    ok = False
                    return ""
                return val

            out = re.sub(r"\$\{\s*([\w$]+)\s*\}", sub, expr[1:-1])
            return [out] if ok and "${" not in out else None
        if re.fullmatch(r"[\w$]+", expr):
            val = self._const(js, expr)
            return [val] if val is not None else None
        return None

    def _const(self, js: JsFile, name: str) -> str | None:
        if name in js.consts:
            return js.consts[name]
        imp = js.imports.get(name)
        if imp and imp.target:
            other = self.project.file(imp.target)
            if other:
                return other.consts.get(imp.name if imp.name != "default" else name)
        return None

    # ----------------------------------------------------------------- routers

    def _router_in(self, f: JsFile, name: str | None) -> Key | None:
        """The router a module exports as ``name`` (or by default)."""
        fd = self.data.get(f.path)
        if fd is None:
            return None
        known = set(fd.routers) | fd.apps | {r.owner for r in fd.routes} | set(fd.mounts)
        skel = f.skel
        if name and name != "default":
            if name in known:
                return f.path, name
            m = re.search(r"(?:module\s*\.\s*)?exports\s*\.\s*" + re.escape(name) + r"\s*=\s*([\w$]+)", skel)
            if m and m.group(1) in known:
                return f.path, m.group(1)
            for blk in re.finditer(r"(?:module\s*\.\s*exports\s*=\s*|export\s*)\{([^}]*)\}", skel):
                for part in blk.group(1).split(","):
                    bits = [b.strip() for b in re.split(r"\s+as\s+|:", part.strip())]
                    if len(bits) == 2 and bits[1] == name and bits[0] in known:
                        return f.path, bits[0]
                    if len(bits) == 1 and bits[0] == name and name in known:
                        return f.path, name
            return None
        for pat in (
            r"module\s*\.\s*exports\s*=\s*([\w$]+)\s*;?",
            r"export\s+default\s+([\w$]+)",
            r"exports\s*\.\s*default\s*=\s*([\w$]+)",
        ):
            m = re.search(pat, skel)
            if m and m.group(1) in known:
                return f.path, m.group(1)
        declared = list(fd.routers)
        if len(declared) == 1:
            return f.path, declared[0]
        if len(known) == 1:
            return f.path, next(iter(known))
        return None

    def _resolve_target(self, js: JsFile, a: int, b: int) -> Key | None:
        expr = js.skel[a:b].strip()
        code = js.code[a:b].strip()
        m = re.fullmatch(r"require\s*\(\s*(['\"])(.+?)\1\s*\)(?:\s*\.\s*([\w$]+))?", code, re.S)
        if m:
            target = self.project.resolve(js.path, m.group(2))
            f = self.project.file(target) if target else None
            return self._router_in(f, m.group(3)) if f else None
        m = re.fullmatch(r"([\w$]+)", expr)
        if m:
            name = m.group(1)
            fd = self.data[js.path]
            if name in fd.routers:
                return js.path, name
            imp = js.imports.get(name)
            if imp and imp.target:
                f = self.project.file(imp.target)
                return self._router_in(f, imp.name) if f else None
            return None
        m = re.fullmatch(r"([\w$]+)\s*\.\s*([\w$]+)", expr)
        if m:
            imp = js.imports.get(m.group(1))
            if imp and imp.target:
                f = self.project.file(imp.target)
                return self._router_in(f, m.group(2)) if f else None
        return None

    def _emit_routes(self) -> None:
        keys: set[Key] = set()
        routes: dict[Key, list[Route]] = {}
        children: dict[Key, list[tuple[Key, list[str]]]] = {}
        for path, fd in self.data.items():
            for var in list(fd.routers) + list(fd.apps):
                keys.add((path, var))
            for r in fd.routes:
                key = (path, r.owner)
                keys.add(key)
                routes.setdefault(key, []).append(r)
        mounted: set[Key] = set()
        for path, fd in self.data.items():
            for owner, mounts in fd.mounts.items():
                parent = (path, owner)
                keys.add(parent)
                for mt in mounts:
                    child = self._resolve_target(mt.js, *mt.target)
                    if child and child != parent:
                        children.setdefault(parent, []).append((child, mt.prefixes))
                        mounted.add(child)
        for key in sorted(keys, key=lambda k: (str(k[0]), k[1])):
            if key in mounted:
                continue
            fd = self.data[key[0]]
            orphan = key[1] in fd.routers and key[1] not in fd.apps
            self._walk(key, "", routes, children, {key}, orphan)

    def _walk(self, key: Key, prefix: str, routes, children, stack: set[Key], orphan: bool) -> None:
        for r in routes.get(key, []):
            for p in r.paths:
                self._add_route(r, join_path(prefix, p), orphan)
        for child, prefixes in children.get(key, []):
            if child in stack:
                continue
            for pre in prefixes:
                self._walk(child, join_path(prefix, pre), routes, children, stack | {child}, False)

    # ------------------------------------------------------------------ routes

    def _add_route(self, r: Route, full: str, orphan: bool) -> None:
        path = PARAM_RE.sub(r"{\1}", full)
        node_id = f"route:{r.verb}:{path}"
        label, hjs, span = self._handler(r)
        unresolved = orphan or (span is None and label != "inline")
        hint = None
        if orphan:
            hint = "router is never mounted by a scanned app"
        elif span is None and label != "inline":
            hint = "handler could not be resolved"
        units: list[Unit] = []
        hits = TableHits(self.registry)
        externals: list[tuple[str, str]] = []
        if span and hjs:
            reach_tables(self.project, self.orm, hjs, span[0], span[1], hits)
            units = self.project.referenced_units(hjs, span[0], span[1])
            for u in self.project.units_by_file.get(hjs.path, []):
                if u.span[0] <= span[0] < u.span[1] and u not in units and u.cls:
                    units.append(u)
            externals = externals_in(hjs, span[0], span[1])
        summary, description = doc_before(r.js.text, r.pos)
        self.graph.add_node(
            node_id,
            "route",
            f"{r.verb} {path}",
            group=self._group(r.js, path),
            method=r.verb,
            path=path,
            handler=label,
            summary=summary,
            description=description,
            doc_source="jsdoc" if description else None,
            source=f"{r.js.rel}:{r.js.line(r.pos)}",
            standalone=None if units or unresolved else True,
            unresolved=True if unresolved else None,
            hint=hint,
        )
        wire_tables(self.graph, node_id, hits)
        evidence = f"{r.js.rel}:{r.js.line(r.pos)}"
        for u in units:
            self.graph.add_edge(node_id, u.node_id, CALLS, evidence=evidence)
        self._wire_external(node_id, externals, r.js.rel)

    def _handler(self, r: Route) -> tuple[str, JsFile | None, tuple[int, int] | None]:
        """(label, file, body span) of the last handler argument of a route."""
        if not r.handlers:
            return "inline", None, None
        a, b = r.handlers[-1]
        return self._target(r.js, a, b, 0)

    def _target(self, js: JsFile, a: int, b: int, depth: int) -> tuple[str, JsFile | None, tuple[int, int] | None]:
        skel = js.skel
        text = skel[a:b].strip()
        if not text:
            return "inline", None, None
        a += len(skel[a:b]) - len(skel[a:b].lstrip())
        if text.startswith("["):
            inner = split_top(skel, a + 1, a + len(text) - 1)
            return self._target(js, *inner[-1], depth + 1) if inner and depth < 4 else ("inline", None, None)
        if INLINE_RE.match(text):
            return "inline", js, (a, a + len(text))
        m = re.fullmatch(r"([\w$.\s]+?)\s*\.\s*bind\s*\(.*\)", text, re.S)
        if m:
            text = m.group(1).strip()
        if REF_RE.match(text):
            ref = re.sub(r"\s+", "", text)
            ref = ref[5:] if ref.startswith("this.") else ref
            found = self._resolve_ref(js, ref)
            if found:
                return ref, found[0], found[1]
            return ref, None, None
        m = re.match(r"([\w$.]+)\s*\(", text)
        if m and text.endswith(")") and m.group(1) != "require" and depth < 4:
            op = a + m.end() - 1
            inner = split_top(skel, op + 1, a + len(text) - 1)
            inner = [(x, y) for x, y in inner if not js.skel[x:y].strip().startswith(("'", '"', "`"))]
            if inner:
                return self._target(js, *inner[-1], depth + 1)
        return "inline", None, None

    def _resolve_ref(self, js: JsFile, ref: str) -> tuple[JsFile, tuple[int, int]] | None:
        parts = ref.split(".")
        if len(parts) == 1:
            span = find_function(js, parts[0])
            if span:
                return js, span
            imp = js.imports.get(parts[0])
            if imp and imp.target:
                other = self.project.file(imp.target)
                if other:
                    span = find_function(other, imp.name if imp.name != "default" else parts[0])
                    if span:
                        return other, span
                    if imp.name == "default":  # module.exports = handler
                        return other, (0, len(other.skel))
            return None
        head, tail = parts[0], parts[-1]
        imp = js.imports.get(head)
        if imp and imp.target:
            other = self.project.file(imp.target)
            if other:
                span = find_function(other, tail)
                if span:
                    return other, span
            return None
        cls_name = js.instances.get(head, head)  # `const ctrl = new OrderController()` / static `OrderController.list`
        for cls in self.project.class_index.get(cls_name, []):
            member = next((m for m in cls.members if m.name == tail), None)
            if member and member.body:
                return cls.js, member.body
            span = find_function(cls.js, tail)
            if span:
                return cls.js, span
        span = find_function(js, tail)  # a local object
        return (js, span) if span else None

    @staticmethod
    def _group(js: JsFile, path: str) -> str:
        stem = re.sub(r"[._-]?(routes?|router)$", "", js.path.stem, flags=re.I) or js.path.stem
        if stem.lower() in {"index", "app", "server", "main", ""}:
            seg = next((s for s in path.split("/") if s and not s.startswith("{")), None)
            return seg or (js.path.parent.name if js.path.parent != js.project.root else "app")
        return stem

    # ------------------------------------------------------- services / repos

    def _emit_units(self) -> None:
        for u in self.project.units:
            hits = TableHits(self.registry)
            reach_tables(self.project, self.orm, u.js, u.span[0], u.span[1], hits)
            summary, description = doc_before(u.js.text, u.cls.start if u.cls else 0)
            meta = dict(
                group=group_of(u.js.rel),
                source=f"{u.js.rel}:{u.line}",
                description=description,
                methods=u.methods,
            )
            if u.kind == "repo":
                meta.update(unresolved=None if hits else True, hint=None if hits else "no table reference resolved")
            self.graph.add_node(u.node_id, u.kind, u.name, **meta)
            for ref in self.project.referenced_units(u.js, u.span[0], u.span[1]):
                if ref is not u and ref.kind == "repo":
                    self.graph.add_edge(u.node_id, ref.node_id, CALLS, evidence=f"{u.js.rel}:{u.line}")
            wire_tables(self.graph, u.node_id, hits)
            self._wire_external(u.node_id, externals_in(u.js, u.span[0], u.span[1]), u.js.rel)

    def _wire_external(self, node_id: str, found: list[tuple[str, str]], evidence: str) -> None:
        for label, via in found:
            ext_id = f"external:{label}"
            self.graph.add_node(ext_id, "external", label, group="external", via=via, description=EXTERNALS[label])
            self.graph.add_edge(node_id, ext_id, SENDS, evidence=evidence)


def _split_plus(expr: str) -> list[str]:
    """Split on top-level ``+`` outside strings/brackets (``BASE + "/x"``)."""
    from ._js import lex

    lx = lex(expr)
    out, depth, start = [], 0, 0
    for i, c in enumerate(lx.skel):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "+" and depth == 0:
            out.append(expr[start:i])
            start = i + 1
    out.append(expr[start:])
    return out
