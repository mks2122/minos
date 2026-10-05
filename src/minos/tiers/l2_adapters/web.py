"""L2 web adapter -- drive a web page by its controls, not its pixels.

The GUI tier reaches a website the way a person with a mouse does: find the
control on screen, click its coordinate, hope nothing moved. That is the
OSWorld problem, and it is the one a small planner is worst at. This adapter
turns it back into tool calling.

A page is read into a list of its controls, each with a reference::

    e12 button "Start a post"
    e13 textbox "Search"

and every action names a reference. ``web.click(ref="e12")`` resolves the
reference in the live DOM at execute time and clicks *that element* -- there
is no coordinate to get wrong, and the person's mouse and keyboard are never
touched, so they can keep working while it runs.

Which browser
-------------

Playwright drives a Chromium the runtime launched itself, over a pipe, with
its **own** profile (``~/.minos/browser`` by default). Its sign-ins persist
there, so a site is signed into once, by the person, in that window -- the
planner is told to ask rather than type a password. Chrome or Edge is used
when installed (no download needed), Playwright's bundled Chromium otherwise.

``MINOS_CDP_URL`` attaches to a browser the person started themselves with
``--remote-debugging-port`` instead. That is their choice to make: a debugging
port lets any local process drive that browser, which is why it is never the
default.

Why not an MCP server
---------------------

The Playwright MCP server offers the same snapshot-and-ref model. Routing
through it would put an opaque tool between the broker and the effect: the
broker can only admit what an adapter can *declare*, and "call tool X on
server Y" declares nothing. A typed adapter says which site, which control,
and how reversible -- which is what lets "Post" stop for a person while
"Start a post" does not.

Classification
--------------

``web.open``, ``web.snapshot`` and ``web.read`` are ``PURE``: reads of a page
the task may open. ``web.click``, ``web.fill`` and ``web.press`` are
``IRREVERSIBLE``: a page has no checkpoint, and nothing here can undo a sent
message. The control's name is put in the contract, so the approver stops on
commit controls (Post, Send, Delete...) and lets the rest through.

The honest limit: a control's name comes from the page, and a hostile page can
mislabel its buttons. The same is true of the accessibility tree the GUI tier
reads. What the page cannot do is move the action to a different site -- the
host is checked against the grant when the action runs, not only when it was
planned.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...oracles import NullOracle
from ...types import ActionRequest, EffectClass, EffectContract, Grant, Invocation, Tier
from ..base import CapabilityManifest, OperationUnsupported, Preparation

# One definition of "a web page" and of "which site": file:, javascript: and
# chrome:// are refused the same way here as by browser.open, and a grant for
# linkedin.com means the same host to both.
from ..l1_system.browser import _normalised_url, site_of

__all__ = [
    "PlaywrightDriver",
    "StaleReference",
    "WebAdapter",
    "WebDriver",
    "playwright_available",
]

_READS = ("web.open", "web.snapshot", "web.read")
_ACTS = ("web.click", "web.fill", "web.press")

_MAX_ELEMENTS = 60
"""Controls listed per snapshot. Enough for a dialog and the visible part of a
page; a feed's three hundred links would evict the tool schemas from a small
model's context, and it would act on none of them."""

_SNAPSHOT_TEXT = 1200
_READ_TEXT = 6000


class StaleReference(Exception):
    """A reference from an older snapshot, or one the page has since removed."""


def playwright_available() -> bool:
    """Whether the optional dependency is installed. Never imports it."""
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


# -- the driver ------------------------------------------------------------


class WebDriver(Protocol):
    """What the adapter needs from a browser. Faked in the tests."""

    def started(self) -> bool: ...
    def url(self) -> str: ...
    def open(self, url: str) -> None: ...
    def snapshot(self, first_ref: int, limit: int, text_limit: int) -> dict[str, Any]: ...
    def read(self, limit: int) -> dict[str, Any]: ...
    def label(self, ref: str) -> str | None: ...
    def click(self, ref: str) -> None: ...
    def click_text(self, text: str) -> str: ...
    def fill(self, ref: str, text: str) -> str: ...
    def press(self, key: str, ref: str | None) -> None: ...
    def close(self) -> None: ...


