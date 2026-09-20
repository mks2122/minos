"""Hash-chained, append-only audit log.

Cheap to add now; impossible to retrofit credibly. Each record carries the hash
of the previous one, so any edit to history invalidates every record after it
and :meth:`AuditLog.verify` will say where.

This is tamper-*evident*, not tamper-proof: an attacker who can rewrite the whole
file can recompute the chain. Detecting that requires anchoring the head hash
somewhere the agent cannot write, which is out of scope for v0 and noted in
SECURITY.md.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import Invocation, ProvenanceRecord

__all__ = ["GENESIS", "AuditLog", "ChainBreak"]

GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class ChainBreak:
    seq: int
    expected: str
    found: str


def _canonical(payload: dict[str, Any]) -> str:
    """Stable JSON. Sorted keys and no incidental whitespace, or the hash is noise."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


def _serialise_invocation(inv: Invocation) -> dict[str, Any]:
    contract = inv.contract
    return {
        "tier": str(inv.tier),
        "adapter": inv.adapter,
        "tier_reason": inv.tier_reason,
        "request": {
            "goal_id": inv.request.goal_id,
            "intent": inv.request.intent,
            "operation": inv.request.operation,
            "params": inv.request.params,
            "tier_hint": str(inv.request.tier_hint) if inv.request.tier_hint else None,
        },
        "contract": {
            "effect_class": str(contract.effect_class),
            "targets": [str(t) for t in contract.targets],
            "oracle": getattr(contract.oracle, "kind", None),
            "expect": contract.expect,
            "compensation": (contract.compensation.operation if contract.compensation else None),
        },
    }


class AuditLog:
    """Append-only JSONL log with a hash chain."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._head = self._read_head()

    @property
    def head(self) -> str:
        return self._head

    @property
    def count(self) -> int:
        return self._seq

    def append(self, record: ProvenanceRecord) -> str:
        payload = {
            "seq": record.seq,
            "ts": record.ts.isoformat(),
            "status": record.status,
            "invocation": _serialise_invocation(record.invocation),
            "decision": {
                "verdict": record.decision.verdict,
                "rationale": record.decision.rationale,
                "matched_scopes": list(record.decision.matched_scopes),
                "denied_by": record.decision.denied_by,
            },
            "checkpoint_id": record.checkpoint_id,
            "observed": (
                {
                    "kind": record.observed.kind,
                    "verifiable": record.observed.verifiable,
                    "matched": record.observed.matched,
                    "detail": record.observed.detail,
                    "collateral": record.observed.collateral,
                }
                if record.observed
                else None
            ),
            "reversal": (
                {
                    "attempted": record.reversal.attempted,
                    "succeeded": record.reversal.succeeded,
                    "detail": record.reversal.detail,
                }
                if record.reversal
                else None
            ),
        }
        entry_hash = _hash(record.prev_hash, payload)
        line = _canonical({"prev_hash": record.prev_hash, "hash": entry_hash, **payload})
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._seq = record.seq
        self._head = entry_hash
        return entry_hash

    def next_seq(self) -> int:
        return self._seq + 1

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
        return out

    def verify(self) -> list[ChainBreak]:
        """Recompute the chain. Empty list means intact."""
        breaks: list[ChainBreak] = []
        prev = GENESIS
        for entry in self.entries():
            stored_hash = entry["hash"]
            payload = {k: v for k, v in entry.items() if k not in ("prev_hash", "hash")}
            if entry["prev_hash"] != prev:
                breaks.append(ChainBreak(entry["seq"], prev, entry["prev_hash"]))
            expected = _hash(entry["prev_hash"], payload)
            if expected != stored_hash:
                breaks.append(ChainBreak(entry["seq"], expected, stored_hash))
            prev = stored_hash
        return breaks

    def _read_head(self) -> tuple[int, str]:
        entries = self.entries()
        if not entries:
            return 0, GENESIS
        last = entries[-1]
        return int(last["seq"]), str(last["hash"])
