import re
import time
import asyncio
import logging
from playwright.async_api import Page
from src.utils.common import human_delay, human_type, human_click
from src.auth.captcha import solve_captcha_on_page

class CloudflareBlockException(Exception):
    pass

TURNSTILE_STALL_SECONDS = 180  # a Turnstile that stays unsolved this long ends the account (exit 43)


BLOCK_PATTERNS = [
    r"you have been blocked", r"why have i been blocked", r"error 1020", r"error 1015",
    r"rate limited", r"too many requests", r"error 429", r"access denied",
    r"error (?:code )?10(?:06|07|08|10|12)", r"error (?:code )?5(?:2\d|30)", r"unable to access",
]


async def detect_block(page: Page) -> str | None:
    """Return the matched Cloudflare or portal block marker from the current page, or None."""
    try:
        text = (await page.inner_text("body")).lower() + " " + (await page.title()).lower()
    except Exception:
        return None
    for pattern in BLOCK_PATTERNS:
        if re.search(pattern, text):
            return pattern
    return None



async def wait_for_waiting_room(
    page: Page,
    log: logging.Logger,
    timeout_minutes: int = 60,
    ready_selector: str | None = None,
) -> None:
    deadline = time.time() + timeout_minutes * 60
    log.info("Checking for Cloudflare Waiting Room …")
    challenge_since: float | None = None  # when a Turnstile challenge (not a queue) first appeared

    while time.time() < deadline:
        if ready_selector:
            try:
                ready_loc = page.locator(ready_selector)
                if await ready_loc.count() > 0 and await ready_loc.first.is_visible():
                    log.info(f"Waiting room cleared; found ready selector {ready_selector} at URL: {page.url}")
                    return
            except Exception:
                pass

        try:
            html = await page.content()
            title = await page.title()
        except Exception:
            # Page is mid-navigation — wait and retry
            await asyncio.sleep(2)
            continue
            
        html_lower = html.lower()
        title_lower = title.lower()
        if (
            "sorry, you have been blocked" in html_lower 
            or "why have i been blocked" in html_lower 
            or "unable to access" in html_lower
            or "error code 524" in html_lower
            or "a timeout occurred" in html_lower
            or "524" in title_lower
        ):
            log.error("Cloudflare blocked the page (Error 1020/524).")
            raise CloudflareBlockException("Cloudflare Blocked Page Detected")

        marker = await detect_block(page)
        if marker:
            log.error(f"Block or rate-limit page detected before login (marker '{marker}').")
            raise CloudflareBlockException(f"Block page detected: {marker}")
        
        in_queue = any(kw in html.lower() for kw in [
            "waiting room", "you are in the queue", "queue position",
            "estimated wait", "waitingroom", "cfwaitingroom", "waiting-room",
            "just a moment", "verify you are human", "challenge-platform"
        ]) or "moment" in title.lower()

        is_turnstile = any(kw in html_lower for kw in ["just a moment", "verify you are human", "challenge-platform"]) \
            and not any(kw in html_lower for kw in ["waiting room", "you are in the queue", "queue position", "estimated wait"])
        if is_turnstile:
            if challenge_since is None:
                challenge_since = time.time()
                log.info("Cloudflare Turnstile challenge appeared before login; solving it.")
            elif time.time() - challenge_since > TURNSTILE_STALL_SECONDS:
                log.error(f"Turnstile not cleared after {TURNSTILE_STALL_SECONDS}s. Treating as a block.")
                raise CloudflareBlockException("Turnstile stalled before login")
        else:
            challenge_since = None

        if not in_queue:
            log.info(f"Waiting room cleared — URL: {page.url}")
            return
            
        # Attempt to click Cloudflare challenge if present
        try:
            cf_iframe = page.frame_locator("iframe[src*='challenge']")
            cf_input = page.locator('input[name="cf-turnstile-response"]')
            
            if await cf_iframe.locator("input[type='checkbox']").count() > 0:
                await cf_iframe.locator("input[type='checkbox']").first.click(timeout=1000)
                log.info("Clicked Cloudflare challenge checkbox.")
            elif await cf_iframe.locator(".ctp-checkbox-label").count() > 0:
                await cf_iframe.locator(".ctp-checkbox-label").first.click(timeout=1000)
                log.info("Clicked Cloudflare Turnstile label.")
            elif await page.locator("iframe[src*='challenge']").count() > 0:
                await page.locator("iframe[src*='challenge']").first.click(timeout=1000)
                log.info("Clicked Cloudflare challenge iframe directly.")
            elif await cf_input.count() > 0:
                # Often hidden behind closed shadow DOM, click the wrapper div
                await cf_input.first.locator("..").click(position={"x": 30, "y": 30}, timeout=1000)
                log.info("Clicked Cloudflare Turnstile widget wrapper.")
        except Exception:
            pass

        try:
            loc = page.locator("[data-queue-position], .queue-position, #queuePosition, #waitTime")
            pos = (await loc.first.inner_text()).strip() if await loc.count() > 0 else "unknown"
            if pos != "unknown":
                log.info(f"In waiting room — {pos}")
            else:
                log.debug("In waiting room / Cloudflare challenge — waiting for auto-redirect …")
        except Exception:
            log.debug("In waiting room / Cloudflare challenge — waiting for auto-redirect …")

        await asyncio.sleep(5)

    raise TimeoutError("Timed out waiting for waiting room to clear.")


