import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import features


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"TAXI_DB_PATH": str(Path(tmp.name) / "c.sqlite3")})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("bot", None)
        self.bot = importlib.import_module("bot")
        self.addCleanup(lambda: sys.modules.pop("bot", None))
        self.store = self.bot.STORE
        self.guild = NS(
            id=1, name="Sensity RP", icon=None,
            get_role=lambda rid: NS(id=rid), get_channel=lambda cid: NS(id=cid),
        )

    # --- registre ---------------------------------------------------------
    def test_registry_is_consistent(self):
        keys = [f.key for m in features.MODULES.values() for f in m.fields]
        self.assertEqual(len(keys), len(set(keys)), "clés de champ en double")
        self.assertEqual(len(features.CATEGORIES), 8)
        for module in features.MODULES.values():
            texts = [f for f in module.fields if f.kind in ("int", "float", "text")]
            self.assertLessEqual(len(texts), 5, f"{module.key}: 5 champs max par formulaire")

    def test_parse_value(self):
        field = features.FIELDS["prime_1"]
        self.assertEqual(features.parse_value(field, "25 000 $"), 25000)
        self.assertEqual(features.parse_value(features.FIELDS["min_hours"], "7,5"), 7.5)
        with self.assertRaises(ValueError):
            features.parse_value(field, "abc")
        with self.assertRaises(ValueError):
            features.parse_value(field, "1.5")
        with self.assertRaises(ValueError):
            features.parse_value(features.FIELDS["max_break_minutes"], "1")  # min 5
        with self.assertRaises(ValueError):
            features.parse_value(features.FIELDS["announce_image_open"], "pas une url")
        self.assertIsNone(features.parse_value(features.FIELDS["announce_image_open"], ""))

    # --- valeurs et états -----------------------------------------------
    def test_defaults_match_taxi_rules(self):
        cfg = self.bot.gcfg(1)
        self.assertEqual(cfg.primes(), (20000, 15000, 10000))
        self.assertEqual(cfg.zone_rates(), {"ville": 700, "hors_ville": 1000, "cayo": 1300})
        self.assertEqual(cfg.get("min_hours"), 10.0)
        self.assertEqual(self.bot.rules_for(1).max_break_seconds, 1800)
        self.assertEqual(cfg.promotion("novice"), (7500, 7.5))

    def test_toggle_values_and_reset(self):
        features.set_enabled(self.store, 1, "fleet", False)
        self.assertFalse(self.bot.gcfg(1).enabled("fleet"))
        features.set_field(self.store, 1, features.FIELDS["prime_1"], 30000)
        features.set_field(self.store, 1, features.FIELDS["driver_role_id"], 555)
        features.set_field(self.store, 1, features.FIELDS["log_channel_service"], 777)
        cfg = self.bot.gcfg(1)
        self.assertEqual(cfg.primes()[0], 30000)
        self.assertEqual(cfg.get("driver_role_id"), 555)
        self.assertEqual(cfg.get("log_channel_service"), 777)
        features.reset_module(self.store, 1, features.MODULES["primes"])
        features.reset_module(self.store, 1, features.MODULES["fleet"])
        cfg = self.bot.gcfg(1)
        self.assertEqual(cfg.primes()[0], 20000)
        self.assertTrue(cfg.enabled("fleet"))

    def test_ticket_category_reset_keeps_default(self):
        field = features.FIELDS["ticket_category_support_id"]
        features.set_field(self.store, 1, field, 42)
        self.assertEqual(self.bot.gcfg(1).get(field.key), 42)
        features.set_field(self.store, 1, field, None)
        self.assertEqual(self.bot.gcfg(1).get(field.key), features.DEFAULT_TICKET_CATEGORIES["support"])

    def test_module_states_and_counts(self):
        modules = features.MODULES
        cfg = self.bot.gcfg(1)
        self.assertEqual(cfg.state(modules["direction"]), "todo")  # rôle requis manquant
        self.assertEqual(cfg.state(modules["log_general"]), "todo")
        self.assertEqual(cfg.state(modules["grades"]), "off")
        self.assertEqual(cfg.state(modules["tarifs"]), "on")
        features.set_field(self.store, 1, features.FIELDS["direction_role_id"], 9)
        features.set_enabled(self.store, 1, "tarifs", False)
        cfg = self.bot.gcfg(1)
        self.assertEqual(cfg.state(modules["direction"]), "on")
        self.assertEqual(cfg.state(modules["tarifs"]), "off")
        ready, total = cfg.category_counts(features.CATEGORY_BY_KEY["money"])
        self.assertEqual((ready, total), (2, 3))

    # --- règles appliquées -----------------------------------------------
    def test_custom_primes_and_quota_change_ranking(self):
        features.set_field(self.store, 1, features.FIELDS["min_hours"], 4)
        features.set_field(self.store, 1, features.FIELDS["prime_1"], 50000)
        rules = self.bot.rules_for(1)
        stats = {1: {"hours": 5.0, "fare": 100.0, "trips": 1.0}}
        eligible, _ = self.bot.prime_ranking(stats, rules)
        self.assertEqual([u for u, _ in eligible], [1])
        self.assertEqual(self.bot.prime_for(stats, 1, rules), 50000)
        self.assertEqual(self.bot.prime_for(stats, 1), 0)  # règles par défaut : 10 h requises

    def test_custom_rates_in_fare(self):
        features.set_field(self.store, 1, features.FIELDS["rate_cayo"], 2000)
        features.set_field(self.store, 1, features.FIELDS["fare_wait"], 100)
        cfg = self.bot.gcfg(1)
        total, _ = self.bot.compute_fare(
            1.8, "cayo", 2, rates=cfg.zone_rates(), supplements=cfg.supplements()
        )
        self.assertEqual(total, 2 * 2000 + 2 * 100)

    def test_member_grade_prefers_configured_roles(self):
        features.set_field(self.store, 1, features.FIELDS["grade_role_senior"], 99)
        member = NS(roles=[NS(id=99, name="Boss"), NS(id=5, name="Novice")])
        self.assertEqual(self.bot.member_grade(member, self.bot.gcfg(1)), "senior")
        self.assertEqual(self.bot.member_grade(member), "novice")  # repli par nom

    # --- journaux ---------------------------------------------------------
    async def test_log_routing_and_toggles(self):
        sent = {}

        class Chan:
            def __init__(self, cid):
                self.cid = cid

            async def send(self, **kwargs):
                sent.setdefault(self.cid, []).append(kwargs["embed"].title)

        guild = NS(id=1, get_channel=lambda cid: Chan(cid))
        features.set_field(self.store, 1, features.FIELDS["log_channel_id"], 100)
        features.set_field(self.store, 1, features.FIELDS["log_channel_service"], 200)
        await self.bot.log_event(guild, "🕒 Journal de service", "x")
        await self.bot.log_event(guild, "🎫 Ticket ouvert", "x")
        await self.bot.log_event(guild, "⚠️ Avertissement RH", "x")
        self.assertEqual(sent[200], ["🕒 Journal de service"])
        self.assertEqual(sent[100], ["🎫 Ticket ouvert", "⚠️ Avertissement RH"])
        features.set_enabled(self.store, 1, "log_tickets", False)
        self.assertFalse(await self.bot.log_event(guild, "🎫 Ticket fermé", "x"))
        features.set_enabled(self.store, 1, "log_general", False)
        self.assertFalse(await self.bot.log_event(guild, "⚠️ Avertissement RH", "x"))

    # --- interface ---------------------------------------------------------
    async def test_every_screen_fits_discord_limits(self):
        import config_ui

        def check(view):
            self.assertLessEqual(view.total_children_count, 40)
            self.assertLessEqual(view.content_length(), 4000)
            self.assertTrue(view.to_components())

        check(config_ui.home_view(self.guild))
        for category in features.CATEGORIES:
            check(config_ui.category_view(self.guild, category))
            for module in category.modules:
                check(config_ui.module_view(self.guild, module))

    async def test_pickers_preselect_existing_role_and_channel(self):
        import config_ui

        features.set_field(self.store, 1, features.FIELDS["driver_role_id"], 123)
        features.set_field(self.store, 1, features.FIELDS["log_channel_id"], 456)
        for key, expected in (("drivers", 123), ("log_general", 456)):
            view = config_ui.module_view(self.guild, features.MODULES[key])
            selects = [i for i in view.walk_children() if hasattr(i, "default_values")]
            self.assertEqual([d.id for d in selects[0].default_values], [expected])
        missing = NS(id=1, name="x", icon=None, get_role=lambda r: None, get_channel=lambda c: None)
        view = config_ui.module_view(missing, features.MODULES["drivers"])
        selects = [i for i in view.walk_children() if hasattr(i, "default_values")]
        self.assertEqual(list(selects[0].default_values), [])

    async def test_home_lists_counts_per_category(self):
        import config_ui

        view = config_ui.home_view(self.guild)
        texts = [item.content for item in view.walk_children() if hasattr(item, "content")]
        joined = "\n".join(texts)
        self.assertIn("Accès & Hiérarchie", joined)
        self.assertIn("/6`", joined)
        self.assertEqual(sum(1 for t in texts if "·" in t and "/" in t), 8)

    async def test_publish_requires_enabled_module(self):
        features.set_enabled(self.store, 1, "tickets", False)
        replies = []

        class Resp:
            async def send_message(self, text, **kw):
                replies.append(text)

        interaction = NS(guild=NS(id=1), response=Resp(), channel=None)
        await self.bot._publish_tickets(interaction)
        self.assertIn("Active d'abord", replies[0])


if __name__ == "__main__":
    unittest.main()
