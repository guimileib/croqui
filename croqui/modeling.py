"""The modelling layer: what the code says about data, pipelines and agents.

The map answers "which endpoint reaches which table". This module answers the three
questions one level below it, the ones a data or AI team asks first:

* **DER** — every table the repo defines, with its columns, keys and the foreign
  keys between them. Read from the same ground truth as the map (DDL and ORM
  models), so a table here is a table the code really declares.
* **pipelines** — the DAGs that move data: Databricks jobs (asset bundles and
  Jobs API JSON), Delta Live Tables / Lakeflow declarative pipelines (Python and
  SQL), Airflow DAGs and dbt models. Drawn the way Databricks draws them: tasks
  with their dependencies, datasets in medallion layers.
* **agents** — how an LLM application is wired: LangGraph state graphs, CrewAI
  crews, OpenAI Agents SDK / Google ADK / pydantic-ai agents with their tools and
  handoffs, Claude Agent SDK subagents, raw Messages API tool use and Claude Code
  subagents in ``.claude/agents``.

Everything is static and best-effort, like the rest of croqui: no imports, no code
runs, and a construct that cannot be read is skipped rather than guessed. Each
finding keeps a ``file:line`` so the tab can say where it came from.

The result is a plain dict stored under ``"modeling"`` in graph.json. The viewer's
*modelagem* tab draws it from :func:`modeling_docs`, which turns it into mermaid
the existing renderer already knows (plus ``erDiagram``, added for this).
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from .tables import TableRegistry

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".mypy_cache", ".croqui",
    "dist", "build", "target", ".gradle", ".next", ".tox", ".pytest_cache", "site-packages",
}
MAX_FILE_BYTES = 1024 * 1024
# A DER past this many tables stops being a picture; it is split per schema.
MAX_ENTITIES_PER_DIAGRAM = 40
MAX_COLUMNS_SHOWN = 14

PY_SUFFIXES = (".py",)
JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")


# ============================================================== file helpers


def _walk(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.lower().endswith(suffixes):
                out.append(Path(dirpath, name))
    return out


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _notebook_source(text: str) -> str:
    """The code cells of an .ipynb, joined, so a notebook reads like a script."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ""
    cells = data.get("cells") if isinstance(data, dict) else None
    out = []
    for cell in cells or []:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        src = cell.get("source")
        body = "".join(src) if isinstance(src, list) else str(src or "")
        # `%sql` cells and other magics are not Python.
        if body.lstrip().startswith("%"):
            continue
        out.append(body)
    return "\n\n".join(out)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ================================================================ mini YAML


class _Yaml:
    """Block YAML, the subset bundle and crew configs are written in.

    Mappings, sequences, plain/quoted scalars, ``[a, b]`` and ``{k: v}`` flow
    collections and ``|``/``>`` block scalars. No anchors, tags or multi-line flow.
    croqui has no runtime dependencies, and PyYAML is not worth breaking that for
    the handful of keys read here.
    """

    KEY_RE = re.compile(r"""^("[^"]*"|'[^']*'|[^\s"'#\-][^:]*?|-[^\s:][^:]*?)\s*:(?:\s+(.*))?$""")

    def __init__(self, text: str) -> None:
        self.lines: list[tuple[int, str]] = []
        for raw in text.replace("\t", "  ").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
                continue
            line = self._strip_comment(raw.rstrip())
            if not line.strip():
                continue
            self.lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
        self.i = 0

    @staticmethod
    def _strip_comment(line: str) -> str:
        quote = ""
        for idx, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
                return line[:idx].rstrip()
        return line

    def parse(self):
        if not self.lines:
            return None
        try:
            return self._block(self.lines[0][0])
        except (IndexError, RecursionError):
            return None

    def _block(self, indent: int):
        _, content = self.lines[self.i]
        if content == "-" or content.startswith("- "):
            return self._seq(indent)
        return self._map(indent)

    def _seq(self, indent: int) -> list:
        out: list = []
        while self.i < len(self.lines):
            ind, content = self.lines[self.i]
            if ind != indent or not (content == "-" or content.startswith("- ")):
                break
            item = content[1:].strip()
            self.i += 1
            if not item:
                out.append(self._child(indent))
            elif self.KEY_RE.match(item) and not item.startswith(("[", "{")):
                col = ind + (len(content) - len(item))
                self.lines.insert(self.i, (col, item))
                out.append(self._map(col))
            else:
                out.append(self._scalar(item))
        return out

    def _map(self, indent: int) -> dict:
        out: dict = {}
        while self.i < len(self.lines):
            ind, content = self.lines[self.i]
            if ind < indent or (ind == indent and (content == "-" or content.startswith("- "))):
                break
            if ind > indent:  # malformed: skip what we cannot place
                self.i += 1
                continue
            m = self.KEY_RE.match(content)
            self.i += 1
            if not m:
                continue
            key = m.group(1).strip().strip("'\"")
            rest = (m.group(2) or "").strip()
            if not rest:
                out[key] = self._child(indent)
            elif rest[0] in "|>":
                out[key] = self._block_scalar(indent)
            else:
                out[key] = self._scalar(rest)
        return out

    def _child(self, indent: int):
        if self.i < len(self.lines):
            ind, content = self.lines[self.i]
            if ind > indent:
                return self._block(ind)
            if ind == indent and (content == "-" or content.startswith("- ")):
                return self._seq(ind)
        return None

    def _block_scalar(self, indent: int) -> str:
        parts = []
        while self.i < len(self.lines) and self.lines[self.i][0] > indent:
            parts.append(self.lines[self.i][1])
            self.i += 1
        return "\n".join(parts)

    def _scalar(self, text: str):
        text = text.strip()
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            return [self._scalar(p) for p in _split_flow(inner)] if inner else []
        if text.startswith("{") and text.endswith("}"):
            out = {}
            for part in _split_flow(text[1:-1]):
                key, sep, value = part.partition(":")
                if sep:
                    out[key.strip().strip("'\"")] = self._scalar(value)
            return out
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
            return text[1:-1]
        low = text.lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
        if low in ("null", "~"):
            return None
        return text


def _split_flow(text: str) -> list[str]:
    parts, depth, quote, cur = [], 0, "", []
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def load_yaml(text: str):
    """Parse the YAML subset croqui reads. Returns None on anything it cannot read."""
    return _Yaml(text).parse()


# ======================================================================== DER

FK_SUFFIX_RE = re.compile(r"^(?P<stem>[A-Za-z][A-Za-z0-9_]*?)(?:_id|Id|ID)$")


def _plural_forms(stem: str) -> list[str]:
    s = stem.lower()
    forms = [s, s + "s", s + "es"]
    if s.endswith("y"):
        forms.append(s[:-1] + "ies")
    return forms + [f"tb_{f}" for f in list(forms)]


def build_entities(registry: TableRegistry) -> dict:
    """Tables, columns and relations — declared foreign keys first, `x_id` names after."""
    records = [r for r in registry.all() if r.get("kind") in ("table", None)]
    views = [r for r in registry.all() if r.get("kind") in ("view", "matview")]
    # The same name can live in several schemas: a bare name prefers the table's own.
    by_lower: dict[str, list[dict]] = {}
    for r in records:
        by_lower.setdefault(r["name"].lower(), []).append(r)

    def named(name: str, schema: str | None) -> dict | None:
        same = by_lower.get(name, [])
        return next((r for r in same if r.get("schema") == schema), None) or (same[0] if len(same) == 1 else None)

    def qualified(rec: dict) -> str:
        return f"{rec['schema']}.{rec['name']}" if rec.get("schema") else rec["name"]

    entities = []
    for rec in records + views:
        columns = []
        for col in rec.get("columns") or []:
            out = {"name": col["name"], "type": col.get("type") or ""}
            for flag in ("pk", "required", "indexed"):
                if col.get(flag):
                    out[flag] = True
            if col.get("references"):
                out["references"] = col["references"]
            columns.append(out)
        entities.append({
            "id": qualified(rec),
            "name": rec["name"],
            "schema": rec.get("schema"),
            "kind": rec.get("kind") or "table",
            "columns": columns,
            "sources": list(rec.get("sources") or [])[:3],
        })

    relations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for ent, rec in zip(entities, records + views):
        for col in ent["columns"]:
            target, confidence = None, "static"
            if col.get("references"):
                ref = col["references"]
                if "." not in ref:
                    target = named(ref.lower(), rec.get("schema"))
                target = target or registry.resolve(ref)
            else:
                m = FK_SUFFIX_RE.match(col["name"])
                if m and not col.get("pk"):
                    for form in _plural_forms(m.group("stem")):
                        candidate = named(form, rec.get("schema"))
                        if candidate is not None and candidate is not rec:
                            target, confidence = candidate, "inferred"
                            break
            if target is None:
                continue
            key = (ent["id"], qualified(target), col["name"])
            if key in seen:
                continue
            seen.add(key)
            col["fk"] = True
            relations.append({
                "from": ent["id"],
                "to": qualified(target),
                "column": col["name"],
                # A unique / primary-key FK is one-to-one; anything else many-to-one.
                "cardinality": "1:1" if col.get("pk") else "N:1",
                "required": bool(col.get("required")),
                "confidence": confidence,
            })
    return {"entities": entities, "relations": relations}


# ================================================================== pipelines

LAYER_WORDS = {
    "bronze": "bronze", "raw": "bronze", "landing": "bronze", "ingest": "bronze", "src": "bronze",
    "silver": "silver", "clean": "silver", "cleaned": "silver", "staging": "silver", "stg": "silver",
    "conformed": "silver", "int": "silver", "intermediate": "silver",
    "gold": "gold", "mart": "gold", "marts": "gold", "agg": "gold", "fct": "gold", "dim": "gold",
    "report": "gold", "reporting": "gold", "serving": "gold",
}
LAYER_ORDER = ("source", "bronze", "silver", "gold")


