"""SQLite persistence for Taxi Downtown's Discord bot."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(__file__).parent / "data" / "taxi_downtown.sqlite3"
DEFAULT_TICKET_CATEGORIES = {
    "recrutement": 1463497154575466622,
    "support": 1508808019536904233,
    "signalement": 1508808019536904233,
    "partenariat": 1508807957779841175,
}


def resolve_db_path() -> Path:
    """TAXI_DB_PATH is read at call time so tests can never touch the real database."""
    return Path(os.getenv("TAXI_DB_PATH") or DEFAULT_DB_PATH)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class TaxiStore:
    """Small local SQLite store; no CV text or AI prompts are persisted."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else resolve_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    direction_role_id INTEGER,
                    driver_role_id INTEGER,
                    log_channel_id INTEGER,
                    webhook_channel_id INTEGER,
                    ticket_category_recrutement_id INTEGER DEFAULT 1463497154575466622,
                    ticket_category_support_id INTEGER DEFAULT 1508808019536904233,
                    ticket_category_signalement_id INTEGER DEFAULT 1508808019536904233,
                    ticket_category_partenariat_id INTEGER DEFAULT 1508807957779841175,
                    weekly_quota_hours REAL NOT NULL DEFAULT 10
                );
                CREATE TABLE IF NOT EXISTS employees (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    joined_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS service_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_guild_start
                    ON service_sessions (guild_id, started_at);
                CREATE TABLE IF NOT EXISTS service_breaks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES service_sessions(id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    reminded_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_breaks_session
                    ON service_breaks (session_id, started_at);
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    fare REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trips_guild_date
                    ON trips (guild_id, created_at);
                CREATE TABLE IF NOT EXISTS vehicles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    plate TEXT NOT NULL,
                    model TEXT NOT NULL,
                    assigned_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE (guild_id, plate)
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    removed_at TEXT,
                    removed_by INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_warnings_user
                    ON warnings (guild_id, user_id, active);
                CREATE TABLE IF NOT EXISTS schedules (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    schedule_text TEXT NOT NULL,
                    updated_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tickets_user
                    ON tickets (guild_id, user_id, closed_at);
                CREATE TABLE IF NOT EXISTS runtime_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            break_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(service_breaks)")
            }
            if "reminded_at" not in break_columns:
                conn.execute("ALTER TABLE service_breaks ADD COLUMN reminded_at TEXT")
            settings_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(guild_settings)")
            }
            for kind, category_id in DEFAULT_TICKET_CATEGORIES.items():
                column = f"ticket_category_{kind}_id"
                if column not in settings_columns:
                    conn.execute(
                        f"ALTER TABLE guild_settings ADD COLUMN {column} INTEGER DEFAULT {category_id}"
                    )
            self._migrate_extra_plates(conn)

    @staticmethod
    def _migrate_extra_plates(conn: sqlite3.Connection) -> None:
        """Merge the legacy extra_plates list into vehicles, once."""
        done = conn.execute(
            "SELECT 1 FROM runtime_state WHERE state_key = 'extra_plates_migrated'"
        ).fetchone()
        if done:
            return
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'extra_plates'"
        ).fetchone()
        if legacy:
            conn.execute(
                """
                INSERT OR IGNORE INTO vehicles
                    (guild_id, plate, model, assigned_user_id, created_at)
                SELECT guild_id, plate,
                       CASE WHEN TRIM(note) = '' THEN 'Taxi' ELSE note END,
                       assigned_user_id, created_at
                FROM extra_plates
                """
            )
        conn.execute(
            "INSERT OR REPLACE INTO runtime_state (state_key, state_value) "
            "VALUES ('extra_plates_migrated', '1')"
        )

    def backup_to(self, destination: Path | str) -> Path:
        """Create a consistent SQLite snapshot, including committed WAL data."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as source:
                backup = sqlite3.connect(target, timeout=10)
                try:
                    source.backup(backup)
                    result = backup.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        raise sqlite3.DatabaseError("SQLite backup integrity check failed")
                    backup.commit()
                finally:
                    backup.close()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    def get_settings(self, guild_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
            ).fetchone()
        if row is None:
            return {
                "guild_id": guild_id,
                "direction_role_id": None,
                "driver_role_id": None,
                "log_channel_id": None,
                "webhook_channel_id": None,
                **{f"ticket_category_{kind}_id": category_id for kind, category_id in DEFAULT_TICKET_CATEGORIES.items()},
                "weekly_quota_hours": 10.0,
            }
        return dict(row)

    def set_settings(self, guild_id: int, **values: Any) -> dict[str, Any]:
        allowed = {
            "direction_role_id",
            "driver_role_id",
            "log_channel_id",
            "webhook_channel_id",
            "ticket_category_recrutement_id",
            "ticket_category_support_id",
            "ticket_category_signalement_id",
            "ticket_category_partenariat_id",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return self.get_settings(guild_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
                (guild_id,),
            )
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?",
                (*updates.values(), guild_id),
            )
        return self.get_settings(guild_id)

    def set_employee_status(self, guild_id: int, user_id: int, status: str) -> None:
        now = to_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO employees (guild_id, user_id, status, joined_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, status, now, now),
            )

    def set_runtime_heartbeat(self, at: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (state_key, state_value)
                VALUES ('last_heartbeat', ?)
                ON CONFLICT(state_key) DO UPDATE SET state_value = excluded.state_value
                """,
                (to_iso(at),),
            )

    def recover_interrupted_services(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Close open shifts at the last known process heartbeat on restart."""
        recovery_time = (now or utc_now()).astimezone(timezone.utc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            heartbeat = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key = 'last_heartbeat'"
            ).fetchone()
            try:
                cutoff = (
                    min(recovery_time, parse_iso(heartbeat["state_value"]))
                    if heartbeat
                    else recovery_time
                )
            except (TypeError, ValueError):
                cutoff = recovery_time
            sessions = conn.execute(
                "SELECT * FROM service_sessions WHERE ended_at IS NULL ORDER BY id"
            ).fetchall()
            recovered: list[dict[str, Any]] = []
            for session in sessions:
                session_start = parse_iso(session["started_at"])
                ended_at = max(session_start, cutoff)
                pauses = conn.execute(
                    """
                    SELECT * FROM service_breaks
                    WHERE session_id = ? AND ended_at IS NULL
                    """,
                    (session["id"],),
                ).fetchall()
                pause_seconds = 0
                for pause in pauses:
                    pause_start = parse_iso(pause["started_at"])
                    pause_end = max(pause_start, ended_at)
                    pause_seconds += max(
                        0, int((pause_end - pause_start).total_seconds())
                    )
                    conn.execute(
                        "UPDATE service_breaks SET ended_at = ? WHERE id = ?",
                        (to_iso(pause_end), pause["id"]),
                    )
                    ended_at = max(ended_at, pause_end)
                conn.execute(
                    "UPDATE service_sessions SET ended_at = ? WHERE id = ?",
                    (to_iso(ended_at), session["id"]),
                )
                recovered.append(
                    {
                        "guild_id": int(session["guild_id"]),
                        "user_id": int(session["user_id"]),
                        "started_at": session["started_at"],
                        "ended_at": to_iso(ended_at),
                        "open_break_seconds": pause_seconds,
                    }
                )
            conn.execute(
                """
                INSERT INTO runtime_state (state_key, state_value)
                VALUES ('last_heartbeat', ?)
                ON CONFLICT(state_key) DO UPDATE SET state_value = excluded.state_value
                """,
                (to_iso(recovery_time),),
            )
        return recovered

    def overdue_breaks(self, cutoff: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT b.id AS break_id, b.started_at, s.guild_id, s.user_id
                FROM service_breaks b
                JOIN service_sessions s ON s.id = b.session_id
                WHERE b.ended_at IS NULL AND b.reminded_at IS NULL
                  AND b.started_at <= ? AND s.ended_at IS NULL
                ORDER BY b.started_at
                """,
                (to_iso(cutoff),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_break_reminded(self, break_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE service_breaks SET reminded_at = ?
                WHERE id = ? AND ended_at IS NULL AND reminded_at IS NULL
                """,
                (to_iso(), break_id),
            )
            return cur.rowcount == 1

    def get_active_employee_ids(self, guild_id: int) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM employees WHERE guild_id = ? AND status = 'active'",
                (guild_id,),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def employee_status(self, guild_id: int, user_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM employees WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return str(row["status"]) if row else None

    def start_service(self, guild_id: int, user_id: int) -> str:
        now = to_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            employee = conn.execute(
                "SELECT status FROM employees WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if employee and employee["status"] != "active":
                raise ValueError("Ton statut n'est pas actif. Contacte la direction.")
            existing = conn.execute(
                """
                SELECT id FROM service_sessions
                WHERE guild_id = ? AND user_id = ? AND ended_at IS NULL
                """,
                (guild_id, user_id),
            ).fetchone()
            if existing:
                raise ValueError("Un service est déjà en cours.")
            conn.execute(
                "INSERT INTO service_sessions (guild_id, user_id, started_at) VALUES (?, ?, ?)",
                (guild_id, user_id, now),
            )
            conn.execute(
                """
                INSERT INTO employees (guild_id, user_id, status, joined_at, updated_at)
                VALUES (?, ?, 'active', ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (guild_id, user_id, now, now),
            )
        return now

    def _active_session(self, conn: sqlite3.Connection, guild_id: int, user_id: int):
        return conn.execute(
            """
            SELECT * FROM service_sessions
            WHERE guild_id = ? AND user_id = ? AND ended_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        ).fetchone()

    def service_state(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            session = self._active_session(conn, guild_id, user_id)
            if not session:
                return None
            pause = conn.execute(
                "SELECT * FROM service_breaks WHERE session_id = ? AND ended_at IS NULL",
                (session["id"],),
            ).fetchone()
        return {"session": dict(session), "pause": dict(pause) if pause else None}

    def end_service(self, guild_id: int, user_id: int) -> dict[str, Any]:
        now = to_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._active_session(conn, guild_id, user_id)
            if not session:
                raise ValueError("Aucun service n'est en cours.")
            open_break = conn.execute(
                "SELECT * FROM service_breaks WHERE session_id = ? AND ended_at IS NULL",
                (session["id"],),
            ).fetchone()
            break_seconds = 0
            if open_break:
                break_seconds = max(
                    0, int((parse_iso(now) - parse_iso(open_break["started_at"])).total_seconds())
                )
                conn.execute(
                    "UPDATE service_breaks SET ended_at = ? WHERE id = ?",
                    (now, open_break["id"]),
                )
            conn.execute(
                "UPDATE service_sessions SET ended_at = ? WHERE id = ?",
                (now, session["id"]),
            )
        return {"started_at": session["started_at"], "ended_at": now, "open_break_seconds": break_seconds}

    def start_break(self, guild_id: int, user_id: int) -> str:
        now = to_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._active_session(conn, guild_id, user_id)
            if not session:
                raise ValueError("Prends d'abord ton service.")
            open_break = conn.execute(
                "SELECT id FROM service_breaks WHERE session_id = ? AND ended_at IS NULL",
                (session["id"],),
            ).fetchone()
            if open_break:
                raise ValueError("Une pause est déjà en cours.")
            conn.execute(
                "INSERT INTO service_breaks (session_id, started_at) VALUES (?, ?)",
                (session["id"], now),
            )
        return now

    def end_break(self, guild_id: int, user_id: int) -> int:
        now = to_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = self._active_session(conn, guild_id, user_id)
            if not session:
                raise ValueError("Aucun service n'est en cours.")
            pause = conn.execute(
                "SELECT * FROM service_breaks WHERE session_id = ? AND ended_at IS NULL",
                (session["id"],),
            ).fetchone()
            if not pause:
                raise ValueError("Aucune pause n'est en cours.")
            seconds = max(0, int((parse_iso(now) - parse_iso(pause["started_at"])).total_seconds()))
            conn.execute(
                "UPDATE service_breaks SET ended_at = ? WHERE id = ?",
                (now, pause["id"]),
            )
        return seconds

    def record_trip(self, guild_id: int, user_id: int, fare: float) -> None:
        if fare <= 0:
            raise ValueError("Le montant d'une course doit être positif.")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO trips (guild_id, user_id, fare, created_at) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, fare, to_iso()),
            )

    def record_payout(self, guild_id: int, user_id: int, amount: float, reason: str) -> None:
        if amount <= 0:
            raise ValueError("Le montant de la prime doit être positif.")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO payouts (guild_id, user_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, amount, reason, to_iso()),
            )

    def weekly_stats(
        self, guild_id: int, start: datetime, end: datetime
    ) -> dict[int, dict[str, float]]:
        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        start_text, end_text = to_iso(start_utc), to_iso(end_utc)
        now = utc_now()
        stats: dict[int, dict[str, float]] = {}
        with self._connect() as conn:
            sessions = conn.execute(
                """
                SELECT * FROM service_sessions
                WHERE guild_id = ? AND started_at < ?
                  AND COALESCE(ended_at, ?) > ?
                """,
                (guild_id, end_text, to_iso(now), start_text),
            ).fetchall()
            for session in sessions:
                user_id = int(session["user_id"])
                stats.setdefault(user_id, {"hours": 0.0, "fare": 0.0, "trips": 0.0})
                session_start = max(parse_iso(session["started_at"]), start_utc)
                session_end = min(parse_iso(session["ended_at"]) if session["ended_at"] else now, end_utc)
                seconds = max(0.0, (session_end - session_start).total_seconds())
                breaks = conn.execute(
                    "SELECT started_at, ended_at FROM service_breaks WHERE session_id = ?",
                    (session["id"],),
                ).fetchall()
                for pause in breaks:
                    pause_start = max(parse_iso(pause["started_at"]), session_start)
                    pause_end = min(
                        parse_iso(pause["ended_at"]) if pause["ended_at"] else now,
                        session_end,
                    )
                    seconds -= max(0.0, (pause_end - pause_start).total_seconds())
                stats[user_id]["hours"] += max(0.0, seconds) / 3600.0

            trips = conn.execute(
                """
                SELECT user_id, COUNT(*) AS count, COALESCE(SUM(fare), 0) AS fare
                FROM trips WHERE guild_id = ? AND created_at >= ? AND created_at < ?
                GROUP BY user_id
                """,
                (guild_id, start_text, end_text),
            ).fetchall()
            for row in trips:
                user_id = int(row["user_id"])
                stats.setdefault(user_id, {"hours": 0.0, "fare": 0.0, "trips": 0.0})
                stats[user_id]["fare"] = float(row["fare"])
                stats[user_id]["trips"] = float(row["count"])

        return stats

    def add_vehicle(
        self, guild_id: int, plate: str, model: str, assigned_user_id: int | None
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM vehicles WHERE guild_id = ? AND plate = ?",
                (guild_id, plate),
            ).fetchone():
                raise ValueError("Cette plaque est déjà enregistrée.")
            conn.execute(
                """
                INSERT INTO vehicles
                    (guild_id, plate, model, assigned_user_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, plate, model, assigned_user_id, to_iso()),
            )

    def delete_vehicle(self, guild_id: int, plate: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vehicles WHERE guild_id = ? AND plate = ?",
                (guild_id, plate),
            ).fetchone()
            if row:
                conn.execute("DELETE FROM vehicles WHERE id = ?", (row["id"],))
        return dict(row) if row else None

    def get_vehicle(self, guild_id: int, plate: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vehicles WHERE guild_id = ? AND plate = ?",
                (guild_id, plate),
            ).fetchone()
        return dict(row) if row else None

    def assign_vehicle(
        self, guild_id: int, plate: str, user_id: int | None
    ) -> dict[str, Any] | None:
        """Assign (or unassign with None) a vehicle; returns the updated row."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE vehicles SET assigned_user_id = ? WHERE guild_id = ? AND plate = ?",
                (user_id, guild_id, plate),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM vehicles WHERE guild_id = ? AND plate = ?",
                (guild_id, plate),
            ).fetchone()
        return dict(row)

    def vehicles_for_user(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vehicles WHERE guild_id = ? AND assigned_user_id = ? ORDER BY plate",
                (guild_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_vehicles(self, guild_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vehicles WHERE guild_id = ? ORDER BY plate", (guild_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_warning(
        self, guild_id: int, user_id: int, reason: str, moderator_id: int
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO warnings
                    (guild_id, user_id, reason, moderator_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, reason, moderator_id, to_iso()),
            )
            return int(cur.lastrowid)

    def warnings_for_user(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM warnings
                WHERE guild_id = ? AND user_id = ? AND active = 1
                ORDER BY id DESC
                """,
                (guild_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_warning(
        self, guild_id: int, user_id: int, moderator_id: int, warning_id: int | None = None
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            if warning_id is None:
                row = conn.execute(
                    """
                    SELECT * FROM warnings
                    WHERE guild_id = ? AND user_id = ? AND active = 1
                    ORDER BY id DESC LIMIT 1
                    """,
                    (guild_id, user_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM warnings
                    WHERE guild_id = ? AND user_id = ? AND id = ? AND active = 1
                    """,
                    (guild_id, user_id, warning_id),
                ).fetchone()
            if row:
                conn.execute(
                    "UPDATE warnings SET active = 0, removed_at = ?, removed_by = ? WHERE id = ?",
                    (to_iso(), moderator_id, row["id"]),
                )
        return dict(row) if row else None

    def set_schedule(
        self, guild_id: int, user_id: int, schedule_text: str, updated_by: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules (guild_id, user_id, schedule_text, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    schedule_text = excluded.schedule_text,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, schedule_text, updated_by, to_iso()),
            )

    def get_schedule(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    # --- tickets ---------------------------------------------------------
    def create_ticket(
        self, guild_id: int, user_id: int, channel_id: int, kind: str, subject: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tickets (channel_id, guild_id, user_id, kind, subject, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (channel_id, guild_id, user_id, kind, subject, to_iso()),
            )

    def open_ticket_for(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tickets
                WHERE guild_id = ? AND user_id = ? AND closed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (guild_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def ticket_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else None

    def close_ticket(self, channel_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tickets SET closed_at = ? WHERE channel_id = ? AND closed_at IS NULL",
                (to_iso(), channel_id),
            )
            return cur.rowcount == 1

    # --- panel PDS-FDS ---------------------------------------------------
    def active_services(self, guild_id: int) -> list[dict[str, Any]]:
        """Employees currently on duty, with an open pause if any (oldest first)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.user_id, s.started_at, b.started_at AS break_started_at
                FROM service_sessions s
                LEFT JOIN service_breaks b ON b.session_id = s.id AND b.ended_at IS NULL
                WHERE s.guild_id = ? AND s.ended_at IS NULL
                ORDER BY s.started_at
                """,
                (guild_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_pds_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runtime_state (state_key, state_value) VALUES (?, ?)",
                (f"pds_panel:{guild_id}", f"{channel_id}:{message_id}"),
            )

    def get_pds_panel(self, guild_id: int) -> tuple[int, int] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key = ?",
                (f"pds_panel:{guild_id}",),
            ).fetchone()
        if not row:
            return None
        try:
            channel_id, message_id = row["state_value"].split(":")
            return int(channel_id), int(message_id)
        except ValueError:
            return None

    def clear_pds_panel(self, guild_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM runtime_state WHERE state_key = ?", (f"pds_panel:{guild_id}",))