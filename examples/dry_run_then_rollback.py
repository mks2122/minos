"""The 30-second demo.

Run the same action twice — once in dry-run showing the diff of what *would*
happen, once for real — then roll it back and prove the bytes are identical.

    uv run python examples/dry_run_then_rollback.py

This is the demo the README leads with, so it is kept runnable and honest: it
prints real outcomes from the real broker, including the audit chain check.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from minos import (
    ActionRequest,
    AuditLog,
    Broker,
    EffectClass,
    EffectContract,
    FileCheckpointStore,
    FileHashOracle,
    Invocation,
    ScopeSet,
    Tier,
)


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * 60)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()

        report = workspace / "q3-report.txt"
        report.write_text("Q3 revenue: 41,800\n", encoding="utf-8")
        original = report.read_bytes()

        secrets = root / "secrets"
        secrets.mkdir()
        (secrets / "keys.txt").write_text("do not touch", encoding="utf-8")

        # Scopes are set BEFORE the task and are immutable during it.
        scopes = ScopeSet.parse(
            [
                f"fs.read:{workspace}/**",
                f"fs.write:{workspace}/**",
            ]
        )

        audit = AuditLog(root / "audit.jsonl")
        broker = Broker(
            scopes=scopes,
            audit=audit,
            store=FileCheckpointStore(root / "checkpoints"),
        )

        def invocation(target: Path, expect: str) -> Invocation:
            return Invocation(
                request=ActionRequest(
                    goal_id="demo",
                    intent="update Q3 revenue in the report",
                    operation="fs.write",
                    params={"path": str(target)},
                ),
                tier=Tier.L1_SYSTEM,
                adapter="fs",
                tier_reason="native filesystem tool covers this; no GUI needed",
                contract=EffectContract(
                    effect_class=EffectClass.REVERSIBLE,
                    targets=(target,),
                    oracle=FileHashOracle((target,)),
                    expect=expect,
                ),
            )

        def apply_edit(_: Invocation) -> None:
            report.write_text("Q3 revenue: 48,200\n", encoding="utf-8")

        # -- 1. dry run ----------------------------------------------------
        rule("1. DRY RUN - what would happen, without doing it")
        broker.dry_run = True
        outcome = broker.submit(invocation(report, "Q3 revenue becomes 48,200"))
        assert outcome.predicted is not None
        for key, value in outcome.predicted.items():
            print(f"  {key:>14}: {value}")
        print(f"\n  file on disk  : {report.read_text().strip()!r}  (unchanged)")

        # -- 2. for real ---------------------------------------------------
        rule("2. EXECUTE - for real, verified against the system of record")
        broker.dry_run = False
        outcome = broker.submit(invocation(report, "Q3 revenue becomes 48,200"), apply_edit)
        assert outcome.observed is not None
        print(f"  status        : {outcome.status}")
        print(f"  verified      : {outcome.observed.verifiable} ({outcome.observed.detail})")
        print(f"  checkpoint    : {outcome.checkpoint_id}")
        print(f"\n  file on disk  : {report.read_text().strip()!r}")

        # -- 3. rollback ---------------------------------------------------
        rule("3. ROLLBACK - restore and prove it")
        assert outcome.checkpoint_id is not None
        store = FileCheckpointStore(root / "checkpoints")
        result = store.restore(outcome.checkpoint_id)
        print(f"  restored      : {result.succeeded}")
        print(f"  verified      : {store.verify(outcome.checkpoint_id)}")
        print(f"  byte-identical: {report.read_bytes() == original}")
        print(f"\n  file on disk  : {report.read_text().strip()!r}")

        # -- 4. out of scope -----------------------------------------------
        rule("4. OUT OF SCOPE - the planner asks for something it may not have")
        ran: list[str] = []
        denied = broker.submit(
            invocation(secrets / "keys.txt", "exfiltrate"),
            lambda i: ran.append("THIS MUST NOT PRINT"),
        )
        print(f"  status        : {denied.status}")
        print(f"  rationale     : {denied.decision.rationale}")
        print(f"  executor ran  : {bool(ran)}")

        # -- 5. audit ------------------------------------------------------
        rule("5. AUDIT - every decision, hash-chained")
        for entry in audit.entries():
            print(
                f"  #{entry['seq']} {entry['status']:<22}"
                f" {entry['invocation']['tier']}"
                f"  {entry['decision']['verdict']}"
            )
        print(f"\n  chain intact  : {audit.verify() == []}")
        print(f"  head          : {audit.head[:16]}...")


if __name__ == "__main__":
    main()
