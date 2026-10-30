"""收盘批次冻结、个股进程预算、原子聚合与发布的确定性回归。"""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.close_batch import assemble, atomic_json, prepare, score_batch
from scripts.publish_close import validate_folder
import tests.test_close_snapshot as fixtures
from tests.test_close_snapshot import QUOTE, BUILT
from touzi.close_snapshot import public_entry
from touzi.config import load_config
from touzi.market_calendar import TZ


class CloseBatchTests(unittest.TestCase):
    def test_prepare_refuses_intraday_before_touching_sources(self):
        with patch('scripts.close_batch.prior_manifest') as source:
            self.assertFalse(prepare('unused', now=datetime(2026, 10, 26, 14, 59, tzinfo=TZ))["run"])
            source.assert_not_called()

    def test_same_day_valid_snapshot_is_idempotent(self):
        from tests.test_close_schedule import manifest
        previous = manifest()
        with patch('scripts.close_batch.bound_http') as network:
            result = prepare('unused', now=datetime.fromisoformat(BUILT), previous=previous)
            self.assertFalse(result['run'])
            network.assert_not_called()

    def test_prepare_freezes_all_701_eligible_codes_not_top300(self):
        universe = pd.DataFrame([dict(code=f'{i:06d}', name=f'企业{i}', industry='制造业',
                                 price=10., pe_ttm=10., pb=1.) for i in range(1, 703)])
        quotes = universe.iloc[:701].copy()
        quotes['quote_ts'] = pd.Timestamp('2026-10-26 16:15:00')
        fake = SimpleNamespace(load_universe=lambda: universe, prefilter=lambda _: (universe.iloc[:701], universe.iloc[701:]),
                  proxy=None, _div_summary_map=lambda: {}, _bond_yield=lambda: 2.)
        with tempfile.TemporaryDirectory() as folder, patch('scripts.close_batch.bound_http'), \
             patch('touzi.screener.Screener', return_value=fake), \
             patch('touzi.data.stock.fetch_realtime_quote', return_value=quotes), \
             patch('touzi.regime.build_macro_context', return_value=SimpleNamespace(macro_score=70.)), \
             patch('touzi.util.Cache'):
            result = prepare(folder, previous={}, now=datetime(2026, 10, 26, 17, 23, tzinfo=TZ))
            context = json.loads((Path(folder) / 'context.json').read_text(encoding='utf-8'))
            self.assertTrue(result['run'])
            self.assertEqual(result['eligible'], 701)
            self.assertEqual(len(result['matrix']['include']), 8)
            self.assertEqual(context['market_total'], 702)
            self.assertEqual(len(context['quote_times']), 701)

    def test_stock_timeout_is_failed_code_and_other_work_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(root / 'context.json', {'score_date': '2026-10-23'})
            atomic_json(root / 'batches.json', [{'batch': 0, 'codes': ['000001', '000002']}])
            def run(command, **kwargs):
                code = command[command.index('--code') + 1]
                if code == '000001':
                    raise subprocess.TimeoutExpired(command, 1)
                atomic_json(command[command.index('--output') + 1], {'code': code})
            with patch('scripts.close_batch.subprocess.run', side_effect=run):
                score_batch(root, 0, root / 'result', workers=2, stock_timeout=1)
            result = json.loads((root / 'result/batch.json').read_text(encoding='utf-8'))
            self.assertEqual(set(result['failures']), {'000001'})
            self.assertEqual(set(result['entries']), {'000002'})

    def test_real_clock_advances_after_slow_quote_request_but_fixed_clock_does_not(self):
        start = datetime(2026, 10, 26, 16, 8, 30, tzinfo=TZ)
        returned = datetime(2026, 10, 26, 16, 10, tzinfo=TZ)
        universe = pd.DataFrame([dict(code='000001', name='慢请求样本', industry='制造业',
                                     price=10., pe_ttm=10., pb=1.)])
        quotes = universe.copy()
        quotes['quote_ts'] = pd.Timestamp('2026-10-26 16:09:30')
        fake = SimpleNamespace(load_universe=lambda: universe, prefilter=lambda _: (universe, universe.iloc[:0]),
                               proxy=None, _div_summary_map=lambda: {}, _bond_yield=lambda: 2.)
        with tempfile.TemporaryDirectory() as folder, patch('scripts.close_batch.bound_http'), \
             patch('scripts.close_batch.prior_manifest', return_value={}), \
             patch('scripts.close_batch.datetime') as clock, \
             patch('touzi.screener.Screener', return_value=fake), \
             patch('touzi.data.stock.fetch_realtime_quote', return_value=quotes), \
             patch('touzi.regime.build_macro_context', return_value=SimpleNamespace(macro_score=70.)), \
             patch('touzi.util.Cache'):
            clock.now.side_effect = [start, returned, returned]
            self.assertTrue(prepare(folder)['run'])
            context = json.loads((Path(folder) / 'context.json').read_text(encoding='utf-8'))
            self.assertEqual(context['quote_times']['000001'], '2026-10-26T16:09:30+08:00')
            self.assertEqual(context['prepared_at'], returned.isoformat())
            # 显式固定时刻下，相同报价确实在未来，仍须拒绝而非放宽验证。
            with self.assertRaisesRegex(ValueError, '真实收盘日期/时间覆盖不足'):
                prepare(folder, previous={}, now=start)

    def test_assemble_counts_partial_failure_and_preserves_previous_on_missing_batch(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            codes = [f'{i:06d}' for i in range(1, 11)]
            context = dict(score_date='2026-10-23', quote_at=QUOTE, codes=codes,
                           market_total=12, eligible_total=10, excluded=2)
            atomic_json(root / 'input/context.json', context)
            atomic_json(root / 'input/batches.json', [{'batch': 0, 'codes': codes[:5]}, {'batch': 1, 'codes': codes[5:]}])
            entries = {c: public_entry(fixtures.CloseSnapshotTests().sample(c), cfg, 50, QUOTE) for c in codes[:-1]}
            for i in range(2):
                part_codes = codes[i * 5:(i + 1) * 5]
                atomic_json(root / f'results/{i}/batch.json', dict(batch=i, score_date='2026-10-23',
                  entries={c: entries[c] for c in part_codes if c in entries}, failures={c: '数据缺失' for c in part_codes if c not in entries},
                  diagnostics={part_codes[0]: [{'stage': 'fin', 'state': 'start', 'offset_ms': 10}]}))
            manifest = assemble(root / 'input', root / 'results', root / 'output', now=datetime.fromisoformat(BUILT))
            self.assertEqual(manifest['universe']['failed'], 1)
            self.assertEqual(manifest['universe']['scored'], 9)
            validate_folder(root / 'output')
            # 批次诊断只能留在作业artifact，不能进入公开manifest或分片。
            self.assertNotIn('diagnostics', manifest)
            for asset in (root / 'output').rglob('*.json'):
                self.assertNotIn('"diagnostics"', asset.read_text(encoding='utf-8'))
                self.assertNotIn('"offset_ms"', asset.read_text(encoding='utf-8'))
            previous = (root / 'output/close-scores.json').read_bytes()
            atomic_json(root / 'input/batches.json', [{'batch': 3, 'codes': codes}])
            with self.assertRaises(ValueError):
                assemble(root / 'input', root / 'results', root / 'output', now=datetime.fromisoformat(BUILT))
            self.assertEqual((root / 'output/close-scores.json').read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
