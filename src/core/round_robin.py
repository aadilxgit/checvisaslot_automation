"""
Round-robin scheduler for rotating multiple accounts across multiple API keys.

Supports two levels of rotation:
  1. Outer: API keys rotate after all accounts have been cycled.
  2. Inner: Accounts rotate within each API key session.

Persists state to disk so that if the application restarts,
it resumes from the correct account and API key.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


class RoundRobinScheduler:
    """
    Manages round-robin rotation of accounts and API keys.

    Flow:  Key1→A1, Key1→A2, ..., Key1→AN, Key2→A1, Key2→A2, ..., Key2→AN, Key1→A1, ...

    Persists state to disk for restart resilience.
    """

    def __init__(self, accounts: list[dict], api_keys: list[str], state_dir: Path) -> None:
        self.accounts = {acc["customer_name"]: acc for acc in accounts}
        self.api_keys = api_keys if api_keys else ["4XYRAN"]
        self.state_dir = state_dir

        self.state_file = self.state_dir / "round_robin_state.json"

        self.account_order: list[str] = []
        self.current_account_index: int = 0
        self.current_api_key_index: int = 0
        self.last_active_account: str | None = None
        self.last_api_key: str | None = None
        self.last_switch_time: str | None = None
        self.completed_full_cycles: int = 0  # incremented when ALL keys × ALL accounts have been cycled

        self.load_state()

    @property
    def current_api_key(self) -> str:
        """The API key currently in use."""
        return self.api_keys[self.current_api_key_index]

    def load_state(self) -> None:
        """
        Loads persisted state from disk. Reconciles existing state with current
        accounts and API keys list (handles additions and removals).
        """
        current_customers = list(self.accounts.keys())

        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)

                # --- Reconcile account order ---
                saved_order = state.get("account_order", [])
                saved_acc_index = state.get("current_account_index", 0)

                new_order = [c for c in saved_order if c in current_customers]
                for c in current_customers:
                    if c not in new_order:
                        new_order.append(c)

                self.account_order = new_order

                if not self.account_order:
                    self.current_account_index = 0
                elif saved_order:
                    if saved_acc_index >= len(saved_order):
                        saved_acc_index = 0
                    target = saved_order[saved_acc_index] if saved_acc_index < len(saved_order) else None
                    if target and target in self.account_order:
                        self.current_account_index = self.account_order.index(target)
                    else:
                        self.current_account_index = min(saved_acc_index, len(self.account_order) - 1)
                else:
                    self.current_account_index = 0

                # --- Reconcile API key index ---
                saved_api_keys = state.get("api_keys", [])
                saved_key_index = state.get("current_api_key_index", 0)
                
                if saved_api_keys and saved_key_index < len(saved_api_keys):
                    target_key = saved_api_keys[saved_key_index]
                    if target_key in self.api_keys:
                        self.current_api_key_index = self.api_keys.index(target_key)
                    else:
                        self.current_api_key_index = min(saved_key_index, len(self.api_keys) - 1)
                elif saved_key_index < len(self.api_keys):
                    self.current_api_key_index = saved_key_index
                else:
                    self.current_api_key_index = 0

                self.last_active_account = state.get("last_active_account")
                self.last_api_key = state.get("last_api_key")
                self.last_switch_time = state.get("last_switch_time")
                self.completed_full_cycles = state.get("completed_full_cycles", 0)

            except (json.JSONDecodeError, OSError):
                self.account_order = current_customers
                self.current_account_index = 0
                self.current_api_key_index = 0
        else:
            self.account_order = current_customers
            self.current_account_index = 0
            self.current_api_key_index = 0

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.save_state()

    def save_state(self) -> None:
        """Persists current rotation state to disk."""
        state = {
            "account_order": self.account_order,
            "current_account_index": self.current_account_index,
            "api_keys": self.api_keys,
            "current_api_key_index": self.current_api_key_index,
            "last_active_account": self.last_active_account,
            "last_api_key": self.last_api_key,
            "last_switch_time": self.last_switch_time,
            "completed_full_cycles": self.completed_full_cycles,
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def next_account(self) -> dict | None:
        """
        Returns the next account dict in round-robin order WITHOUT advancing.
        Call mark_session_complete() to advance.
        """
        if not self.account_order:
            return None
        customer = self.account_order[self.current_account_index]
        return self.accounts[customer]

    def mark_session_complete(self, customer: str) -> None:
        """
        Records session complete. Advances account index.
        When all accounts are done for the current API key, advances to next API key.
        When all API keys are done, increments completed_full_cycles.
        """
        if not self.account_order:
            return

        self.last_active_account = customer
        self.last_api_key = self.current_api_key
        self.last_switch_time = datetime.now(timezone.utc).isoformat()
        self._advance()

    def skip_current(self) -> None:
        """Moves past the current account without running it (e.g. it is in cooldown)."""
        if self.account_order:
            self._advance()

    def _advance(self) -> None:
        self.current_account_index += 1
        if self.current_account_index >= len(self.account_order):
            # All accounts done for this API key → advance to next key
            self.current_account_index = 0
            self.current_api_key_index += 1
            if self.current_api_key_index >= len(self.api_keys):
                # All keys done → full cycle complete
                self.current_api_key_index = 0
                self.completed_full_cycles += 1

        self.save_state()
