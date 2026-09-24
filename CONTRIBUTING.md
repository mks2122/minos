# Contributing to minos

Thanks for looking. minos is a local agent that drives a real computer, so the bar
for a change is not only "does it work" but "can it still be undone, refused, and
audited afterwards". This page is the short version of what that means in practice.

## Setup

```bash
git clone https://github.com/mks2122/minos.git
cd minos
uv sync --all-extras
uv run minos doctor        # what is installed, what is served, what is missing
```

A local model is optional for most work. The reference planner runs the whole eval
suite with no model at all.

## Before you open a pull request

All of these must pass. CI runs them on Linux, macOS and Windows, on Python 3.11 and
3.13.

```bash
uv run pytest                        # unit and integration tests
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy --platform linux         # strict; CI checks linux, darwin and win32,
uv run mypy --platform win32         # because platform-only code passes on its own OS
uv run minos eval                    # must stay 18/18 with 0 invariant violations
```

If you touched the GUI tier (`src/minos/tiers/l3_gui/`) and you are on Windows, also
run the live harness. It takes your real mouse and keyboard for about thirty seconds,
and only ever sends input to its own throwaway windows:

```bash
uv run python tests/gui/live_check.py
```

## The rules that are not negotiable

These are the architecture, not style preferences. A pull request that weakens one
will be asked to find another way, however useful the feature. The reasoning is in
[ARCHITECTURE.md](ARCHITECTURE.md).

1. **The planner never executes (I1).** It produces an `ActionRequest` and nothing
   else. No filesystem handle, subprocess, socket or input device reaches it.
2. **Every tier passes the same gate (I2).** A new adapter goes through the broker
   like every other one. There is no fast path, including "just for reads".
3. **Degradation is auditable (I3).** Falling back to a weaker tier records why.
4. **Every effect declares its class honestly.** `PURE`, `REVERSIBLE`
   (checkpointed), `COMPENSABLE` (a declared inverse) or `IRREVERSIBLE` (asks the
   person first). If you cannot say what an operation changes, it is irreversible.
   Marking something reversible to avoid a prompt is the one bug this project exists
   to prevent.
5. **Never guess.** Two controls named "Save", two windows matching a title, a vague
   file reference: raise and list the candidates. Picking the first match is how an
   agent clicks "Don't Save".

## Adding an adapter or an operation

- Declare the effect contract: class, targets, and an oracle that reads the system of
  record. A screenshot is not an oracle for a file.
- Add the operation to the planner's tool schemas only once the adapter enforces its
  scope. The broker refuses what the grant does not cover. Test that it does.
- Write the test that proves the contract: that a reversible action really restores,
  and that a denied action really did nothing. Mocks are fine for the contract; the
  eval suite and the live harness check the real thing.

## Security-sensitive areas

Changes to `broker.py`, `checkpoint.py`, `scopes.py`, `src/minos/sandbox/` or the GUI
driver need:

- a test that tries the escape, not only the happy path;
- an update to [SECURITY.md](SECURITY.md) if the change alters what is or is not
  contained. That file says what does *not* hold as plainly as what does. Keep it
  that way.

Found a vulnerability? Please do not open a public issue. See
[SECURITY.md](SECURITY.md#reporting-a-vulnerability).

## Claims need evidence

The docs make measured claims only: numbers come from a run someone can repeat, and
the command that produced them sits next to them. If a change moves a number in
README.md, EVALUATION.md or docs/STATUS.md, update it in the same pull request, with
the run that produced it. "Should be faster" is not a claim this project makes.

## Commits and pull requests

- One logical change per pull request. Small is easier to review, and much easier to
  revert.
- Say *why* in the commit message. The diff already shows what changed.
- Add a line to the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md) for anything
  a user would notice.

## Code of conduct

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Licence

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same as the rest of the project.
