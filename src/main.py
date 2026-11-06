import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

from src.auth.browser import ensure_chrome_debug_running
from src.core.round_robin import RoundRobinScheduler


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


ACCOUNTS_FILE = Path(os.getenv("CHECKVISA_ACCOUNTS_FILE", Path(__file__).parent.parent / "accounts.json"))
SETTINGS_FILE = Path(os.getenv("CHECKVISA_SETTINGS_FILE", Path(__file__).parent.parent / "settings.json"))
PROFILE_ROOT = Path(os.getenv("CHECKVISA_PROFILE_ROOT", Path(__file__).parent.parent))
STATE_DIR = Path(os.getenv("CHECKVISA_STATE_DIR", Path(__file__).parent.parent / "state"))
PYTHON = sys.executable
BASE_CDP_PORT = int(os.getenv("CHECKVISA_BASE_CDP_PORT", "9222"))
OFC_URL = "https://www.usvisascheduling.com/en-US/ofc-schedule/"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [SCHEDULER] {message}", flush=True)


class SchedulerBrowserLog:
    def info(self, message: str) -> None:
        log(message)

    def warning(self, message: str) -> None:
        log(f"WARNING {message}")

    def error(self, message: str) -> None:
        log(f"ERROR {message}")


def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        raise FileNotFoundError(f"accounts.json not found: {ACCOUNTS_FILE}")
    accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("accounts.json must contain a non-empty list")
    deduped = {}
    order = []
    for account in accounts:
        name = (account.get("customer_name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in deduped:
            order.append(key)
        deduped[key] = account
    return [deduped[key] for key in order]


def load_api_keys() -> list[str]:
    """Load API keys from settings.json. Falls back to ['4XYRAN'] if not configured."""
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            keys = settings.get("api_keys", [])
            if isinstance(keys, list) and keys:
                return [k.strip() for k in keys if k.strip()]
        except Exception:
            pass
    return ["4XYRAN"]


# Orchestrator exit code: every account failed in a row (error / block / rate limit / Access Denied).
EXIT_ALL_FAILED = 2


def account_cities(account: dict) -> str:
    cities = account.get("contributionCities") or account.get("ofcCities") or ["ANY"]
    if isinstance(cities, str):
        return cities
    return ",".join(cities)


def state_file(customer: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in customer)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"scheduler_state_{safe}.json"


def write_account_state(customer: str, payload: dict) -> None:
    data = {
        "customer_name": customer,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    state_file(customer).write_text(json.dumps(data, indent=2), encoding="utf-8")


def relay_output(proc: subprocess.Popen, label: str, ready_event: threading.Event | None = None) -> None:
    if not proc.stdout:
        return
    for line in proc.stdout:
        line = line.rstrip()
        print(f"[{label}] {line}", flush=True)
        if ready_event and "[READY]" in line:
            ready_event.set()


def _posix_port_pids(port: int) -> set[int]:
    try:
        result = subprocess.run(
            ["lsof", "-t", "-i", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return set()
    return {int(p) for p in result.stdout.split() if p.isdigit()}


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def kill_port_process(port: int) -> None:
    if os.name != "nt":
        # Linux/macOS: close whatever is listening on the CDP port (Chrome).
        pids = _posix_port_pids(port)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                log(f"Sent SIGTERM to browser process on port {port} (PID {pid})")
            except OSError as exc:
                log(f"Failed to signal PID {pid}: {exc}")
        deadline = time.time() + 5
        while pids and time.time() < deadline and _port_open(port):
            time.sleep(0.5)
        if _port_open(port):
            for pid in _posix_port_pids(port):
                try:
                    os.kill(pid, signal.SIGKILL)
                    log(f"Force-killed PID {pid} on port {port}")
                except OSError:
                    pass
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception:
        return
    pids = set()
    needles = (f"127.0.0.1:{port}", f"0.0.0.0:{port}", f"[::]:{port}", f"[::1]:{port}")
    for line in result.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        if not any(needle in line for needle in needles):
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.add(parts[-1])
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, timeout=10)
            log(f"Closed browser process on port {port} (PID {pid})")
        except Exception as exc:
            log(f"Failed to close PID {pid}: {exc}")

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                time.sleep(0.5)
        except OSError:
            return
    log(f"WARNING port {port} still appears active after cleanup")


def start_process(cmd: list[str], env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        cwd=str(Path(__file__).parent.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def has_saved_profile(profile_dir: Path) -> bool:
    default_dir = profile_dir / "Default"
    return default_dir.exists() and any(
        (default_dir / name).exists()
        for name in ["Network", "Cookies", "Local Storage", "Session Storage"]
    )


def contribution_cmd(account: dict, port: int, args: argparse.Namespace, saved_session_only: bool = False) -> list[str]:
    cmd = [
        PYTHON,
        "-m",
        "src.core.contribution_runner",
        "--cdp-port",
        str(port),
        "--customer",
        account["customer_name"],
        "--cities",
        account_cities(account),
        "--dwell-seconds",
        str(args.dwell_seconds),
        "--cycles",
        "0",
        "--max-rotations",
        str(args.max_rotations),
        "--min-gap-seconds",
        str(args.min_gap_seconds),
        "--max-gap-seconds",
        str(args.max_gap_seconds),
    ]
    if saved_session_only:
        cmd.append("--saved-session-only")
    if account.get("is_reschedule"):
        cmd.append("--reschedule")
    return cmd


# Session outcomes returned by run_account().
OK = "ok"            # contributed (rotation limit reached, or session duration expired)
BLOCKED = "blocked"  # exit 43: Cloudflare block, rate limit (429), Access Denied
ERROR = "error"      # login failure/timeout or any other runner error


def outcome_for(code: int | None) -> str:
    if code == 0:
        return OK
    if code == 43:
        return BLOCKED
    return ERROR


def run_account(account: dict, args: argparse.Namespace, env: dict, api_key: str = "4XYRAN") -> tuple[str, int | None]:
    """Run a single account's login + contribution cycle with the given API key.
    Returns (outcome, exit_code)."""
    customer = account["customer_name"]
    port = BASE_CDP_PORT
    profile_dir = PROFILE_ROOT / f"chrome_profile_{customer}"

    # Set the API key for this session so browser.py picks it up
    env["CHECKVISA_ACCESS_CODE"] = api_key

    kill_port_process(port)
    write_account_state(customer, {
        "status": "starting",
        "rotation_count": 0,
        "max_rotations": args.max_rotations,
    })

    if has_saved_profile(profile_dir):
        log(f"Saved profile found for {customer}; trying direct OFC contribution without login.")
        is_reschedule = account.get("is_reschedule", False)
        start_url = "https://www.usvisascheduling.com/en-US/ofc-schedule/?reschedule=true" if is_reschedule else OFC_URL
        ensure_chrome_debug_running(port, str(profile_dir), SchedulerBrowserLog(), start_url=start_url)
        write_account_state(customer, {
            "status": "checking_saved_session",
            "rotation_count": 0,
            "max_rotations": args.max_rotations,
        })
        contrib_proc = start_process(contribution_cmd(account, port, args, saved_session_only=True), env)
        threading.Thread(target=relay_output, args=(contrib_proc, f"contrib:{customer}"), daemon=True).start()
        code = contrib_proc.wait()
        log(f"Saved-session contribution exited for {customer} with code {code}")
        kill_port_process(port)
        if code == 0:
            return OK, code
        time.sleep(2)
        log(f"Saved session was not usable for {customer}; falling back to login.")

    login_cmd = [
        PYTHON,
        "-m",
        "src.core.login_runner",
        "--username",
        account["username"],
        "--password",
        account["password"],
        "--cdp-port",
        str(port),
        "--customer",
        customer,
        "--profile-dir",
        str(profile_dir),
    ]

    log(f"Starting account {customer} on port {port} (key: {api_key[:4]}...)")
    login_proc = start_process(login_cmd, env)
    ready = threading.Event()
    threading.Thread(target=relay_output, args=(login_proc, f"login:{customer}", ready), daemon=True).start()

    deadline = time.time() + args.login_timeout_seconds
    while time.time() < deadline:
        if ready.is_set():
            break
        if login_proc.poll() is not None:
            break
        time.sleep(1)

    if not ready.is_set():
        code = login_proc.poll()
        write_account_state(customer, {"status": f"login_failed_or_timeout", "rotation_count": 0})
        log(f"Login failed or timeout for {customer} (exit code {code}); moving to next.")
        try:
            login_proc.terminate()
        except Exception:
            pass
        kill_port_process(port)
        # login_runner exits 0 even after a handled login failure, so no [READY] is always a failure.
        return (BLOCKED if code == 43 else ERROR), code

    write_account_state(customer, {
        "status": "contributing",
        "rotation_count": 0,
        "max_rotations": args.max_rotations,
    })
    
    contrib_proc = start_process(contribution_cmd(account, port, args), env)
    threading.Thread(target=relay_output, args=(contrib_proc, f"contrib:{customer}"), daemon=True).start()

    # Enforce session duration: wait for contribution to finish OR session timer to expire
    session_deadline = time.time() + (args.session_duration_minutes * 60)
    expired = False
    while contrib_proc.poll() is None:
        if time.time() >= session_deadline:
            expired = True
            log(f"Session duration ({args.session_duration_minutes} min) expired for {customer}. Terminating contribution.")
            try:
                contrib_proc.terminate()
            except Exception:
                pass
            # Give it a few seconds to clean up
            try:
                contrib_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    contrib_proc.kill()
                except Exception:
                    pass
            break
        time.sleep(5)
    
    code = contrib_proc.poll()
    log(f"Contribution runner exited for {customer} with code {code}")

    try:
        login_proc.terminate()
    except Exception:
        pass
    kill_port_process(port)
    # Terminated by the session timer = a normal, successful session.
    return (OK if expired else outcome_for(code)), code


def main() -> None:
    parser = argparse.ArgumentParser(description="Round-robin contribution scheduler with API key rotation.")
    parser.add_argument("--dwell-seconds", type=int, default=12)
    parser.add_argument("--max-rotations", type=int, default=20)
    parser.add_argument("--cooldown-seconds", type=int, default=2700, help="Per-account cooldown after every session (default 45 min). Accounts in cooldown are skipped.")
    parser.add_argument("--login-timeout-seconds", type=int, default=900)
    parser.add_argument("--min-gap-seconds", type=int, default=15)
    parser.add_argument("--max-gap-seconds", type=int, default=20)
    parser.add_argument("--session-gap-seconds", type=int, default=300, help="Seconds to wait between consecutive account sessions")
    parser.add_argument("--session-duration-minutes", type=int, default=60, help="Max minutes one account contributes before switching")
    parser.add_argument("--switch-cooldown-seconds", type=int, default=300, help="Cooldown seconds between account switches")
    args = parser.parse_args()

    accounts = load_accounts()
    api_keys = load_api_keys()
    log(f"Loaded {len(accounts)} account(s), {len(api_keys)} API key(s). Session={args.session_duration_minutes}min")
    log(f"API keys: {', '.join(k[:4] + '...' for k in api_keys)}")

    # Create round-robin scheduler (rotates both accounts and API keys)
    scheduler = RoundRobinScheduler(accounts, api_keys, STATE_DIR)
    names = [a["customer_name"] for a in accounts]
    log(f"Account order: {' → '.join(names)}")
    log(f"Resuming: account={scheduler.account_order[scheduler.current_account_index]}, "
        f"key={scheduler.current_api_key[:4]}..., full cycles={scheduler.completed_full_cycles}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    # Accounts that failed (error / block / rate limit) since the last successful session.
    # When every account is in here, nothing can contribute: log it and stop.
    failed_since_success: dict[str, str] = {}
    # customer -> time.time() when the account may run again (memory only; lost on restart).
    cooldown_until: dict[str, float] = {}
    exit_code = 0
    stopped = False

    def stop(_signum=None, _frame=None):
        nonlocal stopped
        stopped = True
        log("Stop requested. Finishing current cleanup.")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopped:
        account = scheduler.next_account()
        if account is None:
            log("No accounts available. Exiting.")
            break

        customer = account["customer_name"]
        current_key = scheduler.current_api_key

        now = time.time()
        if cooldown_until.get(customer, 0) > now:
            ready_times = [cooldown_until.get(name, 0) for name in scheduler.account_order]
            if min(ready_times) > now:
                # Every account is cooling down: sleep until the first one is free.
                first_free = min(ready_times)
                wait = int(first_free - now) + 1
                log(f"All {len(ready_times)} accounts in cooldown. Next free at {time.strftime('%H:%M:%S', time.localtime(first_free))}; sleeping {wait}s.")
                end = time.time() + wait
                while time.time() < end and not stopped:
                    time.sleep(min(5, max(0.1, end - time.time())))
                continue
            left = int(cooldown_until[customer] - now)
            log(f"{customer} in cooldown ({left // 60}m {left % 60}s left); skipping to next account.")
            scheduler.skip_current()
            continue

        log(f"=== Round-Robin → '{customer}' | key={current_key[:4]}... "
            f"(acct {scheduler.current_account_index+1}/{len(scheduler.account_order)}, "
            f"key {scheduler.current_api_key_index+1}/{len(api_keys)}, "
            f"cycle {scheduler.completed_full_cycles}) ===")

        outcome, code = run_account(account, args, env, api_key=current_key)

        scheduler.mark_session_complete(customer)
        log(f"Advanced → next: {scheduler.account_order[scheduler.current_account_index]}, "
            f"key: {scheduler.current_api_key[:4]}..., cycles: {scheduler.completed_full_cycles}")

        if stopped:
            break

        if args.cooldown_seconds > 0:
            cooldown_until[customer] = time.time() + args.cooldown_seconds
            log(f"{customer} cooling down for {args.cooldown_seconds // 60} min (until {time.strftime('%H:%M:%S', time.localtime(cooldown_until[customer]))}).")

        if outcome == BLOCKED:
            log(f"Block / rate limit / Access Denied (code {code}) for {customer}. Deleting user session; will retry next round.")
            profile_dir = PROFILE_ROOT / f"chrome_profile_{customer}"
            import shutil
            try:
                shutil.rmtree(profile_dir / "Default" / "Sessions", ignore_errors=True)
                shutil.rmtree(profile_dir / "Default" / "Network", ignore_errors=True)
            except Exception as ex:
                log(f"WARNING Failed to delete session for {customer}: {ex}")
        elif outcome == ERROR:
            log(f"{customer}: session failed (code {code}); will retry next round.")

        write_account_state(customer, {
            "status": f"session_{outcome}",
            "exit_code": code,
            "max_rotations": args.max_rotations,
            "cooldown_until": datetime.fromtimestamp(cooldown_until[customer], tz=timezone.utc).isoformat() if customer in cooldown_until else None,
        })

        if outcome == OK:
            if failed_since_success:
                log(f"{customer} contributed; resetting failure tracker ({len(failed_since_success)} failed before).")
            failed_since_success.clear()
        else:
            failed_since_success[customer] = f"{outcome} (code {code})"
            log(f"Failures since last success: {len(failed_since_success)}/{len(accounts)} account(s).")
            if len(failed_since_success) >= len(accounts):
                log("ALL ACCOUNTS FAILED in a row (rate limit / Access Denied / Cloudflare / error). Stopping automation.")
                for name, why in failed_since_success.items():
                    log(f"  - {name}: {why}")
                exit_code = EXIT_ALL_FAILED
                break

        switch_cooldown = args.switch_cooldown_seconds
        if switch_cooldown > 0 and not stopped:
            log(f"Switch cooldown: waiting {switch_cooldown}s...")
            gap_remaining = switch_cooldown
            while gap_remaining > 0 and not stopped:
                sleep_time = min(5, gap_remaining)
                time.sleep(sleep_time)
                gap_remaining -= sleep_time

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
