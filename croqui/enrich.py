"""LLM enrichment for the nodes static analysis left ``unresolved``.

One code slice per prompt, results cached by content hash. Tables the model returns
are checked against :mod:`croqui.tables`: invented ones are dropped, the rest are
marked ``confidence: "llm"``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

from . import llm
from .tables import build_registry

CACHE_FILE = "cache.json"
MAX_SLICE_CHARS = 6000
BATCH_SIZE = 3
WRITE_OPS = {"INSERT", "UPDATE", "DELETE"}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "The node id given in the task."},
                    "purpose": {"type": "string", "description": "One sentence, what this class is for."},
                    "tables": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Table name, exactly as listed in KNOWN TABLES."},
                                "ops": {
                                    "type": "array",
                                    "items": {"type": "string", "enum": ["SELECT", "INSERT", "UPDATE", "DELETE"]},
                                },
                            },
                            "required": ["name", "ops"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["node", "purpose", "tables"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

PROMPT = """You are mapping which database tables a class touches, for an architecture diagram.

KNOWN TABLES (the only valid values for `name` — never invent one):
{tables}

For each task below, read the code and report:
- `purpose`: one sentence on what the class does.
- `tables`: every table it reads or writes, with the SQL operations it performs.

Rules:
- Use only names from KNOWN TABLES. If the code touches something not listed, omit it.
- Report a table only when the code actually queries it. Do not guess from the class name.
- If the class touches no table at all, return an empty `tables` array.

{tasks}

Reply with JSON only, matching this shape:
{{"results": [{{"node": "...", "purpose": "...", "tables": [{{"name": "...", "ops": ["SELECT"]}}]}}]}}
"""


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------- slicing


def _slice_for(root: Path, node: dict) -> str | None:
    """Extract the source of the class a node points at."""
    source = node["meta"].get("source")
    if not source or ":" not in source:
        return None
    rel, _, line = source.rpartition(":")
    path = root / rel
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text[:MAX_SLICE_CHARS]
    for item in ast.walk(tree):
        if isinstance(item, ast.ClassDef) and item.name == node["label"]:
            segment = ast.get_source_segment(text, item)
            if segment:
                return segment[:MAX_SLICE_CHARS]
    return text[:MAX_SLICE_CHARS]


def _digest(payload: str) -> str:
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------- entry


def enrich(root: Path, graph_path: Path, *, engine: str, dry_run: bool, limit: int) -> int:
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in graph["nodes"]}
    pending = [n for n in graph["nodes"] if n["meta"].get("unresolved")]
    if limit:
        pending = pending[:limit]

    if not pending:
        _say("nothing unresolved — static analysis covered the whole graph")
        return 0

    registry = build_registry(root)
    table_list = ", ".join(f"{r['schema']}.{r['name']}" if r["schema"] else r["name"] for r in registry.all())

    tasks = []
    for node in pending:
        slice_ = _slice_for(root, node)
        if slice_ is None:
            _say(f"  · no source for {node['label']}, skipping")
            continue
        tasks.append((node, slice_))

    if not tasks:
        _say("no readable sources among the unresolved nodes")
        return 0

    cache_path = graph_path.parent / CACHE_FILE
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}

    fresh = [(n, s) for n, s in tasks if _digest(s) not in cache]
    _say(f"{len(tasks)} unresolved · {len(tasks) - len(fresh)} cached · {len(fresh)} to send")

    if dry_run:
        chars = sum(len(s) for _, s in fresh) + len(table_list)
        _say(f"dry run: would send ~{chars} chars (~{chars // 4} tokens) in {(len(fresh) + BATCH_SIZE - 1) // BATCH_SIZE} calls")
        for node, _ in fresh:
            _say(f"  {node['kind']:<8} {node['label']}  ({node['meta'].get('hint', '')})")
        return 0

    try:
        chosen = llm.resolve(engine)
    except llm.LLMError as exc:
        _say(str(exc))
        return 2
    _say(f"  usando {chosen.how}")
    for start in range(0, len(fresh), BATCH_SIZE):
        batch = fresh[start : start + BATCH_SIZE]
        blocks = "\n\n".join(
            f"### TASK node={node['id']}\nfile: {node['meta'].get('source')}\n```python\n{slice_}\n```"
            for node, slice_ in batch
        )
        try:
            payload = llm.parse_json(chosen.complete(
                PROMPT.format(tables=table_list, tasks=blocks), schema=RESULT_SCHEMA))
        except Exception as exc:
            _say(f"  ! batch failed: {exc}")
            continue
        # Cache by slice digest so an unrelated edit elsewhere does not invalidate it.
        for node, slice_ in batch:
            match = next((r for r in payload.get("results", []) if r.get("node") == node["id"]), None)
            if match is not None:
                cache[_digest(slice_)] = match
        _say(f"  ✓ {start + len(batch)}/{len(fresh)}")

    # Replay cached answers for everything we did not just send.
    for node, slice_ in tasks:
        cached = cache.get(_digest(slice_))
        if cached is not None:
            _absorb(graph, nodes, registry, cached)

    graph_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _say(f"✓ updated {graph_path.name}")
    return 0


def _absorb(graph: dict, nodes: dict, registry, item: dict) -> None:
    """Merge one LLM result, rejecting any table not in the ground truth."""
    node = nodes.get(item.get("node"))
    if node is None:
        return
    if item.get("purpose"):
        node["meta"]["summary"] = item["purpose"]

    accepted = 0
    for entry in item.get("tables") or []:
        record = registry.resolve(entry.get("name", ""))
        if record is None:
            continue  # hallucinated or unknown — dropped
        qualified = registry.qualified(entry["name"])
        table_id = f"table:{qualified}"
        if table_id not in nodes:
            new = {
                "id": table_id,
                "kind": "table",
                "label": record["name"],
                "group": record["schema"] or "public",
                "meta": {
                    "schema": record["schema"],
                    "object_kind": record["kind"],
                    "verified": True,
                    "defined_in": record["sources"][:4],
                },
            }
            graph["nodes"].append(new)
            nodes[table_id] = new
        ops = sorted({o for o in entry.get("ops", []) if o in {"SELECT", "INSERT", "UPDATE", "DELETE"}}) or ["SELECT"]
        kind = "write" if WRITE_OPS & set(ops) else "read"
        existing = next(
            (e for e in graph["edges"] if e["from"] == node["id"] and e["to"] == table_id and e["kind"] == kind),
            None,
        )
        if existing is None:
            graph["edges"].append(
                {
                    "from": node["id"],
                    "to": table_id,
                    "kind": kind,
                    "confidence": "llm",
                    "ops": ops,
                    "evidence": [node["meta"].get("source", "")],
                }
            )
        accepted += 1

    if accepted:
        node["meta"].pop("unresolved", None)
        node["meta"].pop("hint", None)
    else:
        node["meta"]["hint"] = "LLM found no table reference either"