# Runs in the page. Tags each visible control with data-minos-ref so a later
# action can find exactly it, and describes it the way a screen reader would.
# When a dialog is open its controls come first: that is where the next action
# almost always is, and a page behind a modal cannot be clicked anyway.
_SNAPSHOT_JS = r"""
([first, limit, textLimit]) => {
  const SEL = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select', 'summary',
    '[role=button]', '[role=link]', '[role=textbox]', '[role=menuitem]', '[role=tab]',
    '[role=checkbox]', '[role=radio]', '[role=combobox]', '[role=option]', '[role=switch]',
    '[contenteditable=""]', '[contenteditable="true"]'
  ].join(',');
  for (const el of document.querySelectorAll('[data-minos-ref]')) {
    el.removeAttribute('data-minos-ref');
  }
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const inView = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight ? 1 : 0;
  };
  const label = (el) => {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = clean(by.split(/\s+/).map((id) => {
        const node = document.getElementById(id);
        return node ? node.innerText : '';
      }).join(' '));
      if (t) return t;
    }
    const own = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    return clean(
      el.getAttribute('aria-label') || own || el.innerText ||
      (el.type === 'password' ? '' : el.value) ||
      el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') ||
      el.getAttribute('data-placeholder') || el.title || el.getAttribute('alt') || ''
    );
  };
  const roleOf = (el) => {
    const role = el.getAttribute('role');
    if (role) return role;
    if (el.isContentEditable) return 'textbox';
    const tag = el.tagName;
    if (tag === 'A') return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return t;
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      return 'textbox';
    }
    return tag.toLowerCase();
  };
  const dialogs = [...document.querySelectorAll(
    '[role=dialog],[role=alertdialog],dialog[open],[aria-modal=true]'
  )].filter(visible);
  const root = dialogs.length ? dialogs[dialogs.length - 1] : document.body;
  const found = [...root.querySelectorAll(SEL)].filter(visible);
  found.sort((a, b) => inView(b) - inView(a));
  const elements = [];
  let n = first;
  for (const el of found) {
    if (elements.length >= limit) break;
    const role = roleOf(el);
    const name = label(el).slice(0, 80);
    if (!name && role !== 'textbox') continue;
    const ref = 'e' + n++;
    el.setAttribute('data-minos-ref', ref);
    const item = { ref, role, name };
    if (role === 'textbox' && el.type !== 'password') {
      const v = el.isContentEditable ? el.innerText : el.value;
      if (clean(v)) item.value = clean(v).slice(0, 80);
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    elements.push(item);
  }
  return {
    url: location.href,
    title: document.title,
    dialog: dialogs.length > 0,
    elements,
    more: Math.max(found.length - elements.length, 0),
    next: n,
    text: clean(root.innerText).slice(0, textLimit),
  };
}
"""

_LABEL_JS = r"""
(el) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const by = el.getAttribute('aria-labelledby');
  if (by) {
    const t = clean(by.split(/\s+/).map((id) => {
      const node = document.getElementById(id);
      return node ? node.innerText : '';
    }).join(' '));
    if (t) return t.slice(0, 80);
  }
  const own = el.labels && el.labels[0] ? el.labels[0].innerText : '';
  return clean(
    el.getAttribute('aria-label') || own || el.innerText ||
    (el.type === 'password' ? '' : el.value) ||
    el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') ||
    el.getAttribute('data-placeholder') || el.title || el.getAttribute('alt') || ''
  ).slice(0, 80);
}
"""

_KEY_NAMES = {
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
    "cmd": "Meta",
    "meta": "Meta",
    "win": "Meta",
    "enter": "Enter",
    "return": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "home": "Home",
    "end": "End",
}


def playwright_key(chord: str) -> str:
    """``ctrl+enter`` -> ``Control+Enter``, which is what Playwright accepts."""
    parts = [p.strip() for p in str(chord).split("+") if p.strip()]
    named = [_KEY_NAMES.get(p.lower(), p if len(p) == 1 else p[:1].upper() + p[1:]) for p in parts]
    return "+".join(named)


