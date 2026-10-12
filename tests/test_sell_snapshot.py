"""公网卖出快照只发布公共条件，个人持仓留在浏览器。"""

from pathlib import Path
import json
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from publish_site import site_js
from touzi.config import load_config


class SellSnapshotTests(unittest.TestCase):
    def test_all_scored_stocks_have_public_sell_snapshot(self):
        cfg = load_config()
        score = SimpleNamespace(
            code="000001", name="测试股票", total=60.0, pe_ttm=10.0,
            pb=1.0, dividend_yield=3.0, parts={}, price=10.0,
        )
        result = {
            "scores": None, "track": None, "stock_results": [score],
            "macro": SimpleNamespace(macro_score=80.0), "flags": {"as_of": "2026-10-12"},
        }
        script = site_js(result, cfg)
        payload = json.loads(script.split("window.__SITE__ = ", 1)[1].removesuffix(";\n"))
        snapshot = payload["entries"]["000001"]["sell_snapshot"]
        self.assertTrue(any("总分" in label for label in snapshot["exit"]))
        self.assertTrue(any("自身历史分位" in label for label in snapshot["missing"]))
        self.assertFalse(any("相对成本" in label for label in snapshot["exit"]))
        self.assertNotIn("买入价", script)
        self.assertNotIn("股数", script)


if __name__ == "__main__":
    unittest.main()
