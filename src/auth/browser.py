import asyncio
import os
import sys
import time
import socket
import logging
import json
import subprocess
import zipfile
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import Page, BrowserContext

env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def resolve_browser_exe(log: logging.Logger) -> str:
    """Resolve the Chrome-compatible browser executable for local development."""
    env_path = (
        os.getenv("CLOAK_BROWSER_EXE", "").strip()
        or os.getenv("CHECKVISA_BROWSER_EXE", "").strip()
    )
    if env_path:
        path = Path(env_path).expanduser().resolve()
        if path.is_file():
            log.info(f"Using configured browser executable: {path}")
            return str(path)
        log.error(f"Configured browser executable does not exist: {path}")
        sys.exit(1)

    # Prefer Playwright Chromium because it supports unpacked extension loading.
    import glob
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        pattern = os.path.join(local_app_data, "ms-playwright", "chromium-*", "chrome-win*", "chrome.exe")
        matches = glob.glob(pattern)
        if matches:
            matches.sort(reverse=True)
            return matches[0]

    return CHROME_EXE


def ensure_extension_assets(extension_path: str, log: logging.Logger) -> None:
    """Extract the extension's img/ folder from img.zip if it is missing (the repo ships it zipped)."""
    ext = Path(extension_path)
    if (ext / "img").is_dir():
        return
    archive = ext / "img.zip"
    if not archive.is_file():
        log.warning(f"Extension has no img/ folder and no img.zip to extract: {ext}")
        return
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            # Only extract img/ entries, and refuse any path that escapes the extension folder.
            if not member.startswith("img/") or ".." in Path(member).parts:
                continue
            zf.extract(member, ext)
    log.info(f"Extracted extension images from {archive.name} into {ext}")


def resolve_extension_path(log: logging.Logger) -> str:
    """Resolve the Chrome extension folder to load into Playwright Chromium."""
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        os.getenv("CHECKVISA_EXTENSION_PATH", "").strip(),
        str(repo_root / "extensions" / "checkvisaslots"),
        str(repo_root / "checkvisastart_flutter_integration" / "extensions" / "checkvisaslots"),
        str(repo_root.parent / "leso-extension" / "build" / "chrome-mv3-prod"),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if (path / "manifest.json").is_file():
            return str(path)

    log.error(
        "No Chrome extension folder found. Expected CheckVisaSlots at:\n"
        f"  {repo_root / 'checkvisastart_flutter_integration' / 'extensions' / 'checkvisaslots'}\n"
        "or set CHECKVISA_EXTENSION_PATH to an unpacked extension folder."
    )
    sys.exit(1)

def disable_password_prompts(profile_dir: str, log: logging.Logger) -> None:
    """Turn off Chrome's save-password offer in this account's profile before launch."""
    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs = {}
    if prefs_path.is_file():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning(f"Could not read Chrome Preferences ({exc}); not changing password settings.")
            return
    prefs["credentials_enable_service"] = False
    prefs.setdefault("profile", {})["password_manager_enabled"] = False
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")


def ensure_chrome_debug_running(
    cdp_port: int,
    profile_dir: str,
    log: logging.Logger,
    start_url: str = "about:blank",
) -> None:
    """
    Start Chrome with remote debugging on the given port if not already running.
    Each account gets its own profile directory and port so sessions are isolated.
    """
    def port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    if port_open(cdp_port):
        log.info(f"Chrome debug port {cdp_port} already active — connecting.")
        return

    log.info(f"Starting Chrome with --remote-debugging-port={cdp_port} …")

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    
    # Delete the Sessions directory to prevent Chrome from restoring previous tabs.
    # We want a fresh 'about:blank' window each time.
    import shutil
    sessions_dir = Path(profile_dir) / "Default" / "Sessions"
    if sessions_dir.exists():
        try:
            shutil.rmtree(sessions_dir)
        except Exception as e:
            log.warning(f"Failed to clear Sessions dir: {e}")

    chrome_exe = resolve_browser_exe(log)

    if not os.path.isfile(chrome_exe):
        log.error(
            "Browser executable not found.\n"
            "Please install Playwright's bundled Chromium by running:\n"
            "  playwright install chromium\n"
            "or set CLOAK_BROWSER_EXE / CHECKVISA_BROWSER_EXE to a Chrome-compatible browser executable.\n"
            "Then run this bot again."
        )
        sys.exit(1)

    extension_path = resolve_extension_path(log)
    ensure_extension_assets(extension_path, log)
    disable_password_prompts(profile_dir, log)
    log.info(f"Using extension from: {extension_path}")

    subprocess.Popen([
        chrome_exe,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--password-store=basic",  # no OS keyring unlock prompt on Linux
        f"--disable-extensions-except={extension_path}",
        f"--load-extension={extension_path}",
        start_url,
    ])

    # Wait up to 15 s for port to open
    for _ in range(30):
        time.sleep(0.5)
        if port_open(cdp_port):
            log.info("Chrome debug port ready.")
            return

    log.error(f"Chrome debug port {cdp_port} did not open in time.")
    sys.exit(1)


