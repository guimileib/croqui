"""Keep ``.croqui/layout.json`` attached to the graph after nodes are renamed.

Migrates layout entries from vanished ids to new ones only when the match is
unambiguous on both sides, and reports what was and was not migrated.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import clean_layout


class Reconciliation:
    """What reconciling *would* do, and what it could not work out."""

    def __init__(self) -> None:
        self.matches: list[tuple[str, str, str]] = []      # (old id, new id, why)
        self.groups: list[tuple[str, str, str]] = []       # (old name, new name, why)
        self.orphans: dict[str, list[str]] = {}            # unmatched id -> what pointed at it
        self.rewritten = 0                                 # references actually changed
        self.changed = False                               # layout.json needs writing
        self.broken: str | None = None                     # layout.json is unreadable

    @property
    def mapping(self) -> dict[str, str]:
        return {old: new for old, new, _ in self.matches}

    @property
    def group_mapping(self) -> dict[str, str]:
        return {old: new for old, new, _ in self.groups}

    def __bool__(self) -> bool:
        return bool(self.matches or self.groups or self.orphans or self.broken)


# --------------------------------------------------------------------- reading


def _nodes(graph: dict | None) -> list[dict]:
    return list((graph or {}).get("nodes") or [])


def _index(graph: dict | None) -> dict[str, dict]:
    return {n["id"]: n for n in _nodes(graph) if isinstance(n, dict) and n.get("id")}


def _meta(node: dict) -> dict:
    meta = node.get("meta")
    return meta if isinstance(meta, dict) else {}


def _file_of(node: dict) -> str | None:
    """``app/services.py:12`` -> ``app/services.py``. Line numbers move on their own."""
    source = _meta(node).get("source")
    if not isinstance(source, str) or not source:
        return None
    return source.rsplit(":", 1)[0] if ":" in source else source


def _group_of(node: dict) -> str:
    # Mirrors the viewer's groupKey(): a route with no group still lands somewhere.
    return node.get("group") or "—"


# ---------------------------------------------------------------- layout walker


def collect_refs(layout: dict) -> dict[str, list[str]]:
    """Every node id the layout names, mapped to what is naming it."""
    refs: dict[str, list[str]] = {}

    def add(node_id, what: str) -> None:
        if isinstance(node_id, str) and node_id:
            refs.setdefault(node_id, []).append(what)

    positions = layout.get("positions")
    if isinstance(positions, dict):
        for node_id in positions:
            add(node_id, "position")
    for node_id in layout.get("hidden") or []:
        add(node_id, "hidden")
    for link in layout.get("links") or []:
        if isinstance(link, dict):
            add(link.get("from"), "relation")
            add(link.get("to"), "relation")
    for shape in layout.get("shapes") or []:
        if not isinstance(shape, dict):
            continue
        if isinstance(shape.get("anchor"), dict):
            add(shape["anchor"].get("node"), "anchored drawing")
        # An arrow holding on to a node by one of its ends.
        for end in ("from", "to"):
            if isinstance(shape.get(end), dict):
                add(shape[end].get("node"), "arrow end")
    view = layout.get("view")
    if isinstance(view, dict):
        add(view.get("focus"), "focus")
    return refs


def rewrite_refs(layout: dict, mapping: dict[str, str], group_mapping: dict[str, str]) -> int:
    """Point every reference at its new id. Returns how many actually moved."""
    if not mapping and not group_mapping:
        return 0
    moved = 0

    positions = layout.get("positions")
    if isinstance(positions, dict) and mapping:
        out: dict[str, dict] = {}
        for key, value in positions.items():
            new = mapping.get(key, key)
            # A collision means the new id already had a position of its own; the
            # node that is really there wins over the one being renamed onto it,
            # and the stale entry is dropped rather than re-pointed.
            if new != key and new in positions:
                continue
            if new != key:
                moved += 1
            out[new] = value
        layout["positions"] = out

    hidden = layout.get("hidden")
    if isinstance(hidden, list) and mapping:
        seen: set[str] = set()
        out_hidden = []
        for node_id in hidden:
            new = mapping.get(node_id, node_id) if isinstance(node_id, str) else node_id
            if new != node_id:
                moved += 1
            if isinstance(new, str):
                if new in seen:
                    continue
                seen.add(new)
            out_hidden.append(new)
        layout["hidden"] = out_hidden

    if mapping:
        for link in layout.get("links") or []:
            if not isinstance(link, dict):
                continue
            for end in ("from", "to"):
                new = mapping.get(link.get(end))
                if new:
                    link[end] = new
                    moved += 1
        for shape in layout.get("shapes") or []:
            if not isinstance(shape, dict):
                continue
            if isinstance(shape.get("anchor"), dict):
                new = mapping.get(shape["anchor"].get("node"))
                if new:
                    shape["anchor"]["node"] = new
                    moved += 1
            for end in ("from", "to"):
                ref = shape.get(end)
                if not isinstance(ref, dict):
                    continue
                new = mapping.get(ref.get("node"))
                if new:
                    ref["node"] = new
                    moved += 1

    view = layout.get("view")
    if isinstance(view, dict):
        new = mapping.get(view.get("focus")) if mapping else None
        if new:
            view["focus"] = new
            moved += 1
        expanded = view.get("expanded")
        # `expanded` holds group *names*, not ids — the viewer synthesises
        # `group:<name>` from them, so they migrate on their own track.
        if isinstance(expanded, list) and group_mapping:
            out_expanded = []
            for name in expanded:
                renamed = group_mapping.get(name, name) if isinstance(name, str) else name
                if renamed != name:
                    moved += 1
                if renamed not in out_expanded:
                    out_expanded.append(renamed)
            view["expanded"] = out_expanded
    return moved


# ----------------------------------------------------------------- match rules

# (why, key) — a key of None means the rule has nothing to say about that node.
# Order is confidence: the first rule to claim a vanished id wins.
RULES: tuple[tuple[str, object], ...] = (
    (
        "same handler, same file",
        lambda n: (
            ("route", _file_of(n), _meta(n).get("handler"))
            if n.get("kind") == "route" and _file_of(n) and _meta(n).get("handler")
            else None
        ),
    ),
    (
        "same handler, file moved",
        lambda n: (
            ("route", _meta(n).get("handler"), _meta(n).get("method"))
            if n.get("kind") == "route" and _meta(n).get("handler")
            else None
        ),
    ),
    (
        "same class, same file",
        lambda n: (
            (n.get("kind"), _file_of(n))
            if n.get("kind") in ("service", "repo") and _file_of(n)
            else None
        ),
    ),
    (
        "same table name, schema changed",
        lambda n: (
            ("table", str(n.get("label") or "").lower())
            if n.get("kind") == "table" and n.get("label")
            else None
        ),
    ),
)


def _bucket(nodes: list[dict], key) -> dict[tuple, list[str]]:
    out: dict[tuple, list[str]] = {}
    for node in nodes:
        k = key(node)
        if k is None or None in k:
            continue
        out.setdefault(k, []).append(node["id"])
    return out


def match_nodes(old_graph: dict | None, new_graph: dict) -> list[tuple[str, str, str]]:
    """Pair vanished ids with new ones. Only unambiguous pairs are returned."""
    old_index, new_index = _index(old_graph), _index(new_graph)
    gone = [old_index[i] for i in old_index if i not in new_index]
    fresh = [new_index[i] for i in new_index if i not in old_index]
    if not gone or not fresh:
        return []

    matches: list[tuple[str, str, str]] = []
    claimed_old: set[str] = set()
    claimed_new: set[str] = set()

    for why, key in RULES:
        gone_left = [n for n in gone if n["id"] not in claimed_old]
        fresh_left = [n for n in fresh if n["id"] not in claimed_new]
        old_buckets = _bucket(gone_left, key)
        new_buckets = _bucket(fresh_left, key)
        for k, olds in sorted(old_buckets.items(), key=lambda item: str(item[0])):
            news = new_buckets.get(k)
            # One on each side or it is a guess, and a guess would move somebody's
            # annotation onto the wrong node.
            if not news or len(olds) != 1 or len(news) != 1:
                continue
            matches.append((olds[0], news[0], why))
            claimed_old.add(olds[0])
            claimed_new.add(news[0])
    return matches


def match_groups(
    old_graph: dict | None, new_graph: dict, mapping: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Endpoint groups are matched by which endpoints they still hold."""

    def members(graph: dict | None) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for node in _nodes(graph):
            if node.get("kind") == "route" and node.get("id"):
                out.setdefault(_group_of(node), set()).add(node["id"])
        return out

    old_members, new_members = members(old_graph), members(new_graph)
    gone = [name for name in old_members if name not in new_members]
    fresh = [name for name in new_members if name not in old_members]
    if not gone or not fresh:
        return []

    matches: list[tuple[str, str, str]] = []
    taken: set[str] = set()
    for name in sorted(gone):
        # Route renames already resolved, so a group whose endpoints only changed
        # name still recognises itself.
        want = {mapping.get(rid, rid) for rid in old_members[name]}
        scored = []
        for candidate in fresh:
            if candidate in taken:
                continue
            have = new_members[candidate]
            union = want | have
            if not union:
                continue
            scored.append((len(want & have) / len(union), candidate))
        scored.sort(reverse=True)
        if not scored or scored[0][0] < 0.5:
            continue
        # A tie means two groups have an equal claim; leave it to the human.
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            continue
        best = scored[0][1]
        taken.add(best)
        matches.append((name, best, f"{int(scored[0][0] * 100)}% of its endpoints in common"))
    return matches


