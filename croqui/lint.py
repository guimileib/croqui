"""``croqui lint``: check a Mermaid file against what the viewer actually draws.

``viewer/diagram.js`` is forgiving on purpose — a line it cannot read is skipped, a
class it does not know is painted nothing — so a diagram that "loads" can still be
wrong, and the only way to find out is to open the tab. This reads a file the way
diagram.js does (the lexer below is a port of its flowchart and sequence parsers)
and says, in a line per problem, what the picture will be missing. It is static and
free, so a model can run it before ``croqui build`` instead of iterating by eye.

Keep this in step with ``diagram.js``: it is a mirror, not a Mermaid validator.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .docs import SKIP_DIRS, section_dirs
from .scaffold import MARKER, MARKER_WINDOW

SUFFIXES = (".mmd", ".mermaid")
MAX_NODES = 25
MAX_WORDS = 6

ERROR, WARNING, INFO = "error", "warning", "info"

# Mirrors of the constants in diagram.js.
PALETTE = {
    "route", "service", "repo", "table", "external",
    "get", "post", "put", "patch", "delete",
    "ok", "warn", "danger", "muted", "accent",
}
DIRS = {"TB", "TD", "BT", "LR", "RL"}
STYLE_KEYS = {"fill", "stroke", "color"}
COLOR_VALUE = re.compile(r"^(#[0-9a-f]{3,8}|(rgb|rgba|hsl|hsla)\([0-9.,%\s]+\)|[a-z]{3,20})$", re.I)
OTHER_TYPES = {
    "classdiagram": "classDiagram", "erdiagram": "erDiagram", "statediagram": "stateDiagram",
    "statediagram-v2": "stateDiagram-v2", "gantt": "gantt", "pie": "pie", "journey": "journey",
    "mindmap": "mindmap", "timeline": "timeline", "quadrantchart": "quadrantChart",
    "gitgraph": "gitGraph", "c4context": "C4Context", "requirementdiagram": "requirementDiagram",
    "sankey": "sankey", "block": "block", "xychart": "xychart",
}

FLOW_HEAD = re.compile(r"^\s*(graph|flowchart)\b\s*([A-Za-z]{2})?\s*[:;]?\s*$", re.I)
SHAPE_PAIRS = (("[[", "]]"), ("[(", ")]"), ("((", "))"), ("([", "])"), ("{{", "}}"), ("[/", "/]"),
               ("[\\", "\\]"), ("[", "]"), ("(", ")"), ("{", "}"), (">", "]"))
NODE_ID = re.compile(r"[A-Za-z0-9_.\-#]+")
EDGE = re.compile(r"(<?)(-\.-+>|-\.-+|-{2,}>|-{2,}|={2,}>|={2,}|~{3})(?:\s*\|([^|]*)\|)?")
CLASS_REF = re.compile(r":::([A-Za-z0-9_-]+)")
SEQ_ARROW = r"(?:<<-{1,2}>>|-{1,2}>>|-{1,2}x|-{1,2}\)|-{1,2}>)"
SEQ_MSG = re.compile(r"^([^\s:]+?)\s*(" + SEQ_ARROW + r")\s*([^\s:]+?)\s*:\s*([\s\S]*)$")
SEQ_BLOCK = re.compile(r"^(loop|alt|else|opt|par|and|critical|rect|break|end|activate|deactivate|autonumber|box)\b", re.I)
SEQ_BAND = {"loop", "alt", "opt", "par", "critical", "rect", "break", "box"}


@dataclass(frozen=True)
class Issue:
    file: str
    line: int
    level: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.level}: {self.message}"


class LintError(Exception):
    """A file the user named that cannot be read."""


# ---------------------------------------------------------------------- lexing


def _normalize(line: str) -> str:
    """`A -- texto --> B` is `A -->|texto| B` (diagram.js `normalizeLabels`)."""
    for pattern, repl in (
        (r"--\s*([^\->|][^>|]*?)\s*-{2,}>", r"-->|\1|"),
        (r"--\s*([^\->|][^>|]*?)\s*-{2,}(?![>-])", r"---|\1|"),
        (r"==\s*([^=>|][^>|]*?)\s*={2,}>", r"==>|\1|"),
        (r"==\s*([^=>|][^>|]*?)\s*={2,}(?![>=])", r"===|\1|"),
        (r"-\.\s*([^.>|][^>|]*?)\s*\.-+>", r"-.->|\1|"),
        (r"-\.\s*([^.>|][^>|]*?)\s*\.-+(?!>)", r"-.-|\1|"),
    ):
        line = re.sub(pattern, repl, line)
    return line


def _read_node(text: str, i: int) -> dict | None:
    """`A`, `A[Label]`, `A{{Label}}`… at ``i`` (diagram.js `readNode`)."""
    m = NODE_ID.match(text, i)
    if not m:
        return None
    ident, j = m.group(0), m.end()
    ref = {"id": ident, "label": None, "shaped": False, "next": j, "cls": None}
    for open_, close in SHAPE_PAIRS:
        if not text.startswith(open_, j):
            continue
        start = j + len(open_)
        label, end = None, -1
        if text[start:start + 1] == '"':
            q = text.find('"', start + 1)
            if q > 0 and text.startswith(close, q + 1):
                label, end = text[start + 1:q], q + 1 + len(close)
        if end < 0:
            k = text.find(close, start)
            if k < 0:
                continue
            label, end = text[start:k], k + len(close)
        ref.update(label=label, shaped=True, next=end)
        break
    c = CLASS_REF.match(text, ref["next"])
    if c:
        ref["cls"] = c.group(1)
        ref["next"] = c.end()
    return ref


def _skip(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _clean(raw: str) -> str:
    return re.sub(r"%%.*$", "", raw).strip().rstrip(";").strip()


def _edit1(a: str, b: str) -> bool:
    """True when the ids differ by exactly one inserted, dropped or changed character."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i + 1:] if len(a) == len(b) else a[i:] == b[i + 1:]


