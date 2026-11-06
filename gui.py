import json
import os
import queue
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


CODESPACE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CODESPACE_DIR
ACCOUNTS_FILE = CODESPACE_DIR / "accounts.json"
SETTINGS_FILE = CODESPACE_DIR / "settings.json"
PROFILES_DIR = CODESPACE_DIR / "profiles"
STATE_DIR = CODESPACE_DIR / "state"
LOGS_DIR = CODESPACE_DIR / "logs"
DEFAULT_EXTENSION_PATH = CODESPACE_DIR / "extensions" / "checkvisaslots"
EXIT_MESSAGES = {
    2: "Stopped: every account failed in a row (rate limit / Access Denied / Cloudflare / error).",
}


def ensure_dirs() -> None:
    for path in [PROFILES_DIR, STATE_DIR, LOGS_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def dedupe_accounts(accounts: list[dict]) -> list[dict]:
    deduped = {}
    order = []
    for account in accounts:
        name = (account.get("customer_name") or "").strip().lower()
        if not name:
            continue
        if name not in deduped:
            order.append(name)
        deduped[name] = account
    return [deduped[name] for name in order]


def split_cities(value: str) -> list[str]:
    cities = []
    for part in value.split(","):
        city = part.strip().upper()
        if city and city not in cities:
            cities.append(city)
    return cities or ["ANY"]


class CodespaceGui(tk.Tk):
    def __init__(self):
        super().__init__()
        ensure_dirs()
        self.title("CheckVisaStart Contribution Codespace")
        self.geometry("1120x740")
        self.minsize(980, 640)

        self.accounts = dedupe_accounts(load_json(ACCOUNTS_FILE, []))
        if ACCOUNTS_FILE.exists():
            save_json(ACCOUNTS_FILE, self.accounts)
        self.settings = load_json(SETTINGS_FILE, {
            "dwell_seconds": 12,
            "cycles": 0,
            "max_rotations": 20,
            "cooldown_minutes": 45,
            "min_gap_seconds": 15,
            "max_gap_seconds": 20,
            "browser_exe": "",
            "extension_path": str(DEFAULT_EXTENSION_PATH),
        })
        self.proc = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self._refresh_account_list()
        self._select_first_account()
        self.after(150, self._drain_log_queue)

    def _build_ui(self):
        root = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        root.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = ttk.Frame(root, padding=8)
        right = ttk.Frame(root, padding=8)
        root.add(left, weight=1)
        root.add(right, weight=2)

        ttk.Label(left, text="Accounts", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.account_list = tk.Listbox(left, height=14)
        self.account_list.pack(fill=tk.BOTH, expand=False, pady=(6, 8))
        self.account_list.bind("<<ListboxSelect>>", self._on_account_select)

        form = ttk.LabelFrame(left, text="Account")
        form.pack(fill=tk.X, pady=6)

        self.customer_var = tk.StringVar()
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.cities_var = tk.StringVar(value="NEW DELHI,MUMBAI,HYDERABAD,CHENNAI,KOLKATA")
        self.account_reschedule_var = tk.BooleanVar(value=False)
        self.q_vars = [(tk.StringVar(), tk.StringVar()) for _ in range(3)]

        self._entry(form, "Customer", self.customer_var, 0)
        self._entry(form, "Username", self.username_var, 1)
        self._entry(form, "Password", self.password_var, 2)
        self._entry(form, "Cities", self.cities_var, 3)
        ttk.Checkbutton(form, text="Reschedule Mode (check booked appointments)", variable=self.account_reschedule_var).grid(row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(2, 6))

        qbox = ttk.LabelFrame(left, text="Security Questions")
        qbox.pack(fill=tk.X, pady=6)
        for idx, (qvar, avar) in enumerate(self.q_vars):
            ttk.Label(qbox, text=f"Q{idx + 1} match").grid(row=idx * 2, column=0, sticky="w", padx=4, pady=(5, 2))
            ttk.Entry(qbox, textvariable=qvar).grid(row=idx * 2, column=1, sticky="ew", padx=4, pady=(5, 2))
            ttk.Label(qbox, text="Answer").grid(row=idx * 2 + 1, column=0, sticky="w", padx=4, pady=(2, 5))
            ttk.Entry(qbox, textvariable=avar).grid(row=idx * 2 + 1, column=1, sticky="ew", padx=4, pady=(2, 5))
        qbox.columnconfigure(1, weight=1)

        buttons = ttk.Frame(left)
        buttons.pack(fill=tk.X, pady=8)
        ttk.Button(buttons, text="Save Account", command=self._save_account).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Delete", command=self._delete_account).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Clear", command=self._clear_form).pack(side=tk.LEFT, padx=6)

        settings = ttk.LabelFrame(right, text="Run Settings")
        settings.pack(fill=tk.X)

        self.dwell_var = tk.StringVar(value=str(self.settings.get("dwell_seconds", 12)))
        self.cycles_var = tk.StringVar(value=str(self.settings.get("cycles", 0)))
        self.max_rotations_var = tk.StringVar(value=str(self.settings.get("max_rotations", 20)))
        self.cooldown_minutes_var = tk.StringVar(value=str(self.settings.get("cooldown_minutes", 45)))
        self.min_gap_var = tk.StringVar(value=str(self.settings.get("min_gap_seconds", 15)))
        self.max_gap_var = tk.StringVar(value=str(self.settings.get("max_gap_seconds", 20)))
        self.session_duration_var = tk.StringVar(value=str(self.settings.get("session_duration_minutes", 60)))
        self.switch_cooldown_var = tk.StringVar(value=str(self.settings.get("switch_cooldown_seconds", 300)))
        self.api_keys_var = tk.StringVar(value=",".join(self.settings.get("api_keys", ["4XYRAN"])))
        self.browser_var = tk.StringVar(value=self.settings.get("browser_exe", ""))
        self.extension_var = tk.StringVar(value=self.settings.get("extension_path", str(DEFAULT_EXTENSION_PATH)))

        self._entry(settings, "Dwell seconds", self.dwell_var, 0, width=12)
        self._entry(settings, "Rotations/account", self.max_rotations_var, 1, width=12)
        self._entry(settings, "Cooldown minutes", self.cooldown_minutes_var, 2, width=12)
        self._entry(settings, "Min gap seconds", self.min_gap_var, 3, width=12)
        self._entry(settings, "Max gap seconds", self.max_gap_var, 4, width=12)
        self._entry(settings, "Session duration (min)", self.session_duration_var, 5, width=12)
        self._entry(settings, "Switch cooldown (sec)", self.switch_cooldown_var, 6, width=12)
        self._entry(settings, "API Keys (comma-sep)", self.api_keys_var, 7)
        self._path_row(settings, "Browser exe", self.browser_var, 8, self._choose_browser)
        self._path_row(settings, "Extension", self.extension_var, 9, self._choose_extension)

        runbar = ttk.Frame(right)
        runbar.pack(fill=tk.X, pady=10)
        ttk.Button(runbar, text="Save Settings", command=self._save_settings).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(runbar, text="Start Contribution", command=self._start).pack(side=tk.LEFT, padx=6)
        ttk.Button(runbar, text="Stop", command=self._stop).pack(side=tk.LEFT, padx=6)
        ttk.Button(runbar, text="Open Folder", command=self._open_folder).pack(side=tk.LEFT, padx=6)
        ttk.Button(runbar, text="Open Logs", command=self._open_logs).pack(side=tk.LEFT, padx=6)

        note = (
            "Mode: contribution-only. The bot will not click submit. "
            "CAPTCHA / human verification remains manual in the visible browser."
        )
        ttk.Label(right, text=note, foreground="#555").pack(anchor="w", pady=(0, 8))

        ttk.Label(right, text="Logs", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.log_text = tk.Text(right, height=24, wrap="word", state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _entry(self, parent, label, var, row, show=None, width=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=5)
        ttk.Entry(parent, textvariable=var, show=show, width=width).grid(row=row, column=1, sticky="ew", padx=4, pady=5)
        parent.columnconfigure(1, weight=1)

    def _path_row(self, parent, label, var, row, command):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=5)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4, pady=5)
        ttk.Button(parent, text="Browse", command=command).grid(row=row, column=2, padx=4, pady=5)
        parent.columnconfigure(1, weight=1)

    def _refresh_account_list(self):
        self.account_list.delete(0, tk.END)
        for account in self.accounts:
            self.account_list.insert(tk.END, account.get("customer_name", "unnamed"))

    def _select_first_account(self):
        if self.accounts:
            self.account_list.selection_clear(0, tk.END)
            self.account_list.selection_set(0)
            self.account_list.activate(0)
            self._on_account_select()

    def _selected_index(self):
        selected = self.account_list.curselection()
        return selected[0] if selected else None

    def _on_account_select(self, _event=None):
        idx = self._selected_index()
        if idx is None:
            return
        account = self.accounts[idx]
        self.customer_var.set(account.get("customer_name", ""))
        self.username_var.set(account.get("username", ""))
        self.password_var.set(account.get("password", ""))
        self.cities_var.set(",".join(account.get("contributionCities") or account.get("ofcCities") or ["ANY"]))
        self.account_reschedule_var.set(account.get("is_reschedule", False))
        questions = list((account.get("security_questions") or {}).items())
        for i, (qvar, avar) in enumerate(self.q_vars):
            qvar.set(questions[i][0] if i < len(questions) else "")
            avar.set(questions[i][1] if i < len(questions) else "")

    def _clear_form(self):
        for var in [self.customer_var, self.username_var, self.password_var]:
            var.set("")
        self.cities_var.set("NEW DELHI,MUMBAI,HYDERABAD,CHENNAI,KOLKATA")
        self.account_reschedule_var.set(False)
        for qvar, avar in self.q_vars:
            qvar.set("")
            avar.set("")

    def _save_account(self):
        customer = self.customer_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get().strip()
        if not customer or not username or not password:
            messagebox.showerror("Missing fields", "Customer, username, and password are required.")
            return

        questions = {}
        for qvar, avar in self.q_vars:
            q = qvar.get().strip()
            a = avar.get().strip()
            if q and a:
                questions[q] = a

        account = {
            "customer_name": customer,
            "username": username,
            "password": password,
            "contributionCities": split_cities(self.cities_var.get()),
            "ofcCities": split_cities(self.cities_var.get()),
            "is_reschedule": self.account_reschedule_var.get(),
            "security_questions": questions,
        }

        idx = self._selected_index()
        existing_idx = next(
            (
                i for i, item in enumerate(self.accounts)
                if (item.get("customer_name") or "").strip().lower() == customer.lower()
            ),
            None,
        )
        if idx is not None:
            self.accounts[idx] = account
        elif existing_idx is not None:
            self.accounts[existing_idx] = account
        else:
            self.accounts.append(account)
        self.accounts = dedupe_accounts(self.accounts)
        save_json(ACCOUNTS_FILE, self.accounts)
        self._refresh_account_list()
        for i, item in enumerate(self.accounts):
            if (item.get("customer_name") or "").strip().lower() == customer.lower():
                self.account_list.selection_set(i)
                self.account_list.activate(i)
                break
        self._log(f"Saved account: {customer}")

    def _delete_account(self):
        idx = self._selected_index()
        if idx is None:
            return
        name = self.accounts[idx].get("customer_name", "account")
        if messagebox.askyesno("Delete account", f"Delete {name}?"):
            del self.accounts[idx]
            save_json(ACCOUNTS_FILE, self.accounts)
            self._refresh_account_list()
            self._clear_form()
            self._log(f"Deleted account: {name}")

    def _save_settings(self):
        # Parse API keys from comma-separated string
        raw_keys = self.api_keys_var.get().strip()
        api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()] or ["4XYRAN"]

        self.settings = {
            "dwell_seconds": int(self.dwell_var.get() or "12"),
            "cycles": int(self.cycles_var.get() or "0"),
            "max_rotations": int(self.max_rotations_var.get() or "20"),
            "cooldown_minutes": int(self.cooldown_minutes_var.get() or "45"),
            "min_gap_seconds": int(self.min_gap_var.get() or "15"),
            "max_gap_seconds": int(self.max_gap_var.get() or "20"),
            "session_duration_minutes": int(self.session_duration_var.get() or "60"),
            "switch_cooldown_seconds": int(self.switch_cooldown_var.get() or "300"),
            "api_keys": api_keys,
            "browser_exe": self.browser_var.get().strip(),
            "extension_path": self.extension_var.get().strip(),
        }
        save_json(SETTINGS_FILE, self.settings)
        self._log("Saved settings.")

    def _choose_browser(self):
        path = filedialog.askopenfilename(title="Choose browser executable", filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if path:
            self.browser_var.set(path)

    def _choose_extension(self):
        path = filedialog.askdirectory(title="Choose unpacked extension folder")
        if path:
            self.extension_var.set(path)

    def _open_path(self, path: Path):
        # os.startfile is Windows-only; use the desktop opener elsewhere.
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _open_folder(self):
        self._open_path(CODESPACE_DIR)

    def _open_logs(self):
        self._open_path(LOGS_DIR)

    def _start(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Already running", "The contribution process is already running.")
            return
        if not self.accounts:
            messagebox.showerror("No accounts", "Add at least one account first.")
            return
        self._save_settings()

        extension_path = Path(self.extension_var.get().strip()).expanduser()
        if not extension_path.is_absolute():
            extension_path = (CODESPACE_DIR / extension_path).resolve()
        if not (extension_path / "manifest.json").exists():
            messagebox.showerror("Extension missing", f"No manifest.json found in:\n{extension_path}")
            return

        env = os.environ.copy()
        env["CHECKVISA_ACCOUNTS_FILE"] = str(ACCOUNTS_FILE)
        env["CHECKVISA_PROFILE_ROOT"] = str(PROFILES_DIR)
        env["CHECKVISA_STATE_DIR"] = str(STATE_DIR)
        env["CHECKVISA_EXTENSION_PATH"] = str(extension_path)
        env["USE_FASTCAPTCHA"] = "false"
        env["PYTHONIOENCODING"] = "utf-8"

        browser_exe = self.browser_var.get().strip()
        if browser_exe:
            env["CHECKVISA_BROWSER_EXE"] = browser_exe

        cmd = [
            sys.executable,
            "-m",
            "src.main",
            "--dwell-seconds",
            str(int(self.dwell_var.get() or "12")),
            "--max-rotations",
            str(int(self.max_rotations_var.get() or "20")),
            "--cooldown-seconds",
            str(int(self.cooldown_minutes_var.get() or "45") * 60),
            "--min-gap-seconds",
            str(int(self.min_gap_var.get() or "15")),
            "--max-gap-seconds",
            str(int(self.max_gap_var.get() or "20")),
            "--session-duration-minutes",
            str(int(self.session_duration_var.get() or "60")),
            "--switch-cooldown-seconds",
            str(int(self.switch_cooldown_var.get() or "300")),
        ]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOGS_DIR / f"run_{timestamp}.log"
        self._log(f"Starting: {' '.join(cmd)}")
        self._log(f"Logging to: {log_path}")
        
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Own process group on Linux/macOS so Stop can close Chrome and every runner.
            start_new_session=(os.name != "nt"),
        )
        threading.Thread(target=self._read_process, args=(self.proc, log_path), daemon=True).start()

    def _stop(self):
        if not self.proc or self.proc.poll() is not None:
            self._log("No running process.")
            return
        self._log("Stopping process...")
        if os.name == "nt":
            self.proc.terminate()
            return
        # Signal the whole group: orchestrator, runners and the Chrome they launched.
        pid = self.proc.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        self.after(8000, lambda: self._force_stop(pid))

    def _force_stop(self, pid: int):
        try:
            os.killpg(pid, signal.SIGKILL)
            self._log("Force-closed remaining browser processes.")
        except ProcessLookupError:
            pass

    def _read_process(self, proc, log_path: Path):
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
                self.log_queue.put(line.rstrip())
            code = proc.wait()
            self.log_queue.put(f"Process exited with code {code}")
            if code in EXIT_MESSAGES:
                self.log_queue.put(EXIT_MESSAGES[code])

    def _drain_log_queue(self):
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._log(line)
        self.after(150, self._drain_log_queue)

    def _log(self, text: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def destroy(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        super().destroy()


if __name__ == "__main__":
    app = CodespaceGui()
    app.mainloop()
