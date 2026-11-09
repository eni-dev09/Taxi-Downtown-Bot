import tempfile
import unittest
import sqlite3
from datetime import timedelta
from pathlib import Path

from storage import TaxiStore, to_iso, utc_now


class TaxiStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "taxi.sqlite3"
        self.store = TaxiStore(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_service_records_survive_reopening_store(self):
        started = self.store.start_service(10, 20)
        self.assertIsNotNone(self.store.service_state(10, 20))
        self.assertIsNotNone(started)

        reopened = TaxiStore(self.path)
        self.assertIsNotNone(reopened.service_state(10, 20))
        reopened.end_service(10, 20)
        self.assertIsNone(reopened.service_state(10, 20))

    def test_weekly_hours_subtract_recorded_breaks(self):
        self.store.start_service(1, 2)
        now = utc_now().replace(microsecond=0)
        started = now - timedelta(hours=1)
        break_started = started + timedelta(minutes=15)
        break_ended = started + timedelta(minutes=45)
        with self.store._connect() as conn:
            session_id = conn.execute(
                "SELECT id FROM service_sessions WHERE guild_id = 1 AND user_id = 2"
            ).fetchone()["id"]
            conn.execute(
                "UPDATE service_sessions SET started_at = ?, ended_at = ? WHERE id = ?",
                (to_iso(started), to_iso(now), session_id),
            )
            conn.execute(
                """
                INSERT INTO service_breaks (session_id, started_at, ended_at)
                VALUES (?, ?, ?)
                """,
                (session_id, to_iso(break_started), to_iso(break_ended)),
            )

        stats = self.store.weekly_stats(
            1, now - timedelta(days=1), now + timedelta(days=1)
        )
        self.assertAlmostEqual(stats[2]["hours"], 0.5, delta=0.01)

    def test_vehicle_plates_are_unique_and_assignable(self):
        self.store.add_vehicle(1, "TD-100", "Sedan", 9)
        with self.assertRaises(ValueError):
            self.store.add_vehicle(1, "TD-100", "Autre", None)
        self.assertEqual(self.store.get_vehicle(1, "TD-100")["model"], "Sedan")
        self.assertEqual(self.store.assign_vehicle(1, "TD-100", 5)["assigned_user_id"], 5)
        self.assertEqual(len(self.store.vehicles_for_user(1, 5)), 1)
        self.assertIsNone(self.store.assign_vehicle(1, "TD-100", None)["assigned_user_id"])
        self.assertIsNone(self.store.assign_vehicle(1, "NOPE", 5))

    def test_legacy_extra_plates_are_merged_once(self):
        path = Path(self.temp_dir.name) / "legacy.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE extra_plates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                plate TEXT NOT NULL, assigned_user_id INTEGER,
                note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            INSERT INTO extra_plates (guild_id, plate, assigned_user_id, note, created_at)
            VALUES (1, 'AA1111BB', 42, 'Eon', '2026-01-01T00:00:00+00:00'),
                   (1, 'CC2222DD', NULL, '', '2026-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()
        store = TaxiStore(path)
        self.assertEqual(store.get_vehicle(1, "AA1111BB")["model"], "Eon")
        self.assertEqual(store.get_vehicle(1, "AA1111BB")["assigned_user_id"], 42)
        self.assertEqual(store.get_vehicle(1, "CC2222DD")["model"], "Taxi")
        store.delete_vehicle(1, "AA1111BB")
        self.assertIsNone(TaxiStore(path).get_vehicle(1, "AA1111BB"))  # pas de ré-import

    def test_warning_removal_is_audited_and_hidden_from_active_list(self):
        warning_id = self.store.add_warning(3, 4, "Retard", 5)
        self.assertEqual(len(self.store.warnings_for_user(3, 4)), 1)
        removed = self.store.remove_warning(3, 4, 5, warning_id)
        self.assertEqual(removed["id"], warning_id)
        self.assertEqual(self.store.warnings_for_user(3, 4), [])

    def test_weekly_trip_totals(self):
        now = utc_now()
        self.store.record_trip(7, 8, 100)
        stats = self.store.weekly_stats(7, now - timedelta(days=1), now + timedelta(days=1))
        self.assertEqual(stats[8]["fare"], 100)
        self.assertEqual(stats[8]["trips"], 1)

    def test_weekly_totals_are_isolated_by_driver_and_guild(self):
        now = utc_now()
        start, end = now - timedelta(days=1), now + timedelta(days=1)
        self.store.record_trip(7, 8, 100)
        self.store.record_trip(7, 9, 300)
        self.store.record_trip(70, 8, 900)

        stats = self.store.weekly_stats(7, start, end)

        self.assertEqual(stats[8]["fare"], 100)
        self.assertEqual(stats[8]["trips"], 1)
        self.assertEqual(stats[9]["fare"], 300)
        self.assertEqual(stats[9]["trips"], 1)

    def test_restart_closes_open_service_at_last_heartbeat(self):
        now = utc_now().replace(microsecond=0)
        self.store.start_service(11, 12)
        self.store.start_break(11, 12)
        started = now - timedelta(hours=2)
        break_started = now - timedelta(minutes=40)
        heartbeat = now - timedelta(minutes=10)
        with self.store._connect() as conn:
            session_id = conn.execute(
                "SELECT id FROM service_sessions WHERE guild_id = 11 AND user_id = 12"
            ).fetchone()["id"]
            conn.execute(
                "UPDATE service_sessions SET started_at = ? WHERE id = ?",
                (to_iso(started), session_id),
            )
            conn.execute(
                "UPDATE service_breaks SET started_at = ? WHERE session_id = ?",
                (to_iso(break_started), session_id),
            )
        self.store.set_runtime_heartbeat(heartbeat)

        recovered = self.store.recover_interrupted_services(now)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["ended_at"], to_iso(heartbeat))
        self.assertEqual(recovered[0]["open_break_seconds"], 30 * 60)
        self.assertIsNone(self.store.service_state(11, 12))

    def test_overdue_break_reminder_is_sent_only_once(self):
        now = utc_now().replace(microsecond=0)
        self.store.start_service(21, 22)
        self.store.start_break(21, 22)
        started = now - timedelta(minutes=40)
        break_started = now - timedelta(minutes=31)
        with self.store._connect() as conn:
            session_id = conn.execute(
                "SELECT id FROM service_sessions WHERE guild_id = 21 AND user_id = 22"
            ).fetchone()["id"]
            conn.execute(
                "UPDATE service_sessions SET started_at = ? WHERE id = ?",
                (to_iso(started), session_id),
            )
            conn.execute(
                "UPDATE service_breaks SET started_at = ? WHERE session_id = ?",
                (to_iso(break_started), session_id),
            )

        overdue = self.store.overdue_breaks(now - timedelta(minutes=30))
        self.assertEqual(len(overdue), 1)
        self.assertTrue(self.store.mark_break_reminded(overdue[0]["break_id"]))
        self.assertFalse(self.store.mark_break_reminded(overdue[0]["break_id"]))
        self.assertEqual(self.store.overdue_breaks(now - timedelta(minutes=30)), [])

    def test_ticket_categories_have_server_defaults_and_are_configurable(self):
        settings = self.store.get_settings(123)
        self.assertEqual(settings["ticket_category_recrutement_id"], 1463497154575466622)
        self.assertEqual(settings["ticket_category_support_id"], 1508808019536904233)
        self.assertEqual(settings["ticket_category_signalement_id"], 1508808019536904233)
        self.assertEqual(settings["ticket_category_partenariat_id"], 1508807957779841175)

        updated = self.store.set_settings(123, ticket_category_support_id=987654321)
        self.assertEqual(updated["ticket_category_support_id"], 987654321)
        self.assertEqual(updated["ticket_category_recrutement_id"], 1463497154575466622)

    def test_existing_settings_table_is_migrated_without_losing_roles(self):
        legacy_path = Path(self.temp_dir.name) / "legacy-settings.sqlite3"
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, "
            "direction_role_id INTEGER, driver_role_id INTEGER, log_channel_id INTEGER, "
            "webhook_channel_id INTEGER, weekly_quota_hours REAL NOT NULL DEFAULT 10)"
        )
        conn.execute(
            "INSERT INTO guild_settings (guild_id, direction_role_id) VALUES (77, 1234)"
        )
        conn.commit()
        conn.close()

        migrated = TaxiStore(legacy_path).get_settings(77)
        self.assertEqual(migrated["direction_role_id"], 1234)
        self.assertEqual(migrated["ticket_category_recrutement_id"], 1463497154575466622)
        self.assertEqual(migrated["ticket_category_partenariat_id"], 1508807957779841175)

    def test_online_backup_contains_committed_data_and_is_valid(self):
        self.store.record_trip(91, 92, 1234)
        destination = Path(self.temp_dir.name) / "backups" / "snapshot.sqlite3"

        result = self.store.backup_to(destination)

        self.assertEqual(result, destination)
        conn = sqlite3.connect(destination)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                conn.execute(
                    "SELECT fare FROM trips WHERE guild_id = 91 AND user_id = 92"
                ).fetchone()[0],
                1234,
            )
        finally:
            conn.close()

    def test_database_connections_are_closed_after_use(self):
        with self.store._connect() as conn:
            connection = conn

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
