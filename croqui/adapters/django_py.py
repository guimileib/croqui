"""Static scanner for Django urls.py + views / DRF viewsets + Django ORM models.

Resolves ``urlpatterns`` recursively (``path`` / ``re_path`` / ``include`` / DRF routers),
turns function views, class-based views and viewsets into routes, and maps ORM calls
(``Model.objects...``, ``instance.save()``, serializers, raw SQL) to table operations.
Plain ``ast``; nothing is imported or executed.
"""

from __future__ import annotations

import ast
import copy
import os
import re
from pathlib import Path

from ..model import Graph
from ..tables import TableRegistry
from ._common import SKIP_DIRS, group_of, iter_files, read
from .flask_py import (
    HTTP_VERBS,
    PyIndex,
    PyMod,
    PyScanner,
    _const_str,
    _kwarg,
    _str_list,
    _tail,
    _toplevel,
    dep_files,
)

DJANGO_DEP_RE = re.compile(r"(?<![\w.-])(?:django|djangorestframework)(?!\w)", re.I)
DEFAULT_MANAGERS = {"objects", "_default_manager", "_base_manager"}

_S, _I = ["SELECT"], ["INSERT"]
_SU, _SD = ["SELECT", "UPDATE"], ["DELETE", "SELECT"]

# Operations a standard DRF action performs when the project does not override it.
ACTION_OPS = {
    "list": _S,
    "retrieve": _S,
    "create": _I,
    "update": _SU,
    "partial_update": _SU,
    "destroy": _SD,
    "perform_create": _I,
    "perform_update": ["UPDATE"],
    "perform_destroy": ["DELETE"],
}
# action -> (HTTP verb, detail route?)
VIEWSET_ACTIONS = {
    "list": ("GET", False),
    "create": ("POST", False),
    "retrieve": ("GET", True),
    "update": ("PUT", True),
    "partial_update": ("PATCH", True),
    "destroy": ("DELETE", True),
}
# Base class -> standard actions it provides.
VIEWSET_BASES = {
    "ModelViewSet": ["list", "create", "retrieve", "update", "partial_update", "destroy"],
    "ReadOnlyModelViewSet": ["list", "retrieve"],
    "ListModelMixin": ["list"],
    "CreateModelMixin": ["create"],
    "RetrieveModelMixin": ["retrieve"],
    "UpdateModelMixin": ["update", "partial_update"],
    "DestroyModelMixin": ["destroy"],
}
# Generic class-based view -> {verb: operations}.
GENERIC_VERBS = {
    "ListAPIView": {"get": _S},
    "CreateAPIView": {"post": _I},
    "RetrieveAPIView": {"get": _S},
    "UpdateAPIView": {"put": _SU, "patch": _SU},
    "DestroyAPIView": {"delete": _SD},
    "ListCreateAPIView": {"get": _S, "post": _I},
    "RetrieveUpdateAPIView": {"get": _S, "put": _SU, "patch": _SU},
    "RetrieveDestroyAPIView": {"get": _S, "delete": _SD},
    "RetrieveUpdateDestroyAPIView": {"get": _S, "put": _SU, "patch": _SU, "delete": _SD},
    "ListView": {"get": _S},
    "DetailView": {"get": _S},
    "TemplateView": {"get": []},
    "RedirectView": {"get": []},
    "FormView": {"get": [], "post": []},
    "CreateView": {"get": [], "post": _I},
    "UpdateView": {"get": _S, "post": _SU},
    "DeleteView": {"get": _S, "post": _SD, "delete": _SD},
}
LOADED_TERMINALS = {"get", "first", "last", "latest", "earliest", "create"}


# -------------------------------------------------------------------- models


def _meta(cls: ast.ClassDef) -> dict:
    """Constant assignments in a model's ``class Meta``."""
    out: dict = {}
    for item in cls.body:
        if isinstance(item, ast.ClassDef) and item.name == "Meta":
            for st in item.body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    if isinstance(st.value, ast.Constant):
                        out[st.targets[0].id] = st.value.value
    return out


def _app_label(mod: PyMod, root: Path, cache: dict) -> str:
    """App label: ``AppConfig.label`` / last part of its ``name``, else the app directory name."""
    directory = mod.path.parent
    if directory.name == "models":
        directory = directory.parent
    if directory in cache:
        return cache[directory]
    label = directory.name or root.name
    apps = directory / "apps.py"
    if apps.is_file():
        try:
            tree = ast.parse(read(apps))
        except (SyntaxError, ValueError):
            tree = None
        for cls in (n for n in (tree.body if tree else []) if isinstance(n, ast.ClassDef)):
            if "AppConfig" not in {_tail(b) for b in cls.bases}:
                continue
            values = {}
            for st in cls.body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    values[st.targets[0].id] = _const_str(st.value)
            if values.get("label"):
                label = values["label"]
            elif values.get("name"):
                label = values["name"].rsplit(".", 1)[-1]
            break
    cache[directory] = label
    return label