def medallion_layer(*names: str | None) -> str | None:
    """bronze / silver / gold from a dataset's name, schema or folder; None if silent."""
    for name in names:
        if not name:
            continue
        for token in re.split(r"[^a-z0-9]+", name.lower()):
            if token in LAYER_WORDS:
                return LAYER_WORDS[token]
    return None


class _Pipe:
    """One pipeline under construction: steps and the edges between them."""

    def __init__(self, pid: str, name: str, kind: str, source: str) -> None:
        self.data = {"id": pid, "name": name, "kind": kind, "source": source, "steps": [], "edges": []}
        self._steps: dict[str, dict] = {}
        self._edges: set[tuple[str, str, str]] = set()

    def step(self, sid: str, label: str | None = None, *, type: str = "task", **meta) -> dict:
        step = self._steps.get(sid)
        if step is None:
            step = {"id": sid, "label": label or sid, "type": type}
            self._steps[sid] = step
            self.data["steps"].append(step)
        elif type not in ("task", "dataset") and step["type"] == "task":
            # A bare reference was created first; the declaration knows better.
            step["type"] = type
        if label and step["label"] == sid:
            step["label"] = label
        for key, value in meta.items():
            if value not in (None, "", [], {}):
                step[key] = value
        return step

    def edge(self, src: str, dst: str, label: str = "") -> None:
        key = (src, dst, label)
        if src == dst or key in self._edges:
            return
        self._edges.add(key)
        edge = {"from": src, "to": dst}
        if label:
            edge["label"] = label
        self.data["edges"].append(edge)

    def empty(self) -> bool:
        return not self.data["steps"]


# -------------------------------------------------------- Databricks bundles

TASK_KINDS = (
    ("notebook_task", "notebook", ("notebook_path",)),
    ("spark_python_task", "python", ("python_file",)),
    ("python_wheel_task", "wheel", ("entry_point", "package_name")),
    ("sql_task", "sql", ("file", "query", "dashboard", "alert")),
    ("pipeline_task", "pipeline", ("pipeline_id",)),
    ("run_job_task", "job", ("job_id",)),
    ("condition_task", "condition", ("op",)),
    ("for_each_task", "foreach", ("inputs",)),
    ("dbt_task", "dbt", ("commands",)),
    ("spark_jar_task", "jar", ("main_class_name",)),
    ("spark_submit_task", "submit", ("parameters",)),
    ("clean_rooms_notebook_task", "notebook", ("notebook_name",)),
)
RESOURCE_REF_RE = re.compile(r"\$\{resources\.(jobs|pipelines)\.([\w-]+)\.\w+\}")


def _detail(value) -> str:
    if isinstance(value, dict):
        for key in ("path", "file_path", "query_id", "dashboard_id", "alert_id"):
            if value.get(key):
                return str(value[key])
        return ""
    if isinstance(value, list):
        return " ".join(str(v) for v in value[:3])
    return str(value or "")


def _job_tasks(pipe: _Pipe, tasks: list, evidence: str) -> None:
    for task in tasks or []:
        if not isinstance(task, dict) or not task.get("task_key"):
            continue
        key = str(task["task_key"])
        kind, detail = "task", ""
        for field, label, keys in TASK_KINDS:
            body = task.get(field)
            if not isinstance(body, dict):
                continue
            kind = label
            for k in keys:
                if body.get(k):
                    detail = _detail(body[k])
                    break
            if field == "condition_task":
                detail = f"{body.get('left', '')} {body.get('op', '')} {body.get('right', '')}".strip()
            if field == "for_each_task" and isinstance(body.get("task"), dict):
                inner = body["task"]
                detail = f"para cada {_detail(body.get('inputs'))[:40]} → {inner.get('task_key', '')}"
            break
        ref = RESOURCE_REF_RE.search(detail)
        if ref:
            detail = f"{'job' if ref.group(1) == 'jobs' else 'DLT'} {ref.group(2)}"
        pipe.step(key, key, type=kind, detail=detail, evidence=evidence)
        for dep in task.get("depends_on") or []:
            if isinstance(dep, dict) and dep.get("task_key"):
                outcome = dep.get("outcome")
                pipe.edge(str(dep["task_key"]), key, str(outcome) if outcome not in (None, "") else "")
            elif isinstance(dep, str):
                pipe.edge(dep, key)


def _bundle_pipelines(root: Path) -> tuple[list[dict], dict[str, dict]]:
    """Jobs from asset bundles (YAML) and Jobs API exports (JSON); DLT pipeline defs."""
    jobs: list[dict] = []
    dlt_defs: dict[str, dict] = {}
    for path in _walk(root, (".yml", ".yaml")):
        text = _read(path)
        if not text or ("tasks:" not in text and "pipelines:" not in text):
            continue
        if "task_key" not in text and "libraries:" not in text:
            continue
        data = load_yaml(text)
        if not isinstance(data, dict):
            continue
        rel = _rel(root, path)
        resources = data.get("resources") if isinstance(data.get("resources"), dict) else {}
        # `targets.<t>.resources` repeats the jobs with overrides; the base is enough.
        for key, job in ((resources.get("jobs") or {}) if isinstance(resources.get("jobs"), dict) else {}).items():
            if not isinstance(job, dict):
                continue
            pipe = _Pipe(f"job:{key}", str(job.get("name") or key), "databricks-job", rel)
            _job_tasks(pipe, job.get("tasks") or [], rel)
            sched = job.get("schedule") or job.get("trigger")
            if isinstance(sched, dict):
                expr = sched.get("quartz_cron_expression") or ("contínuo" if sched.get("continuous") else "")
                if expr:
                    pipe.data["schedule"] = str(expr)
            if not pipe.empty():
                jobs.append(pipe.data)
        for key, spec in ((resources.get("pipelines") or {}) if isinstance(resources.get("pipelines"), dict) else {}).items():
            if not isinstance(spec, dict):
                continue
            libs = []
            for lib in spec.get("libraries") or []:
                if not isinstance(lib, dict):
                    continue
                for kind in ("notebook", "file"):
                    if isinstance(lib.get(kind), dict) and lib[kind].get("path"):
                        libs.append((path.parent / str(lib[kind]["path"])).resolve())
                if isinstance(lib.get("glob"), dict) and lib["glob"].get("include"):
                    inc = str(lib["glob"]["include"]).split("*")[0].rstrip("/")
                    libs.append((path.parent / inc).resolve())
            dlt_defs[key] = {
                "name": str(spec.get("name") or key),
                "libraries": libs,
                "catalog": spec.get("catalog"),
                "schema": spec.get("schema") or spec.get("target"),
                "source": rel,
            }

    for path in _walk(root, (".json",)):
        text = _read(path)
        if not text or '"task_key"' not in text:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = [data] + ([data["settings"]] if isinstance(data, dict) and isinstance(data.get("settings"), dict) else [])
        for spec in candidates:
            if not isinstance(spec, dict) or not isinstance(spec.get("tasks"), list):
                continue
            rel = _rel(root, path)
            name = str(spec.get("name") or path.stem)
            pipe = _Pipe(f"job:{rel}", name, "databricks-job", rel)
            _job_tasks(pipe, spec["tasks"], rel)
            if not pipe.empty():
                jobs.append(pipe.data)
            break
    return jobs, dlt_defs


# ------------------------------------------------- Delta Live Tables (Python)

DLT_MODULES = {"dlt", "dp", "sdp", "pipelines"}
DLT_DATASET_DECOS = {"table", "view", "materialized_view", "temporary_view", "streaming_table"}
DLT_READS = {"read", "read_stream", "table"}
LIVE_REF_RE = re.compile(r"\b(?:LIVE|live)\.([A-Za-z_][\w]*)")


