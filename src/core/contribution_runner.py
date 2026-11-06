import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page, async_playwright, Error as PlaywrightError

from src.auth.browser import connect_to_chrome
from src.auth.login import wait_for_waiting_room, CloudflareBlockException, detect_block
from src.auth.cdp_client import ensure_on_portal


OFC_URL = "https://www.usvisascheduling.com/en-US/ofc-schedule/"

OFC_CITY_VALUES = {
    "CHENNAI": "3f6bf614-b0db-ec11-a7b4-001dd80234f6",
    "CHENNAI VAC": "3f6bf614-b0db-ec11-a7b4-001dd80234f6",
    "HYDERABAD": "436bf614-b0db-ec11-a7b4-001dd80234f6",
    "HYDERABAD VAC": "436bf614-b0db-ec11-a7b4-001dd80234f6",
    "KOLKATA": "466bf614-b0db-ec11-a7b4-001dd80234f6",
    "KOLKATA VAC": "466bf614-b0db-ec11-a7b4-001dd80234f6",
    "MUMBAI": "486bf614-b0db-ec11-a7b4-001dd80234f6",
    "MUMBAI VAC": "486bf614-b0db-ec11-a7b4-001dd80234f6",
    "DELHI": "4a6bf614-b0db-ec11-a7b4-001dd80234f6",
    "NEW DELHI": "4a6bf614-b0db-ec11-a7b4-001dd80234f6",
    "NEW DELHI VAC": "4a6bf614-b0db-ec11-a7b4-001dd80234f6",
}

DEFAULT_CITY_ORDER = ["NEW DELHI", "MUMBAI", "HYDERABAD", "CHENNAI", "KOLKATA"]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OFC_CONTRIB] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ofc_contribution_runner")


