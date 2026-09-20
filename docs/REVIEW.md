# Review — September 2026

A full read of the plan against the repository, plus an audit of the
copy-before-write journal. Written after M16, before the generality work in
[PLAN-GENERALITY.md](PLAN-GENERALITY.md).

Findings are ordered by how much damage they can do, not by how hard they are
to fix.

---

## 1. Fixed: rollback could delete the file it was protecting

**Severity: data loss. Reachable on Windows in normal use.**

`file_digest()` caught `PermissionError` and returned `None`. But `None` had a
meaning in the manifest — *"this file did not exist"* — which restore acted on
by deleting the path.

So a file that existed but could not be read was recorded as a file that was
never there, and rollback removed it. `verify()` then returned `True`, because
it was checking reality against a manifest that already contained the wrong
fact. The component's own docstring names this exact failure:

> a restore that reports success but does not verify is the failure mode this
> whole component exists to prevent

Reproduced before the fix:

```
manifest: { "...\locked.docx": null }
file exists before restore: True
restore reported succeeded: True      <- claimed a clean rollback
file exists AFTER rollback: False     <- the file is gone
verify() says checkpoint honoured: True
```

This was not theoretical. Windows locks files that applications hold open, and
`app.open` (M16) exists to hand documents to applications. Open a `.docx`, act
on it, hit any failure, and the rollback deletes it.

**Fix.** Probing is now `probe()`, which returns an explicit `TargetState` with
a `kind` of `file` / `absent` / `directory` / `symlink`, and raises
`UnprotectableTarget` when a path exists but cannot be copied. The broker turns
that into a refusal: *a target we cannot copy is a target we cannot protect, so
the action does not run.* Acting anyway would mean recording `REVERSIBLE` for an
effect that is not reversible — the one lie this component must never tell.

`file_digest()` survives for oracles, but now returns the `UNREADABLE` sentinel
instead of `None` for a locked file, so a `FileHashOracle` no longer reports a
locked file as a deleted one.

---

## 2. Fixed: directories were recorded as absent

`IsADirectoryError` also returned `None`, so a directory target was recorded as
having never existed. Restore then tried to `unlink()` it, failed, and produced
a spurious `reconciliation_required`.

`fs.mkdir` and `fs.delete` are registered operations, so this was a live path,
not an edge case.

**Fix.** `directory` is a first-class `TargetState.kind`. A directory that
existed is recreated; one that did not is removed with `rmdir` — deliberately
not `rmtree`, because only *declared* targets may be deleted. A directory the
action filled with undeclared files now fails loudly instead of taking the
user's files with it.

---

## 3. Fixed: symlinks were replaced by copies

`Path.resolve()` follows the final component, so checkpointing a symlink
silently checkpointed its target, and restoring wrote a regular file over the
link.

**Fix.** `_normalise()` resolves the parent chain only, preserving the final
component's link-ness. Symlinks are recorded with their target and relinked on
restore.

---

## 4. Fixed: restore discarded permissions and mtime

`shutil.copyfile` copies contents and nothing else. An executable came back
without its `+x` bit, and every restored file got a fresh mtime.

The mtime case matters beyond tidiness: `memory/store.py` keys its file index on
mtime, so **every rollback fabricated a phantom edit in memory**.

**Fix.** Mode and mtime are captured in the manifest and restored.

Still not captured: Windows ACLs, extended attributes, alternate data streams.
Documented rather than fixed.

---

## 5. Fixed: no pre-flight integrity check

`verify()` ran only *after* a restore, so a pruned or corrupted object was
discovered at rollback time — the one moment nothing can be done about it.

**Fix.** `ensure_intact()` checks every referenced object exists, and the broker
calls it immediately after checkpointing. `restore()` also calls it before the
first write, so the common partial-restore case leaves the filesystem untouched
instead of mixed.

---

## 6. Fixed: the gap between copying and acting

Nothing re-checked the targets between `checkpoint()` and calling the executor.
A file someone edited in that window would be rolled back to a state the user
never had.

**Fix.** `unchanged_since()`, called by the broker immediately before execution.
Drift is a refusal, not a silent overwrite.

---

## 7. Fixed: the object store grew without bound

Content-addressed dedup reduced duplication but nothing ever pruned, and
`MODELS.md` already flags this machine as disk-constrained.

**Fix.** `forget()` drops a manifest; `gc()` deletes objects no live manifest
references and returns the count; `usage_bytes()` reports the total for a
retention policy to act on.

No automatic retention policy is wired in yet — that is a CLI decision, not a
store one.

---

## 8. Fixed: no size limit

`MODELS.md` says *"checkpointing needs headroom by definition"*. Nothing enforced
it, so a 20 GB target would copy 20 GB without comment.

**Fix.** `max_target_bytes` (default 1 GiB) refuses an oversized target, and
free space is checked before copying, with a margin left to write the manifest.
Both raise `UnprotectableTarget`, so both become refusals rather than a half-full
disk.

---

## 9. Not fixed: the checkpoint store is plaintext

The store holds unencrypted copies of whatever was checkpointed, in a
predictable location, for as long as the manifest lives. It is now `chmod 0700`
on POSIX and documented in the module docstring, but it is not encrypted and
Windows gets only inherited directory ACLs.

Belongs in `SECURITY.md` as a stated limitation.

---

## 10. Not fixed: restore is not atomic across targets

Object availability is now checked up front, which removes the common cause of a
partial restore. But if the third of five files fails to write, the first two
stay restored. `RestoreResult` reports this honestly; there is no all-or-nothing
guarantee across multiple targets.

---

# Plan-level gaps

These are not bugs. They are places where the repository and `PLAN.md` disagree.

### The plan's own top risk fired, and was not acted on

`PLAN.md` §10 defines the project's first risk as:

> **Rollback stays filesystem-only** — signal: *you're at M2 and external effects
> still aren't modelled* — response: **Stop and fix. This is the whole
> differentiator.**

`STATUS.md` confirms `COMPENSABLE` inverses are declared and scope-checked but
never executed. That trigger fired around M2, and M3 through M16 were built
anyway. This is the largest single gap between the plan and the code, and it is
the part that distinguishes this project from a checkpoint tool.

### Open question 3 was never answered

`PLAN.md` §11 asks whether the broker runs as a separate process, recommends
*"same-process, clean interface, split at M6"*. It is M16, still same-process,
and `SECURITY.md` carries a section titled *"A bug in the broker is a full
bypass."* The plan scheduled its own mitigation and the milestone passed.

### There is no user-facing undo

The broker reverses on *its own* failure. But the store now holds a
content-addressed history of every pre-action state, and there is no way for a
person to say *"undo what you did ten minutes ago."* The most sellable feature in
the project is ~90% built and entirely unexposed in the CLI.

### No concurrency model

Single-process is assumed throughout. Two `minos` runs would share a checkpoint
store and an append-only hash-chained audit log with no locking. The chain is
what breaks.

### No credentials model

Nothing in `scopes.py` or `SECURITY.md` describes what a credential is or how one
is handled. This becomes load-bearing the moment the sandbox or the virtual
device touches a logged-in application.

### Evaluation is short-horizon only

`EVALUATION.md` states every task is under 10 steps, and that this is *"the
regime where published agents look good and real work does not live."* Correct,
and still unaddressed. See M23 in [PLAN-GENERALITY.md](PLAN-GENERALITY.md) for
the invariant-based approach that replaces enumerating capabilities.
