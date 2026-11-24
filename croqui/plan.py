"""``croqui plan`` and ``croqui next``: one request, one PRD of steps, worked in relay.

``croqui plan "<request>"`` writes ``prd/pedido-<date>-<time>-<slug>.md``: the request broken
into numbered steps, each a ``### [ ] N. title`` heading. ``croqui next`` prints the
first open step — with what the agents before left as notes — and
``croqui next --done N --note "…"`` ticks it, so whoever opens the project next
(another agent, another session, a person) picks up exactly where the last one stopped.

croqui runs no agent: the file is the hand-off.
"""

from __future__ import annotations

import datetime
import json
import re
import unicodedata
from pathlib import Path

from . import llm
from .context import build, render
from .docs import section_dirs
from .scaffold import DEFAULT_ROOT, target_dir

MAX_TOKENS = 6000
PLAN_MARKER = "croqui:plano"
STEP_RE = re.compile(r"^### \[( |x|X)\] (\d+)\. (.+)$", re.MULTILINE)

PROMPT = """Você vai planejar a implementação de um pedido num sistema que já existe.
O plano será executado em revezamento: um agente de código faz uma etapa, registra o
que fez, e o próximo — que não viu a conversa anterior — pega a etapa seguinte. Por
isso cada etapa tem de se bastar sozinha.

PEDIDO
------
{request}
------

MAPA DO SISTEMA (extraído do código por análise estática; é factual)
---------------
{context}
---------------

Escreva em **português do Brasil**, em markdown, com exatamente estas seções:

# Plano — <título curto do pedido>

## Pedido
O pedido, reescrito em uma ou duas frases sem perder nada do que foi pedido.

## Contexto
O que do mapa este pedido toca: endpoints, serviços, tabelas, arquivos. Cite nomes reais.

## Etapas
De 2 a 8 etapas, na ordem em que têm de ser feitas. Cada uma neste formato exato:

### [ ] 1. Título da etapa
**O que fazer:** a mudança, concreta o bastante para alguém que só leu este arquivo.
**Arquivos:** os arquivos que provavelmente mudam.
**Como verificar:** um comando ou uma checagem que alguém consegue de fato executar.

## Fora de escopo
O que este pedido não inclui, para nenhum agente fazer a mais.

Regras:
- O cabeçalho de cada etapa é `### [ ] N. Título`, com N começando em 1. Não mude esse formato.
- Uma etapa é uma mudança que se entrega e se verifica sozinha; nada de "parte 1 de 2".
- **Não invente.** Arquivo, endpoint ou tabela que não está no mapa só entra se a etapa
  for justamente criá-lo, e então diga isso.
- Sem preâmbulo e sem despedida. Não envolva a resposta numa cerca de código.
"""

SKELETON = """# Plano — {title}

## Pedido
{request}

## Contexto
<o que do código este pedido toca: endpoints, serviços, tabelas, arquivos>

## Etapas

### [ ] 1. <título da etapa>
**O que fazer:** <a mudança, concreta o bastante para quem só leu este arquivo>
**Arquivos:** <os arquivos que provavelmente mudam>
**Como verificar:** <um comando ou checagem que dá para executar>

### [ ] 2. <título da etapa>
**O que fazer:**
**Arquivos:**
**Como verificar:**

## Fora de escopo
<o que este pedido não inclui>
"""

HEADER = "> {marker} — pedido de {today}, etapas escritas por {author}. `croqui next` mostra a próxima.\n\n"


def _slug(text: str, limit: int = 40) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return slug[:limit].rstrip("-") or "pedido"


def steps(text: str) -> list[dict]:
    """Every step heading in a plan, with the span of text that belongs to it."""
    found = []
    for match in STEP_RE.finditer(text):
        after = re.search(r"^#{2,3} ", text[match.end():], re.MULTILINE)
        end = match.end() + after.start() if after else len(text)
        found.append({
            "n": int(match.group(2)), "title": match.group(3).strip(),
            "done": match.group(1) != " ", "start": match.start(), "end": end,
        })
    return found


def plans(root: Path) -> list[Path]:
    """Every plan file of the project, oldest request first."""
    found = []
    for directory in section_dirs(root, "prd"):
        for path in directory.rglob("*.md"):
            try:
                if PLAN_MARKER in path.read_text(encoding="utf-8", errors="replace")[:400]:
                    found.append(path)
            except OSError:
                continue
    return sorted(found, key=lambda p: p.stem)


