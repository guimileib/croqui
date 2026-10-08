"""Find and call whatever LLM this machine already has, with zero dependencies.

``--engine auto`` tries, in order: an agent CLI on PATH (``claude``, ``codex``,
``gemini``, ``cursor-agent``, ``opencode``, ``llm``, ``ollama``), an
OpenAI-compatible HTTP endpoint from the environment, then the Anthropic SDK.
``CROQUI_LLM_CMD`` overrides them all: any command that takes the prompt as its
last argument and prints the answer.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

TIMEOUT = 600
DEFAULT_MAX_TOKENS = 8000

# The Anthropic SDK path picks its own model; the others are told by the user.
ANTHROPIC_MODEL = "claude-opus-5"

# Agent CLIs that take a prompt non-interactively and print the answer. Order is
# preference: the ones that read a whole repo well come first.
#
# Only flags I am confident about are listed. Anything else — a CLI I have never
# seen, a newer one, your own wrapper — goes through CROQUI_LLM_CMD, which is why
# this table not being exhaustive is not a limitation.
CLI_ENGINES = (
    ("claude", ["-p"], "Claude Code"),
    ("codex", ["exec"], "OpenAI Codex CLI"),
    ("gemini", ["-p"], "Gemini CLI"),
    ("cursor-agent", ["-p"], "Cursor Agent"),
    ("opencode", ["run"], "opencode"),
    ("llm", [], "llm (simonw) — whatever provider it is configured for"),
)

# Environment that names an OpenAI-compatible endpoint, most explicit first.
HTTP_KEYS = ("CROQUI_LLM_API_KEY", "OPENAI_API_KEY")
HTTP_BASE = "CROQUI_LLM_BASE_URL"
HTTP_MODEL = "CROQUI_LLM_MODEL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class LLMError(RuntimeError):
    """Something went wrong talking to the model. The message is for a human."""


@dataclass
class Engine:
    """One way to ask a model a question, and how to describe it to the user."""

    name: str           # what to pass to --engine
    kind: str           # "cli" | "http" | "anthropic"
    how: str            # one line, printed before anything runs
    argv: list | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None

    def complete(self, prompt: str, *, max_tokens: int = DEFAULT_MAX_TOKENS,
                 schema: dict | None = None) -> str:
        if self.kind == "cli":
            return _run_cli(self, prompt)
        if self.kind == "http":
            return _run_http(self, prompt, max_tokens)
        return _run_anthropic(self, prompt, max_tokens, schema)


# ------------------------------------------------------------------- discovery


def _custom() -> Engine | None:
    """``CROQUI_LLM_CMD`` — the user's own command, prompt appended last."""
    raw = (os.environ.get("CROQUI_LLM_CMD") or "").strip()
    if not raw:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise LLMError(f"CROQUI_LLM_CMD não é um comando válido: {exc}") from exc
    if not argv:
        return None
    return Engine(name="cmd", kind="cli", argv=argv,
                  how=f"CROQUI_LLM_CMD (`{os.path.basename(argv[0])}`)")


def _cli_engines() -> list[Engine]:
    found = []
    for binary, flags, label in CLI_ENGINES:
        path = shutil.which(binary)
        if not path:
            continue
        found.append(Engine(name=binary, kind="cli", argv=[path] + list(flags),
                            how=f"{label} (`{binary}`, já autenticado)"))
    ollama = shutil.which("ollama")
    model = os.environ.get(HTTP_MODEL)
    if ollama and model:
        # `ollama run` needs to be told which model; without one there is nothing
        # sensible to pick, so it only shows up once CROQUI_LLM_MODEL says.
        found.append(Engine(name="ollama", kind="cli", argv=[ollama, "run", model],
                            how=f"ollama, modelo {model}"))
    return found


def _http_engine() -> Engine | None:
    key = next((os.environ[k] for k in HTTP_KEYS if os.environ.get(k)), None)
    base = (os.environ.get(HTTP_BASE) or "").rstrip("/")
    if not base and key:
        base = DEFAULT_BASE_URL
    if not base:
        return None
    model = os.environ.get(HTTP_MODEL)
    if not model:
        # A base URL with no model is half an answer; saying so beats a 400 later.
        return None
    where = base.split("//")[-1].split("/")[0]
    return Engine(name="http", kind="http", base_url=base, api_key=key, model=model,
                  how=f"API compatível com OpenAI em {where}, modelo {model}")


