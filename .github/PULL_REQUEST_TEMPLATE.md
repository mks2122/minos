## What and why

<!-- What this changes, and the problem it solves. Link the issue if there is one. -->

## How it was tested

<!-- Commands you ran and what they showed. For GUI changes on Windows, the live
     harness result (uv run python tests/gui/live_check.py). -->

## Checklist

- [ ] `uv run pytest`, `ruff check`, `ruff format --check` and `mypy` pass
- [ ] `uv run minos eval` is still 18/18 with 0 invariant violations
- [ ] New operations declare an honest effect class, and a test proves the contract
- [ ] No path around the broker (I1–I3 in [ARCHITECTURE.md](../ARCHITECTURE.md))
- [ ] SECURITY.md updated if this changes what is or is not contained
- [ ] Any number in the docs that this moves is updated, with the run behind it
- [ ] CHANGELOG.md `Unreleased` has a line, if a user would notice
