"""Computer-state memory.

Not conversation history: an index of what the computer was doing over time,
plus the provenance of every action this runtime admitted. That provenance is
what lets it answer "the Excel we were working on yesterday" with a path *and*
an explanation of how it decided.

Memory content is untrusted. Resolution informs planning; it never widens scope.
"""

from .resolve import Candidate, Resolution, resolve
from .store import FileRecord, MemoryStore, Opening, Touch
from .watch import Change, FileWatcher, scan

__all__ = [
    "Candidate",
    "Change",
    "FileRecord",
    "FileWatcher",
    "MemoryStore",
    "Opening",
    "Resolution",
    "Touch",
    "resolve",
    "scan",
]