@dataclass
class PlaywrightDriver:
    """A browser the runtime launched, with its own persistent profile.

    Started lazily, on the first page the task opens: a run that never touches
    the web never starts a browser. Every call happens on the thread that
    started it, which is the agent's -- Playwright's sync API requires that.
    """

    profile: Path
    cdp_url: str = ""
    headless: bool = False
    settle: float = 0.8
    """Seconds to let a single-page app react after an action. Dialogs on
    sites like LinkedIn render a beat after the click that opens them."""

    _playwright: Any = field(default=None, init=False, repr=False)
    _context: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)
    _page: Any = field(default=None, init=False, repr=False)

    def started(self) -> bool:
        return self._page is not None

    def _ensure(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._context is not None:
            # The person closed the tab; the context is still there.
            pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = pages[-1] if pages else self._context.new_page()
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "the web tier needs Playwright: uv sync --extra web "
                "(and, without Chrome or Edge installed: uv run playwright install chromium)"
            ) from exc

        self._playwright = sync_playwright().start()
        chromium = self._playwright.chromium
        if self.cdp_url:
            self._browser = chromium.connect_over_cdp(self.cdp_url)
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else self._browser.new_context()
        else:
            self.profile.mkdir(parents=True, exist_ok=True)
            failures: list[str] = []
            for channel in ("chrome", "msedge", None):
                try:
                    self._context = chromium.launch_persistent_context(
                        str(self.profile),
                        channel=channel,
                        headless=self.headless,
                        no_viewport=True,
                        args=["--no-first-run", "--no-default-browser-check"],
                    )
                    break
                except Exception as exc:
                    failures.append(f"{channel or 'bundled chromium'}: {_first_line(exc)}")
            if self._context is None:
                self._playwright.stop()
                self._playwright = None
                raise RuntimeError(
                    "could not start a browser for the web tier ("
                    + "; ".join(failures)
                    + "). Install Chrome or Edge, or run: uv run playwright install chromium"
                )
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[0] if pages else self._context.new_page()
        return self._page

    def url(self) -> str:
        return str(self._page.url) if self.started() and not self._page.is_closed() else ""

    def open(self, url: str) -> None:
        page = self._ensure()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        self._settle(page)

    def snapshot(self, first_ref: int, limit: int, text_limit: int) -> dict[str, Any]:
        page = self._ensure()
        result: dict[str, Any] = page.evaluate(_SNAPSHOT_JS, [first_ref, limit, text_limit])
        return result

    def read(self, limit: int) -> dict[str, Any]:
        page = self._ensure()
        text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
        return {"url": page.url, "title": page.title(), "text": " ".join(text.split())[:limit]}

    def _locate(self, ref: str) -> Any:
        page = self._ensure()
        locator = page.locator(f'[data-minos-ref="{ref}"]')
        if locator.count() == 0:
            raise StaleReference(
                f"{ref} is not on the page any more (it navigated or re-rendered). "
                "Call web_snapshot for fresh references."
            )
        return locator.first

    def label(self, ref: str) -> str | None:
        try:
            return str(self._locate(ref).evaluate(_LABEL_JS))
        except StaleReference:
            return None

    def click(self, ref: str) -> None:
        self._locate(ref).click(timeout=10_000)
        self._settle(self._page)

    def click_text(self, text: str) -> str:
        """Click the one control whose accessible name is exactly ``text``."""
        page = self._ensure()
        for role in ("button", "link", "menuitem", "tab", "option"):
            matches = page.get_by_role(role, name=text, exact=True)
            count = matches.count()
            if count > 1:
                raise ValueError(
                    f"{count} {role}s are named {text!r}; call web_snapshot and click by ref"
                )
            if count == 1:
                matches.first.click(timeout=10_000)
                self._settle(page)
                return role
        raise ValueError(f"no button or link is named {text!r}; call web_snapshot to see the names")

    def fill(self, ref: str, text: str) -> str:
        element = self._locate(ref)
        try:
            element.fill(text, timeout=10_000)
        except Exception:
            # Some rich editors refuse fill(); typing into them after focusing
            # is what a person does, and what they accept.
            element.click(timeout=10_000)
            self._page.keyboard.insert_text(text)
        self._settle(self._page)
        value = element.evaluate(
            "(el) => el.isContentEditable ? el.innerText : (el.value ?? el.innerText ?? '')"
        )
        return str(value or "")

    def press(self, key: str, ref: str | None) -> None:
        if ref:
            self._locate(ref).press(playwright_key(key), timeout=10_000)
        else:
            self._ensure().keyboard.press(playwright_key(key))
        self._settle(self._page)

    def _settle(self, page: Any) -> None:
        # A slow page is not a failed action.
        with contextlib.suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=5_000)
        if self.settle > 0:
            time.sleep(self.settle)

    def close(self) -> None:
        for closing in (self._context if not self.cdp_url else None, self._browser):
            if closing is not None:
                with contextlib.suppress(Exception):
                    closing.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                self._playwright.stop()
        self._playwright = self._context = self._browser = self._page = None