# ------------------------------------------------------------------- the checks


class _Report:
    def __init__(self, file: str) -> None:
        self.file = file
        self.issues: list[Issue] = []

    def add(self, line: int, level: str, message: str) -> None:
        self.issues.append(Issue(self.file, line, level, message))


def _flow(rep: _Report, lines: list[tuple[int, str]], head_line: int, direction: str | None) -> None:
    if direction and direction.upper() not in DIRS:
        rep.add(head_line, WARNING, f"direção `{direction}` desconhecida — será desenhado como TB")

    classdefs: set[str] = set()
    for _, raw in lines:
        m = re.match(r"^classDef\s+([A-Za-z0-9_,-]+)\s+(.+)$", _clean(raw), re.I)
        if m:
            classdefs.update(m.group(1).split(","))
    known = PALETTE | classdefs | {"default"}

    stack: list[int] = []
    subgraphs: set[str] = set()
    declared: dict[str, int] = {}        # id -> first line where it got a shape or label
    bare: dict[str, int] = {}            # id -> first line where it is only mentioned
    wordy: set[str] = set()

    glued: set[int] = set()

    def touch(ref: dict, number: int) -> None:
        ident = ref["id"]
        if ident.endswith(("--", "==", "-.")):
            # diagram.js reads ids greedily, so `A-->B` is a node called `A--`.
            glued.add(number)
            rep.add(number, WARNING, f"`{ident}` engole a seta — ponha espaços em volta de `-->`")
        if ref["shaped"]:
            declared.setdefault(ident, number)
        else:
            bare.setdefault(ident, number)
        if ref["cls"] and ref["cls"] not in known:
            unknown_class(ref["cls"], number)
        if ref["label"] and ident not in wordy:
            words = len(re.sub(r"<br\s*/?>", " ", ref["label"], flags=re.I).split())
            if words > MAX_WORDS:
                wordy.add(ident)
                rep.add(number, INFO, f"rótulo de `{ident}` com {words} palavras — prefira até 4")

    def unknown_class(name: str, number: int) -> None:
        rep.add(number, WARNING, f"classe `{name}` desconhecida: não é da paleta do croqui nem tem "
                                 f"`classDef` — o nó fica sem cor")

    for number, raw in lines:
        line = _clean(raw)
        if not line:
            continue

        sub = re.match(r"^subgraph\s+(.*)$", line, re.I)
        if sub:
            stack.append(number)
            spec = sub.group(1).strip()
            named = re.match(r"^([A-Za-z0-9_.\-]+)\s*[\[(]", spec) or re.match(r"^([A-Za-z0-9_.\-]+)$", spec)
            if named:
                subgraphs.add(named.group(1))
            continue
        if re.match(r"^end$", line, re.I):
            if stack:
                stack.pop()
            else:
                rep.add(number, ERROR, "`end` sem `subgraph` aberto")
            continue
        cd = re.match(r"^classDef\s+([A-Za-z0-9_,-]+)\s+(.+)$", line, re.I)
        if cd:
            for pair in re.split(r"[,;]", cd.group(2)):
                kv = pair.split(":")
                if len(kv) != 2:
                    continue
                key, value = kv[0].strip().lower(), kv[1].strip()
                if key not in STYLE_KEYS or not COLOR_VALUE.match(value):
                    rep.add(number, INFO, f"classDef: `{key}:{value}` é ignorado — só fill, stroke e color com cor")
            continue
        cl = re.match(r"^class\s+([A-Za-z0-9_.,#-]+)\s+([A-Za-z0-9_-]+)\s*$", line, re.I)
        if cl:
            for ident in cl.group(1).split(","):
                bare.setdefault(ident, number)
            if cl.group(2) not in known:
                unknown_class(cl.group(2), number)
            continue
        st = re.match(r"^style\s+([A-Za-z0-9_.#-]+)\s+(.+)$", line, re.I)
        if st:
            bare.setdefault(st.group(1), number)
            continue
        ig = re.match(r"^(linkStyle|click|direction)\b", line, re.I)
        if ig:
            word = ig.group(1)
            rep.add(number, INFO, f"`{word}` é lido e ignorado pelo croqui")
            continue

        line = _normalize(line)
        at = _skip(line, 0)
        ref = _read_node(line, at)
        if not ref:
            rep.add(number, WARNING, f"linha não reconhecida, ignorada: {raw.strip()[:50]}")
            continue
        touch(ref, number)
        at = _skip(line, ref["next"])
        while at < len(line) and line[at] == "&":
            nxt = _read_node(line, _skip(line, at + 1))
            if not nxt:
                break
            touch(nxt, number)
            at = _skip(line, nxt["next"])
        while True:
            edge = EDGE.match(line, at)
            if not edge:
                break
            at = _skip(line, edge.end())
            tail = _read_node(line, at)
            if not tail:
                rep.add(number, WARNING, f"aresta sem destino: {raw.strip()[:50]}")
                at = len(line)
                break
            touch(tail, number)
            at = _skip(line, tail["next"])
            while at < len(line) and line[at] == "&":
                nxt = _read_node(line, _skip(line, at + 1))
                if not nxt:
                    break
                touch(nxt, number)
                at = _skip(line, nxt["next"])
        if at < len(line) and number not in glued:
            rep.add(number, WARNING, f"texto ignorado depois do nó: {line[at:at + 40]}")

    for number in stack:
        rep.add(number, ERROR, "`subgraph` sem `end`")

    nodes = (set(declared) | set(bare)) - subgraphs
    if len(nodes) > MAX_NODES:
        rep.add(head_line, WARNING, f"{len(nodes)} nós (mais de {MAX_NODES}) — difícil de ler; divida em arquivos")
    if not nodes:
        rep.add(head_line, WARNING, "nenhum nó — a aba mostra o diagrama vazio")

    # A typo is only worth saying when it is plain: a three-letter-plus id that is never
    # given a shape, one character away from one that is. Anything vaguer stays quiet.
    for ident, number in sorted(bare.items(), key=lambda kv: kv[1]):
        if ident in declared or ident in subgraphs or len(ident) < 3:
            continue
        near = [d for d in declared if len(d) >= 3 and _edit1(ident, d)]
        if len(near) == 1:
            rep.add(number, INFO, f"`{ident}` nunca é declarado — erro de digitação de `{near[0]}`?")


