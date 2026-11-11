"""Atomic, encrypted-at-rest Web configuration in the persistent data directory."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from cryptography.fernet import InvalidToken

from sync_sub2api import SyncError, write_private_json

CONFIG_KEYS = {"config", "quota_rule"}
SECRET_FIELDS = {"upstream_password", "upstream_access_token", "upstream_refresh_token",
                 "target_password", "target_admin_api_key", "target_access_token", "target_refresh_token"}
_locks = {}
_locks_guard = threading.Lock()


class ConfigurationFile:
    def __init__(self, directory: Path, cipher):
        self.path = directory / "config.json"
        self.cipher = cipher
        with _locks_guard:
            self.lock = _locks.setdefault(str(self.path.resolve()), threading.RLock())

    def _read(self):
        if not self.path.exists():
            return {"version": 1}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise SyncError("config.json 无法读取；请恢复有效配置文件，现有文件不会被覆盖") from None
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1
                or any(key not in {"version", *CONFIG_KEYS} for key in value)
                or any(not isinstance(value[key], dict) for key in CONFIG_KEYS if key in value)):
            raise SyncError("config.json 格式或版本无效，请恢复有效配置文件")
        return value

    def _decode(self, key, value):
        value = copy.deepcopy(value)
        if key == "config":
            for field in SECRET_FIELDS & value.keys():
                token = value[field]
                if token == "":
                    continue
                if not isinstance(token, dict) or set(token) != {"encrypted"} or not isinstance(token["encrypted"], str):
                    raise SyncError("config.json 中的凭据格式无效，请通过 Web 保存密码和密钥")
                try:
                    value[field] = self.cipher.decrypt(token["encrypted"].encode()).decode("utf-8")
                except (InvalidToken, UnicodeError, ValueError):
                    raise SyncError("config.json 凭据无法解密；请恢复配套的 flow.key") from None
        return value

    def _encode(self, key, value):
        value = copy.deepcopy(value)
        if key == "config":
            for field in SECRET_FIELDS & value.keys():
                secret = value[field]
                if not isinstance(secret, str):
                    raise SyncError("配置凭据必须为文本")
                value[field] = {"encrypted": self.cipher.encrypt(secret.encode()).decode()} if secret else ""
        return value

    def get(self, key, default=None):
        with self.lock:
            document = self._read()
            return self._decode(key, document[key]) if key in document else copy.deepcopy(default)

    def put(self, key, value):
        with self.lock:
            document = self._read()
            document[key] = self._encode(key, value)
            try:
                write_private_json(self.path, document)
            except OSError:
                raise SyncError("config.json 保存失败，请检查数据目录是否可写；原配置保持不变") from None

    def migrate(self, legacy):
        """Import SQLite settings once; the file is authoritative after migration."""
        with self.lock:
            document = self._read()
            missing = {key: value for key, value in legacy.items() if key not in document}
            for key, value in missing.items():
                document[key] = self._encode(key, value)
            if not self.path.exists() or missing:
                write_private_json(self.path, document)
