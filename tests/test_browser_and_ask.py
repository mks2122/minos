"""Opening web pages, asking the person, and which GUI inputs stop for approval.

What is worth pinning: a browser profile is asked for once per site and then
remembered -- but only a person's answer is remembered; only http(s) opens; a
commit control (Post, Send...) always stops while ordinary input does not; and
a policy approval is never written to the audit log as a human's.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from conftest import make_invocation
from minos.approval import SessionApprover, is_commit, terminal_ask
from minos.audit import AuditLog
from minos.broker import Broker
from minos.checkpoint import FileCheckpointStore
from minos.memory import MemoryStore
from minos.planner.claude import system_prompt
from minos.planner.schemas import operation_for_tool, tool_definitions
from minos.router import Router
from minos.scopes import ScopeSet
from minos.tiers.base import OperationUnsupported
from minos.tiers.l1_system import BrowserAdapter, UserAdapter
from minos.tiers.l1_system.browser import (
    BROWSERS,
    BrowserProfile,
    installed_profiles,
    site_of,
)
from minos.types import ActionRequest, AdmissionDecision, EffectClass

PROMPT = AdmissionDecision(verdict="prompt", rationale="irreversible")
CHROME = next(b for b in BROWSERS if b.key == "chrome")
EDGE = next(b for b in BROWSERS if b.key == "edge")
FIREFOX = next(b for b in BROWSERS if b.key == "firefox")


def profiles() -> list[BrowserProfile]:
    return [
        BrowserProfile(CHROME, "chrome.exe", "Default", "Karthick"),
        BrowserProfile(CHROME, "chrome.exe", "Profile 1", "qik.company", last_used=True),
        BrowserProfile(EDGE, "msedge.exe", "Default", "Profile 1"),
    ]


class Asker:
    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.questions: list[tuple[str, list[str]]] = []

    def __call__(self, question, choices):  # type: ignore[no-untyped-def]
        self.questions.append((question, list(choices)))
        return self.replies.pop(0) if self.replies else None


def adapter(memory: MemoryStore, ask: Asker | None, launched: list[list[str]]) -> BrowserAdapter:
    return BrowserAdapter(
        preferences=memory, ask=ask, profiles=profiles, launch=launched.append, settle=0
    )


def open_url(browser: BrowserAdapter, url: str, **params: str) -> dict:
    request = ActionRequest("g", "open", "browser.open", {"url": url, **params})
    prep = browser.prepare(request)
    return prep.execute(None)  # type: ignore[arg-type]


# -- browser profiles ----------------------------------------------------------


def test_the_person_is_asked_once_per_site_and_the_answer_is_remembered():
    memory, launched = MemoryStore(), []
    ask = Asker("Google Chrome: qik.company (Profile 1)")
    browser = adapter(memory, ask, launched)

    first = open_url(browser, "https://www.linkedin.com/feed/")
    again = open_url(browser, "linkedin.com")

    assert len(ask.questions) == 1
    assert "linkedin.com" in ask.questions[0][0]
    assert first["profile"] == again["profile"] == "qik.company"
    assert "remembered" in again["profile_chosen_by"]
    assert launched[-1] == ["chrome.exe", "--profile-directory=Profile 1", "https://linkedin.com"]


def test_a_different_site_is_asked_about_separately():
    memory, launched = MemoryStore(), []
    ask = Asker("1", "Microsoft Edge: Profile 1 (Default)")
    browser = adapter(memory, ask, launched)
    ask.replies = ["Google Chrome: Karthick (Default)", "Microsoft Edge: Profile 1 (Default)"]

    open_url(browser, "https://linkedin.com")
    result = open_url(browser, "https://mail.google.com")

    assert len(ask.questions) == 2
    assert result["browser"] == "Microsoft Edge"
    assert launched[-1][0] == "msedge.exe"


def test_a_profile_the_planner_names_is_used_but_not_remembered():
    memory, launched = MemoryStore(), []
    browser = adapter(memory, Asker(), launched)

    result = open_url(browser, "https://linkedin.com", profile="Karthick")

    assert result["profile"] == "Karthick"
    assert memory.preference("browser.profile:linkedin.com") is None


def test_an_ambiguous_profile_name_is_refused_rather_than_guessed():
    browser = adapter(MemoryStore(), None, [])
    # "Default" is a directory in both Chrome and Edge.
    with pytest.raises(ValueError, match="no browser profile matches"):
        open_url(browser, "https://linkedin.com", profile="Default")
    assert open_url(browser, "https://linkedin.com", profile="edge: Default")["browser"] == (
        "Microsoft Edge"
    )


def test_with_no_one_to_ask_it_falls_back_to_the_last_used_profile_and_remembers_nothing():
    memory, launched = MemoryStore(), []
    result = open_url(adapter(memory, None, launched), "https://linkedin.com")

    assert result["profile"] == "qik.company"
    assert "no one to ask" in result["profile_chosen_by"]
    assert memory.preference("browser.profile:linkedin.com") is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
        "data:text/html,hi",
        "chrome://settings",
        "https://",
    ],
)
def test_only_web_pages_open(url):
    with pytest.raises(OperationUnsupported):
        adapter(MemoryStore(), None, []).prepare(
            ActionRequest("g", "open", "browser.open", {"url": url})
        )


def test_the_grant_is_the_site_so_a_scope_can_name_one():
    prep = adapter(MemoryStore(), None, []).prepare(
        ActionRequest("g", "open", "browser.open", {"url": "https://www.linkedin.com/in/me"})
    )
    assert prep.contract.effect_class is EffectClass.PURE
    assert [(g.capability, g.subject) for g in prep.grants] == [("browser.open", "linkedin.com")]
    assert site_of("https://WWW.LinkedIn.com:443/x") == "linkedin.com"
    port = adapter(MemoryStore(), None, []).prepare(
        ActionRequest("g", "open", "browser.open", {"url": "localhost:8080/app"})
    )
    assert port.grants[0].subject == "localhost"


def test_profiles_are_read_from_each_browsers_own_records(tmp_path: Path):
    chrome = tmp_path / "chrome"
    chrome.mkdir()
    (chrome / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "last_used": "Profile 2",
                    "info_cache": {"Default": {"name": "Home"}, "Profile 2": {"name": "Work"}},
                }
            }
        ),
        encoding="utf-8",
    )
    firefox = tmp_path / "firefox"
    firefox.mkdir()
    (firefox / "profiles.ini").write_text(
        "[Profile0]\nName=default-release\nDefault=1\n[General]\nVersion=2\n", encoding="utf-8"
    )
    roots = {"chrome": chrome, "firefox": firefox}

    found = installed_profiles(
        (CHROME, EDGE, FIREFOX),
        locate=lambda spec: f"{spec.key}.exe" if spec.key in roots else None,
        data_root=lambda spec: roots[spec.key],
    )

    assert [p.label() for p in found] == [
        "Google Chrome: Home (Default)",
        "Google Chrome: Work (Profile 2)",
        "Mozilla Firefox: default-release",
    ]
    assert next(p for p in found if p.last_used).name == "Work"
    assert found[-1].command("https://x.org") == [
        "firefox.exe",
        "-P",
        "default-release",
        "-new-tab",
        "https://x.org",
    ]


def test_preferences_survive_a_reopen(tmp_path: Path):
    with MemoryStore(tmp_path / "m.db") as memory:
        memory.remember("browser.profile:linkedin.com", "chrome|Profile 1")
    with MemoryStore(tmp_path / "m.db") as memory:
        assert memory.preference("browser.profile:linkedin.com") == "chrome|Profile 1"
        memory.forget("browser.profile:linkedin.com")
        assert memory.preference("browser.profile:linkedin.com") is None


# -- asking the person ---------------------------------------------------------


def test_a_numbered_choice_or_free_text_is_the_answer():
    replies = iter(["2"])
    out = io.StringIO()
    assert terminal_ask("Which?", ["a", "b"], ask=lambda _: next(replies), out=out) == "b"
    assert "1. a" in out.getvalue()
    replies = iter(["", "something else"])
    assert terminal_ask("Which?", ["a"], ask=lambda _: next(replies), out=out) == "something else"


def test_a_question_cannot_put_escape_codes_on_the_terminal():
    out = io.StringIO()
    terminal_ask("\x1b[2Jgotcha", ["\x1b]0;x\x07"], ask=lambda _: "1", out=out)
    assert "\x1b" not in out.getvalue()


def test_no_one_to_answer_is_a_failed_step_not_an_invented_answer():
    prep = UserAdapter(ask=None).prepare(
        ActionRequest("g", "ask", "user.ask", {"question": "Which account?"})
    )
    with pytest.raises(RuntimeError, match="no one is at the terminal"):
        prep.execute(None)  # type: ignore[arg-type]


def test_ask_user_is_a_tool_and_the_prompt_explains_it():
    names = {t["name"] for t in tool_definitions(("user.ask", "browser.open"))}
    assert {"ask_user", "browser_open"} <= names
    assert operation_for_tool("ask_user") == "user.ask"
    assert "ASKING THE PERSON" in system_prompt(("user.ask",))
    assert "ASKING THE PERSON" not in system_prompt(("fs.read",))
    assert "browser_open" in system_prompt(("ui.click",))
    assert "win+r" not in system_prompt(("ui.click",))


# -- which GUI input stops -----------------------------------------------------


def gui(operation: str, **params: object):
    return make_invocation(
        operation=operation, effect_class=EffectClass.IRREVERSIBLE, params=dict(params)
    )


@pytest.mark.parametrize(
    ("invocation", "commit"),
    [
        (gui("ui.click", element="Post"), True),
        (gui("ui.click", element="Send now"), True),
        (gui("ui.click", element="Delete message"), True),
        (gui("ui.click", element="Start a post"), False),
        (gui("ui.click", element="Home"), False),
        (gui("ui.click", x=10, y=20), True),
        (gui("ui.key", chord="ctrl+enter"), True),
        (gui("ui.key", chord="enter"), False),
        (gui("ui.key", chord="ctrl+s"), False),
        (gui("ui.type", text="hi"), False),
        (gui("ui.type", text="hi\n"), True),
    ],
)
def test_only_commit_controls_are_commits(invocation, commit):
    assert is_commit(invocation) is commit


def test_under_the_commit_policy_ordinary_input_is_not_asked_and_commits_are(capsys):
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    approver = SessionApprover(ask=ask, gui="commit")
    assert approver(gui("ui.type", text="hi"), PROMPT)
    assert asked == []
    assert approver(gui("ui.click", element="Post"), PROMPT) is False
    assert len(asked) == 1


def test_the_default_policy_still_asks_for_everything(capsys):
    asked: list[str] = []
    SessionApprover(ask=lambda p: asked.append(p) or "n")(gui("ui.type", text="hi"), PROMPT)
    assert asked


def test_a_policy_approval_is_not_recorded_as_a_human_one(tmp_path: Path, capsys):
    broker = Broker(
        scopes=ScopeSet.parse(["ui.input:*"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
        approver=SessionApprover(ask=lambda _: "n", gui="commit"),
    )
    from minos.tiers.l3_gui import GuiAdapter

    routed = Router(adapters=(GuiAdapter(),)).route(
        ActionRequest("g", "type", "ui.type", {"text": "hi"})
    )
    outcome = broker.submit(routed.invocation, routed.execute)

    assert outcome.decision.verdict == "allow"
    assert outcome.decision.rationale.startswith("policy approved")
    assert "human approved" not in outcome.decision.rationale