def _call_name(node: ast.AST) -> tuple[str | None, str | None]:
    """`dlt.table` -> ("dlt", "table"); `table` -> (None, "table")."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        base = node.value
        owner = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else None)
        return owner, node.attr
    if isinstance(node, ast.Name):
        return None, node.id
    return None, None


def _kw(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _const(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _strip_live(name: str) -> str:
    name = name.strip("`\"' ")
    return name[5:] if name.lower().startswith("live.") else name


def _dlt_python(text: str, rel: str, pipe: _Pipe) -> int:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return 0
    found = 0

    def reads(body: list[ast.stmt]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Call):
                owner, attr = _call_name(node)
                arg = _const(node.args[0]) if node.args else None
                if attr in DLT_READS and arg and (owner in DLT_MODULES or owner in ("read", "readStream", "spark")):
                    stream = attr == "read_stream" or owner == "readStream"
                    out.append((_strip_live(arg), "stream" if stream else ""))
                elif attr == "load" and arg and "cloudFiles" in ast.unparse(node.func):
                    out.append((f"@{arg}", "auto loader"))
                elif attr == "sql" and arg:
                    out.extend((m, "") for m in LIVE_REF_RE.findall(arg))
        return out

    for node in ast.walk(tree):
        # dlt.create_streaming_table("x") / dp.create_streaming_table(name="x")
        if isinstance(node, ast.Call):
            owner, attr = _call_name(node)
            if owner in DLT_MODULES and attr in ("create_streaming_table", "create_target_table"):
                name = _const(node.args[0]) if node.args else _const(_kw(node, "name"))
                if name:
                    pipe.step(name, name, type="dataset", streaming=True,
                              layer=medallion_layer(name), evidence=f"{rel}:{node.lineno}")
                    found += 1
            if owner in DLT_MODULES and attr in ("apply_changes", "create_auto_cdc_flow", "apply_changes_from_snapshot"):
                target = _const(_kw(node, "target"))
                source = _const(_kw(node, "source"))
                if target and source:
                    pipe.step(target, target, type="dataset", layer=medallion_layer(target),
                              evidence=f"{rel}:{node.lineno}")
                    pipe.step(_strip_live(source), _strip_live(source), type="dataset",
                              layer=medallion_layer(source))
                    pipe.edge(_strip_live(source), target, "CDC")
                    found += 1
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        dataset, kind, expects, flow_target = None, None, 0, None
        for deco in node.decorator_list:
            owner, attr = _call_name(deco)
            if attr in DLT_DATASET_DECOS and (owner in DLT_MODULES or owner is None):
                kind = attr
                dataset = (_const(_kw(deco, "name")) if isinstance(deco, ast.Call) else None) or node.name
            elif attr and attr.startswith("expect") and owner in DLT_MODULES:
                expects += 1
            elif attr == "append_flow" and owner in DLT_MODULES and isinstance(deco, ast.Call):
                flow_target = _const(_kw(deco, "target"))
        target = dataset or flow_target
        if not target:
            continue
        found += 1
        sources = reads(node.body)
        streaming = any(mode == "stream" or mode == "auto loader" for _, mode in sources)
        pipe.step(
            target, target, type="dataset", kind=kind if dataset else None,
            streaming=streaming or None, expectations=expects or None,
            layer=medallion_layer(target), evidence=f"{rel}:{node.lineno}",
        )
        for src, mode in sources:
            if src.startswith("@"):
                pipe.step(src, src[1:], type="source", detail="Auto Loader", layer="source")
            else:
                pipe.step(src, src, type="dataset", layer=medallion_layer(src))
            pipe.edge(src, target, "stream" if mode in ("stream",) else "")
    return found


# --------------------------------------------------- Delta Live Tables (SQL)

DLT_SQL_CREATE = re.compile(
    r"CREATE\s+(?:OR\s+REFRESH\s+)?(?:OR\s+REPLACE\s+)?"
    r"(?P<kind>(?:STREAMING|LIVE)\s+(?:TABLE|LIVE\s+TABLE)|STREAMING\s+LIVE\s+TABLE|MATERIALIZED\s+VIEW|"
    r"TEMPORARY\s+(?:LIVE\s+)?VIEW|LIVE\s+VIEW|LIVE\s+TABLE|TABLE|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[`\w.]+)",
    re.I,
)
SQL_FROM = re.compile(r"\b(?:FROM|JOIN)\s+(?:STREAM\s*\(\s*)?(?:STREAM\s+)?(?P<name>[`\w.]+)", re.I)
SQL_FILES = re.compile(r"\b(?:read_files|cloud_files)\s*\(\s*['\"]([^'\"]+)['\"]", re.I)
DLT_SQL_HINT = re.compile(r"CREATE\s+OR\s+REFRESH|STREAMING\s+TABLE|\bLIVE\.\w|STREAM\s*\(|LIVE\s+TABLE", re.I)


def _dlt_sql(text: str, rel: str, pipe: _Pipe) -> int:
    if not DLT_SQL_HINT.search(text):
        return 0
    found = 0
    clean = re.sub(r"--[^\n]*", "", text)
    for stmt in clean.split(";"):
        m = DLT_SQL_CREATE.search(stmt)
        if not m:
            continue
        name = _strip_live(m.group("name")).split(".")[-1]
        line = text[: text.find(m.group(0))].count("\n") + 1 if m.group(0) in text else 0
        kind = " ".join(m.group("kind").lower().split())
        body = stmt[m.end():]
        pipe.step(name, name, type="dataset", kind=kind, streaming=("stream" in kind) or None,
                  expectations=len(re.findall(r"\bCONSTRAINT\b[^,]*\bEXPECT\b", body, re.I)) or None,
                  layer=medallion_layer(name), evidence=f"{rel}:{line}" if line else rel)
        found += 1
        for path in SQL_FILES.findall(body):
            sid = f"@{path}"
            pipe.step(sid, path, type="source", detail="arquivos", layer="source")
            pipe.edge(sid, name)
        for ref in SQL_FROM.finditer(body):
            src = _strip_live(ref.group("name")).split(".")[-1]
            if not src or src.lower() in ("select", "values") or src.lower().startswith(("read_files", "cloud_files")):
                continue
            if src == name:
                continue
            pipe.step(src, src, type="dataset", layer=medallion_layer(ref.group("name")))
            pipe.edge(src, name, "stream" if "stream" in body[max(0, ref.start() - 2):ref.end()].lower() else "")
    return found


def _dlt_pipelines(root: Path, dlt_defs: dict[str, dict]) -> list[dict]:
    files: list[tuple[Path, str, str]] = []
    for path in _walk(root, (".py", ".sql", ".ipynb")):
        text = _read(path)
        if not text:
            continue
        if path.suffix == ".ipynb":
            text = _notebook_source(text)
            lang = "py"
        else:
            lang = path.suffix[1:]
        if lang == "py" and not re.search(r"^\s*(?:import\s+dlt|from\s+dlt\b|from\s+pyspark\s+import\s+pipelines|import\s+pyspark\.pipelines)", text, re.M):
            continue
        files.append((path.resolve(), text, lang))

    def owner_of(path: Path) -> str | None:
        for key, spec in dlt_defs.items():
            for lib in spec["libraries"]:
                if path == lib or path.with_suffix("") == lib.with_suffix("") or lib in path.parents:
                    return key
        return None

    pipes: dict[str, _Pipe] = {}
    for path, text, lang in files:
        key = owner_of(path) or "_"
        spec = dlt_defs.get(key)
        if key not in pipes:
            name = spec["name"] if spec else "Delta Live Tables"
            pipes[key] = _Pipe(f"dlt:{key}", name, "dlt", spec["source"] if spec else _rel(root, path))
            if spec and (spec.get("catalog") or spec.get("schema")):
                pipes[key].data["target"] = ".".join(str(p) for p in (spec.get("catalog"), spec.get("schema")) if p)
        rel = _rel(root, path)
        if lang == "py":
            _dlt_python(text, rel, pipes[key])
        else:
            _dlt_sql(text, rel, pipes[key])
    return [p.data for p in pipes.values() if any(s["type"] == "dataset" for s in p.data["steps"])]


# ---------------------------------------------------------------------- dbt

DBT_REF = re.compile(r"\{\{\s*ref\(\s*(?:['\"][\w-]+['\"]\s*,\s*)?['\"]([\w.-]+)['\"]\s*(?:,[^)]*)?\)\s*\}\}")
DBT_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"]([\w-]+)['\"]\s*,\s*['\"]([\w.-]+)['\"]\s*\)\s*\}\}")
DBT_MATERIALIZED = re.compile(r"materialized\s*=\s*['\"](\w+)['\"]")


def _dbt_pipelines(root: Path) -> list[dict]:
    out = []
    for project in _walk(root, ("dbt_project.yml",)):
        base = project.parent
        conf = load_yaml(_read(project) or "") or {}
        name = str(conf.get("name") or base.name) if isinstance(conf, dict) else base.name
        dirs = conf.get("model-paths") or conf.get("source-paths") or ["models"] if isinstance(conf, dict) else ["models"]
        if not isinstance(dirs, list):
            dirs = ["models"]
        pipe = _Pipe(f"dbt:{_rel(root, base)}", f"dbt · {name}", "dbt", _rel(root, project))
        for d in dirs:
            for path in _walk(base / str(d), (".sql",)):
                text = _read(path) or ""
                model = path.stem
                folders = path.relative_to(base / str(d)).parts[:-1]
                mat = DBT_MATERIALIZED.search(text)
                pipe.step(model, model, type="dataset", kind=mat.group(1) if mat else None,
                          layer=medallion_layer(model, *folders), evidence=_rel(root, path))
                for ref in DBT_REF.findall(text):
                    pipe.step(ref, ref, type="dataset", layer=medallion_layer(ref))
                    pipe.edge(ref, model)
                for src, table in DBT_SOURCE.findall(text):
                    sid = f"{src}.{table}"
                    pipe.step(sid, sid, type="source", layer="source", detail="source")
                    pipe.edge(sid, model)
        if not pipe.empty():
            out.append(pipe.data)
    return out


# ------------------------------------------------------------------ Airflow

AIRFLOW_TASK_DECOS = {"task", "python", "bash", "branch", "sensor", "virtualenv", "docker", "kubernetes", "short_circuit"}


def _airflow(text: str, rel: str) -> dict | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    dag_id = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _, attr = _call_name(node)
            if attr == "DAG":
                dag_id = _const(node.args[0]) if node.args else _const(_kw(node, "dag_id"))
                if dag_id:
                    break
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if _call_name(deco)[1] == "dag":
                    dag_id = (_const(_kw(deco, "dag_id")) if isinstance(deco, ast.Call) else None) or node.name
    if not dag_id:
        return None
    pipe = _Pipe(f"airflow:{rel}:{dag_id}", dag_id, "airflow", rel)
    var_task: dict[str, str] = {}
    task_funcs: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                owner, attr = _call_name(deco)
                if attr in AIRFLOW_TASK_DECOS and (owner in (None, "task") or attr == "task"):
                    tid = (_const(_kw(deco, "task_id")) if isinstance(deco, ast.Call) else None) or node.name
                    task_funcs[node.name] = tid
                    kind = "branch" if attr in ("branch", "short_circuit") else (
                        "sensor" if attr == "sensor" else "python" if attr in ("task", "python") else attr)
                    pipe.step(tid, tid, type=kind, evidence=f"{rel}:{node.lineno}")

    def task_of(node) -> list[str]:
        if isinstance(node, ast.Name):
            return [var_task[node.id]] if node.id in var_task else []
        if isinstance(node, (ast.List, ast.Tuple)):
            return [t for el in node.elts for t in task_of(el)]
        if isinstance(node, ast.Call):
            _, attr = _call_name(node)
            if attr in task_funcs:
                return [task_funcs[attr]]
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("override", "expand", "partial"):
                return task_of(node.func.value)
        if isinstance(node, ast.Attribute) and node.attr == "output":
            return task_of(node.value)
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            call = node.value
            tid = _const(_kw(call, "task_id"))
            _, op = _call_name(call)
            if tid:
                kind = "branch" if op and "Branch" in op else "sensor" if op and op.endswith("Sensor") else "task"
                if op and "Databricks" in op:
                    kind = "notebook"
                pipe.step(tid, tid, type=kind, detail=op, evidence=f"{rel}:{node.lineno}")
                var_task[target.id] = tid
            elif op in task_funcs:
                var_task[target.id] = task_funcs[op]

    # TaskFlow data passing: load(transform(extract())) is extract → transform → load.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _, attr = _call_name(node)
            if attr in task_funcs:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    for src in task_of(arg):
                        pipe.edge(src, task_funcs[attr])

    def link(a: list[str], b: list[str]) -> None:
        for x in a:
            for y in b:
                pipe.edge(x, y)

    def walk_shift(node) -> None:
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            walk_shift(node.left)
            left_last = _rightmost(node.left)
            if isinstance(node.op, ast.RShift):
                link(task_of(left_last), task_of(node.right))
            else:
                link(task_of(node.right), task_of(left_last))

    def _rightmost(node):
        while isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            node = node.right
        return node

    for node in ast.walk(tree):
        if isinstance(node, ast.Expr):
            walk_shift(node.value)
            if isinstance(node.value, ast.Call):
                owner, attr = _call_name(node.value)
                call = node.value
                if attr in ("set_downstream", "set_upstream") and isinstance(call.func, ast.Attribute):
                    me = task_of(call.func.value)
                    other = task_of(call.args[0]) if call.args else []
                    link(me, other) if attr == "set_downstream" else link(other, me)
                elif attr == "chain":
                    groups = [task_of(a) for a in call.args]
                    for a, b in zip(groups, groups[1:]):
                        link(a, b)
                elif attr == "cross_downstream" and len(call.args) == 2:
                    link(task_of(call.args[0]), task_of(call.args[1]))
    return None if pipe.empty() else pipe.data


def _airflow_pipelines(root: Path) -> list[dict]:
    out = []
    for path in _walk(root, PY_SUFFIXES):
        text = _read(path)
        if not text or "airflow" not in text:
            continue
        found = _airflow(text, _rel(root, path))
        if found:
            out.append(found)
    return out


def build_pipelines(root: Path) -> list[dict]:
    jobs, dlt_defs = _bundle_pipelines(root)
    pipelines = jobs + _dlt_pipelines(root, dlt_defs) + _dbt_pipelines(root) + _airflow_pipelines(root)
    # Name the DLT pipeline a job task triggers, so the two diagrams point at each other.
    dlt_names = {key: spec["name"] for key, spec in dlt_defs.items()}
    for pipe in pipelines:
        for step in pipe["steps"]:
            detail = step.get("detail") or ""
            if detail.startswith("DLT "):
                step["detail"] = f"DLT {dlt_names.get(detail[4:], detail[4:])}"
    return pipelines


# ===================================================================== agents

TOOL_DECOS = {"tool", "function_tool", "tool_plain", "beta_tool"}
GENERIC_AGENT_CLASSES = {
    "Agent": "agent", "LlmAgent": "agent", "ReActAgent": "agent", "AssistantAgent": "agent",
    "SequentialAgent": "sequential", "ParallelAgent": "parallel", "LoopAgent": "loop",
}
LANGGRAPH_CLASSES = {"StateGraph", "MessageGraph", "Graph"}
# A file is read for agents only when it imports one of these: `Agent(...)` alone is
# far too common a name (a CRM has agents too) to trust without the import.
LLM_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+(?:anthropic|openai|langchain\w*|langgraph|crewai|google\.adk|pydantic_ai|agents|"
    r"claude_agent_sdk|claude_code_sdk|autogen\w*|llama_index|strands|smolagents|swarm)(?!\w)",
    re.M,
)
JS_LLM_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\(\s*)['"](?:@openai/agents|@mastra/|@langchain/|langchain|@anthropic-ai/|@google/adk|@voltagent/)"""
)


