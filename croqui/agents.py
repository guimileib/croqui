"""`croqui install`: the /croqui command for Claude Code, Codex and Gemini CLI.

One instruction text, written in each tool's own format:

* Claude Code  ``~/.claude/skills/croqui/SKILL.md``  -> ``/croqui <request>``
* Codex        ``~/.agents/skills/croqui/SKILL.md``  -> ``$croqui <request>`` (or ``/skills``)
               ``~/.codex/prompts/croqui.md``        -> ``/prompts:croqui <request>``
* Gemini CLI   ``~/.gemini/commands/croqui.toml``    -> ``/croqui <request>``

Next to it goes ``croqui-plan``, in the same places: the instruction that makes an
assistant turn every implementation request into a PRD of steps (``croqui plan``) and
work it one step at a time (``croqui next``), so the next agent picks up the hand-off.

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

A diagram argues, it does not list: real names, real evidence, a structure that
makes the point. Mermaid `.mmd` for a quick view; Excalidraw `.excalidraw` to explain
or present.

## 1. Draw from the map (free: no model tokens)

About this codebase:

1. No `.croqui/graph.json`, or the code changed since: `croqui scan .` (seconds, no LLM).
2. `croqui draw "<endpoint path, table or service>"` writes a finished diagram to the
   UML folder: layers, colours, labelled edges, the chain down to the tables.
   `--format excalidraw` adds a summary line, sections and evidence (columns,
   methods, handlers). Several targets are fine; none draws an overview. Also
   `--name`, `--direction TB`, `--depth N`, `--max-nodes N`, `--stdout`.
3. Edit only what the map cannot know (business rules, decisions, better names), then
   delete the `croqui:draw` marker (`%%` line, or the `"source"` text) so the next
   draw does not replace it. `croqui context --node "<name>"` has the facts; open at
   most 3 source files, only the relevant function.
4. Never run `croqui enrich` or `croqui prd` (they call an LLM). Install if missing:
   `pipx install croqui`.

Not about this codebase (e.g. "a login flow for a new app"): write it by hand, section 2.

## 2. Writing one by hand

Excalidraw: run `croqui design` and follow it. File: `<uml folder>/<name>.excalidraw`.

Mermaid: `<uml folder>/<kebab-case-name>.mmd` (folder under "como escrever aqui" in
`croqui context`, default `.croqui/uml/`). Never edit a file with `%% croqui:gerado`.
croqui draws `flowchart` (`LR` pipelines, `TB` hierarchies) and `sequenceDiagram`;
do an ER view as a flowchart of `[(table)]` nodes.

- Frontmatter `title`; one `subgraph` with a quoted title per layer or context.
- Shapes: endpoint `([POST /leads])`, service `[LeadService]`, repository
  `[[LeadRepository]]`, table `[(tb_leads)]`, decision `{{qualified?}}`, external
  `{{{{Stripe}}}}`.
- Colour is meaning, by palette class (no `classDef`): `:::get` `:::post` `:::put`
  `:::patch` `:::delete` `:::route` `:::service` `:::repo` `:::table` `:::external`
  `:::ok` `:::warn` `:::danger` `:::muted` `:::accent`.
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

- `croqui lint` (exit 1 = error): fix everything it reports (for `.excalidraw`:
  bindings, text that does not fit, overlaps) and re-run until clean.
- `croqui build .` writes `.croqui/croqui.html`; the **UML** tab draws both formats,
  and its "levar ao mapa" copies a diagram onto the map.
- Never run `croqui serve` (it never exits). Tell the user it is the live view
  (http://127.0.0.1:7777).

Reply in at most 3 lines: what you drew, the file path, how to open it. Do not paste
the diagram unless asked.
"""


PLAN_DESCRIPTION = (
    "Turn a request to implement, build, fix or change code into a PRD of steps and work it "
    "one step at a time, handing off to the next agent. Use on every implementation request, "
    "when the user types /croqui-plan, or asks to continue, resume or pick up pending work."
)
PLAN_HINT = 'what to implement, e.g. "export orders as CSV" (empty: continue the open plan)'

