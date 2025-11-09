import json
import os
import time
import uuid
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, RowMapping


def _default_sqlite_path() -> str:
    in_docker = os.path.exists("/.dockerenv")
    if in_docker:
        return "/data/akv.sqlite"
    return os.path.join(os.getcwd(), "data", "akv.sqlite")


class Storage:
    def __init__(self):
        backend = os.getenv("FAKE_AKV_STORAGE", "sqlite")
        if backend == "memory":
            self._mem: Dict[str, Dict[str, dict]] = {}
            self._deleted: Dict[str, dict] = {}
            self._engine = None
        else:
            path = os.getenv("FAKE_AKV_SQLITE_PATH") or _default_sqlite_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
            self._init_sqlite()

    def _init_sqlite(self):
        assert isinstance(self._engine, Engine)
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                CREATE TABLE IF NOT EXISTS secrets (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    value TEXT NOT NULL,
                    tags TEXT,
                    attributes TEXT,
                    enabled INTEGER DEFAULT 1,
                    deleted INTEGER DEFAULT 0,
                    created INTEGER,
                    updated INTEGER,
                    PRIMARY KEY (name, version)
                );
                """
                )
            )

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @staticmethod
    def _new_version() -> str:
        return uuid.uuid4().hex

    def put_secret(
        self, name: str, value: str, tags: Optional[dict], attributes: Optional[dict]
    ) -> Tuple[str, dict]:
        version = self._new_version()
        created = updated = self._now()
        attrs = attributes or {}
        if self._engine is None:
            versions = self._mem.setdefault(name, {})
            versions[version] = {
                "value": value,
                "tags": tags or {},
                "attributes": attrs,
                "enabled": True,
                "deleted": False,
                "created": created,
                "updated": updated,
            }
        else:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO secrets(name, version, value, tags, attributes, enabled, deleted, created, updated)\n"
                        "VALUES(:n,:v,:val,:t,:a,1,0,:c,:u)"
                    ),
                    {
                        "n": name,
                        "v": version,
                        "val": value,
                        "t": (tags and json_dumps(tags)) or None,
                        "a": (attrs and json_dumps(attrs)) or None,
                        "c": created,
                        "u": updated,
                    },
                )
        return version, {
            "enabled": True,
            "created": created,
            "updated": updated,
            **attrs,
        }

    def get_latest(self, name: str) -> Optional[Tuple[str, dict]]:
        if self._engine is None:
            versions = self._mem.get(name) or {}
            if not versions:
                return None
            version = max(versions.items(), key=lambda kv: kv[1]["updated"])[0]
            return version, versions[version]
        else:
            with self._engine.begin() as conn:
                row = (
                    conn.execute(
                        text(
                            "SELECT version, value, tags, attributes, enabled, deleted, created, updated \n"
                            "FROM secrets WHERE name=:n AND deleted=0 ORDER BY created DESC LIMIT 1"
                        ),
                        {"n": name},
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    return None
                return row["version"], sqlrow_to_dict(row)

    def get_version(self, name: str, version: str) -> dict | None:
        if self._engine is None:
            return self._mem.get(name, {}).get(version)
        else:
            with self._engine.begin() as conn:
                row: Optional[RowMapping] = (
                    conn.execute(
                        text(
                            "SELECT version, value, tags, attributes, enabled, deleted, created, updated "
                            "FROM secrets WHERE name=:n AND version=:v"
                        ),
                        {"n": name, "v": version},
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return None
                return sqlrow_to_dict(row)

    def list_versions(self, name: str) -> List[Tuple[str, dict]]:
        if self._engine is None:
            return [
                (v, d)
                for v, d in (self._mem.get(name) or {}).items()
                if not d.get("deleted")
            ]
        else:
            with self._engine.begin() as conn:
                rows = (
                    conn.execute(
                        text(
                            "SELECT version, value, tags, attributes, enabled, deleted, created, updated \n"
                            "FROM secrets WHERE name=:n AND deleted=0 ORDER BY created DESC"
                        ),
                        {"n": name},
                    )
                    .mappings()
                    .all()
                )
                return [(r["version"], sqlrow_to_dict(r)) for r in rows]

    def soft_delete(self, name: str) -> Optional[dict]:
        now = self._now()
        if self._engine is None:
            versions = self._mem.get(name)
            if not versions:
                return None
            for v in versions.values():
                v["deleted"] = True
                v["updated"] = now
            self._deleted[name] = {
                "deletedDate": now,
                "scheduledPurgeDate": now + 86400 * 7,
            }
            return self._deleted[name]
        else:
            with self._engine.begin() as conn:
                res = conn.execute(
                    text("SELECT COUNT(1) c FROM secrets WHERE name=:n"), {"n": name}
                ).scalar()
                if not res:
                    return None
                conn.execute(
                    text("UPDATE secrets SET deleted=1, updated=:u WHERE name=:n"),
                    {"n": name, "u": now},
                )
                return {"deletedDate": now, "scheduledPurgeDate": now + 86400 * 7}

    def get_deleted(self, name: str) -> Optional[dict]:
        if self._engine is None:
            return self._deleted.get(name)
        else:
            with self._engine.begin() as conn:
                row = (
                    conn.execute(
                        text(
                            "SELECT MIN(updated) AS deletedDate FROM secrets WHERE name=:n AND deleted=1"
                        ),
                        {"n": name},
                    )
                    .mappings()
                    .first()
                )
                if not row or row["deletedDate"] is None:
                    return None
                return {
                    "deletedDate": int(row["deletedDate"]),
                    "scheduledPurgeDate": int(row["deletedDate"]) + 86400 * 7,
                }

    def list_names_latest(self) -> Iterable[Tuple[str, dict]]:
        """
        Yield (name, latest_data_dict) for each non-deleted secret name.
        latest_data_dict has the same shape as get_latest()[1].
        """
        if self._engine is None:
            for name, versions in self._mem.items():
                # skip names that are fully deleted
                if all(v.get("deleted") for v in versions.values()):
                    continue
                version = max(versions.items(), key=lambda kv: kv[1]["updated"])[0]
                yield name, versions[version]
        else:
            with self._engine.begin() as conn:
                rows = (
                    conn.execute(
                        text("SELECT DISTINCT name FROM secrets WHERE deleted=0")
                    )
                    .scalars()
                    .all()
                )
            for name in rows:
                latest = self.get_latest(name)
                if latest is None:
                    continue
                _, data = latest
                yield name, data

    def recover(self, name: str) -> bool:
        if self._engine is None:
            versions = self._mem.get(name)
            if not versions:
                return False
            for v in versions.values():
                v["deleted"] = False
            self._deleted.pop(name, None)
            return True
        else:
            with self._engine.begin() as conn:
                res = conn.execute(
                    text("SELECT COUNT(1) FROM secrets WHERE name=:n AND deleted=1"),
                    {"n": name},
                ).scalar()
                if not res:
                    return False
                conn.execute(
                    text("UPDATE secrets SET deleted=0 WHERE name=:n"), {"n": name}
                )
                return True


def json_dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def json_loads(s: Optional[str]):
    return s and json.loads(s)


def sqlrow_to_dict(row) -> dict:
    return {
        "value": row["value"],
        "tags": json_loads(row["tags"]) or {},
        "attributes": json_loads(row["attributes"]) or {},
        "enabled": bool(row["enabled"]),
        "deleted": bool(row["deleted"]),
        "created": int(row["created"]) if row["created"] is not None else None,
        "updated": int(row["updated"]) if row["updated"] is not None else None,
    }
