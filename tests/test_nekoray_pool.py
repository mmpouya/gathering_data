from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crawl_hadith import nekoray_pool


class NekoRayPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "source"
        (self.source / "profiles").mkdir(parents=True)
        (self.source / "groups").mkdir()
        (self.source / "routes").mkdir()
        (self.source / "profiles" / "0.json").write_text(
            json.dumps(
                {
                    "type": "vless",
                    "bean": {"name": "test profile", "addr": "x", "port": 1},
                }
            ),
            encoding="utf-8",
        )
        (self.source / "groups" / "nekoray.json").write_text(
            json.dumps(
                {
                    "inbound_socks_port": 2080,
                    "inbound_http_port": 2081,
                    "remember_enable": False,
                    "remember_id": -1919,
                    "spmode2": [],
                }
            ),
            encoding="utf-8",
        )
        (self.source / "groups" / "coreType").write_text("0", encoding="utf-8")
        (self.source / "routes" / "Default").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_load_profiles(self) -> None:
        profiles = nekoray_pool.load_profiles(self.source)
        self.assertEqual(list(profiles), [0])
        self.assertIn("test profile", nekoray_pool.profile_label(0, profiles[0]))

    def test_prepare_instance_sets_unique_ports(self) -> None:
        dest = self.root / "instance" / "config"
        settings = nekoray_pool.prepare_instance(
            self.source, dest, 0, 12100, 12101
        )
        self.assertEqual(settings["inbound_socks_port"], 12100)
        self.assertEqual(settings["inbound_http_port"], 12101)
        self.assertTrue(settings["remember_enable"])
        self.assertEqual(settings["remember_id"], 0)
        self.assertEqual(settings["spmode2"], [])
        self.assertTrue(settings["start_minimal"])
        self.assertTrue((dest / "profiles" / "0.json").exists())
        saved = json.loads(
            (dest / "groups" / "nekoray.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["inbound_http_port"], 12101)

    def test_prepare_instance_missing_profile(self) -> None:
        with self.assertRaises(ValueError):
            nekoray_pool.prepare_instance(
                self.source, self.root / "x" / "config", 99, 12100, 12101
            )


if __name__ == "__main__":
    unittest.main()