def default_profile() -> Path:
    configured = os.environ.get("MINOS_BROWSER_PROFILE", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".minos" / "browser"


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:160] if text else type(exc).__name__


# -- the adapter -----------------------------------------------------------


@dataclass
class WebAdapter:
    """Pages read as lists of named controls; actions name a control."""

    driver: WebDriver
    max_elements: int = _MAX_ELEMENTS

    _next_ref: int = field(default=1, init=False)
    """References are numbered across the whole session, never reused. A
    stale ``e5`` must fail, not silently mean whatever is fifth on the next
    page."""
    _names: dict[str, str] = field(default_factory=dict, init=False)

    manifest = CapabilityManifest(
        adapter="l2.web",
        tier=Tier.L2_ADAPTER,
        operations=(*_READS, *_ACTS),
        summary="Drive web pages by their controls, in a browser the runtime owns",
    )

    @classmethod
    def launching(cls, profile: Path | None = None, cdp_url: str = "") -> WebAdapter:
        return cls(
            driver=PlaywrightDriver(
                profile=profile or default_profile(),
                cdp_url=cdp_url or os.environ.get("MINOS_CDP_URL", "").strip(),
            )
        )

    def close(self) -> None:
        self.driver.close()

    def prepare(self, request: ActionRequest) -> Preparation:
        op = request.operation
        params = request.params
        if op == "web.open":
            raw = params.get("url")
            if not raw:
                raise OperationUnsupported("web.open requires 'url'")
            url = _normalised_url(str(raw))
            return self._pure(
                "browser.open",
                site_of(url),
                f"open {url}",
                lambda: self._opened(url),
            )

        site = self._current_site(op)
        if op == "web.snapshot":
            return self._pure("browser.open", site, f"list the controls on {site}", self._snap)
        if op == "web.read":
            return self._pure(
                "browser.open",
                site,
                f"read the text of {site}",
                lambda: self.driver.read(_READ_TEXT),
            )
        if op == "web.click":
            return self._click(params, site)
        if op == "web.fill":
            return self._fill(params, site)
        if op == "web.press":
            return self._press(params, site)
        raise OperationUnsupported(op)

    # -- reads -------------------------------------------------------------

    def _pure(
        self, capability: str, site: str, expect: str, run: Callable[[], dict[str, Any]]
    ) -> Preparation:
        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                oracle=NullOracle(reason="reading a web page; there is nothing to verify"),
                expect=expect,
            ),
            execute=lambda _: run(),
            grants=(Grant(capability, site),),
        )

    def _opened(self, url: str) -> dict[str, Any]:
        self.driver.open(url)
        return self._snap()

    def _snap(self) -> dict[str, Any]:
        raw = self.driver.snapshot(self._next_ref, self.max_elements, _SNAPSHOT_TEXT)
        self._next_ref = max(int(raw.get("next", self._next_ref)), self._next_ref)
        # Only the latest snapshot's names count: the older references were
        # stripped from the page by this one.
        self._names = {}
        lines: list[str] = []
        for item in raw.get("elements") or []:
            ref, role, name = str(item["ref"]), str(item["role"]), str(item.get("name", ""))
            self._names[ref] = name
            line = f'{ref} {role} "{name}"'
            if item.get("value"):
                line += f' value="{item["value"]}"'
            if item.get("disabled"):
                line += " (disabled)"
            lines.append(line)
        result: dict[str, Any] = {
            "url": raw.get("url", ""),
            "title": raw.get("title", ""),
            "controls": lines or ["(no controls found -- the page may still be loading)"],
        }
        if raw.get("dialog"):
            result["note"] = "a dialog is open; its controls are listed"
        if raw.get("more"):
            result["more"] = f"{raw['more']} more controls not listed"
        if raw.get("text"):
            result["page_text"] = raw["text"]
        return result

    # -- actions -----------------------------------------------------------

    def _ref(self, params: dict[str, Any]) -> str:
        ref = str(params.get("ref") or "").strip()
        return ref if ref.startswith("e") and ref[1:].isdigit() else ""

    def _click(self, params: dict[str, Any], site: str) -> Preparation:
        ref = self._ref(params)
        text = str(params.get("text") or params.get("element") or "").strip()
        if not ref and not text:
            raise OperationUnsupported("web.click requires 'ref' (from web_snapshot) or 'text'")
        name = self._names.get(ref, "") if ref else text

        def click() -> dict[str, Any]:
            if ref:
                self._unchanged(ref, name)
                self.driver.click(ref)
            else:
                self.driver.click_text(text)
            return self._snap()

        what = f"{ref} {name!r}" if ref else repr(text)
        return self._acting(site, name, f"click {what} on {site}", click)

    def _fill(self, params: dict[str, Any], site: str) -> Preparation:
        ref = self._ref(params)
        text = params.get("text")
        if not ref or text is None:
            raise OperationUnsupported("web.fill requires 'ref' (from web_snapshot) and 'text'")
        name = self._names.get(ref, "")
        content = str(text)

        def fill() -> dict[str, Any]:
            self._unchanged(ref, name)
            value = self.driver.fill(ref, content)
            # The field read back is the evidence; a page with no system of
            # record has nothing better. A mismatch is a failure the planner
            # sees, not a success it assumes.
            if " ".join(content.split()) not in " ".join(value.split()):
                raise RuntimeError(
                    f"{ref} does not hold the text afterwards (it reads {value[:120]!r}); "
                    "the control may not accept typing -- call web_snapshot and pick a textbox"
                )
            return {"filled": ref, "control": name, "now_reads": value[:300]}

        return self._acting(site, "", f"put {len(content)} characters into {ref} {name!r}", fill)

    def _press(self, params: dict[str, Any], site: str) -> Preparation:
        key = str(params.get("key") or params.get("chord") or "").strip()
        if not key:
            raise OperationUnsupported("web.press requires 'key', e.g. 'Enter' or 'ctrl+enter'")
        ref = self._ref(params) or None
        name = self._names.get(ref, "") if ref else ""

        def press() -> dict[str, Any]:
            if ref:
                self._unchanged(ref, name)
            self.driver.press(key, ref)
            return self._snap()

        where = f" in {ref} {name!r}" if ref else ""
        return self._acting(site, "", f"press {key}{where} on {site}", press)

    def _acting(
        self, site: str, control: str, expect: str, run: Callable[[], dict[str, Any]]
    ) -> Preparation:
        def execute(_: Invocation) -> dict[str, Any]:
            # Checked when the input is sent, not only when it was planned: a
            # click that navigated away between the two must not land on a
            # site the grant never named.
            now = site_of(self.driver.url())
            if now != site:
                raise RuntimeError(
                    f"the page is on {now or 'nothing'} now, not {site}; nothing was sent"
                )
            return run()

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.IRREVERSIBLE,
                oracle=NullOracle(reason="a web page has no system of record this runtime reads"),
                expect=expect + "; a page has no checkpoint, so this cannot be undone here",
                control=control,
            ),
            execute=execute,
            grants=(Grant("web.input", site),),
        )

    def _unchanged(self, ref: str, name: str) -> None:
        """Refuse when the control at ``ref`` is no longer the one that was approved."""
        now = self.driver.label(ref)
        if now is None:
            raise StaleReference(
                f"{ref} is not on the page any more. Call web_snapshot for fresh references."
            )
        if name and now != name:
            raise StaleReference(
                f"{ref} is now {now!r}, not {name!r}; nothing was done. Call web_snapshot."
            )

    def _current_site(self, op: str) -> str:
        if not self.driver.started():
            raise OperationUnsupported(f"{op}: no page is open yet -- call web_open first")
        site = site_of(self.driver.url())
        if not site:
            raise OperationUnsupported(f"{op}: the browser is not on a web page -- call web_open")
        return site
