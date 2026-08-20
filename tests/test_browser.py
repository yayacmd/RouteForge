"""End-to-end browser test: setup -> demo data -> solve -> run sheets.

Runs against a live server whose routing provider is stubbed, so no API key
and no network are needed.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8812"
PASSWORD = "dispatch-2026"
failures = []
console_errors = []


def check(label, ok, detail=""):
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(f"PAGEERROR: {e}"))
    failed_requests = []
    page.on("requestfailed", lambda r: failed_requests.append(r.url))
    page.on("response", lambda r: failed_requests.append(f"{r.status} {r.url}")
            if r.status >= 400 else None)

    print("\n=== Setup screen ===")
    page.goto(BASE, wait_until="networkidle")
    check("setup form is shown", page.is_visible("#gate-setup"))
    page.screenshot(path="/tmp/shot-1-setup.png")

    page.fill("input[name=organization_name]", "Yayac Fuel Co")
    page.fill("input[name=password]", PASSWORD)
    page.fill("input[name=routing_api_key]", "pk.stub")

    # provider switch should reveal OSRM fields
    page.select_option("#setup-provider", "osrm")
    check("OSRM fields appear", page.is_visible("#setup-osrm-fields"))
    check("API key field hides for OSRM", not page.is_visible("#setup-key-field"))
    page.select_option("#setup-provider", "locationiq")
    check("API key field returns", page.is_visible("#setup-key-field"))

    page.click("#gate-setup button[type=submit]")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)
    check("app opens after setup", page.is_visible("#app"))
    brand = page.inner_text("#brand-name")
    check("org name in topbar", brand.lower() == "yayac fuel co", f"got {brand!r}")

    print("\n=== Empty state and demo data ===")
    check("empty state visible", page.is_visible("#commodity-empty"))
    page.screenshot(path="/tmp/shot-2-empty.png")

    page.click("#load-demo")
    page.wait_for_timeout(700)
    check("stops view shown after demo", page.is_visible("section[data-view=stops].active"))
    check("4 stops loaded", page.inner_text("#count-stops") == "4")
    check("2 vehicles loaded", page.inner_text("#count-vehicles") == "2")
    check("2 products loaded", page.inner_text("#count-commodities") == "2")
    check("1 depot loaded", page.inner_text("#count-depots") == "1")
    page.screenshot(path="/tmp/shot-3-stops.png", full_page=True)

    print("\n=== Edit a stop ===")
    first_priority = page.locator("#stop-list select[data-f=priority]").first
    first_priority.select_option("must")
    page.wait_for_timeout(300)
    page.locator("#stop-list button[data-act=toggle-stop]").nth(2).click()
    page.wait_for_timeout(300)
    check("holding a stop back updates the count", page.inner_text("#count-stops") == "3")
    page.locator("#stop-list button[data-act=toggle-stop]").nth(2).click()
    page.wait_for_timeout(300)
    check("putting it back restores the count", page.inner_text("#count-stops") == "4")

    print("\n=== Plan settings ===")
    page.click("button.nav-item[data-view=plan]")
    page.wait_for_timeout(300)
    check("plan summary reads correctly",
          "4 stops across 2 vehicles" in page.inner_text("#plan-summary"),
          page.inner_text("#plan-summary"))
    page.select_option("#opt-effort", "quick")
    page.screenshot(path="/tmp/shot-4-plan.png", full_page=True)

    print("\n=== Solve ===")
    page.click("#btn-solve")
    page.wait_for_selector("section[data-view=results].active", timeout=90000)
    page.wait_for_timeout(1800)
    check("run sheets rendered", page.locator(".run-sheet").count() > 0,
          f"count={page.locator('.run-sheet').count()}")
    check("totals bar shown", page.is_visible(".totals"))
    check("map visible", page.is_visible("#map"))
    check("time rail legs rendered", page.locator(".leg").count() > 3)
    check("export button appeared", page.is_visible("#btn-export"))
    print(f"     -> {page.locator('.run-sheet').count()} run sheet(s), "
          f"{page.locator('.leg').count()} legs")
    print("     -> totals:", " | ".join(page.inner_text(".totals").split("\n")))
    page.wait_for_timeout(1200)
    page.screenshot(path="/tmp/shot-5-results.png", full_page=True)

    # Check the load bar (signature element) actually rendered with widths
    widths = page.eval_on_selector_all(
        ".load-fill", "els => els.map(e => e.style.width)")
    check("load bars have varying widths", len(set(widths)) > 1, str(widths[:6]))

    print("\n=== Infeasible plan gives plain-language help ===")
    page.click("button.nav-item[data-view=stops]")
    page.wait_for_timeout(300)
    big = page.locator("#stop-list input[data-f=demand]").first
    big.fill("999999")
    page.wait_for_timeout(400)
    page.click("button.nav-item[data-view=plan]")
    page.click("#btn-solve")
    page.wait_for_selector(".feedback.blocked", timeout=60000)
    txt = page.inner_text(".feedback.blocked")
    check("blocked feedback shown", "won't fit" in txt.lower() or "couldn't" in txt.lower())
    check("explanation is human-readable", "capacity" in txt.lower() or "holds" in txt.lower(), txt[:160])
    jargon = ["dimension", "cumulvar", "disjunction", "callback"]
    check("no solver jargon leaked", not any(j in txt.lower() for j in jargon))
    print("     ->", txt.replace("\n", " ")[:190])
    page.screenshot(path="/tmp/shot-6-blocked.png", full_page=True)

    # restore
    page.click("button.nav-item[data-view=stops]")
    page.wait_for_timeout(400)
    page.locator("#stop-list input[data-f=demand]").first.fill("900")
    page.wait_for_timeout(400)

    print("\n=== Settings and API token ===")
    page.click("button.nav-item[data-view=settings]")
    page.wait_for_timeout(400)
    token = page.inner_text("#api-token")
    check("api token displayed", len(token) > 10)

    print("\n=== Persistence across reload ===")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1200)
    check("still signed in after reload", page.is_visible("#app"))
    check("data persisted", page.inner_text("#count-stops") == "4",
          f"got {page.inner_text('#count-stops')}")
    page.click("button.nav-item[data-view=results]")
    page.wait_for_timeout(900)
    check("planned routes survive a reload", page.locator(".run-sheet").count() > 0,
          f"count={page.locator('.run-sheet').count()}")

    print("\n=== Mobile layout ===")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(500)
    check("hamburger appears on mobile", page.is_visible("#nav-toggle"))
    page.click("#nav-toggle")
    page.wait_for_timeout(400)
    check("sidebar opens on mobile", "open" in (page.get_attribute("#sidebar", "class") or ""))
    page.click("button.nav-item[data-view=results]")
    page.wait_for_timeout(800)
    page.screenshot(path="/tmp/shot-7-mobile.png", full_page=True)

    print("\n=== Sign out ===")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.wait_for_timeout(300)
    page.click("#btn-logout")
    page.wait_for_timeout(1500)
    check("login screen after sign out", page.is_visible("#gate-login"))
    page.fill("#gate-login input[name=password]", "wrong-password")
    page.click("#gate-login button[type=submit]")
    page.wait_for_timeout(1500)
    check("wrong password shows an error", page.is_visible("#login-error"))
    page.fill("#gate-login input[name=password]", PASSWORD)
    page.click("#gate-login button[type=submit]")
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)
    check("sign back in works", page.is_visible("#app"))
    page.screenshot(path="/tmp/shot-8-login.png")

    browser.close()

print("\n=== Requests that failed ===")
for u in dict.fromkeys(failed_requests):
    print("  ", u[:130])

# External CDNs (fonts, map tiles) are blocked in this sandbox, and the
# sign-out test deliberately triggers 401s. Neither is a code fault.
def is_expected(u):
    return ("fonts.googleapis" in u or "fonts.gstatic" in u
            or "cartocdn" in u or "openstreetmap" in u
            or "401" in u.split()[0])

unexpected = [u for u in dict.fromkeys(failed_requests) if not is_expected(u)]
check("no unexpected failed requests", not unexpected, str(unexpected[:3]))

real_errors = [e for e in console_errors
               if "favicon" not in e.lower() and "Signed out" not in e
               and "Failed to load resource" not in e
               and "PAGEERROR" in e]
print(f"\n=== Console errors: {len(real_errors)} ===")
for e in real_errors[:10]:
    print("  !", e[:190])
if real_errors:
    failures.append("javascript console errors")

print("\n" + "=" * 60)
if failures:
    print("FAILURES:")
    for f in failures:
        print("  X", f)
    sys.exit(1)
print("All browser checks passed.")
