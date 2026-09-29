"""Where the model lives: on this machine, or behind someone's API.

Every provider here speaks OpenAI's ``/v1/chat/completions`` with tool calling,
so one planner (:class:`~minos.planner.local.LocalPlanner`) serves all of them.
What differs is small and is all in this table: the URL, which environment
variable holds the key, a model to start from, and whether the traffic leaves
the machine.

That last field is the one that matters. A hosted provider is a remote model,
and two policies follow from it without anyone having to remember them:

* ``--offline`` refuses it, the same way it refuses ``--planner claude``.
* Code it writes is treated as ``remote-planner`` code and runs in a container
  (see SECURITY.md). The task is yours; the context that produced the script is
  not one you can see.

Keys are read from the environment and never printed. The default models are
starting points, not recommendations -- catalogues change faster than this
file, so ``--model`` or ``MINOS_HOSTED_MODEL`` always wins.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .local import LocalPlanner

__all__ = [
    "PROVIDERS",
    "MissingKey",
    "Provider",
    "ResolvedProvider",
    "build_planner",
    "is_loopback",
    "key_status",
    "resolve",
]


class MissingKey(LookupError):
    """A hosted provider was chosen and no key for it is set."""


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    base_url: str
    key_env: str = ""
    """Where the key is read from. Empty means the server wants none."""

    default_model: str = ""
    local: bool = False
    """True only when the server runs on this machine."""

    label: str = ""
    """How error messages name it: "OpenRouter", "the local server"."""

    headers: tuple[tuple[str, str], ...] = ()
    """Sent with every request. Never a secret -- those come from ``key_env``."""

    docs: str = ""


PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in (
        # -- on this machine -------------------------------------------------
        Provider(
            "ollama",
            "http://localhost:11434/v1",
            default_model="qwen3:8b",
            local=True,
            label="the local server",
            docs="https://ollama.com",
        ),
        Provider(
            "lmstudio",
            "http://localhost:1234/v1",
            local=True,
            label="LM Studio",
            docs="https://lmstudio.ai",
        ),
        Provider(
            "llamacpp",
            "http://localhost:8080/v1",
            local=True,
            label="llama.cpp server",
            docs="https://github.com/ggml-org/llama.cpp",
        ),
        Provider(
            "vllm",
            "http://localhost:8000/v1",
            local=True,
            label="vLLM",
            docs="https://docs.vllm.ai",
        ),
        # -- hosted ----------------------------------------------------------
        Provider(
            "openrouter",
            "https://openrouter.ai/api/v1",
            key_env="OPENROUTER_API_KEY",
            default_model="anthropic/claude-sonnet-4.5",
            label="OpenRouter",
            # Optional attribution headers OpenRouter documents. Not secrets.
            headers=(
                ("HTTP-Referer", "https://github.com/mks2122/minos"),
                ("X-Title", "minos"),
            ),
            docs="https://openrouter.ai/models",
        ),
        Provider(
            "openai",
            "https://api.openai.com/v1",
            key_env="OPENAI_API_KEY",
            default_model="gpt-4.1-mini",
            label="OpenAI",
            docs="https://platform.openai.com/docs/models",
        ),
        Provider(
            "groq",
            "https://api.groq.com/openai/v1",
            key_env="GROQ_API_KEY",
            default_model="llama-3.3-70b-versatile",
            label="Groq",
            docs="https://console.groq.com/docs/models",
        ),
        Provider(
            "together",
            "https://api.together.xyz/v1",
            key_env="TOGETHER_API_KEY",
            default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            label="Together",
            docs="https://docs.together.ai/docs/serverless-models",
        ),
        Provider(
            "deepseek",
            "https://api.deepseek.com/v1",
            key_env="DEEPSEEK_API_KEY",
            default_model="deepseek-chat",
            label="DeepSeek",
            docs="https://api-docs.deepseek.com",
        ),
        Provider(
            "mistral",
            "https://api.mistral.ai/v1",
            key_env="MISTRAL_API_KEY",
            default_model="mistral-large-latest",
            label="Mistral",
            docs="https://docs.mistral.ai/getting-started/models/",
        ),
        Provider(
            "fireworks",
            "https://api.fireworks.ai/inference/v1",
            key_env="FIREWORKS_API_KEY",
            default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
            label="Fireworks",
            docs="https://fireworks.ai/models",
        ),
        Provider(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            key_env="GEMINI_API_KEY",
            default_model="gemini-2.5-flash",
            label="Gemini",
            docs="https://ai.google.dev/gemini-api/docs/openai",
        ),
        Provider(
            "xai",
            "https://api.x.ai/v1",
            key_env="XAI_API_KEY",
            default_model="grok-4",
            label="xAI",
            docs="https://docs.x.ai/docs/models",
        ),
        Provider(
            "cerebras",
            "https://api.cerebras.ai/v1",
            key_env="CEREBRAS_API_KEY",
            default_model="llama-3.3-70b",
            label="Cerebras",
            docs="https://inference-docs.cerebras.ai",
        ),
    )
}
"""Named presets. ``custom`` is not here: it is whatever MINOS_BASE_URL says."""


def is_loopback(url: str) -> bool:
    """Does this URL point at this machine?

    Decided from the host alone. A LAN box is not local for ``--offline``'s
    purposes: the promise is that nothing leaves the machine.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in {"localhost", "::1"} or host.startswith("127.")