def _sequence(rep: _Report, lines: list[tuple[int, str]]) -> None:
    blocks: set[str] = set()
    for number, raw in lines:
        line = re.sub(r"%%.*$", "", raw).strip()
        if not line:
            continue
        if re.match(r"^(participant|actor)\s+([A-Za-z0-9_.\-]+)(?:\s+as\s+(.*))?$", line, re.I):
            continue
        if re.match(r"^note\s+(over|left of|right of)\s+([^:]+):\s*(.*)$", line, re.I):
            continue
        if SEQ_MSG.match(line):
            continue
        block = SEQ_BLOCK.match(line)
        if block:
            word = block.group(1).lower()
            if word in SEQ_BAND:
                rep.add(number, INFO, f"`{word}` não é desenhado como faixa — as mensagens aparecem em sequência")
            elif word in ("autonumber", "activate", "deactivate") and word not in blocks:
                blocks.add(word)
                rep.add(number, INFO, f"`{word}` é ignorado pelo croqui")
            continue
        rep.add(number, WARNING, f"linha não reconhecida, ignorada: {line[:50]}")


def lint_text(text: str, file: str = "<texto>") -> list[Issue]:
    """Every problem in one diagram, in line order."""
    rep = _Report(file)
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    numbered = [(i + 1, line) for i, line in enumerate(raw)]

    # A ```mermaid fence and a front-matter block are noise to diagram.js, but the
    # front matter is where the title lives.
    if numbered and re.match(r"^\s*```[a-zA-Z]*\s*$", numbered[0][1]):
        numbered = numbered[1:]
    while numbered and not numbered[-1][1].strip():
        numbered = numbered[:-1]
    if numbered and re.match(r"^\s*```\s*$", numbered[-1][1]):
        numbered = numbered[:-1]
    has_title = False
    if numbered and numbered[0][1].rstrip() == "---":
        for k in range(1, len(numbered)):
            if numbered[k][1].rstrip() == "---":
                has_title = any(re.match(r"^\s*title\s*:\s*\S", n[1]) for n in numbered[1:k])
                numbered = numbered[k + 1:]
                break
    numbered = [(n, line) for n, line in numbered if not re.match(r"^\s*%%\{.*\}%%\s*$", line)]

    body = [(n, line) for n, line in numbered if re.sub(r"%%.*$", "", line).strip()]
    if not body:
        rep.add(1, WARNING, "arquivo vazio")
        return rep.issues
    head_line, head = body[0][0], re.sub(r"%%.*$", "", body[0][1]).strip()
    rest = [(n, line) for n, line in numbered if n > head_line]

    flow = FLOW_HEAD.match(head)
    if flow:
        _flow(rep, rest, head_line, flow.group(2))
    elif re.match(r"^sequenceDiagram\b", head, re.I):
        _sequence(rep, rest)
    else:
        word = re.split(r"[\s{]", head)[0]
        name = OTHER_TYPES.get(word.lower(), head[:40])
        rep.add(head_line, WARNING, f"diagrama `{name}` não é desenhado pelo croqui — será mostrado como código; "
                                    f"use flowchart ou sequenceDiagram")
        return rep.issues
    if not has_title:
        rep.add(head_line, INFO, "sem `title:` no frontmatter — a aba usa o nome do arquivo")
    return sorted(rep.issues, key=lambda i: i.line)


