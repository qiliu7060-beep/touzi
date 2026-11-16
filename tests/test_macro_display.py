"""宏观模板标记必须隔离当前宏观、历史报告和私人持仓区域。"""
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "vendor"), str(ROOT), str(ROOT / "scripts")]

from touzi.report import render_macro, render_overview
from scripts import webui
from scripts.publish_site import overview_body

KEYS = ("pmi", "ppi", "fed", "cpi", "gdp")


def macro_fixture():
    data = {
        "as_of": "2026-10-12", "macro_score": 48.54, "position": .547,
        "regime": "中性（均衡配置）", "unavailable": [], "staleness": {},
        "weights": {key: .2 for key in KEYS},
        "rules": {key: {"score": 50 + i, "state": "源状态<待核验>",
                        "explain": '<script>alert("fixture")</script>'}
                  for i, key in enumerate(KEYS)},
    }
    return SimpleNamespace(as_dict=lambda: deepcopy(data))


class MarkerParser(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.records = []
        self.stack = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.records.append((tag, values, list(self.stack)))
        if tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.stack.append((tag, values))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def marked(self, key):
        return [item for item in self.records if key in item[1]]


class MacroTemplateTests(unittest.TestCase):
    def test_overview_marks_only_three_macro_cards(self):
        text = render_overview(macro_fixture())
        parser = MarkerParser(text)
        cards = parser.marked("data-macro-card")
        self.assertEqual([attrs["data-macro-card"] for _, attrs, _ in cards],
                         ["score", "position", "computed_at"])
        self.assertEqual([attrs["data-macro-value"] for _, attrs, _ in parser.marked("data-macro-value")],
                         ["score", "position", "computed_at"])
        for _, attrs, ancestors in parser.marked("data-macro-value"):
            self.assertTrue(any(parent.get("data-macro-card") == attrs["data-macro-value"]
                                for _, parent in ancestors))
        self.assertIn('data-macro-value="score">48.5<', text)
        self.assertIn('data-macro-value="position">55%<', text)
        self.assertIn('data-macro-value="computed_at">2026-10-12<', text)
        self.assertIn("宏观快照日期", text)
        self.assertIn("我的持仓浮动盈亏", text)
        self.assertEqual(text.count("data-macro-card="), 3)

    def test_detail_current_container_owns_all_rules_and_escapes_source_text(self):
        text = render_macro(macro_fixture())
        parser = MarkerParser(text)
        self.assertEqual(len(parser.marked("data-macro-current")), 1)
        self.assertEqual({attrs["data-macro-rule"] for _, attrs, _ in parser.marked("data-macro-rule")}, set(KEYS))
        for key in ("data-macro-rule", "data-macro-rule-value", "data-macro-rule-weight",
                    "data-macro-rule-state", "data-macro-rule-explain"):
            self.assertEqual(len(parser.marked(key)), 5)
            self.assertTrue(all(any("data-macro-current" in ancestor for _, ancestor in ancestors)
                                for _, _, ancestors in parser.marked(key)))
        self.assertFalse(any(tag == "script" for tag, _, _ in parser.records))
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("这不是逐条核实的实际披露时间", text)
        self.assertIn("历史报告保留原日期", text)
        self.assertEqual(parser.stack, [])

    def test_missing_macro_position_does_not_render_a_zero_allocation(self):
        text = render_overview()
        self.assertIn('data-macro-value="position">—<', text)
        self.assertNotIn('data-macro-value="position">0%<', text)

    def test_static_and_macro_page_keep_history_sections_separate(self):
        macro = macro_fixture()
        overview = overview_body({"macro": macro})
        self.assertIn('data-macro-card="score"', overview)
        self.assertIn("同一独立数据源", overview)
        sections = [
            {"key": "macro", "title": "宏观", "html": render_macro(macro)},
            {"key": "macro_bt", "title": "历史回测", "html": '<p id="old-backtest">历史原结果</p>'},
            {"key": "policy", "title": "政策", "html": '<p id="old-policy">政策原日期</p>'},
        ]
        with patch.object(webui, "sections_of", return_value=sections):
            current = webui.view_macro({})
            report = webui.view_report({})
        self.assertIn("不随当前宏观刷新", current)
        self.assertIn('id="sec-macro"', current)
        self.assertNotIn('id="sec-macro" data-macro-history', current)
        self.assertIn('id="sec-macro" data-macro-history', report)
        self.assertIn("完整历史快照和原始日期", report)
        parser = MarkerParser(current)
        for _, attrs, ancestors in parser.records:
            if attrs.get("id") in {"old-backtest", "old-policy"}:
                self.assertFalse(any("data-macro-current" in parent for _, parent in ancestors))
        self.assertIn("政策原日期", report)
        self.assertIn("历史原结果", report)


if __name__ == "__main__":
    unittest.main()
