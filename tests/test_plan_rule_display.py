"""卖出规则展示必须与实际执行引擎同源。"""

import unittest
from html import escape
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from touzi.config import load_config
from touzi.plans import evaluate_sell
from touzi.report import _plan_rules_md, render_plan_rules


class PlanRuleDisplayTests(unittest.TestCase):
    def test_html_and_markdown_match_executable_sell_rules(self):
        plan = load_config().get("plan", {})
        checks = evaluate_sell(None, {"plan": plan})
        html = render_plan_rules(plan)
        markdown = _plan_rules_md(plan)

        self.assertEqual(html.count('<tr><td><span class="pill '), len(checks))
        for check in checks:
            self.assertIn(f'>{check.level}</span></td><td class="l">{escape(check.label)}', html)
            self.assertIn(f'| {check.level} | {check.label} |', markdown)

        levels = {check.key: check.level for check in checks}
        self.assertEqual(levels["sell_macro_score"], "减仓")
        self.assertEqual(levels["sell_take_profit"], "减仓")
        self.assertEqual(levels["sell_cashflow"], "预警")
        self.assertEqual(levels["sell_growth_negative"], "预警")
        self.assertIn("网页不能获知你是否已经成交", html)


if __name__ == "__main__":
    unittest.main()
