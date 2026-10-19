"""Static scanner for Spring Boot @RestController -> @Service -> @Repository -> JPA @Entity/@Table (Java and Kotlin).

No JVM and no external parser: a comment/string-aware lexer blanks comments (and, for
structure, string contents), a bracket matcher walks the result, and regexes read
declarations from it. Whatever cannot be proven is flagged ``unresolved``.
"""

from __future__ import annotations

import bisect
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..model import CALLS, SENDS, Graph
from ..tables import TableRegistry
from ._common import SKIP_DIRS, TableHits, iter_files, join_path, line_of, read, rel, sql_refs, wire_tables

SUFFIXES = (".java", ".kt")
HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
MAPPINGS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
    "RequestMapping": None,
}
SPRING_DATA_BASES = {
    "JpaRepository", "CrudRepository", "PagingAndSortingRepository", "ListCrudRepository",
    "ListPagingAndSortingRepository", "ReactiveCrudRepository", "ReactiveSortingRepository",
    "R2dbcRepository", "CoroutineCrudRepository", "CoroutineSortingRepository",
    "JpaSpecificationExecutor", "QuerydslPredicateExecutor", "SimpleJpaRepository", "Repository",
}
EXTERNAL_TYPES = {
    "RestTemplate": ("HTTP / Webhook", "outbound HTTP"),
    "RestClient": ("HTTP / Webhook", "outbound HTTP"),
    "WebClient": ("HTTP / Webhook", "outbound HTTP"),
    "OkHttpClient": ("HTTP / Webhook", "outbound HTTP"),
    "HttpClient": ("HTTP / Webhook", "outbound HTTP"),
    "KafkaTemplate": ("Kafka", "message broker"),
    "ReactiveKafkaProducerTemplate": ("Kafka", "message broker"),
    "RedisTemplate": ("Redis", "cache"),
    "StringRedisTemplate": ("Redis", "cache"),
    "ReactiveRedisTemplate": ("Redis", "cache"),
    "RabbitTemplate": ("RabbitMQ", "message broker"),
    "AmqpTemplate": ("RabbitMQ", "message broker"),
    "AmazonS3": ("AWS", "AWS SDK"),
    "S3Client": ("AWS", "AWS SDK"),
    "SqsClient": ("AWS", "AWS SDK"),
    "SnsClient": ("AWS", "AWS SDK"),
}
WRAPPER_TYPES = {"Optional", "ObjectProvider", "Provider", "Lazy"}
JAVA_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "new", "synchronized", "throw", "super",
    "this", "else", "case", "assert", "try", "do", "instanceof",
}
CONFIG_NAMES = {"application.properties", "application.yml", "application.yaml"}

