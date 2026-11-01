"""缺项仅改变证据展示：分数/贡献/交易条件保持原样。"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_financial_analysis import analysis, quality, NOW
from financial_fixtures import statement_tables
from touzi.data.financial import analyze_financials
from touzi.financial_coverage import quality_evidence


class QualityEvidenceTests(unittest.TestCase):
    def test_missing_weak_items_can_raise_score_without_full_verification(self):
        # 虚拟样本：已知两项各50分时82.5；隐藏两项后100。绝非实际股票补全。
        a = analysis(liabilities=140., capex=62.5)
        full = quality(a)
        self.assertEqual(full.score, 82.5)
        missing = copy.deepcopy(a)
        for key in ('debt_equity', 'capex_to_profit'):
            missing['metrics'][key]['status'] = 'missing'
        partial = quality(missing)
        self.assertEqual(partial.score, 100.)
        entry = {'financial_analysis': partial.detail['financial_analysis'], 'total': 88, 'action': '观望'}
        before = copy.deepcopy(entry)
        evidence = quality_evidence(entry)
        self.assertEqual(evidence['coverage'], .65)
        self.assertEqual(evidence['pending'], ['debt', 'capex'])
        self.assertAlmostEqual(evidence['effective_weights']['roe'], .25 / .65)
        self.assertEqual(entry, before)
        self.assertAlmostEqual(sum(v for v in entry['financial_analysis']['quality']['contributions'].values() if v is not None), partial.score)

    def test_exemption_invalid_zero_and_unknown_are_distinct(self):
        q = quality(analyze_financials(statement_tables(), '银行', as_of=NOW), '银行')
        e = {'financial_analysis': q.detail['financial_analysis']}
        ev = quality_evidence(e)
        self.assertEqual(ev['exempt'], ['debt', 'capex'])
        self.assertEqual(ev['pending'], [])
        zero = quality(analysis(equity=0))
        ev = quality_evidence({'financial_analysis': zero.detail['financial_analysis']})
        self.assertEqual(ev['statuses']['debt'], 'scored')
        self.assertGreater(ev['effective_weights']['debt'], 0)
        self.assertIsNone(quality_evidence({})['coverage'])
        self.assertEqual(len(quality_evidence({})['pending']), 5)

    def test_python_browser_parity_and_bundled_helper(self):
        entries = [{}, {'financial_analysis': quality(analysis()).detail['financial_analysis']},
                   {'financial_analysis': quality(analysis(equity=0)).detail['financial_analysis']}]
        missing = copy.deepcopy(entries[1])
        missing['financial_analysis']['quality']['sub_scores']['debt'] = None
        missing['financial_analysis']['metrics']['debt_equity']['status'] = 'stale'
        entries.append(missing)
        code = "import {qualityEvidence} from './scripts/pick-financial.mjs'; console.log(JSON.stringify(JSON.parse(process.argv[1]).map(qualityEvidence)));"
        out = subprocess.check_output(['node', '--input-type=module', '-e', code, json.dumps(entries)], text=True)
        self.assertEqual(json.loads(out), [quality_evidence(e) for e in entries])
        root = Path(__file__).resolve().parents[1]
        helper = (root/'scripts/pick-financial.mjs').read_text().replace('export ', '').strip()
        self.assertIn(helper, (root/'scripts/static/app.js').read_text())


if __name__ == '__main__':
    unittest.main()
