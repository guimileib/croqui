"""``croqui prd``: write the PRD from the map with whatever model is available.

Only replaces files still marked ``croqui:rascunho`` (croqui's draft); a file a
person wrote is refused unless ``--force`` or a new name is given.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from . import llm
from .context import build, render
from .docs import collect_section
from .scaffold import DRAFT_MARKER, PRD_FILE, target_dir

MAX_TOKENS = 8000

PROMPT = """Você vai escrever o PRD de um sistema que já existe, a partir do mapa da
arquitetura dele. O mapa foi extraído do código-fonte por análise estática, então ele
é factual: os endpoints, as tabelas e as ligações abaixo existem de verdade.

{steer}
MAPA DO SISTEMA
---------------
{context}
---------------

Escreva o PRD em **português do Brasil**, em markdown, com exatamente estas seções e
nesta ordem:

# PRD — {name}

## Problema
Que dor este sistema resolve, para quem, e como ela aparecia antes dele. Deduza do que
o sistema faz. Não descreva a solução aqui.

## Quem usa
Os papéis que o mapa deixa ver — quem chama estes endpoints — e o que cada um precisa
conseguir terminar.

## Escopo
O que o sistema entrega hoje. Uma lista curta de capacidades verificáveis, cada uma
apoiada em endpoints ou tabelas que existem no mapa.

## Fora de escopo
O que o sistema visivelmente **não** faz. Deduza das ausências no mapa e diga que é
uma leitura sua.

## Requisitos
Uma tabela markdown com as colunas `#`, `requisito` e `como se verifica`. Cada
requisito tem de ser rastreável a algo no mapa, e a verificação tem de ser uma coisa
que alguém consegue de fato executar.

## Como isto se relaciona com o código
Ligue cada capacidade aos endpoints e tabelas concretos que a implementam. É aqui que
o PRD e o mapa se encontram, então cite nomes reais.

## Perguntas em aberto
O que o código não responde: regra de negócio que só uma pessoa sabe, decisão que
parece deliberada mas não está explicada, ligação que o mapa marca como incerta.

Regras, e elas importam mais que o formato:
- **Não invente.** Endpoint, tabela ou serviço que não está no mapa não entra no
  documento. Se algo que um PRD normalmente teria não dá para saber pelo código,
  escreva isso em *Perguntas em aberto* em vez de preencher com plausibilidade.
- Onde você estiver deduzindo em vez de lendo, diga. "Pelo mapa, parece que…" é uma
  frase melhor que uma afirmação errada com cara de certa.
- Sem preâmbulo e sem despedida. Comece no `# PRD` e termine no fim da última seção.
- Não envolva a resposta inteira numa cerca de código.
"""

HEADER = """> {marker} — rascunho escrito por {engine} em {today} a partir do mapa.
> Revise: o modelo leu a arquitetura, não a sua intenção. Apague este parágrafo e o
> arquivo passa a ser seu — o croqui nunca mais o reescreve.

"""


def _is_draft(text: str) -> bool:
    """Whether this file is still croqui's to rewrite."""
    head = text[:1200]
    # The seed written before this command existed had no marker, only the sentence.
    return DRAFT_MARKER in head or "Rascunho criado pelo croqui" in head


def _existing(root: Path, name: str) -> tuple[Path, str | None]:
    """The target file and its current contents, if any."""
    target = target_dir(root, "prd") / name
    if not target.is_file():
        return target, None
    try:
        return target, target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return target, None


def draft(root: Path, graph_path: Path, *, engine: str | None = None, name: str = PRD_FILE,
          about: str = "", force: bool = False, dry_run: bool = False,
          say=print, today: datetime.date | None = None) -> int:
    """Write the PRD. Returns a process exit code."""
    import json

    today = today or datetime.date.today()
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        say(f"não deu para ler {graph_path}: {exc}")
        return 2

    target, current = _existing(root, name)
    rel = target.relative_to(root) if target.is_relative_to(root) else target
    if current is not None and not _is_draft(current) and not force:
        say(f"`{rel}` foi escrito por uma pessoa — não vou por em cima dele.")
        say("  · outro nome:  croqui prd --name 02-visao-do-modelo.md")
        say("  · por cima:    croqui prd --force")
        return 1

    try:
        chosen = llm.resolve(engine)
    except llm.LLMError as exc:
        say(str(exc))
        return 2

    digest = render(build(root, graph))
    project = (graph.get("project") or {}).get("name") or root.name
    steer = f"O que a pessoa quer que o PRD enfatize: {about.strip()}\n\n" if about.strip() else ""
    prompt = PROMPT.format(context=digest, name=project, steer=steer)

    if dry_run:
        say(f"engine: {chosen.how}")
        say(f"alvo:   {rel}" + ("  (substitui o rascunho do croqui)" if current else "  (novo)"))
        say(f"prompt: {len(prompt)} caracteres, ~{len(prompt) // 4} tokens")
        return 0

    say(f"escrevendo o PRD com {chosen.how}…")
    try:
        answer = chosen.complete(prompt, max_tokens=MAX_TOKENS)
    except llm.LLMError as exc:
        say(f"falhou: {exc}")
        return 1

    # A ```markdown wrapper is packaging; a ```mermaid block inside the PRD is
    # content the tab draws, so only the outer one comes off.
    body = llm.strip_fence(answer, keep="mermaid")
    if not body.strip():
        say("o modelo respondeu vazio — nada foi escrito.")
        return 1
    if not body.lstrip().startswith("#"):
        body = f"# PRD — {project}\n\n{body}"

    header = HEADER.format(marker=DRAFT_MARKER, engine=chosen.how, today=today.isoformat())
    text = header + body.strip() + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        say(f"não deu para escrever {rel}: {exc.strerror or exc}")
        return 1

    words = len(body.split())
    say(f"✓ {rel} — {words} palavras")
    others = [d["name"] for d in collect_section(root, "prd") if d["name"] != str(rel)]
    if others:
        say(f"  a aba PRD agora tem {len(others) + 1} documentos")
    say("  abra com `croqui serve` — a aba relê o disco a cada reload")
    return 0