class _Sys:
    """One agent system: agents, tools, router steps and how control flows between them."""

    def __init__(self, sid: str, name: str, kind: str, source: str) -> None:
        self.data = {"id": sid, "name": name, "kind": kind, "source": source, "nodes": [], "edges": []}
        self._nodes: dict[str, dict] = {}
        self._edges: set[tuple] = set()

    def node(self, nid: str, label: str | None = None, *, type: str = "node", **meta) -> dict:
        node = self._nodes.get(nid)
        if node is None:
            node = {"id": nid, "label": label or nid, "type": type}
            self._nodes[nid] = node
            self.data["nodes"].append(node)
        elif type != "node" and node["type"] == "node":
            node["type"] = type
        if label and node["label"] == nid:
            node["label"] = label
        for key, value in meta.items():
            if value not in (None, "", [], {}):
                node[key] = value
        return node

    def edge(self, src: str, dst: str, label: str = "", *, style: str = "flow") -> None:
        key = (src, dst, label, style)
        if key in self._edges:
            return
        self._edges.add(key)
        edge = {"from": src, "to": dst, "style": style}
        if label:
            edge["label"] = label
        self.data["edges"].append(edge)

    def empty(self) -> bool:
        return not any(n["type"] not in ("start", "end") for n in self.data["nodes"])


def _names_in(node) -> list[str]:
    """`[a, b, c.as_tool()]` -> ["a", "b", "c"]; strings kept as they are."""
    if node is None:
        return []
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [n for el in node.elts for n in _names_in(el)]
    if isinstance(node, ast.Dict):
        return [n for k in node.keys if k is not None for n in _names_in(k)]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("as_tool", "as_agent_tool"):
            return _names_in(func.value)
        _, attr = _call_name(node)
        if attr in ("handoff", "AgentTool") and node.args:
            return _names_in(node.args[0])
        if attr == "AgentTool":
            return _names_in(_kw(node, "agent"))
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self":
            return [func.attr]  # CrewAI: self.researcher()
        if attr:
            return [attr]
    return []


def _first_line(doc: str | None) -> str:
    return (doc or "").strip().splitlines()[0][:90] if (doc or "").strip() else ""


