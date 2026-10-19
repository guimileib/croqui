"""JavaScript / TypeScript parsing helpers shared by the Express and NestJS scanners.

No real parser: a small lexer blanks comments and string contents so brackets can be
matched safely, and regexes run on that "skeleton". The skeleton has the same length
as the source, so an offset found in it indexes the comment-free ``code`` (strings
intact) and the original text alike.

ORM table registration (Prisma, TypeORM, Sequelize, Knex migrations) and table-access
detection live here too, because both scanners need the same model -> table maps.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from ._common import SKIP_DIRS, TableHits, line_of, rel, sql_refs

JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts")
SKIP_FILE_RE = re.compile(r"\.(d|spec|test|e2e-spec|stories)\.(ts|js)$|\.config\.(ts|js|mjs|cjs)$")
EXT_TRY = (".ts", ".js", ".mjs", ".cjs")
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")

# --------------------------------------------------------------------------- lexer


@dataclass
class Lexed:
    """Source split three ways, all the same length: original, comment-free, skeleton."""

    text: str
    code: str  # comments blanked, strings intact
    skel: str  # comments blanked and string/regex contents blanked
    literals: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end, raw inner text)


_NEXT = re.compile(r"""//|/\*|['"`/]""")
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KW = {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do", "yield", "await"}
_NOT_NL = re.compile(r"[^\n]")


def _blank(chars: list[str], a: int, b: int) -> None:
    for k in range(a, b):
        if chars[k] != "\n":
            chars[k] = " "


def _skip_template(text: str, i: int) -> int:
    """Index just past the template literal opening at ``text[i] == '`'`."""
    n = len(text)
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1
        if c == "$" and text[j + 1 : j + 2] == "{":
            j = _skip_braces(text, j + 1)
            continue
        j += 1
    return n


def _skip_braces(text: str, i: int) -> int:
    """Index past the ``{...}`` of a template substitution starting at ``text[i] == '{'``."""
    n = len(text)
    depth = 0
    j = i
    while j < n:
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        elif c in "'\"":
            j += 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
        elif c == "`":
            j = _skip_template(text, j)
            continue
        j += 1
    return n


def lex(text: str) -> Lexed:
    n = len(text)
    code = list(text)
    skel = list(text)
    lits: list[tuple[int, int, str]] = []
    i = 0
    while i < n:
        m = _NEXT.search(text, i)
        if not m:
            break
        i = m.start()
        tok = m.group()
        if tok == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank(code, i, j)
            _blank(skel, i, j)
            i = j
        elif tok == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            _blank(code, i, j)
            _blank(skel, i, j)
            i = j
        elif tok in "'\"":
            j = i + 1
            while j < n and text[j] != tok and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            if j >= n or text[j] != tok:  # unterminated: just a stray quote
                i += 1
                continue
            lits.append((i, j + 1, text[i + 1 : j]))
            _blank(skel, i + 1, j)
            i = j + 1
        elif tok == "`":
            j = _skip_template(text, i)
            lits.append((i, j, text[i + 1 : max(i + 1, j - 1)]))
            _blank(skel, i + 1, max(i + 1, j - 1))
            i = j
        else:  # '/': division or a regex literal
            k = i - 1
            while k >= 0 and code[k] in " \t\r\n":
                k -= 1
            prev = code[k] if k >= 0 else ""
            regex_ok = not prev or prev in _REGEX_PREV
            if not regex_ok and (prev.isalnum() or prev in "_$"):
                w = k
                while w >= 0 and (code[w].isalnum() or code[w] in "_$"):
                    w -= 1
                regex_ok = "".join(code[w + 1 : k + 1]) in _REGEX_KW
            end = -1
            if regex_ok:
                j = i + 1
                in_class = False
                while j < n:
                    c = text[j]
                    if c == "\n":
                        break
                    if c == "\\":
                        j += 2
                        continue
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        end = j
                        break
                    j += 1
            if end > 0:
                _blank(skel, i + 1, end)
                i = end + 1
            else:
                i += 1
    return Lexed(text, "".join(code), "".join(skel), lits)


_LEX_CACHE: dict[tuple[str, int, int], Lexed] = {}


def lex_file(path: Path) -> Lexed:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _LEX_CACHE.get(key)
    if hit is None:
        hit = lex(path.read_text(encoding="utf-8", errors="replace"))
        _LEX_CACHE[key] = hit
    return hit


# ------------------------------------------------------------------ bracket helpers

_OPEN = "([{"
_CLOSE = ")]}"


def match_close(skel: str, i: int) -> int:
    """Index of the bracket closing the one at ``skel[i]``, or -1."""
    depth = 0
    for j in range(i, len(skel)):
        c = skel[j]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
            if depth == 0:
                return j
    return -1


def match_open_back(skel: str, i: int) -> int:
    """Index of the bracket opening the one that closes at ``skel[i]``, or -1."""
    depth = 0
    for j in range(i, -1, -1):
        c = skel[j]
        if c in _CLOSE:
            depth += 1
        elif c in _OPEN:
            depth -= 1
            if depth == 0:
                return j
    return -1


def split_top(skel: str, s: int, e: int) -> list[tuple[int, int]]:
    """Comma-separated spans at bracket depth 0 inside ``skel[s:e]``, trimmed, empties dropped."""
    out: list[tuple[int, int]] = []
    depth = 0
    start = s
    for j in range(s, e):
        c = skel[j]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == "," and depth == 0:
            out.append((start, j))
            start = j + 1
    out.append((start, e))
    spans = []
    for a, b in out:
        while a < b and skel[a].isspace():
            a += 1
        while b > a and skel[b - 1].isspace():
            b -= 1
        if b > a:
            spans.append((a, b))
    return spans


def skip_ws(skel: str, i: int, end: int | None = None) -> int:
    end = len(skel) if end is None else end
    while i < end and skel[i].isspace():
        i += 1
    return i


def skip_angles(skel: str, i: int) -> int:
    """Past a ``<...>`` generic argument list starting at ``skel[i] == '<'`` (``=>`` ignored)."""
    depth = 0
    n = len(skel)
    j = i
    while j < n:
        c = skel[j]
        if c == "<":
            depth += 1
        elif c == ">" and skel[j - 1 : j] != "=":
            depth -= 1
            if depth == 0:
                return j + 1
        elif c == ";":
            return i + 1
        j += 1
    return i + 1


def after_signature(skel: str, i: int, end: int) -> int:
    """From just past a parameter list: skip an optional ``: ReturnType`` and return the index
    of the ``{`` body brace, or -1 when the member has no body."""
    j = skip_ws(skel, i, end)
    if j < end and skel[j] == ":":
        depth = 0
        angle = 0
        last = ":"
        j += 1
        while j < end:
            c = skel[j]
            if c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            elif c == "<":
                angle += 1
            elif c == ">" and skel[j - 1] != "=":
                angle -= 1
            elif c == "{" and depth == 0 and angle <= 0:
                if last in ":|&":  # an inline object type, not the body
                    k = match_close(skel, j)
                    if k < 0:
                        return -1
                    j = k + 1
                    last = "}"
                    continue
                return j
            elif c == ";" and depth == 0 and angle <= 0:
                return -1
            if not c.isspace():
                last = c
            j += 1
        return -1
    return j if j < end and skel[j] == "{" else -1


_CONT_NEXT = set(".?:+-*/&|=,<>")
_CONT_PREV = set("=,(+-*/&|?:>[{")


def expr_end(skel: str, i: int, stop_comma: bool = False, limit: int | None = None) -> int:
    """End offset of the expression/statement starting at ``i`` (exclusive)."""
    n = len(skel) if limit is None else limit
    depth = 0
    j = i
    while j < n:
        c = skel[j]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            if depth == 0:
                return j
            depth -= 1
        elif depth == 0 and c == ";":
            return j
        elif depth == 0 and stop_comma and c == ",":
            return j
        elif depth == 0 and c == "\n":
            k = j - 1
            while k >= i and skel[k] in " \t\r":
                k -= 1
            nxt = skip_ws(skel, j, n)
            if k < i or skel[k] not in _CONT_PREV:
                if nxt >= n or skel[nxt] not in _CONT_NEXT or skel[nxt : nxt + 2] in ("//", "/*"):
                    return j
        j += 1
    return n


# --------------------------------------------------------------- literal utilities


def str_value(expr: str) -> str | None:
    """Value of a plain quoted string / ``${}``-free template literal expression, else None."""
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in "'\"`":
        inner = expr[1:-1]
        if expr[0] == "`" and "${" in inner:
            return None
        if expr[0] != "`" and expr[0] in inner.replace("\\" + expr[0], ""):
            return None
        return inner
    return None


def str_list(expr: str) -> list[str]:
    """Strings of ``"a"`` or ``["a", "b"]``; empty when the expression is not literal."""
    expr = expr.strip()
    if expr.startswith("["):
        lx = lex(expr)
        end = match_close(lx.skel, 0)
        out = []
        for a, b in split_top(lx.skel, 1, end if end > 0 else len(expr)):
            v = str_value(expr[a:b])
            if v is not None:
                out.append(v)
        return out
    v = str_value(expr)
    return [v] if v is not None else []


def parse_object(expr: str) -> dict[str, str]:
    """Top-level ``key: value`` pairs of an object literal expression (values are code text)."""
    expr = expr.strip()
    if not expr.startswith("{"):
        return {}
    lx = lex(expr)
    end = match_close(lx.skel, 0)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for a, b in split_top(lx.skel, 1, end):
        piece = lx.skel[a:b]
        if piece.startswith("..."):
            continue
        colon = _top_colon(piece)
        if colon < 0:
            m = IDENT_RE.fullmatch(piece.strip())
            if m:
                out[m.group()] = m.group()
            continue
        key = lx.code[a : a + colon].strip().strip("'\"`")
        if key.startswith("[") or not key:
            continue
        out[key] = lx.code[a + colon + 1 : b].strip()
    return out


def _top_colon(piece: str) -> int:
    depth = 0
    for j, c in enumerate(piece):
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == ":" and depth == 0:
            return j
    return -1


def snake_case(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def pluralize(name: str) -> str:
    """Sequelize's default is the pluralized model name; this is the simple version of it."""
    low = name.lower()
    if low.endswith("s"):
        return low
    if low.endswith("y") and len(low) > 1 and low[-2] not in "aeiou":
        return low[:-1] + "ies"
    return low + "s"


def pascal(text: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", text) if p]
    return "".join(p[0].upper() + p[1:] for p in parts)


# ------------------------------------------------------------------ doc comments

_TAG_LINE = re.compile(r"^\s*@\w+")


def doc_before(text: str, pos: int) -> tuple[str | None, str | None]:
    """(summary, description) from a ``/** ... */`` block that ends right before ``pos``."""
    head = text[max(0, pos - 4000) : pos]
    m = re.search(r"/\*\*(.*?)\*/\s*$", head, re.S)
    if not m:
        return None, None
    lines = []
    for raw in m.group(1).splitlines():
        line = re.sub(r"^\s*\*?\s?", "", raw).rstrip()
        if _TAG_LINE.match(line):
            break
        lines.append(line)
    body = "\n".join(lines).strip()
    if not body:
        return None, None
    return body.splitlines()[0].strip(), body


# -------------------------------------------------------------------- source files


@dataclass
class Import:
    local: str
    spec: str
    name: str  # "default" for default / whole-module / namespace, else the imported name
    target: Path | None = None


@dataclass
class Deco:
    name: str
    args: list[str]  # code text of each top-level argument
    raw: str  # code text between the parentheses
    pos: int


@dataclass
class Param:
    name: str
    type: str | None
    decos: list[Deco]
    prop: bool  # has an access modifier -> constructor parameter property


@dataclass
class Member:
    kind: str  # "method" | "prop"
    name: str
    pos: int
    decos: list[Deco]
    params: list[Param] = field(default_factory=list)
    body: tuple[int, int] | None = None  # span inside the braces (exclusive of them)
    type: str | None = None
    start: int = 0  # first decorator / header offset (for doc comments)


@dataclass
class ClassInfo:
    name: str
    js: "JsFile"
    pos: int
    start: int  # first decorator or `export`
    body: tuple[int, int]  # indices of `{` and matching `}`
    decos: list[Deco]
    extends: str | None
    members: list[Member]

    def deco(self, name: str) -> Deco | None:
        return next((d for d in self.decos if d.name == name), None)

    @property
    def ctor_params(self) -> list[Param]:
        for m in self.members:
            if m.kind == "method" and m.name == "constructor":
                return m.params
        return []


_IMPORT_RE = re.compile(r"""\bimport\s+(?!type\b)(?P<clause>[^;'"=]*?)\s*from\s*['"](?P<spec>[^'"]+)['"]""", re.S)
_REQUIRE_RE = re.compile(
    r"""(?:const|let|var)\s+(?P<lhs>[\w$]+|\{[^}]*\})\s*(?::[^=]+)?=\s*(?:await\s+)?require\(\s*['"](?P<spec>[^'"]+)['"]\s*\)(?:\.(?P<tail>[\w$]+))?"""
)


class JsFile:
    def __init__(self, project: "JsProject", path: Path) -> None:
        self.project = project
        self.path = path
        self.rel = rel(project.root, path)
        lx = lex_file(path)
        self.text, self.code, self.skel, self.literals = lx.text, lx.code, lx.skel, lx.literals

    def line(self, offset: int) -> int:
        return line_of(self.text, offset)

    # ---- imports
    @cached_property
    def imports(self) -> dict[str, Import]:
        out: dict[str, Import] = {}
        for m in _IMPORT_RE.finditer(self.code):
            spec, clause = m.group("spec"), m.group("clause").strip()
            named = re.search(r"\{([^}]*)\}", clause)
            head = clause.split("{")[0].strip().rstrip(",").strip()
            if head:
                ns = re.match(r"\*\s+as\s+([\w$]+)", head)
                if ns:
                    out[ns.group(1)] = Import(ns.group(1), spec, "default")
                else:
                    out[head.split(",")[0].strip()] = Import(head.split(",")[0].strip(), spec, "default")
            if named:
                for part in named.group(1).split(","):
                    bits = re.split(r"\s+as\s+", part.strip().replace("type ", "", 1))
                    if bits and bits[0]:
                        local = bits[-1].strip()
                        out[local] = Import(local, spec, bits[0].strip())
        for m in _REQUIRE_RE.finditer(self.code):
            spec, lhs, tail = m.group("spec"), m.group("lhs"), m.group("tail")
            if lhs.startswith("{"):
                for part in lhs.strip("{} ").split(","):
                    bits = [b.strip() for b in part.split(":")]
                    if bits and bits[0]:
                        out[bits[-1]] = Import(bits[-1], spec, bits[0])
            else:
                out[lhs] = Import(lhs, spec, tail or "default")
        for imp in out.values():
            imp.target = self.project.resolve(self.path, imp.spec)
        return out

    # ---- constants (`const NAME = "literal"`), used for paths and SQL
    @cached_property
    def consts(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in re.finditer(r"\b(?:const|let|var)\s+([\w$]+)\s*(?::[^=]+)?=\s*", self.skel):
            for a, b, raw in self._lits_at(m.end()):
                out[m.group(1)] = raw
                break
        return out

    def _lits_at(self, pos: int):
        # a literal starting exactly at ``pos`` (after optional whitespace)
        lits = self.__dict__.setdefault("_lit_index", {a: (a, b, raw) for a, b, raw in self.literals})
        if pos in lits:
            yield lits[pos]

    # ---- local instances: `const svc = new UserService()` / `this.svc = new UserService()`
    @cached_property
    def instances(self) -> dict[str, str]:
        return {
            m.group(1): m.group(2)
            for m in re.finditer(r"(?<![\w$.])(?:this\s*\.\s*)?([\w$]+)\s*(?::[^=;]+)?=\s*new\s+([\w$]+)\s*[(<]", self.skel)
        }

    # ---- classes
    @cached_property
    def classes(self) -> list[ClassInfo]:
        return parse_classes(self)

    def tokens(self, s: int, e: int) -> set[str]:
        return set(IDENT_RE.findall(self.skel[s:e]))


class JsProject:
    """Source files of one or more package directories, parsed lazily."""

    def __init__(self, root: Path, dirs: list[Path] | None = None) -> None:
        self.root = root
        self.dirs = dirs or [root]
        self.paths = list_js_files(root, self.dirs)
        self._files: dict[Path, JsFile] = {}
        self._resolve_cache: dict[tuple[Path, str], Path | None] = {}
        self.pathset = set(self.paths)

    def file(self, path: Path) -> JsFile | None:
        if path not in self.pathset:
            return None
        got = self._files.get(path)
        if got is None:
            got = self._files[path] = JsFile(self, path)
        return got

    def files(self) -> list[JsFile]:
        return [f for f in (self.file(p) for p in self.paths) if f]

    def resolve(self, origin: Path, spec: str) -> Path | None:
        key = (origin, spec)
        if key in self._resolve_cache:
            return self._resolve_cache[key]
        out = self._resolve(origin, spec)
        self._resolve_cache[key] = out
        return out

    def _resolve(self, origin: Path, spec: str) -> Path | None:
        if spec.startswith("."):
            bases = [origin.parent / spec]
        else:
            tail = re.sub(r"^(@/|~/|#)", "", spec)
            if tail.startswith("@"):  # scoped npm package
                return None
            bases = [self.root / tail, self.root / "src" / tail]
        for base in bases:
            base = Path(os.path.normpath(base))
            cands = [base]
            if base.suffix in (".js", ".mjs", ".cjs"):
                cands += [base.with_suffix(ext) for ext in EXT_TRY]
            cands += [Path(str(base) + ext) for ext in EXT_TRY]
            cands += [base / f"index{ext}" for ext in EXT_TRY]
            for cand in cands:
                if cand in self.pathset:
                    return cand
        return None

    # ---- classes / units
    @cached_property
    def class_index(self) -> dict[str, list[ClassInfo]]:
        out: dict[str, list[ClassInfo]] = {}
        for f in self.files():
            for c in f.classes:
                out.setdefault(c.name, []).append(c)
        return out

    @cached_property
    def units(self) -> list["Unit"]:
        return build_units(self)

    @cached_property
    def units_by_file(self) -> dict[Path, list["Unit"]]:
        out: dict[Path, list[Unit]] = {}
        for u in self.units:
            out.setdefault(u.js.path, []).append(u)
        return out

    @cached_property
    def unit_by_class(self) -> dict[str, "Unit"]:
        return {u.cls.name: u for u in self.units if u.cls}

    def referenced_units(self, js: JsFile, s: int, e: int) -> list["Unit"]:
        """Service/repository units that the code in ``js[s:e]`` refers to."""
        found: list[Unit] = []

        def add(u: Unit) -> None:
            if u not in found:
                found.append(u)

        for tok in sorted(js.tokens(s, e)):
            imp = js.imports.get(tok)
            if imp and imp.target:
                for u in self._units_for_import(imp):
                    add(u)
            cls = js.instances.get(tok)
            if cls and cls in self.unit_by_class:
                add(self.unit_by_class[cls])
            if tok in self.unit_by_class:
                add(self.unit_by_class[tok])
        return found

    def _units_for_import(self, imp: Import) -> list["Unit"]:
        target = self.file(imp.target) if imp.target else None
        if not target:
            return []
        units = self.units_by_file.get(target.path, [])
        if not units:
            return []
        class_units = [u for u in units if u.cls]
        if imp.name != "default":
            exact = [u for u in class_units if u.cls and u.cls.name == imp.name]
            if exact:
                return exact
            m = re.search(r"\b(?:const|let|var)\s+" + re.escape(imp.name) + r"\s*(?::[^=]+)?=\s*new\s+([\w$]+)", target.skel)
            if m:
                return [u for u in class_units if u.cls and u.cls.name == m.group(1)]
            low = [u for u in class_units if u.cls and u.cls.name[0].lower() + u.cls.name[1:] == imp.name]
            if low:
                return low
        else:
            m = re.search(r"(?:module\.exports\s*=|export\s+default)\s*(?:new\s+)?(?:class\s+)?([\w$]+)", target.skel)
            if m:
                named = [u for u in class_units if u.cls and u.cls.name == m.group(1)]
                if named:
                    return named
                inst = target.instances.get(m.group(1))
                if inst:
                    return [u for u in class_units if u.cls and u.cls.name == inst]
        if len(class_units) == 1:
            return class_units
        mods = [u for u in units if not u.cls]
        return mods or units


def list_js_files(root: Path, dirs: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for base in dirs:
        for cur, subdirs, names in os.walk(base):
            subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS)
            for name in sorted(names):
                p = Path(cur) / name
                if p.suffix in JS_SUFFIXES and not SKIP_FILE_RE.search(name) and p not in seen:
                    seen.add(p)
                    out.append(p)
    return sorted(out)


def package_dirs(root: Path, want: str, avoid: str | None = None) -> list[Path]:
    """Directories whose package.json depends on ``want`` (and not on ``avoid``)."""
    out: list[Path] = []
    for cur, subdirs, names in os.walk(root):
        depth = len(Path(cur).relative_to(root).parts)
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS] if depth < 4 else []
        if "package.json" not in names:
            continue
        try:
            data = json.loads((Path(cur) / "package.json").read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        deps: dict = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(data.get(key), dict):
                deps.update(data[key])
        if want in deps and not (avoid and avoid in deps):
            out.append(Path(cur))
    return out


# ------------------------------------------------------------------ class parsing

_DECO_NAME = re.compile(r"[\w$.]+")
_MODIFIERS = r"(?:(?:public|private|protected|static|async|readonly|abstract|override|declare|get|set)\s+|\*\s*)*"
_MEMBER_HEAD = re.compile(_MODIFIERS + r"(?P<name>[\w$]+|\[[^\]\n]*\])\s*(?P<opt>[?!]?)")


def decorators_before(js: JsFile, idx: int) -> tuple[list[Deco], int]:
    """Decorators (and the start offset) directly preceding the declaration at ``idx``."""
    skel = js.skel
    i = idx
    while True:
        k = i
        while k > 0 and skel[k - 1].isspace():
            k -= 1
        m = re.search(r"(?:^|[^\w$])(export|default|abstract|declare)$", skel[max(0, k - 20) : k])
        if not m:
            break
        i = k - len(m.group(1))
    start = i
    decos: list[Deco] = []
    while True:
        k = i
        while k > 0 and skel[k - 1].isspace():
            k -= 1
        if k > 0 and skel[k - 1] == ")":
            op = match_open_back(skel, k - 1)
            if op < 0:
                break
            m = re.search(r"@([\w$.]+)\s*$", skel[max(0, op - 80) : op])
            if not m:
                break
            at = max(0, op - 80) + m.start()
            inner = (op + 1, k - 1)
            args = [js.code[a:b] for a, b in split_top(skel, *inner)]
            decos.append(Deco(m.group(1), args, js.code[inner[0] : inner[1]], at))
            i = at
            start = at
        else:
            m = re.search(r"@([\w$.]+)$", skel[max(0, k - 80) : k])
            if not m:
                break
            at = k - len(m.group())
            decos.append(Deco(m.group(1), [], "", at))
            i = at
            start = at
    decos.reverse()
    return decos, start


def _parse_decos(js: JsFile, i: int, e: int) -> tuple[list[Deco], int]:
    skel = js.skel
    decos: list[Deco] = []
    while i < e and skel[i] == "@":
        m = _DECO_NAME.match(skel, i + 1)
        if not m:
            return decos, i + 1
        pos = i
        name = m.group()
        i = m.end()
        j = skip_ws(skel, i, e)
        args: list[str] = []
        raw = ""
        if j < e and skel[j] == "(":
            k = match_close(skel, j)
            if k < 0 or k > e:
                return decos, e
            args = [js.code[a:b] for a, b in split_top(skel, j + 1, k)]
            raw = js.code[j + 1 : k]
            i = k + 1
        decos.append(Deco(name, args, raw, pos))
        i = skip_ws(skel, i, e)
    return decos, i


_PARAM_MOD = re.compile(r"(?:(?:public|private|protected|readonly|override)\s+)+")


def parse_params(js: JsFile, s: int, e: int) -> list[Param]:
    skel = js.skel
    out: list[Param] = []
    for a, b in split_top(skel, s, e):
        decos, i = _parse_decos(js, a, b)
        mm = _PARAM_MOD.match(skel, i, b)
        prop = bool(mm)
        if mm:
            i = mm.end()
        m = re.compile(r"\.\.\.|[\w$]+").match(skel, i, b)
        name = ""
        ptype = None
        if m:
            name = m.group().lstrip(".")
            if m.group() == "...":
                m2 = IDENT_RE.match(skel, m.end(), b)
                name = m2.group() if m2 else ""
                i = m2.end() if m2 else m.end()
            else:
                i = m.end()
        elif i < b and skel[i] in "{[":
            k = match_close(skel, i)
            i = k + 1 if k > 0 else b
            name = "{}"
        j = skip_ws(skel, i, b)
        if j < b and skel[j] == "?":
            j = skip_ws(skel, j + 1, b)
        if j < b and skel[j] == ":":
            end = b
            depth = 0
            for t in range(j + 1, b):
                c = skel[t]
                if c in "([{<":
                    depth += 1
                elif c in ")]}":
                    depth -= 1
                elif c == ">" and skel[t - 1] != "=":
                    depth -= 1
                elif c == "=" and depth <= 0 and skel[t + 1 : t + 2] != ">":
                    end = t
                    break
            ptype = js.code[j + 1 : end].strip() or None
        out.append(Param(name, ptype, decos, prop))
    return out


def type_head(t: str | None) -> tuple[str | None, str | None]:
    """``Repository<User>`` -> ("Repository", "User"); ``Foo | null`` -> ("Foo", None)."""
    if not t:
        return None, None
    t = re.sub(r"\|\s*(null|undefined)\b|\b(null|undefined)\s*\|", "", t).strip()
    m = re.match(r"([\w$.]+)\s*(?:<\s*([\w$.]+)[^>]*>)?", t)
    if not m:
        return None, None
    return m.group(1).split(".")[-1], (m.group(2) or "").split(".")[-1] or None


def _member_end(skel: str, i: int, e: int) -> int:
    depth = 0
    j = i
    while j < e:
        c = skel[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return j
        elif depth == 0 and c == ";":
            return j + 1
        elif depth == 0 and c == "\n":
            k = j - 1
            while k >= i and skel[k] in " \t\r":
                k -= 1
            nxt = skip_ws(skel, j, e)
            if (k < i or skel[k] not in "=|&:,+(<") and (nxt >= e or skel[nxt] not in ".|&?:+-*/=,"):
                return j
        j += 1
    return e


def parse_members(js: JsFile, s: int, e: int) -> list[Member]:
    skel = js.skel
    out: list[Member] = []
    i = s
    while i < e:
        while i < e and (skel[i].isspace() or skel[i] == ";"):
            i += 1
        if i >= e:
            break
        begin = i
        decos, i = _parse_decos(js, i, e)
        m = _MEMBER_HEAD.match(skel, i, e)
        if not m:
            i = max(i + 1, begin + 1)
            continue
        name = m.group("name")
        j = skip_ws(skel, m.end(), e)
        if j < e and skel[j] == "<":
            j = skip_angles(skel, j)
            j = skip_ws(skel, j, e)
        if j < e and skel[j] == "(":
            k = match_close(skel, j)
            if k < 0 or k >= e:
                break
            params = parse_params(js, j + 1, k)
            b = after_signature(skel, k + 1, e)
            body = None
            nxt = k + 1
            if b >= 0:
                be = match_close(skel, b)
                if be < 0:
                    break
                body = (b + 1, be)
                nxt = be + 1
            else:
                nxt = _member_end(skel, k + 1, e)
            out.append(Member("method", name, m.start("name"), decos, params, body, None, begin))
            i = max(nxt, begin + 1)
        else:
            end = _member_end(skel, m.end(), e)
            seg = js.code[m.end() : end]
            ptype = None
            tm = re.match(r"\s*:\s*([^=;]+?)\s*(?:=|;|$)", seg, re.S)
            if tm:
                ptype = tm.group(1).strip()
            out.append(Member("prop", name, m.start("name"), decos, [], None, ptype, begin))
            i = max(end, begin + 1)
    return out


def parse_classes(js: JsFile) -> list[ClassInfo]:
    skel = js.skel
    out: list[ClassInfo] = []
    for m in re.finditer(r"(?<![\w$.])class\s+([\w$]+)", skel):
        j = m.end()
        # header up to the body brace; generics on the class or its `extends` are skipped
        k = j
        while k < len(skel) and skel[k] != "{":
            if skel[k] == "<":
                k = skip_angles(skel, k)
                continue
            if skel[k] == ";":
                k = -1
                break
            k += 1
        if k < 0 or k >= len(skel):
            continue
        close = match_close(skel, k)
        if close < 0:
            continue
        header = js.code[j:k]
        em = re.search(r"\bextends\s+([\w$.]+(?:\s*<[^{]*?>)?)", header)
        decos, start = decorators_before(js, m.start())
        out.append(
            ClassInfo(
                name=m.group(1),
                js=js,
                pos=m.start(),
                start=start,
                body=(k, close),
                decos=decos,
                extends=em.group(1).strip() if em else None,
                members=parse_members(js, k + 1, close),
            )
        )
    return out


# --------------------------------------------------------------- function finder


def find_function(js: JsFile, name: str) -> tuple[int, int] | None:
    """Span of the body of function/method/arrow ``name`` defined in ``js`` (first match)."""
    skel = js.skel
    n = re.escape(name)
    # 1. function declaration
    m = re.search(r"(?<![\w$.])function\s*\*?\s*" + n + r"\s*(?:<[^()]*>)?\(", skel)
    if m:
        k = match_close(skel, m.end() - 1)
        b = after_signature(skel, k + 1, len(skel)) if k > 0 else -1
        if b >= 0:
            return b + 1, match_close(skel, b)
    # 2/3. const NAME = ... / exports.NAME = ...
    for pat in (
        r"(?<![\w$.])(?:const|let|var)\s+" + n + r"\s*(?::[^=]+)?=\s*",
        r"(?<![\w$])(?:module\s*\.\s*)?exports\s*\.\s*" + n + r"\s*=\s*",
    ):
        m = re.search(pat, skel)
        if m:
            return m.end(), expr_end(skel, m.end())
    # 4. class method / object method shorthand
    for m in re.finditer(r"(?<![\w$.])(?:(?:static|async|public|private|protected)\s+)*" + n + r"\s*(?:<[^()]*>)?\(", skel):
        k = match_close(skel, m.end() - 1)
        if k < 0:
            continue
        b = after_signature(skel, k + 1, len(skel))
        if b >= 0:
            return b + 1, match_close(skel, b)
    # 5. class property arrow / object property
    m = re.search(r"(?<![\w$.])(?:(?:static|public|private|protected|readonly)\s+)*" + n + r"\s*(?::[^=;,{}]+)?[=:]\s*(?=(?:async\b|function\b|\(|[\w$]+\s*=>))", skel)
    if m:
        return m.end(), expr_end(skel, m.end(), stop_comma=True)
    return None


# ------------------------------------------------------------------------- units

_SERVICE_NAME = re.compile(r"Service$")
_REPO_NAME = re.compile(r"(Repository|Repo|Dao|DAO)$")
REPO_DIRS = {"repositories", "repository", "repos", "dao", "daos"}
SERVICE_DIRS = {"services", "service"}


def classify_name(name: str) -> str | None:
    if _REPO_NAME.search(name):
        return "repo"
    if _SERVICE_NAME.search(name):
        return "service"
    return None


def classify_path(relpath: str) -> tuple[str, str] | None:
    """("service"|"repo", PascalName) for a module-of-functions file, from its location/name."""
    p = Path(relpath)
    stem = p.stem
    if stem.lower() == "index":
        return None
    low = stem.lower()
    dirs = {d.lower() for d in p.parts[:-1]}
    kind = None
    suffix_re = None
    if re.search(r"[._-]?(repository|repo|dao)$", low):
        kind, suffix_re = "repo", r"[._-]?(repository|repo|dao)$"
    elif re.search(r"[._-]?service$", low):
        kind, suffix_re = "service", r"[._-]?service$"
    elif dirs & REPO_DIRS:
        kind = "repo"
    elif dirs & SERVICE_DIRS:
        kind = "service"
    if not kind:
        return None
    base = re.sub(suffix_re, "", stem, flags=re.I) if suffix_re else stem
    base = pascal(base) or pascal(stem)
    return kind, base + ("Repository" if kind == "repo" else "Service")


@dataclass(eq=False)
class Unit:
    """A service or repository: a class, or a module of functions."""

    kind: str
    name: str
    js: JsFile
    span: tuple[int, int]
    pos: int
    cls: ClassInfo | None = None

    @property
    def node_id(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def line(self) -> int:
        return self.js.line(self.pos)

    @property
    def methods(self) -> list[str] | None:
        if self.cls:
            return [m.name for m in self.cls.members if m.kind == "method" and m.name != "constructor" and not m.name.startswith(("_", "#"))]
        names = re.findall(r"(?<![\w$.])function\s*\*?\s*([\w$]+)", self.js.skel)
        names += re.findall(r"(?<![\w$])exports\s*\.\s*([\w$]+)\s*=", self.js.skel)
        names += re.findall(r"\bexport\s+(?:const|let|var)\s+([\w$]+)", self.js.skel)
        return sorted({n for n in names if not n.startswith("_")}) or None


def build_units(project: JsProject) -> list[Unit]:
    out: list[Unit] = []
    for js in project.files():
        found = False
        for cls in js.classes:
            kind = classify_name(cls.name)
            if not kind:
                continue
            found = True  # a class decides the file; never fall back to a module-level unit
            if (cls.extends and "PrismaClient" in cls.extends) or cls.name == "PrismaService":
                continue
            if cls.deco("Controller") or cls.deco("Module"):
                continue
            out.append(Unit(kind, cls.name, js, (cls.body[0], cls.body[1] + 1), cls.pos, cls))
        if not found:
            hit = classify_path(js.rel)
            if hit:
                out.append(Unit(hit[0], hit[1], js, (0, len(js.skel)), 0))
    return out


# --------------------------------------------------------------------- externals

HTTP_MODULES = {"axios", "got", "node-fetch", "undici", "superagent", "request", "needle", "ky", "cross-fetch", "@nestjs/axios", "isomorphic-fetch"}
REDIS_MODULES = {"ioredis", "redis", "@redis/client", "@nestjs-modules/ioredis", "@liaoliaots/nestjs-redis"}
EXTERNALS = {
    "HTTP / Webhook": "outbound HTTP",
    "Redis": "cache",
    "AWS": "AWS SDK",
}


def externals_in(js: JsFile, s: int, e: int) -> list[tuple[str, str]]:
    """(label, via-module) of external systems used in ``js[s:e]``."""
    toks = js.tokens(s, e)
    found: dict[str, str] = {}
    for local, imp in js.imports.items():
        if local not in toks or imp.target:
            continue
        spec = imp.spec
        if spec in HTTP_MODULES:
            found.setdefault("HTTP / Webhook", spec)
        elif spec in REDIS_MODULES:
            found.setdefault("Redis", spec)
        elif spec.startswith("@aws-sdk/") or spec == "aws-sdk":
            found.setdefault("AWS", spec)
    if "HTTP / Webhook" not in found and re.search(r"(?<![\w$.])fetch\s*\(", js.skel[s:e]):
        found["HTTP / Webhook"] = "fetch"
    return sorted(found.items())


# --------------------------------------------------------------------- ORM tables

PRISMA_OPS = {
    "findMany": ["SELECT"], "findUnique": ["SELECT"], "findUniqueOrThrow": ["SELECT"], "findFirst": ["SELECT"],
    "findFirstOrThrow": ["SELECT"], "count": ["SELECT"], "aggregate": ["SELECT"], "groupBy": ["SELECT"],
    "create": ["INSERT"], "createMany": ["INSERT"], "createManyAndReturn": ["INSERT"],
    "upsert": ["INSERT", "UPDATE"], "update": ["UPDATE"], "updateMany": ["UPDATE"],
    "delete": ["DELETE"], "deleteMany": ["DELETE"],
}
# TypeORM repository / entity-manager methods
REPO_OPS = {
    "find": ["SELECT"], "findOne": ["SELECT"], "findBy": ["SELECT"], "findOneBy": ["SELECT"], "findAndCount": ["SELECT"],
    "findAndCountBy": ["SELECT"], "findOneOrFail": ["SELECT"], "findOneByOrFail": ["SELECT"], "count": ["SELECT"],
    "countBy": ["SELECT"], "exist": ["SELECT"], "exists": ["SELECT"], "existsBy": ["SELECT"], "sum": ["SELECT"],
    "average": ["SELECT"], "minimum": ["SELECT"], "maximum": ["SELECT"], "createQueryBuilder": ["SELECT"],
    "save": ["INSERT", "UPDATE"], "insert": ["INSERT"], "upsert": ["INSERT", "UPDATE"], "update": ["UPDATE"],
    "increment": ["UPDATE"], "decrement": ["UPDATE"], "softDelete": ["UPDATE"], "softRemove": ["UPDATE"],
    "restore": ["UPDATE"], "delete": ["DELETE"], "remove": ["DELETE"], "clear": ["DELETE"],
}
# Sequelize model statics (plus TypeORM active-record names via REPO_OPS)
SEQ_OPS = {
    "findAll": ["SELECT"], "findOne": ["SELECT"], "findByPk": ["SELECT"], "findAndCountAll": ["SELECT"],
    "findOrCreate": ["SELECT", "INSERT"], "count": ["SELECT"], "max": ["SELECT"], "min": ["SELECT"], "sum": ["SELECT"],
    "create": ["INSERT"], "bulkCreate": ["INSERT"], "update": ["UPDATE"], "destroy": ["DELETE"], "truncate": ["DELETE"],
    "upsert": ["INSERT", "UPDATE"], "increment": ["UPDATE"], "decrement": ["UPDATE"], "restore": ["UPDATE"],
}
STATIC_OPS = {**REPO_OPS, **SEQ_OPS}
KNEX_WRITE = {
    "insert": "INSERT", "upsert": "INSERT", "onConflict": "INSERT", "update": "UPDATE", "increment": "UPDATE",
    "decrement": "UPDATE", "del": "DELETE", "delete": "DELETE", "truncate": "DELETE",
}
KNEX_JOINS = {"join", "leftJoin", "rightJoin", "innerJoin", "fullJoin", "crossJoin", "leftOuterJoin", "rightOuterJoin"}
NOT_KNEX_OWNERS = {"Array", "Buffer", "Object", "Promise", "JSON", "Symbol", "String", "Number", "Date", "Map", "Set", "BigInt"}

SQL_WORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN|WITH|CALL)\b", re.I)
BARE_QUALIFIED_RE = re.compile(r"^\s*[\"`]?[a-zA-Z_]\w*[\"`]?\.[\"`]?[a-zA-Z_]\w*[\"`]?\s*$")


def looks_like_sql(text: str) -> bool:
    if BARE_QUALIFIED_RE.match(text):
        return True
    return bool(SQL_WORD_RE.search(text)) and not text.rstrip().endswith((".", "!", "?"))


def chain_methods(skel: str, i: int, limit: int) -> list[str]:
    """Method names of a call chain ``.a(...).b(...)`` that continues at ``skel[i:]``."""
    names: list[str] = []
    while i < limit:
        j = skip_ws(skel, i, limit)
        if skel[j : j + 2] == "?.":
            j += 1
        if j >= limit or skel[j] != ".":
            break
        m = IDENT_RE.match(skel, skip_ws(skel, j + 1, limit))
        if not m:
            break
        names.append(m.group())
        k = skip_ws(skel, m.end(), limit)
        if k < limit and skel[k] == "<":
            k = skip_angles(skel, k)
        if k < limit and skel[k] == "(":
            c = match_close(skel, k)
            if c < 0:
                break
            i = c + 1
        else:
            i = m.end()
    return names


def statement_ops(skel: str, pos: int, end: int, lo: int, hi: int) -> list[str]:
    """SQL ops of the query-builder statement around ``pos`` (window clipped to ``lo:hi``)."""
    a = max(lo, pos - 300)
    cut = skel.rfind(";", a, pos)
    if cut >= 0:
        a = cut + 1
    b = min(hi, end + 400)
    cut = skel.find(";", end, b)
    if cut >= 0:
        b = cut
    ops: list[str] = []
    for name in re.findall(r"\.\s*(\w+)\s*\(", skel[a:b]):
        op = KNEX_WRITE.get(name)
        if op and op not in ops:
            ops.append(op)
    return ops or ["SELECT"]


class OrmIndex:
    """Model -> table maps for every ORM seen in the project, plus table-access detection."""

    def __init__(self) -> None:
        self.prisma: dict[str, str] = {}  # client accessor (user) -> table name
        self.entities: dict[str, str] = {}  # class / model variable name -> table name
        self.tables: list[dict] = []  # {name, schema, source, columns}

    # ---------------------------------------------------------------- building

    @classmethod
    def build(cls, root: Path, project: JsProject | None = None) -> "OrmIndex":
        idx = cls()
        project = project or JsProject(root)
        idx._prisma(root)
        for js in project.files():
            text = js.text
            if "@Entity" in text or "@Table" in text:
                idx._decorated(js)
            if ".define" in text:
                idx._sequelize_define(js)
            if ".init" in text and "Model" in text:
                idx._sequelize_init(js)
            if "createTable" in text:
                idx._knex(js)
        return idx

    def register(self, registry) -> None:
        for t in self.tables:
            registry.add(t["name"], schema=t["schema"], kind="table", source=t["source"])
            if t["columns"]:
                registry.set_columns(t["name"], t["columns"], authoritative=False)

    def _add(self, name: str, schema: str | None, source: str, columns: list[dict]) -> None:
        self.tables.append({"name": name, "schema": schema, "source": source, "columns": columns})

    # ---- Prisma
    def _prisma(self, root: Path) -> None:
        files: list[Path] = []
        for cur, subdirs, names in os.walk(root):
            subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
            files += [Path(cur) / n for n in names if n.endswith(".prisma")]
        for path in sorted(files):
            text = path.read_text(encoding="utf-8", errors="replace")
            lx = lex(re.sub(r"//[^\n]*", lambda m: " " * len(m.group()), text))
            models = [(m.group(1), m.end() - 1) for m in re.finditer(r"(?m)^\s*model\s+(\w+)\s*\{", lx.skel)]
            names = {n for n, _ in models}
            for name, brace in models:
                close = match_close(lx.skel, brace)
                if close < 0:
                    continue
                table, schema, cols = name, None, []
                pk_composite: set[str] = set()
                for off, raw in _lines(lx.code, brace + 1, close):
                    line = raw.strip()
                    if not line:
                        continue
                    if line.startswith("@@"):
                        mm = re.match(r'@@map\(\s*(?:name:\s*)?"([^"]+)"', line)
                        if mm:
                            table = mm.group(1)
                        ms = re.match(r'@@schema\(\s*"([^"]+)"', line)
                        if ms:
                            schema = ms.group(1)
                        mi = re.match(r"@@id\(\s*(?:fields:\s*)?\[([^\]]*)\]", line)
                        if mi:
                            pk_composite = {x.strip() for x in mi.group(1).split(",")}
                        continue
                    fm = re.match(r"(\w+)\s+(\w+)(\[\])?(\?)?(.*)$", line)
                    if not fm:
                        continue
                    fname, ftype, is_list, _opt, rest = fm.groups()
                    if ftype in names:  # relation field, not a column
                        continue
                    cmap = re.search(r'@map\(\s*(?:name:\s*)?"([^"]+)"', rest)
                    col = {"name": cmap.group(1) if cmap else fname, "type": ftype + ("[]" if is_list else "")}
                    if re.search(r"@id\b", rest) or fname in pk_composite:
                        col["pk"] = True
                    cols.append(col)
                self.prisma[name[0].lower() + name[1:]] = table
                self._add(table, schema, f"{rel_to(root, path)}:{line_of(text, brace)}", cols)

    # ---- TypeORM (@Entity) and sequelize-typescript (@Table)
    def _decorated(self, js: JsFile) -> None:
        for c in js.classes:
            d = c.deco("Entity") or c.deco("Table") or c.deco("ViewEntity")
            if not d:
                continue
            name, schema = None, None
            for arg in d.args:
                v = str_value(arg)
                if v:
                    name = name or v
                obj = parse_object(arg)
                if obj:
                    name = str_value(obj.get("name", "")) or str_value(obj.get("tableName", "")) or name
                    schema = str_value(obj.get("schema", "")) or schema
                    if d.name == "Table" and "freezeTableName" in obj and not name and "true" in obj["freezeTableName"]:
                        name = c.name
            if not name:
                # TypeORM's default naming strategy snake_cases the class name; sequelize-typescript pluralizes it.
                name = pluralize(c.name) if d.name == "Table" else snake_case(c.name)
            cols = self._decorated_columns(c)
            self.entities[c.name] = name
            self._add(name, schema, f"{js.rel}:{js.line(c.pos)}", cols)

    @staticmethod
    def _decorated_columns(c: ClassInfo) -> list[dict]:
        cols = []
        for m in c.members:
            if m.kind != "prop":
                continue
            names = {d.name: d for d in m.decos}
            col_d = next((names[n] for n in names if n in {
                "Column", "PrimaryGeneratedColumn", "PrimaryColumn", "CreateDateColumn", "UpdateDateColumn",
                "DeleteDateColumn", "VersionColumn", "ObjectIdColumn", "PrimaryKey"}), None)
            if not col_d:
                continue
            col_name, col_type = m.name, (m.type or "").strip() or None
            for arg in col_d.args:
                v = str_value(arg)
                if v and col_d.name != "PrimaryGeneratedColumn":
                    col_type = v
                elif v:
                    col_type = v
                obj = parse_object(arg)
                if obj:
                    col_name = str_value(obj.get("name", "")) or col_name
                    col_type = str_value(obj.get("type", "")) or (obj.get("type") or "").split(".")[-1] or col_type
            if col_d.name in {"CreateDateColumn", "UpdateDateColumn", "DeleteDateColumn"}:
                col_type = col_type or "timestamp"
            col = {"name": col_name, "type": col_type or "unknown"}
            if col_d.name in {"PrimaryGeneratedColumn", "PrimaryColumn", "PrimaryKey"} or "PrimaryKey" in names:
                col["pk"] = True
            cols.append(col)
        return cols

    # ---- Sequelize
    def _sequelize_define(self, js: JsFile) -> None:
        for m in re.finditer(r"(?<![\w$.])([\w$.]+)\s*\.\s*define\s*(?:<[^()]*>)?\(", js.skel):
            close = match_close(js.skel, m.end() - 1)
            if close < 0:
                continue
            args = [js.code[a:b] for a, b in split_top(js.skel, m.end(), close)]
            model = str_value(args[0]) if args else None
            if not model or len(args) < 2:
                continue
            opts = parse_object(args[2]) if len(args) > 2 else {}
            var = re.search(r"([\w$]+)\s*=\s*(?:await\s+)?$", js.skel[max(0, m.start() - 60) : m.start()])
            self._sequelize_model(js, model, var.group(1) if var else None, args[1], opts, m.start())

    def _sequelize_init(self, js: JsFile) -> None:
        classes = {m.group(1) for m in re.finditer(r"class\s+([\w$]+)\s+extends\s+(?:[\w$]+\.)?Model\b", js.skel)}
        for m in re.finditer(r"(?<![\w$.])([\w$]+)\s*\.\s*init\s*\(", js.skel):
            if m.group(1) not in classes:
                continue
            close = match_close(js.skel, m.end() - 1)
            if close < 0:
                continue
            args = [js.code[a:b] for a, b in split_top(js.skel, m.end(), close)]
            if not args:
                continue
            opts = parse_object(args[1]) if len(args) > 1 else {}
            model = str_value(opts.get("modelName", "")) or m.group(1)
            self._sequelize_model(js, model, m.group(1), args[0], opts, m.start())

    def _sequelize_model(self, js: JsFile, model: str, var: str | None, attrs: str, opts: dict, pos: int) -> None:
        table = str_value(opts.get("tableName", ""))
        if not table:
            table = model.lower() if "true" in opts.get("freezeTableName", "") else pluralize(model)
        schema = str_value(opts.get("schema", "")) or None
        cols = []
        for key, val in parse_object(attrs).items():
            ctype, pk, cname = val, False, key
            if val.startswith("{"):
                o = parse_object(val)
                ctype = o.get("type", "")
                pk = "true" in o.get("primaryKey", "")
                cname = str_value(o.get("field", "")) or key
            ctype = re.sub(r"^(?:DataTypes|Sequelize|DataType)\.", "", ctype.strip()).split("(")[0] or "unknown"
            col = {"name": cname, "type": ctype}
            if pk:
                col["pk"] = True
            cols.append(col)
        self.entities[model] = table
        if var:
            self.entities[var] = table
        self._add(table, schema, f"{js.rel}:{js.line(pos)}", cols)

    # ---- Knex migrations
    _KNEX_NOT_COLUMN = {
        "index", "unique", "foreign", "primary", "dropColumn", "dropColumns", "dropForeign", "dropIndex", "dropPrimary",
        "dropUnique", "check", "comment", "engine", "charset", "collate", "inherits", "raw", "timestamps", "dropTimestamps",
    }

    def _knex(self, js: JsFile) -> None:
        for m in re.finditer(r"\bcreateTable(?:IfNotExists)?\s*\(", js.skel):
            close = match_close(js.skel, m.end() - 1)
            if close < 0:
                continue
            spans = split_top(js.skel, m.end(), close)
            if len(spans) < 2:
                continue
            name = str_value(js.code[spans[0][0] : spans[0][1]])
            if not name:
                continue
            schema = None
            ws = re.search(r"withSchema\(\s*(['\"`])(\w+)\1\s*\)\s*\.\s*$", js.code[max(0, m.start() - 120) : m.start()].rstrip())
            if ws:
                schema = ws.group(2)
            if "." in name:
                schema, name = name.split(".", 1)
            cb_s, cb_e = spans[1]
            param = re.match(r"\s*(?:async\s*)?(?:function\s*)?\(?\s*([\w$]+)", js.skel[cb_s:cb_e])
            cols = []
            if param:
                t = re.escape(param.group(1))
                for cm in re.finditer(r"(?<![\w$.])" + t + r"\s*\.\s*(\w+)\s*\(\s*(['\"`])([\w$]+)\2", js.code[cb_s:cb_e]):
                    method = cm.group(1)
                    if method in self._KNEX_NOT_COLUMN:
                        continue
                    stmt_end = js.skel.find(";", cb_s + cm.end(), cb_e)
                    tail = js.skel[cb_s + cm.end() : stmt_end if stmt_end > 0 else cb_e]
                    col = {"name": cm.group(3), "type": method}
                    if method in ("increments", "bigIncrements") or ".primary(" in tail.split("\n")[0]:
                        col["pk"] = True
                    cols.append(col)
            self._add(name, schema, f"{js.rel}:{js.line(m.start())}", cols)

    # ---------------------------------------------------------------- detection

    def bound_repos(self, js: JsFile) -> dict[str, str]:
        """Variables / properties bound to a TypeORM repository for one entity, in one file."""
        cache = js.__dict__.setdefault("_bound", {})
        if "v" in cache:
            return cache["v"]
        out: dict[str, str] = {}
        for m in re.finditer(r"([\w$]+)\s*(?::[^=;]+)?=\s*[\w$.()]*?\bget(?:Custom)?Repository\s*(?:<[^>()]*>)?\(\s*([\w$]+)", js.skel):
            out[m.group(1)] = m.group(2)
        for m in re.finditer(r"@Inject(?:Repository|Model)\s*\(\s*([\w$]+)[^)]*\)\s*(?:(?:private|public|protected|readonly)\s+)*([\w$]+)", js.skel):
            out[m.group(2)] = m.group(1)
        for m in re.finditer(r"([\w$]+)\s*[?!]?\s*:\s*Repository\s*<\s*([\w$]+)\s*>", js.skel):
            out.setdefault(m.group(1), m.group(2))
        for m in re.finditer(r"\bextends\s+Repository\s*<\s*([\w$]+)\s*>", js.skel):
            out["this"] = m.group(1)
        m = re.search(r"@EntityRepository\(\s*([\w$]+)", js.skel)
        if m:
            out["this"] = m.group(1)
        cache["v"] = out
        return out

    def hits_in(self, js: JsFile, s: int, e: int, hits: TableHits, evidence: str | None = None) -> None:
        """Record every table accessed by the code in ``js[s:e]`` (unknown tables are ignored)."""
        skel, code = js.skel, js.code
        def add(table: str | None, ops: list[str], pos: int) -> None:
            if not table:
                return
            where = f"{js.rel}:{js.line(pos)}"
            for op in ops:
                hits.add(table, op, where)

        # Prisma: <client>.<model>.<op>(
        if self.prisma:
            for m in re.finditer(r"([\w$]+)\s*\.\s*([\w$]+)\s*\.\s*(\w+)\s*\(", skel[s:e]):
                ops = PRISMA_OPS.get(m.group(3))
                if ops and m.group(2) in self.prisma:
                    add(self.prisma[m.group(2)], ops, s + m.start())
        # entity statics: User.findAll( / User.find(
        if self.entities:
            for m in re.finditer(r"(?<![\w$.])([\w$]+)\s*\.\s*(\w+)\s*\(", skel[s:e]):
                if m.group(1) in self.entities and m.group(2) in STATIC_OPS:
                    add(self.entities[m.group(1)], STATIC_OPS[m.group(2)], s + m.start())
            # manager.save(User, ...) / getRepository(User).find()
            for m in re.finditer(r"\.\s*(\w+)\s*\(\s*([\w$]+)\s*[,)]", skel[s:e]):
                if m.group(2) in self.entities and m.group(1) in REPO_OPS and m.group(1) != "createQueryBuilder":
                    add(self.entities[m.group(2)], REPO_OPS[m.group(1)], s + m.start())
            for m in re.finditer(r"getRepository\s*(?:<[^>()]*>)?\(\s*([\w$]+)\s*\)\s*\.\s*(\w+)\s*\(", skel[s:e]):
                if m.group(1) in self.entities and m.group(2) in REPO_OPS:
                    ops = REPO_OPS[m.group(2)]
                    if m.group(2) == "createQueryBuilder":
                        ops = statement_ops(skel, s + m.start(), s + m.end(), s, e)
                    add(self.entities[m.group(1)], ops, s + m.start())
            for m in re.finditer(r"\.\s*(into|from|update)\s*\(\s*([\w$]+)\s*[,)]", skel[s:e]):
                if m.group(2) in self.entities:
                    ops = {"into": ["INSERT"], "update": ["UPDATE"]}.get(m.group(1))
                    if ops is None:
                        ops = ["DELETE"] if re.search(r"\.\s*delete\s*\(\s*\)\s*$", skel[max(s, s + m.start() - 40) : s + m.start()]) else ["SELECT"]
                    add(self.entities[m.group(2)], ops, s + m.start())
        # bound repositories: this.usersRepo.find(
        for var, entity in self.bound_repos(js).items():
            table = self.entities.get(entity)
            if not table:
                continue
            owner = r"this" if var == "this" else r"(?<![\w$])(?:this\s*\.\s*)?" + re.escape(var)
            for m in re.finditer(owner + r"\s*\.\s*(\w+)\s*\(", skel[s:e]):
                ops = REPO_OPS.get(m.group(1))
                if not ops:
                    continue
                if m.group(1) == "createQueryBuilder":
                    ops = statement_ops(skel, s + m.start(), s + m.end(), s, e)
                add(table, ops, s + m.start())
        # Knex: knex("users").where(...)
        for m in re.finditer(r"(?<![\w$.])([\w$]+)\s*(?:<[^()]*>)?\(\s*(['\"`])([\w$]+(?:\.[\w$]+)?)\2\s*\)", code[s:e]):
            if m.group(1) in ("require", "import", "Router", "route", "get", "post", "put", "patch", "delete", "use", "Entity", "Controller"):
                continue
            end = s + m.end()
            chain = chain_methods(skel, end, e)
            if chain and (set(chain) & set(KNEX_WRITE) or chain[0] in KNEX_SELECTORS):
                writes = [KNEX_WRITE[c] for c in chain if c in KNEX_WRITE]
                add(m.group(3), list(dict.fromkeys(writes)) or ["SELECT"], s + m.start())
        for m in re.finditer(r"([\w$]*)\s*\.\s*(from|into|table|" + "|".join(KNEX_JOINS) + r")\s*\(\s*(['\"`])([\w$]+(?:\.[\w$]+)?)\3", code[s:e]):
            if m.group(1) in NOT_KNEX_OWNERS:
                continue
            kind = m.group(2)
            if kind in KNEX_JOINS:
                ops = ["SELECT"]
            elif kind == "into":
                ops = ["INSERT"]
            else:
                ops = statement_ops(skel, s + m.start(), s + m.end(), s, e)
            add(m.group(4), ops, s + m.start())
        # raw SQL in string / template literals, including same-file and imported constants
        def scan_lit(raw: str, pos: int) -> None:
            if len(raw) > 12 and looks_like_sql(raw):
                for name, op in sql_refs(raw):
                    hits.add(name, op, f"{js.rel}:{js.line(pos)}")

        for a, b, raw in js.literals:
            if s <= a < e:
                scan_lit(raw, a)
        for tok in js.tokens(s, e):
            val = js.consts.get(tok)
            if val is None:
                imp = js.imports.get(tok)
                if imp and imp.target:
                    other = js.project.file(imp.target)
                    if other:
                        val = other.consts.get(imp.name if imp.name != "default" else tok)
            if val is not None:
                scan_lit(val, s)


KNEX_SELECTORS = {"select", "where", "whereIn", "first", "count", "pluck", "join", "leftJoin", "orderBy", "limit", "andWhere",
                  "whereNull", "whereNot", "distinct", "max", "min", "sum", "avg", "groupBy", "having", "columns", "offset", "forUpdate"}


def _lines(code: str, s: int, e: int):
    pos = s
    for raw in code[s:e].split("\n"):
        yield pos, raw
        pos += len(raw) + 1


def rel_to(root: Path, path: Path) -> str:
    return rel(root, path)


def reach_tables(project: JsProject, orm: OrmIndex, js: JsFile, s: int, e: int, hits: TableHits, depth: int = 2, seen: set | None = None) -> None:
    """Tables touched by ``js[s:e]``, following plain function calls into helper code.

    Calls into service/repository units are *not* followed (those get their own nodes).
    """
    seen = seen if seen is not None else set()
    key = (js.path, s, e)
    if key in seen:
        return
    seen.add(key)
    orm.hits_in(js, s, e, hits)
    if depth <= 0:
        return
    unit_files = set(project.units_by_file)
    skel = js.skel
    for tok in sorted(js.tokens(s, e)):
        imp = js.imports.get(tok)
        if imp and imp.target and imp.target not in unit_files:
            other = project.file(imp.target)
            if not other:
                continue
            if imp.name != "default":
                span = find_function(other, imp.name)
                if span:
                    reach_tables(project, orm, other, span[0], span[1], hits, depth - 1, seen)
            else:
                for mm in re.finditer(r"(?<![\w$.])" + re.escape(tok) + r"\s*\.\s*([\w$]+)\s*\(", skel[s:e]):
                    span = find_function(other, mm.group(1))
                    if span:
                        reach_tables(project, orm, other, span[0], span[1], hits, depth - 1, seen)
        elif not imp and re.search(r"(?<![\w$.])" + re.escape(tok) + r"\s*\(", skel[s:e]):
            span = find_function(js, tok)
            if span and not (s <= span[0] < e):
                reach_tables(project, orm, js, span[0], span[1], hits, depth - 1, seen)