async def connect_to_chrome(playwright, cdp_port: int, log: logging.Logger, handle_dialogs: bool = False):
    """Connect Playwright to a running Chrome via CDP."""
    log.info(f"Connecting to Chrome on ws://127.0.0.1:{cdp_port} …")

    browser = await playwright.chromium.connect_over_cdp(
        f"http://127.0.0.1:{cdp_port}"
    )

    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Use existing page or open new one
    if context.pages:
        page = context.pages[-1]
    else:
        page = await context.new_page()

    if handle_dialogs:
        async def handle_dialog(dialog):
            try:
                await dialog.accept()
            except Exception:
                pass
        page.on("dialog", handle_dialog)

    # Hot-load CheckVisaSlots extension access code if not already set
    async def inject_access_code():
        try:
            target_key = os.getenv("CHECKVISA_ACCESS_CODE", "4XYRAN").strip()
            if not target_key:
                return

            extension_id = None
            for _ in range(15):  # wait up to 3 seconds for extension background/service worker registration
                for worker in context.service_workers:
                    if "chrome-extension://" in worker.url and "sw.js" in worker.url:
                        extension_id = worker.url.split("/")[2]
                        break
                if extension_id:
                    break
                for bg_page in context.background_pages:
                    if "chrome-extension://" in bg_page.url and ("options.html" in bg_page.url or "popup.html" in bg_page.url):
                        extension_id = bg_page.url.split("/")[2]
                        break
                if extension_id:
                    break
                await asyncio.sleep(0.2)

            if extension_id:
                if "-" in extension_id:
                    log.info(f"Dynamic UUID detected ({extension_id}). Resolving static extension ID via popup.html...")
                    temp_page = await context.new_page()
                    try:
                        await temp_page.goto(f"chrome-extension://{extension_id}/popup.html", wait_until="domcontentloaded", timeout=10000)
                        static_id = await temp_page.evaluate("() => chrome.runtime.id")
                        if static_id:
                            log.info(f"Resolved static extension ID: {static_id}")
                            extension_id = static_id
                    except Exception as ex:
                        log.warning(f"Failed to resolve static extension ID via popup: {ex}")
                    finally:
                        try:
                            await temp_page.close()
                        except Exception:
                            pass

                opt_page = await context.new_page()
                await opt_page.goto(f"chrome-extension://{extension_id}/options.html", wait_until="domcontentloaded")
                
                # Check stored key
                stored_key = await opt_page.evaluate(
                    "() => new Promise(resolve => chrome.storage.local.get('apiKey', data => resolve(data.apiKey)))"
                )
                if stored_key != target_key:
                    log.info(f"Extension access code not set or different. Hot-loading '{target_key}'...")
                    await opt_page.evaluate(
                        """(key) => new Promise(resolve => {
                            chrome.storage.local.set({
                                apiKey: key,
                                valDated: new Date().toDateString()
                            }, resolve);
                        })""",
                        target_key
                    )
                    log.info("Extension access code successfully hot-loaded.")
                await opt_page.close()
        except Exception as ex:
            log.warning(f"CheckVisaSlots hot-loading skipped/failed: {ex}")

    asyncio.create_task(inject_access_code())

    # Wait briefly to let extension's auto-opened pages spawn
    await asyncio.sleep(2)

    # Clean up unwanted tabs (like extension welcome pages) and ensure focus
    try:
        for p in context.pages:
            if p != page and "usvisascheduling.com" not in p.url.lower():
                try:
                    await p.close()
                except Exception:
                    pass
        # Force the main working page to the front so Cloudflare can see it
        await page.bring_to_front()
    except Exception as e:
        log.warning(f"Failed to focus main page or close extra tabs: {e}")

    log.info(f"Connected — current page: {page.url}")
    return browser, context, page