PLAN_BODY = """# croqui-plan: one request, one PRD of steps, worked in relay

Request: {request}

Another agent may have started this work and you may not finish it. The plan file is
the hand-off: nothing that matters may live only in your conversation.

## 1. Look for open work first

Run `croqui next`. If it prints a step, that plan is not finished: do that step (go
to 3) unless the request above is clearly a different piece of work.

## 2. A new request gets a plan before any code

- Run `croqui plan --manual "<the request, in the user's words>"`. It creates
  `prd/pedido-<date>-<slug>.md` and prints the path.
- Fill that file in: **Contexto** from the code (`croqui context` is the map, as
  text), then 2 to 8 steps. Keep each heading exactly `### [ ] N. Title`, with
  **O que fazer**, **Arquivos** and **Como verificar** under it. A step is a change
  that ships and is verified on its own, written for someone who read only this file.
- Show the user the list of steps in a few lines before starting.

## 3. One step at a time

- `croqui next` prints the step to do and the notes of the steps before it.
- Implement that step only, then run its **Como verificar**.
- `croqui next --done N --note "what changed, what the next agent must know"`:
  files touched, decisions taken, anything left half-done. No note, no hand-off.
- Stop after the step when the user asked for one step, when verification fails, or
  when the next step needs a decision that is theirs. Otherwise take the next one.

Reply in the user's language: which step you did, how it was verified, what is next.
"""

# name, description, argument hint, body: every command `croqui install` writes.
SKILLS = [
    ("croqui", DESCRIPTION, ARGUMENT_HINT, BODY),
    ("croqui-plan", PLAN_DESCRIPTION, PLAN_HINT, PLAN_BODY),
]


def _yaml(text: str) -> str:
    """A single-quoted YAML scalar: safe for the colons and quotes in our text."""
    return "'" + text.replace("'", "''") + "'"


def _skill(spec: tuple, request: str) -> str:
    name, description, hint, body = spec
    return (
        f"---\nname: {name}\ndescription: {_yaml(description)}\nargument-hint: {_yaml(hint)}\n---\n\n"
        + body.format(request=request)
    )


def _codex_prompt(spec: tuple) -> str:
    _, description, hint, body = spec
    return (
        f"---\ndescription: {_yaml(description)}\nargument-hint: {_yaml(hint)}\n---\n\n"
        + body.format(request="$ARGUMENTS")
    )


def _gemini_command(spec: tuple = SKILLS[0]) -> str:
    _, description, _, body = spec
    body = body.format(request="{{args}}")
    # TOML multi-line literal strings cannot contain three single quotes in a row.
    assert "'''" not in body
    return f'description = "{description}"\n' + "prompt = '''\n" + body + "'''\n"


def _targets(tool: str) -> list:
    out = []
    for spec in SKILLS:
        name = spec[0]
        if tool == "claude":
            out.append((f".claude/skills/{name}/SKILL.md", lambda spec=spec: _skill(spec, "$ARGUMENTS")))
        elif tool == "codex":
            out.append((f".agents/skills/{name}/SKILL.md",
                        lambda spec=spec: _skill(spec, f"the text after ${spec[0]} in the user's message")))
            out.append((f".codex/prompts/{name}.md", lambda spec=spec: _codex_prompt(spec)))
        else:
            out.append((f".gemini/commands/{name}.toml", lambda spec=spec: _gemini_command(spec)))
    return out


TARGETS = {tool: _targets(tool) for tool in ("claude", "codex", "gemini")}
USAGE = {
    "claude": "/croqui <desenho>   /croqui-plan <pedido>",
    "codex": "$croqui <desenho>  ou  /prompts:croqui <desenho>   $croqui-plan <pedido>",
    "gemini": "/croqui <desenho>   /croqui-plan <pedido>",
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