@dataclass(frozen=True, slots=True)
class ResolvedProvider:
    """A provider with its URL, model and key decided. ``repr`` omits the key."""

    name: str
    base_url: str
    model: str
    local: bool
    label: str
    key_env: str = ""
    api_key: str = field(default="", repr=False)
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def hosted(self) -> bool:
        return not self.local


def resolve(
    name: str,
    *,
    model: str = "",
    base_url: str = "",
    environ: dict[str, str] | None = None,
    require_key: bool = True,
) -> ResolvedProvider:
    """Turn a provider name into something a planner can be built from.

    ``custom`` takes the URL from ``base_url`` (MINOS_BASE_URL) and the key from
    ``MINOS_API_KEY``, and is local only if that URL is loopback. A named preset
    reads its own key variable, falling back to ``MINOS_API_KEY``.
    """
    env = os.environ if environ is None else environ

    if name == "custom":
        if not base_url:
            raise ValueError("--planner custom needs MINOS_BASE_URL (or --base-url)")
        local = is_loopback(base_url)
        key = env.get("MINOS_API_KEY", "")
        if require_key and not local and not key:
            raise MissingKey(
                f"{base_url} is not on this machine, so it almost certainly wants a key. "
                "Set MINOS_API_KEY in .env."
            )
        if not model:
            raise ValueError("--planner custom needs --model (or MINOS_HOSTED_MODEL)")
        return ResolvedProvider(
            name="custom",
            base_url=base_url,
            model=model,
            local=local,
            label="the local server" if local else base_url,
            key_env="MINOS_API_KEY",
            api_key=key,
        )

    preset = PROVIDERS.get(name)
    if preset is None:
        known = ", ".join(sorted([*PROVIDERS, "custom"]))
        raise ValueError(f"unknown provider {name!r}; known: {known}")

    key = ""
    if preset.key_env:
        key = env.get(preset.key_env, "") or env.get("MINOS_API_KEY", "")
        if require_key and not key:
            raise MissingKey(
                f"{preset.label} needs an API key. Put {preset.key_env}=... in .env "
                f"(get one at {preset.docs})."
            )

    chosen = model or preset.default_model
    if not chosen:
        raise ValueError(f"{preset.label} has no default model; pass --model with one it serves")

    return ResolvedProvider(
        name=preset.name,
        base_url=base_url or preset.base_url,
        model=chosen,
        local=preset.local,
        label=preset.label or preset.name,
        key_env=preset.key_env,
        api_key=key or "not-needed",
        headers=preset.headers,
    )


def build_planner(
    provider: ResolvedProvider,
    operations: tuple[str, ...],
    *,
    context_tokens: int = 16384,
    timeout: float = 600.0,
    thinking: bool = True,
) -> LocalPlanner:
    """The OpenAI-compatible planner, configured for this provider."""
    from . import local

    return local.LocalPlanner(
        operations=operations,
        base_url=provider.base_url,
        model=provider.model,
        api_key=provider.api_key or "not-needed",
        label=provider.label,
        local_server=provider.local,
        headers=provider.headers,
        context_tokens=context_tokens,
        timeout=timeout,
        thinking=thinking,
        max_tokens=0 if provider.local else 4096,
    )


def key_status(environ: dict[str, str] | None = None) -> list[tuple[Provider, bool]]:
    """Each preset, and whether its key is set. Never the key itself."""
    env = os.environ if environ is None else environ
    return [
        (p, (not p.key_env) or bool(env.get(p.key_env) or env.get("MINOS_API_KEY")))
        for p in PROVIDERS.values()
    ]
