"""``minos doctor`` -- can this machine run fully offline?

Answers one question with evidence rather than encouragement: is everything
needed to run without a network present, and if not, exactly what is missing.

Recommendations are sized from detected VRAM, because the usual way people
conclude "local inference doesn't work" is pulling a model that does not fit.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MODELS", "Report", "diagnose", "recommend", "render"]

# Q4_K_M weight sizes in GB. Roughly 1GB of KV-cache headroom is subtracted
# from available VRAM before matching, so these are the raw weights.
MODELS: list[tuple[str, float, str]] = [
    ("qwen3:4b", 2.6, "short plans only; for 6 GB cards"),
    ("qwen2.5:7b-instruct", 4.7, "solid tool calling, widely available"),
    ("llama3.1:8b", 4.9, "good tool calling"),
    ("qwen3:8b", 5.0, "good tool calling -- the default"),
    ("qwen3:14b", 8.5, "noticeably better at longer plans"),
    ("qwen3:32b", 19.0, "strong, needs a 24 GB card"),
]

DEFAULT_URL = "http://localhost:11434/v1"


@dataclass
class Report:
    platform: str = sys.platform
    python: str = field(default_factory=lambda: sys.version.split()[0])
    gpus: list[tuple[str, float]] = field(default_factory=list)
    ram_gb: float = 0.0
    free_disk_gb: float = 0.0
    server_url: str = DEFAULT_URL
    server_up: bool = False
    installed_models: list[str] = field(default_factory=list)
    runner: str = ""

    @property
    def vram_gb(self) -> float:
        return max((vram for _, vram in self.gpus), default=0.0)

    @property
    def can_run_offline(self) -> bool:
        """The whole point of the command, as one boolean."""
        return self.server_up and bool(self.installed_models)

    def blockers(self) -> list[str]:
        problems: list[str] = []
        # A responding server settles the question. Reporting "no runner found"
        # while one is demonstrably serving is how a working setup gets
        # mistaken for a broken one.
        if not self.runner and not self.server_up:
            problems.append(
                "No local runner found. Install Ollama (https://ollama.com), or "
                "llama.cpp's llama-server -- anything speaking OpenAI-compatible "
                "/v1/chat/completions with tool calling will do."
            )
        elif not self.server_up:
            problems.append(
                f"{self.runner} is installed but nothing is serving at "
                f"{self.server_url}. Start it:  ollama serve"
            )
        if self.server_up and not self.installed_models:
            fits = recommend(self.vram_gb)
            suggestion = fits[0][0] if fits else "qwen3:4b"
            problems.append(f"The server has no models. Pull one:  ollama pull {suggestion}")
        if self.free_disk_gb and self.free_disk_gb < 15:
            problems.append(
                f"Only {self.free_disk_gb:.0f} GB free. A model is ~5 GB and "
                "checkpointing needs headroom by design."
            )
        return problems


def recommend(vram_gb: float) -> list[tuple[str, float, str]]:
    """Models that fit in VRAM, largest first. Empty when none do."""
    usable = max(0.0, vram_gb - 1.0)  # KV cache
    return sorted((m for m in MODELS if m[1] <= usable), key=lambda m: m[1], reverse=True)


# -- probes ----------------------------------------------------------------


def _nvidia_gpus() -> list[tuple[str, float]]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    gpus: list[tuple[str, float]] = []
    for line in completed.stdout.strip().splitlines():
        name, _, mib = line.partition(",")
        try:
            gpus.append((name.strip(), round(float(mib.strip()) / 1024, 1)))
        except ValueError:
            continue
    return gpus


def _ram_gb() -> float:
    if hasattr(os, "sysconf"):
        try:
            total = float(os.sysconf("SC_PAGE_SIZE")) * float(os.sysconf("SC_PHYS_PAGES"))
            return round(total / 1024**3, 1)
        except (ValueError, OSError):
            pass

    if sys.platform == "win32":
        try:

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(float(status.ullTotalPhys) / 1024**3, 1)
        except (AttributeError, OSError):
            return 0.0
    return 0.0


def _installed_models(base_url: str) -> list[str]:
    try:
        request = urllib.request.Request(f"{base_url.rstrip('/')}/models", method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    # `.get(key, default)` returns a stored None rather than the default, and
    # Ollama sends {"data": null} when it has no models pulled yet.
    entries = (data.get("data") or []) if isinstance(data, dict) else []
    return sorted(str(m.get("id", "")) for m in entries if isinstance(m, dict) and m.get("id"))


def _find_runner() -> str:
    r"""Locate a local runner.

    PATH alone is not enough: a Windows installer drops Ollama in
    %LOCALAPPDATA%\Programs and an already-open shell will not have picked it
    up, so a freshly installed and actively serving runner looks absent.
    """
    for runner in ("ollama", "llama-server", "lms"):
        if shutil.which(runner):
            return runner

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Ollama" / "ollama.exe",
        Path("/usr/local/bin/ollama"),
        Path("/opt/homebrew/bin/ollama"),
        Path.home() / ".local" / "bin" / "ollama",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return "ollama"
        except OSError:
            continue
    return ""


def diagnose(base_url: str = DEFAULT_URL) -> Report:
    from .planner.local import server_available

    report = Report(server_url=base_url)
    report.gpus = _nvidia_gpus()
    report.ram_gb = _ram_gb()
    with contextlib.suppress(OSError):
        report.free_disk_gb = round(shutil.disk_usage(Path.cwd()).free / 1024**3, 1)

    report.runner = _find_runner()

    report.server_up = server_available(base_url)
    if report.server_up:
        report.installed_models = _installed_models(base_url)
    return report


# -- output ----------------------------------------------------------------


def _sandbox_section() -> list[str]:
    """What the sandbox can import, and what it cannot.

    The sandbox has no network by design, so the set of usable packages is
    decided before a task runs rather than during it. Someone whose PDF
    conversion just failed needs to see this, not go looking for it.
    """
    from .sandbox import survey

    statuses = survey()
    have = [s for s in statuses if s.available]
    missing = [s for s in statuses if not s.available]

    lines = ["", f"  sandbox    : offline, {len(have)}/{len(statuses)} recommended packages"]
    if have:
        lines.append(f"    available: {', '.join(sorted(s.module for s in have))}")
    if missing:
        # Three is enough to make the point without turning doctor into a wall.
        shown = ", ".join(s.distribution for s in missing[:6])
        more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
        lines.append(f"    missing  : {shown}{more}")
        lines.append(f"    install  : uv pip install {' '.join(s.distribution for s in missing)}")
    return lines


def render(report: Report) -> str:
    lines = ["", "minos doctor -- can this machine run fully offline?", "=" * 62, ""]
    lines.append(f"  platform   : {report.platform}  (python {report.python})")
    for name, vram in report.gpus or [("no NVIDIA GPU detected", 0.0)]:
        lines.append(f"  gpu        : {name}" + (f"  --  {vram} GB VRAM" if vram else ""))
    if report.ram_gb:
        lines.append(f"  ram        : {report.ram_gb} GB")
    if report.free_disk_gb:
        lines.append(f"  free disk  : {report.free_disk_gb} GB")
    lines.append(f"  runner     : {report.runner or 'none found'}")
    lines.append(
        f"  server     : {'up' if report.server_up else 'not reachable'} at {report.server_url}"
    )
    if report.installed_models:
        lines.append(f"  models     : {', '.join(report.installed_models)}")

    lines += _sandbox_section()

    lines += [
        "",
        "-" * 62,
        "",
        f"  FULLY OFFLINE: {'YES' if report.can_run_offline else 'NOT YET'}",
    ]

    blockers = report.blockers()
    if blockers:
        lines.append("")
        for blocker in blockers:
            lines.append(f"    - {blocker}")

    if report.vram_gb:
        fits = recommend(report.vram_gb)
        lines += ["", f"  Models that fit {report.vram_gb} GB of VRAM at full speed:"]
        if fits:
            for name, size, note in fits:
                installed = "   [installed]" if name in report.installed_models else ""
                lines.append(f"    {name:<24} ~{size} GB   {note}{installed}")
        else:
            lines.append("    none at full GPU speed -- see CPU offload below")

    lines += [
        "",
        "  Bigger models, slower, still fully local:",
        "",
        "    llama.cpp keeps most weights in system RAM and still serves an",
        "    OpenAI-compatible endpoint, which this runtime already speaks:",
        "",
        "      llama-server -m qwen3-32b-q4_k_m.gguf --n-gpu-layers 20 --port 11434",
        "",
        "    Expect single-digit tokens/sec. That is usable here, because a tool",
        "    call is short -- unlike GUI agents, which need hundreds of steps.",
        "",
        "  Not recommended: AirLLM-style per-layer streaming. It exists to run a",
        "  70B on 4 GB at 0.5-2 tok/s, which turns a five-step task into an hour.",
        "  An 8B that fits in VRAM does this job better and roughly 100x faster.",
        "",
    ]
    return "\n".join(lines)