class _PyAgents:
    """Everything agent-shaped in one Python file."""

    def __init__(self, text: str, rel: str) -> None:
        self.rel = rel
        self.tree = ast.parse(text)
        self.funcs: dict[str, ast.FunctionDef] = {}
        self.assigns: dict[str, ast.Call] = {}
        self.tools: dict[str, dict] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.setdefault(node.name, node)
                for deco in node.decorator_list:
                    _, attr = _call_name(deco)
                    if attr in TOOL_DECOS:
                        name = (_const(deco.args[0]) if isinstance(deco, ast.Call) and deco.args else None) or node.name
                        self.tools[node.name] = {"name": name, "doc": _first_line(ast.get_docstring(node)),
                                                 "evidence": f"{rel}:{node.lineno}"}
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.value, ast.Call):
                t = node.targets[0]
                if isinstance(t, ast.Name):
                    self.assigns[t.id] = node.value

    def ev(self, node: ast.AST) -> str:
        return f"{self.rel}:{getattr(node, 'lineno', 0)}"

    # ---------------------------------------------------------------- LangGraph

    def langgraph(self) -> list[dict]:
        graphs: dict[str, _Sys] = {}
        for var, call in self.assigns.items():
            _, attr = _call_name(call)
            if attr in LANGGRAPH_CLASSES:
                state = ast.unparse(call.args[0]) if call.args else ""
                sys = _Sys(f"langgraph:{self.rel}:{var}", var, "langgraph", self.rel)
                if state:
                    sys.data["state"] = state
                graphs[var] = sys
        if not graphs:
            return []

        def ref(node) -> str | None:
            if isinstance(node, ast.Name) and node.id in ("START", "END"):
                return f"__{node.id.lower()}__"
            if isinstance(node, ast.Attribute) and node.attr in ("START", "END"):
                return f"__{node.attr.lower()}__"
            s = _const(node)
            if s in ("__start__", "__end__"):
                return s
            if s is not None:
                return s
            if isinstance(node, ast.Name):
                return node.id
            return None

        def touch(sys: _Sys, nid: str) -> None:
            if nid == "__start__":
                sys.node(nid, "início", type="start")
            elif nid == "__end__":
                sys.node(nid, "fim", type="end")
            else:
                sys.node(nid, nid, type="node")

        def router_targets(fn_name: str) -> list[str]:
            if fn_name == "tools_condition":
                return ["tools", "__end__"]
            fn = self.funcs.get(fn_name)
            out: list[str] = []
            if fn is None:
                return out
            for node in ast.walk(fn):
                if isinstance(node, ast.Return) and node.value is not None:
                    vals = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
                    for v in vals:
                        if isinstance(v, ast.Call) and _call_name(v)[1] == "Send" and v.args:
                            v = v.args[0]
                        r = ref(v)
                        if r and (isinstance(v, ast.Constant) or r.startswith("__")):
                            out.append(r)
            return out

        def describe(sys: _Sys, name: str, impl) -> None:
            if impl is None:
                touch(sys, name)
                return
            impl_call = impl if isinstance(impl, ast.Call) else (
                self.assigns.get(impl.id) if isinstance(impl, ast.Name) else None)
            if impl_call is not None:
                _, attr = _call_name(impl_call)
                if attr == "ToolNode":
                    tools = _names_in(impl_call.args[0] if impl_call.args else _kw(impl_call, "tools"))
                    sys.node(name, name, type="tools", tools=[self.tool_label(t) for t in tools],
                             evidence=self.ev(impl_call))
                    for t in tools:
                        tid = f"tool:{t}"
                        sys.node(tid, self.tool_label(t), type="tool", evidence=self.tools.get(t, {}).get("evidence"))
                        sys.edge(name, tid, style="uses")
                    return
                if attr in ("create_react_agent", "create_agent"):
                    tools = _names_in(_kw(impl_call, "tools") or (impl_call.args[1] if len(impl_call.args) > 1 else None))
                    sys.node(name, name, type="agent", detail="ReAct", evidence=self.ev(impl_call))
                    for t in tools:
                        tid = f"tool:{t}"
                        sys.node(tid, self.tool_label(t), type="tool")
                        sys.edge(name, tid, style="uses")
                    return
                if isinstance(impl_call.func, ast.Attribute) and impl_call.func.attr == "compile":
                    sys.node(name, name, type="subgraph", detail=ast.unparse(impl_call.func.value))
                    return
            fn_name = impl.id if isinstance(impl, ast.Name) else (impl.attr if isinstance(impl, ast.Attribute) else None)
            fn = self.funcs.get(fn_name or "")
            detail = fn_name if fn_name and fn_name != name else None
            sys.node(name, name, type="node", detail=detail,
                     doc=_first_line(ast.get_docstring(fn)) if fn else None,
                     evidence=self.ev(fn) if fn else None)
            # Command(goto="x") inside the node is a hand-off the graph never declares.
            if fn is not None:
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Call) and _call_name(sub)[1] == "Command":
                        goto = _kw(sub, "goto")
                        for tgt in ([goto] if not isinstance(goto, (ast.List, ast.Tuple)) else goto.elts):
                            r = ref(tgt) if tgt is not None else None
                            if r:
                                touch(sys, r)
                                sys.edge(name, r, "goto", style="conditional")

        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in graphs:
                continue
            sys, method, args = graphs[owner.id], node.func.attr, node.args
            if method == "add_node":
                if args and _const(args[0]) is not None:
                    describe(sys, _const(args[0]), args[1] if len(args) > 1 else _kw(node, "action"))
                elif args:
                    name = ref(args[0])
                    if name:
                        describe(sys, name, args[0])
            elif method == "add_edge" and len(args) >= 2:
                sources = [ref(a) for a in args[0].elts] if isinstance(args[0], (ast.List, ast.Tuple)) else [ref(args[0])]
                dst = ref(args[1])
                for src in sources:
                    if src and dst:
                        touch(sys, src)
                        touch(sys, dst)
                        sys.edge(src, dst)
            elif method == "add_sequence" and args and isinstance(args[0], (ast.List, ast.Tuple)):
                names = []
                for el in args[0].elts:
                    if isinstance(el, ast.Tuple) and el.elts:
                        n = _const(el.elts[0])
                        if n:
                            describe(sys, n, el.elts[1] if len(el.elts) > 1 else None)
                            names.append(n)
                    else:
                        n = ref(el)
                        if n:
                            describe(sys, n, el)
                            names.append(n)
                for a, b in zip(names, names[1:]):
                    sys.edge(a, b)
            elif method == "add_conditional_edges" and args:
                src = ref(args[0])
                if not src:
                    continue
                touch(sys, src)
                router = args[1] if len(args) > 1 else _kw(node, "path")
                mapping = args[2] if len(args) > 2 else _kw(node, "path_map")
                rname = router.id if isinstance(router, ast.Name) else (
                    router.attr if isinstance(router, ast.Attribute) else None)
                if isinstance(mapping, ast.Dict):
                    for k, v in zip(mapping.keys, mapping.values):
                        dst = ref(v)
                        if dst:
                            touch(sys, dst)
                            label = (_const(k) or ast.unparse(k)) if k is not None else ""
                            sys.edge(src, dst, label or "", style="conditional")
                elif isinstance(mapping, (ast.List, ast.Tuple)):
                    for v in mapping.elts:
                        dst = ref(v)
                        if dst:
                            touch(sys, dst)
                            sys.edge(src, dst, rname or "", style="conditional")
                elif rname:
                    for dst in router_targets(rname):
                        touch(sys, dst)
                        sys.edge(src, dst, rname if rname != "tools_condition" else "", style="conditional")
                if rname:
                    sys.data.setdefault("routers", [])
                    if rname not in sys.data["routers"]:
                        sys.data["routers"].append(rname)
            elif method in ("set_entry_point", "set_finish_point") and args:
                n = ref(args[0])
                if n:
                    touch(sys, n)
                    if method == "set_entry_point":
                        touch(sys, "__start__")
                        sys.edge("__start__", n)
                    else:
                        touch(sys, "__end__")
                        sys.edge(n, "__end__")
            elif method == "set_conditional_entry_point" and args:
                router = args[0]
                rname = router.id if isinstance(router, ast.Name) else None
                touch(sys, "__start__")
                mapping = args[1] if len(args) > 1 else None
                targets = [ref(v) for v in mapping.values] if isinstance(mapping, ast.Dict) else (
                    router_targets(rname) if rname else [])
                for dst in targets:
                    if dst:
                        touch(sys, dst)
                        sys.edge("__start__", dst, rname or "", style="conditional")
        # Name the graph after what it compiles into, when that is clearer.
        for var, call in self.assigns.items():
            if isinstance(call.func, ast.Attribute) and call.func.attr == "compile":
                owner = call.func.value
                if isinstance(owner, ast.Name) and owner.id in graphs:
                    graphs[owner.id].data["name"] = f"{var} ({owner.id})"
        return [g.data for g in graphs.values() if not g.empty()]

    def tool_label(self, name: str) -> str:
        return self.tools.get(name, {}).get("name") or name

    # ------------------------------------------------------------------- CrewAI

    def crewai(self) -> list[dict]:
        agents: dict[str, dict] = {}
        tasks: dict[str, dict] = {}
        crews: list[tuple[str, ast.Call]] = []
        for var, call in self.assigns.items():
            _, attr = _call_name(call)
            if attr == "Agent" and _kw(call, "role") is not None:
                agents[var] = {"role": _const(_kw(call, "role")) or var,
                               "tools": _names_in(_kw(call, "tools")),
                               "delegates": isinstance(_kw(call, "allow_delegation"), ast.Constant)
                               and _kw(call, "allow_delegation").value is True,
                               "evidence": self.ev(call)}
            elif attr == "Task" and (_kw(call, "description") is not None or _kw(call, "agent") is not None):
                desc = _const(_kw(call, "description")) or ""
                agent = _names_in(_kw(call, "agent"))
                tasks[var] = {"desc": _first_line(desc)[:60], "agent": agent[0] if agent else None,
                              "context": _names_in(_kw(call, "context")),
                              "tools": _names_in(_kw(call, "tools")), "evidence": self.ev(call)}
            elif attr == "Crew":
                crews.append((var, call))
        # @CrewBase classes: @agent / @task methods, roles in config/*.yaml.
        for cls in (n for n in ast.walk(self.tree) if isinstance(n, ast.ClassDef)):
            if not any(_call_name(d)[1] == "CrewBase" for d in cls.decorator_list):
                continue
            for fn in (n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                decos = {_call_name(d)[1] for d in fn.decorator_list}
                if "agent" in decos:
                    tools: list[str] = []
                    for sub in ast.walk(fn):
                        if isinstance(sub, ast.Call) and _call_name(sub)[1] == "Agent":
                            tools = _names_in(_kw(sub, "tools"))
                    agents[fn.name] = {"role": fn.name, "tools": tools, "delegates": False,
                                       "evidence": self.ev(fn)}
                elif "task" in decos:
                    agent = None
                    ctx: list[str] = []
                    for sub in ast.walk(fn):
                        if isinstance(sub, ast.Call) and _call_name(sub)[1] == "Task":
                            a = _names_in(_kw(sub, "agent"))
                            agent = a[0] if a else None
                            ctx = _names_in(_kw(sub, "context"))
                    tasks[fn.name] = {"desc": "", "agent": agent, "context": ctx, "tools": [],
                                      "evidence": self.ev(fn)}
            if not crews:
                crews.append((cls.name, None))
        if not agents and not tasks:
            return []
        out = []
        for var, call in crews or [("crew", None)]:
            sys = _Sys(f"crewai:{self.rel}:{var}", var, "crewai", self.rel)
            hierarchical = call is not None and "hierarchical" in ast.unparse(_kw(call, "process") or ast.Constant(""))
            crew_agents = _names_in(_kw(call, "agents")) if call is not None else list(agents)
            crew_tasks = _names_in(_kw(call, "tasks")) if call is not None else list(tasks)
            crew_agents = [a for a in crew_agents if a in agents] or list(agents)
            crew_tasks = [t for t in crew_tasks if t in tasks] or list(tasks)
            sys.data["process"] = "hierarchical" if hierarchical else "sequential"
            sys.node("__start__", "início", type="start")
            sys.node("__end__", "fim", type="end")
            if hierarchical:
                sys.node("manager", "gerente", type="agent", detail="processo hierárquico")
            for a in crew_agents:
                info = agents[a]
                sys.node(f"agent:{a}", info["role"], type="agent", evidence=info["evidence"],
                         detail="delega" if info["delegates"] else None)
                if hierarchical:
                    sys.edge("manager", f"agent:{a}", "delega", style="handoff")
                for t in info["tools"]:
                    sys.node(f"tool:{t}", self.tool_label(t), type="tool")
                    sys.edge(f"agent:{a}", f"tool:{t}", style="uses")
            prev = "__start__"
            for t in crew_tasks:
                info = tasks[t]
                tid = f"task:{t}"
                sys.node(tid, t, type="task", detail=info["desc"], evidence=info["evidence"])
                sys.edge(prev, tid)
                prev = tid
                if info["agent"] and info["agent"] in agents:
                    sys.edge(tid, f"agent:{info['agent']}", "executa", style="uses")
                for c in info["context"]:
                    if c in tasks:
                        sys.edge(f"task:{c}", tid, "contexto", style="data")
                for tool in info["tools"]:
                    sys.node(f"tool:{tool}", self.tool_label(tool), type="tool")
                    sys.edge(tid, f"tool:{tool}", style="uses")
            sys.edge(prev, "__end__")
            if not sys.empty():
                out.append(sys.data)
        return out

    # ---------------------------------------- Agents SDK / ADK / pydantic-ai

    def generic_agents(self) -> list[dict]:
        """Agent(...) definitions that are not CrewAI. Returned as loose records; grouped later."""
        out = []
        for var, call in self.assigns.items():
            _, attr = _call_name(call)
            if attr not in GENERIC_AGENT_CLASSES or _kw(call, "role") is not None:
                continue
            name = _const(_kw(call, "name")) or var
            model = _kw(call, "model")
            if model is None and call.args:
                model = call.args[0]
            sub_agents = _names_in(_kw(call, "sub_agents"))
            handoffs = _names_in(_kw(call, "handoffs"))
            tools = _names_in(_kw(call, "tools"))
            output = _kw(call, "output_type") or _kw(call, "output_schema") or _kw(call, "result_type")
            out.append({
                "var": var, "name": name, "type": GENERIC_AGENT_CLASSES[attr], "class": attr,
                "model": (_const(model) or ast.unparse(model))[:40] if model is not None else None,
                "tools": tools, "handoffs": handoffs, "sub_agents": sub_agents,
                "output": ast.unparse(output)[:40] if output is not None else None,
                "evidence": self.ev(call), "file": self.rel,
            })
        # pydantic-ai style: @support_agent.tool
        for fn in self.funcs.values():
            for deco in fn.decorator_list:
                node = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(node, ast.Attribute) and node.attr in ("tool", "tool_plain") and isinstance(node.value, ast.Name):
                    for rec in out:
                        if rec["var"] == node.value.id and fn.name not in rec["tools"]:
                            rec["tools"].append(fn.name)
        return out

    # ------------------------------------------------------- Claude Agent SDK

    def claude_sdk(self) -> list[dict]:
        out = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or _call_name(node)[1] not in ("ClaudeAgentOptions", "ClaudeCodeOptions"):
                continue
            agents = _kw(node, "agents")
            if isinstance(agents, ast.Name):
                for sub in ast.walk(self.tree):
                    if isinstance(sub, ast.Assign) and any(isinstance(t, ast.Name) and t.id == agents.id for t in sub.targets):
                        agents = sub.value
            sys = _Sys(f"claude-sdk:{self.rel}:{node.lineno}", "Claude Agent SDK", "claude-agent-sdk", self.rel)
            sys.node("main", "agente principal", type="agent", detail="Claude", evidence=self.ev(node))
            for t in _names_in(_kw(node, "allowed_tools")):
                sys.node(f"tool:{t}", t, type="tool")
                sys.edge("main", f"tool:{t}", style="uses")
            servers = _kw(node, "mcp_servers")
            if isinstance(servers, ast.Dict):
                for k in servers.keys:
                    name = _const(k)
                    if name:
                        sys.node(f"mcp:{name}", name, type="mcp")
                        sys.edge("main", f"mcp:{name}", "MCP", style="uses")
            if isinstance(agents, ast.Dict):
                for k, v in zip(agents.keys, agents.values):
                    name = _const(k)
                    if not name:
                        continue
                    desc = _const(_kw(v, "description")) if isinstance(v, ast.Call) else None
                    sys.node(f"agent:{name}", name, type="agent", detail=_first_line(desc)[:60],
                             evidence=self.ev(v))
                    sys.edge("main", f"agent:{name}", "subagente", style="handoff")
                    if isinstance(v, ast.Call):
                        for t in _names_in(_kw(v, "tools")):
                            sys.node(f"tool:{t}", t, type="tool")
                            sys.edge(f"agent:{name}", f"tool:{t}", style="uses")
            if not sys.empty() and len(sys.data["nodes"]) > 1:
                out.append(sys.data)
        return out

    # --------------------------------------------------- Messages API tool use

    def messages_tool_use(self) -> list[dict]:
        out = []
        for fn in self.funcs.values():
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("create", "stream")):
                    continue
                chain = ast.unparse(node.func)
                if not any(part in chain for part in (".messages.", "chat.completions", ".responses.")):
                    continue
                tools = _kw(node, "tools")
                names: list[str] = []
                if isinstance(tools, ast.Name):
                    for sub in ast.walk(self.tree):
                        if isinstance(sub, ast.Assign) and any(isinstance(t, ast.Name) and t.id == tools.id for t in sub.targets):
                            tools = sub.value
                if isinstance(tools, (ast.List, ast.Tuple)):
                    for el in tools.elts:
                        if isinstance(el, ast.Dict):
                            for k, v in zip(el.keys, el.values):
                                if _const(k) == "name" and _const(v):
                                    names.append(_const(v))
                                if _const(k) == "function" and isinstance(v, ast.Dict):
                                    for k2, v2 in zip(v.keys, v.values):
                                        if _const(k2) == "name" and _const(v2):
                                            names.append(_const(v2))
                        else:
                            names.extend(_names_in(el))
                if not names:
                    continue
                model = _const(_kw(node, "model"))
                sys = _Sys(f"llm:{self.rel}:{fn.name}", fn.name, "tool-use", self.rel)
                sys.node("llm", fn.name, type="agent", detail=model or "LLM", evidence=self.ev(node))
                for t in names:
                    sys.node(f"tool:{t}", t, type="tool")
                    sys.edge("llm", f"tool:{t}", style="uses")
                # A loop around the call is the agentic loop.
                if any(isinstance(n, (ast.While, ast.For)) for n in ast.walk(fn)):
                    sys.data["loop"] = True
                out.append(sys.data)
                break
        return out


