"""``writ doctor`` and the ``--offline`` guarantee.

Two things are being pinned here. The doctor must size its recommendations from
real VRAM, because the usual way someone concludes "local doesn't work" is
pulling a model that does not fit. And ``--offline`` must be a guarantee, not a
preference: it fails rather than quietly reaching the network.
"""

from __future__ import annotations

import pytest

from writ.__main__ import _planner, build_parser, main
from writ.doctor import MODELS, Report, recommend, render

# -- model sizing ----------------------------------------------------------


def test_recommends_only_what_fits_8gb():
    """The maintainer's card. qwen3:8b fits; qwen3:14b does not."""
    names = [name for name, _, _ in recommend(8.0)]
    assert "qwen3:8b" in names
    assert "llama3.1:8b" in names
    assert "qwen3:14b" not in names
    assert "qwen3:32b" not in names


def test_reserves_headroom_for_the_kv_cache():
    """A model exactly the size of VRAM does not fit -- the cache needs room."""
    assert "qwen3:8b" not in [n for n, _, _ in recommend(5.0)]
    assert "qwen3:8b" in [n for n, _, _ in recommend(6.0)]


def test_recommendations_are_largest_first():
    sizes = [size for _, size, _ in recommend(24.0)]
    assert sizes == sorted(sizes, reverse=True)


def test_a_tiny_card_still_gets_an_option():
    assert [n for n, _, _ in recommend(6.0)]


def test_no_gpu_recommends_nothing():
    assert recommend(0.0) == []


def test_every_model_entry_is_well_formed():
    for name, size, note in MODELS:
        assert ":" in name
        assert 0 < size < 100
        assert note


# -- the verdict -----------------------------------------------------------


def test_offline_needs_a_server_and_a_model():
    assert not Report(server_up=False, installed_models=[]).can_run_offline
    assert not Report(server_up=True, installed_models=[]).can_run_offline
    assert Report(server_up=True, installed_models=["qwen3:8b"]).can_run_offline


def test_blockers_name_the_missing_runner():
    blockers = Report(runner="", server_up=False).blockers()
    assert any("Ollama" in b for b in blockers)


def test_blockers_distinguish_installed_from_running():
    blockers = Report(runner="ollama", server_up=False).blockers()
    assert any("ollama serve" in b for b in blockers)
    assert not any("Install Ollama" in b for b in blockers)


def test_blockers_suggest_a_model_that_fits():
    report = Report(runner="ollama", server_up=True, installed_models=[], gpus=[("card", 8.0)])
    blockers = report.blockers()
    assert any("ollama pull qwen3:8b" in b for b in blockers)


def test_blockers_warn_about_disk_because_checkpointing_needs_it():
    blockers = Report(
        runner="ollama", server_up=True, installed_models=["x"], free_disk_gb=5.0
    ).blockers()
    assert any("checkpointing needs headroom" in b for b in blockers)


def test_render_states_the_verdict_plainly():
    ready = render(Report(server_up=True, installed_models=["qwen3:8b"]))
    assert "FULLY OFFLINE: YES" in ready
    assert "FULLY OFFLINE: NOT YET" in render(Report())


def test_render_marks_models_already_pulled():
    text = render(Report(gpus=[("card", 8.0)], server_up=True, installed_models=["qwen3:8b"]))
    assert "[installed]" in text


def test_render_warns_against_airllm():
    """Cheap to say once, and it saves someone an hour-long five-step task."""
    text = render(Report())
    assert "AirLLM" in text
    assert "0.5-2 tok/s" in text


def test_render_offers_the_big_slow_path():
    text = render(Report())
    assert "llama-server" in text
    assert "--n-gpu-layers" in text


def test_doctor_output_survives_a_windows_console():
    render(Report(gpus=[("card", 8.0)], server_up=True, installed_models=["a"])).encode("cp1252")


