"""选股模板展示两项财务标准，但不修改买入动作或原有表单协议。"""
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.webui import SHELL_CSS, view_pick


class PickParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headers = []
        self.cells = []
        self.rows = []
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'th' and 'data-pick-financial-header' in values:
            self.headers.append(values['data-pick-financial-header'])
        if tag == 'td' and 'data-pick-financial' in values:
            self.cells.append(values['data-pick-financial'])
        if tag == 'tr' and 'data-pick' in values:
            self.rows.append(values['data-pick'])
        if tag == 'input':
            self.inputs.append(values)


class PickFinancialTests(unittest.TestCase):
    def test_selected_row_has_readable_theme(self):
        self.assertIn('tr.picked{background:#e5f6ec;color:#1f2937}', SHELL_CSS)
        self.assertIn('tr.picked .muted{color:#526275}', SHELL_CSS)

    def test_all_groups_have_columns_and_original_form_protocol(self):
        rows = []
        for code, action in [('000001', '建仓'), ('000002', '接近建仓'), ('000003', '观望')]:
            rows.append({'代码': code, '名称': '虚拟企业', '建议动作': action, '总分': 80,
                         '现价': 10, 'PE_TTM': 10, 'PB': 1, '股息率%': 3,
                         '未通过的门槛': '(全部通过)', '预警': '原有预警'})
        html = view_pick({'track': pd.DataFrame(rows)})
        parsed = PickParser()
        parsed.feed(html)
        self.assertEqual(parsed.headers, ['debt_equity', 'capex_to_profit'] * 3)
        self.assertEqual(parsed.cells, ['debt_equity', 'capex_to_profit'] * 3)
        self.assertEqual(parsed.rows, ['000001', '000002', '000003'])
        self.assertIn('优良&lt;0.8', html)
        self.assertIn('达标&lt;25%', html)
        self.assertIn('原买入门槛通过', html)
        self.assertNotIn('(全部通过)', html)
        self.assertIn('并非新增买入硬门槛', html)
        self.assertEqual(len([i for i in parsed.inputs if i.get('class') == 'ip']), 3)
        self.assertEqual(len([i for i in parsed.inputs if i.get('class') == 'is']), 3)
        self.assertEqual(len([i for i in parsed.inputs if i.get('type') == 'checkbox']), 3)
        self.assertEqual(html.count('class="scroll"'), 3)
        self.assertIn('原有预警', html)

    def test_empty_groups_do_not_invent_metric_values(self):
        html = view_pick({})
        self.assertIn('本次没有这一类标的', html)
        self.assertNotIn('data-pick-financial="debt_equity"', html)


if __name__ == '__main__':
    unittest.main()
