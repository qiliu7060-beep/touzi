"""财务聚合与JS逐字段对照，不联网不读取持仓。"""
import json
from pathlib import Path
import subprocess
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from touzi.financial_coverage import financial_coverage


class CoverageTests(unittest.TestCase):
    def test_python_node_parity(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(['node', 'tests/test_financial_coverage.mjs', '--json'], cwd=root, capture_output=True, text=True, encoding='utf-8', check=True)
        data = json.loads(run.stdout)
        self.assertEqual(financial_coverage(data['entries']), data['actual'])

    def test_absent_and_true_zero(self):
        self.assertEqual(financial_coverage({})['total'],0)
        row = {k:0 for k in ['revenue','operating_profit','net_profit','operating_cash_flow','capex','deducted_parent_net_profit']}
        summary = financial_coverage({'000001':{'industry':'制造业','financial_analysis':{'years':[row]*5,'quality':{'coverage':0}}}})
        self.assertEqual(summary['fields']['deducted_parent_missing'],0)
        self.assertEqual(summary['reasons']['amounts_missing'],0)


if __name__ == '__main__':
    unittest.main()
