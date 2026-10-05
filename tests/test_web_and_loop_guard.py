"""The web tier, the loop guard, goal splitting, and the run summary.

The web adapter is driven through a fake driver: what is under test is the
adapter's contract -- which site it grants, which control it names to the
approver, and what it refuses -- not Playwright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from minos.__main__ import summary_lines
from minos.agent import Agent, AgentLimits, _error_signature
from minos.approval import is_commit
from minos.audit import AuditLog
from minos.broker import Broker
from minos.checkpoint import FileCheckpointStore
from minos.planner.base import Done, Observation
from minos.planner.claude import parse_subtasks, system_prompt
from minos.planner.scripted import ScriptedPlanner
from minos.router import Router
from minos.scopes import ScopeSet
from minos.tiers.base import OperationUnsupported
from minos.tiers.l1_system import FilesystemAdapter
from minos.tiers.l2_adapters.web import StaleReference, WebAdapter, playwright_key
from minos.tiers.l2_code.adapter import _unescaped
from minos.types import ActionRequest, EffectClass, Invocation, Tier


def req(operation: str, **params: Any) -> ActionRequest:
    return ActionRequest(goal_id="task", intent=operation, operation=operation, params=params)


# -- the web adapter -------------------------------------------------------


@dataclass
class FakeDriver:
    page_url: str = ""
    controls: list[dict[str, Any]] = field(default_factory=list)
    clicked: list[str] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)
    readback: str | None = None
    live: dict[str, str] = field(default_factory=dict)

    def started(self) -> bool:
        return bool(self.page_url)

    def url(self) -> str:
        return self.page_url

    def open(self, url: str) -> None:
        self.page_url = url

    def snapshot(self, first_ref: int, limit: int, text_limit: int) -> dict[str, Any]:
        elements = []
        self.live = {}
        for offset, control in enumerate(self.controls[:limit]):
            ref = f"e{first_ref + offset}"
            elements.append({"ref": ref, **control})
            self.live[ref] = control["name"]
        return {
            "url": self.page_url,
            "title": "Feed",
            "elements": elements,
            "next": first_ref + len(elements),
            "text": "hello",
        }

    def read(self, limit: int) -> dict[str, Any]:
        return {"url": self.page_url, "title": "Feed", "text": "page text"}

    def label(self, ref: str) -> str | None:
        return self.live.get(ref)

    def click(self, ref: str) -> None:
        self.clicked.append(ref)

    def click_text(self, text: str) -> str:
        self.clicked.append(text)
        return "button"

    def fill(self, ref: str, text: str) -> str:
        self.filled[ref] = text
        return text if self.readback is None else self.readback

    def press(self, key: str, ref: str | None) -> None:
        self.clicked.append(f"key:{key}")

    def close(self) -> None:
        pass


FEED = [
    {"role": "button", "name": "Start a post"},
    {"role": "textbox", "name": "Search"},
    {"role": "button", "name": "Post"},
]


def opened(driver: FakeDriver | None = None) -> tuple[WebAdapter, FakeDriver, dict[str, Any]]:
    driver = driver or FakeDriver(controls=list(FEED))
    adapter = WebAdapter(driver=driver)
    prep = adapter.prepare(req("web.open", url="www.linkedin.com/feed/"))
    result = prep.execute(None)  # type: ignore[arg-type]
    return adapter, driver, result


def invocation(adapter: WebAdapter, request: ActionRequest) -> Invocation:
    prep = adapter.prepare(request)
    return Invocation(
        request=request,
        tier=Tier.L2_ADAPTER,
        adapter="l2.web",
        tier_reason="test",
        contract=prep.contract,
        grants=prep.grants,
    )


def test_open_lists_controls_by_reference_and_grants_the_site() -> None:
    adapter, _, result = opened()
    assert result["controls"] == [
        'e1 button "Start a post"',
        'e2 textbox "Search"',
        'e3 button "Post"',
    ]
    prep = adapter.prepare(req("web.open", url="https://www.linkedin.com/in/me"))
    assert [(g.capability, g.subject) for g in prep.grants] == [("browser.open", "linkedin.com")]
    assert prep.contract.effect_class is EffectClass.PURE


def test_open_refuses_non_web_schemes() -> None:
    adapter = WebAdapter(driver=FakeDriver())
    for url in ("file:///C:/secrets.txt", "javascript:alert(1)"):
        with pytest.raises(OperationUnsupported):
            adapter.prepare(req("web.open", url=url))


def test_acting_before_any_page_is_open_is_declined() -> None:
    adapter = WebAdapter(driver=FakeDriver())
    with pytest.raises(OperationUnsupported, match="web_open"):
        adapter.prepare(req("web.click", ref="e1"))


def test_click_is_irreversible_and_names_the_control_for_the_approver() -> None:
    adapter, _, _ = opened()
    compose = invocation(adapter, req("web.click", ref="e1"))
    post = invocation(adapter, req("web.click", ref="e3"))

    assert compose.contract.effect_class is EffectClass.IRREVERSIBLE
    assert [(g.capability, g.subject) for g in compose.grants] == [("web.input", "linkedin.com")]
    assert compose.contract.control == "Start a post"
    assert not is_commit(compose)
    assert post.contract.control == "Post"
    assert is_commit(post)


def test_an_unknown_reference_is_treated_as_a_commit() -> None:
    adapter, _, _ = opened()
    assert is_commit(invocation(adapter, req("web.click", ref="e99")))


def test_click_result_is_a_fresh_snapshot_and_old_refs_go_stale() -> None:
    adapter, driver, _ = opened()
    result = adapter.prepare(req("web.click", ref="e1")).execute(None)  # type: ignore[arg-type]
    assert driver.clicked == ["e1"]
    # Numbered on from the last snapshot, never reused.
    assert result["controls"][0].startswith("e4 ")
    with pytest.raises(StaleReference):
        adapter.prepare(req("web.click", ref="e1")).execute(None)  # type: ignore[arg-type]


def test_click_refuses_when_the_control_changed_name() -> None:
    adapter, driver, _ = opened()
    prep = adapter.prepare(req("web.click", ref="e1"))
    driver.live["e1"] = "Delete account"
    with pytest.raises(StaleReference, match="Delete account"):
        prep.execute(None)  # type: ignore[arg-type]
    assert driver.clicked == []


def test_nothing_is_sent_once_the_page_left_the_granted_site() -> None:
    adapter, driver, _ = opened()
    prep = adapter.prepare(req("web.click", ref="e1"))
    driver.page_url = "https://evil.example/"
    with pytest.raises(RuntimeError, match="nothing was sent"):
        prep.execute(None)  # type: ignore[arg-type]
    assert driver.clicked == []


def test_fill_reads_the_field_back_and_fails_when_it_does_not_hold_the_text() -> None:
    adapter, driver, _ = opened()
    fill = invocation(adapter, req("web.fill", ref="e2", text="minos\nagent"))
    assert not is_commit(fill)
    done = adapter.prepare(req("web.fill", ref="e2", text="minos\nagent")).execute(None)  # type: ignore[arg-type]
    assert done["now_reads"] == "minos\nagent"

    driver.readback = ""
    with pytest.raises(RuntimeError, match="does not hold the text"):
        adapter.prepare(req("web.fill", ref="e2", text="minos")).execute(None)  # type: ignore[arg-type]


def test_modified_enter_is_a_commit_and_keys_translate_for_playwright() -> None:
    adapter, _, _ = opened()
    assert is_commit(invocation(adapter, req("web.press", key="ctrl+enter")))
    assert not is_commit(invocation(adapter, req("web.press", key="Escape")))
    assert playwright_key("ctrl+enter") == "Control+Enter"
    assert playwright_key("esc") == "Escape"
    assert playwright_key("a") == "a"


def test_prompt_steers_websites_to_the_web_tier_when_present() -> None:
    both = system_prompt(("web.open", "web.click", "ui.click"))
    assert "THE WEB" in both and "Never use them for a website" in both
    assert "browser_open" not in both
    assert "browser_open" in system_prompt(("browser.open", "ui.click"))


# -- the loop guard --------------------------------------------------------


def build(workspace: Path, planner: Any, **limits: Any) -> Agent:
    root = workspace.parent
    return Agent(
        planner=planner,
        router=Router(adapters=(FilesystemAdapter(),)),
        broker=Broker(
            scopes=ScopeSet.parse([f"fs.read:{workspace}/**"]),
            audit=AuditLog(root / "audit.jsonl"),
            store=FileCheckpointStore(root / "cp"),
            approver=lambda inv, dec: False,
        ),
        limits=AgentLimits(**limits),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hi", encoding="utf-8")
    return ws


def test_the_same_failing_request_is_warned_then_stopped(workspace: Path) -> None:
    missing = req("fs.read", path=str(workspace / "sales_2025.csv"))
    agent = build(workspace, ScriptedPlanner([missing] * 6))
    trajectory = agent.run("read it")

    assert len(trajectory.observations) == 3
    assert trajectory.observations[1].warning
    assert "ends the run" in trajectory.observations[1].warning
    assert trajectory.finished is not None and not trajectory.finished.succeeded
    assert "loop guard" in trajectory.finished.summary


def test_the_same_error_under_new_arguments_is_stopped(workspace: Path) -> None:
    script = [req("fs.read", path=str(workspace / f"missing{i}.csv")) for i in range(6)]
    trajectory = build(workspace, ScriptedPlanner(list(script))).run("read them")
    assert len(trajectory.observations) == 3
    assert trajectory.finished is not None and "same error" in trajectory.finished.summary


def test_an_unroutable_request_counts_too(workspace: Path) -> None:
    trajectory = build(workspace, ScriptedPlanner([req("ui.click", target="x")] * 5)).run("click")
    assert [o.status for o in trajectory.observations] == ["unroutable"] * 3
    assert trajectory.outcomes == []


def test_repeating_a_working_request_forever_is_stopped(workspace: Path) -> None:
    listing = req("fs.list", path=str(workspace))
    trajectory = build(workspace, ScriptedPlanner([listing] * 10)).run("list")
    assert len(trajectory.observations) == 4
    assert trajectory.observations[2].warning
    assert "in a row" in (trajectory.finished.summary if trajectory.finished else "")


def test_progress_is_not_mistaken_for_a_loop(workspace: Path) -> None:
    script = [
        req("fs.read", path=str(workspace / "nope.txt")),
        req("fs.list", path=str(workspace)),
        req("fs.read", path=str(workspace / "notes.txt")),
        Done(summary="read it"),
    ]
    trajectory = build(workspace, ScriptedPlanner(script)).run("read the notes")
    assert trajectory.succeeded
    assert not any(o.warning for o in trajectory.observations)


def test_error_signature_ignores_the_quoted_parts() -> None:
    one = "FileNotFoundError: [Errno 2] No such file or directory: 'E:\\x\\a.csv'"
    two = "FileNotFoundError: [Errno 2] No such file or directory: 'E:\\x\\b.csv'"
    assert _error_signature(one) == _error_signature(two)


# -- splitting a large goal ------------------------------------------------


@dataclass
class SplittingPlanner:
    subtasks: list[str]
    goals: list[str] = field(default_factory=list)
    resets: int = 0
    fail_at: int = 0

    def decompose(self, goal: str, scopes: ScopeSet) -> list[str]:
        return list(self.subtasks)

    def reset(self) -> None:
        self.resets += 1

    def next_action(self, goal: str, observations: list[Observation], scopes: ScopeSet) -> Any:
        if goal not in self.goals:
            self.goals.append(goal)
            return req("fs.list", path=str(self._root))
        number = len(self.goals)
        return Done(summary=f"result {number}", succeeded=number != self.fail_at)

    _root: Path = Path(".")


LONG = "read the notes in the workspace and then write a short summary of them for the team"


def test_a_large_goal_runs_as_subtasks_that_see_earlier_reports(workspace: Path) -> None:
    planner = SplittingPlanner(subtasks=["read the notes", "summarise them"])
    planner._root = workspace
    trajectory = build(workspace, planner).run_task(LONG)

    assert trajectory.succeeded
    assert planner.resets == 2
    assert len(planner.goals) == 2
    assert "subtask 1 of 2" in planner.goals[0]
    assert "result 1" in planner.goals[1]  # the first report reached the second
    assert trajectory.finished is not None
    assert trajectory.finished.summary == "1. result 1\n2. result 2"
    assert len(trajectory.observations) == 2


def test_a_failed_subtask_stops_the_rest(workspace: Path) -> None:
    planner = SplittingPlanner(subtasks=["a first", "a second", "a third"], fail_at=2)
    planner._root = workspace
    trajectory = build(workspace, planner).run_task(LONG)
    assert not trajectory.succeeded
    assert len(planner.goals) == 2
    assert trajectory.finished is not None
    assert trajectory.finished.summary.startswith("stopped at subtask 2 of 3")


def test_short_goals_and_no_split_run_whole(workspace: Path) -> None:
    planner = SplittingPlanner(subtasks=["x one", "x two"])
    planner._root = workspace
    build(workspace, planner).run_task("list the files")
    assert planner.resets == 0

    planner = SplittingPlanner(subtasks=["x one", "x two"])
    planner._root = workspace
    build(workspace, planner, split=False).run_task(LONG)
    assert planner.resets == 0


def test_parse_subtasks_accepts_lists_and_numbered_prose() -> None:
    assert parse_subtasks(["read it", "", "  write it "]) == ["read it", "write it"]
    assert parse_subtasks("1. read the files\n2) draft the post\n- publish it") == [
        "read the files",
        "draft the post",
        "publish it",
    ]
    assert len(parse_subtasks([f"step {i}" for i in range(20)])) == 6
    assert parse_subtasks(None) == []


# -- the run summary -------------------------------------------------------


def test_summary_stays_aligned_after_an_unroutable_step(workspace: Path) -> None:
    script = [
        req("fs.list", path=str(workspace)),
        req("ui.click", target="profile-link"),
        req("fs.read", path=str(workspace / "notes.txt")),
        Done(summary="done"),
    ]
    trajectory = build(workspace, ScriptedPlanner(script)).run("go")
    lines = summary_lines(trajectory)
    assert lines[0].endswith("fs.list") and lines[0].lstrip().startswith("ok")
    assert lines[1].lstrip().startswith("SKIP") and lines[1].endswith("ui.click")
    assert lines[3].lstrip().startswith("ok") and lines[3].endswith("fs.read")


# -- code.run's escaped newlines -------------------------------------------


def test_double_escaped_code_is_repaired_only_when_that_is_the_fix() -> None:
    broken = "import os\\n\\nprint(os.listdir('.'))"
    fixed, repaired = _unescaped(broken)
    assert repaired and fixed == "import os\n\nprint(os.listdir('.'))"

    one_liner = 'print("a\\nb")'
    assert _unescaped(one_liner) == (one_liner, False)

    hopeless = "def (\\n"
    assert _unescaped(hopeless) == (hopeless, False)
