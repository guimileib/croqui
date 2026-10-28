"""Language-level structure scanner: modules, entry points and who imports whom.

The other adapters read a framework; this one reads the code itself, so a CLI, a
library, a worker or a game still gets a map. Python goes through ``ast``; JS/TS,
Java/Kotlin and Go are read with regexes — enough to see an import, never enough to
run a compiler, and nothing is imported or executed.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..model import CALLS, Graph
from ..tables import TableRegistry
from ._common import TableHits, iter_files, line_of, read, wire_tables
from ._common import rel as rel_of

# Past this many source files one node per file is a hairball, so modules are
# merged per directory. 80 is about where a laid-out map stops fitting on a screen.
AGGREGATE_ABOVE = 80
# Third-party packages shown on the map, most imported first. A long tail of
# one-off utilities would bury the project's own structure.
MAX_EXTERNALS = 25
MAX_NAMES = 12
SUMMARY_CHARS = 160

EXTERNAL_GROUP = "dependências"

PY_SUFFIXES = (".py",)
JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
JVM_SUFFIXES = (".java", ".kt")
GO_SUFFIXES = (".go",)
SUFFIXES = PY_SUFFIXES + JS_SUFFIXES + JVM_SUFFIXES + GO_SUFFIXES

# Directories whose files are programs by convention, whatever they contain.
ENTRY_DIRS = {"bin", "scripts"}
# A file with one of these stems stands for its directory (``pkg/__init__.py``).
PACKAGE_STEMS = {"__init__", "index", "mod"}

NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http",
    "http2", "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream", "string_decoder", "sys",
    "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib",
}
JVM_STD_PREFIXES = ("java", "javax", "kotlin", "kotlinx")

# --------------------------------------------------------------------- patterns

# `import x from "y"`, `export * from "y"` (the clause may span lines), bare
# `import "y"`, and `require("y")` / dynamic `import("y")`.
JS_FROM_RE = re.compile(r"""\b(?:import|export)\b[^'";]*?\bfrom\s*(['"])([^'"\n]+)\1""")
JS_BARE_RE = re.compile(r"""\bimport\s*(['"])([^'"\n]+)\1""")
JS_CALL_RE = re.compile(r"""\b(?:require|import)\s*\(\s*(['"])([^'"\n]+)\1\s*\)""")
JS_CLASS_RE = re.compile(r"^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(\w+)", re.M)
JS_FUNC_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"
    r"|^export\s+(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>",
    re.M,
)
JS_MAIN_RE = re.compile(r"\brequire\.main\s*===\s*module\b")

JAVA_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;?\s*$", re.M)
JAVA_CLASS_RE = re.compile(r"^(?:[a-z]+\s+)*(?:class|interface|enum|object|record)\s+(\w+)", re.M)
JAVA_MAIN_RE = re.compile(r"\bstatic\s+void\s+main\s*\(")
KOTLIN_MAIN_RE = re.compile(r"^\s*(?:suspend\s+)?fun\s+main\s*\(", re.M)
KOTLIN_FUNC_RE = re.compile(r"^(?:(?:public|internal|suspend|inline)\s+)*fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(\w+)\s*\(", re.M)

GO_MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.M)
GO_IMPORT_BLOCK_RE = re.compile(r"^import\s*\((.*?)^\)", re.M | re.S)
GO_IMPORT_ONE_RE = re.compile(r"^import\s+(?:[\w.]+\s+)?\"([^\"]+)\"", re.M)
GO_IMPORT_LINE_RE = re.compile(r"^\s*(?:[\w.]+\s+)?\"([^\"]+)\"", re.M)
GO_TYPE_RE = re.compile(r"^type\s+(\w+)\s+(?:struct|interface)\b", re.M)
GO_FUNC_RE = re.compile(r"^func\s+([A-Z]\w*)\s*\(", re.M)
GO_MAIN_RE = re.compile(r"^func\s+main\s*\(\s*\)", re.M)
GO_PACKAGE_MAIN_RE = re.compile(r"^package\s+main\b", re.M)

