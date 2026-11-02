"""独立来源的缓存、汇总、快照、浏览器回归；全部模拟，不请求上游。"""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import pandas as pd
from touzi.util import Cache, cached_fetch
from touzi.data.stock import fetch_financial_indicators
from touzi.signals.quality import summarize_financials, score_quality
from touzi.config import load_config
from test_financial_analysis import analysis
import test_close_snapshot as close_tests
QUOTE, BUILT = close_tests.QUOTE, close_tests.BUILT
from touzi.close_snapshot import public_entry, export_snapshot, write_snapshot


class FinancialProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Cache(self.tmp.name)
        self.raw = pd.DataFrame({'日期': ['2023-12-31', '2024-12-31', '2025-12-31', '2026-06-30'],
                                 '净资产收益率(%)': [10., None, 20., 2.],
                                 '经营现金净流量与净利润的比率(%)': [1., 2., 3., .2],
                                 '净利润增长率(%)': [10., 20., 30., 1.]})
        self.key = 'fin_600519_2015'

    def meta(self, stamp):
        _, p = self.cache._key_path(self.key)
        m = json.loads(p.read_text()); m['saved_at'] = stamp
        p.write_text(json.dumps(m))

    def fetch(self, fail=False):
        api = SimpleNamespace(stock_financial_analysis_indicator=lambda **kw: self.raw.copy())
        if fail:
            def error(**kw):
                raise ConnectionError('upstream')
            api.stock_financial_analysis_indicator = error
        with patch.dict(sys.modules, akshare=api), patch('touzi.util.time.sleep'):
            return fetch_financial_indicators('600519', self.cache, ttl_days=1)

    def evidence(self, fin):
        return summarize_financials(fin)['indicator_evidence']

    def browser(self, evidence, expression):
        entry = {'financial_analysis': {'quality': {'indicator_evidence': evidence}}}
        code = "import {indicatorFreshness,qualitySourceHTML,qualityEvidenceText,validateIndicatorEvidence} from './scripts/pick-financial.mjs'; const entry=JSON.parse(process.argv[1]); const evidence=entry.financial_analysis.quality.indicator_evidence; " + expression
        return subprocess.check_output(['node', '--input-type=module', '-e', code, json.dumps(entry)], text=True)

    def test_refresh_hit_no_extra_request_and_per_metric_periods(self):
        first = self.fetch(); original = self.cache.saved_at(self.key)
        ev = self.evidence(first)
        self.assertEqual(ev['roe']['cache_status'], 'refreshed')
        self.assertEqual(ev['roe']['freshness'], 'fresh')
        self.assertEqual(ev['roe']['report_periods'], ['2023-12-31', '2025-12-31'])
        self.assertEqual(ev['cashflow']['report_periods'], ['2023-12-31', '2024-12-31', '2025-12-31'])
        self.assertIsNone(ev['stability']['disclosed_at'])
        hit = self.fetch(fail=True)  # 缓存内无需请求，即使接口不可用也命中。
        self.assertEqual(hit.attrs['indicator_source']['cache_status'], 'hit')
        self.assertEqual(self.cache.saved_at(self.key), original)
        self.assertEqual(hit.attrs['indicator_source']['fetched_at'], original.isoformat())
        self.assertIn('重新抓取成功', self.browser(ev, 'console.log(qualitySourceHTML(entry));'))
        self.browser(ev, 'validateIndicatorEvidence(entry.financial_analysis.quality, Date.now());')

    def test_stale_fallback_preserves_actual_time_and_numeric_scores(self):
        normal = self.fetch()
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.meta(old)
        stale = self.fetch(fail=True); ev = self.evidence(stale)
        self.assertEqual(ev['roe']['cache_status'], 'stale_fallback')
        self.assertEqual(ev['roe']['source_status'], 'failed')
        self.assertEqual(ev['roe']['fetched_at'], old)
        self.assertEqual(ev['roe']['freshness'], 'stale')
        a = analysis()
        results = [score_quality({**summarize_financials(f), 'financial_analysis': copy.deepcopy(a)}, load_config()) for f in (normal, stale)]
        self.assertEqual(results[0].score, results[1].score)
        for k in ('weights', 'contributions', 'coverage', 'sub_scores'):
            self.assertEqual(results[0].detail['financial_analysis']['quality'][k], results[1].detail['financial_analysis']['quality'][k])
        self.assertIn('来源失败，回退旧缓存', self.browser(ev, 'console.log(qualityEvidenceText(entry));'))
        self.assertIn('缓存陈旧', self.browser(ev, 'console.log(qualityEvidenceText(entry));'))
        self.browser(ev, 'validateIndicatorEvidence(entry.financial_analysis.quality, Date.now());')

    def test_unknown_malformed_naive_future_are_not_verified(self):
        self.fetch()
        for stamp, state in [('bad', 'missing'), (None, 'missing'),
                             ('2026-01-01T00:00:00', 'unknown_timezone'),
                             ((datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), 'future')]:
            self.meta(stamp)
            ev = self.evidence(self.fetch(fail=True))
            self.assertEqual(ev['roe']['time_status'], state)
            self.assertIsNone(ev['roe']['fetched_at'])
            self.assertEqual(ev['roe']['freshness'], 'unknown')
            self.assertIn('时间未知或异常', self.browser(ev, 'console.log(qualityEvidenceText(entry));'))
            self.browser(ev, 'validateIndicatorEvidence(entry.financial_analysis.quality, Date.now());')

    def test_missing_source_and_empty_schema_are_explicit(self):
        with self.assertRaises(RuntimeError) as caught:
            self.fetch(fail=True)
        meta = caught.exception.fetch_metadata
        self.assertEqual(meta['cache_status'], 'source_failed')
        self.assertEqual(meta['source_status'], 'failed')
        self.assertIsNone(meta['fetched_at'])
        self.raw = pd.DataFrame({'wrong': [1]})
        fin = self.fetch()
        self.assertEqual(fin.attrs['indicator_source']['data_status'], 'invalid_schema')
        self.assertTrue(fin.empty)
        self.assertIn('取数时间未知或异常', self.browser({}, 'console.log(qualityEvidenceText(entry));'))

    def test_integrity_rejects_claims_and_browser_escapes(self):
        ev = self.evidence(self.fetch())
        ev['roe']['freshness'] = 'stale'
        self.browser(ev, "try { validateIndicatorEvidence(entry.financial_analysis.quality,Date.now()); throw new Error('accepted'); } catch(e) {if(e.message==='accepted') process.exit(1);}")
        ev['roe']['source'] = '<script>alert(1)</script>'
        text = self.browser(ev, 'console.log(qualitySourceHTML(entry));')
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)

    def test_expired_cache_refresh_and_missing_metadata_file(self):
        self.fetch()
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.meta(old)
        ev = self.evidence(self.fetch())
        self.assertEqual(ev['roe']['cache_status'], 'refreshed')
        self.assertNotEqual(ev['roe']['fetched_at'], old)
        _, path = self.cache._key_path(self.key)
        path.unlink()
        ev = self.evidence(self.fetch(fail=True))
        self.assertIsNone(ev['roe']['fetched_at'])
        self.assertEqual(ev['roe']['time_status'], 'missing')
        self.assertEqual(ev['roe']['cache_status'], 'stale_fallback')

    def test_full_snapshot_consumer_and_html_preserve_independent_evidence(self):
        class FixedTime(datetime):
            @classmethod
            def now(cls, tz=None):
                actual = datetime.fromisoformat('2026-10-23T09:00:00+00:00')
                return actual.astimezone(tz) if tz else actual.replace(tzinfo=None)
        # 快照时间之前实际已存在的报告，避免把测试未来报告带入历史样本。
        self.raw = self.raw.iloc[:2].copy()
        with patch('touzi.util.datetime', FixedTime):
            fin = self.fetch()
        # 与实际收盘评分相同，报告期截断必须保留 attrs。
        from touzi.screener import _clip_history
        fin = _clip_history(fin, pd.Timestamp('2026-10-23'))
        score = close_tests.CloseSnapshotTests().sample()
        source_analysis = score.parts['quality'].detail['financial_analysis']
        score.parts['quality'] = score_quality({**summarize_financials(fin), 'industry': '制造业',
                                               'financial_analysis': source_analysis}, load_config())
        expected = score.parts['quality'].detail['financial_analysis']['quality']['indicator_evidence']
        entry = public_entry(score, load_config(), 50, QUOTE)
        self.assertEqual(entry['financial_analysis']['quality']['indicator_evidence'], expected)
        counts = dict(market_total=1, eligible_total=1, attempted=1, scored=1, failed=0, excluded=0)
        with tempfile.TemporaryDirectory() as folder:
            _, files = export_snapshot({entry['code']: entry}, load_config(), '2026-10-23', QUOTE, counts, BUILT)
            write_snapshot(Path(folder), files)
            subprocess.run(['node', 'scripts/check_close_snapshot.mjs', folder], check=True, capture_output=True)
        html = self.browser(expected, 'console.log(qualitySourceHTML(entry));')
        self.assertIn('2023-12-31', html)
        self.assertIn('2026-10-23T09:00:00+00:00', html)
        self.assertNotIn('2026-10-23T17:00:00+08:00', html)  # 禁用三表时间
        # 报告期、正式披露时间、来源、取数时间异常都不能冒充完整证据。
        for field, value in [('fetched_at', '2026-02-30T09:00:00Z'),
                             ('checked_at', '2026-10-23T09:00:00'),
                             ('report_periods', ['2026-02-30']),
                             ('disclosed_at', '2026-10-23'), ('source', 'sina-finance-report')]:
            bad = copy.deepcopy(expected); bad['roe'][field] = value
            self.browser(bad, "try { validateIndicatorEvidence(entry.financial_analysis.quality,Date.now()); process.exit(1); } catch(e) {}")

    def test_cache_default_contract_and_fresh_future_rejection(self):
        frame = cached_fetch(self.cache, 'other', lambda: self.raw, retries=0)
        self.assertIsInstance(frame, pd.DataFrame)
        self.fetch()
        self.meta((datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.assertIsNone(self.cache.get(self.key, ttl_days=1))


if __name__ == '__main__':
    unittest.main()
