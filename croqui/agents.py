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
BODY = """# croqui: draw what the user asks, cheaply and beautifully

Request: {request}

If the request is empty, ask in one line what to draw and stop.
Answer in the user's language.

## 1. Gather facts, cheaply

Read croqui's static map, not the source tree. Every command below is static
analysis: it costs no model tokens beyond its own output.

- Request about this codebase:
  1. If `.croqui/graph.json` is missing, or the code changed since it was written,
     run `croqui scan .` (seconds, no LLM).
  2. Narrow first. For each concrete thing the request names (an endpoint path, a
     table, a service), run `croqui context --node "<name>"`. Run plain
     `croqui context` (the whole digest) only when the request is about the
     whole system.
  3. Open source files only for what the digest cannot tell (a business rule, a
     condition, an error path). Read at most 3 files, and only the relevant
     function.
  4. Never run `croqui enrich` or `croqui prd` unless asked: they call an LLM.
- Request not about this codebase (e.g. "a login flow for a new app"): skip all of
  the above and draw from the description.

If `croqui` is not installed: `pipx install croqui` (or `pip install croqui`).

## 2. Draw one Mermaid file

Write `<uml folder>/<kebab-case-name>.mmd`. The UML folder is the one under
"como escrever aqui" in `croqui context` (default `.croqui/uml/`). Never edit a
file that still contains the line `%% croqui:gerado`; write a new file next to it.

croqui draws `flowchart` (preferred; `LR` for pipelines, `TB` for hierarchies)
and `sequenceDiagram` (preferred for request/response timing). Other diagram
types are shown as source code, not drawn: express an ER view as a flowchart of
`[(table)]` nodes.

Style rules, for a diagram that looks good and reads at a glance:

- Start with a frontmatter `title`.
- Shapes: endpoint `([POST /leads])`, service `[LeadService]`, repository
  `[[LeadRepository]]`, table `[(tb_leads)]`, decision `{{qualified?}}`, external
  system `{{{{Stripe}}}}`.
- Colour with croqui's built-in palette classes. They follow the light/dark
  theme and need no `classDef`: `:::get` `:::post` `:::put` `:::patch`
  `:::delete` `:::route` `:::service` `:::repo` `:::table` `:::external` `:::ok`
  `:::warn` `:::danger` `:::muted` `:::accent`. Or use `class A,B table`.
- Group each layer or bounded context in a `subgraph` with a quoted title.
- Label edges with what happens: `-->|grava: insert|`, `-.->|lê|` for reads, `==>`
  for the main path.
- At most 25 nodes and 4 words per label, one idea per diagram. Split a big
  request into several files rather than one dense one.

Example:

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
    t2[(tb_lead_note)]:::table
  end
  crm{{{{CRM API}}}}:::external
  r1 ==> s1 ==> d1
  d1 -->|yes| t1
  d1 -->|yes| t2
  d1 -.->|no| r1
  s1 -->|sync| crm
```

## 3. Show it

- Run `croqui build .`. It returns at once and writes one self-contained
  `.croqui/croqui.html`; the diagram is in its **UML** tab.
- Do not run `croqui serve` yourself: it is a server and never exits, so it would
  hang this session. Tell the user they can run `croqui serve` for the live,
  editable map (http://127.0.0.1:7777, **UML** tab, re-reads the disk on reload).

Reply in at most 3 lines: what you drew, the file path, how to open it. Do not
paste the diagram source unless asked.
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