# A statement, not a sentence that happens to contain "from": the registry decides
# whether the table is real, this only decides what looks like SQL at all.
SQL_STMT_RE = re.compile(
    r"\b(?:SELECT\b[^;\"'`]{0,300}?\bFROM|INSERT\s+INTO|UPDATE\s+[\w.\"`]+\s+SET|DELETE\s+FROM)\b"
    r"[^;\"'`]{0,300}",
    re.I | re.S,
)


# ------------------------------------------------------------------------ model


@dataclass
class Ref:
    """One import statement, as written."""

    spec: str
    line: int
    level: int = 0  # Python relative-import dots
    names: tuple[str, ...] = ()  # Python ``from x import a, b``


@dataclass
class Source:
    rel: str
    lang: str
    lines: int
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    summary: str | None = None
    refs: list[Ref] = field(default_factory=list)
    main_line: int | None = None  # where the file says "I am a program"
    sql: TableHits | None = None


@dataclass
class EntryPoint:
    rel: str
    how: str  # script | bin | main | main-guard | dir | module-main
    command: str | None = None
    line: int | None = None


# ---------------------------------------------------------------------- helpers


def _clip(text: str, limit: int = SUMMARY_CHARS) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _names(found: list[str]) -> list[str]:
    """First ``MAX_NAMES`` distinct names, in source order."""
    return list(dict.fromkeys(found))[:MAX_NAMES]


def leading_comment(text: str) -> str | None:
    """First line of the comment that opens a C-style file, ignoring licence boilerplate."""
    body = text.lstrip("﻿")
    if body.startswith("#!"):
        body = body.partition("\n")[2]
    body = body.lstrip()
    block = re.match(r"/\*+(.*?)\*/", body, re.S)
    if block:
        chunk = [ln.strip(" \t*") for ln in block.group(1).splitlines()]
    else:
        chunk = []
        for ln in body.splitlines():
            ln = ln.strip()
            if not ln.startswith("//") or ln.startswith(("//go:", "// +build")):
                break
            chunk.append(ln.lstrip("/ ").strip())
    first = next((ln for ln in chunk if ln and not ln.startswith("@")), None)
    if first is None or re.search(r"copyright|licen[sc]e", first, re.I):
        return None
    return _clip(first)


def _package_name(spec: str) -> str:
    """npm package from an import specifier: ``@scope/pkg/sub`` -> ``@scope/pkg``."""
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


# ---------------------------------------------------------------------- scanner


