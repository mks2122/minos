"""The web tier against a real browser, on a page shaped like LinkedIn's composer.

Found on a real run: LinkedIn draws its post composer inside an open shadow
root on an overlay. The snapshot scanned the light DOM only, so the editor and
its Post button never appeared, and every click on the feed behind the overlay
timed out while Playwright scrolled and retried. Skipped where Playwright or a
browser is not available.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright.sync_api")

from minos.tiers.l2_adapters.web import CoveredByOverlay, PlaywrightDriver

PAGE = """<!doctype html><html><body>
<h1>Feed</h1>
<button id="start">Start a post</button>
<div id="interop-outlet" style="display:none"></div>
<script>
document.getElementById('start').onclick = () => {
  const host = document.getElementById('interop-outlet');
  host.style.cssText = 'display:block;position:fixed;inset:0;z-index:10;';
  const root = host.attachShadow({mode: 'open'});
  root.innerHTML = `
    <div role="dialog" aria-modal="true" style="background:#fff;margin:40px;padding:20px">
      <div contenteditable="true" aria-label="Text editor for creating content"
           style="min-height:80px;border:1px solid #999"></div>
      <button id="post">Post</button>
    </div>`;
  root.getElementById('post').onclick = () => {
    window.posted = root.querySelector('[contenteditable]').innerText;
  };
};
</script></body></html>"""


@pytest.fixture
def driver(tmp_path):
    page = tmp_path / "feed.html"
    page.write_text(PAGE, encoding="utf-8")
    browser = PlaywrightDriver(profile=tmp_path / "profile", headless=True, settle=0.2)
    try:
        browser.open(page.as_uri())
    except Exception as exc:  # no Chrome, Edge or bundled Chromium here
        pytest.skip(f"no browser to drive: {exc}")
    yield browser
    close = getattr(browser, "close", None)
    if callable(close):
        close()


def _open_composer(driver):
    start = next(e for e in driver.snapshot(1, 50, 400)["elements"] if e["name"] == "Start a post")
    driver.click(start["ref"])
    return driver.snapshot(100, 50, 400)


def test_controls_inside_a_shadow_root_are_seen(driver):
    snap = _open_composer(driver)
    assert snap["dialog"] is True
    roles = {(e["role"], e["name"]) for e in snap["elements"]}
    assert ("textbox", "Text editor for creating content") in roles
    assert ("button", "Post") in roles


def test_text_with_emoji_and_paragraphs_is_posted_from_inside_it(driver):
    snap = _open_composer(driver)
    editor = next(e for e in snap["elements"] if e["role"] == "textbox")
    post = next(e for e in snap["elements"] if e["name"] == "Post")
    text = "\U0001f680 Meet minos!\n\nThe model asks. The runtime decides. ✅ #AI"

    held = driver.fill(editor["ref"], text)
    driver.click(post["ref"])

    posted = driver._page.evaluate("() => window.posted")
    assert " ".join(held.split()) == " ".join(text.split())
    assert " ".join(posted.split()) == " ".join(text.split())


def test_a_click_behind_the_overlay_says_so(driver):
    _open_composer(driver)
    started = time.monotonic()
    with pytest.raises(CoveredByOverlay, match="dialog or panel is open"):
        driver.click_text("Start a post")
    assert time.monotonic() - started < 30
