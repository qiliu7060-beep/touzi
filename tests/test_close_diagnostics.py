"""诊断保留真实失败分类与最后阶段，但不公开原始响应或凭据。"""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

import pandas as pd

from scripts.close_batch import atomic_json, score_batch
from touzi.data.financial import fetch_financial_statements
from touzi.diagnostics import DiagnosticRecorder, MAX_BYTES, MAX_EVENTS, error_category, read_diagnostics
from touzi.screener import Screener
from touzi.util import cached_fetch


class MemoryCache:
    def __init__(self, fresh=None, stale=None):
        self.fresh, self.stale = fresh, stale

    def get(self, key, ttl_days=None, allow_stale=False):
        return self.stale if allow_stale else self.fresh

    def put(self, key, frame):
        pass


class CloseDiagnosticsTests(unittest.TestCase):
    def test_wrapped_http_and_timeout_keep_category_without_exception_text(self):
        original = urllib.error.HTTPError('https://user:password@example.test/?token=secret', 429, 'private', {}, None)
        with self.assertRaises(RuntimeError) as failure:
            cached_fetch(MemoryCache(), 'public-test', lambda: (_ for _ in ()).throw(original), retries=0)
        self.assertIs(failure.exception.__cause__, original)
        self.assertEqual(error_category(failure.exception), {'category': 'http', 'http_status': 429})
        self.assertEqual(error_category(urllib.error.URLError(TimeoutError('secret'))), {'category': 'timeout'})

    def test_financial_http_failure_and_stale_cache_are_distinct_and_safe(self):
        error = urllib.error.HTTPError('https://user:password@example.test/?token=secret', 403, 'private', {}, None)
        for stale in (None, pd.DataFrame([{'fetched_at': '2026-10-23T10:00:00+08:00'}])):
            events = []
            with patch('touzi.data.financial.urllib.request.urlopen', side_effect=error), \
                 patch('touzi.util.time.sleep'), self.assertLogs('touzi', level='WARNING') as logs:
                result = fetch_financial_statements('000708', MemoryCache(stale=stale), diagnostic=events.append)
            endings = [e for e in events if e['state'] in ('stale_cache', 'failed') and 'elapsed_ms' in e]
            for source in ('fzb', 'lrb', 'llb'):
                end = [e for e in endings if e['stage'] == source][-1]
                self.assertEqual(end['state'], 'failed' if stale is None else 'stale_cache')
                self.assertEqual(end['category'], 'http')
                self.assertEqual(end['http_status'], 403)
                self.assertEqual(result[source].empty, stale is None)
            evidence = json.dumps(events) + str(logs.output)
            for secret in ('password', 'token=secret', 'example.test', 'private'):
                self.assertNotIn(secret, evidence)

    def test_financial_json_structure_and_normalization_differ(self):
        cases = [(b'{', 'json'), (b'{"result":{}}', 'structure'),
                 (json.dumps({'result': {'data': {'report_list': {'20251231': {
                     'rCurrency': 'USD', 'rType': '合并期末', 'data': []}}}}}).encode(), 'normalization')]
        for raw, expected in cases:
            events = []
            with patch('touzi.data.financial.urllib.request.urlopen', side_effect=lambda *a, **k: io.BytesIO(raw)), \
                 patch('touzi.util.time.sleep'):
                result = fetch_financial_statements('000708', MemoryCache(), diagnostic=events.append)
            self.assertTrue(all(frame.empty for frame in result.values()))
            self.assertEqual({e.get('category') for e in events if e['state'] == 'failed'}, {expected})

    def test_fresh_cache_does_not_fetch_or_change_original_time(self):
        stamp = '2026-10-28T18:00:00+08:00'
        events = []
        with patch('touzi.data.financial.urllib.request.urlopen') as network:
            result = fetch_financial_statements('000708', MemoryCache(fresh=pd.DataFrame([{'fetched_at': stamp}])), diagnostic=events.append)
        network.assert_not_called()
        self.assertEqual([e['state'] for e in events].count('fresh_cache'), 3)
        self.assertTrue(all(frame.iloc[0]['fetched_at'] == stamp for frame in result.values()))

    def test_retry_success_is_not_mislabeled_as_stale_cache(self):
        raw = json.dumps({'result': {'data': {'report_list': {'20251231': {
            'rCurrency': 'CNY', 'rType': '合并期末', 'data': []}}}}}).encode()
        counts, lock, events = {}, threading.Lock(), []
        def response(url, **kwargs):
            with lock:
                counts[url] = counts.get(url, 0) + 1
                attempt = counts[url]
            if attempt == 1:
                raise TimeoutError('credential must not be copied')
            return io.BytesIO(raw)
        with patch('touzi.data.financial.urllib.request.urlopen', side_effect=response), \
             patch('touzi.util.time.sleep'):
            result = fetch_financial_statements('000708', MemoryCache(), diagnostic=events.append)
        self.assertTrue(all(not frame.empty for frame in result.values()))
        self.assertEqual(sum(e['state'] == 'ok' for e in events), 3)
        self.assertFalse(any(e['state'] == 'stale_cache' for e in events))
        self.assertEqual(sum(e.get('category') == 'timeout' for e in events), 3)

    def test_cache_read_failure_is_classified_without_exposing_path(self):
        cache = MemoryCache()
        cache.get = lambda *a, **k: (_ for _ in ()).throw(PermissionError('private-cache-path'))
        events = []
        with self.assertLogs('touzi', level='WARNING') as logs:
            result = fetch_financial_statements('000708', cache, diagnostic=events.append)
        self.assertTrue(all(frame.empty for frame in result.values()))
        self.assertEqual({e.get('category') for e in events if e['state'] == 'failed'}, {'cache'})
        self.assertNotIn('private-cache-path', json.dumps(events) + str(logs.output))

    def test_fetch_stage_failure_still_runs_remaining_getters(self):
        screener = object.__new__(Screener)
        events = []
        screener.ttls = {'history': 1, 'fund': 1}
        screener.cache, screener.proxy, screener.diagnostic = None, None, events.append
        with patch('touzi.screener.fetch_valuation_history', side_effect=[TimeoutError('secret'), pd.DataFrame()]), \
             patch('touzi.screener.fetch_dividend_detail', return_value=pd.DataFrame()), \
             patch('touzi.screener.fetch_financial_indicators', return_value=pd.DataFrame()), \
             patch('touzi.screener.fetch_financial_statements', return_value={'lrb': pd.DataFrame()}):
            result = screener._fetch_one('000708')
        self.assertIsNone(result['pe_hist'])
        self.assertIn('statements', result)
        completed = [e for e in events if e['state'] != 'start']
        self.assertEqual([e['stage'] for e in completed], ['pe_hist', 'pb_hist', 'div', 'fin', 'statements'])
        self.assertEqual(completed[0]['category'], 'timeout')
        self.assertTrue(all(e['elapsed_ms'] >= 0 for e in completed))
        self.assertNotIn('secret', json.dumps(events))

    def test_recorder_is_bounded_and_timeout_batch_retains_last_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sidecar = root / 'record.json'
            record = DiagnosticRecorder(sidecar)
            for _ in range(MAX_EVENTS + 9):
                record({'stage': 'fin', 'state': 'start', 'raw_error': 'secret', 'url': 'private'})
            self.assertEqual(len(read_diagnostics(sidecar)), MAX_EVENTS)
            self.assertLessEqual(sidecar.stat().st_size, MAX_BYTES)
            self.assertNotIn('secret', sidecar.read_text())
            atomic_json(root / 'context.json', {'score_date': '2026-10-28'})
            atomic_json(root / 'batches.json', [{'batch': 0, 'codes': ['000708']}])
            def run(command, **kwargs):
                output = Path(command[command.index('--output') + 1])
                DiagnosticRecorder(output.with_suffix('.diagnostics.json'))({'stage': 'fin', 'state': 'start'})
                raise subprocess.TimeoutExpired(command, 90)
            with patch('scripts.close_batch.subprocess.run', side_effect=run):
                score_batch(root, 0, root / 'results')
            batch = json.loads((root / 'results/batch.json').read_text(encoding='utf-8'))
            self.assertIn('000708', batch['failures'])
            self.assertEqual(batch['diagnostics']['000708'][-1]['stage'], 'fin')
            self.assertEqual(batch['diagnostics']['000708'][-1]['state'], 'start')
            # 下一轮若worker尚未启动就失败，不能误保留上次fin阶段。
            with patch('scripts.close_batch.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'worker')):
                score_batch(root, 0, root / 'results')
            batch = json.loads((root / 'results/batch.json').read_text(encoding='utf-8'))
            self.assertEqual(batch['diagnostics']['000708'][-1]['stage'], 'worker')

    def test_malformed_sidecar_tags_do_not_interrupt_batch_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'malformed.json'
            path.write_text(json.dumps({'events': [
                {'stage': [], 'state': 'start'},
                {'stage': 'fin', 'state': {}},
                {'stage': 'fin', 'state': 'failed', 'category': ['secret'], 'elapsed_ms': 10 ** 400},
            ]}), encoding='utf-8')
            self.assertEqual(read_diagnostics(path), [{'stage': 'fin', 'state': 'failed'}])


if __name__ == '__main__':
    unittest.main()