class StructureScanner:
    """Modules and entry points of any project, wired by their imports."""

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        self.root = root
        self.graph = graph
        self.registry = registry
        self.openapi_source = openapi
        self.warnings: list[str] = []
        self.sources: dict[str, Source] = {}
        self._py_index: dict[str, str] = {}
        self._jvm_index: dict[str, str] = {}
        self._go_module: str | None = None
        self._go_dirs: dict[str, list[str]] = {}

    @staticmethod
    def detect(root: Path) -> bool:
        """True when there is any source file in a language this scanner reads."""
        root = root.resolve()
        return any(
            not any(part.startswith(".") for part in p.relative_to(root).parts[:-1])
            for p in iter_files(root, SUFFIXES)
        )

    def _source_files(self, suffixes: tuple[str, ...]) -> list[Path]:
        """``iter_files`` minus dot-directories: ``.planning/``, ``.github/`` are tooling, not the project."""
        return [
            p for p in iter_files(self.root, suffixes)
            if not any(part.startswith(".") for part in p.relative_to(self.root).parts[:-1])
        ]

    # ----------------------------------------------------------------- scan

    def scan(self) -> None:
        for path in self._source_files(SUFFIXES):
            if path.name.endswith("_test.go"):
                continue
            src = self._parse(path)
            if src is not None:
                self.sources[src.rel] = src
        if not self.sources:
            return

        self._build_indexes()
        entries = self._find_entries()
        aggregate = len(self.sources) > AGGREGATE_ABOVE
        node_of = self._add_modules(aggregate)

        imports: dict[tuple[str, str], str] = {}  # (from module, to module) -> evidence
        externals: dict[str, dict[str, str]] = {}  # package -> {from module: evidence}
        for src in self.sources.values():
            for ref in src.refs:
                targets, package = self._resolve(src, ref)
                evidence = f"{src.rel}:{ref.line}"
                for target in targets:
                    a, b = node_of[src.rel], node_of[target]
                    if a != b:
                        imports.setdefault((a, b), evidence)
                if package:
                    externals.setdefault(package, {}).setdefault(node_of[src.rel], evidence)

        for (a, b), evidence in imports.items():
            self.graph.add_edge(a, b, CALLS, evidence=evidence)
        self._add_externals(externals)
        self._add_entries(entries, node_of)
        self._add_tables(node_of)

    # ---------------------------------------------------------------- parse

    def _parse(self, path: Path) -> Source | None:
        rel = rel_of(self.root, path)
        text = read(path)
        suffix = path.suffix
        lang = (
            "python" if suffix in PY_SUFFIXES
            else "go" if suffix in GO_SUFFIXES
            else "kotlin" if suffix == ".kt"
            else "java" if suffix == ".java"
            else "typescript" if suffix in (".ts", ".tsx")
            else "javascript"
        )
        src = Source(rel, lang, len(text.splitlines()))
        if lang == "python":
            self._parse_python(src, text)
        elif lang in ("javascript", "typescript"):
            self._parse_js(src, text)
        elif lang in ("java", "kotlin"):
            self._parse_jvm(src, text)
        else:
            self._parse_go(src, text)

        hits = TableHits(self.registry)
        for m in SQL_STMT_RE.finditer(text):
            hits.add_sql(m.group(0), f"{rel}:{line_of(text, m.start())}")
        src.sql = hits or None
        return src

    def _parse_python(self, src: Source, text: str) -> None:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            self.warnings.append(f"skip {src.rel}: {exc.__class__.__name__}")
            return
        doc = ast.get_docstring(tree)
        if doc:
            src.summary = _clip(doc.strip().splitlines()[0])
        classes, functions = [], []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                functions.append(node.name)
            elif isinstance(node, ast.If) and _is_main_guard(node.test):
                src.main_line = node.lineno
        src.classes, src.functions = _names(classes), _names(functions)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                src.refs.extend(Ref(alias.name, node.lineno) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names = tuple(a.name for a in node.names if a.name != "*")
                src.refs.append(Ref(node.module or "", node.lineno, node.level, names))

    def _parse_js(self, src: Source, text: str) -> None:
        src.summary = leading_comment(text)
        src.classes = _names(JS_CLASS_RE.findall(text))
        src.functions = _names([a or b for a, b in JS_FUNC_RE.findall(text)])
        for pattern in (JS_FROM_RE, JS_BARE_RE, JS_CALL_RE):
            for m in pattern.finditer(text):
                src.refs.append(Ref(m.group(2), line_of(text, m.start())))
        if text.startswith("#!") and "node" in text.partition("\n")[0]:
            src.main_line = 1
        elif (m := JS_MAIN_RE.search(text)):
            src.main_line = line_of(text, m.start())

    def _parse_jvm(self, src: Source, text: str) -> None:
        src.summary = leading_comment(text)
        src.classes = _names(JAVA_CLASS_RE.findall(text))
        if src.lang == "kotlin":
            src.functions = _names(KOTLIN_FUNC_RE.findall(text))
        main = (KOTLIN_MAIN_RE if src.lang == "kotlin" else JAVA_MAIN_RE).search(text)
        if main:
            src.main_line = line_of(text, main.start())
        for m in JAVA_IMPORT_RE.finditer(text):
            src.refs.append(Ref(m.group(1), line_of(text, m.start())))

    def _parse_go(self, src: Source, text: str) -> None:
        src.summary = leading_comment(text)
        src.classes = _names(GO_TYPE_RE.findall(text))
        src.functions = _names(GO_FUNC_RE.findall(text))
        main = GO_MAIN_RE.search(text)
        if main and GO_PACKAGE_MAIN_RE.search(text):
            src.main_line = line_of(text, main.start())
        for block in GO_IMPORT_BLOCK_RE.finditer(text):
            for m in GO_IMPORT_LINE_RE.finditer(block.group(1)):
                src.refs.append(Ref(m.group(1), line_of(text, block.start(1) + m.start())))
        for m in GO_IMPORT_ONE_RE.finditer(text):
            src.refs.append(Ref(m.group(1), line_of(text, m.start())))

    # -------------------------------------------------------------- indexes

    def _build_indexes(self) -> None:
        for rel in self.sources:
            src = self.sources[rel]
            if src.lang == "python":
                parts = rel[: -len(".py")].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if not parts:
                    continue
                self._py_index.setdefault(".".join(parts), rel)
                # src/ layout: the package is importable without the wrapper dir.
                if parts[0] == "src" and len(parts) > 1:
                    self._py_index.setdefault(".".join(parts[1:]), rel)
        for rel in self.sources:
            src = self.sources[rel]
            if src.lang in ("java", "kotlin"):
                text = read(self.root / rel)
                pkg = JAVA_PACKAGE_RE.search(text)
                prefix = f"{pkg.group(1)}." if pkg else ""
                stem = posixpath.splitext(posixpath.basename(rel))[0]
                # Kotlin files may hold several classes; any of them names the file.
                for name in [stem, *src.classes]:
                    self._jvm_index.setdefault(prefix + name, rel)
            elif src.lang == "go":
                self._go_dirs.setdefault(posixpath.dirname(rel), []).append(rel)
        go_mod = self.root / "go.mod"
        if go_mod.is_file():
            m = GO_MODULE_RE.search(read(go_mod))
            self._go_module = m.group(1) if m else None

    # -------------------------------------------------------------- resolve

    def _resolve(self, src: Source, ref: Ref) -> tuple[list[str], str | None]:
        """(project files this import points at, third-party package) — never both."""
        if src.lang == "python":
            return self._resolve_python(src, ref)
        if src.lang in ("javascript", "typescript"):
            return self._resolve_js(src, ref)
        if src.lang == "go":
            return self._resolve_go(ref)
        return self._resolve_jvm(ref)

    def _resolve_python(self, src: Source, ref: Ref) -> tuple[list[str], str | None]:
        module = [p for p in ref.spec.split(".") if p]
        if ref.level:
            package = src.rel.split("/")[:-1]
            if ref.level > 1:
                package = package[: len(package) - (ref.level - 1)]
            module = package + module
        else:
            top = module[0] if module else ""
            local = any(k.split(".")[0] == top for k in self._py_index)
            if not local:
                stdlib = top in sys.stdlib_module_names or top == "__future__"
                return [], (None if stdlib or not top else top)

        # ``from pkg import a, b`` names submodules when they exist as files, and
        # plain attributes of pkg/__init__.py when they do not.
        found = [self._py_index[d] for n in ref.names if (d := ".".join(module + [n])) in self._py_index]
        if found:
            return [f for f in found if f != src.rel], None
        for end in range(len(module), 0, -1):
            hit = self._py_index.get(".".join(module[:end]))
            if hit:
                return ([] if hit == src.rel else [hit]), None
        return [], None

    def _resolve_js(self, src: Source, ref: Ref) -> tuple[list[str], str | None]:
        spec = ref.spec
        if spec.startswith("node:"):
            return [], None
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(src.rel), spec))
            hit = self._js_file(base)
            return ([hit] if hit and hit != src.rel else []), None
        # Aliases (`@/x`, `~/x`, `#x`) are the project's own, not a dependency.
        if spec.startswith(("@/", "~", "#", "/")):
            return [], None
        package = _package_name(spec)
        return [], (None if package in NODE_BUILTINS else package)

    def _js_file(self, base: str) -> str | None:
        """File an extension-less or directory import lands on, or None."""
        stem = base
        # TS ESM writes `./x.js` for what is on disk as `x.ts`.
        root, ext = posixpath.splitext(base)
        if ext in (".js", ".mjs", ".cjs", ".jsx"):
            stem = root
        candidates = [base] + [stem + e for e in JS_SUFFIXES] + [f"{base}/index{e}" for e in JS_SUFFIXES]
        return next((c for c in candidates if c in self.sources), None)

    def _resolve_jvm(self, ref: Ref) -> tuple[list[str], str | None]:
        parts = ref.spec.split(".")
        # Drop trailing segments until a class matches: nested classes, static members
        # and Kotlin top-level functions are all imported through their file's class.
        for end in range(len(parts), 1, -1):
            hit = self._jvm_index.get(".".join(parts[:end]))
            if hit:
                return [hit], None
        if parts[0] in JVM_STD_PREFIXES:
            return [], None
        head = ".".join(parts[:2])
        # An import of our own package that matched no file (wildcard, generated code)
        # is not a dependency.
        if any(k.startswith(head + ".") or k == head for k in self._jvm_index):
            return [], None
        return [], head

    def _resolve_go(self, ref: Ref) -> tuple[list[str], str | None]:
        spec = ref.spec
        module = self._go_module
        if module and (spec == module or spec.startswith(module + "/")):
            directory = spec[len(module):].strip("/")
            files = self._go_dirs.get(directory) or []
            # A Go import names a package (a directory); one representative file keeps
            # the edge single: the file named like the directory, else the first.
            named = [f for f in files if posixpath.splitext(posixpath.basename(f))[0] == posixpath.basename(directory)]
            return (named or files)[:1], None
        if "." not in spec.split("/")[0]:
            return [], None  # standard library
        return [], "/".join(spec.split("/")[:3])

    # -------------------------------------------------------------- entries

    def _find_entries(self) -> dict[str, EntryPoint]:
        """Files that are programs, keyed by path. The most specific reason wins."""
        found: dict[str, EntryPoint] = {}

        def add(rel: str | None, how: str, command: str | None = None, line: int | None = None) -> None:
            if rel and rel in self.sources and rel not in found:
                found[rel] = EntryPoint(rel, how, command, line)

        for path in self._source_files((".toml", ".py", ".json")):
            if path.name == "pyproject.toml":
                for name, target in _pyproject_scripts(read(path)):
                    add(self._py_target(target), "script", name)
            elif path.name == "setup.py":
                for name, target in _setup_scripts(read(path)):
                    add(self._py_target(target), "script", name)
            elif path.name == "package.json":
                self._package_json_entries(path, add)

        for rel, src in self.sources.items():
            parts = rel.split("/")
            if posixpath.basename(rel) == "__main__.py":
                package = ".".join(parts[:-1])
                add(rel, "module-main", f"python -m {package}" if package else None)
            if ENTRY_DIRS & set(parts[:-1]):
                add(rel, "dir")
            if src.main_line:
                add(rel, "main-guard" if src.lang == "python" else "main", None, src.main_line)
        return found

    def _py_target(self, spec: str) -> str | None:
        """``pkg.cli:main`` -> the file of ``pkg.cli``."""
        return self._py_index.get(spec.split(":")[0].strip())

    def _package_json_entries(self, path: Path, add) -> None:
        try:
            data = json.loads(read(path))
        except json.JSONDecodeError:
            self.warnings.append(f"skip {rel_of(self.root, path)}: invalid JSON")
            return
        if not isinstance(data, dict):
            return
        base = posixpath.dirname(rel_of(self.root, path))
        bins = data.get("bin")
        if isinstance(bins, str):
            bins = {data.get("name") or "bin": bins}
        for name, target in (bins.items() if isinstance(bins, dict) else []):
            if isinstance(target, str):
                add(self._js_file(posixpath.normpath(posixpath.join(base, target))), "bin", name)
        main = data.get("main")
        if isinstance(main, str):
            add(self._js_file(posixpath.normpath(posixpath.join(base, main))), "main")

    # ---------------------------------------------------------------- nodes

    def _add_modules(self, aggregate: bool) -> dict[str, str]:
        """Create one node per file (or per directory) and return file path -> node id."""
        strip = self._group_strip()
        units: dict[str, list[Source]] = {}
        for rel, src in self.sources.items():
            units.setdefault(posixpath.dirname(rel) if aggregate else rel, []).append(src)

        labels = _labels(list(units), aggregate, self.root.name)
        node_of: dict[str, str] = {}
        for key, members in units.items():
            node_id = f"module:{(key or '.') + '/'}" if aggregate else f"module:{key}"
            for src in members:
                node_of[src.rel] = node_id
            group = _group(key, strip, aggregate)
            langs = sorted({s.lang for s in members})
            meta: dict = {
                "file": f"{key or '.'}/" if aggregate else key,
                "lang": langs[0] if len(langs) == 1 else None,
                "langs": langs if len(langs) > 1 else None,
                "lines": sum(s.lines for s in members),
                "classes": _names([c for s in members for c in s.classes]) or None,
                "functions": _names([f for s in members for f in s.functions]) or None,
                "summary": self._summary_of(members, aggregate),
            }
            if aggregate:
                meta["files"] = len(members)
            self.graph.add_node(node_id, "module", labels[key], group=group, **meta)
        return node_of

    @staticmethod
    def _summary_of(members: list[Source], aggregate: bool) -> str | None:
        if not aggregate:
            return members[0].summary
        # A directory is described by the file that stands for it, if it has one.
        for src in members:
            if posixpath.splitext(posixpath.basename(src.rel))[0] in PACKAGE_STEMS and src.summary:
                return src.summary
        return None

    def _group_strip(self) -> str:
        """Leading directory every nested file shares (``croqui/``), dropped from groups.

        Without this a single-package project has one group holding the whole map.
        """
        nested = [r.split("/") for r in self.sources if "/" in r]
        if nested and len({p[0] for p in nested}) == 1:
            return nested[0][0]
        return ""

    def _add_externals(self, externals: dict[str, dict[str, str]]) -> None:
        ranked = sorted(externals, key=lambda p: (-len(externals[p]), p))
        for package in ranked[:MAX_EXTERNALS]:
            ext_id = f"external:{package}"
            self.graph.add_node(ext_id, "external", package, group=EXTERNAL_GROUP)
            for node_id, evidence in externals[package].items():
                self.graph.add_edge(node_id, ext_id, CALLS, evidence=evidence)
        rest = ranked[MAX_EXTERNALS:]
        if rest:
            shown = ", ".join(rest[:5]) + ("…" if len(rest) > 5 else "")
            self.warnings.append(f"{len(rest)} less-imported third-party packages left off the map ({shown})")

    def _add_entries(self, entries: dict[str, EntryPoint], node_of: dict[str, str]) -> None:
        labels = _entry_labels(entries)
        for rel, entry in entries.items():
            src = self.sources[rel]
            module_id = node_of[rel]
            entry_id = f"entry:{rel}"
            module = self.graph.nodes[module_id]
            line = entry.line or src.main_line
            self.graph.add_node(
                entry_id,
                "entry",
                labels[rel],
                group=module["group"],
                file=rel,
                lang=src.lang,
                how=entry.how,
                command=entry.command,
                line=line,
                summary=src.summary,
            )
            self.graph.add_edge(entry_id, module_id, CALLS, evidence=f"{rel}:{line}" if line else rel)

    def _add_tables(self, node_of: dict[str, str]) -> None:
        """Inline SQL against tables the registry knows, merged onto the module's node."""
        merged: dict[str, TableHits] = {}
        for src in self.sources.values():
            if not src.sql:
                continue
            target = merged.setdefault(node_of[src.rel], TableHits(self.registry))
            for name, info in src.sql.hits.items():
                for op in info["ops"]:
                    for evidence in info["evidence"]:
                        target.add(name, op, evidence)
        for node_id, hits in merged.items():
            wire_tables(self.graph, node_id, hits)