def test_doctor_command_exit_code_reflects_readiness(monkeypatch, capsys):
    import writ.doctor as doctor_module

    monkeypatch.setattr(
        doctor_module,
        "diagnose",
        lambda url: Report(server_up=True, installed_models=["qwen3:8b"]),
    )
    assert main(["doctor"]) == 0
    assert "FULLY OFFLINE: YES" in capsys.readouterr().out

    monkeypatch.setattr(doctor_module, "diagnose", lambda url: Report())
    assert main(["doctor"]) == 1


# -- the --offline guarantee ----------------------------------------------


def test_offline_refuses_an_explicit_remote_planner():
    args = build_parser().parse_args(["run", "g", "--planner", "claude", "--offline"])
    with pytest.raises(ImportError, match="would call a remote API"):
        _planner(args, ("fs.read",))


def test_offline_refuses_to_fall_back_when_no_server(monkeypatch):
    """The whole point: it fails rather than quietly reaching the network."""
    import writ.planner.local as local_module

    monkeypatch.setattr(local_module, "server_available", lambda url, timeout=1.5: False)
    args = build_parser().parse_args(["run", "g", "--offline"])
    with pytest.raises(ImportError, match="no local server is reachable"):
        _planner(args, ("fs.read",))


def test_offline_points_at_the_doctor(monkeypatch):
    import writ.planner.local as local_module

    monkeypatch.setattr(local_module, "server_available", lambda url, timeout=1.5: False)
    args = build_parser().parse_args(["run", "g", "--offline"])
    with pytest.raises(ImportError, match="writ doctor"):
        _planner(args, ("fs.read",))


def test_offline_is_satisfied_by_a_running_server(monkeypatch):
    import writ.planner.local as local_module
    from writ.planner.local import LocalPlanner

    monkeypatch.setattr(local_module, "server_available", lambda url, timeout=1.5: True)
    args = build_parser().parse_args(["run", "g", "--offline"])
    assert isinstance(_planner(args, ("fs.read",)), LocalPlanner)


def test_without_offline_the_fallback_is_allowed(monkeypatch):
    import writ.planner.local as local_module

    monkeypatch.setattr(local_module, "server_available", lambda url, timeout=1.5: False)
    args = build_parser().parse_args(["run", "g"])
    # Falls through to the Claude planner rather than raising. Constructing it
    # needs the SDK, which may or may not be installed; either outcome proves
    # the offline guard did not fire.
    try:
        _planner(args, ("fs.read",))
    except ImportError as exc:
        assert "offline" not in str(exc)


def test_a_server_with_no_models_does_not_crash(monkeypatch):
    """Ollama sends {"data": null} before anything is pulled.

    `.get(key, default)` returns the stored None, not the default -- found by
    pointing the doctor at a freshly installed server.
    """
    import writ.doctor as doctor_module

    class _Response:
        status = 200

        def read(self) -> bytes:
            return b'{"object": "list", "data": null}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(doctor_module.urllib.request, "urlopen", lambda *a, **k: _Response())
    assert doctor_module._installed_models("http://localhost:11434/v1") == []


def test_a_serving_runtime_is_not_reported_as_missing():
    """A responding server settles it. Found on a freshly installed machine:
    Ollama lands outside an already-open shell's PATH, so `which` fails while
    the service is demonstrably answering."""
    blockers = Report(runner="", server_up=True, installed_models=["qwen3:8b"]).blockers()
    assert not any("No local runner found" in b for b in blockers)


def test_a_missing_runner_is_still_reported_when_nothing_serves():
    blockers = Report(runner="", server_up=False).blockers()
    assert any("No local runner found" in b for b in blockers)


def test_runner_is_found_outside_path(monkeypatch, tmp_path):
    import writ.doctor as doctor_module

    programs = tmp_path / "Programs" / "Ollama"
    programs.mkdir(parents=True)
    (programs / "ollama.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert doctor_module._find_runner() == "ollama"