def _is_django_base(base: ast.AST, mod: PyMod) -> bool:
    """``models.Model`` / ``Model`` imported from django, or an auth abstract user."""
    if _tail(base) not in ("Model", "AbstractUser", "AbstractBaseUser"):
        return False
    if isinstance(base, ast.Name):
        return mod.imports.get(base.id, "").startswith("django")
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        return mod.imports.get(base.value.id, "").startswith("django")
    return False


def _dj_fields(cls: ast.ClassDef) -> list[dict]:
    """Field declarations of a model body (columns, foreign keys, many-to-many)."""
    out: list[dict] = []
    for st in cls.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            name, value = st.targets[0].id, st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            name, value = st.target.id, st.value
        else:
            continue
        if name.startswith("_") or not isinstance(value, ast.Call):
            continue
        tail = _tail(value.func) or ""
        if tail in ("GenericForeignKey", "GenericRelation") or not (tail.endswith("Field") or tail == "ForeignKey"):
            continue
        field: dict = {"name": name, "type": tail, "rel": None, "target": None}
        if tail in ("ForeignKey", "OneToOneField", "ManyToManyField"):
            field["rel"] = "m2m" if tail == "ManyToManyField" else "fk"
            field["target"] = value.args[0] if value.args else _kwarg(value, "to")
            field["through"] = _kwarg(value, "through") is not None
            field["db_table"] = _const_str(_kwarg(value, "db_table"))
        for kw in value.keywords:
            if kw.arg == "primary_key" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                field["pk"] = True
            elif kw.arg == "db_column":
                field["db_column"] = _const_str(kw.value)
            elif kw.arg == "unique" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                field["unique"] = True
        out.append(field)
    return out


def _dj_managers(cls: ast.ClassDef) -> set[str]:
    """Names of custom managers assigned in a model body (``active = ActiveManager()``)."""
    out = set()
    for st in cls.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            if isinstance(st.value, ast.Call) and "Manager" in ast.unparse(st.value.func):
                out.add(st.targets[0].id)
    return out


