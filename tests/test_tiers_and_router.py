"""L1 adapters and the tier router, end to end through the broker.

These are the first tests where a real filesystem operation goes all the way
through admission, checkpointing, verification and reversal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from writ.audit import AuditLog
from writ.broker import Broker
from writ.checkpoint import FileCheckpointStore
from writ.router import NoAdapter, Router
from writ.scopes import ScopeSet
from writ.tiers.base import CapabilityManifest, OperationUnsupported, Preparation
from writ.tiers.l1_system import FilesystemAdapter, ProcessAdapter
from writ.types import (
    ActionRequest,
    EffectClass,
    EffectContract,
    Tier,
)


@pytest.fixture
def router() -> Router:
    return Router(adapters=(FilesystemAdapter(), ProcessAdapter()))


@pytest.fixture
def rig(tmp_path: Path, router: Router):
    """Router + broker over a scoped workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    broker = Broker(
        scopes=ScopeSet.parse(
            [
                f"fs.read:{ws}/**",
                f"fs.write:{ws}/**",
                f"fs.delete:{ws}/**",
            ]
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )

    def run(operation: str, **params):
        request = ActionRequest(
            goal_id="t", intent=f"test {operation}", operation=operation, params=params
        )
        routed = router.route(request)
        return broker.submit(routed.invocation, routed.execute)

    return ws, broker, router, run


# -- routing ---------------------------------------------------------------


def test_routes_to_l1_natively(router, tmp_path):
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="read",
            operation="fs.read",
            params={"path": str(tmp_path / "a.txt")},
        )
    )
    assert routed.invocation.tier is Tier.L1_SYSTEM
    assert routed.invocation.adapter == "l1.fs"
    assert "no fallback needed" in routed.invocation.tier_reason


def test_unknown_operation_is_refused_at_the_router(router):
    with pytest.raises(NoAdapter, match="not registered"):
        router.route(ActionRequest(goal_id="t", intent="x", operation="fs.obliterate"))


def test_registered_but_unhandled_operation_raises(router):
    """net.http is a known capability but nothing implements it yet."""
    with pytest.raises(NoAdapter, match="no adapter could prepare"):
        router.route(
            ActionRequest(
                goal_id="t", intent="x", operation="net.http", params={"url": "https://example.com"}
            )
        )


def test_tier_hint_does_not_bind_the_router(router, tmp_path):
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="read",
            operation="fs.read",
            params={"path": str(tmp_path / "a.txt")},
            tier_hint=Tier.L3_GUI,  # planner asks for pixels; router knows better
        )
    )
    assert routed.invocation.tier is Tier.L1_SYSTEM


def test_degradation_is_recorded(tmp_path):
    """I3: falling to a lower tier must say what was missing."""

    class FussyL1:
        manifest = CapabilityManifest(
            adapter="l1.fussy", tier=Tier.L1_SYSTEM, operations=("fs.write",)
        )

        def prepare(self, request):
            raise OperationUnsupported("cannot handle files over 1 byte")

    class GuiFallback:
        manifest = CapabilityManifest(adapter="l3.gui", tier=Tier.L3_GUI, operations=("fs.write",))

        def prepare(self, request):
            return Preparation(
                contract=EffectContract(effect_class=EffectClass.REVERSIBLE),
                execute=lambda inv: None,
            )

    router = Router(adapters=(GuiFallback(), FussyL1()))
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="w",
            operation="fs.write",
            params={"path": str(tmp_path / "a.txt"), "content": "xx"},
        )
    )
    assert routed.invocation.tier is Tier.L3_GUI
    assert "l1.fussy declined" in routed.invocation.tier_reason
    assert "fell back to l3.gui" in routed.invocation.tier_reason


def test_platform_unavailable_adapter_is_skipped(tmp_path):
    other = "win32" if sys.platform != "win32" else "linux"

    class WrongPlatform:
        manifest = CapabilityManifest(
            adapter="l1.wrong",
            tier=Tier.L1_SYSTEM,
            operations=("fs.write",),
            platforms=(other,),
        )

        def prepare(self, request):  # pragma: no cover - must never be called
            raise AssertionError("adapter ran on the wrong platform")

    class Portable:
        manifest = CapabilityManifest(
            adapter="l2.portable", tier=Tier.L2_ADAPTER, operations=("fs.write",)
        )

        def prepare(self, request):
            return Preparation(
                contract=EffectContract(effect_class=EffectClass.REVERSIBLE),
                execute=lambda inv: None,
            )

    router = Router(adapters=(WrongPlatform(), Portable()))
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="w",
            operation="fs.write",
            params={"path": str(tmp_path / "a.txt"), "content": "x"},
        )
    )
    assert routed.invocation.adapter == "l2.portable"
    assert "not available on" in routed.invocation.tier_reason


def test_fallback_rate_is_tracked(router, tmp_path):
    for _ in range(4):
        router.route(
            ActionRequest(
                goal_id="t",
                intent="r",
                operation="fs.read",
                params={"path": str(tmp_path / "a.txt")},
            )
        )
    assert router.stats.summary()["L1"] == 4
    assert router.stats.fallback_rate == 0.0


# -- filesystem adapter ----------------------------------------------------


def test_write_then_read(rig):
    ws, _, _, run = rig
    out = run("fs.write", path=str(ws / "a.txt"), content="hello")
    assert out.status == "ok"

    out = run("fs.read", path=str(ws / "a.txt"))
    assert out.status == "ok"
    assert out.result == "hello"


def test_read_is_pure_and_takes_no_checkpoint(rig):
    ws, _, _, run = rig
    (ws / "a.txt").write_text("content", encoding="utf-8")
    out = run("fs.read", path=str(ws / "a.txt"))
    assert out.invocation.contract.effect_class is EffectClass.PURE
    assert out.checkpoint_id is None