def _crew_yaml(root: Path) -> list[dict]:
    """CrewAI projects keep agents and tasks in config/agents.yaml and config/tasks.yaml."""
    out = []
    for agents_file in _walk(root, ("agents.yaml", "agents.yml")):
        base = agents_file.parent
        tasks_file = next((base / n for n in ("tasks.yaml", "tasks.yml") if (base / n).is_file()), None)
        agents = load_yaml(_read(agents_file) or "")
        tasks = load_yaml(_read(tasks_file) or "") if tasks_file else {}
        if not isinstance(agents, dict) or not any(isinstance(v, dict) and "role" in v for v in agents.values()):
            continue
        rel = _rel(root, agents_file)
        sys = _Sys(f"crewai:{rel}", base.parent.name or "crew", "crewai", rel)
        sys.node("__start__", "início", type="start")
        sys.node("__end__", "fim", type="end")
        for key, spec in agents.items():
            if isinstance(spec, dict):
                role = str(spec.get("role") or key).strip().splitlines()[0][:50]
                sys.node(f"agent:{key}", role, type="agent", evidence=rel)
        prev = "__start__"
        for key, spec in (tasks or {}).items() if isinstance(tasks, dict) else []:
            if not isinstance(spec, dict):
                continue
            tid = f"task:{key}"
            sys.node(tid, key, type="task", detail=_first_line(str(spec.get("description") or ""))[:60],
                     evidence=_rel(root, tasks_file))
            sys.edge(prev, tid)
            prev = tid
            agent = spec.get("agent")
            if agent and f"agent:{agent}" in {n["id"] for n in sys.data["nodes"]}:
                sys.edge(tid, f"agent:{agent}", "executa", style="uses")
            for c in spec.get("context") or []:
                sys.edge(f"task:{c}", tid, "contexto", style="data")
        sys.edge(prev, "__end__")
        if not sys.empty():
            out.append(sys.data)
    return out


def _claude_subagents(root: Path) -> list[dict]:
    folder = root / ".claude" / "agents"
    if not folder.is_dir():
        return []
    sys = _Sys("claude-code:agents", "Subagentes do Claude Code", "claude-code", ".claude/agents")
    sys.node("main", "Claude Code", type="agent", detail="sessão principal")
    for path in sorted(folder.glob("*.md")):
        text = _read(path) or ""
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
        meta = load_yaml(m.group(1)) if m else {}
        meta = meta if isinstance(meta, dict) else {}
        name = str(meta.get("name") or path.stem)
        rel = _rel(root, path)
        sys.node(f"agent:{name}", name, type="agent", detail=_first_line(str(meta.get("description") or ""))[:60],
                 evidence=rel)
        sys.edge("main", f"agent:{name}", "subagente", style="handoff")
        tools = meta.get("tools")
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        for t in tools or []:
            sys.node(f"tool:{t}", str(t), type="tool")
            sys.edge(f"agent:{name}", f"tool:{t}", style="uses")
    return [sys.data] if len(sys.data["nodes"]) > 1 else []


# ---------------------------------------------------------------- JS / TS

JS_ADD_NODE = re.compile(r"\.addNode\(\s*['\"`]([\w-]+)['\"`]\s*(?:,\s*([\w.]+))?")
JS_ADD_EDGE = re.compile(r"\.addEdge\(\s*(['\"`][\w-]+['\"`]|START|END)\s*,\s*(['\"`][\w-]+['\"`]|START|END)\s*\)")
JS_COND = re.compile(r"\.addConditionalEdges\(\s*['\"`]([\w-]+)['\"`]\s*,\s*([\w.]+)\s*(?:,\s*([\[{][^\])}]*[\]}]))?")
JS_AGENT = re.compile(r"(?:const|let|var|export\s+const)\s+(\w+)\s*=\s*(?:new\s+Agent|Agent\.create)\s*\(\s*\{")
JS_PROP_STR = r"{key}\s*:\s*['\"`]([^'\"`]+)['\"`]"
JS_PROP_LIST = r"{key}\s*:\s*[\[{]([^\]}]*)[\]}]"


def _js_block(text: str, start: int) -> str:
    depth = 0
    for i in range(start, min(len(text), start + 20000)):
        ch = text[i]
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start : start + 2000]


def _js_ref(token: str) -> str:
    token = token.strip().strip("'\"`")
    return {"START": "__start__", "END": "__end__"}.get(token, token)


