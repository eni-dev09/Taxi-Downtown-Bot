import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict(
            os.environ,
            {"TAXI_DB_PATH": str(Path(self.temp_dir.name) / "health.sqlite3")},
        )
        self.env_patch.start()
        sys.modules.pop("bot", None)
        self.module = importlib.import_module("bot")
        self.bot = self.module.TaxiBot()

    async def asyncSetUp(self):
        app = web.Application()
        app.router.add_get("/health", self.bot.health_check)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.bot.close()
        await self.module.bot.close()
        sys.modules.pop("bot", None)
        self.env_patch.stop()

    async def test_health_reports_ready_discord_and_database(self):
        with patch.object(self.bot, "is_ready", return_value=True):
            response = await self.client.get("/health")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.json(),
            {
                "status": "ok",
                "discord": "ready",
                "database": "ok",
                "webhook": (
                    "enabled" if os.getenv("INGAME_WEBHOOK_SECRET") else "disabled"
                ),
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_health_reports_degraded_before_discord_is_ready(self):
        with patch.object(self.bot, "is_ready", return_value=False):
            response = await self.client.get("/health")

        self.assertEqual(response.status, 503)
        payload = await response.json()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["discord"], "not_ready")
        self.assertEqual(payload["database"], "ok")

    async def test_employee_profile_includes_private_employment_details_safely(self):
        started = datetime(2026, 10, 26, tzinfo=timezone.utc)
        stats = {
            22: {"hours": 12.0, "fare": 500.0, "trips": 4.0},
            33: {"hours": 10.0, "fare": 0.0, "trips": 0.0},
        }
        guild = type("Guild", (), {"id": 1, "get_role": lambda _self, _id: None})()
        member = type(
            "Member",
            (),
            {"id": 22, "display_name": "Taxi @everyone", "roles": []},
        )()
        store = self.module.STORE
        with (
            patch.object(
                self.module,
                "weekly_totals",
                return_value=(started, started, stats),
            ),
            patch.object(
                store,
                "get_settings",
                return_value={"driver_role_id": None},
            ),
            patch.object(store, "employee_status", return_value="active"),
            patch.object(store, "service_state", return_value={"pause": None}),
            patch.object(
                store,
                "vehicles_for_user",
                return_value=[{"plate": "TD-1", "model": "Berline"}],
            ),
            patch.object(
                store,
                "warnings_for_user",
                return_value=[
                    {
                        "id": 7,
                        "created_at": started.isoformat(),
                        "reason": "@everyone test",
                    }
                ],
            ),
            patch.object(
                store,
                "get_schedule",
                return_value={"schedule_text": "Lundi <@123456>"},
            ),
        ):
            profile = self.module.employee_profile(guild, member)

        self.assertIn("Profil —", profile)
        self.assertIn("Aucun avertissement à prévoir (avertissements actifs : 1)", profile)
        self.assertIn("Top 1 heures : **oui**", profile)
        self.assertIn("1re place — 20 000 $", profile)
        self.assertNotIn("@everyone", profile)
        self.assertNotIn("<@123456>", profile)


if __name__ == "__main__":
    unittest.main()
