"""L1 browser adapter -- open a web page in the person's own browser.

Without it the only way to a website is the GUI tier: Win+R, type a URL,
press Enter, then guess what the new window is called. Every one of those is
an input the runtime cannot verify, and the last is where runs went wrong.

This asks the browser directly. ``chrome --profile-directory=<dir> <url>``
(and its equivalents) opens the page as a tab in that profile's existing
window when one is open, or starts it otherwise -- so the page arrives signed
in as whoever that profile is, with no clicking.

Which browser, which profile
----------------------------

A window title does not say which profile it belongs to, so "the browser that
is already open" is not something this runtime can identify. Profiles are read
from each installed browser's own records instead -- ``Local State`` for the
Chromium family (Chrome, Edge, Brave, Vivaldi, Opera), ``profiles.ini`` for
Firefox -- and the choice is **per site**: the first time a site is opened the
person is asked, and the answer is kept in memory so the next run goes
straight there. The planner may name a browser or profile, but only a
person's answer is remembered -- a page cannot plant one.

Classification
--------------

``PURE``: opening a page is a read of it. The honest limit is that a GET can
have side effects on a badly built site, and the tab outlives the action.
What happens *in* the page afterwards goes through the GUI tier, where commit
controls still stop for a person.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ...types import ActionRequest, EffectClass, EffectContract, Grant, Invocation, Tier
from ..base import CapabilityManifest, OperationUnsupported, Preparation

__all__ = [
    "BROWSERS",
    "Ask",
    "BrowserAdapter",
    "BrowserProfile",
    "BrowserSpec",
    "installed_profiles",
    "site_of",
]

Ask = Callable[[str, Sequence[str]], "str | None"]
"""Put a question to the person: ``(question, choices) -> answer``, or None
when no one is there to answer."""

_SCHEMES = ("http", "https")
_BARE_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")


class Preferences(Protocol):
    def preference(self, key: str) -> str | None: ...
    def remember(self, key: str, value: str) -> None: ...


# -- the browsers ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserSpec:
    key: str
    """Stable id, stored in memory: ``chrome``, ``edge``..."""
    name: str
    """What the person calls it, and what its window titles end with."""
    kind: str
    """``chromium`` (Local State, ``--profile-directory``) or ``firefox``."""
    executables: tuple[str, ...]
    """Candidate paths, with ``%ENV%`` markers, tried in order."""
    commands: tuple[str, ...]
    """Names to look up on PATH when no candidate path exists."""
    data: str
    """The profile store: a Chromium ``User Data`` dir, or Firefox's root."""
    title: str = ""
    """Window-title suffix, when it differs from ``name``."""
    profiles: bool = True
    """Whether it takes a profile flag. Opera keeps a ``Local State`` but has
    one profile and no ``--profile-directory``."""

    def window_suffix(self) -> str:
        return self.title or self.name


_WIN = sys.platform == "win32"
_MAC = sys.platform == "darwin"


def _spec(
    key: str,
    name: str,
    *,
    win: tuple[str, ...],
    mac: tuple[str, ...],
    commands: tuple[str, ...],
    data_win: str,
    data_mac: str,
    data_linux: str,
    kind: str = "chromium",
    title: str = "",
    profiles: bool = True,
) -> BrowserSpec:
    return BrowserSpec(
        key=key,
        name=name,
        kind=kind,
        executables=win if _WIN else mac if _MAC else (),
        commands=commands,
        data=data_win if _WIN else data_mac if _MAC else data_linux,
        title=title,
        profiles=profiles,
    )


_PF = ("%PROGRAMFILES%", "%PROGRAMFILES(X86)%", "%LOCALAPPDATA%")