def _js_agents(text: str, rel: str) -> tuple[list[dict], list[dict]]:
    systems, loose = [], []
    if "StateGraph" in text and ".addNode(" in text:
        sys = _Sys(f"langgraph:{rel}", Path(rel).stem, "langgraph", rel)

        def touch(n: str) -> None:
            if n == "__start__":
                sys.node(n, "início", type="start")
            elif n == "__end__":
                sys.node(n, "fim", type="end")
            else:
                sys.node(n, n)

        for m in JS_ADD_NODE.finditer(text):
            impl = m.group(2) or ""
            sys.node(m.group(1), m.group(1), type="tools" if "ToolNode" in impl or m.group(1) == "tools" else "node",
                     detail=impl if impl and impl != m.group(1) else None,
                     evidence=f"{rel}:{text[: m.start()].count(chr(10)) + 1}")
        for m in JS_ADD_EDGE.finditer(text):
            a, b = _js_ref(m.group(1)), _js_ref(m.group(2))
            touch(a)
            touch(b)
            sys.edge(a, b)
        for m in JS_COND.finditer(text):
            src = m.group(1)
            touch(src)
            targets = re.findall(r"['\"`]([\w-]+)['\"`]|\b(END|START)\b", m.group(3) or "")
            if m.group(3) and m.group(3).startswith("{"):
                pairs = re.findall(r"['\"`]?([\w-]+)['\"`]?\s*:\s*(['\"`][\w-]+['\"`]|END|START)", m.group(3))
                for k, v in pairs:
                    touch(_js_ref(v))
                    sys.edge(src, _js_ref(v), k, style="conditional")
            else:
                for a, b in targets:
                    dst = _js_ref(a or b)
                    touch(dst)
                    sys.edge(src, dst, m.group(2), style="conditional")
        if not sys.empty():
            systems.append(sys.data)
    for m in JS_AGENT.finditer(text):
        body = _js_block(text, m.end() - 1)
        name = re.search(JS_PROP_STR.format(key="name"), body)
        tools = re.search(JS_PROP_LIST.format(key="tools"), body)
        handoffs = re.search(JS_PROP_LIST.format(key="handoffs"), body)
        model = re.search(JS_PROP_STR.format(key="model"), body)

        def idents(found) -> list[str]:
            if not found:
                return []
            return [re.split(r"[\s:(.]", t.strip())[0] for t in found.group(1).split(",") if t.strip()]

        loose.append({
            "var": m.group(1), "name": name.group(1) if name else m.group(1), "type": "agent", "class": "Agent",
            "model": model.group(1) if model else None, "tools": idents(tools), "handoffs": idents(handoffs),
            "sub_agents": [], "output": None,
            "evidence": f"{rel}:{text[: m.start()].count(chr(10)) + 1}", "file": rel,
        })
    return systems, loose


def _group_generic(records: list[dict], tool_names: dict[str, str]) -> list[dict]:
    """Agents that hand off to each other are one system; a lone agent is its own."""
    by_var = {}
    for rec in records:
        by_var.setdefault(rec["var"], rec)
    links: dict[str, set[str]] = {v: set() for v in by_var}
    for rec in by_var.values():
        for other in rec["handoffs"] + rec["sub_agents"] + rec["tools"]:
            if other in by_var and other != rec["var"]:
                links[rec["var"]].add(other)
                links[other].add(rec["var"])
    seen: set[str] = set()
    systems = []
    for var in by_var:
        if var in seen:
            continue
        comp, stack = [], [var]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            comp.append(cur)
            stack.extend(links[cur] - seen)
        targets = {o for v in comp for o in by_var[v]["handoffs"] + by_var[v]["sub_agents"] + by_var[v]["tools"]}
        roots = [v for v in comp if v not in targets] or comp[:1]
        head = by_var[roots[0]]
        sys = _Sys(f"agents:{head['file']}:{head['var']}", head["name"], "agents", head["file"])
        sys.node("__start__", "entrada", type="start")
        for r in roots:
            sys.edge("__start__", f"agent:{r}")
        for v in comp:
            rec = by_var[v]
            detail = " · ".join(x for x in (rec["model"], rec["class"] if rec["type"] != "agent" else None) if x)
            sys.node(f"agent:{v}", rec["name"], type="agent" if rec["type"] == "agent" else "orchestrator",
                     detail=detail or None, output=rec["output"], evidence=rec["evidence"])
            for h in rec["handoffs"]:
                if h in by_var:
                    sys.edge(f"agent:{v}", f"agent:{h}", "handoff", style="handoff")
            subs = [s for s in rec["sub_agents"] if s in by_var]
            for i, s in enumerate(subs):
                label = {"sequential": str(i + 1), "parallel": "paralelo", "loop": "loop"}.get(rec["type"], "subagente")
                sys.edge(f"agent:{v}", f"agent:{s}", label, style="handoff")
            for t in rec["tools"]:
                if t in by_var:
                    sys.edge(f"agent:{v}", f"agent:{t}", "como ferramenta", style="uses")
                    continue
                sys.node(f"tool:{t}", tool_names.get(t, t), type="tool")
                sys.edge(f"agent:{v}", f"tool:{t}", style="uses")
        systems.append(sys.data)
    return systems


def build_agents(root: Path) -> list[dict]:
    systems: list[dict] = []
    generic: list[dict] = []
    tool_names: dict[str, str] = {}
    for path in _walk(root, PY_SUFFIXES):
        text = _read(path)
        if not text or not LLM_IMPORT_RE.search(text):
            continue
        try:
            file = _PyAgents(text, _rel(root, path))
        except (SyntaxError, ValueError, RecursionError):
            continue
        tool_names.update({k: v["name"] for k, v in file.tools.items()})
        systems.extend(file.langgraph())
        systems.extend(file.crewai())
        systems.extend(file.claude_sdk())
        generic.extend(file.generic_agents())
        systems.extend(file.messages_tool_use())
    for path in _walk(root, JS_SUFFIXES):
        if path.name.endswith(".d.ts"):
            continue
        text = _read(path)
        if not text or not JS_LLM_IMPORT_RE.search(text):
            continue
        found, loose = _js_agents(text, _rel(root, path))
        systems.extend(found)
        generic.extend(loose)
    systems.extend(_group_generic(generic, tool_names))
    systems.extend(_crew_yaml(root))
    systems.extend(_claude_subagents(root))
    # One system per id; a file scanned by two readers must not draw twice.
    unique: dict[str, dict] = {}
    for sys in systems:
        unique.setdefault(sys["id"], sys)
    return list(unique.values())


# ===================================================================== entry


def build_modeling(root: Path, registry: TableRegistry) -> dict:
    """The whole modelling block for graph.json."""
    root = root.resolve()
    der = build_entities(registry)
    return {
        "entities": der["entities"],
        "relations": der["relations"],
        "pipelines": build_pipelines(root),
        "agents": build_agents(root),
    }


def summary(modeling: dict | None) -> dict[str, int]:
    modeling = modeling or {}
    return {
        "entities": len(modeling.get("entities") or []),
        "relations": len(modeling.get("relations") or []),
        "pipelines": len(modeling.get("pipelines") or []),
        "agents": len(modeling.get("agents") or []),
    }


# ============================================================ mermaid views

_ID_RE = re.compile(r"[^A-Za-z0-9_]")


def _mid(prefix: str, raw: str, used: dict[str, str]) -> str:
    """A mermaid-safe, unique node id for `raw`."""
    if raw in used:
        return used[raw]
    base = prefix + (_ID_RE.sub("_", raw).strip("_") or "n")
    out, n = base, 2
    taken = set(used.values())
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    used[raw] = out
    return out


def _q(text: str) -> str:
    """A label inside `["…"]`: no double quotes, no newlines."""
    return str(text).replace('"', "'").replace("\n", " ").strip()


def _er_type(value: str) -> str:
    """One token, as the type column of an entity: `java.math.BigDecimal` -> `BigDecimal`."""
    t = (value or "").strip()
    if "(" not in t:
        t = t.split(".")[-1]
    t = re.sub(r"\s+", "_", t).replace('"', "").replace("{", "").replace("}", "")
    return t[:28] or "?"


def er_mermaid(entities: list[dict], relations: list[dict], title: str) -> str:
    ids: dict[str, str] = {}
    lines = ["---", f"title: {title}", "---", "erDiagram"]
    names = {e["id"] for e in entities}
    for ent in entities:
        eid = _mid("", ent["id"], ids)
        lines.append(f'  {eid}["{_q(ent["id"])}"] {{')
        cols = ent["columns"]
        shown = cols[:MAX_COLUMNS_SHOWN]
        # Keys first: they are what the lines between boxes are about.
        shown = sorted(shown, key=lambda c: (not c.get("pk"), not c.get("fk")))
        for col in shown:
            keys = [k for k, flag in (("PK", col.get("pk")), ("FK", col.get("fk"))) if flag]
            line = f"    {_er_type(col.get('type') or '')} {_ID_RE.sub('_', col['name'])}"
            if keys:
                line += " " + ",".join(keys)
            if col.get("required") and not col.get("pk"):
                line += ' "not null"'
            lines.append(line)
        if len(cols) > MAX_COLUMNS_SHOWN:
            lines.append(f"    … mais {len(cols) - MAX_COLUMNS_SHOWN} colunas")
        lines.append("  }")
    for rel in relations:
        if rel["from"] not in names or rel["to"] not in names:
            continue
        a, b = ids[rel["from"]], ids[rel["to"]]
        many = "|o" if rel["cardinality"] == "1:1" else "}o"
        if rel.get("required"):
            many = "||" if rel["cardinality"] == "1:1" else "}|"
        link = "--" if rel["confidence"] == "static" else ".."
        label = rel["column"] + ("" if rel["confidence"] == "static" else " (inferido)")
        lines.append(f'  {a} {many}{link}|| {b} : "{_q(label)}"')
    return "\n".join(lines) + "\n"


STEP_SHAPE = {
    "dataset": ('[("', '")]'), "source": ('[/"', '"/]'), "condition": ('{"', '"}'), "branch": ('{"', '"}'),
    "foreach": ('{{"', '"}}'), "sensor": ('(["', '"])'), "pipeline": ('[["', '"]]'), "job": ('[["', '"]]'),
}
STEP_TONE = {
    "notebook": "service", "python": "service", "wheel": "service", "jar": "service", "submit": "service",
    "sql": "repo", "dbt": "repo", "pipeline": "accent", "job": "accent", "condition": "warn",
    "branch": "warn", "foreach": "warn", "sensor": "muted", "source": "external",
}
STEP_ICON = {
    "notebook": "📓", "python": "🐍", "sql": "🗄", "pipeline": "⇶", "job": "⚙", "dbt": "▤",
    "wheel": "📦", "condition": "?", "foreach": "↻", "sensor": "⏱",
}
LAYER_TITLE = {"source": "fontes", "bronze": "bronze · bruto", "silver": "silver · tratado", "gold": "gold · negócio"}


