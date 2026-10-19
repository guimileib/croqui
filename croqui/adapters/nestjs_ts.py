"""Static scanner for NestJS @Controller / @Get + @Injectable services + TypeORM / Prisma repositories.

Reads decorators straight from the TypeScript source (no compiler): ``@Controller`` prefixes,
``@Get/@Post/...`` method paths, ``app.setGlobalPrefix`` and URI versioning, and the
constructor-injected dependencies of each controller. A route is linked only to the injected
services its handler method actually uses (``this.usersService.x``, following private helper
methods of the controller); when the body cannot be read, every injected service is linked.

Conventions:
* A *service* / *repository* is a class named ``*Service`` / ``*Repository`` (``*Repo``, ``*Dao``).
  ``PrismaService`` (anything extending ``PrismaClient``) is infrastructure, not a node.
* ``@InjectRepository(Entity)`` has no repository class, so the tables are edges straight from
  the class that injects it (service or controller).
* TypeORM ``@Entity()`` without a name maps to the snake_case class name (its default naming
  strategy); sequelize-typescript ``@Table`` without a name maps to the pluralized class name.
Not supported: ``RouterModule.register`` path trees, Mongoose (no SQL tables), custom provider tokens.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model import CALLS, SENDS, Graph
from ..tables import TableRegistry
from ._common import TableHits, group_of, join_path, wire_tables
from ._js import (
    EXTERNALS,
    ClassInfo,
    JsFile,
    JsProject,
    Member,
    OrmIndex,
    Unit,
    doc_before,
    externals_in,
    package_dirs,
    parse_object,
    reach_tables,
    str_list,
    str_value,
    type_head,
)

ROUTE_DECOS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH", "Delete": "DELETE",
    "All": "ALL", "Head": "HEAD", "Options": "OPTIONS", "Sse": "GET",
}
PARAM_RE = re.compile(r":([A-Za-z_]\w*)(?:\([^)]*\))?\??")
DTO_RE = re.compile(r"(Dto|DTO|Request|Payload|Input)$")


class NestScanner:
    stack = "node/nestjs"

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        self.root = root
        self.graph = graph
        self.registry = registry
        self.openapi_source = openapi
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ hooks

    @staticmethod
    def detect(root: Path) -> bool:
        """True when a package.json depends on @nestjs/core."""
        return bool(package_dirs(root.resolve(), "@nestjs/core"))

    @staticmethod
    def register_tables(root: Path, registry: TableRegistry) -> None:
        """Register TypeORM entities, Prisma models, sequelize models and Knex createTable calls."""
        OrmIndex.build(root.resolve()).register(registry)

    # ------------------------------------------------------------------- entry

    def scan(self) -> None:
        root = self.root.resolve()
        self.project = JsProject(root, package_dirs(root, "@nestjs/core") or [root])
        self.orm = OrmIndex.build(root)
        self.global_prefix = ""
        self.version_prefix: str | None = None  # "v" when URI versioning is on
        self.default_version: str | None = None
        self._read_bootstrap()
        for js in self.project.files():
            for cls in js.classes:
                if cls.deco("Controller"):
                    self._controller(cls)
        self._emit_units()

    def _read_bootstrap(self) -> None:
        for js in self.project.files():
            if "setGlobalPrefix" in js.code:
                m = re.search(r"setGlobalPrefix\s*\(\s*(['\"`])([^'\"`]*)\1", js.code)
                if m:
                    self.global_prefix = m.group(2)
            if "enableVersioning" in js.code:
                m = re.search(r"enableVersioning\s*\(\s*(\{.*?\})\s*\)", js.code, re.S)
                if m:
                    opts = parse_object(m.group(1))
                    if "URI" in opts.get("type", ""):
                        pre = opts.get("prefix")
                        self.version_prefix = "" if pre is not None and pre.strip() == "false" else (str_value(pre) if pre else "v") or "v"
                        dv = opts.get("defaultVersion")
                        self.default_version = (str_list(dv) or [None])[0] if dv else None  # type: ignore[assignment]

    # ------------------------------------------------------------ controllers

    def _controller(self, cls: ClassInfo) -> None:
        deco = cls.deco("Controller")
        assert deco is not None
        prefixes, versions = [""], []
        if deco.args:
            first = deco.args[0].strip()
            obj = parse_object(first)
            if obj:
                prefixes = str_list(obj.get("path", "")) or [""]
                versions = str_list(obj.get("version", ""))
            else:
                prefixes = str_list(first) or [""]
        cv = cls.deco("Version")
        if cv and cv.args:
            versions = str_list(cv.args[0]) or versions
        tags = cls.deco("ApiTags")
        injected = self._injected(cls)
        methods = {m.name: m for m in cls.members if m.kind == "method"}
        for member in cls.members:
            if member.kind != "method":
                continue
            rdeco = next((d for d in member.decos if d.name in ROUTE_DECOS), None)
            if not rdeco:
                continue
            mv = next((d for d in member.decos if d.name == "Version"), None)
            mversions = (str_list(mv.args[0]) if mv and mv.args else []) or versions
            paths = (str_list(rdeco.args[0]) if rdeco.args else []) or [""]
            if not rdeco.args or not str_list(rdeco.args[0]):
                paths = [""]
            for prefix in prefixes:
                for sub in paths:
                    for ver in (mversions or [self.default_version]):
                        self._add_route(cls, member, methods, injected, ROUTE_DECOS[rdeco.name], prefix, sub, ver, tags)

    def _injected(self, cls: ClassInfo) -> dict[str, str]:
        """Member name -> class name of everything injected through the constructor or a property."""
        out: dict[str, str] = {}
        params = [(p.name, p.type, p.decos) for p in cls.ctor_params]
        params += [(m.name, m.type, m.decos) for m in cls.members if m.kind == "prop" and any(d.name.startswith("Inject") for d in m.decos)]
        for name, ptype, decos in params:
            tname = type_head(ptype)[0]
            for d in decos:
                if d.name == "Inject":
                    for tok in re.findall(r"[A-Za-z_$][\w$]*", d.raw):
                        if tok in self.project.unit_by_class:
                            tname = tok
                            break
            if tname:
                out[name] = tname
        return out

    def _add_route(self, cls, member: Member, methods, injected, verb, prefix, sub, version, tags) -> None:
        js = cls.js
        vseg = f"{self.version_prefix}{version}" if self.version_prefix is not None and version else ""
        full = join_path(self.global_prefix, vseg, prefix, sub)
        path = PARAM_RE.sub(r"{\1}", full)
        node_id = f"route:{verb}:{path}"
        op = next((d for d in member.decos if d.name == "ApiOperation"), None)
        opts = parse_object(op.args[0]) if op and op.args else {}
        summary = str_value(opts.get("summary", ""))
        description = str_value(opts.get("description", ""))
        doc_source = "swagger" if (summary or description) else None
        if not summary and not description:
            summary, description = doc_before(js.text, member.start)
            doc_source = "jsdoc" if description else None
        group = (str_value(tags.args[0]) if tags and tags.args else None) or next(
            (s for s in path.split("/") if s and not s.startswith("{") and s != self.global_prefix.strip("/")), None
        ) or re.sub(r"Controller$", "", cls.name).lower()
        units, hits = self._reach(cls, member, methods, injected)
        body_dto = next(
            (type_head(p.type)[0] for p in member.params if any(d.name == "Body" for d in p.decos) and type_head(p.type)[0] and DTO_RE.search(type_head(p.type)[0] or "")),
            None,
        )
        self.graph.add_node(
            node_id,
            "route",
            f"{verb} {path}",
            group=group,
            method=verb,
            path=path,
            handler=member.name,
            controller=cls.name,
            summary=summary,
            description=description,
            doc_source=doc_source,
            deprecated=True if "true" in opts.get("deprecated", "") else None,
            request_body=body_dto,
            source=f"{js.rel}:{js.line(member.pos)}",
            internal=True if any(d.name == "ApiExcludeEndpoint" for d in member.decos) else None,
            standalone=None if units else True,
        )
        wire_tables(self.graph, node_id, hits)
        evidence = f"{js.rel}:{js.line(member.pos)}"
        for u in units:
            self.graph.add_edge(node_id, u.node_id, CALLS, evidence=evidence)
        for label, via in self._externals(cls, member, methods):
            self._wire_external(node_id, label, via, js.rel)

    @staticmethod
    def _spans(js: JsFile, member: Member, methods: dict[str, Member]) -> list[tuple[int, int]]:
        """The handler body plus the bodies of the controller's own helper methods it calls."""
        spans: list[tuple[int, int]] = []
        seen: set[str] = set()

        def visit(m: Member, depth: int) -> None:
            if not m.body or m.name in seen:
                return
            seen.add(m.name)
            spans.append(m.body)
            if depth >= 2:
                return
            for name in set(re.findall(r"this\s*\.\s*([\w$]+)\s*\(", js.skel[m.body[0] : m.body[1]])):
                helper = methods.get(name)
                if helper and not any(d.name in ROUTE_DECOS for d in helper.decos):
                    visit(helper, depth + 1)

        visit(member, 0)
        return spans

    def _reach(self, cls: ClassInfo, member: Member, methods, injected) -> tuple[list[Unit], TableHits]:
        hits = TableHits(self.registry)
        spans = self._spans(cls.js, member, methods)
        for a, b in spans:
            reach_tables(self.project, self.orm, cls.js, a, b, hits, depth=1)
        if not spans:
            used = set(injected)  # cannot tell: link everything injected
        else:
            used = set()
            for a, b in spans:
                used |= set(re.findall(r"this\s*\.\s*([\w$]+)", cls.js.skel[a:b]))
        units: list[Unit] = []
        for name in injected:
            u = self.project.unit_by_class.get(injected[name]) if name in used else None
            if u and u not in units:
                units.append(u)
        return units, hits

    def _externals(self, cls: ClassInfo, member: Member, methods) -> list[tuple[str, str]]:
        found: dict[str, str] = {}
        spans = self._spans(cls.js, member, methods)
        for a, b in spans:
            for label, via in externals_in(cls.js, a, b):
                found.setdefault(label, via)
        # `this.httpService.get(...)` clients are injected, so check the used members' types too
        used = set()
        for a, b in spans:
            used |= set(re.findall(r"this\s*\.\s*([\w$]+)", cls.js.skel[a:b]))
        for p in cls.ctor_params:
            if p.name in used and type_head(p.type)[0] == "HttpService":
                found.setdefault("HTTP / Webhook", "@nestjs/axios")
        return sorted(found.items())

    # ------------------------------------------------------- services / repos

    def _emit_units(self) -> None:
        for u in self.project.units:
            hits = TableHits(self.registry)
            reach_tables(self.project, self.orm, u.js, u.span[0], u.span[1], hits)
            _, description = doc_before(u.js.text, u.cls.start if u.cls else 0)
            meta = dict(group=group_of(u.js.rel), source=f"{u.js.rel}:{u.line}", description=description, methods=u.methods)
            if u.kind == "repo":
                meta.update(unresolved=None if hits else True, hint=None if hits else "no table reference resolved")
            self.graph.add_node(u.node_id, u.kind, u.name, **meta)
            for ref in self.project.referenced_units(u.js, u.span[0], u.span[1]):
                if ref is not u and ref.kind == "repo":
                    self.graph.add_edge(u.node_id, ref.node_id, CALLS, evidence=f"{u.js.rel}:{u.line}")
            wire_tables(self.graph, u.node_id, hits)
            found = dict(externals_in(u.js, u.span[0], u.span[1]))
            if u.cls and any(type_head(p.type)[0] == "HttpService" for p in u.cls.ctor_params):
                found.setdefault("HTTP / Webhook", "@nestjs/axios")
            for label, via in sorted(found.items()):
                self._wire_external(u.node_id, label, via, u.js.rel)

    def _wire_external(self, node_id: str, label: str, via: str, evidence: str) -> None:
        ext_id = f"external:{label}"
        self.graph.add_node(ext_id, "external", label, group="external", via=via, description=EXTERNALS[label])
        self.graph.add_edge(node_id, ext_id, SENDS, evidence=evidence)
