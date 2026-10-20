"""The web app in a real browser, against a live Wren server with made-up data.

Runs in WebKit (closest to iPhone Safari) where it launches, otherwise Chromium;
set WREN_E2E_BROWSER=webkit|chromium to choose. Skipped without Playwright:
pip install -e '.[e2e]' && playwright install webkit chromium
"""

import base64
import os
import socket
import threading
import time

import pytest
from conftest import item

playwright = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from wren import subscriptions  # noqa: E402

pytestmark = pytest.mark.e2e

SHOW = "https://www.youtube.com/playlist?list=UUtest"
# Thumbnails come from YouTube; the browser gets this blank pixel instead.
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
# An iPhone 13's screen; Playwright's own iPhone profiles only launch in WebKit.
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True,
         "has_touch": True, "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
         "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"}
DESKTOP = {"viewport": {"width": 1280, "height": 800}}


@pytest.fixture(scope="session")
def base_url():
    from wren import server
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def browser():
    with playwright.sync_playwright() as p:
        names = [os.environ["WREN_E2E_BROWSER"]] if os.environ.get("WREN_E2E_BROWSER") else ["webkit", "chromium"]
        errors = []
        for name in names:
            try:
                b = getattr(p, name).launch()
            except Exception as e:
                errors.append(f"{name}: {str(e).splitlines()[0]}")
                continue
            yield b
            b.close()
            return
        # A browser asked for by name must run (as in CI); otherwise skip.
        if os.environ.get("WREN_E2E_BROWSER"):
            pytest.fail("browser won't launch: " + "; ".join(errors))
        pytest.skip("no browser launches: " + "; ".join(errors))


def open_app(browser, base_url, device):
    """A page on the app, failing the test on any script error."""
    ctx = browser.new_context(base_url=base_url, service_workers="block", **device)
    ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base_url)
              else route.fulfill(body=PIXEL, content_type="image/png"))
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    # Failed requests are listed by URL; the console's own message doesn't name them.
    page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
            and page.errors.append(m.text))
    page.on("response", lambda r: r.status >= 400 and page.errors.append(f"HTTP {r.status}: {r.url}"))
    return page


@pytest.fixture
def phone(browser, base_url):
    page = open_app(browser, base_url, PHONE)
    yield page
    page.context.close()
    assert page.errors == []


@pytest.fixture
def desktop(browser, base_url):
    page = open_app(browser, base_url, DESKTOP)
    yield page
    page.context.close()
    assert page.errors == []