# -------------------------------------------------------------- free functions


def _is_main_guard(test: ast.AST) -> bool:
    """``__name__ == "__main__"`` in either order."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, test.comparators[0]]
    has_name = any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
    has_main = any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)
    return has_name and has_main


def _pyproject_scripts(text: str) -> list[tuple[str, str]]:
    """``name = "pkg.mod:func"`` pairs of [project.scripts] / [tool.poetry.scripts].

    Read by hand: ``tomllib`` only exists from 3.11 and this package supports 3.10.
    """
    out: list[tuple[str, str]] = []
    active = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            active = stripped.strip("[] ") in ("project.scripts", "tool.poetry.scripts")
            continue
        m = re.match(r"""["']?([\w.-]+)["']?\s*=\s*["']([\w.]+(?::[\w.]+)?)["']""", stripped)
        if active and m:
            out.append((m.group(1), m.group(2)))
    return out


def _setup_scripts(text: str) -> list[tuple[str, str]]:
    """``"name=pkg.mod:func"`` strings of setup.py console_scripts."""
    if "console_scripts" not in text:
        return []
    return re.findall(r"""["']([\w.-]+)\s*=\s*([\w.]+:[\w.]+)["']""", text)


def _group(key: str, strip: str, aggregate: bool) -> str:
    """Top-level directory the file or directory lives in, or "app" at the root."""
    parts = key.split("/") if key else []
    if not aggregate:
        parts = parts[:-1]
    if strip and parts and parts[0] == strip:
        parts = parts[1:]
    return parts[0] if parts else "app"


