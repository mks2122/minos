from __future__ import annotations

from pathlib import Path

import pytest

from writ.audit import AuditLog
from writ.broker import Broker
from writ.checkpoint import FileCheckpointStore
from writ.scopes import ScopeSet
from writ.types import (
    ActionRequest,
    EffectClass,
    EffectContract,
    Invocation,
    Tier,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def store(tmp_path: Path) -> FileCheckpointStore:
    return FileCheckpointStore(tmp_path / "checkpoints")


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def broker(workspace: Path, store: FileCheckpointStore, audit: AuditLog) -> Broker:
    return Broker(
        scopes=ScopeSet.parse([f"fs.write:{workspace}/**", f"fs.read:{workspace}/**"]),
        audit=audit,
        store=store,
        approver=lambda inv, dec: False,
    )


def make_invocation(
    *,
    operation: str = "fs.write",
    targets: tuple[Path, ...] = (),
    effect_class: EffectClass = EffectClass.REVERSIBLE,
    oracle: object | None = None,
    compensation: ActionRequest | None = None,
    tier: Tier = Tier.L1_SYSTEM,
    params: dict | None = None,
    expect: str = "",
) -> Invocation:
    return Invocation(
        request=ActionRequest(
            goal_id="g1",
            intent="test action",
            operation=operation,
            params=params or {},
        ),
        tier=tier,
        adapter="test",
        tier_reason="test fixture",
        contract=EffectContract(
            effect_class=effect_class,
            targets=targets,
            oracle=oracle,  # type: ignore[arg-type]
            expect=expect,
            compensation=compensation,
        ),
    )