BROWSERS: tuple[BrowserSpec, ...] = (
    _spec(
        "chrome",
        "Google Chrome",
        win=tuple(f"{b}\\Google\\Chrome\\Application\\chrome.exe" for b in _PF),
        mac=("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",),
        commands=("chrome", "google-chrome", "google-chrome-stable"),
        data_win="%LOCALAPPDATA%\\Google\\Chrome\\User Data",
        data_mac="~/Library/Application Support/Google/Chrome",
        data_linux="~/.config/google-chrome",
    ),
    _spec(
        "edge",
        "Microsoft Edge",
        win=tuple(f"{b}\\Microsoft\\Edge\\Application\\msedge.exe" for b in _PF),
        mac=("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",),
        commands=("msedge", "microsoft-edge", "microsoft-edge-stable"),
        data_win="%LOCALAPPDATA%\\Microsoft\\Edge\\User Data",
        data_mac="~/Library/Application Support/Microsoft Edge",
        data_linux="~/.config/microsoft-edge",
        # Edge titles read "Page - Profile 1 - Microsoft Edge"; the suffix holds.
    ),
    _spec(
        "brave",
        "Brave",
        win=tuple(f"{b}\\BraveSoftware\\Brave-Browser\\Application\\brave.exe" for b in _PF),
        mac=("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",),
        commands=("brave", "brave-browser"),
        data_win="%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\User Data",
        data_mac="~/Library/Application Support/BraveSoftware/Brave-Browser",
        data_linux="~/.config/BraveSoftware/Brave-Browser",
    ),
    _spec(
        "vivaldi",
        "Vivaldi",
        win=("%LOCALAPPDATA%\\Vivaldi\\Application\\vivaldi.exe",),
        mac=("/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",),
        commands=("vivaldi", "vivaldi-stable"),
        data_win="%LOCALAPPDATA%\\Vivaldi\\User Data",
        data_mac="~/Library/Application Support/Vivaldi",
        data_linux="~/.config/vivaldi",
    ),
    _spec(
        "opera",
        "Opera",
        win=("%LOCALAPPDATA%\\Programs\\Opera\\opera.exe",),
        mac=("/Applications/Opera.app/Contents/MacOS/Opera",),
        commands=("opera",),
        data_win="%APPDATA%\\Opera Software\\Opera Stable",
        data_mac="~/Library/Application Support/com.operasoftware.Opera",
        data_linux="~/.config/opera",
        profiles=False,
    ),
    _spec(
        "firefox",
        "Mozilla Firefox",
        kind="firefox",
        win=tuple(f"{b}\\Mozilla Firefox\\firefox.exe" for b in _PF[:2]),
        mac=("/Applications/Firefox.app/Contents/MacOS/firefox",),
        commands=("firefox",),
        data_win="%APPDATA%\\Mozilla\\Firefox",
        data_mac="~/Library/Application Support/Firefox",
        data_linux="~/.mozilla/firefox",
    ),
)


def _expand(path: str) -> Path | None:
    expanded = os.path.expanduser(os.path.expandvars(path))
    # An unset variable is left as "%NAME%"; that is not a path to try.
    return None if "%" in expanded or "$" in expanded else Path(expanded)


def _executable(spec: BrowserSpec) -> str | None:
    for candidate in spec.executables:
        path = _expand(candidate)
        if path is not None and path.is_file():
            return str(path)
    for command in spec.commands:
        found = shutil.which(command)
        if found:
            return found
    return None


# -- profiles --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    browser: BrowserSpec
    executable: str
    directory: str
    """What the browser's profile flag takes: ``Profile 1``, or a Firefox
    profile name. Empty for a browser with a single, unnamed profile."""
    name: str
    """What the person sees in the browser's profile menu."""
    last_used: bool = False

    @property
    def key(self) -> str:
        """How the choice is remembered: ``chrome|Profile 1``."""
        return f"{self.browser.key}|{self.directory}"

    def label(self) -> str:
        if not self.directory or self.name == self.directory:
            return f"{self.browser.name}: {self.name}"
        return f"{self.browser.name}: {self.name} ({self.directory})"

    def command(self, url: str) -> list[str]:
        if self.browser.kind == "firefox":
            flags = ["-P", self.directory] if self.directory else []
            return [self.executable, *flags, "-new-tab", url]
        flags = [f"--profile-directory={self.directory}"] if self.directory else []
        return [self.executable, *flags, url]