def _labels(keys: list[str], is_dir: bool, project: str) -> dict[str, str]:
    """Short readable label per file/dir: the name, widened with parents only on collisions."""

    def base(key: str) -> list[str]:
        parts = key.split("/") if key else [project or "app"]
        if not is_dir:
            stem = posixpath.splitext(parts[-1])[0]
            parts = parts[:-1] + [stem]
            if stem in PACKAGE_STEMS and len(parts) > 1:
                parts = parts[:-1]  # `pkg/__init__.py` is called `pkg`
        return parts

    parts_of = {k: base(k) for k in keys}
    out: dict[str, str] = {}
    for depth in (1, 2, 99):
        pending = [k for k in keys if k not in out]
        names = {k: "/".join(parts_of[k][-depth:]) for k in pending}
        counts: dict[str, int] = {}
        for k in keys:
            counts["/".join(parts_of[k][-depth:])] = counts.get("/".join(parts_of[k][-depth:]), 0) + 1
        for k in pending:
            if counts[names[k]] == 1 or depth == 99:
                out[k] = names[k]
    return out


def _entry_labels(entries: dict[str, EntryPoint]) -> dict[str, str]:
    """A declared command name when there is one, else the file's name; widened on collisions."""
    wanted = {rel: e.command or _labels([rel], False, "")[rel] for rel, e in entries.items()}
    seen: dict[str, int] = {}
    for label in wanted.values():
        seen[label] = seen.get(label, 0) + 1
    return {
        rel: label if seen[label] == 1 else posixpath.splitext(rel)[0]
        for rel, label in wanted.items()
    }