def django_models(index: PyIndex) -> dict[str, dict]:
    """Model class name -> {table, columns, rel, line, own, managers, extra_tables}.

    ``own`` is False for proxy models (they share their parent's table). Abstract models
    only contribute their fields to subclasses.
    """
    label_cache: dict = {}
    infos: dict[tuple[str, str], dict] = {}

    def info_of(mod: PyMod, cls: ast.ClassDef, stack: tuple) -> dict:
        key = (mod.dotted, cls.name)
        if key in infos:
            return infos[key]
        if key in stack:
            return {"model": False}
        parents = []
        for base in cls.bases:
            found = index.resolve_expr(base, mod)
            if found and found[2] == "class":
                parents.append(info_of(found[0], found[1], stack + (key,)))
        model = any(_is_django_base(b, mod) for b in cls.bases) or any(p["model"] for p in parents)
        if not model:
            infos[key] = {"model": False}
            return infos[key]
        meta = _meta(cls)
        abstract = meta.get("abstract") is True
        label = str(meta.get("app_label") or _app_label(mod, index.root, label_cache))
        concrete = [p for p in parents if p["model"] and not p["abstract"]]
        fields: list[dict] = []
        for p in parents:
            if p["model"] and p["abstract"]:
                fields.extend(p["fields"])
        fields.extend(_dj_fields(cls))
        if meta.get("proxy") is True and concrete:
            table, own = concrete[0]["table"], False
        elif abstract:
            table, own = None, False
        else:
            table, own = str(meta.get("db_table") or f"{label}_{cls.name.lower()}"), True
        managers = set(DEFAULT_MANAGERS) | _dj_managers(cls)
        for p in parents:
            managers |= p.get("managers", set())
        infos[key] = {
            "model": True, "abstract": abstract, "table": table, "own": own, "label": label,
            "fields": fields, "parents": concrete, "mod": mod, "cls": cls, "name": cls.name,
            "rel": mod.rel, "line": cls.lineno, "managers": managers,
        }
        return infos[key]

    for mod in index.mods.values():
        for cls in mod.classes.values():
            info_of(mod, cls, ())

    by_name: dict[str, list[dict]] = {}
    for info in infos.values():
        if info["model"]:
            by_name.setdefault(info["name"], []).append(info)

    def target_table(info: dict, expr: ast.AST | None) -> str | None:
        if expr is None:
            return None
        text = _const_str(expr)
        if text is not None:
            if text == "self":
                return info["table"]
            label, _, name = text.rpartition(".")
            cands = [c for c in by_name.get(name, []) if c["table"] and (not label or c["label"] == label)]
            same = [c for c in cands if c["label"] == info["label"]]
            return (same or cands or [{"table": None}])[0]["table"]
        found = index.resolve_expr(expr, info["mod"])
        if found and found[2] == "class":
            other = infos.get((found[0].dotted, found[1].name))
            return other["table"] if other and other["model"] else None
        return None

    out: dict[str, dict] = {}
    for info in infos.values():
        if not info["model"] or not info["table"]:
            continue
        columns: list[dict] = []
        extra: list[dict] = []
        has_pk = any(f.get("pk") for f in info["fields"])
        if not has_pk and info["own"] and not info["parents"]:
            columns.append({"name": "id", "type": "AutoField", "pk": True})
        elif info["own"] and info["parents"] and not has_pk:
            parent = info["parents"][0]
            columns.append({"name": f"{parent['name'].lower()}_ptr_id", "type": "OneToOneField", "pk": True,
                            "references": parent["table"]})
        for field in info["fields"]:
            if field["rel"] == "m2m":
                if not field.get("through"):
                    target = target_table(info, field["target"])
                    tname = field.get("db_table") or f"{info['table']}_{field['name']}"
                    own_col = f"{info['name'].lower()}_id"
                    text = _const_str(field["target"])
                    other_name = text.rpartition(".")[2] if text else (_tail(field["target"]) or field["name"])
                    other_col = f"{other_name.lower()}_id"
                    extra.append({"table": tname, "columns": [
                        {"name": "id", "type": "AutoField", "pk": True},
                        {"name": own_col, "type": "ForeignKey", "references": info["table"]},
                        {"name": other_col, "type": "ForeignKey", **({"references": target} if target else {})},
                    ]})
                continue
            column: dict = {"name": field.get("db_column") or field["name"], "type": field["type"]}
            if field["rel"] == "fk":
                column["name"] = field.get("db_column") or f"{field['name']}_id"
                ref = target_table(info, field["target"])
                if ref:
                    column["references"] = ref
            if field.get("pk"):
                column["pk"] = True
            if field.get("unique"):
                column["unique"] = True
            columns.append(column)
        out.setdefault(info["name"], {
            "table": info["table"], "columns": columns, "rel": info["rel"], "line": info["line"],
            "own": info["own"], "managers": info["managers"], "extra_tables": extra,
        })
    return out


# ------------------------------------------------------------- URL patterns


def _dj_route(route: str) -> str:
    """``orders/<int:pk>/`` -> ``orders/{pk}/``."""
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", route)