async def login(page: Page, username: str, password: str, api_key: str, log: logging.Logger) -> bool:
    log.info("Starting login …")

    u_selectors = [
        "#signInName", "#email", "input[name='signInName']",
        "input[name='UserName']", "input[name='username']",
        "input[name='email']",    "input[type='email']",
        "input[id='UserName']",   "input[id='username']",
        "input[placeholder*='username' i]", "input[placeholder*='email' i]",
        "input[type='text']",
    ]
    p_selectors = [
        "#password", "input[name='Password']", "input[name='password']",
        "input[type='password']", "input[id='Password']",
        "input[placeholder*='password' i]",
    ]

    u_combined = ", ".join(u_selectors)
    p_combined = ", ".join(p_selectors)

    try:
        await page.wait_for_selector(u_combined, state="visible", timeout=30000)
    except Exception:
        log.error("Username field did not appear within 30s.")
        return False

    u_field = None
    for s in u_selectors:
        if await page.locator(s).count() > 0:
            u_field = s
            break

    p_field = None
    for s in p_selectors:
        if await page.locator(s).count() > 0:
            p_field = s
            break

    if not u_field:
        log.error("Username field not found.")
        return False
    if not p_field:
        log.error("Password field not found.")
        return False

    await human_type(page, u_field, username)
    log.debug(f"Typed username: {username}")
    await human_type(page, p_field, password)
    log.debug("Typed password.")

    if not await solve_captcha_on_page(page, api_key, log):
        log.error("CAPTCHA failed.")
        return False

    await human_delay(300, 600)

    signin_selectors = [
        "input[type='submit'][value*='Sign' i]",
        "input[type='submit'][value*='Log' i]",
        "button[type='submit']",
        "button:has-text('Sign In')",
        "button:has-text('Login')",
        "input[value='Login']",
        "#loginButton", ".login-btn",
    ]

    clicked = False
    for sel in signin_selectors:
        if await page.locator(sel).count() > 0:
            await human_click(page, sel)
            log.info(f"Clicked Sign In: {sel}")
            clicked = True
            break

    if not clicked:
        log.error("Sign In button not found.")
        return False

    log.info("Waiting for login redirect ...")
    try:
        for _ in range(45):
            u = page.url.lower()
            if "selfasserted" in u or not any(k in u for k in ["b2clogin", "login", "logon", "signin", "sign-in"]):
                break
            await asyncio.sleep(1)
    except Exception:
        pass

    try:
        url  = page.url
        html = await page.inner_text("body")  # MUST use inner_text
    except Exception as e:
        if page.is_closed() or "closed" in str(e).lower():
            log.error("Page was unexpectedly closed!")
            return False
        log.info(f"Page navigating, couldn't read content ({e}) — assuming successful redirect.")
        return True

    if any(p in html.lower() for p in [
        "invalid credentials", "incorrect password", "login failed",
        "the user name or password", "invalid username",
        "character", "match the image", "try again", "does not match", "captcha failed", "image captcha"
    ]):
        log.error("Login error / invalid CAPTCHA message on page. Aborting attempt and forcing refresh...")
        return False

    url_lower = url.lower()
    if "selfasserted" not in url_lower and any(k in url_lower for k in ["logon", "login", "signin", "sign-in", "b2clogin"]):
        log.error("Login still failed after initial submit. Aborting attempt.")
        return False

    try:
        log.info(f"Login successful — URL: {page.url}")
    except Exception:
        log.info("Login successful — navigating ...")
    return True
