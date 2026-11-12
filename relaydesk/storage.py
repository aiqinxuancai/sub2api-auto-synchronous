"""Durable settings, jobs, logs and monthly execution receipts."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from .configuration import CONFIG_KEYS, ConfigurationFile


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.path = directory / "flow.sqlite3"
        key_path = directory / "flow.key"
        if not key_path.exists():
            try:
                descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(Fernet.generate_key())
            except FileExistsError:
                pass
        self.cipher = Fernet(key_path.read_bytes())
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, finished REAL,
                    payload BLOB NOT NULL, result TEXT, error TEXT,
                    request_key TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    level TEXT NOT NULL, message TEXT NOT NULL, job_id TEXT);
                CREATE TABLE IF NOT EXISTS receipts (
                    period TEXT NOT NULL, user_id INTEGER NOT NULL, state TEXT NOT NULL,
                    job_id TEXT NOT NULL, error TEXT, updated REAL NOT NULL,
                    PRIMARY KEY(period, user_id));
                CREATE TABLE IF NOT EXISTS artifacts (
                    job_id TEXT PRIMARY KEY, value BLOB NOT NULL);
            """)
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        self.configuration = ConfigurationFile(directory, self.cipher)
        with self.connect() as connection:
            rows = connection.execute("SELECT key,value FROM settings WHERE key IN ('config','quota_rule')").fetchall()
            legacy = {row["key"]: self.decrypt(row["value"]) for row in rows}
            self.configuration.migrate(legacy)
            # Remove stale copies only after the atomic file write succeeds.
            connection.execute("DELETE FROM settings WHERE key IN ('config','quota_rule')")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value))

    def get(self, key, default=None):
        if key in CONFIG_KEYS:
            return self.configuration.get(key, default)
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return self.decrypt(row[0]) if row else default

    def put(self, key, value):
        if key in CONFIG_KEYS:
            self.configuration.put(key, value)
            return
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, self.encrypt(value)))

    def setup_admin(self, password_hash):
        with self.connect() as connection:
            try:
                connection.execute("INSERT INTO settings VALUES ('admin',?)", (self.encrypt(password_hash),))
            except sqlite3.IntegrityError:
                return False
        return True

    def new_session(self):
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            connection.execute("INSERT INTO sessions VALUES (?,?,?)",
                               (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time() + 43200))
        return token, csrf

    def session(self, token):
        with self.connect() as connection:
            row = connection.execute("SELECT csrf FROM sessions WHERE token=? AND expires>?",
                                     (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return row[0] if row else None

    def logout(self, token):
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token=?",
                               (hashlib.sha256(token.encode()).hexdigest(),))

    def create_job(self, kind, payload, request_key=None):
        job_id = uuid.uuid4().hex
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO jobs (id,kind,status,created,payload,request_key) VALUES (?,?,?,?,?,?)",
                    (job_id, kind, "queued", time.time(), self.encrypt(payload), request_key))
            except sqlite3.IntegrityError:
                row = connection.execute("SELECT id FROM jobs WHERE request_key=?", (request_key,)).fetchone()
                return row[0], False
        return job_id, True

    def job_payload(self, job_id):
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self.decrypt(row[0]) if row else None

    def set_job(self, job_id, status, result=None, error=None):
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, started=CASE WHEN ?='running' THEN COALESCE(started,?) ELSE started END, finished=?, result=?, error=? WHERE id=?",
                (status, status, time.time(), None if status in {"queued", "running"} else time.time(),
                 json.dumps(result, ensure_ascii=False) if result is not None else None, error, job_id))

    JOB_COLUMNS = ("SELECT id,kind,status,created,started,finished,result,error, "
                   "EXISTS(SELECT 1 FROM artifacts WHERE artifacts.job_id=jobs.id) AS export_available FROM jobs ")

    def jobs(self, limit=30, before=None):
        """Newest first; `before` pages back through older history by creation time."""
        query, params = self.JOB_COLUMNS, []
        if before is not None:
            query += "WHERE created<? "
            params.append(before)
        with self.connect() as connection:
            rows = connection.execute(query + "ORDER BY created DESC LIMIT ?", (*params, limit)).fetchall()
        return [dict(row, result=json.loads(row["result"]) if row["result"] else None) for row in rows]

    def job(self, job_id):
        with self.connect() as connection:
            row = connection.execute(self.JOB_COLUMNS + "WHERE id=?", (job_id,)).fetchone()
        return dict(row, result=json.loads(row["result"]) if row["result"] else None) if row else None

    def log(self, level, message, job_id=None):
        with self.connect() as connection:
            connection.execute("INSERT INTO logs(at,level,message,job_id) VALUES (?,?,?,?)",
                               (time.time(), level, message, job_id))
            connection.execute("DELETE FROM logs WHERE id < (SELECT MAX(id)-10000 FROM logs)")

    def logs(self, after=0, level=None, search="", limit=300, job_id=None):
        """The newest `limit` matching lines after id `after`, oldest first."""
        query, params = "SELECT * FROM logs WHERE id>?", [after]
        if job_id:
            query += " AND job_id=?"
            params.append(job_id)
        if level:
            query += " AND level=?"
            params.append(level)
        if search:
            query += " AND message LIKE ? ESCAPE '\\'"
            params.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()][::-1]

    def claim_receipt(self, period, user_id, job_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM receipts WHERE period=? AND user_id=?",
                                     (period, user_id)).fetchone()
            if row and row[0] in {"success", "uncertain", "running"}:
                return False
            connection.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?)",
                               (period, user_id, "running", job_id, None, time.time()))
        return True

    def receipt(self, period, user_id, state, job_id, error=None):
        with self.connect() as connection:
            connection.execute("UPDATE receipts SET state=?,job_id=?,error=?,updated=? WHERE period=? AND user_id=?",
                               (state, job_id, error, time.time(), period, user_id))

    def receipts(self, limit=100):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM receipts ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()]

    def save_artifact(self, job_id, value):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?)", (job_id, self.encrypt(value)))

    def artifact(self, job_id):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM artifacts WHERE job_id=?", (job_id,)).fetchone()
        return self.decrypt(row[0]) if row else None

    def recover_jobs(self):
        with self.connect() as connection:
            recoverable = [row[0] for row in connection.execute(
                "SELECT id FROM jobs WHERE kind='quota_reset' AND status IN ('queued','running')")]
            connection.execute(
                "UPDATE jobs SET status='interrupted', finished=?, error='服务重启，任务已中断；可查看结果后重试' "
                "WHERE status IN ('queued','running')", (time.time(),))
            # A lost acknowledgement must not cause a second monthly reset.
            connection.execute("UPDATE receipts SET state='uncertain',error='服务重启时执行结果未确认' WHERE state='running'")
        return recoverable
