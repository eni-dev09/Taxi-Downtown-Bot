import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RulesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"TAXI_DB_PATH": str(Path(tmp.name) / "r.sqlite3")})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("bot", None)
        self.bot = importlib.import_module("bot")
        self.addCleanup(lambda: sys.modules.pop("bot", None))

    def test_fare_matches_zone_examples(self):
        cases = {
            "ville": 700, "hors_ville": 1000, "cayo": 1300,
        }
        for zone, rate in cases.items():
            for km, units in ((1, 1), (1.7, 1), (1.8, 2), (2, 2), (2.8, 3)):
                with self.subTest(zone=zone, km=km):
                    self.assertEqual(self.bot.compute_fare(km, zone)[0], rate * units)
        self.assertEqual(self.bot.compute_fare(0.3, "cayo")[0], 1300)  # minimum 1 km
        self.assertEqual(self.bot.compute_fare(2, "ville", 4, 2, 1, True)[0], 1400 + 200 + 100 + 25 + 50)

    def test_prime_ranking_requires_ten_hours_and_orders_by_money(self):
        stats = {
            1: {"hours": 11, "fare": 5000, "trips": 10},
            2: {"hours": 15, "fare": 9000, "trips": 8},
            3: {"hours": 9.9, "fare": 99999, "trips": 50},  # sous 10 h : exclu
            4: {"hours": 10, "fare": 5000, "trips": 12},   # égalité d'argent : plus de courses
            5: {"hours": 20, "fare": 100, "trips": 1},
        }
        eligible, others = self.bot.prime_ranking(stats)
        self.assertEqual([uid for uid, _ in eligible], [2, 4, 1, 5])
        self.assertEqual([uid for uid, _ in others], [3])
        self.assertEqual(self.bot.prime_for(stats, 2), 20_000)
        self.assertEqual(self.bot.prime_for(stats, 4), 15_000)
        self.assertEqual(self.bot.prime_for(stats, 1), 10_000)
        self.assertEqual(self.bot.prime_for(stats, 5), 0)
        self.assertEqual(self.bot.prime_for(stats, 3), 0)

    def _profile(self, role_name, data, warnings=0, prev=None):
        from datetime import datetime, timezone
        from types import SimpleNamespace as NS
        from unittest.mock import patch
        member = NS(id=1, display_name="Test", roles=[NS(name=role_name)])
        guild = NS(id=1)
        stats = {1: data}
        store = self.bot.STORE
        start = datetime(2026, 10, 26, tzinfo=timezone.utc)
        with (
            patch.object(self.bot, "weekly_totals", return_value=(start, start, stats)),
            patch.object(self.bot, "previous_week_stats", return_value={1: prev or data}),
            patch.object(store, "employee_status", return_value="active"),
            patch.object(store, "service_state", return_value=None),
            patch.object(store, "warnings_for_user", return_value=[
                {"id": i, "created_at": start.isoformat(), "reason": "x"} for i in range(warnings)
            ]),
        ):
            return self.bot.employee_profile(guild, member)

    def test_grade_detection_ignores_accents_and_case(self):
        from types import SimpleNamespace as NS
        grade = lambda n: self.bot.member_grade(NS(roles=[NS(name=n)]))
        self.assertEqual(grade("Chauffeur Confirmé"), "confirme")
        self.assertEqual(grade("chauffeur senior"), "senior")
        self.assertEqual(grade("Novice"), "novice")
        self.assertEqual(grade("Formateur & recruteur"), "chef")
        self.assertIsNone(grade("Directeur"))

    def test_novice_promotion_and_warning_rules(self):
        ok = {"hours": 10.0, "fare": 8000.0, "trips": 5.0}
        self.assertIn("Peut monter en grade", self._profile("Novice", ok))
        self.assertIn("Ne peut pas encore", self._profile("Novice", ok, warnings=1))
        low = {"hours": 3.0, "fare": 1000.0, "trips": 1.0}
        text = self._profile("Novice", low)
        self.assertIn("avertissement à prévoir", text)
        self.assertIn("il manque 6 500 $ à rapporter + 4 h 30 de service", text)
        self.assertIn("licenciement / démission", self._profile("Novice", low, warnings=1))

    def test_confirme_needs_two_regular_weeks(self):
        good = {"hours": 9.0, "fare": 9000.0, "trips": 5.0}
        bad = {"hours": 1.0, "fare": 0.0, "trips": 0.0}
        self.assertIn("Peut monter en grade", self._profile("Chauffeur Confirmé", good))
        self.assertIn("Ne peut pas encore", self._profile("Chauffeur Confirmé", good, prev=bad))


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"TAXI_DB_PATH": str(Path(tmp.name) / "p.sqlite3")})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("bot", None)
        self.bot = importlib.import_module("bot")
        self.addCleanup(lambda: sys.modules.pop("bot", None))

    async def test_persistent_views_have_unique_custom_ids(self):
        ids = []
        for view in (self.bot.PdsView(), self.bot.TicketPanelView(), self.bot.TicketCloseView()):
            self.assertIsNone(view.timeout)
            ids += [item.custom_id for item in view.children]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            {"td:pds:start", "td:pds:end", "td:pds:break_start", "td:pds:break_end"} - set(ids),
            set(),
        )
        self.assertEqual(len([i for i in ids if i.startswith("td:ticket:")]), 5)  # 4 types + fermeture

    def test_pds_embed_lists_active_employees_with_colored_dots(self):
        store = self.bot.STORE
        store.start_service(1, 10)
        store.start_service(1, 20)
        store.start_break(1, 20)
        store.start_service(2, 30)  # autre serveur : exclu
        text = self.bot.pds_embed(1).fields[0].value
        self.assertIn("🟢 <@10>", text)
        self.assertIn("🟡 <@20>", text)
        self.assertNotIn("<@30>", text)
        store.end_break(1, 20)
        store.end_service(1, 10)
        text = self.bot.pds_embed(1).fields[0].value
        self.assertNotIn("<@10>", text)
        self.assertIn("🟢 <@20>", text)
        store.end_service(1, 20)
        self.assertIn("Personne", self.bot.pds_embed(1).fields[0].value)

    def test_pds_panel_pointer_roundtrip(self):
        store = self.bot.STORE
        self.assertIsNone(store.get_pds_panel(1))
        store.set_pds_panel(1, 111, 222)
        self.assertEqual(store.get_pds_panel(1), (111, 222))
        store.clear_pds_panel(1)
        self.assertIsNone(store.get_pds_panel(1))

    def test_ticket_category_uses_configured_category_for_each_kind(self):
        from types import SimpleNamespace as NS
        from unittest.mock import MagicMock, patch

        cases = {
            "partenariat": 1508807957779841175,
            "recrutement": 1463497154575466622,
            "support": 1508808019536904233,
            "signalement": 1508808019536904233,
        }
        categories = {}
        for kind, category_id in cases.items():
            category = MagicMock(spec=self.bot.discord.CategoryChannel)
            categories[category_id] = category
            guild = NS(id=42, get_channel=lambda requested, values=categories: values.get(requested))
            with patch.object(
                self.bot.STORE, "get_settings",
                return_value={f"ticket_category_{kind}_id": category_id},
            ):
                self.assertIs(self.bot.ticket_category_for(guild, kind), category)

    def test_ticket_category_returns_none_for_missing_or_invalid_category(self):
        from types import SimpleNamespace as NS
        from unittest.mock import patch

        guild = NS(id=42, get_channel=lambda _requested: None)
        with patch.object(
            self.bot.STORE, "get_settings",
            return_value={"ticket_category_support_id": 123},
        ):
            self.assertIsNone(self.bot.ticket_category_for(guild, "support"))
        self.assertIsNone(self.bot.ticket_category_for(guild, "unknown"))

    def test_ticket_category_config_view_exposes_four_category_selectors(self):
        view = self.bot.TicketCategoryConfigView()
        self.assertEqual(len(view.children), 4)
        self.assertEqual(
            {item.placeholder for item in view.children},
            {
                "Catégorie Partenariat",
                "Catégorie Recrutement",
                "Catégorie Direction · Support",
                "Catégorie Direction · Signalement",
            },
        )

    def test_ticket_category_config_embed_shows_the_configured_ids(self):
        from types import SimpleNamespace as NS
        from unittest.mock import patch

        settings = {
            "ticket_category_partenariat_id": 1508807957779841175,
            "ticket_category_recrutement_id": 1463497154575466622,
            "ticket_category_support_id": 1508808019536904233,
            "ticket_category_signalement_id": 1508808019536904233,
        }
        with patch.object(self.bot.STORE, "get_settings", return_value=settings):
            embed = self.bot.ticket_category_config_embed(NS(id=42))
        rendered = " ".join(field.value for field in embed.fields)
        for category_id in settings.values():
            self.assertIn(str(category_id), rendered)

    def test_ticket_panel_describes_every_ticket_kind(self):
        embed = self.bot.ticket_embed()
        for label, _emoji, _style in self.bot.TICKET_KINDS.values():
            with self.subTest(ticket_kind=label):
                self.assertIn(label, embed.description)

    def test_ticket_lifecycle_storage(self):
        store = self.bot.STORE
        self.assertIsNone(store.open_ticket_for(1, 2))
        store.create_ticket(1, 2, 555, "support", "Question")
        self.assertEqual(store.open_ticket_for(1, 2)["channel_id"], 555)
        self.assertEqual(store.ticket_by_channel(555)["user_id"], 2)
        self.assertTrue(store.close_ticket(555))
        self.assertFalse(store.close_ticket(555))
        self.assertIsNone(store.open_ticket_for(1, 2))


if __name__ == "__main__":
    unittest.main()
