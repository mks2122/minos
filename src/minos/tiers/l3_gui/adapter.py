"""L3 GUI adapter -- the fallback, and honest about being one.

Every other tier can name what it changed. This one cannot. A synthetic click
lands somewhere and something happens; the runtime has no model of what. So L3
declares the weakest contract in the system, and the design makes that weakness
*visible* rather than papering over it:

* Effects are ``IRREVERSIBLE`` unless the caller declares targets. A click has
  no inverse and no checkpoint, so it always prompts.
* The oracle is a :class:`ScreenOracle`, which is **explicitly weak**. It
  reports that the screen rendered differently, which is not evidence that
  anything is true. Measured elsewhere: screen-only verification silently
  accepted ~75% of wrong effects.
* Every L3 routing decision is recorded as a degradation, and the fallback rate
  is published.

The intended use is the long tail -- the 2003 imaging viewer, the AS/400 green
screen, the bespoke ERP client with no API. Reaching L3 for something an L2
adapter could have handled is a bug in the adapter registry, and the fallback
rate is how you find out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...oracles import FileHashOracle
from ...types import (
    ActionRequest,
    EffectClass,
    EffectContract,
    Grant,
    Invocation,
    Tier,
)
from ..base import CapabilityManifest, OperationUnsupported, Preparation
from .driver import GuiDriver, StubDriver

__all__ = ["GuiAdapter", "ScreenOracle"]

_OPERATIONS = ("ui.click", "ui.type", "ui.key", "ui.screenshot")


@dataclass(slots=True)
class ScreenOracle:
    """Deliberately weak evidence, and labelled as such.

    ``verifiable()`` returns True because a screen digest *is* an observation --
    but every docstring and the README say plainly what it is worth. A screen
    tells you what was rendered, not what is true.
    """

    driver: GuiDriver
    kind: str = field(default="screen", init=False)

    def observe(self) -> dict[str, Any]:
        state = self.driver.observe()
        return {
            "digest": state.digest,
            "focused_window": state.focused_window,
            "_weak": "a screen digest is not a system-of-record read",
        }

    def verifiable(self) -> bool:
        return True


class GuiAdapter:
    """Vision plus synthetic input. Used only when nothing better applies."""

    manifest = CapabilityManifest(
        adapter="l3.gui",
        tier=Tier.L3_GUI,
        operations=_OPERATIONS,
        summary="Synthetic input fallback for applications with no typed adapter",
    )

    def __init__(self, driver: GuiDriver | None = None) -> None:
        self.driver = driver or StubDriver()

    def prepare(self, request: ActionRequest) -> Preparation:
        op = request.operation
        if op not in _OPERATIONS:
            raise OperationUnsupported(op)

        window = str(request.params.get("window", "*"))
        oracle = ScreenOracle(self.driver)

        if op == "ui.screenshot":

            def observe(_: Invocation) -> dict[str, Any]:
                state = self.driver.observe()
                return {
                    "digest": state.digest,
                    "focused_window": state.focused_window,
                    "elements": list(state.elements),
                }

            return Preparation(
                contract=EffectContract(
                    effect_class=EffectClass.PURE,
                    targets=(),
                    oracle=oracle,
                    expect="capture the screen; nothing changes",
                ),
                execute=observe,
                grants=(Grant("ui.input", window),),
            )

        if op == "ui.click":
            x, y = _coords(request.params)

            def click(_: Invocation) -> None:
                self.driver.click(x, y, str(request.params.get("button", "left")))

            return self._acting(request, click, oracle, window, f"click at ({x}, {y}) in {window}")

        if op == "ui.type":
            text = request.params.get("text")
            if text is None:
                raise OperationUnsupported("ui.type requires 'text'")

            def type_text(_: Invocation) -> None:
                self.driver.type_text(str(text))

            return self._acting(
                request,
                type_text,
                oracle,
                window,
                f"type {len(str(text))} characters into {window}",
            )

        chord = request.params.get("chord")
        if chord is None:
            raise OperationUnsupported("ui.key requires 'chord'")

        def key(_: Invocation) -> None:
            self.driver.key(str(chord))

        return self._acting(request, key, oracle, window, f"press {chord} in {window}")

    # -- helper ------------------------------------------------------------

    def _acting(
        self,
        request: ActionRequest,
        execute: Any,
        oracle: ScreenOracle,
        window: str,
        expect: str,
    ) -> Preparation:
        """Build a contract for an action that changes something unknown.

        If the caller can name the files the interaction will touch, the effect
        becomes checkpointable and reversible. That is worth encouraging, so the
        adapter honours a declared ``targets`` param -- but it will never guess
        one, because a wrong guess produces a checkpoint that does not cover
        what actually changed.

        **When targets are declared, verification switches to those files.** A
        screen digest would be the wrong instrument: the contract promises
        something about a file, so the oracle must read that file. Checkpointing
        a target while verifying a screenshot is how a runtime ends up reversing
        cleanly and confidently reporting the wrong thing.
        """
        declared = request.params.get("targets")
        targets = tuple(Path(str(t)) for t in declared) if declared else ()

        grants: list[Grant] = [Grant("ui.input", window)]
        grants.extend(Grant("fs.write", str(target)) for target in targets)

        return Preparation(
            contract=EffectContract(
                effect_class=(EffectClass.REVERSIBLE if targets else EffectClass.IRREVERSIBLE),
                targets=targets,
                # Strongest available evidence: the files when we know them,
                # the screen only when there is nothing better.
                oracle=FileHashOracle(targets) if targets else oracle,
                expect=expect + ("" if targets else "; effects unknown to this runtime"),
            ),
            execute=execute,
            grants=tuple(grants),
        )


def _coords(params: dict[str, Any]) -> tuple[int, int]:
    try:
        return int(params["x"]), int(params["y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OperationUnsupported("ui.click requires integer 'x' and 'y'") from exc