# ------------------------------------------------------------------- entrypoint


def plan(layout: dict, old_graph: dict | None, new_graph: dict) -> Reconciliation:
    """Work out the migration without touching anything."""
    report = Reconciliation()
    refs = collect_refs(layout)
    known = set(_index(new_graph))

    found = match_nodes(old_graph, new_graph)
    # Groups are matched by which endpoints they still hold, so they need *every*
    # route rename — including ones no position or relation happens to name.
    report.groups = match_groups(old_graph, new_graph, {old: new for old, new, _ in found})
    # Only what the layout actually points at is worth rewriting or reporting.
    report.matches = [m for m in found if m[0] in refs]
    mapping = report.mapping

    for node_id, pointers in sorted(refs.items()):
        if node_id in mapping or node_id in known:
            continue
        # `group:<name>` boxes only exist while the viewer has them collapsed;
        # they are legitimate references to a name, not to a graph node.
        if node_id.startswith("group:"):
            continue
        report.orphans[node_id] = pointers
    return report


def apply(layout: dict, report: Reconciliation) -> int:
    """Rewrite the layout in place. Returns how many references moved."""
    report.rewritten = rewrite_refs(layout, report.mapping, report.group_mapping)
    report.changed = report.rewritten > 0
    return report.rewritten


def write_layout(path: Path, layout: dict) -> None:
    """Same shape the viewer writes, so a re-scan never shows up as a diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(clean_layout(layout), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def reconcile_file(
    layout_path: Path, old_graph: dict | None, new_graph: dict, *, migrate: bool = True
) -> Reconciliation:
    """Read the layout, migrate it against the new graph, write it back."""
    if not layout_path.is_file():
        return Reconciliation()
    report = Reconciliation()
    # An unreadable layout is reported, never rewritten: whatever is in there is
    # somebody's work, and the viewer refuses to save over it for the same reason.
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.broken = f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        return report
    except OSError as exc:
        report.broken = str(exc)
        return report
    if not isinstance(layout, dict):
        report.broken = f"expected an object, found {type(layout).__name__}"
        return report

    report = plan(layout, old_graph, new_graph)
    if migrate and (report.matches or report.groups):
        apply(layout, report)
        if report.changed:
            write_layout(layout_path, layout)
    return report
