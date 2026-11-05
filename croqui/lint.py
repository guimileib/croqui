"""``croqui lint``: check a Mermaid or Excalidraw file against what the viewer draws.

``viewer/diagram.js`` is forgiving on purpose — a line it cannot read is skipped, a
class it does not know is painted nothing — so a diagram that "loads" can still be
wrong, and the only way to find out is to open the tab. This reads a file the way
diagram.js does (the lexer below is a port of its flowchart and sequence parsers)
and says, in a line per problem, what the picture will be missing. It is static and
free, so a model can run it before ``croqui build`` instead of iterating by eye.

Keep this in step with ``diagram.js``: it is a mirror, not a Mermaid validator.

An ``.excalidraw`` file gets the defect pass of a render-and-look loop, done on the
geometry instead of on a picture: broken bindings, writing that does not fit its
box, shapes and text on top of each other, arrows that stop short of what they
point at. See ``lint_excalidraw``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .docs import SKIP_DIRS, section_dirs
from .scaffold import MARKER, MARKER_WINDOW

SUFFIXES = (".mmd", ".mermaid", ".excalidraw")
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


# ------------------------------------------------------------------ excalidraw

# Average advance of a character, in font sizes, per Excalidraw font number: the
# monospaced ones (3 Cascadia, 8 Comic Shanns) are exact, the rest a fair average.
FONT_ADVANCE = {3: 0.6, 8: 0.6}
DEFAULT_ADVANCE = 0.55
SHAPES = {"rectangle", "ellipse", "diamond", "image", "embeddable", "iframe"}
MAX_ELEMENTS = 300
MIN_FONT = 12
ARROW_REACH = 45       # how far, in px, a bound arrow may stop from its element


def _box(el: dict) -> tuple[float, float, float, float]:
    """x1, y1, x2, y2 — an arrow's from its points, everything else from its size."""
    x, y = _num(el.get("x")), _num(el.get("y"))
    pts = el.get("points") if el.get("type") in ("arrow", "line", "freedraw") else None
    if isinstance(pts, list) and pts:
        xs = [x + _num(p[0]) for p in pts if isinstance(p, list) and len(p) >= 2]
        ys = [y + _num(p[1]) for p in pts if isinstance(p, list) and len(p) >= 2]
        if xs:
            return min(xs), min(ys), max(xs), max(ys)
    w, h = _num(el.get("width")), _num(el.get("height"))
    return min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h)