# ------------------------------------------------------------------------ files


def is_generated(text: str) -> bool:
    """`relacoes.mmd` and friends: croqui rewrites them, so there is nothing to fix by hand."""
    return MARKER in text[:MARKER_WINDOW]


def collect(root: Path) -> list[Path]:
    """Every diagram in the project's UML folder(s)."""
    found: list[Path] = []
    for directory in section_dirs(root, "uml"):
        for path in sorted(directory.rglob("*")):
            parts = path.relative_to(directory).parts
            if path.is_file() and path.suffix.lower() in SUFFIXES \
                    and not any(p in SKIP_DIRS or p.startswith(".") for p in parts) and path not in found:
                found.append(path)
    return found


def _shown(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def lint_paths(root: Path, files: list[str] | None = None) -> tuple[list[Issue], int]:
    """Lint the given files (or every UML diagram); returns (issues, files checked)."""
    if files:
        paths = []
        for name in files:
            path = Path(name)
            path = path if path.is_absolute() else (Path.cwd() / path)
            if not path.is_file():
                raise LintError(f"Não encontrei o arquivo {name}.")
            paths.append(path)
    else:
        paths = collect(root)

    issues: list[Issue] = []
    checked = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LintError(f"Não deu para ler {path.name}: {exc.strerror or exc}") from exc
        if is_generated(text):
            continue
        checked += 1
        issues.extend(lint_text(text, _shown(path, root)))
    return issues, checked


def has_errors(issues: list[Issue]) -> bool:
    return any(i.level == ERROR for i in issues)


def render(issues: list[Issue], checked: int) -> str:
    """The report an agent reads: one line per problem, then a count."""
    counts = {level: sum(1 for i in issues if i.level == level) for level in (ERROR, WARNING, INFO)}
    lines = [str(i) for i in issues]
    lines.append(f"{checked} arquivo(s): {counts[ERROR]} erro(s), {counts[WARNING]} aviso(s), {counts[INFO]} info")
    return "\n".join(lines) + "\n"


def as_json(issues: list[Issue], checked: int) -> str:
    return json.dumps({
        "files": checked,
        "errors": sum(1 for i in issues if i.level == ERROR),
        "warnings": sum(1 for i in issues if i.level == WARNING),
        "issues": [asdict(i) for i in issues],
    }, indent=2, ensure_ascii=False) + "\n"
