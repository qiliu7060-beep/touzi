"""五年历史阈值、导出接线及 Python/浏览器分位判断一致性（无网络）。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from publish_site import export, valuation_thresholds
from touzi.config import load_config
from touzi.plans import evaluate_sell
from touzi.signals.valuation import percentile_of
from touzi.util import Cache


class ValuationThresholdTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.plan = self.cfg['plan']

    def history(self, values):
        return pd.DataFrame({'date': pd.date_range(end='2026-10-21', periods=len(values)),
                             'value': values})

    def thresholds(self, values, **kwargs):
        cache = Mock()
        cache.get.return_value = self.history(values)
        result = valuation_thresholds(cache, ['000001', '000001'], self.plan, 1, '2026-10-21', **kwargs)
        self.assertEqual(cache.get.call_count, 2)  # 去重、PE/PB 各一次，只读缓存
        self.assertEqual(cache.method_calls[0].args[0], 'val_baidu_000001_市盈率(TTM)_近五年')
        return result

    def test_exact_boundaries_and_python_js_parity(self):
        cases = []
        # 包含重复值、边界精度及会被 score_valuation 的四位舍入提升的分位。
        for values in [np.arange(1., 32.), [1.123456789] * 21,
                       [1.] * 8 + [2.] * 13, np.arange(1., 20002.)]:
            for q in [0., .80004, .8, .8357, 1.]:
                self.plan['sell_pe_percentile'] = q
                self.plan['sell_pb_percentile'] = q
                thr = self.thresholds(values)
                boundary = thr['000001']['pe']['boundary']
                for value in [0., -1., boundary, np.nextafter(boundary, np.inf),
                              max(.0001, boundary - 1e-8), max(values) + 1]:
                    pctl = round(percentile_of(float(value), np.asarray(values)), 4)
                    detail = {f'{m}_percentile': pctl for m in ('pe', 'pb')}
                    detail.update({f'{m}_percentile_source': '个股5年历史' for m in ('pe', 'pb')})
                    score = SimpleNamespace(parts={'valuation': SimpleNamespace(detail=detail)})
                    hits = {c.key: c.hit for c in evaluate_sell(score, self.cfg)}
                    cases.append({'thr': thr, 'q': q, 'value': float(value),
                                  'expected': hits['sell_pe_percentile']})
        program = r'''
const assert = require('node:assert/strict');
const fs = require('fs');
Date.now = () => Date.parse('2026-10-21T02:00:00Z');
global.window = {__SITE__: {plan:{},thr:{}}};
const site = window.__SITE__;
const {sellDecision} = require('./scripts/static/app.js');
for (const c of JSON.parse(fs.readFileSync(0, 'utf8'))) {
  site.thr = c.thr;
  site.plan.sell_pe_percentile = c.q; site.plan.sell_pb_percentile = c.q;
  const d = sellDecision({'代码':'000001'}, {}, {code:'000001',price:10,
    pe_ttm:c.value,pb:c.value,quote_ts:'2026-10-21 10:00:00'});
  assert.equal(d.action === '清仓', c.expected, JSON.stringify(c));
}
'''
        subprocess.run(['node', '-e', program], cwd=ROOT, input=json.dumps(cases),
                       text=True, check=True, capture_output=True)

    def test_missing_stale_cache_and_sample_gates(self):
        for values in [[], list(range(1, 21)), [-1.] * 21, [0.] * 18 + [1.] * 4]:
            self.assertFalse(self.thresholds(values)['000001']['pe']['known'])
        self.assertTrue(self.thresholds(list(range(1, 22)))['000001']['pe']['known'])
        # 评分端要求原序列 >20，percentile_of 再要求至少5个正有限值。
        self.assertTrue(self.thresholds([-1.] * 16 + [1.] * 5)['000001']['pe']['known'])
        cache = Mock(); cache.get.return_value = None
        result = valuation_thresholds(cache, ['000001'], self.plan, 1, '2026-10-21')
        self.assertFalse(result['000001']['pe']['known'])
        with tempfile.TemporaryDirectory() as folder:
            real = Cache(folder)
            real.put('val_baidu_000001_市盈率(TTM)_近五年', self.history(range(30)))
            self.assertFalse(valuation_thresholds(real, ['000001'], self.plan, -1,
                                                 '2026-10-21')['000001']['pe']['known'])

    def test_dates_cleaning_clipping_and_metric_independence(self):
        raw = self.history(range(1, 32))
        raw.loc[len(raw)] = ['2026-10-22', 1e9]
        raw.loc[len(raw)] = ['bad-date', 1e9]
        raw.loc[len(raw)] = ['2026-10-21', 31]  # 与 fetch_valuation_history 同样去重
        cache = Mock(); cache.get.side_effect = [raw, None]
        item = valuation_thresholds(cache, ['000001'], self.plan, 1, '2026-10-21')['000001']
        self.assertEqual(item['pe']['samples'], 31)
        self.assertEqual(item['pe']['history_end'], '2026-10-21')
        self.assertFalse(item['pb']['known'])
        cache.get.side_effect = None; cache.get.return_value = self.history(range(10))
        self.assertFalse(valuation_thresholds(cache, ['000001'], self.plan, 1,
                                              '2026-10-21')['000001']['pe']['known'])

    def test_export_wires_thresholds_without_network(self):
        cache = Mock(); cache.get.return_value = self.history(range(1, 32))
        res = {'stock_results': [SimpleNamespace(code='000001', parts={})],
               'flags': {'as_of': '2026-10-21'}}
        with tempfile.TemporaryDirectory() as folder, patch('publish_site.NAV', []), \
                patch('publish_site.webui.page', return_value=''):
            export(res, self.cfg, Path(folder), cache=cache)
            payload = json.loads((Path(folder) / 'site.js').read_text().split('window.__SITE__ = ')[1][:-2])
        self.assertTrue(payload['thr']['000001']['pe']['known'])
        self.assertIn('sell_pe_percentile', payload['entries']['000001']['sell_snapshot']['percentile_labels'])
        self.assertEqual(cache.get.call_count, 2)


if __name__ == '__main__':
    unittest.main()