def _num(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _meets(a: tuple, b: tuple, slack: float = 0.0) -> bool:
    return a[0] < b[2] - slack and b[0] < a[2] - slack and a[1] < b[3] - slack and b[1] < a[3] - slack


def _inside(a: tuple, b: tuple, slack: float = 2.0) -> bool:
    return a[0] >= b[0] - slack and a[1] >= b[1] - slack and a[2] <= b[2] + slack and a[3] <= b[3] + slack


def _name(el: dict) -> str:
    """How an element is called in a message: its writing if it has any, else its id."""
    text = str(el.get("originalText") or el.get("text") or el.get("name") or "").strip().split("\n")[0]
    return f"`{text[:30]}`" if text else f"{el.get('type')} `{el.get('id')}`"


def _lines_needed(text: str, size: float, advance: float, room: float) -> tuple[int, float]:
    """(lines, widest word) for ``text`` wrapped to ``room`` px, as Excalidraw wraps it."""
    lines, widest = 0, 0.0
    for raw in str(text).split("\n"):
        lines += 1
        run = 0.0
        for word in raw.split(" "):
            w = len(word) * size * advance
            widest = max(widest, w)
            step = (size * advance if run else 0.0) + w
            if run and run + step > room:
                lines += 1
                run = w
            else:
                run += step
    return lines, widest


def lint_excalidraw(text: str, file: str = "<texto>") -> list[Issue]:
    """Every problem in one .excalidraw file, in line order."""
    rep = _Report(file)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        rep.add(exc.lineno, ERROR, f"JSON inválido: {exc.msg}")
        return rep.issues
    if not isinstance(doc, dict) or doc.get("type") not in ("excalidraw", "excalidraw/clipboard"):
        rep.add(1, ERROR, "não é um arquivo do Excalidraw (falta `\"type\": \"excalidraw\"`)")
        return rep.issues
    elements = doc.get("elements")
    if not isinstance(elements, list) or not elements:
        rep.add(1, ERROR, "sem elementos — nada para desenhar")
        return rep.issues

    line_of: dict[str, int] = {}
    for number, raw in enumerate(text.split("\n"), start=1):
        for found in re.finditer(r'"id"\s*:\s*"([^"]+)"', raw):
            line_of.setdefault(found.group(1), number)

    live = [el for el in elements if isinstance(el, dict) and not el.get("isDeleted")]
    counts: dict[str, int] = {}
    for el in live:
        if not isinstance(el.get("id"), str) or not isinstance(el.get("type"), str):
            rep.add(1, ERROR, "elemento sem `id` ou sem `type`")
            continue
        counts[el["id"]] = counts.get(el["id"], 0) + 1
    for eid, n in counts.items():
        if n > 1:
            rep.add(line_of.get(eid, 1), ERROR,
                    f"id `{eid}` aparece {n} vezes — colado, cada cópia vira um desenho a mais")
    by_id = {el["id"]: el for el in live if isinstance(el.get("id"), str)}

    def at(el: dict) -> int:
        return line_of.get(str(el.get("id")), 1)

    def lists(holder: dict, eid: str) -> bool:
        return any(isinstance(b, dict) and b.get("id") == eid for b in holder.get("boundElements") or [])

    small = faint = 0
    for el in live:
        kind = el.get("type")
        for ref in el.get("boundElements") or []:
            if isinstance(ref, dict) and ref.get("id") not in by_id:
                rep.add(at(el), WARNING, f"{_name(el)} lista `{ref.get('id')}`, que não existe")
        if el.get("opacity", 100) != 100:
            faint += 1
        if kind == "text":
            if _num(el.get("fontSize")) and _num(el.get("fontSize")) < MIN_FONT:
                small += 1
            host_id = el.get("containerId")
            if host_id:
                host = by_id.get(host_id)
                if host is None:
                    rep.add(at(el), ERROR, f"o texto {_name(el)} está preso a `{host_id}`, que não existe")
                    continue
                if not lists(host, el["id"]):
                    rep.add(at(el), WARNING, f"{_name(host)} não lista o texto {_name(el)} em boundElements")
                if host.get("type") in ("rectangle", "ellipse", "diamond"):
                    _fits(rep, at(el), el, host)
        if kind == "arrow":
            for end in ("startBinding", "endBinding"):
                bind = el.get(end)
                if not isinstance(bind, dict):
                    continue
                target = by_id.get(bind.get("elementId"))
                if target is None:
                    rep.add(at(el), ERROR, f"a seta {_name(el)} está presa a `{bind.get('elementId')}`, que não existe")
                    continue
                if not lists(target, el["id"]):
                    rep.add(at(el), WARNING, f"{_name(target)} não lista a seta `{el['id']}` em boundElements")
                pts = el.get("points") or []
                if pts and isinstance(pts[0], list):
                    p = pts[0] if end == "startBinding" else pts[-1]
                    px, py = _num(el.get("x")) + _num(p[0]), _num(el.get("y")) + _num(p[1])
                    x1, y1, x2, y2 = _box(target)
                    gap = max(x1 - px, 0, px - x2) ** 2 + max(y1 - py, 0, py - y2) ** 2
                    if gap > ARROW_REACH ** 2:
                        rep.add(at(el), WARNING, f"a seta `{el['id']}` para a {int(gap ** 0.5)}px de "
                                                 f"{_name(target)}, a que está presa")
    if small:
        rep.add(1, INFO, f"{small} texto(s) com menos de {MIN_FONT}px — difíceis de ler no tamanho exportado")
    if faint:
        rep.add(1, INFO, f"{faint} elemento(s) com opacidade abaixo de 100 — use cor e espessura para hierarquia")
    if len(live) > MAX_ELEMENTS:
        rep.add(1, INFO, f"{len(live)} elementos — grande para um diagrama só; divida em seções ou arquivos")
        return sorted(rep.issues, key=lambda i: i.line)
    _overlaps(rep, live, by_id, at)
    return sorted(rep.issues, key=lambda i: i.line)


def _fits(rep: _Report, line: int, text: dict, host: dict) -> None:
    """Writing bound to a shape fits inside it: Excalidraw wraps it to the shape's
    width but does not grow the shape on open, so what does not fit spills out."""
    size = _num(text.get("fontSize")) or 20
    advance = FONT_ADVANCE.get(int(_num(text.get("fontFamily"))), DEFAULT_ADVANCE)
    w, h = abs(_num(host.get("width"))), abs(_num(host.get("height")))
    inner = {"ellipse": 0.707, "diamond": 0.5}.get(str(host.get("type")), 1.0)
    room_w, room_h = w * inner - 10, h * inner - 10
    lines, widest = _lines_needed(str(text.get("originalText") or text.get("text") or ""), size, advance, room_w)
    need = lines * size * (_num(text.get("lineHeight")) or 1.25)
    if widest > room_w + 1:
        rep.add(line, WARNING, f"uma palavra de {_name(text)} é mais larga que {_name(host)} "
                               f"(~{int(widest)}px, cabem {int(room_w)}) — alargue a forma")
    elif need > room_h + 1:
        rep.add(line, WARNING, f"o texto {_name(text)} não cabe em {_name(host)}: precisa de ~{int(need)}px "
                               f"de altura, tem {int(room_h)} — aumente a forma ou encurte o texto")


def _overlaps(rep: _Report, live: list[dict], by_id: dict, at) -> None:
    """Shapes on top of shapes, and loose text on top of anything, unless one sits
    wholly inside the other — a label in its box, a dot on its line, a block of
    code on its card are all deliberate."""
    shapes = [el for el in live if el.get("type") in SHAPES]
    loose = [el for el in live if el.get("type") == "text" and not el.get("containerId")]
    boxes = {el["id"]: _box(el) for el in shapes + loose if isinstance(el.get("id"), str)}

    def clash(a: dict, b: dict) -> bool:
        ba, bb = boxes[a["id"]], boxes[b["id"]]
        return _meets(ba, bb, 1.0) and not _inside(ba, bb) and not _inside(bb, ba)

    for i, a in enumerate(shapes):
        for b in shapes[i + 1:]:
            if clash(a, b):
                rep.add(at(b), WARNING, f"{_name(a)} e {_name(b)} se sobrepõem")
    for i, t in enumerate(loose):
        for other in loose[i + 1:] + shapes:
            if clash(t, other):
                rep.add(at(t), WARNING, f"o texto {_name(t)} está por cima de {_name(other)}")


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
        check = lint_excalidraw if path.suffix.lower() == ".excalidraw" else lint_text
        issues.extend(check(text, _shown(path, root)))
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