def draft(root: Path, request: str, *, engine: str | None = None, manual: bool = False,
          dry_run: bool = False, say=print, now: datetime.datetime | None = None) -> int:
    """Write the plan for one request. Returns a process exit code."""
    request = " ".join(request.split())
    if not request:
        say('diga o pedido:  croqui plan "o que precisa ser feito"')
        return 2
    now = now or datetime.datetime.now()
    # The timestamp is the queue: `croqui next` works the oldest request first.
    target = target_dir(root, "prd") / f"pedido-{now.strftime('%Y%m%d-%H%M%S')}-{_slug(request)}.md"
    rel = target.relative_to(root) if target.is_relative_to(root) else target

    if manual:
        author, body = "quem abriu o pedido", SKELETON.format(title=request[:60], request=request)
    else:
        try:
            chosen = llm.resolve(engine)
        except llm.LLMError as exc:
            say(str(exc))
            say("  · sem modelo:  croqui plan --manual \"…\"  escreve o esqueleto para preencher à mão")
            return 2
        try:
            graph = json.loads((root / DEFAULT_ROOT / "graph.json").read_text(encoding="utf-8"))
            context = render(build(root, graph))
        except (OSError, json.JSONDecodeError):
            context = "(sem mapa: `croqui scan` ainda não rodou neste projeto)"
        prompt = PROMPT.format(request=request, context=context)
        if dry_run:
            say(f"engine: {chosen.how}")
            say(f"alvo:   {rel}")
            say(f"prompt: {len(prompt)} caracteres, ~{len(prompt) // 4} tokens")
            return 0
        say(f"planejando as etapas com {chosen.how}…")
        try:
            body = llm.strip_fence(chosen.complete(prompt, max_tokens=MAX_TOKENS))
        except llm.LLMError as exc:
            say(f"falhou: {exc}")
            return 1
        if not steps(body):
            say("o modelo não devolveu nenhuma etapa no formato `### [ ] N. título` — nada foi escrito.")
            return 1
        author = chosen.how
    if dry_run:
        say(f"alvo:   {rel}  (esqueleto, sem modelo)")
        return 0

    text = HEADER.format(marker=PLAN_MARKER, today=now.date().isoformat(), author=author) + body.strip() + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        say(f"não deu para escrever {rel}: {exc.strerror or exc}")
        return 1
    say(f"plano escrito em {rel}" + (" — preencha as etapas" if manual else f" ({len(steps(text))} etapas)"))
    say("  · próxima etapa:  croqui next")
    return 0


def next_step(root: Path, *, plan: str = "", done: int | None = None, note: str = "",
              say=print, today: datetime.date | None = None) -> int:
    """Print the first open step, or tick one off. Returns a process exit code."""
    today = today or datetime.date.today()
    found = plans(root)
    if plan:
        found = [p for p in found if plan in p.name]
    if not found:
        say("nenhum plano aqui" + (f" com `{plan}` no nome" if plan else "") + '. Crie um:  croqui plan "o pedido"')
        return 1

    def read(path: Path) -> tuple[str, list[dict]]:
        text = path.read_text(encoding="utf-8")
        return text, steps(text)

    # The plan being worked is the oldest one with a step still open.
    path = next((p for p in found if any(not s["done"] for s in read(p)[1])), None)
    if path is None:
        say("todas as etapas de todos os planos estão concluídas.")
        return 0
    text, all_steps = read(path)

    if done is not None:
        step = next((s for s in all_steps if s["n"] == done and not s["done"]), None)
        if step is None:
            say(f"{path.name} não tem etapa {done} em aberto.")
            return 1
        head = text[step["start"]:step["end"]].replace("### [ ]", "### [x]", 1).rstrip()
        record = f"> feito em {today.isoformat()}" + (f": {' '.join(note.split())}" if note.strip() else "")
        path.write_text(text[:step["start"]] + head + "\n" + record + "\n\n" + text[step["end"]:], encoding="utf-8")
        say(f"etapa {done} concluída em {path.name}")
        text, all_steps = read(path)
        if all(s["done"] for s in all_steps):
            say("plano concluído.")
            return 0

    step = next(s for s in all_steps if not s["done"])
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    say(f"plano: {rel} — etapa {step['n']} de {len(all_steps)}")
    asked = re.search(r"^## Pedido\s*\n(.+?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    if asked:
        say(f"pedido: {' '.join(asked.group(1).split())}")
    for s in all_steps:
        if s["done"]:
            notes = re.findall(r"^> feito em .*$", text[s["start"]:s["end"]], re.MULTILINE)
            say(f"  [x] {s['n']}. {s['title']}" + (f"  {notes[-1][2:]}" if notes else ""))
    say("")
    say(text[step["start"]:step["end"]].strip())
    say("")
    say(f'ao terminar:  croqui next --done {step["n"]} --note "o que mudou e o que o próximo precisa saber"')
    return 0
