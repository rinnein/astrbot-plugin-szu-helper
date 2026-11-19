from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import (
    SHANGHAI,
    Binding,
    Catalog,
    Check,
    Location,
    Reading,
    Recharge,
    Report,
    timestamp,
)

RETAIN_DAYS = 62


@dataclass
class RoomState:
    """Per-dorm cached history: full readings, recharges, and fetch marks."""

    readings: list[Reading] = field(default_factory=list)
    recharges: list[Recharge] = field(default_factory=list)
    usage_checks: dict[str, Check] = field(default_factory=dict)
    recharge_checks: dict[str, Check] = field(default_factory=dict)
    balance: float | None = None
    balance_observed_at: datetime | None = None
    usage_updated_at: float | None = None
    recharge_updated_at: float | None = None
    retry_after: float = 0.0
    error: str | None = None
    recharge_error: str | None = None
    recharge_retry_after: float = 0.0


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
            CREATE TABLE IF NOT EXISTS rooms (
                dorm_key TEXT NOT NULL, source TEXT NOT NULL,
                balance REAL, balance_observed_at TEXT,
                usage_updated_at REAL, recharge_updated_at REAL,
                retry_after REAL NOT NULL DEFAULT 0, error TEXT,
                recharge_error TEXT, recharge_retry_after REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(dorm_key, source)
            );
            CREATE TABLE IF NOT EXISTS readings (
                dorm_key TEXT NOT NULL, source TEXT NOT NULL, date TEXT NOT NULL,
                observed_at TEXT, remaining REAL, cumulative REAL, daily REAL,
                PRIMARY KEY(dorm_key, source, date)
            );
            CREATE TABLE IF NOT EXISTS recharges (
                dorm_key TEXT NOT NULL, source TEXT NOT NULL, time TEXT NOT NULL,
                kwh REAL, yuan REAL, source_kind TEXT NOT NULL,
                PRIMARY KEY(dorm_key, source, time)
            );
            CREATE TABLE IF NOT EXISTS checks (
                dorm_key TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
                date TEXT NOT NULL, fetched_at REAL NOT NULL, expires_at REAL NOT NULL,
                PRIMARY KEY(dorm_key, source, kind, date)
            );
            DROP TABLE IF EXISTS query_cache;
            PRAGMA user_version=2;
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

    async def other_locations(self, platform: str, origin: str, sender: str) -> list[Location]:
        """Distinct dorms owned by this sender in other sessions of the same bot."""
        async with self.lock:
            rows = await self._all(
                "SELECT * FROM bindings WHERE platform_id=? AND sender_id=? AND origin<>? ORDER BY id DESC",
                (platform, sender, origin),
            )
            locations = {}
            for row in rows:
                location = self._binding(row).location
                locations.setdefault(location.key, location)
            return list(locations.values())

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

    async def load_room(self, source: str, dorm_key: str) -> RoomState:
        async with self.lock:
            room = RoomState()
            rows = await self._all(
                "SELECT * FROM rooms WHERE dorm_key=? AND source=?", (dorm_key, source)
            )
            if rows:
                row = rows[0]
                room.balance = row["balance"]
                room.balance_observed_at = (
                    timestamp(row["balance_observed_at"]) if row["balance_observed_at"] else None
                )
                room.usage_updated_at = row["usage_updated_at"]
                room.recharge_updated_at = row["recharge_updated_at"]
                room.retry_after = row["retry_after"] or 0.0
                room.error = row["error"]
                room.recharge_error = row["recharge_error"]
                room.recharge_retry_after = row["recharge_retry_after"] or 0.0
            for row in await self._all(
                "SELECT * FROM readings WHERE dorm_key=? AND source=? ORDER BY date",
                (dorm_key, source),
            ):
                if not row["observed_at"]:
                    continue
                at = timestamp(row["observed_at"])
                if at is None:
                    continue
                room.readings.append(Reading(at, row["remaining"], row["cumulative"], row["daily"]))
            for row in await self._all(
                "SELECT * FROM recharges WHERE dorm_key=? AND source=?", (dorm_key, source)
            ):
                room.recharges.append(
                    Recharge(row["time"], row["kwh"], row["yuan"], None, row["source_kind"])
                )
            for row in await self._all(
                "SELECT * FROM checks WHERE dorm_key=? AND source=?", (dorm_key, source)
            ):
                check = Check(row["fetched_at"], row["expires_at"])
                target = room.usage_checks if row["kind"] == "usage" else room.recharge_checks
                target[row["date"]] = check
            return room

    async def save_room(self, source: str, dorm_key: str, room: RoomState, now: float):
        """Replace the dorm's whole row set in one transaction, pruning history
        older than the retention window. `now` shares the service clock so the
        cutoff follows the same timeline as the stored dates."""
        cutoff = (
            datetime.fromtimestamp(now, SHANGHAI).date() - timedelta(days=RETAIN_DAYS - 1)
        ).isoformat()
        async with self.lock:
            assert self.db is not None
            try:
                await self.db.execute(
                    "DELETE FROM readings WHERE dorm_key=? AND source=?", (dorm_key, source)
                )
                await self.db.execute(
                    "DELETE FROM recharges WHERE dorm_key=? AND source=?", (dorm_key, source)
                )
                await self.db.execute(
                    "DELETE FROM checks WHERE dorm_key=? AND source=?", (dorm_key, source)
                )
                await self.db.execute(
                    "INSERT OR REPLACE INTO rooms VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        dorm_key,
                        source,
                        room.balance,
                        room.balance_observed_at.isoformat() if room.balance_observed_at else None,
                        room.usage_updated_at,
                        room.recharge_updated_at,
                        room.retry_after,
                        room.error,
                        room.recharge_error,
                        room.recharge_retry_after,
                    ),
                )
                await self.db.executemany(
                    "INSERT OR REPLACE INTO readings VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            dorm_key,
                            source,
                            reading.at.date().isoformat(),
                            reading.at.isoformat(),
                            reading.remaining,
                            reading.cumulative,
                            reading.daily,
                        )
                        for reading in room.readings
                    ],
                )
                await self.db.executemany(
                    "INSERT OR REPLACE INTO recharges VALUES(?,?,?,?,?,?)",
                    [
                        (dorm_key, source, r.time, r.kwh, r.yuan, r.source_kind)
                        for r in room.recharges
                    ],
                )
                await self.db.executemany(
                    "INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?,?)",
                    [
                        (dorm_key, source, kind, day, check.fetched_at, check.expires_at)
                        for kind, marks in (
                            ("usage", room.usage_checks),
                            ("recharge", room.recharge_checks),
                        )
                        for day, check in marks.items()
                    ],
                )
                # Global retention: rows of dorms that stopped being queried age out too.
                await self.db.execute("DELETE FROM readings WHERE date<?", (cutoff,))
                await self.db.execute("DELETE FROM checks WHERE date<?", (cutoff,))
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def observe(self, report: Report, threshold: float) -> int | None:
        if report.effective_remaining is None or report.observed_at is None:
            return None
        async with self.lock:
            assert self.db is not None
            key, observed = report.location.key, report.observed_at.isoformat()
            rows = await self._all("SELECT * FROM dorm_state WHERE dorm_key=?", (key,))
            old = rows[0] if rows else None
            # Do not let an older provider/cache response reopen or reset an episode.
            if old and observed < old["observed_at"]:
                return None
            low = report.effective_remaining < threshold
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
