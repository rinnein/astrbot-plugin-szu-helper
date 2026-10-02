from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

import aiosqlite

from .models import Binding, Catalog, Location, Report


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_id TEXT NOT NULL, origin TEXT NOT NULL, sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL, is_group INTEGER NOT NULL, platform_name TEXT NOT NULL,
                dorm_key TEXT NOT NULL, location TEXT NOT NULL,
                UNIQUE(platform_id, origin, sender_id)
            );
            CREATE INDEX IF NOT EXISTS bindings_dorm ON bindings(dorm_key);
            CREATE TABLE IF NOT EXISTS dorm_state (
                dorm_key TEXT PRIMARY KEY, low INTEGER NOT NULL DEFAULT 0,
                episode INTEGER NOT NULL DEFAULT 0, observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                binding_id INTEGER NOT NULL REFERENCES bindings(id) ON DELETE CASCADE,
                episode INTEGER NOT NULL, sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(binding_id, episode)
            );
            CREATE TABLE IF NOT EXISTS catalogs (
                source TEXT PRIMARY KEY, fetched_at REAL NOT NULL, value TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)
        await self.db.commit()

    async def close(self):
        async with self.lock:
            if self.db:
                await self.db.close()
                self.db = None

    async def _all(self, sql: str, params=()):
        assert self.db is not None
        async with self.db.execute(sql, params) as cursor:
            return await cursor.fetchall()

    @staticmethod
    def _binding(row) -> Binding:
        return Binding(
            row["id"],
            row["platform_id"],
            row["origin"],
            row["sender_id"],
            row["sender_name"],
            bool(row["is_group"]),
            row["platform_name"],
            Location.from_dict(json.loads(row["location"])),
        )

    async def get_binding(self, platform: str, origin: str, sender: str) -> Binding | None:
        async with self.lock:
            rows = await self._all(
                "SELECT * FROM bindings WHERE platform_id=? AND origin=? AND sender_id=?",
                (platform, origin, sender),
            )
            return self._binding(rows[0]) if rows else None

    async def bind(
        self,
        platform: str,
        origin: str,
        sender: str,
        name: str,
        is_group: bool,
        platform_name: str,
        location: Location,
    ):
        async with self.lock:
            assert self.db is not None
            try:
                rows = await self._all(
                    "SELECT id,dorm_key FROM bindings WHERE platform_id=? AND origin=? AND sender_id=?",
                    (platform, origin, sender),
                )
                if rows and rows[0]["dorm_key"] != location.key:
                    # A fresh identity prevents an in-flight scan targeting a replacement binding.
                    await self.db.execute("DELETE FROM bindings WHERE id=?", (rows[0]["id"],))
                await self.db.execute(
                    """
                    INSERT INTO bindings(platform_id,origin,sender_id,sender_name,is_group,platform_name,dorm_key,location)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(platform_id,origin,sender_id)
                    DO UPDATE SET sender_name=excluded.sender_name,is_group=excluded.is_group,
                      platform_name=excluded.platform_name,location=excluded.location
                """,
                    (
                        platform,
                        origin,
                        sender,
                        name,
                        int(is_group),
                        platform_name,
                        location.key,
                        json.dumps(asdict(location), ensure_ascii=False),
                    ),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def unbind(self, platform: str, origin: str, sender: str):
        async with self.lock:
            assert self.db is not None
            await self.db.execute(
                "DELETE FROM bindings WHERE platform_id=? AND origin=? AND sender_id=?",
                (platform, origin, sender),
            )
            await self.db.commit()

    async def bindings(self) -> list[Binding]:
        async with self.lock:
            return [self._binding(r) for r in await self._all("SELECT * FROM bindings ORDER BY id")]

    async def catalog(self, source: str) -> tuple[float, Catalog] | None:
        async with self.lock:
            rows = await self._all("SELECT * FROM catalogs WHERE source=?", (source,))
            if rows:
                return rows[0]["fetched_at"], Catalog.from_dict(json.loads(rows[0]["value"]))
            return None

    async def save_catalog(self, source: str, catalog: Catalog, fetched_at: float):
        async with self.lock:
            assert self.db is not None
            await self.db.execute(
                "INSERT OR REPLACE INTO catalogs VALUES(?,?,?)",
                (source, fetched_at, json.dumps(catalog.to_dict(), ensure_ascii=False)),
            )
            await self.db.commit()

    async def observe(self, report: Report, threshold: float) -> int | None:
        if report.expired or report.remaining is None or report.observed_at is None:
            return None
        async with self.lock:
            assert self.db is not None
            key, observed = report.location.key, report.observed_at.isoformat()
            rows = await self._all("SELECT * FROM dorm_state WHERE dorm_key=?", (key,))
            old = rows[0] if rows else None
            # Do not let an older provider/cache response reopen or reset an episode.
            if old and observed < old["observed_at"]:
                return None
            low = report.remaining < threshold
            episode = old["episode"] if old else 0
            if low and (not old or not old["low"]):
                episode += 1
            await self.db.execute(
                "INSERT OR REPLACE INTO dorm_state VALUES(?,?,?,?)",
                (key, int(low), episode, observed),
            )
            await self.db.commit()
            return episode if low else None

    async def deliver(
        self,
        dorm_key: str,
        episode: int | None,
        origin: str,
        ids: list[int],
        send: Callable[[list[Binding]], Awaitable[bool]],
    ) -> bool | None:
        """Send to current bindings; episode=None is a manual, non-recording send.

        None means all targets were skipped, False means the adapter failed.
        """
        # Serialize the final binding check with bind/unbind until send finishes.
        # The caller bounds send time; no database transaction spans network I/O.
        async with self.lock:
            assert self.db is not None
            if episode is not None:
                state = await self._all(
                    "SELECT low,episode FROM dorm_state WHERE dorm_key=?", (dorm_key,)
                )
                if not state or not state[0]["low"] or state[0]["episode"] != episode:
                    return None
            query = "SELECT b.* FROM bindings b WHERE dorm_key=? AND origin=?"
            params = [dorm_key, origin]
            if episode is not None:
                query += " AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.binding_id=b.id AND d.episode=?)"
                params.append(episode)
            rows = await self._all(query + " ORDER BY b.id", params)
            allowed = set(ids)
            pending = [self._binding(r) for r in rows if r["id"] in allowed]
            if not pending:
                return None
            if not await send(pending):
                return False
            if episode is not None:
                await self.db.executemany(
                    "INSERT OR IGNORE INTO deliveries(binding_id,episode) VALUES(?,?)",
                    [(b.id, episode) for b in pending],
                )
                await self.db.commit()
            return True