def _state_file(customer: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in customer)
    state_dir = Path(os.getenv("CHECKVISA_STATE_DIR", Path(__file__).parent.parent.parent / "state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"contribution_state_{safe}.json"


def _write_state(customer: str, payload: dict) -> None:
    payload = {
        "customer_name": customer,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    _state_file(customer).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def normalize_city(city: str) -> str:
    city = city.strip().upper()
    if city not in OFC_CITY_VALUES:
        raise ValueError(f"Unsupported OFC city: {city}")
    return city


def parse_cities(raw: str) -> list[str]:
    if not raw:
        return DEFAULT_CITY_ORDER[:]
    cities = []
    for part in raw.split(","):
        if not part.strip():
            continue
        if part.strip().upper() == "ANY":
            return DEFAULT_CITY_ORDER[:]
        city = normalize_city(part)
        if city not in cities:
            cities.append(city)
    return cities or DEFAULT_CITY_ORDER[:]


async def _wait_for_page_idle(page: Page, timeout_ms: int = 15_000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


async def try_saved_session_ofc_page(page: Page, customer: str, wait_seconds: int = 600) -> bool:
    log.info("Checking saved session directly on OFC page.")
    try:
        await page.goto(OFC_URL, wait_until="domcontentloaded", timeout=120_000)
    except Exception as exc:
        log.warning(f"Direct OFC navigation did not complete cleanly: {exc}")

    try:
        await wait_for_waiting_room(
            page,
            log,
            timeout_minutes=max(1, wait_seconds // 60),
            ready_selector="#post_select",
        )
    except TimeoutError:
        log.info(f"Saved session stayed in waiting room too long. Current URL: {page.url}")
        _write_state(customer, {"status": "saved_session_waiting_room_timeout", "url": page.url})
        return False
    except CloudflareBlockException as e:
        log.error(f"Cloudflare Block detected during saved session check: {e}")
        sys.exit(43)

    await _wait_for_page_idle(page)

    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if await _ofc_dropdown_ready(page) and await _profile_name_visible(page):
                _write_state(customer, {"status": "ofc_page_ready", "url": page.url, "session_reused": True})
                log.info("Saved session active; OFC page ready.")
                return True

            url = page.url.lower()
            login_visible = await page.locator("#signInName, input[type='password']").count() > 0
            if any(k in url for k in ["login", "logon", "signin", "b2clogin"]) or login_visible:
                log.info(f"Saved session redirected to login. Current URL: {page.url}")
                _write_state(customer, {"status": "saved_session_inactive", "url": page.url})
                return False
        except Exception:
            pass

        await asyncio.sleep(1)

    log.info(f"Saved session did not expose OFC controls after waiting. Current URL: {page.url}")
    _write_state(customer, {"status": "saved_session_inactive", "url": page.url})
    return False


PROFILE_NAME_RE = re.compile(r"\(\d{6,}\)")  # header user info, e.g. "Name (754993242)"
PORTAL_PREFIX = "https://www.usvisascheduling.com"


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


async def _profile_name_visible(page: Page) -> bool:
    try:
        loc = page.get_by_text(PROFILE_NAME_RE)
        return await loc.count() > 0 and await loc.first.is_visible()
    except Exception:
        return False


async def _ofc_dropdown_ready(page: Page) -> bool:
    """True when we are on the OFC page and #post_select is visible with the city options loaded."""
    try:
        if "/ofc-schedule" not in page.url.lower():
            return False
        sel = page.locator("#post_select")
        if await sel.count() == 0 or not await sel.first.is_visible():
            return False
        values = await sel.first.evaluate("el => Array.from(el.options).map(o => o.value)")
        return any(v in OFC_CITY_VALUES.values() for v in values)
    except Exception:
        return False


async def wait_for_profile_overview(page: Page, customer: str, timeout_seconds: int = 120, stable_seconds: float = 2.0) -> bool:
    """Step 4 of the portal flow: after security questions the portal redirects (b2clogin ->
    /profile/?ReturnUrl -> /en-US/). Wait until we are on the portal, the header user info is
    visible, and the URL has stopped changing for stable_seconds."""
    deadline = _loop_time() + timeout_seconds
    last_url, url_since = None, _loop_time()
    while _loop_time() < deadline:
        url = page.url
        now = _loop_time()
        if url != last_url:
            last_url, url_since = url, now
        if url.startswith(PORTAL_PREFIX) and now - url_since >= stable_seconds and await _profile_name_visible(page):
            log.info(f"Profile overview ready (header user info visible): {url}")
            return True
        await asyncio.sleep(0.5)
    log.error(f"Profile overview never settled (header user info missing). Current URL: {page.url}")
    return False


async def wait_for_ofc_ready(page: Page, customer: str, timeout_seconds: int = 120) -> bool:
    """Step 6: start rotating only when the OFC page shows the header user info AND the
    city dropdown is visible with its options loaded."""
    deadline = _loop_time() + timeout_seconds
    while _loop_time() < deadline:
        if await _ofc_dropdown_ready(page) and await _profile_name_visible(page):
            return True
        marker = await detect_block(page)
        if marker:
            log.error(f"Block marker '{marker}' while waiting for OFC page. Current URL: {page.url}")
            _write_state(customer, {"status": "blocked", "reason": f"page marker '{marker}'", "url": page.url})
            sys.exit(43)
        await asyncio.sleep(0.5)
    return False


async def _click_schedule_option(page: Page, timeout_seconds: int = 30) -> bool:
    """Step 5: wait for the schedule option on the overview page to become visible, then click it."""
    if "reschedule=true" in OFC_URL:
        selectors = ["#reschedule_appointment", "a[href*='reschedule=true']", "a:has-text('Reschedule Appointment')"]
    else:
        selectors = ["#continue_application", "a[href*='/ofc-schedule']", "a:has-text('Schedule Appointment')"]
    deadline = _loop_time() + timeout_seconds
    while _loop_time() < deadline:
        for selector in selectors:
            try:
                loc = page.locator(selector)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click()
                    log.info(f"Clicked schedule link: {selector}")
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return False


async def go_to_ofc_page(page: Page, customer: str) -> bool:
    if "/ofc-schedule" in page.url.lower():
        log.info("Already on OFC schedule page.")
    else:
        if not await ensure_on_portal(page, log, timeout_seconds=90):
            return False

        if not await wait_for_profile_overview(page, customer):
            _write_state(customer, {"status": "profile_overview_failed", "url": page.url})
            return False

        if not await _click_schedule_option(page):
            log.info("Schedule link not found on overview page after 30s; navigating directly to OFC page.")
            try:
                await page.goto(OFC_URL, wait_until="domcontentloaded", timeout=120_000)
            except Exception as nav_err:
                log.warning(f"Direct OFC navigation interrupted ({nav_err.__class__.__name__}); waiting for page to settle...")
                await asyncio.sleep(3)
                # If we landed on the profile page, try to complete it or navigate past it
                if "/profile" in page.url.lower():
                    log.info("Landed on profile page; attempting to proceed...")
                    for btn_sel in ["button:has-text('Continue')", "button:has-text('Save')", "input[type='submit']", "a:has-text('Continue')"]:
                        try:
                            if await page.locator(btn_sel).count() > 0:
                                await page.locator(btn_sel).first.click()
                                log.info(f"Clicked profile button: {btn_sel}")
                                await asyncio.sleep(3)
                                break
                        except Exception:
                            continue
                    # Try OFC navigation again
                    try:
                        await page.goto(OFC_URL, wait_until="domcontentloaded", timeout=60_000)
                    except Exception:
                        log.warning("Second OFC navigation also interrupted; continuing to check page state...")

    await _wait_for_page_idle(page)

    if not await wait_for_ofc_ready(page, customer):
        log.error(f"OFC page not ready (needs header user info + loaded city dropdown). Current URL: {page.url}")
        _write_state(customer, {"status": "ofc_page_failed", "url": page.url})
        return False

    _write_state(customer, {"status": "ofc_page_ready", "url": page.url})
    log.info("OFC page ready: header user info and city dropdown both visible.")
    return True


async def rotate_city(page: Page, customer: str, city: str, dwell_seconds: int) -> dict:
    city_value = OFC_CITY_VALUES[city]
    log.info(f"Selecting OFC city: {city}")

    await page.select_option("#post_select", value=city_value)

    loading_deadline = time.time() + 45
    last_message = ""
    while time.time() < loading_deadline:
        try:
            msg = (await page.locator("#datepicker-message").inner_text(timeout=1000)).strip()
            last_message = " ".join(msg.split())
            if "loading" not in last_message.lower():
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    await asyncio.sleep(max(2, dwell_seconds))

    try:
        message = (await page.locator("#datepicker-message").inner_text(timeout=1000)).strip()
        message = " ".join(message.split())
    except Exception:
        message = last_message

    try:
        available_dates = await page.locator(".greenday").count()
    except Exception:
        available_dates = 0

    try:
        submit_enabled = await page.locator("#submitbtn").is_enabled()
    except Exception:
        submit_enabled = False

    status = {
        "status": "city_checked",
        "mode": "contribution_only",
        "city": city,
        "city_value": city_value,
        "message": message,
        "available_date_count": available_dates,
        "submit_enabled": submit_enabled,
        "url": page.url,
    }
    _write_state(customer, status)
    log.info(f"{city}: {message or 'no message'} | green dates: {available_dates}")
    return status


async def run(
    cdp_port: int,
    customer: str,
    cities: list[str],
    dwell_seconds: int,
    cycles: int,
    max_rotations: int,
    min_gap_seconds: int,
    max_gap_seconds: int,
) -> None:
    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, cdp_port, log, handle_dialogs=True)

        if not await go_to_ofc_page(page, customer):
            sys.exit(1)

        await contribute_from_page(page, customer, cities, dwell_seconds, cycles, max_rotations, min_gap_seconds, max_gap_seconds)


async def contribute_from_page(
    page: Page,
    customer: str,
    cities: list[str],
    dwell_seconds: int,
    cycles: int,
    max_rotations: int,
    min_gap_seconds: int,
    max_gap_seconds: int,
) -> None:
    cycle_no = 0
    rotation_count = 0
    consecutive_failures = 0
    while cycles <= 0 or cycle_no < cycles:
        cycle_no += 1
        log.info(f"Starting contribution cycle {cycle_no}.")
        for city in cities:
            if max_rotations > 0 and rotation_count >= max_rotations:
                _write_state(customer, {
                    "status": "rotation_limit_reached",
                    "mode": "contribution_only",
                    "rotation_count": rotation_count,
                    "max_rotations": max_rotations,
                })
                log.info(f"Rotation limit reached: {rotation_count}/{max_rotations}")
                return
            try:
                await rotate_city(page, customer, city, dwell_seconds)
                rotation_count += 1
                consecutive_failures = 0  # reset failures on success
                _write_state(customer, {
                    "status": "rotation_count_updated",
                    "mode": "contribution_only",
                    "city": city,
                    "rotation_count": rotation_count,
                    "max_rotations": max_rotations,
                })
                log.info(f"Rotation count for {customer}: {rotation_count}/{max_rotations or 'unlimited'}")
                gap = random.uniform(min_gap_seconds, max_gap_seconds)
                log.info(f"Waiting {gap:.1f}s before next rotation.")
                await asyncio.sleep(gap)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                consecutive_failures += 1
                exc_str = str(exc).lower()

                # Get page url safely to prevent nested crash if browser is closed/crashed
                url = "unknown (page/browser closed)"
                try:
                    if not page.is_closed():
                        url = page.url
                except Exception:
                    pass

                log.error(f"City rotation failed for {city} (consecutive failures: {consecutive_failures}): {exc}", exc_info=True)
                _write_state(customer, {
                    "status": "city_failed",
                    "city": city,
                    "error": str(exc),
                    "url": url,
                })

                # Check if we should exit with 429 cooldown
                is_rate_limited = "429" in exc_str or "too many requests" in exc_str or "access denied" in exc_str
                is_closed = page.is_closed() or "closed" in exc_str or "target closed" in exc_str
                block_marker = None if is_closed else await detect_block(page)

                if is_rate_limited or block_marker:
                    reason = f"page marker '{block_marker}'" if block_marker else "exception text"
                    log.error(f"Block or rate limit detected for {customer} ({reason}). Exiting with code 43: session wiped, next account.")
                    _write_state(customer, {"status": "blocked", "reason": reason, "url": url})
                    sys.exit(43)
                elif is_closed:
                    log.error(f"Page or browser context was closed for {customer}. Exiting with code 42 to signal restart cooldown.")
                    sys.exit(42)
                elif consecutive_failures >= 3:
                    log.error(f"Too many consecutive failures ({consecutive_failures}) for {customer}. Exiting with code 42 to signal restart cooldown.")
                    sys.exit(42)

                await asyncio.sleep(10)

    _write_state(customer, {
        "status": "complete",
        "mode": "contribution_only",
        "cycles": cycles,
        "rotation_count": rotation_count,
        "max_rotations": max_rotations,
    })
    log.info("Contribution cycles complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate OFC cities for contribution-only sessions.")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--customer", default="default")
    parser.add_argument("--cities", default=",".join(DEFAULT_CITY_ORDER))
    parser.add_argument("--dwell-seconds", type=int, default=12)
    parser.add_argument("--cycles", type=int, default=0, help="0 means run forever")
    parser.add_argument("--max-rotations", type=int, default=0, help="0 means no rotation limit")
    parser.add_argument("--min-gap-seconds", type=int, default=15)
    parser.add_argument("--max-gap-seconds", type=int, default=20)
    parser.add_argument("--saved-session-only", action="store_true", help="Only use existing session; exit if OFC is not already accessible")
    parser.add_argument("--saved-session-wait-seconds", type=int, default=600, help="Seconds to wait for queue/waiting room during saved-session check")
    parser.add_argument("--reschedule", action="store_true", help="Run in reschedule mode")
    args = parser.parse_args()

    global OFC_URL
    if args.reschedule:
        OFC_URL = "https://www.usvisascheduling.com/en-US/ofc-schedule/?reschedule=true"

    cities = parse_cities(args.cities)
    log.info(f"Contribution-only mode for {args.customer}: {', '.join(cities)}")
    if args.saved_session_only:
        async def saved_session_main() -> None:
            async with async_playwright() as pw:
                browser, context, page = await connect_to_chrome(pw, args.cdp_port, log, handle_dialogs=True)
                if not await try_saved_session_ofc_page(page, args.customer, args.saved_session_wait_seconds):
                    sys.exit(3)
                await contribute_from_page(
                    page,
                    args.customer,
                    cities,
                    args.dwell_seconds,
                    args.cycles,
                    args.max_rotations,
                    args.min_gap_seconds,
                    max(args.min_gap_seconds, args.max_gap_seconds),
                )

        asyncio.run(saved_session_main())
    else:
        asyncio.run(run(
            args.cdp_port,
            args.customer,
            cities,
            args.dwell_seconds,
            args.cycles,
            args.max_rotations,
            args.min_gap_seconds,
            max(args.min_gap_seconds, args.max_gap_seconds),
        ))


if __name__ == "__main__":
    main()