_GAP_STOP = re.compile(r"[;{}()=]")
_KT_MODS = (
    r"public|private|protected|internal|abstract|open|final|override|suspend|inline|lateinit|const|"
    r"data|sealed|enum|annotation|inner|operator|infix|tailrec|external|vararg|expect|actual|fun"
)
_KT_GAP = re.compile(rf"(?:\s+|\b(?:{_KT_MODS})\b|<[^>]*>)*")
_ANN_RE = re.compile(r"(?<![\w@])@[ \t]*(?:\w+[ \t]*:[ \t]*)?([A-Za-z_][\w.]*)")
_CLASS_RE = re.compile(r"(?<![\w.:@])(class|interface|record|object)\s+([A-Za-z_]\w*)")
_KT_CTOR_KW = re.compile(r"(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:private|internal|public|protected)\s+)?constructor\s*(?=\()")
_KT_HEADER = re.compile(r"(?P<hdr>[ \t]*(?::[^{\n]*)?(?:\n[ \t]*[,:][^{\n]*)*)(?P<ws>\s*)(?P<brace>\{)?")
_JAVA_CALL = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
_JAVA_TAIL = re.compile(r"\s*(?:throws\s+[\w.,\s]+?)?\s*(?:default\s+[^;{]*)?([{;])")
_JAVA_FIELD = re.compile(
    r"(?<![\w.@])([A-Za-z_][\w.]*(?:\s*<[^;(){}=]*?>)?(?:\s*\[\s*\])*)\s+([A-Za-z_]\w*)\s*(?=[;=,])"
)
_KT_FUN = re.compile(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.<>?]+\.)?([A-Za-z_]\w*|`[^`]+`)\s*\(")
_KT_PROP = re.compile(
    r"(?<![\w.])(val|var)\s+(?:<[^>]*>\s*)?([A-Za-z_]\w*)\s*(?::\s*(?P<type>[^=\n{;]+?))?[ \t]*(?=[=\n;{]|by\b|$)"
)
_KT_EXPR_END = re.compile(
    r"\n\s*(?:@\w|(?:public|private|protected|internal|override|open|suspend|abstract|final|inline|operator|"
    r"lateinit|const)\b|(?:fun|val|var|class|interface|object|companion|init|constructor)\b|\})"
)
_QUERY_KEYWORDS = re.compile(r"\b(INSERT\s+INTO|DELETE\s+FROM|DELETE|UPDATE|FROM|JOIN)\s+(?:FETCH\s+)?([\w.$]+)", re.I)
_SQL_WORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN|WITH|CALL)\b", re.I)
_OP_BY_KEYWORD = {"INSERT INTO": "INSERT", "DELETE FROM": "DELETE", "DELETE": "DELETE", "UPDATE": "UPDATE", "FROM": "SELECT", "JOIN": "SELECT"}


# --------------------------------------------------------------------------- lexer


def _block_end(raw: str, i: int, kotlin: bool) -> int:
    depth, j, n = 0, i, len(raw)
    while j < n:
        if raw.startswith("/*", j) and (kotlin or depth == 0):
            depth += 1
            j += 2
        elif raw.startswith("*/", j):
            depth -= 1
            j += 2
            if depth <= 0:
                return j
        else:
            j += 1
    return n


def _template_end(raw: str, j: int) -> int:
    depth, n = 1, len(raw)
    while j < n and depth:
        c = raw[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == '"':
            j = _string_end(raw, j, True)
            continue
        j += 1
    return j


def _string_end(raw: str, i: int, kotlin: bool) -> int:
    """Index just past the string literal that starts at ``i`` (``"..."`` or a text block)."""
    n = len(raw)
    if raw.startswith('"""', i):
        j = raw.find('"""', i + 3)
        if j < 0:
            return n
        j += 3
        while j < n and raw[j] == '"':
            j += 1
        return j
    j = i + 1
    while j < n:
        c = raw[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if c == "\n":
            return j
        if kotlin and c == "$" and raw.startswith("${", j):
            j = _template_end(raw, j + 2)
            continue
        j += 1
    return n


def _char_end(raw: str, i: int) -> int:
    n = len(raw)
    if i + 1 < n and raw[i + 1] == "\\":
        j = raw.find("'", i + 2)
        return j + 1 if 0 <= j <= i + 8 else i + 1
    if i + 2 < n and raw[i + 2] == "'":
        return i + 3
    return i + 1


def _blank(buf: list[str], a: int, b: int) -> None:
    for k in range(a, b):
        if buf[k] not in "\r\n":
            buf[k] = " "


def _lex(raw: str, kotlin: bool) -> tuple[str, str, list[tuple[int, int, str]]]:
    """(clean, skel, docs): ``clean`` blanks comments; ``skel`` also blanks string contents.

    Offsets are identical in all three views, so a position found in ``skel`` can be read from ``clean``.
    """
    clean, skel = list(raw), list(raw)
    docs: list[tuple[int, int, str]] = []
    n, i = len(raw), 0
    while i < n:
        c = raw[i]
        nxt = raw[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = raw.find("\n", i)
            j = n if j < 0 else j
            _blank(clean, i, j)
            _blank(skel, i, j)
            i = j
        elif c == "/" and nxt == "*":
            j = _block_end(raw, i, kotlin)
            if raw.startswith("/**", i) and not raw.startswith("/**/", i):
                docs.append((i, j, raw[i:j]))
            _blank(clean, i, j)
            _blank(skel, i, j)
            i = j
        elif c == '"':
            j = _string_end(raw, i, kotlin)
            _blank(skel, i + 1, j - 1)
            i = j
        elif c == "'":
            j = _char_end(raw, i)
            _blank(skel, i + 1, j - 1)
            i = max(j, i + 1)
        else:
            i += 1
    return "".join(clean), "".join(skel), docs


def _match(s: str, i: int) -> int:
    """Index of the bracket closing the one at ``i`` (``skel`` text: strings are already blank)."""
    o = s[i]
    c = {"(": ")", "{": "}", "[": "]"}[o]
    depth = 0
    for j in range(i, len(s)):
        ch = s[j]
        if ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return j
    return len(s) - 1


def _skip_ws(s: str, j: int) -> int:
    while j < len(s) and s[j].isspace():
        j += 1
    return j


def _skip_angle(s: str, j: int) -> int:
    depth = 0
    while j < len(s):
        if s[j] == "<":
            depth += 1
        elif s[j] == ">" and s[j - 1] != "-":
            depth -= 1
            if depth == 0:
                return j + 1
        elif s[j] in "{;(":
            return j
        j += 1
    return j


def _split_spans(text: str, sep: str = ",", angle: bool = False) -> list[tuple[int, int]]:
    """Offsets of the pieces of ``text`` split on top-level ``sep`` (quotes and brackets respected)."""
    spans: list[tuple[int, int]] = []
    depth, start, i, n, quote = 0, 0, 0, len(text), ""
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                i = j + 3 if j >= 0 else n
                continue
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif angle and c == "<":
            depth += 1
        elif angle and c == ">" and text[i - 1 : i] != "-":
            depth -= 1
        elif c == sep and depth <= 0:
            spans.append((start, i))
            start = i + 1
        i += 1
    spans.append((start, n))
    return spans


def _split_top(text: str, sep: str = ",", angle: bool = False) -> list[str]:
    return [text[a:b].strip() for a, b in _split_spans(text, sep, angle) if text[a:b].strip()]


def _close_idx(text: str, i: int) -> int:
    """Quote-aware index of the bracket closing the one at ``i`` in ``text`` (-1 if none)."""
    o = text[i]
    c = {"(": ")", "{": "}", "[": "]"}[o]
    depth, quote, j = 0, "", i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


# --------------------------------------------------------------------- values


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _literal(p: str) -> str | None:
    if len(p) >= 6 and p.startswith('"""') and p.endswith('"""'):
        return p[3:-3]
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        return _unescape(p[1:-1])
    return None


def _items(expr: str) -> list[str]:
    """Elements of an annotation array value: ``{"a","b"}``, ``["a"]``, ``arrayOf("a")`` or a lone value."""
    e = expr.strip()
    e = re.sub(r"^new\s+\w+\s*\[\s*\]\s*", "", e)
    e = re.sub(r"^(?:arrayOf|listOf|setOf)\s*(?=\()", "", e)
    if e and e[0] in "{[(" and _close_idx(e, 0) == len(e) - 1:
        return _split_top(e[1:-1])
    return [e] if e else []


def _ann_args(text: str) -> tuple[list[str], dict[str, str]]:
    pos: list[str] = []
    named: dict[str, str] = {}
    for part in _split_top(text):
        m = re.match(r"([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", part, re.S)
        if m:
            named[m.group(1)] = m.group(2).strip()
        else:
            pos.append(part)
    return pos, named


class _Consts:
    """``static final String`` / ``const val`` constants, so ``@RequestMapping(ApiPaths.USERS)`` resolves."""

    def __init__(self) -> None:
        self.raw: dict[str, str] = {}
        self._cache: dict[str, str | None] = {}
        self._busy: set[str] = set()

    def add_source(self, text: str) -> None:
        for m in re.finditer(r"\bString\s+([A-Z_][A-Za-z0-9_]*)\s*=\s*([^;]+);", text):
            self.raw.setdefault(m.group(1), m.group(2).strip())
        for m in re.finditer(r"\bval\s+([A-Z][A-Z0-9_]*)\s*(?::\s*String\s*)?=\s*([^\n]+(?:\+\s*\n[^\n]+)*)", text):
            self.raw.setdefault(m.group(1), m.group(2).strip())

    def value(self, name: str) -> str | None:
        key = name.split(".")[-1]
        if key in self._cache:
            return self._cache[key]
        expr = self.raw.get(key)
        if expr is None or key in self._busy:
            return None
        self._busy.add(key)
        text, missing = self.eval(expr)
        self._busy.discard(key)
        self._cache[key] = None if missing else text
        return self._cache[key]

    def eval(self, expr: str) -> tuple[str, list[str]]:
        """Concatenate the string pieces of ``expr``; unresolved names become ``{Name}`` and are reported."""
        missing: list[str] = []
        out: list[str] = []
        for part in _split_top(expr, "+"):
            part = part.strip()
            while part.startswith("(") and part.endswith(")") and _close_idx(part, 0) == len(part) - 1:
                part = part[1:-1].strip()
            lit = _literal(part)
            if lit is not None:
                out.append(self._templates(lit, missing))
            elif re.fullmatch(r"[\w.]+", part):
                value = self.value(part)
                if value is None:
                    missing.append(part)
                    out.append("{" + part + "}")
                else:
                    out.append(value)
            else:
                missing.append(part)
                out.append("{?}")
        return "".join(out), missing

    def _templates(self, text: str, missing: list[str]) -> str:
        def sub(m: re.Match) -> str:
            name = m.group(1) or m.group(2)
            value = self.value(name)
            if value is None:
                missing.append(name)
                return "{" + name + "}"
            return value

        return re.sub(r"\$\{([\w.]+)\}|\$([A-Za-z_]\w*)", sub, text) if "$" in text else text


def _clean_path(path: str) -> str:
    """Drop regex constraints from path variables: ``{id:[0-9]+}`` -> ``{id}``."""
    out: list[str] = []
    i = 0
    while i < len(path):
        if path[i] == "{":
            depth, j = 1, i + 1
            while j < len(path) and depth:
                depth += (path[j] == "{") - (path[j] == "}")
                j += 1
            out.append("{" + path[i + 1 : j - 1].split(":", 1)[0] + "}")
            i = j
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


def _snake(name: str) -> str:
    """Spring Boot's default physical naming: ``userAccount`` -> ``user_account``."""
    out: list[str] = []
    for i, ch in enumerate(name):
        if 0 < i < len(name) - 1 and name[i - 1].islower() and ch.isupper() and name[i + 1].islower():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _unquote_ident(text: str) -> str:
    return text.strip().strip('"`[]\\').strip()


def _simple(type_text: str | None) -> str:
    """``org.x.Foo<Bar>`` -> ``Foo``; ``Optional<Foo>`` -> ``Foo``; Kotlin ``Foo?`` -> ``Foo``."""
    if not type_text:
        return ""
    t = re.sub(r"\s+", "", type_text).rstrip("?")
    m = re.match(r"([\w.]+)(?:<(.*)>)?(?:\[\])*\??$", t)
    if not m:
        return ""
    base = m.group(1).split(".")[-1]
    if base in WRAPPER_TYPES and m.group(2):
        return _simple(_split_top(m.group(2), angle=True)[0])
    return base


def _head(type_text: str | None) -> str:
    m = re.match(r"\s*(\w+)", type_text or "")
    return m.group(1) if m else ""


def _label(type_text: str | None) -> str | None:
    if not type_text:
        return None
    return re.sub(r"\s+", " ", type_text).strip().rstrip("?") or None


