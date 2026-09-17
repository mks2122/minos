"""Computer-state memory.

Not conversation history, and not a RAG index of document *contents*. An index
of **what the computer was doing, over time**: which files exist, when they
changed, and which action in which session touched them.

That last part is the bit nobody else has. Every desktop assistant can tell you
a file's mtime. This can tell you *"you edited it during the task called `update
Q3 forecast`, in the step that set cell B4"* -- because the broker records every
admitted action, and those records are the provenance.

**Memory content is untrusted** (see SECURITY.md). A filename can carry an
injected instruction. Resolution may inform planning; it may never widen a
scope.

Storage is SQLite with FTS5, which ships with Python. Deliberately not Letta or
Mem0: those model conversational memory, and a filesystem-and-provenance index
is a different data model.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..types import Outcome

__all__ = ["FileRecord", "MemoryStore", "Opening", "Touch"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    suffix      TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    exists_now  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    id       TEXT PRIMARY KEY,
    goal     TEXT NOT NULL,
    started  REAL NOT NULL,
    ended    REAL
);

CREATE TABLE IF NOT EXISTS touches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL,
    ts           REAL NOT NULL,
    session_id   TEXT,
    goal         TEXT,
    operation    TEXT NOT NULL,
    adapter      TEXT NOT NULL,
    tier         TEXT NOT NULL,
    effect_class TEXT NOT NULL,
    status       TEXT NOT NULL
);

-- What was opened, in what, and when. ARCHITECTURE.md promised app/window
-- history and this is the honest subset: openings this runtime performed. A
-- document opened by double-clicking in Explorer is not here, and cannot be
-- without OS-level window enumeration.
CREATE TABLE IF NOT EXISTS opens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL,
    handler    TEXT NOT NULL,
    ts         REAL NOT NULL,
    session_id TEXT,
    goal       TEXT
);

CREATE INDEX IF NOT EXISTS opens_path ON opens(path);
CREATE INDEX IF NOT EXISTS opens_ts   ON opens(ts);
CREATE INDEX IF NOT EXISTS touches_path ON touches(path);
CREATE INDEX IF NOT EXISTS touches_ts   ON touches(ts);
CREATE INDEX IF NOT EXISTS files_mtime  ON files(mtime);

-- NOT contentless: a `content=''` table cannot return its own columns, so
-- the join back to `files` silently yields nothing.
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
USING fts5(path, name);
"""

# Writes are the interesting provenance. A read tells you the agent looked;
# these tell you it changed something.
MUTATING = frozenset({"reversible", "compensable", "irreversible"})


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    name: str
    suffix: str
    size: int
    mtime: float
    last_seen: float
    exists_now: bool


@dataclass(frozen=True, slots=True)
class Touch:
    path: str
    ts: float
    session_id: str | None
    goal: str | None
    operation: str
    adapter: str
    tier: str
    effect_class: str
    status: str

    @property
    def mutating(self) -> bool:
        return self.effect_class in MUTATING


@dataclass(frozen=True, slots=True)
class Opening:
    """A file this runtime handed to an application."""

    path: str
    handler: str
    ts: float
    session_id: str | None
    goal: str | None