@pytest.fixture
def show(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Test Show", [item("e2"), item("e1")])
    s = subscriptions.subscribe(SHOW)
    subscriptions.sync()
    return s


def title(page):
    return page.locator("#view-title")


@pytest.mark.parametrize("hash, heading", [
    ("#/", "Shows"), ("#/add", "Add"), ("#/alerts", "Alerts"), ("#/status", "Status"),
    ("#/singles", "Single episodes"),
    ("#/recent", "Recent downloads"),
])
@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_pages_render(request, size, hash, heading):
    page = request.getfixturevalue(size)
    page.goto("/" + hash)
    playwright.expect(title(page)).to_have_text(heading)
    playwright.expect(page.locator("#view")).not_to_contain_text("Loading…")


def test_empty_home(phone):
    phone.goto("/")
    playwright.expect(phone.locator("#view")).to_contain_text("No shows yet.")


def test_tap_through_to_a_show_and_back(phone, show):
    phone.goto("/")
    entries = phone.evaluate("history.length")
    phone.locator(".show-list .row").tap()  # the app's own touchend handler
    playwright.expect(title(phone)).to_have_text("Test Show")
    playwright.expect(phone.locator(".ep-title")).to_have_count(2)
    assert phone.evaluate("history.length") == entries  # in-app links replace history entries
    phone.locator("#back").tap()
    playwright.expect(title(phone)).to_have_text("Shows")


def test_delete_episode_takes_two_taps(phone, show):
    phone.goto(f"/#/show/{show['id']}")
    phone.locator(".ep-head").first.tap()
    delete = phone.locator(".ep.open .danger")
    delete.tap()
    playwright.expect(delete).to_have_text("Tap again to delete")
    delete.tap()
    playwright.expect(phone.locator(".ep-title")).to_have_count(1)


def test_subscribe_from_a_pasted_link(phone, fake_feeds, monkeypatch):
    monkeypatch.setattr(subscriptions, "ensure_art", lambda show: None)
    fake_feeds.set(SHOW, "New Show", [item("a", title="First episode")])
    # Looking a link up asks YouTube; answer for the server.
    phone.route("**/api/resolve", lambda route: route.fulfill(json={
        "kind": "show", "url": SHOW, "title": "New Show", "subscribed": None,
        "latest": [{"title": "First episode", "duration": 600, "thumb_url": None, "live": False}]}))
    phone.goto("/#/add")
    phone.locator("#add-url").fill(SHOW)
    phone.get_by_role("button", name="Look up").tap()
    playwright.expect(phone.locator(".preview-title")).to_have_text("First episode")
    phone.get_by_role("button", name="Subscribe to New Show").tap()
    playwright.expect(title(phone)).to_have_text("New Show")
    assert [s["name"] for s in subscriptions.list_shows()] == ["New Show"]


def test_desktop_panes(desktop, show):
    desktop.goto("/")
    # With the show list always beside it, "Shows" opens the first show.
    playwright.expect(title(desktop)).to_have_text("Test Show")
    playwright.expect(desktop.locator("#sidebar")).to_be_visible()
    playwright.expect(desktop.locator("#listpane")).to_contain_text("Test Show")


def test_recent_downloads(phone, show):
    phone.goto("/")
    # The home screen shows the latest few, and leads to the rest.
    playwright.expect(phone.locator(".section", has_text="Recently downloaded").locator(".ep-title")).to_have_count(2)
    phone.locator(".see-all").tap()
    playwright.expect(title(phone)).to_have_text("Recent downloads")
    playwright.expect(phone.locator(".section h2").first).to_have_text("Today")
    phone.locator(".ep-head").first.tap()
    phone.locator(".ep.open").get_by_text("Go to show").tap()
    playwright.expect(title(phone)).to_have_text("Test Show")


def test_download_times(phone, show):
    phone.goto(f"/#/show/{show['id']}")
    phone.locator(".ep-head").first.tap()
    playwright.expect(phone.locator(".ep.open .ep-when")).to_contain_text("Downloaded today at")
    phone.goto("/#/status")
    # Both finished the same minute: one group of two dots, which lists both.
    group = phone.locator(".act-dot.group")
    playwright.expect(group).to_have_count(1)
    playwright.expect(group.locator(".count")).to_have_text("2")
    group.tap()
    playwright.expect(phone.locator(".act-detail")).to_contain_text("2 downloads at")
    playwright.expect(phone.locator(".act-detail")).to_contain_text("Episode e1")


def test_sponsor_cuts_table(phone, show):
    import json
    from wren import db
    cuts = [{"start": 2, "end": 11, "source": "sponsorblock", "category": "intro"},
            {"start": 781, "end": 862, "source": "sponsorblock"},
            {"start": 2259, "end": 2315, "source": "transcript", "advertiser": "Hims", "confidence": "high"}]
    with db.connect() as conn:
        conn.execute("UPDATE episodes SET sponsor_state = 'done', sponsor_removed = 146, sponsor_segments = ?, "
                     "sponsor_cuts = ? WHERE video_id = 'e1'",
                     (json.dumps([[c["start"], c["end"]] for c in cuts]), json.dumps({"cuts": cuts, "note": None})))
    phone.goto(f"/#/show/{show['id']}")
    playwright.expect(phone.locator(".ep", has_text="Episode e1").locator(".badge.cut")).to_have_text("Ads removed")
    playwright.expect(phone.locator(".ep", has_text="Episode e2").locator(".badge.cut")).to_have_count(0)
    phone.locator(".ep-head", has_text="Episode e1").tap()
    table = phone.locator(".ep.open .cuts")
    playwright.expect(table.locator(".cuts-head")).to_have_text("Removed 2 sponsor reads and the intro · 2m 26s in all")
    playwright.expect(table.locator("tbody tr")).to_have_count(3)
    playwright.expect(table.locator("tbody tr").first).to_have_text("0:02–0:119sIntroSponsorBlock")
    playwright.expect(table.locator("tbody tr").nth(1)).to_have_text("13:01–14:221m 21s—SponsorBlock")
    playwright.expect(table.locator("tbody tr").last).to_contain_text("Hims")