def _doc_text(raw: str) -> str:
    """Javadoc/KDoc body without the tags (``@param`` ...)."""
    body = re.sub(r"^/\*\*|\*/$", "", raw.strip())
    lines = [re.sub(r"^\s*\*\s?", "", ln) for ln in body.splitlines()]
    kept: list[str] = []
    for ln in lines:
        if ln.strip().startswith("@"):
            break
        kept.append(ln)
    text = "\n".join(kept)
    text = re.sub(r"\{@(?:code|link|linkplain)\s+([^}]*)\}", r"\1", text)
    text = re.sub(r"</?\w+[^>]*>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


# ----------------------------------------------------------------- declarations


@dataclass
class _Ann:
    name: str
    start: int
    end: int
    args: str


@dataclass
class _Param:
    name: str
    type: str
    anns: list[_Ann]
    mods: str
    kw: str | None  # Kotlin val/var
    pos: int


@dataclass
class _Field:
    name: str
    type: str
    anns: list[_Ann]
    static: bool
    transient: bool
    prop: bool  # a persistent/declared property rather than a bare constructor argument
    pos: int


@dataclass
class _Method:
    name: str
    anchor: int
    pos: int
    open: int
    close: int
    body: tuple[int, int] | None
    ret: str | None
    private: bool
    is_ctor: bool
    anns: list[_Ann]


@dataclass
class _Members:
    methods: list[_Method] = field(default_factory=list)
    fields: list[_Field] = field(default_factory=list)


@dataclass
class _Super:
    name: str
    args: list[str]


@dataclass
class _Cls:
    name: str
    kind: str
    src: "_Src"
    kw_start: int
    name_pos: int
    header: str
    supers: list[_Super]
    ctor_span: tuple[int, int] | None
    body_open: int | None
    body_close: int | None
    anns: list[_Ann] = field(default_factory=list)
    _members: _Members | None = None
    _masks: tuple[str, str] | None = None

    @property
    def line(self) -> int:
        return self.src.line(self.name_pos)

    def masks(self) -> tuple[str, str]:
        """The class body with nested braces blanked, and with parenthesised content blanked too."""
        if self._masks is None:
            if self.body_open is None or self.body_close is None:
                self._masks = ("", "")
            else:
                text = self.src.skel[self.body_open + 1 : self.body_close]
                out: list[str] = []
                depth = 0
                for ch in text:
                    if ch == "{":
                        out.append("{" if depth == 0 else " ")
                        depth += 1
                    elif ch == "}":
                        depth = max(0, depth - 1)
                        out.append("}" if depth == 0 else " ")
                    elif depth:
                        out.append("\n" if ch == "\n" else " ")
                    else:
                        out.append(ch)
                masked = "".join(out)
                out, depth = [], 0
                for ch in masked:
                    if ch == "(":
                        out.append("(" if depth == 0 else " ")
                        depth += 1
                    elif ch == ")":
                        depth = max(0, depth - 1)
                        out.append(")" if depth == 0 else " ")
                    elif depth:
                        out.append("\n" if ch == "\n" else " ")
                    else:
                        out.append(ch)
                self._masks = (masked, "".join(out))
        return self._masks

    def members(self) -> _Members:
        if self._members is None:
            self._members = _parse_members(self)
        return self._members


class _Src:
    """One lexed source file."""

    def __init__(self, root: Path, path: Path, raw: str) -> None:
        self.path = path
        self.rel = rel(root, path)
        self.kotlin = path.suffix == ".kt"
        self.raw = raw
        self.clean, self.skel, self.docs = _lex(raw, self.kotlin)
        self.anns = self._find_annotations()
        self._ends = [a.end for a in self.anns]
        self._doc_ends = [d[1] for d in self.docs]
        self.classes: list[_Cls] = []

    def line(self, off: int) -> int:
        return line_of(self.raw, off)

    def _find_annotations(self) -> list[_Ann]:
        out: list[_Ann] = []
        pos, n = 0, len(self.skel)
        while True:
            m = _ANN_RE.search(self.skel, pos)
            if not m:
                return out
            name = m.group(1)
            end, args = m.end(), ""
            if name == "interface":
                pos = end
                continue
            k = end
            while k < n and self.skel[k] in " \t":
                k += 1
            if k < n and self.skel[k] == "(":
                r = _match(self.skel, k)
                args, end = self.clean[k + 1 : r], r + 1
            out.append(_Ann(name.split(".")[-1], m.start(), end, args))
            pos = end

    def _gap_ok(self, gap: str, hop: bool) -> bool:
        if hop:
            return not gap.strip()
        if self.kotlin:
            return _KT_GAP.fullmatch(gap) is not None
        return _GAP_STOP.search(gap) is None

    def attached(self, pos: int) -> list[_Ann]:
        """Annotations that decorate the declaration whose keyword/name starts at ``pos``."""
        out: list[_Ann] = []
        anchor, hop = pos, False
        k = bisect.bisect_right(self._ends, pos)
        while k > 0:
            a = self.anns[k - 1]
            if a.end > anchor:
                k -= 1
                continue
            if not self._gap_ok(self.skel[a.end : anchor], hop):
                break
            out.append(a)
            anchor, hop = a.start, True
            k -= 1
        out.reverse()
        return out

    def doc_before(self, anns: list[_Ann], pos: int) -> str | None:
        start = anns[0].start if anns else pos
        k = bisect.bisect_right(self._doc_ends, start)
        if not k:
            return None
        a, b, text = self.docs[k - 1]
        if not self._gap_ok(self.skel[b:start], False):
            return None
        return _doc_text(text) or None


def _parse_supers(header: str, kotlin: bool) -> list[_Super]:
    h = re.sub(r"\bpermits\b.*", "", header, flags=re.S)
    if kotlin:
        h = re.sub(r"\bwhere\b.*", "", h, flags=re.S).strip().lstrip(":")
    else:
        h = re.sub(r"\b(?:extends|implements)\b", ",", h)
    out: list[_Super] = []
    for item in _split_top(h, ",", angle=True):
        m = re.match(r"\s*([\w.]+)\s*(?:<(.*)>)?", item, re.S)
        if not m:
            continue
        args = [_simple(a) for a in _split_top(m.group(2) or "", angle=True)]
        out.append(_Super(m.group(1).split(".")[-1], [a for a in args if a]))
    return out


def _parse_classes(src: _Src) -> None:
    s, n = src.skel, len(src.skel)
    for m in _CLASS_RE.finditer(s):
        kind, name = m.group(1), m.group(2)
        if kind == "object" and not src.kotlin:
            continue
        if kind == "record" and src.kotlin:
            continue
        j = _skip_ws(s, m.end())
        if j < n and s[j] == "<":
            j = _skip_ws(s, _skip_angle(s, j))
        if src.kotlin:
            km = _KT_CTOR_KW.match(s, j)
            if km:
                j = km.end()
        ctor = None
        if j < n and s[j] == "(" and (src.kotlin or kind == "record"):
            r = _match(s, j)
            ctor = (j + 1, r)
            j = r + 1
        if src.kotlin:
            hm = _KT_HEADER.match(s, j)
            header_end = j + len(hm.group("hdr"))
            body_open = hm.end() - 1 if hm.group("brace") else None
        else:
            b = s.find("{", j)
            body_open = b if b >= 0 else None
            header_end = b if b >= 0 else j
        body_close = _match(s, body_open) if body_open is not None else None
        header = src.clean[j:header_end]
        cls = _Cls(
            name=name,
            kind=kind,
            src=src,
            kw_start=m.start(),
            name_pos=m.start(2),
            header=header,
            supers=_parse_supers(header, src.kotlin),
            ctor_span=ctor,
            body_open=body_open,
            body_close=body_close,
        )
        cls.anns = src.attached(m.start())
        src.classes.append(cls)


def _params(src: _Src, lo: int, hi: int, kotlin: bool) -> list[_Param]:
    """Parameters between ``lo`` and ``hi`` (offsets of the text inside the parentheses)."""
    text = src.clean[lo:hi]
    out: list[_Param] = []
    for a, b in _split_spans(text, angle=True):
        anns = [x for x in src.anns if lo + a <= x.start < lo + b]
        chunk = list(text[a:b])
        for x in anns:
            for k in range(max(x.start - lo - a, 0), min(x.end - lo - a, len(chunk))):
                chunk[k] = " "
        body = "".join(chunk).strip()
        if not body:
            continue
        if kotlin:
            m = re.match(r"((?:\w+\s+)*?)(?:(val|var)\s+)?(\w+)\s*:\s*(.+?)(?:\s*=[^=].*)?$", body, re.S)
            if m:
                out.append(_Param(m.group(3), m.group(4).strip(), anns, m.group(1), m.group(2), lo + a))
        else:
            body = re.sub(r"^final\s+", "", body)
            m = re.match(r"(.+?)\s*(?:\.\.\.)?\s+(\w+)$", body, re.S)
            if m:
                out.append(_Param(m.group(2), m.group(1).strip(), anns, "", None, lo + a))
    return out


def _parse_members(cls: _Cls) -> _Members:
    src, mem = cls.src, _Members()
    if cls.ctor_span:
        for p in _params(src, cls.ctor_span[0], cls.ctor_span[1], src.kotlin):
            is_prop = p.kw is not None or cls.kind == "record"
            mem.fields.append(_Field(p.name, p.type, p.anns, False, False, is_prop, p.pos))
    if cls.body_open is None:
        return mem
    masked, maskp = cls.masks()
    base = cls.body_open + 1
    if src.kotlin:
        _kotlin_members(cls, mem, masked, maskp, base)
    else:
        _java_members(cls, mem, masked, maskp, base)
    return mem


def _word_before(text: str, k: int) -> str:
    j = k
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
        j -= 1
    return text[j:k]


def _in_annotation(src: _Src, pos: int) -> bool:
    k = bisect.bisect_right([a.start for a in src.anns], pos) - 1
    return k >= 0 and src.anns[k].end > pos


def _java_members(cls: _Cls, mem: _Members, masked: str, maskp: str, base: int) -> None:
    src, s = cls.src, cls.src.skel
    starts = [a.start for a in src.anns]
    for mt in _JAVA_CALL.finditer(masked):
        name = mt.group(1)
        if name in JAVA_KEYWORDS:
            continue
        pos = base + mt.start(1)
        k = bisect.bisect_right(starts, pos) - 1
        if k >= 0 and src.anns[k].end > pos:
            continue
        k = mt.start(1) - 1
        while k >= 0 and masked[k].isspace():
            k -= 1
        if k >= 0:
            prev = masked[k]
            if not (prev.isalnum() or prev in "_>]"):
                continue
            if _word_before(masked, k + 1) in JAVA_KEYWORDS | {"throws", "extends", "implements"}:
                continue
        elif name != cls.name:
            continue
        open_p = base + mt.end() - 1
        close_p = _match(s, open_p)
        tail = _JAVA_TAIL.match(s, close_p + 1)
        if not tail:
            continue
        body = None
        if tail.group(1) == "{":
            bo = tail.end(1) - 1
            body = (bo, _match(s, bo))
        # statement start: after the previous ; { } at member level
        cut = max(masked.rfind(c, 0, mt.start(1)) for c in ";{}")
        gap = masked[cut + 1 : mt.start(1)]
        anns = src.attached(pos)
        ret = None
        if name != cls.name:
            text = src.clean[(anns[-1].end if anns else base + cut + 1) : pos]
            text = re.sub(r"\b(public|private|protected|static|final|abstract|default|synchronized|native|strictfp)\b", " ", text)
            text = re.sub(r"^\s*<[^>]*>", " ", text)
            ret = re.sub(r"\s+", " ", text).strip() or None
        mem.methods.append(
            _Method(
                name=name,
                anchor=pos,
                pos=pos,
                open=open_p,
                close=close_p,
                body=body,
                ret=ret,
                private=bool(re.search(r"\bprivate\b", gap)),
                is_ctor=name == cls.name,
                anns=anns,
            )
        )
    for fm in _JAVA_FIELD.finditer(maskp):
        typ, name = fm.group(1), fm.group(2)
        if typ in ("return", "throw", "new", "else", "package", "import") or typ.split(".")[0] in JAVA_KEYWORDS:
            continue
        pos = base + fm.start(1)
        cut = max(maskp.rfind(c, 0, fm.start(1)) for c in ";{}")
        mods = maskp[cut + 1 : fm.start(1)]
        mem.fields.append(
            _Field(
                name=name,
                type=src.clean[pos : base + fm.end(1)].strip(),
                anns=src.attached(pos),
                static=bool(re.search(r"\bstatic\b", mods)),
                transient=bool(re.search(r"\btransient\b", mods)),
                prop=True,
                pos=pos,
            )
        )


def _kotlin_members(cls: _Cls, mem: _Members, masked: str, maskp: str, base: int) -> None:
    src, s = cls.src, cls.src.skel
    for mt in _KT_FUN.finditer(masked):
        name = mt.group(1).strip("`")
        pos = base + mt.start(1)
        open_p = base + mt.end() - 1
        close_p = _match(s, open_p)
        eol = s.find("\n", close_p + 1)
        eol = len(s) if eol < 0 else eol
        rest = s[close_p + 1 : eol]
        stop = re.search(r"\{|(?<![=!<>])=(?![=>])", rest)
        body = None
        if stop and stop.group() == "{":
            bo = close_p + 1 + stop.start()
            body = (bo, _match(s, bo))
        elif stop:
            m_rest = masked[close_p + 1 - base :]
            end = _KT_EXPR_END.search(m_rest)
            body = (close_p + 1 + stop.start(), close_p + 1 + (end.start() if end else len(m_rest)))
        ret = None
        rm = re.match(r"\s*:\s*([^{=\n]+)", src.clean[close_p + 1 : eol])
        if rm:
            ret = rm.group(1).strip() or None
        anchor = base + mt.start()
        line_start = max(masked.rfind("\n", 0, mt.start()), masked.rfind("}", 0, mt.start()), masked.rfind(";", 0, mt.start()))
        mem.methods.append(
            _Method(
                name=name,
                anchor=anchor,
                pos=pos,
                open=open_p,
                close=close_p,
                body=body,
                ret=ret,
                private=bool(re.search(r"\bprivate\b", masked[line_start + 1 : mt.start()])),
                is_ctor=False,
                anns=src.attached(anchor),
            )
        )
    for pm in _KT_PROP.finditer(maskp):
        pos = base + pm.start()
        cut = max(maskp.rfind(c, 0, pm.start()) for c in "\n;{}")
        mods = maskp[cut + 1 : pm.start()]
        mem.fields.append(
            _Field(
                name=pm.group(2),
                type=(pm.group("type") or "").strip(),
                anns=src.attached(pos),
                static=False,
                transient=False,
                prop=True,
                pos=pos,
            )
        )


# --------------------------------------------------------------------- config


def _yaml_flat(text: str) -> dict[str, str]:
    """Flatten the first document of a simple YAML file to dotted keys (scalars only)."""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        if line.strip() == "---" and out:
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([\w.\-\"']+)\s*:\s*(.*)$", line)
        if not m:
            continue
        indent, key, val = len(m.group(1)), m.group(2).strip("\"'"), m.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        val = re.sub(r"\s+#.*$", "", val).strip().strip("\"'")
        if val:
            out[".".join([k for _, k in stack] + [key])] = val
        stack.append((indent, key))
    return out


def _props_flat(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#!":
            continue
        m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _placeholder(value: str) -> str | None:
    m = re.fullmatch(r"\$\{[^:}]+:([^}]*)\}", value)
    if m:
        return m.group(1)
    return None if "${" in value else value


def _norm_prefix(value: str | None) -> str:
    if not value:
        return ""
    value = _placeholder(value.strip())
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


# ----------------------------------------------------------------- entities


@dataclass
class _Entity:
    cls: _Cls
    table: str
    schema: str | None
    jpa_name: str
    columns: list[dict]


def _first_string(consts: _Consts, expr: str | None) -> str | None:
    if not expr:
        return None
    items = _items(expr)
    if not items:
        return None
    text, missing = consts.eval(items[0])
    return None if missing or not text else text


def _named_ann(anns: list[_Ann], name: str) -> _Ann | None:
    return next((a for a in anns if a.name == name), None)


def _entity_columns(proj: "_Project", cls: _Cls, consts: _Consts, seen: set[str] | None = None) -> list[dict]:
    seen = seen or set()
    if cls.name in seen:
        return []
    seen.add(cls.name)
    cols: list[dict] = []
    for sup in cls.supers:  # @MappedSuperclass parents contribute their columns
        parent = proj.classes.get(sup.name)
        if parent and _named_ann(parent.anns, "MappedSuperclass"):
            cols.extend(_entity_columns(proj, parent, consts, seen))
    for f in cls.members().fields:
        if f.static or f.transient or not f.prop:
            continue
        names = {a.name: a for a in f.anns}
        if names.keys() & {"Transient", "OneToMany", "ManyToMany", "Embedded", "ElementCollection", "Formula"}:
            continue
        one = names.get("OneToOne")
        if one and "mappedBy" in _ann_args(one.args)[1]:
            continue
        column: dict = {}
        relation = "ManyToOne" in names or one is not None
        target = _simple(f.type)
        if relation:
            jc = names.get("JoinColumn")
            jname = _first_string(consts, _ann_args(jc.args)[1].get("name")) if jc else None
            column["name"] = _unquote_ident(jname) if jname else f"{_snake(f.name)}_id"
            column["type"] = target or None
            column["references_entity"] = target
        else:
            col = names.get("Column")
            pos, named = _ann_args(col.args) if col else ([], {})
            cname = _first_string(consts, named.get("name") or (pos[0] if pos else None))
            column["name"] = _unquote_ident(cname) if cname else _snake(f.name)
            column["type"] = _label(f.type)
            if col and named.get("nullable") == "false":
                column["required"] = True
        if "Id" in names or "EmbeddedId" in names:
            column["pk"] = True
        cols.append(column)
    return cols


def _collect_entities(proj: "_Project", consts: _Consts, default_schema: str | None) -> dict[str, _Entity]:
    found: dict[str, _Entity] = {}
    for cls in proj.class_list:
        ent, tbl = _named_ann(cls.anns, "Entity"), _named_ann(cls.anns, "Table")
        if (ent is None and tbl is None) or cls.kind not in ("class", "record"):
            continue
        pos, named = _ann_args(tbl.args) if tbl else ([], {})
        table = _first_string(consts, named.get("name") or (pos[0] if pos else None))
        schema = _first_string(consts, named.get("schema")) or default_schema
        table = _unquote_ident(table) if table else _snake(cls.name)
        jpa_name = _first_string(consts, _ann_args(ent.args)[1].get("name")) if ent else None
        found[cls.name] = _Entity(cls, table, _unquote_ident(schema) if schema else None, jpa_name or cls.name, [])
    tables = {name: e.table for name, e in found.items()}
    for name, e in found.items():
        cols = _entity_columns(proj, e.cls, consts)
        for col in cols:
            target = col.pop("references_entity", None)
            if target and target in tables:
                col["references"] = tables[target]
        e.columns = cols
    return found


def _register_entity(registry: TableRegistry, e: _Entity) -> None:
    registry.add(e.table, schema=e.schema, kind="table", source=f"{e.cls.src.rel}:{e.cls.line}")
    registry.set_columns(e.table, e.columns, authoritative=False)


# ------------------------------------------------------------------ project


class _Project:
    def __init__(self, root: Path, only: tuple[str, ...] | None = None) -> None:
        self.root = root
        self.srcs: list[_Src] = []
        self.class_list: list[_Cls] = []
        self.classes: dict[str, _Cls] = {}
        self.consts = _Consts()
        self.warnings: list[str] = []
        for path in iter_files(root, SUFFIXES):
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                raw = read(path)
                if only and not any(tok in raw for tok in only):
                    continue
                src = _Src(root, path, raw)
                _parse_classes(src)
            except Exception as exc:  # a malformed file must not kill the scan
                self.warnings.append(f"skip {rel(root, path)}: {exc}")
                continue
            self.consts.add_source(src.clean)
            self.srcs.append(src)
            for cls in src.classes:
                self.class_list.append(cls)
                self.classes.setdefault(cls.name, cls)

    def deps(self, cls: _Cls, seen: set[str] | None = None) -> dict[str, str]:
        """Injected collaborators of ``cls``: variable name -> declared type (superclass fields included)."""
        seen = seen if seen is not None else set()
        if cls.name in seen:
            return {}
        seen.add(cls.name)
        out: dict[str, str] = {}
        for sup in cls.supers:
            parent = self.classes.get(sup.name)
            if parent is not None and parent is not cls and parent.kind == "class":
                out.update(self.deps(parent, seen))
        mem = cls.members()
        for f in mem.fields:
            if not f.static and f.type:
                out[f.name] = f.type
        for m in mem.methods:
            if m.is_ctor:
                for p in _params(cls.src, m.open + 1, m.close, False):
                    out[p.name] = p.type
        return out


def _app_prefixes(root: Path) -> list[tuple[Path, str]]:
    """(module dir, URL prefix) for every application.properties/yml: context-path + servlet path."""
    out: list[tuple[Path, str]] = []
    for path in iter_files(root, (".properties", ".yml", ".yaml")):
        if path.name not in CONFIG_NAMES:
            continue
        flat = _props_flat(read(path)) if path.suffix == ".properties" else _yaml_flat(read(path))
        ctx = flat.get("server.servlet.context-path") or flat.get("server.context-path")
        servlet = flat.get("spring.mvc.servlet.path")
        prefix = _norm_prefix(ctx) + _norm_prefix(servlet)
        module = path.parent.parent.parent if path.parent.name == "resources" else path.parent
        out.append((module, prefix))
    return sorted(out, key=lambda t: -len(t[0].parts))


def _default_schema(root: Path) -> str | None:
    for path in iter_files(root, (".properties", ".yml", ".yaml")):
        if path.name not in CONFIG_NAMES:
            continue
        flat = _props_flat(read(path)) if path.suffix == ".properties" else _yaml_flat(read(path))
        value = flat.get("spring.jpa.properties.hibernate.default_schema") or flat.get("hibernate.default_schema")
        if value and "${" not in value:
            return value
    return None


# -------------------------------------------------------------------- queries


def _ops_for_name(name: str) -> list[str]:
    """SQL operations implied by a repository method name (derived queries and CRUD verbs)."""

    def has(*prefixes: str) -> bool:
        return any(re.match(rf"{p}(?=[A-Z]|$)", name) for p in prefixes)

    if has("delete", "remove", "purge", "clear"):
        return ["DELETE"]
    if has("save", "upsert", "store", "merge"):
        return ["INSERT", "UPDATE"]
    if has("insert", "create", "add", "persist"):
        return ["INSERT"]
    if has("update", "modify", "set", "mark", "change"):
        return ["UPDATE"]
    if has("find", "get", "read", "query", "search", "stream", "count", "exists", "list", "load", "select", "fetch", "is", "has"):
        return ["SELECT"]
    return []


def _jpql_refs(query: str, entity_tables: dict[str, str]) -> list[tuple[str, str]]:
    """(table, op) pairs from a JPQL string: entity names map to their tables, path joins are skipped."""
    out: list[tuple[str, str]] = []
    for kw, token in _QUERY_KEYWORDS.findall(query):
        op = _OP_BY_KEYWORD[" ".join(kw.upper().split())]
        name = token.split(".")[-1]
        if "." in token and name not in entity_tables:
            continue  # `JOIN u.leads`, `EXTRACT(YEAR FROM u.created)`
        table = entity_tables.get(name)
        if table:
            out.append((table, op))
    return out


def _sql_literals(clean: str, kotlin: bool) -> list[tuple[int, str]]:
    """String literals in ``clean`` text (adjacent ``"a" + "b"`` pieces merged), as (offset, value)."""
    out: list[tuple[int, int, str]] = []
    i, n = 0, len(clean)
    while i < n:
        c = clean[i]
        if c == '"':
            j = _string_end(clean, i, kotlin)
            lit = _literal(clean[i:j]) if j > i + 1 else None
            if lit is not None:
                if out and re.fullmatch(r"\s*\+\s*", clean[out[-1][1] : i]):
                    out[-1] = (out[-1][0], j, out[-1][2] + lit)
                else:
                    out.append((i, j, lit))
            i = max(j, i + 1)
        elif c == "'":
            i = max(_char_end(clean, i), i + 1)
        else:
            i += 1
    return [(a, v) for a, _, v in out]


def _looks_like_sql(text: str) -> bool:
    return len(text) > 12 and bool(_SQL_WORD_RE.search(text)) and not text.rstrip().endswith((".", "!"))


# ------------------------------------------------------------------- scanner


class SpringScanner:
    stack = "java/spring"

    def __init__(self, root: Path, graph: Graph, registry: TableRegistry, openapi: str | None = None) -> None:
        self.root = root
        self.graph = graph
        self.registry = registry
        self.openapi_source = openapi
        self.warnings: list[str] = []
        self.openapi: dict[tuple[str, str], dict] = {}

    # ----------------------------------------------------------------- detection

    @staticmethod
    def detect(root: Path) -> bool:
        """True for a Spring Boot build file, or a ``@SpringBootApplication`` class."""
        sources: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if name in ("pom.xml", "build.gradle", "build.gradle.kts"):
                    text = read(path)
                    if "spring-boot" in text or "org.springframework.boot" in text:
                        return True
                elif name.endswith(SUFFIXES):
                    sources.append(path)
        for path in sources:
            try:
                if "@SpringBootApplication" in read(path):
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def register_tables(root: Path, registry: TableRegistry) -> None:
        """Add the tables JPA ``@Entity`` / Spring Data JDBC ``@Table`` classes declare to the registry."""
        proj = _Project(root, only=("@Entity", "@Table", "@MappedSuperclass"))
        for entity in _collect_entities(proj, proj.consts, _default_schema(root)).values():
            _register_entity(registry, entity)

    # ---------------------------------------------------------------------- scan

    def scan(self) -> None:
        self.proj = _Project(self.root)
        self.warnings.extend(self.proj.warnings)
        self.prefixes = _app_prefixes(self.root)
        self.entities = _collect_entities(self.proj, self.proj.consts, _default_schema(self.root))
        for entity in self.entities.values():
            if self.registry.resolve(entity.table) is None:
                _register_entity(self.registry, entity)
        self.entity_tables = {e.cls.name: e.table for e in self.entities.values()}
        for e in self.entities.values():
            self.entity_tables.setdefault(e.jpa_name, e.table)
        self._index_roles()
        self._index_repo_calls()
        self._load_openapi()
        self._scan_routes()
        self._scan_services()
        self._scan_repositories()

    # --------------------------------------------------------------------- roles

    def _spring_data_entity(self, cls: _Cls) -> tuple[bool, str | None]:
        """(is a Spring Data repository, entity class name) from the supertypes of ``cls``."""
        is_repo, entity = False, None
        for sup in cls.supers:
            base = sup.name in SPRING_DATA_BASES or sup.name.endswith(("Repository", "Executor"))
            if not base or sup.name == cls.name:
                continue
            if sup.name == "Repository" and not sup.args:
                continue
            is_repo = True
            for arg in sup.args[:1]:
                if arg in self.entities and entity is None:
                    entity = arg
        return is_repo, entity

    def _is_mybatis(self, cls: _Cls) -> bool:
        raw = cls.src.raw
        return "org.apache.ibatis" in raw or "org.mybatis" in raw

    def _role_of(self, cls: _Cls) -> str | None:
        names = {a.name for a in cls.anns}
        if cls.kind in ("class", "object") and names & {"RestController", "Controller"}:
            return "controller"
        if "FeignClient" in names:
            return "feign"
        if self._spring_data_entity(cls)[0]:
            return "repo"
        if names & {"Repository"} or re.search(r"(Repository|Dao|DAO)(Impl)?$", cls.name):
            return "repo"
        if self._is_mybatis(cls) and ("Mapper" in names or cls.name.endswith("Mapper")):
            return "repo"
        if "Service" in names or re.search(r"Service(Impl)?$", cls.name):
            return "service"
        return None

    def _index_roles(self) -> None:
        classes = self.proj.classes
        self.role: dict[str, str | None] = {}
        for cls in self.proj.class_list:
            self.role.setdefault(cls.name, self._role_of(cls))
        self.canon: dict[str, str] = {}
        self.members: dict[str, list[_Cls]] = {}
        for cls in self.proj.class_list:
            role = self.role.get(cls.name)
            if role not in ("service", "repo") or cls.name in self.canon:
                continue
            target = cls.name
            if cls.kind != "interface":
                for sup in cls.supers:
                    other = classes.get(sup.name)
                    if other and other.kind == "interface" and self.role.get(other.name) == role:
                        target = other.name
                        break
                else:
                    stem = cls.name[:-4] if cls.name.endswith("Impl") else None
                    if stem and stem in classes and self.role.get(stem) == role:
                        target = stem
            self.canon[cls.name] = target
        for name, target in self.canon.items():
            self.members.setdefault(target, [])
        for cls in self.proj.class_list:
            if cls.name in self.canon and cls not in self.members[self.canon[cls.name]]:
                # a class collapsed into its interface still contributes its body; keep the interface first
                lst = self.members[self.canon[cls.name]]
                lst.append(cls)
        for lst in self.members.values():
            lst.sort(key=lambda c: c.kind != "interface")
        # entity per repository
        self.repo_entity: dict[str, str | None] = {}
        for target, lst in self.members.items():
            if self.role.get(target) != "repo":
                continue
            entity = None
            for cls in lst:
                entity = entity or self._spring_data_entity(cls)[1]
            self.repo_entity[target] = entity

    def _resolve_dep(self, type_text: str) -> tuple[str, str] | None:
        simple, head = _simple(type_text), _head(type_text)
        cls = self.proj.classes.get(simple)
        if cls is not None:
            role = self.role.get(cls.name)
            if role in ("service", "repo"):
                return role, self.canon.get(cls.name, cls.name)
            if role == "feign":
                return "feign", cls.name
        ext = EXTERNAL_TYPES.get(simple) or EXTERNAL_TYPES.get(head)
        if ext:
            return "ext", simple if simple in EXTERNAL_TYPES else head
        return None

    # ------------------------------------------------------------------ usage

    def _usage_re(self, names: list[str]) -> re.Pattern | None:
        if not names:
            return None
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        return re.compile(rf"(?<![\w.])(?:this\s*\.\s*)?({alt})\s*(?:\?|!!)?\s*(?:\.|::)\s*(\w+)")

    def _uses(self, cls: _Cls, span: tuple[int, int], deps: dict[str, str]) -> list[tuple[str, str, int]]:
        pattern = self._usage_re(list(deps))
        if pattern is None:
            return []
        s = cls.src.skel
        return [(m.group(1), m.group(2), m.start()) for m in pattern.finditer(s, span[0], span[1])]

    def _handler_uses(self, cls: _Cls, meth: _Method, deps: dict[str, str], depth: int = 2, seen: set[str] | None = None):
        """Collaborator calls reachable from a handler: its body plus same-class helpers it calls."""
        seen = seen if seen is not None else set()
        seen.add(meth.name)
        if meth.body is None:
            return []
        uses = self._uses(cls, meth.body, deps)
        if depth:
            text = cls.src.skel[meth.body[0] : meth.body[1]]
            for other in cls.members().methods:
                if other.name in seen or other.is_ctor or other.body is None:
                    continue
                if re.search(rf"(?<![\w.])(?:this\s*\.\s*)?{re.escape(other.name)}\s*\(", text):
                    uses += self._handler_uses(cls, other, deps, depth - 1, seen)
        return uses

    def _index_repo_calls(self) -> None:
        """Inherited CRUD (``repo.save(...)``) as callers use it: repo -> {method: evidence}."""
        self.repo_calls: dict[str, dict[str, str]] = {}
        for cls in self.proj.class_list:
            if cls.body_open is None:
                continue
            deps = self.proj.deps(cls)
            repo_vars = {v: self._resolve_dep(t) for v, t in deps.items()}
            repo_vars = {v: r[1] for v, r in repo_vars.items() if r and r[0] == "repo"}
            if not repo_vars:
                continue
            for var, method, off in self._uses(cls, (cls.body_open, cls.body_close or len(cls.src.skel)), {v: "" for v in repo_vars}):
                self.repo_calls.setdefault(repo_vars[var], {}).setdefault(method, f"{cls.src.rel}:{cls.src.line(off)}")

    # ----------------------------------------------------------------- openapi

    def _load_openapi(self) -> None:
        candidates = [self.openapi_source] if self.openapi_source else [
            "contracts/openapi.json", "openapi.json", "docs/openapi.json", "openapi/openapi.json",
            "api/openapi.json", "static/openapi.json", "swagger.json",
            "src/main/resources/openapi.json", "src/main/resources/static/openapi.json",
        ]
        for candidate in candidates:
            spec = self._read_spec(candidate)
            if spec is None:
                continue
            for route_path, methods in (spec.get("paths") or {}).items():
                if isinstance(methods, dict):
                    for method, op in methods.items():
                        if method.upper() in HTTP_VERBS and isinstance(op, dict):
                            self.openapi[(method.upper(), route_path)] = op
            self.graph.project.setdefault("openapi", candidate)
            return
        if self.openapi_source:
            self.warnings.append(f"could not read spec: {self.openapi_source}")

    def _read_spec(self, candidate: str) -> dict | None:
        try:
            if candidate.startswith(("http://", "https://")):
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

    # ------------------------------------------------------------------ routes

    def _prefix_for(self, path: Path) -> str:
        for module, prefix in self.prefixes:
            try:
                path.relative_to(module)
            except ValueError:
                continue
            return prefix
        return ""

    def _mapping(self, ann: _Ann) -> tuple[str, list[str], list[str]]:
        """(path, verbs, unresolved names) of one mapping annotation; arrays take their first path."""
        pos, named = _ann_args(ann.args)
        expr = named.get("value") or named.get("path") or (pos[0] if pos else None)
        path, missing = "", []
        if expr:
            items = _items(expr)
            if items:
                path, missing = self.proj.consts.eval(items[0])
        verbs = [MAPPINGS[ann.name]] if MAPPINGS.get(ann.name) else []
        if ann.name == "RequestMapping":
            verbs = re.findall(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", named.get("method", "")) or ["ANY"]
        return path, verbs, missing

    def _scan_routes(self) -> None:
        for cls in self.proj.class_list:
            if self.role.get(cls.name) != "controller" or self.proj.classes.get(cls.name) is not cls:
                continue
            src = cls.src
            cls_map = _named_ann(cls.anns, "RequestMapping")
            cls_path, _, cls_missing = self._mapping(cls_map) if cls_map else ("", [], [])
            tag = _named_ann(cls.anns, "Tag")
            tag_name = _first_string(self.proj.consts, _ann_args(tag.args)[1].get("name")) if tag else None
            group = tag_name or re.sub(r"(Rest)?Controller$", "", cls.name) or cls.name
            ctx = self._prefix_for(src.path)
            deps = self.proj.deps(cls)
            for meth in cls.members().methods:
                if meth.is_ctor:
                    continue
                for ann in meth.anns:
                    if ann.name not in MAPPINGS:
                        continue
                    sub, verbs, missing = self._mapping(ann)
                    full = _clean_path(join_path(ctx, cls_path, sub))
                    for verb in verbs:
                        self._add_route(cls, meth, ann, verb, full, group, deps, missing + cls_missing)

    def _add_route(self, cls: _Cls, meth: _Method, ann: _Ann, verb: str, path: str, group: str, deps: dict[str, str], missing: list[str]) -> None:
        src = cls.src
        node_id = f"route:{verb}:{path}"
        spec = self.openapi.get((verb, path), {})
        doc = src.doc_before(meth.anns, meth.anchor)
        op = _named_ann(meth.anns, "Operation") or _named_ann(meth.anns, "ApiOperation")
        op_pos, op_named = _ann_args(op.args) if op else ([], {})
        consts = self.proj.consts
        summary = _first_string(consts, op_named.get("summary") or op_named.get("value") or (op_pos[0] if op_pos else None))
        description = _first_string(consts, op_named.get("description") or op_named.get("notes"))
        doc_first = doc.splitlines()[0].strip() if doc else None
        params, request_body = self._handler_params(src, meth)
        uses = self._handler_uses(cls, meth, deps)
        services, repos, feigns, externals = [], [], [], []
        for var, _, _ in uses:
            res = self._resolve_dep(deps.get(var, ""))
            if not res:
                continue
            bucket = {"service": services, "repo": repos, "feign": feigns, "ext": externals}[res[0]]
            if res[1] not in bucket:
                bucket.append(res[1])
        inferred = False
        if meth.body is None:  # no body to read (abstract/interface): link every injected service
            for var, typ in deps.items():
                res = self._resolve_dep(typ)
                if res and res[0] == "service" and res[1] not in services:
                    services.append(res[1])
                    inferred = True
        ret = _label(meth.ret)
        if ret:
            m = re.match(r"ResponseEntity<(.*)>$", ret)
            ret = m.group(1) if m else ret
        has_hidden = _named_ann(meth.anns, "Hidden") is not None or _named_ann(cls.anns, "Hidden") is not None
        self.graph.add_node(
            node_id,
            "route",
            f"{verb} {path}",
            group=(spec.get("tags") or [group])[0],
            method=verb,
            path=path,
            handler=meth.name,
            summary=spec.get("summary") or summary or doc_first,
            description=spec.get("description") or description or doc,
            doc_source="openapi" if spec.get("description") else ("annotation" if description else ("javadoc" if doc else None)),
            deprecated=spec.get("deprecated") or (True if _named_ann(meth.anns, "Deprecated") else None),
            operation_id=spec.get("operationId"),
            tags=spec.get("tags") or None,
            params=params or None,
            request_body=request_body,
            response_model=ret if ret and ret not in ("void", "Unit") else None,
            source=f"{src.rel}:{src.line(meth.pos)}",
            in_spec=bool(spec),
            internal=True if has_hidden else None,
            standalone=None if (services or repos) else True,
            unresolved=True if missing else None,
            hint=f"path constant could not be resolved: {', '.join(dict.fromkeys(missing))}" if missing else None,
        )
        evidence = f"{src.rel}:{src.line(meth.pos)}"
        for name in services:
            self.graph.add_edge(node_id, f"service:{name}", CALLS, evidence=evidence, confidence="inferred" if inferred else "static")
        for name in repos:
            self.graph.add_edge(node_id, f"repo:{name}", CALLS, evidence=evidence)
        for name in feigns:
            self._wire_external(node_id, "HTTP / Webhook", "outbound HTTP", name, evidence)
        for name in externals:
            label, desc = EXTERNAL_TYPES[name]
            self._wire_external(node_id, label, desc, name, evidence)

    def _handler_params(self, src: _Src, meth: _Method) -> tuple[list[dict], str | None]:
        out: list[dict] = []
        body = None
        for p in _params(src, meth.open + 1, meth.close, src.kotlin):
            names = {a.name: a for a in p.anns}
            if "RequestBody" in names:
                body = _simple(p.type) or None
            for ann_name, where in (("PathVariable", "path"), ("RequestParam", "query"), ("RequestHeader", "header")):
                ann = names.get(ann_name)
                if not ann:
                    continue
                pos, named = _ann_args(ann.args)
                nm = _first_string(self.proj.consts, named.get("value") or named.get("name") or (pos[0] if pos else None))
                optional = named.get("required") == "false" or "defaultValue" in named or p.type.rstrip().endswith("?")
                out.append({k: v for k, v in {
                    "name": nm or p.name, "in": where, "required": not optional, "type": _label(p.type),
                }.items() if v is not None})
        return out, body

    # ---------------------------------------------------------------- services

    def _scan_services(self) -> None:
        for canon, members in self.members.items():
            if self.role.get(canon) != "service":
                continue
            primary = self.proj.classes[canon]
            src = primary.src
            node_id = f"service:{canon}"
            deps: dict[str, str] = {}
            methods: list[str] = []
            for cls in members:
                deps.update(self.proj.deps(cls))
                for m in cls.members().methods:
                    if not m.is_ctor and not m.private and m.name not in methods:
                        methods.append(m.name)
            group = re.sub(r"Service(Impl)?$", "", canon) or canon
            self.graph.add_node(
                node_id,
                "service",
                canon,
                group=group,
                source=f"{src.rel}:{primary.line}",
                description=src.doc_before(primary.anns, primary.kw_start),
                methods=methods or None,
                impl=[c.name for c in members if c is not primary] or None,
            )
            evidence = f"{src.rel}:{primary.line}"
            for typ in deps.values():
                res = self._resolve_dep(typ)
                if not res:
                    continue
                kind, name = res
                if kind == "repo":
                    self.graph.add_edge(node_id, f"repo:{name}", CALLS, evidence=evidence)
                elif kind == "service" and name != canon:
                    self.graph.add_edge(node_id, f"service:{name}", CALLS, evidence=evidence)
                elif kind == "feign":
                    self._wire_external(node_id, "HTTP / Webhook", "outbound HTTP", name, evidence)
                elif kind == "ext":
                    label, desc = EXTERNAL_TYPES[name]
                    self._wire_external(node_id, label, desc, name, evidence)

    def _wire_external(self, node_id: str, label: str, desc: str, via: str, evidence: str) -> None:
        ext_id = f"external:{label}"
        self.graph.add_node(ext_id, "external", label, group="external", via=via, description=desc)
        self.graph.add_edge(node_id, ext_id, SENDS, evidence=evidence)

    # ------------------------------------------------------------ repositories

    def _scan_repositories(self) -> None:
        xml_sql = self._mybatis_xml()
        for canon, members in self.members.items():
            if self.role.get(canon) != "repo":
                continue
            primary = self.proj.classes[canon]
            src = primary.src
            node_id = f"repo:{canon}"
            hits = TableHits(self.registry)
            hint = self._repo_tables(canon, members, hits)
            for sql, evidence in xml_sql.pop(canon, []):
                hits.add_sql(sql, evidence)
            entity = self.repo_entity.get(canon)
            self.graph.add_node(
                node_id,
                "repo",
                canon,
                group=re.sub(r"(Jdbc|Jpa)?(Repository|Dao|DAO|Mapper)(Impl)?$", "", canon) or canon,
                source=f"{src.rel}:{primary.line}",
                description=src.doc_before(primary.anns, primary.kw_start),
                entity=entity,
                unresolved=None if hits else True,
                hint=None if hits else (hint or "no table reference resolved"),
            )
            wire_tables(self.graph, node_id, hits)
            evidence = f"{src.rel}:{primary.line}"
            for cls in members:
                for typ in self.proj.deps(cls).values():
                    res = self._resolve_dep(typ)
                    if res and res[0] == "ext":
                        label, desc = EXTERNAL_TYPES[res[1]]
                        self._wire_external(node_id, label, desc, res[1], evidence)
                    elif res and res[0] == "feign":
                        self._wire_external(node_id, "HTTP / Webhook", "outbound HTTP", res[1], evidence)
        for name, items in xml_sql.items():  # XML mappers whose interface is not in the project
            hits = TableHits(self.registry)
            for sql, evidence in items:
                hits.add_sql(sql, evidence)
            if hits:
                self.graph.add_node(f"repo:{name}", "repo", name, group=re.sub(r"Mapper$", "", name) or name, source=items[0][1])
                wire_tables(self.graph, f"repo:{name}", hits)

    def _repo_tables(self, canon: str, members: list[_Cls], hits: TableHits) -> str | None:
        """Fill ``hits`` for one repository. Returns a hint when nothing could be resolved."""
        entity_name = self.repo_entity.get(canon)
        entity = self.entities.get(entity_name) if entity_name else None
        declared: set[str] = set()
        hint = None
        for cls in members:
            src = cls.src
            data_repo = self._spring_data_entity(cls)[0]
            mybatis = self._is_mybatis(cls)
            for m in cls.members().methods:
                if m.is_ctor:
                    continue
                declared.add(m.name)
                evidence = f"{src.rel}:{src.line(m.pos)}"
                self._method_hits(cls, m, entity, hits, evidence, data_repo, mybatis)
            if cls.kind != "interface" and cls.body_open is not None and not data_repo:
                self._literal_hits(cls, hits)
        if entity is None and any(self._spring_data_entity(c)[0] for c in members):
            args = next((s.args[0] for c in members for s in c.supers if s.args and s.name.endswith(("Repository", "Executor"))), "?")
            hint = f"entity {args} is not a @Entity/@Table class in this project"
        if entity is not None:
            primary = self.proj.classes[canon]
            for method, evidence in self.repo_calls.get(canon, {}).items():
                if method in declared:
                    continue
                for op in _ops_for_name(method):
                    hits.add(entity.table, op, evidence)
            # A repository of an entity reads it at minimum, even when nothing else says so.
            if not any(h["record"]["name"].lower() == entity.table.lower() for h in hits.hits.values()):
                hits.add(entity.table, "SELECT", f"{primary.src.rel}:{primary.line}")
        return hint

    def _method_hits(self, cls: _Cls, m: _Method, entity: _Entity | None, hits: TableHits, evidence: str, data_repo: bool, mybatis: bool) -> None:
        consts = self.proj.consts
        query = _named_ann(m.anns, "Query")
        if query:
            pos, named = _ann_args(query.args)
            expr = named.get("value") or (pos[0] if pos else None)
            sql = " ".join(consts.eval(e)[0] for e in _items(expr)) if expr else ""
            if named.get("nativeQuery") == "true":
                hits.add_sql(sql, evidence)
            else:
                for table, op in _jpql_refs(sql, self.entity_tables):
                    hits.add(table, op, evidence)
                if entity is None:
                    hits.add_sql(sql, evidence)
            return
        for ann in m.anns:
            if ann.name in ("Select", "Insert", "Update", "Delete"):
                pos, named = _ann_args(ann.args)
                expr = named.get("value") or (pos[0] if pos else None)
                if expr:
                    hits.add_sql(" ".join(consts.eval(e)[0] for e in _items(expr)), evidence)
                return
        if entity is not None and data_repo and cls.kind == "interface":
            for op in _ops_for_name(m.name):
                hits.add(entity.table, op, evidence)

    def _literal_hits(self, cls: _Cls, hits: TableHits) -> None:
        """SQL held in string literals of a JDBC/EntityManager repository class."""
        src = cls.src
        text = src.clean[cls.body_open : (cls.body_close or len(src.clean)) + 1]
        for off, value in _sql_literals(text, src.kotlin):
            if not _looks_like_sql(value):
                continue
            evidence = f"{src.rel}:{src.line(cls.body_open + off)}"
            hits.add_sql(value, evidence)
            for table, op in _jpql_refs(value, self.entity_tables):
                hits.add(table, op, evidence)

    # ----------------------------------------------------------------- MyBatis

    def _mybatis_xml(self) -> dict[str, list[tuple[str, str]]]:
        """Mapper XML statements: interface simple name -> [(sql text, evidence)]."""
        out: dict[str, list[tuple[str, str]]] = {}
        for path in iter_files(self.root, (".xml",)):
            try:
                text = read(path)
            except OSError:
                continue
            ns = re.search(r"<mapper\b[^>]*\bnamespace\s*=\s*\"([\w.$]+)\"", text)
            if not ns:
                continue
            name = ns.group(1).split(".")[-1]
            for m in re.finditer(r"<(select|insert|update|delete|sql)\b[^>]*>(.*?)</\1>", text, re.S | re.I):
                body = re.sub(r"<!\[CDATA\[|\]\]>", " ", m.group(2))
                body = re.sub(r"<[^>]+>", " ", body)
                out.setdefault(name, []).append((body, f"{rel(self.root, path)}:{line_of(text, m.start())}"))
        return out