def _chromium_profiles(spec: BrowserSpec, root: Path, exe: str) -> list[BrowserProfile]:
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    info = (state.get("profile") or {}) if spec.profiles else {}
    cache = info.get("info_cache") or {}
    last = info.get("last_used")
    found = [
        BrowserProfile(
            browser=spec,
            executable=exe,
            directory=str(directory),
            name=str(meta.get("name") or directory),
            last_used=directory == last,
        )
        for directory, meta in cache.items()
    ]
    if not found:
        # Installed, never used a second profile (Opera, usually): one
        # profile, and passing no flag is what selects it.
        found = [BrowserProfile(browser=spec, executable=exe, directory="", name="default")]
    found.sort(key=lambda p: (p.directory not in ("", "Default"), p.name.lower()))
    return found


def _firefox_profiles(spec: BrowserSpec, root: Path, exe: str) -> list[BrowserProfile]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(root / "profiles.ini", encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    found = [
        BrowserProfile(
            browser=spec,
            executable=exe,
            directory=parser[section]["Name"],
            name=parser[section]["Name"],
            last_used=parser[section].get("Default") == "1",
        )
        for section in parser.sections()
        if section.startswith("Profile") and "Name" in parser[section]
    ]
    return found or [BrowserProfile(browser=spec, executable=exe, directory="", name="default")]


def installed_profiles(
    browsers: Sequence[BrowserSpec] = BROWSERS,
    *,
    locate: Callable[[BrowserSpec], str | None] = _executable,
    data_root: Callable[[BrowserSpec], Path | None] | None = None,
) -> list[BrowserProfile]:
    """Every profile of every browser that is actually installed."""
    found: list[BrowserProfile] = []
    for spec in browsers:
        exe = locate(spec)
        if exe is None:
            continue
        root = data_root(spec) if data_root else _expand(spec.data)
        if root is None:
            continue
        if spec.kind == "firefox":
            found.extend(_firefox_profiles(spec, root, exe))
        else:
            found.extend(_chromium_profiles(spec, root, exe))
    return found


def site_of(url: str) -> str:
    """The key a profile is remembered under: the host, without ``www.``."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _normalised_url(raw: str) -> str:
    text = raw.strip()
    if "://" not in text:
        # "javascript:..." and "data:..." have a scheme and no "//"; prefixing
        # them would turn them into a host. "site.com:8080" is a port, not one.
        if _BARE_SCHEME.match(text):
            raise OperationUnsupported(f"browser.open only opens http(s) pages, not {raw!r}")
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in _SCHEMES or not parts.hostname:
        # file:, javascript:, chrome:// and friends are not "open a web page".
        raise OperationUnsupported(f"browser.open only opens http(s) pages, not {raw!r}")
    return text


# -- the adapter -----------------------------------------------------------


class BrowserAdapter:
    """Open a URL in a browser profile chosen once per site by the person."""

    manifest = CapabilityManifest(
        adapter="l1.browser",
        tier=Tier.L1_SYSTEM,
        operations=("browser.open",),
        summary="Open a web page in the person's browser, in a remembered profile",
    )

    def __init__(
        self,
        preferences: Preferences | None = None,
        ask: Ask | None = None,
        *,
        profiles: Callable[[], list[BrowserProfile]] = installed_profiles,
        launch: Callable[[list[str]], None] | None = None,
        settle: float = 6.0,
    ) -> None:
        self.preferences = preferences
        self.ask = ask
        self._profiles = profiles
        self._launch = launch or _spawn
        self.settle = settle
        """Seconds to wait for the tab's window to come to the front."""

    def prepare(self, request: ActionRequest) -> Preparation:
        if request.operation != "browser.open":
            raise OperationUnsupported(request.operation)
        raw = request.params.get("url")
        if not raw:
            raise OperationUnsupported("browser.open requires 'url'")
        url = _normalised_url(str(raw))
        site = site_of(url)
        requested = request.params.get("profile")

        def execute(_: Invocation) -> dict[str, Any]:
            profile, how = self._profile_for(site, str(requested) if requested else None)
            self._launch(profile.command(url))
            result: dict[str, Any] = {
                "opened": url,
                "browser": profile.browser.name,
                "profile": profile.name,
                "profile_chosen_by": how,
            }
            window = (
                _wait_for_front_window(profile.browser.window_suffix(), self.settle)
                if self.settle > 0
                else ""
            )
            if window:
                # The title the GUI tools need. Guessing it is how a run ends
                # up typing into a window that does not exist.
                result["window"] = window
            return result

        return Preparation(
            contract=EffectContract(effect_class=EffectClass.PURE, expect=f"open {url}"),
            execute=execute,
            grants=(Grant("browser.open", site),),
        )

    # -- profile choice ----------------------------------------------------

    def _profile_for(self, site: str, requested: str | None) -> tuple[BrowserProfile, str]:
        profiles = self._profiles()
        if not profiles:
            raise RuntimeError("no supported browser is installed where it can be found")

        if requested:
            match = _find(profiles, requested)
            if match is None:
                names = "; ".join(p.label() for p in profiles)
                raise ValueError(f"no browser profile matches {requested!r}; there are: {names}")
            return match, "named in the request"

        key = f"browser.profile:{site}"
        remembered = self.preferences.preference(key) if self.preferences else None
        if remembered:
            match = next((p for p in profiles if p.key == remembered), None)
            if match is not None:
                return match, f"remembered for {site}"

        if len(profiles) == 1:
            return profiles[0], "the only browser profile"

        if self.ask is not None:
            answer = self.ask(
                f"Which browser profile should be used for {site}? (remembered for next time)",
                [p.label() for p in profiles],
            )
            if answer:
                match = next((p for p in profiles if p.label() == answer), None) or _find(
                    profiles, answer
                )
                if match is None:
                    raise ValueError(f"{answer!r} is not one of the browser profiles")
                if self.preferences is not None:
                    self.preferences.remember(key, match.key)
                return match, f"chosen by the person, remembered for {site}"

        fallback = next((p for p in profiles if p.last_used), profiles[0])
        return fallback, f"{fallback.browser.name}'s last-used profile (no one to ask)"


def _find(profiles: Sequence[BrowserProfile], wanted: str) -> BrowserProfile | None:
    """Match "Karthick", "Profile 1", "edge", "Chrome: Karthick" -- only if unique."""
    lowered = wanted.strip().lower()
    browser, _, rest = lowered.partition(":")
    scoped = [
        p for p in profiles if rest and browser.strip() in (p.browser.key, p.browser.name.lower())
    ]
    pool, target = (scoped, rest.strip()) if scoped else (list(profiles), lowered)
    hits = [
        p
        for p in pool
        if target in (p.directory.lower(), p.name.lower(), p.label().lower())
        or (not scoped and target in (p.browser.key, p.browser.name.lower()))
    ]
    return hits[0] if len(hits) == 1 else None


def _spawn(args: list[str]) -> None:
    if _WIN:
        subprocess.Popen(
            args,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    else:
        subprocess.Popen(
            args,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _wait_for_front_window(suffix: str, timeout: float) -> str:
    """The foreground window's title once it is the browser's, or ''."""
    if not _WIN:
        return ""
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
    deadline = time.monotonic() + timeout
    title = ""
    while time.monotonic() < deadline:
        handle = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        title = buffer.value
        # A fresh tab is titled with its URL until the page names itself.
        if title.endswith(suffix) and "://" not in title and "Untitled" not in title:
            return title
        time.sleep(0.25)
    return title if title.endswith(suffix) else ""
