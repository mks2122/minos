"""Hosted and local OpenAI-compatible providers.

What these pin down: a preset resolves to the right URL and key, the key never
appears in a repr or the doctor output, a hosted request carries none of the
Ollama-only fields, ``--offline`` refuses anything that leaves the machine, and
code a hosted model writes is not trusted with the subprocess jail.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from minos.__main__ import _code_origin, _planner, build_parser, main
from minos.config import settings
from minos.planner.local import LocalPlanner
from minos.planner.providers import (
    PROVIDERS,
    MissingKey,
    build_planner,
    is_loopback,
    key_status,
    resolve,
)

SECRET = "sk-or-v1-this-must-never-be-printed"


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """A developer's real keys must not make these tests pass or fail."""
    for provider in PROVIDERS.values():
        if provider.key_env:
            monkeypatch.delenv(provider.key_env, raising=False)
    for name in ("MINOS_API_KEY", "MINOS_HOSTED_MODEL", "MINOS_SANDBOX_ORIGIN"):
        monkeypatch.delenv(name, raising=False)


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    class Response:
        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "function": {
                                            "name": "finish",
                                            "arguments": json.dumps(
                                                {"summary": "done", "succeeded": True}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


# -- resolution --------------------------------------------------------------


def test_every_hosted_preset_names_a_key_and_is_not_loopback():
    for provider in PROVIDERS.values():
        assert provider.local == is_loopback(provider.base_url), provider.name
        if not provider.local:
            assert provider.key_env, provider.name
            assert provider.base_url.startswith("https://"), provider.name


def test_openrouter_resolves_from_its_own_key():
    resolved = resolve("openrouter", environ={"OPENROUTER_API_KEY": SECRET})
    assert resolved.base_url == "https://openrouter.ai/api/v1"
    assert resolved.api_key == SECRET
    assert resolved.hosted
    assert resolved.model == PROVIDERS["openrouter"].default_model


def test_a_model_given_beats_the_preset_default():
    resolved = resolve("openai", model="gpt-5", environ={"OPENAI_API_KEY": "k"})
    assert resolved.model == "gpt-5"


def test_minos_api_key_is_the_fallback():
    assert resolve("groq", environ={"MINOS_API_KEY": "k"}).api_key == "k"


def test_a_hosted_provider_without_a_key_says_which_variable():
    with pytest.raises(MissingKey, match="OPENROUTER_API_KEY"):
        resolve("openrouter", environ={})


def test_a_local_preset_needs_no_key():
    resolved = resolve("ollama", environ={})
    assert resolved.local
    assert resolved.base_url == "http://localhost:11434/v1"


def test_custom_is_local_only_on_loopback():
    local = resolve("custom", base_url="http://127.0.0.1:9000/v1", model="m", environ={})
    assert local.local
    with pytest.raises(MissingKey):
        resolve("custom", base_url="https://llm.example.com/v1", model="m", environ={})
    hosted = resolve(
        "custom", base_url="https://llm.example.com/v1", model="m", environ={"MINOS_API_KEY": "k"}
    )
    assert hosted.hosted


def test_a_lan_box_is_not_local():
    """--offline promises nothing leaves the machine; a LAN server is elsewhere."""
    assert not is_loopback("http://192.168.1.20:11434/v1")
    assert is_loopback("http://localhost:11434/v1")
    assert is_loopback("http://[::1]:8000/v1")


def test_an_unknown_provider_lists_the_known_ones():
    with pytest.raises(ValueError, match="openrouter"):
        resolve("nope", environ={})


# -- the key stays secret ----------------------------------------------------


def test_the_key_is_not_in_any_repr():
    resolved = resolve("openrouter", environ={"OPENROUTER_API_KEY": SECRET})
    planner = build_planner(resolved, ("fs.read",))
    assert SECRET not in repr(resolved)
    assert SECRET not in repr(planner)


def test_describe_says_a_key_is_set_without_printing_it(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    lines = "\n".join(settings(dotenv=tmp_path / "absent.env").describe())
    assert "openrouter" in lines
    assert SECRET not in lines


def test_key_status_reports_presence_only(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    status = {p.name: ready for p, ready in key_status()}
    assert status["groq"] is True
    assert status["openai"] is False
    assert status["ollama"] is True


def test_providers_command_never_prints_a_key(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    assert main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY set" in out
    assert SECRET not in out


# -- what goes over the wire -------------------------------------------------


def test_a_hosted_request_carries_no_ollama_fields(monkeypatch):
    """OpenAI rejects unknown parameters; num_ctx and chat_template_kwargs are Ollama's."""
    captured = _capture(monkeypatch)
    resolved = resolve("openai", environ={"OPENAI_API_KEY": SECRET})
    planner = build_planner(resolved, ("fs.read",), thinking=False)
    planner.next_action("goal", [], _scopes())

    body = captured["body"]
    assert "options" not in body
    assert "chat_template_kwargs" not in body
    assert body["max_tokens"] > 0
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == f"Bearer {SECRET}"


def test_a_hosted_model_is_not_told_it_is_a_small_model(monkeypatch):
    captured = _capture(monkeypatch)
    build_planner(resolve("openai", environ={"OPENAI_API_KEY": "k"}), ("fs.read",)).next_action(
        "goal", [], _scopes()
    )
    assert "smaller model" not in captured["body"]["messages"][0]["content"]


def test_a_local_request_still_asks_for_its_context(monkeypatch):
    captured = _capture(monkeypatch)
    build_planner(resolve("ollama", environ={}), ("fs.read",), context_tokens=8192).next_action(
        "goal", [], _scopes()
    )
    assert captured["body"]["options"]["num_ctx"] == 8192
    assert "max_tokens" not in captured["body"]


def test_openrouter_sends_its_attribution_headers(monkeypatch):
    captured = _capture(monkeypatch)
    planner = build_planner(resolve("openrouter", environ={"OPENROUTER_API_KEY": "k"}), ())
    planner.next_action("goal", [], _scopes())
    assert captured["headers"]["x-title"] == "minos"


def test_a_refused_key_is_named_as_the_key(monkeypatch):
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":{"message":"bad key"}}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    planner = build_planner(resolve("openrouter", environ={"OPENROUTER_API_KEY": "k"}), ())
    done = planner.next_action("goal", [], _scopes())
    assert not done.succeeded
    assert "OpenRouter refused the API key" in done.summary


def test_out_of_credit_is_said_plainly(monkeypatch):
    def broke(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(urllib.request, "urlopen", broke)
    planner = build_planner(resolve("openrouter", environ={"OPENROUTER_API_KEY": "k"}), ())
    assert "out of credit" in planner.next_action("goal", [], _scopes()).summary


# -- the CLI -----------------------------------------------------------------


def test_run_accepts_a_provider_as_the_planner(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    args = build_parser().parse_args(
        ["run", "g", "--planner", "openrouter", "--model", "openai/gpt-4.1"]
    )
    planner = _planner(args, ("fs.read",))
    assert isinstance(planner, LocalPlanner)
    assert planner.base_url == "https://openrouter.ai/api/v1"
    assert planner.model == "openai/gpt-4.1"
    assert not planner.local_server


def test_run_without_the_key_explains_itself(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here to supply a key
    code = main(["run", "g", "-w", str(tmp_path), "--planner", "groq", "--state", str(tmp_path)])
    assert code == 2
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_offline_refuses_a_hosted_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    args = build_parser().parse_args(["run", "g", "--planner", "openrouter", "--offline"])
    with pytest.raises(ImportError, match="not on this machine"):
        _planner(args, ("fs.read",))


def test_offline_allows_a_local_preset():
    args = build_parser().parse_args(
        ["run", "g", "--planner", "lmstudio", "--model", "m", "--offline"]
    )
    assert _planner(args, ("fs.read",)).base_url == "http://localhost:1234/v1"


def test_code_from_a_hosted_model_goes_to_the_container(monkeypatch):
    """The origin policy in SECURITY.md: remote-planner code is not trusted with the jail."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    args = build_parser().parse_args(["run", "g", "--planner", "openai"])
    _planner(args, ("fs.read",))
    assert _code_origin(args) == "remote-planner"


def test_code_from_claude_goes_to_the_container_too():
    args = build_parser().parse_args(["run", "g", "--planner", "claude"])
    try:
        _planner(args, ("fs.read",))
    except ImportError:
        args.planner_hosted = True  # SDK not installed here; the decision is what matters
    assert _code_origin(args) == "remote-planner"


def test_code_from_a_local_model_stays_in_the_jail():
    args = build_parser().parse_args(["run", "g", "--planner", "local"])
    _planner(args, ("fs.read",))
    assert _code_origin(args) == "local-planner"


def test_a_typed_code_origin_still_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    args = build_parser().parse_args(
        ["run", "g", "--planner", "openai", "--code-origin", "local-planner"]
    )
    _planner(args, ("fs.read",))
    assert _code_origin(args) == "local-planner"


def test_eval_accepts_a_provider_and_needs_its_key(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["eval", "--planner", "mistral"]) == 2
    assert "MISTRAL_API_KEY" in capsys.readouterr().err


def _scopes():
    from minos.scopes import ScopeSet

    return ScopeSet.parse(["fs.read:/tmp/**"])