def _anthropic_engine() -> Engine | None:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return Engine(name="anthropic", kind="anthropic", model=ANTHROPIC_MODEL,
                  how=f"SDK da Anthropic, modelo {ANTHROPIC_MODEL}")


def available() -> list[Engine]:
    """Every engine this machine can actually use, best first."""
    found: list[Engine] = []
    custom = _custom()
    if custom:
        found.append(custom)
    found.extend(_cli_engines())
    http = _http_engine()
    if http:
        found.append(http)
    anthropic = _anthropic_engine()
    if anthropic:
        found.append(anthropic)
    return found


# Names kept working from before this module existed.
ALIASES = {"claude-code": "claude", "api": "anthropic"}


def resolve(name: str | None) -> Engine:
    """The engine the user asked for, or the best one there is."""
    name = ALIASES.get(name or "auto", name or "auto")
    found = available()
    if not found:
        raise LLMError(_nothing_available())
    if name == "auto":
        return found[0]
    for engine in found:
        if engine.name == name:
            return engine
    have = ", ".join(e.name for e in found) or "nenhuma"
    raise LLMError(f"engine `{name}` não está disponível nesta máquina. Disponíveis: {have}.")


def _nothing_available() -> str:
    return (
        "Nenhum modelo conectado nesta máquina. Qualquer um destes resolve:\n"
        "  · uma CLI de agente no PATH — claude, codex, gemini, cursor-agent, opencode, llm\n"
        "  · um endpoint compatível com OpenAI:\n"
        f"      export {HTTP_BASE}=https://api.openai.com/v1   # ou groq, openrouter, ollama/v1…\n"
        f"      export {HTTP_KEYS[0]}=...\n"
        f"      export {HTTP_MODEL}=...\n"
        "  · qualquer outro comando seu:\n"
        '      export CROQUI_LLM_CMD="minha-cli --prompt"   # o prompt vai como último argumento'
    )


# --------------------------------------------------------------------- calling


def _run_cli(engine: Engine, prompt: str) -> str:
    argv = list(engine.argv or []) + [prompt]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError as exc:
        raise LLMError(f"`{argv[0]}` sumiu do PATH entre a checagem e a chamada.") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"`{argv[0]}` passou de {TIMEOUT}s sem responder.") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise LLMError(f"`{argv[0]}` saiu com {proc.returncode}: {detail}")
    if not proc.stdout.strip():
        raise LLMError(f"`{argv[0]}` respondeu vazio.")
    return proc.stdout


def _run_http(engine: Engine, prompt: str, max_tokens: int) -> str:
    body = json.dumps({
        "model": engine.model,
        "max_tokens": max_tokens,
        # Some OpenAI-compatible servers want the newer name and ignore the old
        # one; sending both is harmless and saves a round of "why is it truncated".
        "max_completion_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if engine.api_key:
        headers["Authorization"] = f"Bearer {engine.api_key}"
    request = urllib.request.Request(
        f"{engine.base_url}/chat/completions", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        raise LLMError(f"{engine.base_url} respondeu {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"não deu para falar com {engine.base_url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"{engine.base_url} respondeu algo que não é JSON.") from exc
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"resposta em formato inesperado de {engine.base_url}.") from exc


def _run_anthropic(engine: Engine, prompt: str, max_tokens: int, schema: dict | None) -> str:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the user's env
        raise LLMError('SDK anthropic não instalado — `pip install "croqui[api]"`') from exc

    config: dict = {"effort": "low"}
    if schema:
        config["format"] = {"type": "json_schema", "schema": schema}
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=engine.model or ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        output_config=config,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise LLMError("o modelo recusou este pedido")
    return next((b.text for b in response.content if b.type == "text"), "")


# ---------------------------------------------------------------------- output


def parse_json(raw: str) -> dict:
    """Pull the JSON out of an answer that may be wrapped in prose or a fence."""
    raw = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def strip_fence(raw: str, *, keep: str = "") -> str:
    """Unwrap an answer wrapped in a code fence, including an unclosed (truncated) one.

    Fence languages in ``keep`` (e.g. ``mermaid``) are document content and stay.
    """
    text = (raw or "").strip()
    match = re.match(r"^```([a-zA-Z]*)[ \t]*\n([\s\S]*?)\n?```\s*$", text)
    if match:
        return text if match.group(1).lower() == keep.lower() else match.group(2).strip()
    opening = re.match(r"^```([a-zA-Z]*)[ \t]*\n([\s\S]*)$", text)
    if opening and opening.group(1).lower() != keep.lower() and "```" not in opening.group(2):
        return opening.group(2).strip()
    return text
