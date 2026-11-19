"""Small durable store; every public value is JSON, never a pickle."""
import copy
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "launcher.sqlite3"
        self.lock = threading.RLock()
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT, updated REAL, PRIMARY KEY(kind,id))")
        self.path.chmod(0o600)

    def connection(self):
        return sqlite3.connect(str(self.path), timeout=15)

    def put(self, kind, value):
        value = copy.deepcopy(value)
        value.setdefault("id", uuid.uuid4().hex)
        value["updated_unix"] = time.time()
        with self.lock, self.connection() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                       (kind, value["id"], json.dumps(value, ensure_ascii=False, allow_nan=False), value["updated_unix"]))
        return value

    def get(self, kind, identifier):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        if row is None:
            raise KeyError("找不到记录：" + str(identifier))
        return json.loads(row[0])

    def list(self, kind):
        with self.lock, self.connection() as db:
            rows = db.execute("SELECT body FROM records WHERE kind=? ORDER BY updated DESC", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, kind, identifier):
        with self.lock, self.connection() as db:
            result = db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, identifier))
            if result.rowcount == 0:
                raise KeyError("找不到记录：" + str(identifier))

    def update(self, kind, identifier, **changes):
        with self.lock:
            value = self.get(kind, identifier)
            value.update(changes)
            return self.put(kind, value)

    def event(self, identifier, status, message, **changes):
        with self.lock:
            value = self.get("session", identifier)
            events = value.get("events", [])
            events.append(dict(status=status, message=message, time=time.time()))
            value.update(status=status, events=events[-80:], **changes)
            return self.put("session", value)