def test_delete_is_reversible_not_irreversible(rig):
    """The checkpoint holds the bytes, so calling this irreversible would be theatre."""
    ws, broker, _, run = rig
    target = ws / "doomed.txt"
    target.write_text("important", encoding="utf-8")

    out = run("fs.delete", path=str(target))
    assert out.status == "ok"
    assert out.invocation.contract.effect_class is EffectClass.REVERSIBLE
    assert not target.exists()

    assert broker.store.restore(out.checkpoint_id).succeeded
    assert target.read_text() == "important"


def test_copy_requires_read_on_source_and_write_on_dest(rig):
    ws, broker, router, _run = rig
    source = ws / "src.txt"
    source.write_text("payload", encoding="utf-8")

    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="copy",
            operation="fs.copy",
            params={"source": str(source), "path": str(ws / "dst.txt")},
        )
    )
    caps = {g.capability for g in routed.invocation.grants}
    assert caps == {"fs.read", "fs.write"}

    out = broker.submit(routed.invocation, routed.execute)
    assert out.status == "ok"
    assert (ws / "dst.txt").read_text() == "payload"


def test_copy_denied_when_source_unreadable(tmp_path, router):
    ws = tmp_path / "ws"
    ws.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "keys.txt").write_text("sensitive", encoding="utf-8")

    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{ws}/**", f"fs.write:{ws}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="exfiltrate",
            operation="fs.copy",
            params={"source": str(secrets / "keys.txt"), "path": str(ws / "stolen.txt")},
        )
    )
    out = broker.submit(routed.invocation, routed.execute)

    # The destination is writable, so a single-capability check would have
    # allowed this. Explicit grants are what catch it.
    assert out.status == "denied"
    assert "fs.read" in out.decision.rationale
    assert not (ws / "stolen.txt").exists()


def test_move_checkpoints_both_ends(rig):
    ws, broker, _, run = rig
    source = ws / "from.txt"
    source.write_text("moving", encoding="utf-8")

    out = run("fs.move", source=str(source), path=str(ws / "to.txt"))
    assert out.status == "ok"
    assert not source.exists()
    assert (ws / "to.txt").read_text() == "moving"

    assert broker.store.restore(out.checkpoint_id).succeeded
    assert source.read_text() == "moving"
    assert not (ws / "to.txt").exists()


def test_mkdir_is_compensable_and_prompts_without_delete_scope(tmp_path, router):
    """No fs.delete grant means the inverse is unavailable, so it cannot auto-allow."""
    ws = tmp_path / "ws"
    ws.mkdir()
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.write:{ws}/**", f"fs.read:{ws}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t", intent="mkdir", operation="fs.mkdir", params={"path": str(ws / "new")}
        )
    )
    assert routed.invocation.contract.effect_class is EffectClass.COMPENSABLE
    assert broker.admit(routed.invocation).verdict == "prompt"


def test_mkdir_auto_allows_when_its_inverse_is_in_scope(rig):
    ws, _broker, _, run = rig  # the rig grants fs.delete
    out = run("fs.mkdir", path=str(ws / "new"))
    assert out.status == "ok"
    assert (ws / "new").is_dir()
    assert out.decision.verdict == "allow"


def test_write_outside_scope_is_denied_before_execution(rig):
    ws, _, _, run = rig
    out = run("fs.write", path=str(ws.parent / "escape.txt"), content="nope")
    assert out.status == "denied"
    assert not (ws.parent / "escape.txt").exists()


def test_missing_required_param_declines_cleanly(router):
    with pytest.raises(NoAdapter, match="declined"):
        router.route(
            ActionRequest(
                goal_id="t", intent="w", operation="fs.write", params={"path": "/tmp/a.txt"}
            )
        )


# -- process adapter -------------------------------------------------------


def test_spawn_is_always_irreversible(router):
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="run",
            operation="proc.spawn",
            params={"command": sys.executable, "args": ["-c", "print(1)"]},
        )
    )
    contract = routed.invocation.contract
    assert contract.effect_class is EffectClass.IRREVERSIBLE
    assert contract.oracle is not None and not contract.oracle.verifiable()


def test_spawn_prompts_even_when_in_scope(tmp_path, router):
    broker = Broker(
        scopes=ScopeSet.parse([f"proc.spawn:{sys.executable}"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="run",
            operation="proc.spawn",
            params={"command": sys.executable, "args": ["-c", "print(1)"]},
        )
    )
    assert broker.admit(routed.invocation).verdict == "prompt"

    # And with no approver wired up, it is refused rather than silently run.
    assert broker.submit(routed.invocation, routed.execute).status == "denied"


def test_approved_spawn_returns_untrusted_output(tmp_path, router):
    broker = Broker(
        scopes=ScopeSet.parse([f"proc.spawn:{sys.executable}"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
        approver=lambda inv, dec: True,
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="run",
            operation="proc.spawn",
            params={"command": sys.executable, "args": ["-c", "print('hello from a subprocess')"]},
        )
    )
    out = broker.submit(routed.invocation, routed.execute)
    assert out.status == "ok"
    assert out.result.returncode == 0
    assert "hello from a subprocess" in out.result.stdout
    assert out.observed is not None and out.observed.verifiable is False


def test_spawn_outside_scope_denied(tmp_path, router):
    broker = Broker(
        scopes=ScopeSet.parse(["proc.spawn:/usr/bin/soffice"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
        approver=lambda inv, dec: True,
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="run",
            operation="proc.spawn",
            params={"command": sys.executable, "args": ["-c", "print(1)"]},
        )
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"