def pipeline_mermaid(pipe: dict) -> str:
    ids: dict[str, str] = {}
    head = [f"title: {pipe['name']}"]
    lines = ["flowchart LR"]
    layered = pipe["kind"] in ("dlt", "dbt") or any(s.get("layer") for s in pipe["steps"] if s["type"] == "dataset")

    def node(step: dict) -> str:
        sid = _mid("s_", step["id"], ids)
        label = step["label"]
        icon = STEP_ICON.get(step["type"])
        if icon and step["type"] not in ("dataset", "source"):
            label = f"{icon} {label}"
        extras = []
        if step.get("streaming"):
            extras.append("streaming")
        if step.get("kind") and step["type"] == "dataset":
            extras.append(str(step["kind"]).replace("_", " "))
        if step.get("expectations"):
            extras.append(f"{step['expectations']} expectativa(s)")
        if step.get("detail") and step["type"] not in ("dataset",):
            extras.append(str(step["detail"])[:48])
        if extras:
            label += "<br>" + " · ".join(extras)
        open_, close = STEP_SHAPE.get(step["type"], ('["', '"]'))
        tone = step.get("layer") if step["type"] == "dataset" and step.get("layer") else STEP_TONE.get(step["type"])
        return f"{sid}{open_}{_q(label)}{close}" + (f":::{tone}" if tone else "")

    if layered:
        groups: dict[str, list[dict]] = {}
        for step in pipe["steps"]:
            layer = step.get("layer") if step["type"] in ("dataset", "source") else None
            # A table the pipeline reads but never declares is an input, whatever its name.
            if step["type"] == "dataset" and not layer and not step.get("evidence"):
                layer = "source"
            groups.setdefault(layer or "", []).append(step)
        for layer in [*LAYER_ORDER, ""]:
            members = groups.get(layer) or []
            if not members:
                continue
            if layer:
                lines.append(f'  subgraph L_{layer}["{LAYER_TITLE[layer]}"]')
                lines.extend(f"    {node(s)}" for s in members)
                lines.append("  end")
            else:
                lines.extend(f"  {node(s)}" for s in members)
    else:
        lines.extend(f"  {node(s)}" for s in pipe["steps"])
    for edge in pipe["edges"]:
        if edge["from"] not in ids or edge["to"] not in ids:
            continue
        label = edge.get("label") or ""
        arrow = "-.->" if label == "stream" else ("==>" if label == "CDC" else "-->")
        lines.append(f"  {ids[edge['from']]} {arrow}{f'|{_q(label)}|' if label else ''} {ids[edge['to']]}")
    return "---\n" + "\n".join(head) + "\n---\n" + "\n".join(lines) + "\n"


NODE_SHAPE = {
    "start": ('(["', '"])'), "end": ('(["', '"])'), "tool": ('{{"', '"}}'), "tools": ('[["', '"]]'),
    "mcp": ('[("', '")]'), "router": ('{"', '"}'), "subgraph": ('[["', '"]]'), "orchestrator": ('[["', '"]]'),
}
NODE_TONE = {
    "start": "muted", "end": "muted", "agent": "agent", "orchestrator": "agent", "tool": "tool",
    "tools": "tool", "mcp": "external", "task": "service", "subgraph": "accent", "node": "service",
}
NODE_ICON = {"agent": "🤖", "orchestrator": "🧭", "tool": "🔧", "tools": "🧰", "task": "📋", "mcp": "🔌"}


def agents_mermaid(system: dict) -> str:
    ids: dict[str, str] = {}
    direction = "TB" if system["kind"] in ("langgraph", "crewai") else "LR"
    lines = [f"flowchart {direction}"]
    for n in system["nodes"]:
        nid = _mid("a_", n["id"], ids)
        label = n["label"]
        icon = NODE_ICON.get(n["type"])
        if icon:
            label = f"{icon} {label}"
        extras = [str(x)[:52] for x in (n.get("detail"), n.get("doc")) if x]
        if n.get("output"):
            extras.append(f"→ {n['output']}")
        if extras:
            label += "<br>" + "<br>".join(extras[:2])
        open_, close = NODE_SHAPE.get(n["type"], ('["', '"]'))
        tone = NODE_TONE.get(n["type"])
        lines.append(f"  {nid}{open_}{_q(label)}{close}" + (f":::{tone}" if tone else ""))
    for e in system["edges"]:
        if e["from"] not in ids or e["to"] not in ids:
            continue
        style = e.get("style", "flow")
        arrow = {"conditional": "-.->", "uses": "-.-", "data": "-.->", "handoff": "==>"}.get(style, "-->")
        label = e.get("label") or ""
        lines.append(f"  {ids[e['from']]} {arrow}{f'|{_q(label)}|' if label else ''} {ids[e['to']]}")
    return f"---\ntitle: {system['name']}\n---\n" + "\n".join(lines) + "\n"


PIPELINE_KIND = {"databricks-job": "job Databricks", "dlt": "Delta Live Tables", "dbt": "dbt", "airflow": "DAG Airflow"}
AGENT_KIND = {
    "langgraph": "LangGraph", "crewai": "CrewAI", "agents": "agentes", "claude-agent-sdk": "Claude Agent SDK",
    "claude-code": "Claude Code", "tool-use": "tool use",
}


def modeling_docs(modeling: dict | None) -> list[dict]:
    """The modelagem tab's documents: one DER (per schema when large), one per pipeline and per agent system."""
    modeling = modeling or {}
    docs: list[dict] = []
    entities = modeling.get("entities") or []
    relations = modeling.get("relations") or []
    if entities:
        if len(entities) <= MAX_ENTITIES_PER_DIAGRAM:
            chunks = [("DER — modelo de dados", entities)]
        else:
            by_schema: dict[str, list[dict]] = {}
            for ent in entities:
                by_schema.setdefault(ent.get("schema") or "public", []).append(ent)
            chunks = []
            for schema, ents in sorted(by_schema.items()):
                for i in range(0, len(ents), MAX_ENTITIES_PER_DIAGRAM):
                    part = ents[i : i + MAX_ENTITIES_PER_DIAGRAM]
                    suffix = f" ({i // MAX_ENTITIES_PER_DIAGRAM + 1})" if len(ents) > MAX_ENTITIES_PER_DIAGRAM else ""
                    chunks.append((f"DER — {schema}{suffix}", part))
        for i, (title, ents) in enumerate(chunks):
            docs.append({
                "name": f"modelagem/der{'' if i == 0 else f'-{i + 1}'}.mmd",
                "title": title, "format": "markdown", "group": "der",
                "summary": f"{len(ents)} tabelas",
                "text": er_mermaid(ents, relations, title),
            })
    for pipe in modeling.get("pipelines") or []:
        slug = _ID_RE.sub("-", pipe["id"]).strip("-").lower()[:60]
        docs.append({
            "name": f"modelagem/pipeline-{slug}.mmd",
            "title": pipe["name"], "format": "markdown", "group": "pipeline",
            "summary": f"{PIPELINE_KIND.get(pipe['kind'], pipe['kind'])} · {len(pipe['steps'])} etapas · {pipe['source']}",
            "text": pipeline_mermaid(pipe),
        })
    for system in modeling.get("agents") or []:
        slug = _ID_RE.sub("-", system["id"]).strip("-").lower()[:60]
        agents = sum(1 for n in system["nodes"] if n["type"] in ("agent", "orchestrator", "node", "task"))
        tools = sum(1 for n in system["nodes"] if n["type"] in ("tool", "tools", "mcp"))
        docs.append({
            "name": f"modelagem/agentes-{slug}.mmd",
            "title": system["name"], "format": "markdown", "group": "agents",
            "summary": f"{AGENT_KIND.get(system['kind'], system['kind'])} · {agents} nós · {tools} ferramentas · {system['source']}",
            "text": agents_mermaid(system),
        })
    return docs


# ================================================================ as text


def modeling_text(modeling: dict | None, *, max_items: int = 40) -> list[str]:
    """The modelagem block as markdown lines, for `croqui context` (an LLM's window)."""
    modeling = modeling or {}
    out: list[str] = []
    relations = modeling.get("relations") or []
    entities = modeling.get("entities") or []
    if entities:
        out.append(f"### modelo de dados — {len(entities)} tabelas, {len(relations)} relações")
        for rel in relations[:max_items]:
            how = "" if rel["confidence"] == "static" else " _(inferida pelo nome)_"
            out.append(f"- `{rel['from']}.{rel['column']}` → `{rel['to']}` ({rel['cardinality']}){how}")
        out.append("")
    for pipe in (modeling.get("pipelines") or [])[:max_items]:
        kind = PIPELINE_KIND.get(pipe["kind"], pipe["kind"])
        out.append(f"### pipeline {pipe['name']} — {kind} · `{pipe['source']}`")
        labels = {s["id"]: s["label"] for s in pipe["steps"]}
        for edge in pipe["edges"][:max_items]:
            tag = f" [{edge['label']}]" if edge.get("label") else ""
            out.append(f"- {labels.get(edge['from'], edge['from'])} → {labels.get(edge['to'], edge['to'])}{tag}")
        lone = [s for s in pipe["steps"] if not any(s["id"] in (e["from"], e["to"]) for e in pipe["edges"])]
        for step in lone[:max_items]:
            out.append(f"- {step['label']} ({step['type']})")
        out.append("")
    for system in (modeling.get("agents") or [])[:max_items]:
        kind = AGENT_KIND.get(system["kind"], system["kind"])
        out.append(f"### agentes {system['name']} — {kind} · `{system['source']}`")
        labels = {n["id"]: n["label"] for n in system["nodes"]}
        for edge in system["edges"][:max_items]:
            verb = {"uses": "usa", "handoff": "passa para", "conditional": "pode ir para", "data": "alimenta"}.get(
                edge.get("style", "flow"), "→")
            tag = f" [{edge['label']}]" if edge.get("label") else ""
            out.append(f"- {labels.get(edge['from'], edge['from'])} {verb} {labels.get(edge['to'], edge['to'])}{tag}")
        out.append("")
    return out
