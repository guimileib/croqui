"""`croqui install`: the /croqui command for Claude Code, Codex and Gemini CLI.

One instruction text, written in each tool's own format:

* Claude Code  ``~/.claude/skills/croqui/SKILL.md``  -> ``/croqui <request>``
* Codex        ``~/.agents/skills/croqui/SKILL.md``  -> ``$croqui <request>`` (or ``/skills``)
               ``~/.codex/prompts/croqui.md``        -> ``/prompts:croqui <request>``
* Gemini CLI   ``~/.gemini/commands/croqui.toml``    -> ``/croqui <request>``

``--project`` writes the same files under the current repository instead of $HOME.
"""

from __future__ import annotations

from pathlib import Path

DESCRIPTION = (
    "Draw what the user asks as a hand-drawn diagram in croqui: API flows, architecture, "
    "which endpoints touch which tables, sequences, or any idea described in words. Use when "
    "the user types /croqui or asks to visualize, diagram or map code, a flow or the database."
)
ARGUMENT_HINT = 'what to draw, e.g. "lead qualification flow" or "tables written by POST /leads"'

# The instructions every tool gets. {request} is each tool's argument placeholder.
BODY = """# croqui: draw what the user asks, cheaply

Request: {request}

If the request is empty, ask in one line what to draw and stop. Answer in the user's language.

## 1. Draw from the map (free: no model tokens)

About this codebase:

1. No `.croqui/graph.json`, or the code changed since: `croqui scan .` (seconds, no LLM).
2. `croqui draw "<endpoint path, table or service>"` writes a finished, styled diagram
   to the UML folder: layers, shapes, colours, labelled edges, the chain down to the
   tables. Several targets are fine; none draws an overview. Options: `--name`,
   `--direction TB`, `--depth N`, `--max-nodes N`, `--stdout`.
3. Edit that file only for what the map cannot know (business conditions, decisions,
   better names). Delete its `%% croqui:draw` line once you do, or the next draw
   replaces it. `croqui context --node "<name>"` has the facts; open at most 3
   source files, only the relevant function.
4. Never run `croqui enrich` or `croqui prd` (they call an LLM). Install if missing:
   `pipx install croqui`.

Not about this codebase (e.g. "a login flow for a new app"): skip the above and write
the diagram by hand, section 2.

## 2. Writing one by hand

File: `<uml folder>/<kebab-case-name>.mmd` (folder under "como escrever aqui" in
`croqui context`, default `.croqui/uml/`). Never edit a file with `%% croqui:gerado`.

croqui draws `flowchart` (`LR` pipelines, `TB` hierarchies) and `sequenceDiagram`
(request/response timing). Other types are shown as source: do an ER view as a
flowchart of `[(table)]` nodes.

- Start with a frontmatter `title`.
- Shapes: endpoint `([POST /leads])`, service `[LeadService]`, repository
  `[[LeadRepository]]`, table `[(tb_leads)]`, decision `{{qualified?}}`, external
  `{{{{Stripe}}}}`.
- Colour with palette classes (theme-aware, no `classDef`): `:::get` `:::post`
  `:::put` `:::patch` `:::delete` `:::route` `:::service` `:::repo` `:::table`
  `:::external` `:::ok` `:::warn` `:::danger` `:::muted` `:::accent`.
- One `subgraph` with a quoted title per layer or context.
- Label edges: `-->|grava: insert|`, `-.->|lê|` reads, `==>` main path.
- At most 25 nodes and 4 words per label; split big requests into several files.

```mermaid
---
title: Leads — POST /leads
---
flowchart LR
  subgraph api["API"]
    r1([POST /leads]):::post
  end
  subgraph domain["domain"]
    s1[LeadService]:::service
    d1{{qualified?}}:::warn
  end
  subgraph data["database"]
    t1[(tb_leads)]:::table
  end
  crm{{{{CRM API}}}}:::external
  r1 ==> s1 ==> d1
  d1 -->|yes| t1
  d1 -.->|no| r1
  s1 -->|sync| crm
```

## 3. Check and show

- `croqui lint` (exit 1 = error): fix what it reports before building.
- `croqui build .` writes `.croqui/croqui.html`; the diagram is in its **UML** tab.
- Never run `croqui serve` (it never exits). Tell the user it is the live view
  (http://127.0.0.1:7777).

Reply in at most 3 lines: what you drew, the file path, how to open it. Do not paste
the diagram unless asked.
"""


def _yaml(text: str) -> str:
    """A single-quoted YAML scalar: safe for the colons and quotes in our text."""
    return "'" + text.replace("'", "''") + "'"


def _skill(request: str) -> str:
    return (
        f"---\nname: croqui\ndescription: {_yaml(DESCRIPTION)}\nargument-hint: {_yaml(ARGUMENT_HINT)}\n---\n\n"
        + BODY.format(request=request)
    )


def _codex_prompt() -> str:
    return (
        f"---\ndescription: {_yaml(DESCRIPTION)}\nargument-hint: {_yaml(ARGUMENT_HINT)}\n---\n\n"
        + BODY.format(request="$ARGUMENTS")
    )


def _gemini_command() -> str:
    body = BODY.format(request="{{args}}")
    # TOML multi-line literal strings cannot contain three single quotes in a row.
    assert "'''" not in body
    return f'description = "{DESCRIPTION}"\n' + "prompt = '''\n" + body + "'''\n"


TARGETS = {
    "claude": [(".claude/skills/croqui/SKILL.md", lambda: _skill("$ARGUMENTS"))],
    "codex": [
        (".agents/skills/croqui/SKILL.md", lambda: _skill("the text after $croqui in the user's message")),
        (".codex/prompts/croqui.md", _codex_prompt),
    ],
    "gemini": [(".gemini/commands/croqui.toml", _gemini_command)],
}
USAGE = {
    "claude": "/croqui <pedido>",
    "codex": "$croqui <pedido>  ou  /prompts:croqui <pedido>",
    "gemini": "/croqui <pedido>",
}
# A tool counts as present when its home folder exists.
MARKERS = {"claude": ".claude", "codex": ".codex", "gemini": ".gemini"}


def detect_tools(home: Path) -> list[str]:
    return [tool for tool, marker in MARKERS.items() if (home / marker).is_dir()]


def install(tools: list[str], base: Path) -> list[tuple[str, Path]]:
    """Write the command files for ``tools`` under ``base``; return (tool, path) written."""
    written: list[tuple[str, Path]] = []
    for tool in tools:
        for relpath, render in TARGETS[tool]:
            # Codex reads custom prompts from $HOME only; in a repo the skill is enough.
            if relpath.startswith(".codex/prompts") and base != Path.home():
                continue
            target = base / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render(), encoding="utf-8", newline="\n")
            written.append((tool, target))
    return written


def uninstall(tools: list[str], base: Path) -> list[Path]:
    removed: list[Path] = []
    for tool in tools:
        for relpath, _ in TARGETS[tool]:
            target = base / relpath
            if target.is_file():
                target.unlink()
                removed.append(target)
                if target.name == "SKILL.md" and not any(target.parent.iterdir()):
                    target.parent.rmdir()
    return removed
