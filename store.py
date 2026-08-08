import json
import os
import shutil
import threading

_lock = threading.Lock()


class UserStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.users_path = os.path.join(data_dir, "users.json")
        os.makedirs(data_dir, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.users_path):
            try:
                with open(self.users_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self):
        tmp = self.users_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.users_path)

    def register_user(self, chat_id: str) -> bool:
        with _lock:
            uid = str(chat_id)
            if uid not in self._data:
                self._data[uid] = {"registered": True, "accounts": {}}
                self._save()
                return True
            self._data[uid]["registered"] = True
            self._save()
            return False

    def is_registered(self, chat_id) -> bool:
        return str(chat_id) in self._data

    def account_home(self, chat_id, acc_name: str) -> str:
        return os.path.join(self.data_dir, str(chat_id), acc_name)

    def list_accounts(self, chat_id) -> list[dict]:
        uid = str(chat_id)
        accs = self._data.get(uid, {}).get("accounts", {})
        return [
            {"name": name, "email": info.get("email", "?")}
            for name, info in sorted(accs.items())
        ]

    def has_account(self, chat_id, acc_name: str) -> bool:
        return acc_name in self._data.get(str(chat_id), {}).get("accounts", {})

    def add_account(self, chat_id, acc_name: str, email: str):
        with _lock:
            uid = str(chat_id)
            self._data.setdefault(uid, {"registered": True, "accounts": {}})
            self._data[uid]["accounts"][acc_name] = {"email": email}
            self._save()

    def remove_account(self, chat_id, acc_name: str):
        with _lock:
            uid = str(chat_id)
            accs = self._data.get(uid, {}).get("accounts", {})
            if acc_name in accs:
                del accs[acc_name]
                self._save()
                home = self.account_home(chat_id, acc_name)
                if os.path.isdir(home):
                    shutil.rmtree(home, ignore_errors=True)

    def next_account_name(self, chat_id, max_accounts: int) -> str | None:
        existing = self.list_accounts(chat_id)
        if len(existing) >= max_accounts:
            return None
        return f"acc{len(existing) + 1}"
