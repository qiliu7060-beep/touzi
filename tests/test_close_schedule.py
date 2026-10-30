"""调度跨日、安全恢复窗口与Actions最终发布门禁；全部使用固定时钟和假数据。"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.close_batch import (prepare, recovery_window, scheduled_target,
                                 snapshot_covers, workflow_outcome)
from touzi.close_snapshot import export_snapshot, public_entry
from touzi.config import load_config
from touzi.market_calendar import TZ
import tests.test_close_snapshot as fixtures
from tests.test_close_snapshot import QUOTE, BUILT


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def manifest():
    cfg = load_config()
    entry = public_entry(fixtures.CloseSnapshotTests().sample(), cfg, 50, QUOTE)
    counts = dict(market_total=1, eligible_total=1, attempted=1, scored=1, failed=0, excluded=0)
    return export_snapshot({entry['code']: entry}, cfg, '2026-10-23', QUOTE, counts, BUILT)[0]


class CloseScheduleTests(unittest.TestCase):
    def freeze(self, now, quote='2026-10-29 15:05:00', target=date(2026, 10, 29), quotes=None):
        universe = pd.DataFrame([dict(code=f'{i:06d}', name=f'测试{i}', industry='制造业',
                                     price=10., pe_ttm=10., pb=1.) for i in range(10)])
        if quotes is None:
            quotes = universe.copy()
            quotes['quote_ts'] = pd.Timestamp(quote)
        fake = SimpleNamespace(load_universe=lambda: universe,
             prefilter=lambda _: (universe, universe.iloc[:0]), proxy=None,
             _div_summary_map=lambda: {}, _bond_yield=lambda: 2.)
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            for name, value in [('scripts.close_batch.bound_http', None),
                                ('touzi.screener.Screener', fake),
                                ('touzi.data.stock.fetch_realtime_quote', quotes),
                                ('touzi.regime.build_macro_context', SimpleNamespace(macro_score=70.)),
                                ('touzi.util.Cache', None)]:
                stack.enter_context(patch(name, return_value=value))
            result = prepare(folder, now=now, previous={}, target_day=target)
            context = json.loads((Path(folder) / 'context.json').read_text()) if result['run'] else None
            return result, context

    def test_both_reported_delayed_runs_target_oct29(self):
        self.assertEqual(scheduled_target('2026-10-29T16:17:01Z', '23 9 * * 1-5'), date(2026, 10, 29))
        self.assertEqual(scheduled_target('2026-10-29T16:59:38Z', '23 10 * * 1-5'), date(2026, 10, 29))

    def test_nominal_and_early_morning_compensation_target_same_close(self):
        for at, cron in [('2026-10-29T09:23:00Z', '23 9 * * 1-5'),
                         ('2026-10-29T21:23:00Z', '23 21 * * 1-5')]:
            self.assertEqual(scheduled_target(at, cron), date(2026, 10, 29))
        # 周五补偿在北京时间周六；不能误判周六休市而跳过周五。
        self.assertEqual(scheduled_target('2026-10-30T21:23:00Z', '23 21 * * 1-5'), date(2026, 10, 30))
        with self.assertRaises(ValueError):
            scheduled_target('2026-10-29T21:23:00Z', 'unrecognised')

    def test_normal_close_and_cross_midnight_freeze_keep_real_quote(self):
        for now in ['2026-10-29T17:23:00', '2026-10-30T00:17:00', '2026-10-30T09:29:59']:
            result, context = self.freeze(instant(now))
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(context['score_date'], '2026-10-29')
            self.assertEqual(context['quote_at'], '2026-10-29T15:05:00+08:00')
            self.assertTrue(all(t.startswith('2026-10-29T') for t in context['quote_times'].values()))

    def test_wrong_day_stale_intraday_and_future_quotes_fail(self):
        for quote in ['2026-10-28 15:05:00', '2026-10-30 15:05:00', '2026-10-29 14:59:59']:
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, '真实收盘日期/时间覆盖不足'):
                self.freeze(instant('2026-10-30T00:17:00'), quote)
        with self.assertRaisesRegex(ValueError, '真实收盘日期/时间覆盖不足'):
            self.freeze(instant('2026-10-29T15:05:00'), '2026-10-29 15:06:00')

    def test_opening_or_later_refuses_before_any_source(self):
        for now in ['2026-10-30T09:30:00', '2026-10-30T14:00:00', '2026-11-02T00:00:00']:
            with patch('scripts.close_batch.bound_http') as source, self.assertRaisesRegex(ValueError, '漏更'):
                prepare('unused', now=instant(now), previous={}, target_day=date(2026, 10, 29))
            source.assert_not_called()

    def test_quote_request_crosses_opening_is_rejected(self):
        with patch('scripts.close_batch.datetime') as clock:
            clock.now.side_effect = [instant('2026-10-30T09:29:00'), instant('2026-10-30T09:30:00')]
            # fixed now=None uses the real-clock branch, while network is mocked by freeze().
            with self.assertRaisesRegex(ValueError, '漏更'):
                self.freeze(None)

    def test_holiday_is_explicit_skip_unknown_calendar_is_error(self):
        with patch('scripts.close_batch.prior_manifest') as source:
            result = prepare('unused', now=instant('2026-10-02T00:17:00'), target_day=date(2026, 10, 1))
            self.assertEqual(result['status'], 'non_trading_day')
            source.assert_not_called()
            with self.assertRaisesRegex(ValueError, '交易日历'):
                prepare('unused', now=instant('2027-01-01T17:23:00'))

    def test_weekend_retained_quote_and_next_open_boundary(self):
        recovery_window(date(2026, 10, 30), instant('2026-10-31T05:23:00'))
        recovery_window(date(2026, 9, 30), instant('2026-10-08T09:29:59'))
        with self.assertRaises(ValueError):
            recovery_window(date(2026, 9, 30), instant('2026-10-08T09:30:00'))

    def test_existing_snapshot_is_no_network_idempotent_even_after_window(self):
        previous = manifest()
        with patch('scripts.close_batch.bound_http') as source:
            for now in ['2026-10-24T00:17:00', '2026-10-26T10:00:00']:
                value = prepare('unused', now=instant(now), previous=previous, target_day=date(2026, 10, 23))
                self.assertEqual(value['status'], 'already_published')
            source.assert_not_called()

    def test_corrupt_existing_snapshot_does_not_silently_skip(self):
        for change in [{'kind': 'bad'}, {'universe': {'scored': 1}}, {'index': []}]:
            previous = dict(manifest(), **change)
            with self.assertRaises((ValueError, KeyError)):
                snapshot_covers(previous, date(2026, 10, 23), instant('2026-10-24T00:17:00'))

    def test_publish_duplicate_and_newer_snapshot_never_write(self):
        import base64
        from io import BytesIO
        from scripts.publish_close import publish
        from touzi.close_snapshot import write_snapshot
        cfg = load_config()
        entry = public_entry(fixtures.CloseSnapshotTests().sample(), cfg, 50, QUOTE)
        previous, files = export_snapshot({entry['code']: entry}, cfg, '2026-10-23', QUOTE,
            dict(market_total=1, eligible_total=1, attempted=1, scored=1, failed=0, excluded=0), BUILT)
        for existing_day in ['2026-10-23', '2026-10-26']:
            calls = []
            def request(req, **kwargs):
                calls.append(req.get_method())
                if '/git/ref/' in req.full_url:
                    value = {'object': {'sha': 'parent'}}
                elif '/git/commits/' in req.full_url:
                    value = {'tree': {'sha': 'base'}}
                else:
                    value = {'content': base64.b64encode(json.dumps(dict(previous, score_date=existing_day)).encode()).decode()}
                return BytesIO(json.dumps(value).encode())
            with tempfile.TemporaryDirectory() as folder:
                write_snapshot(Path(folder), files)
                self.assertFalse(publish(folder, 'test-only-token', request=request)['published'])
            self.assertEqual(calls, ['GET', 'GET', 'GET'])

    def test_post_publish_readback_missing_or_stale_is_failure(self):
        from scripts.close_batch import main
        with patch('sys.argv', ['close_batch.py', 'verify', '--score-date', '2026-10-29']):
            for previous in [None, manifest()]:
                with patch('scripts.close_batch.prior_manifest', return_value=previous):
                    with self.assertRaisesRegex(ValueError, '收盘漏更'):
                        main()

    def test_manifest_read_is_pinned_to_branch_commit(self):
        from io import BytesIO
        from scripts.close_batch import prior_manifest
        urls = []
        def request(req, **kwargs):
            urls.append(req.full_url)
            value = {'object': {'sha': 'a' * 40}} if '/git/ref/' in req.full_url else {'score_date': '2026-10-29'}
            return BytesIO(json.dumps(value).encode())
        with patch('scripts.close_batch.urllib.request.urlopen', side_effect=request):
            self.assertEqual(prior_manifest()['score_date'], '2026-10-29')
        self.assertIn('/git/ref/heads/close-data', urls[0])
        self.assertIn('/' + 'a' * 40 + '/close-scores.json', urls[1])

    def test_workflow_gate_fails_skipped_or_failed_real_publication(self):
        needs = {key: {'result': 'success'} for key in ['check', 'prepare', 'score', 'publish']}
        needs['prepare']['outputs'] = {'status': 'ready'}
        self.assertEqual(workflow_outcome(needs), 'published')
        for key in needs:
            for state in ['failure', 'cancelled', 'skipped']:
                broken = deepcopy(needs)
                broken[key]['result'] = state
                with self.assertRaises(ValueError):
                    workflow_outcome(broken)
        for status in ['already_published', 'non_trading_day']:
            skipped = deepcopy(needs)
            skipped['prepare']['outputs']['status'] = status
            skipped['score']['result'] = skipped['publish']['result'] = 'skipped'
            self.assertEqual(workflow_outcome(skipped), status)
        for status in ['', 'outside_window']:
            broken = deepcopy(needs)
            broken['prepare']['outputs']['status'] = status
            with self.assertRaises(ValueError):
                workflow_outcome(broken)


if __name__ == '__main__':
    unittest.main()