def _regex_route(pattern: str) -> str:
    """A ``re_path`` regex as a readable path: named groups become ``{name}``."""
    s = pattern.lstrip("^").rstrip("$")
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            out.append(s[i + 1] if not s[i + 1].isalpha() else "")
            i += 2
        elif ch == "(":
            name = None
            if s.startswith("(?P<", i):
                name = s[i + 4 : s.index(">", i)]
            depth, k = 1, i + 1
            while k < len(s) and depth:
                if s[k] == "\\":
                    k += 2
                    continue
                depth += (s[k] == "(") - (s[k] == ")")
                k += 1
            if s.startswith("(?:", i):
                out.append("")
            else:
                out.append("{" + (name or "arg") + "}")
            i = k
        elif ch in "^$?+*":
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _method_test(test: ast.AST) -> set[str] | None:
    """Verbs an ``if request.method == "POST"`` style test selects, or None if it is not one."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        parts = [_method_test(v) for v in test.values]
        return set().union(*parts) if all(p is not None for p in parts) else None
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left = test.left
        if isinstance(left, ast.Attribute) and left.attr == "method":
            right = test.comparators[0]
            if isinstance(test.ops[0], ast.Eq) and _const_str(right):
                return {_const_str(right).upper()}
            if isinstance(test.ops[0], ast.In):
                return {v.upper() for v in _str_list(right)}
    return None


def _prune(stmts: list[ast.stmt], verb: str) -> list[ast.stmt]:
    """Keep only the statements that run for ``verb`` (``if request.method == ...`` branches)."""
    out: list[ast.stmt] = []
    for st in stmts:
        verbs = _method_test(st.test) if isinstance(st, ast.If) else None
        if verbs is None:
            out.append(st)
        elif verb in verbs:
            out.extend(_prune(st.body, verb))
            if st.body and isinstance(st.body[-1], (ast.Return, ast.Raise)) and not st.orelse:
                return out  # early return: the rest is for other verbs
        else:
            out.extend(_prune(st.orelse, verb))
    return out


def _for_verb(func, verb: str):
    """A copy of ``func`` holding only the branches that serve ``verb``."""
    if not any(isinstance(n, ast.If) and _method_test(n.test) is not None for n in ast.walk(func)):
        return func
    clone = copy.copy(func)
    clone.body = _prune(func.body, verb) or [ast.Pass()]
    return clone


def _calls_super(func) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Call) and _tail(n.func.value.func) == "super"
        for n in ast.walk(func)
    )


def _class_attr(cls: ast.ClassDef, name: str) -> ast.AST | None:
    for st in cls.body:
        if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in st.targets):
            return st.value
        if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id == name:
            return st.value
    return None


# -------------------------------------------------------------- ORM visitor


def _django_ops(names: list[str]) -> list[str]:
    """Operations a ``Model.objects...`` chain performs, from the method names in it."""
    if "update_or_create" in names:
        return ["SELECT", "INSERT", "UPDATE"]
    if "get_or_create" in names:
        return ["SELECT", "INSERT"]
    if "create" in names or "bulk_create" in names:
        return ["INSERT"]
    if "delete" in names:
        return ["DELETE"]
    if "update" in names or "bulk_update" in names:
        return ["UPDATE"]
    return ["SELECT"]


class _DjVisitor(ast.NodeVisitor):
    """Django ORM operations in one function body, tracking what local variables hold."""

    def __init__(self, scanner: DjangoScanner, mod: PyMod, func, ctx: dict | None) -> None:
        self.scanner = scanner
        self.mod = mod
        self.ctx = ctx or {}
        self.model_ctx: str | None = self.ctx.get("model")
        self.ops: list[tuple[str, str, int]] = []
        # variable -> (kind, model, flag): kind is new | loaded | qs | ser (flag: has an instance)
        self.env: dict[str, tuple] = {}
        self.handled: set[int] = set()
        if self.model_ctx:
            for arg in func.args.args:
                if arg.arg == "serializer":
                    self.env["serializer"] = ("ser", self.model_ctx, func.name in ("perform_update", "update", "partial_update"))
                elif arg.arg in ("instance", "obj"):
                    self.env[arg.arg] = ("loaded", self.model_ctx, False)

    def model(self, expr: ast.AST) -> str | None:
        return self.scanner.model_of(expr, self.mod)

    def emit(self, model: str, op: str, node: ast.AST) -> None:
        self.ops.append((self.scanner.models[model]["table"], op, getattr(node, "lineno", 0)))

    @staticmethod
    def chain(node: ast.AST) -> list[ast.AST]:
        out: list[ast.AST] = []
        cur = node
        while isinstance(cur, (ast.Call, ast.Attribute)):
            out.append(cur)
            cur = cur.func if isinstance(cur, ast.Call) else cur.value
        return out

    def _starter(self, chain: list[ast.AST]):
        for n in chain:
            if isinstance(n, ast.Attribute):
                m = self.model(n.value)
                if m and n.attr in self.scanner.models[m]["managers"]:
                    return m
        return None

    def state(self, expr: ast.AST | None):
        while isinstance(expr, ast.Await):
            expr = expr.value
        if isinstance(expr, ast.Name):
            return self.env.get(expr.id)
        if not isinstance(expr, ast.Call):
            return None
        func, name = expr.func, _tail(expr.func)
        m = self.model(func)
        if m:
            return "new", m, False
        has_instance = bool(expr.args) or _kwarg(expr, "instance") is not None
        ser = self.scanner.serializer_model(func, self.mod)
        if ser:
            return "ser", ser, has_instance
        if name in ("get_object_or_404", "get_object") and expr.args and not isinstance(func, ast.Attribute):
            for sub in ast.walk(expr.args[0]):
                got = self.model(sub) if isinstance(sub, (ast.Name, ast.Attribute)) else None
                if got:
                    return "loaded", got, False
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self" and self.model_ctx:
            if name == "get_object":
                return "loaded", self.model_ctx, False
            if name == "get_serializer":
                return "ser", self.model_ctx, has_instance
        chain = self.chain(expr)
        found = self._starter(chain)
        if found:
            terminal = name or ""
            kind = "loaded" if terminal in LOADED_TERMINALS else "qs"
            return kind, found, False
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
        found = self._starter(chain)
        if found:
            for op in _django_ops(names):
                self.emit(found, op, node)
            for n in chain:
                if isinstance(n, ast.Call):
                    self.handled.add(id(n))
            return
        func, name = node.func, _tail(node.func)
        if isinstance(func, ast.Name) and name in ("get_object_or_404", "get_list_or_404") and node.args:
            m = self.model(node.args[0])
            if m:
                self.emit(m, "SELECT", node)
            return
        if not isinstance(func, ast.Attribute):
            return
        owner = func.value
        if name in ("save", "delete", "update"):
            st = self.state(owner)
            if st:
                kind, model, flag = st
                if name == "save" and kind == "new":
                    self.emit(model, "INSERT", node)
                elif name == "save" and kind == "loaded":
                    self.emit(model, "UPDATE", node)
                elif name == "save" and kind == "ser":
                    self.emit(model, "UPDATE" if flag else "INSERT", node)
                elif name == "delete" and kind in ("loaded", "qs"):
                    self.emit(model, "DELETE", node)
                elif name == "update" and kind == "qs":
                    self.emit(model, "UPDATE", node)
                return
        # self.get_object() / super().create(...) / self.list(...): the framework's own behaviour
        is_self = isinstance(owner, ast.Name) and owner.id == "self"
        is_super = isinstance(owner, ast.Call) and _tail(owner.func) == "super"
        if self.model_ctx and (is_self or is_super):
            if name in ("get_queryset", "get_object") and is_self:
                self.emit(self.model_ctx, "SELECT", node)
            elif name in ACTION_OPS and (is_super or name not in self.ctx.get("defined", ())):
                for op in ACTION_OPS[name]:
                    self.emit(self.model_ctx, op, node)


# ------------------------------------------------------------------ scanner


class DjangoScanner(PyScanner):
    stack = "python/django"

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        super().__init__(root, graph, registry, openapi)
        # (module dotted, variable) -> {"trailing": bool, "regs": [(prefix, viewset expr, module)]}
        self.routers: dict[tuple[str, str], dict] = {}
        self._serializers: dict[int, str | None] = {}

    # ---------------------------------------------------------------- detection

    @staticmethod
    def detect(root: Path) -> bool:
        """True when this project uses Django."""
        base_depth = len(root.parts)
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if len(Path(current).parts) - base_depth >= 3:
                dirs[:] = []
            if "manage.py" in files and "DJANGO_SETTINGS_MODULE" in read(Path(current) / "manage.py"):
                return True
        return any(DJANGO_DEP_RE.search(read(path)) for path in dep_files(root))

    @staticmethod
    def register_tables(root: Path, registry: TableRegistry) -> None:
        """Register every concrete model's table (``Meta.db_table`` or ``<app>_<model>``) with columns."""
        for info in django_models(PyIndex(root)).values():
            if info["own"]:
                registry.add(info["table"], schema=None, kind="table", source=f"{info['rel']}:{info['line']}")
                registry.set_columns(info["table"], info["columns"], authoritative=False)
            for extra in info["extra_tables"]:
                registry.add(extra["table"], schema=None, kind="table", source=f"{info['rel']}:{info['line']}")
                registry.set_columns(extra["table"], extra["columns"], authoritative=False)

    # --------------------------------------------------------------------- ORM

    def orm_ops(self, func, tree, mod, cls, ctx):
        if not self.models:
            return []
        visitor = _DjVisitor(self, mod, func, ctx)
        visitor.visit(tree)
        return visitor.ops

    def serializer_model(self, expr: ast.AST, mod: PyMod) -> str | None:
        """The model a ModelSerializer class declares in ``Meta.model``."""
        found = self.index.class_of(expr, mod)
        if not found:
            return None
        key = id(found[1])
        if key not in self._serializers:
            self._serializers[key] = None
            chain, _ = self.index.class_chain(found[1], found[0])
            for m, c in chain:
                for item in c.body:
                    if isinstance(item, ast.ClassDef) and item.name == "Meta":
                        value = _class_attr(item, "model")
                        if value is not None and self.model_of(value, m):
                            self._serializers[key] = self.model_of(value, m)
                            break
                if self._serializers[key]:
                    break
        return self._serializers[key]

    def _first_model(self, node: ast.AST, mod: PyMod) -> str | None:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Name, ast.Attribute)):
                m = self.model_of(sub, mod)
                if m:
                    return m
        return None

    def _class_model(self, chain) -> str | None:
        """The model a view class works on: queryset / model / serializer / get_queryset."""
        for attr in ("queryset", "model"):
            for m, c in chain:
                value = _class_attr(c, attr)
                found = self._first_model(value, m) if value is not None else None
                if found:
                    return found
        for m, c in chain:
            value = _class_attr(c, "serializer_class")
            found = self.serializer_model(value, m) if value is not None else None
            if found:
                return found
        for m, c in chain:
            for item in c.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "get_queryset":
                    found = self._first_model(item, m)
                    if found:
                        return found
        return None

    # --------------------------------------------------------------------- scan

    def scan(self) -> None:
        self.models = django_models(self.index)
        self._index_routers()
        for mod in self._root_urlconfs():
            self._walk(mod, "", ())
        self.scan_layers()

    def _index_routers(self) -> None:
        for mod in self.index.mods.values():
            for node in ast.walk(mod.tree):
                if (
                    isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and (_tail(node.value.func) or "").endswith("Router")
                ):
                    trailing = _kwarg(node.value, "trailing_slash")
                    self.routers[(mod.dotted, node.targets[0].id)] = {
                        "trailing": not (isinstance(trailing, ast.Constant) and trailing.value is False),
                        "regs": [],
                    }
        for mod in self.index.mods.values():
            for node in ast.walk(mod.tree):
                if (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register" and len(node.args) >= 2
                ):
                    key = self.find_owner(node.func.value, mod, self.routers)
                    prefix = _const_str(node.args[0])
                    if key and prefix is not None:
                        self.routers[key]["regs"].append((prefix, node.args[1], mod))

    def _include_target(self, call: ast.Call, mod: PyMod) -> PyMod | None:
        arg = call.args[0] if call.args else None
        if isinstance(arg, (ast.Tuple, ast.List)) and arg.elts:
            arg = arg.elts[0]
        text = _const_str(arg)
        if text is not None:
            return self.index.find(text)
        found = self.index.resolve_expr(arg, mod) if arg is not None else None
        return found[0] if found and found[2] == "module" else None

    def _root_urlconfs(self) -> list[PyMod]:
        roots: list[PyMod] = []
        for mod in self.index.mods.values():
            target = _const_str(mod.constants.get("ROOT_URLCONF"))
            found = self.index.find(target) if target else None
            if found and found not in roots:
                roots.append(found)
        if roots:
            return roots
        urls_mods = [m for m in self.index.mods.values() if m.dotted.rsplit(".", 1)[-1] == "urls"]
        included: set[str] = set()
        for mod in urls_mods:
            for node in ast.walk(mod.tree):
                if isinstance(node, ast.Call) and _tail(node.func) == "include":
                    target = self._include_target(node, mod)
                    if target:
                        included.add(target.dotted)
        return [m for m in urls_mods if m.dotted not in included] or urls_mods

    # -- urlpatterns

    def _flatten(self, expr: ast.AST | None, mod: PyMod, depth: int = 0):
        """Yield ``("path", call)`` / ``("router", key)`` items of a urlpatterns expression."""
        if expr is None or depth > 6:
            return
        if isinstance(expr, (ast.List, ast.Tuple)):
            for e in expr.elts:
                yield from self._flatten(e, mod, depth + 1)
        elif isinstance(expr, ast.Starred):
            yield from self._flatten(expr.value, mod, depth + 1)
        elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            yield from self._flatten(expr.left, mod, depth + 1)
            yield from self._flatten(expr.right, mod, depth + 1)
        elif isinstance(expr, ast.Call):
            name = _tail(expr.func)
            if name in ("path", "re_path", "url"):
                yield "path", expr
            elif name == "get_urls" and isinstance(expr.func, ast.Attribute):
                key = self.find_owner(expr.func.value, mod, self.routers)
                if key:
                    yield "router", key
            elif name in ("format_suffix_patterns", "i18n_patterns"):
                for a in expr.args:
                    yield from self._flatten(a, mod, depth + 1)
        elif isinstance(expr, ast.Attribute) and expr.attr == "urls":
            key = self.find_owner(expr.value, mod, self.routers)
            if key:
                yield "router", key
        elif isinstance(expr, ast.Name) and expr.id in mod.constants and expr.id != "urlpatterns":
            yield from self._flatten(mod.constants[expr.id], mod, depth + 1)

    def _walk(self, mod: PyMod, prefix: str, stack: tuple) -> None:
        if mod.dotted in stack:
            return
        stack = stack + (mod.dotted,)
        for node in _toplevel(mod.tree.body):
            value = None
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "urlpatterns" for t in node.targets):
                value = node.value
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "urlpatterns":
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "urlpatterns":
                value = node.value
            if value is not None:
                self._items(self._flatten(value, mod), mod, prefix, stack)

    def _items(self, items, mod: PyMod, prefix: str, stack: tuple) -> None:
        for kind, payload in items:
            if kind == "router":
                self._router(payload, prefix)
                continue
            call: ast.Call = payload
            route = _const_str(call.args[0]) if call.args else None
            if route is None:
                continue
            piece = _regex_route(route) if _tail(call.func) in ("re_path", "url") else _dj_route(route)
            view = call.args[1] if len(call.args) > 1 else _kwarg(call, "view")
            self._view(view, mod, prefix + piece, call, stack)

    def _view(self, view: ast.AST | None, mod: PyMod, full: str, anchor: ast.Call, stack: tuple, depth: int = 0) -> None:
        if view is None or depth > 4:
            return
        if isinstance(view, ast.Call):
            name = _tail(view.func)
            if name == "include":
                arg = view.args[0] if view.args else None
                if isinstance(arg, ast.Tuple) and arg.elts:
                    arg = arg.elts[0]
                items = list(self._flatten(arg, mod)) if _const_str(arg) is None else []
                if items:
                    self._items(items, mod, full, stack)
                else:
                    target = self._include_target(view, mod)
                    if target:
                        self._walk(target, full, stack)
            elif name == "as_view" and isinstance(view.func, ast.Attribute):
                self._class_view(view.func.value, view, mod, full)
            elif view.args:  # csrf_exempt(login_required(view))
                self._view(view.args[0], mod, full, anchor, stack, depth + 1)
            return
        found = self.index.resolve_expr(view, mod)
        if found and found[2] == "func":
            self._function_view(found[1], found[0], full)

    # -- function views

    @staticmethod
    def _function_verbs(func) -> tuple[list[str], bool]:
        for deco in func.decorator_list:
            call = deco if isinstance(deco, ast.Call) else None
            name = _tail(call.func if call else deco)
            if name == "api_view":
                arg = call.args[0] if call and call.args else (_kwarg(call, "http_method_names") if call else None)
                return [v.upper() for v in _str_list(arg)] or ["GET"], False
            if name == "require_http_methods" and call and call.args:
                return [v.upper() for v in _str_list(call.args[0])] or ["GET"], False
            if name == "require_GET":
                return ["GET"], False
            if name == "require_POST":
                return ["POST"], False
            if name == "require_safe":
                return ["GET", "HEAD"], False
        return ["GET"], True

    def _function_view(self, func, fmod: PyMod, full: str) -> None:
        verbs, guess = self._function_verbs(func)
        for verb in verbs:
            self.add_route(verb, _path(full), label=func.name, func=_for_verb(func, verb), mod=fmod, cls=None,
                           group=group_of(fmod.rel), method_guess=True if guess else None)

    # -- class-based views and viewsets

    def _class_view(self, cls_expr: ast.AST, call: ast.Call, mod: PyMod, full: str) -> None:
        found = self.index.resolve_expr(cls_expr, mod)
        actions = call.args[0] if call.args and isinstance(call.args[0], ast.Dict) else None
        if not (found and found[2] == "class"):
            name = _tail(cls_expr) or "view"  # TemplateView.as_view(...) and friends
            self.add_route("GET", _path(full), label=name, func=None, mod=mod, cls=None,
                           group=group_of(mod.rel), anchor=call, method_guess=True)
            return
        cmod, cls = found[0], found[1]
        chain, ext = self.index.class_chain(cls, cmod)
        methods = self._methods(chain)
        view = _ViewInfo(self, cls, cmod, chain, ext, methods)
        if actions is not None:
            for key, value in zip(actions.keys, actions.values):
                verb, action = _const_str(key), _const_str(value)
                if verb and action:
                    self._emit(view, verb.upper(), _path(full), action, ACTION_OPS.get(action))
            return
        generic = {}
        for name in ext:
            generic.update(GENERIC_VERBS.get(name, {}))
        verbs = [v for v in HTTP_VERBS if v in methods or v in generic]
        for verb in verbs:
            self._emit(view, verb.upper(), _path(full), verb, generic.get(verb))

    @staticmethod
    def _methods(chain) -> dict[str, tuple[PyMod, ast.AST, ast.ClassDef]]:
        methods: dict[str, tuple[PyMod, ast.AST, ast.ClassDef]] = {}
        for m, c in chain:
            for item in c.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(item.name, (m, item, c))
        return methods

    def _emit(self, view: _ViewInfo, verb: str, path: str, action: str, defaults, handler=None) -> None:
        """One route of a class-based view; ``defaults`` are the ORM ops the framework performs itself."""
        found = handler or view.methods.get(action)
        func = found[1] if found else None
        extra_ops = []
        if defaults and view.model and (func is None or _calls_super(func)):
            table = self.models[view.model]["table"]
            extra_ops = [(table, op, f"{view.mod.rel}:{view.cls.lineno}") for op in defaults]
        extra_funcs = []
        perform = {"create": "perform_create", "update": "perform_update", "partial_update": "perform_update",
                   "destroy": "perform_destroy"}.get(action)
        if perform and perform in view.methods and (func is None or func is not view.methods[perform][1]):
            hm, hf, hc = view.methods[perform]
            extra_funcs.append((hf, hm, hc))
        label = f"{view.cls.name}.{func.name if func is not None else action}"
        self.add_route(
            verb, path, label=label, func=func, mod=found[0] if found else view.mod, cls=found[2] if found else view.cls,
            group=group_of(view.mod.rel), extra_refs=view.class_refs, extra_ops=extra_ops, extra_funcs=extra_funcs,
            anchor=view.cls, ctx={"model": view.model, "defined": set(view.methods)},
        )

    def _router(self, key: tuple[str, str], full: str) -> None:
        router = self.routers[key]
        slash = "/" if router["trailing"] else ""
        for prefix, expr, rmod in router["regs"]:
            found = self.index.resolve_expr(expr, rmod)
            if not (found and found[2] == "class"):
                continue
            cmod, cls = found[0], found[1]
            chain, ext = self.index.class_chain(cls, cmod)
            methods = self._methods(chain)
            view = _ViewInfo(self, cls, cmod, chain, ext, methods)
            lookup = "pk"
            for m, c in chain:
                value = _const_str(_class_attr(c, "lookup_url_kwarg")) or _const_str(_class_attr(c, "lookup_field"))
                if value:
                    lookup = value
                    break
            base = full + prefix.strip("/")
            provided = {a for name in ext for a in VIEWSET_BASES.get(name, [])}
            provided |= {a for a in VIEWSET_ACTIONS if a in methods}
            for action in VIEWSET_ACTIONS:
                if action not in provided:
                    continue
                verb, detail = VIEWSET_ACTIONS[action]
                path = f"{base}/{{{lookup}}}{slash}" if detail else f"{base}{slash}"
                self._emit(view, verb, _path(path), action, ACTION_OPS[action])
            for name, (m, func, c) in methods.items():
                for deco in func.decorator_list:
                    if not (isinstance(deco, ast.Call) and _tail(deco.func) == "action"):
                        continue
                    detail_kw = _kwarg(deco, "detail")
                    url_path = _const_str(_kwarg(deco, "url_path")) or name
                    verbs = [v.upper() for v in _str_list(_kwarg(deco, "methods"))] or ["GET"]
                    middle = f"/{{{lookup}}}" if isinstance(detail_kw, ast.Constant) and detail_kw.value else ""
                    for verb in verbs:
                        self._emit(view, verb, _path(f"{base}{middle}/{url_path}{slash}"), name, None, handler=(m, func, c))


class _ViewInfo:
    """What the scanner knows about one view class while emitting its routes."""

    def __init__(self, scanner: DjangoScanner, cls: ast.ClassDef, mod: PyMod, chain, ext, methods) -> None:
        self.cls = cls
        self.mod = mod
        self.chain = chain
        self.ext = ext
        self.methods = methods
        self.model = scanner._class_model(chain)
        # Services reached through ``__init__`` or class attributes apply to every verb.
        self.class_refs: list[tuple[ast.AST, PyMod]] = []
        for m, c in chain:
            for item in c.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or item.name == "__init__":
                    self.class_refs.append((item, m))


def _path(full: str) -> str:
    """``api/`` + ``orders/{pk}/`` pieces -> ``/api/orders/{pk}/``."""
    return re.sub(r"/{2,}", "/", "/" + full.lstrip("/"))