class MemoryStore:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because FileWatcher records from its own
        # thread. sqlite3 connections are thread-bound by default and raise
        # ProgrammingError otherwise -- which, inside a watcher loop that
        # catches broadly, looks exactly like a watcher that works and silently
        # records nothing. The lock is what makes relaxing that safe.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(_SCHEMA)
            self.db.commit()

    # -- thread-safe access ------------------------------------------------

    def _query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Read, fetching inside the lock so no cursor crosses a thread."""
        with self._lock:
            return self.db.execute(sql, tuple(params)).fetchall()

    def _write(self, sql: str, params: Sequence[object] = (), *, commit: bool = True) -> None:
        with self._lock:
            self.db.execute(sql, tuple(params))
            if commit:
                self.db.commit()

    def _commit(self) -> None:
        with self._lock:
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- indexing ----------------------------------------------------------

    def index_tree(
        self,
        root: Path | str,
        *,
        skip_hidden: bool = True,
        max_files: int = 100_000,
    ) -> int:
        """Scan a directory tree. Returns the number of files recorded.

        A scan rather than a watcher: portable, testable, and the watcher
        (inotify / FSEvents / ReadDirectoryChangesW) is an optimisation layered
        on this, not a different data path.
        """
        root_path = Path(root).expanduser().resolve()
        now = time.time()
        count = 0
        for path in _walk(root_path, skip_hidden=skip_hidden):
            if count >= max_files:
                break
            self.record_file(path, now=now)
            count += 1
        self._commit()
        return count

    def record_file(self, path: Path | str, *, now: float | None = None) -> None:
        resolved = Path(path).expanduser().resolve()
        now = now if now is not None else time.time()
        try:
            stat = resolved.stat()
            size, mtime, exists = stat.st_size, stat.st_mtime, 1
        except OSError:
            size, mtime, exists = 0, 0.0, 0

        self._write(
            """
            INSERT INTO files (path, name, suffix, size, mtime,
                               first_seen, last_seen, exists_now)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,
                mtime=excluded.mtime,
                last_seen=excluded.last_seen,
                exists_now=excluded.exists_now
            """,
            (
                str(resolved),
                resolved.name,
                resolved.suffix.lower(),
                size,
                mtime,
                now,
                now,
                exists,
            ),
        )
        self._write("DELETE FROM files_fts WHERE path = ?", (str(resolved),), commit=False)
        self._write(
            "INSERT INTO files_fts (path, name) VALUES (?, ?)",
            (str(resolved), _searchable(resolved)),
        )

    # -- provenance --------------------------------------------------------

    def start_session(self, session_id: str, goal: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO sessions (id, goal, started) VALUES (?, ?, ?)",
            (session_id, goal, time.time()),
        )

    def end_session(self, session_id: str) -> None:
        self._write("UPDATE sessions SET ended = ? WHERE id = ?", (time.time(), session_id))

    def record_outcome(
        self, outcome: Outcome, *, session_id: str | None = None, goal: str | None = None
    ) -> None:
        """Record what an admitted action touched.

        This is the difference between an mtime and a provenance: the store
        learns not just that a file changed, but which task changed it and why.
        """
        invocation = outcome.invocation
        contract = invocation.contract
        targets = contract.targets or tuple(
            Path(str(v)) for v in (invocation.request.params.get("path"),) if v
        )
        now = time.time()
        for target in targets:
            resolved = Path(target).expanduser().resolve()
            self.record_file(resolved, now=now)
            self._write(
                """
                INSERT INTO touches (path, ts, session_id, goal, operation,
                                     adapter, tier, effect_class, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(resolved),
                    now,
                    session_id,
                    goal,
                    invocation.request.operation,
                    invocation.adapter,
                    str(invocation.tier),
                    str(contract.effect_class),
                    outcome.status,
                ),
            )

    def record_open(
        self,
        path: Path | str,
        handler: str,
        *,
        session_id: str | None = None,
        goal: str | None = None,
    ) -> None:
        """Note that a file was opened in an application.

        This is what makes "the thing I had open" answerable for anything this
        runtime opened. It is deliberately not inferred from a touch: reading a
        file and opening it in a viewer are different events and conflating
        them would make the history lie.
        """
        resolved = Path(path).expanduser().resolve()
        self.record_file(resolved)
        self._write(
            "INSERT INTO opens (path, handler, ts, session_id, goal) VALUES (?, ?, ?, ?, ?)",
            (str(resolved), handler, time.time(), session_id, goal),
        )

    def openings(self, *, since: float | None = None, limit: int = 50) -> list[Opening]:
        sql = "SELECT * FROM opens WHERE 1=1"
        params: list[object] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return [
            Opening(
                path=row["path"],
                handler=row["handler"],
                ts=row["ts"],
                session_id=row["session_id"],
                goal=row["goal"],
            )
            for row in self._query(sql, params)
        ]

    def last_opened(self) -> Opening | None:
        found = self.openings(limit=1)
        return found[0] if found else None

    # -- queries -----------------------------------------------------------

    def files(self, *, suffixes: Sequence[str] | None = None) -> list[FileRecord]:
        sql = "SELECT * FROM files WHERE exists_now = 1"
        params: list[object] = []
        if suffixes:
            placeholders = ",".join("?" for _ in suffixes)
            sql += f" AND suffix IN ({placeholders})"
            params.extend(s.lower() for s in suffixes)
        sql += " ORDER BY mtime DESC"
        return [_file_record(row) for row in self._query(sql, params)]

    def search(self, text: str, *, limit: int = 50) -> list[FileRecord]:
        """FTS over path and filename. Returns [] rather than raising on bad syntax."""
        cleaned = _fts_query(text)
        if not cleaned:
            return []
        try:
            rows = self._query(
                """
                SELECT f.* FROM files_fts
                JOIN files f ON f.path = files_fts.path
                WHERE files_fts MATCH ? AND f.exists_now = 1
                ORDER BY f.mtime DESC LIMIT ?
                """,
                (cleaned, limit),
            )
        except sqlite3.OperationalError:
            return []
        return [_file_record(row) for row in rows]

    def touches(self, *, path: str | Path | None = None, since: float | None = None) -> list[Touch]:
        sql = "SELECT * FROM touches WHERE 1=1"
        params: list[object] = []
        if path is not None:
            sql += " AND path = ?"
            params.append(str(Path(path).expanduser().resolve()))
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC"
        return [_touch(row) for row in self._query(sql, params)]

    def recently_touched(
        self, *, since: float | None = None, mutating_only: bool = False
    ) -> list[Touch]:
        rows = self.touches(since=since)
        if mutating_only:
            rows = [t for t in rows if t.mutating]
        seen: set[str] = set()
        out: list[Touch] = []
        for touch in rows:
            if touch.path not in seen:
                seen.add(touch.path)
                out.append(touch)
        return out


# -- helpers ---------------------------------------------------------------


def _walk(root: Path, *, skip_hidden: bool) -> Iterator[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if skip_hidden and any(part.startswith(".") for part in path.parts):
            continue
        yield path


def _searchable(path: Path) -> str:
    """Split a path into words FTS can match.

    ``sales_2025.csv`` should be findable by ``sales`` and by ``2025``, so
    separators become spaces.
    """
    text = str(path)
    for ch in ("/", "\\", "_", "-", ".", ":"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


def _fts_query(text: str) -> str:
    """Bare OR-joined terms. User text never reaches FTS as syntax."""
    words = [w for w in _searchable(Path(text)).split() if len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words)


def _file_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        path=row["path"],
        name=row["name"],
        suffix=row["suffix"],
        size=row["size"],
        mtime=row["mtime"],
        last_seen=row["last_seen"],
        exists_now=bool(row["exists_now"]),
    )


def _touch(row: sqlite3.Row) -> Touch:
    return Touch(
        path=row["path"],
        ts=row["ts"],
        session_id=row["session_id"],
        goal=row["goal"],
        operation=row["operation"],
        adapter=row["adapter"],
        tier=row["tier"],
        effect_class=row["effect_class"],
        status=row["status"],
    )
