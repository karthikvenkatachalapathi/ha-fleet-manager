import json
import tempfile
import unittest
from pathlib import Path

from ha_update_dashboard.scanner import classify, load_instances


class ScannerTests(unittest.TestCase):
    def test_classifies_core_manual(self):
        self.assertEqual(classify({"entity_id": "update.home_assistant_core_update", "title": "Home Assistant Core"}), "manual_core_haos")

    def test_classifies_firmware_review(self):
        self.assertEqual(classify({"entity_id": "update.router_firmware", "title": "Router firmware"}), "needs_review")

    def test_classifies_addon_candidate_safe(self):
        self.assertEqual(classify({"entity_id": "update.cloudflared_update", "title": "Cloudflared"}), "candidate_safe")

    def test_load_instances(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"instances":[{"alias":"x","label":"X","url":"https://ha.example","token_key":"HASS_TOKEN_X"}]}))
            items = load_instances(p)
            self.assertEqual(items[0].alias, "x")
            self.assertEqual(items[0].url, "https://ha.example")


if __name__ == "__main__":
    unittest.main()
