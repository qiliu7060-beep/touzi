"""投资环境发布闸门与并发保护，全部使用确定性假网络。"""
import base64
from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from scripts.publish_investment import publish, validate
from touzi.market_calendar import CALENDAR, TZ

NOW = datetime(2026, 11, 2, 18, tzinfo=TZ)


def fixture():
    return {"schema_version": 1, "kind": "touzi-investment-v1", "rule_version": "yield-trend-v1", "as_of": "2026-11-02", "built_at": NOW.isoformat(), "trading_calendar": deepcopy(CALENDAR),
            "macro": {"score": 50, "position": .5, "computed_at": NOW.isoformat(), "status": "ok"},
            "bond": {"yield_pct": 1.65, "observation_date": "2026-10-30", "fetched_at": NOW.isoformat(), "delta20_bp": 1, "delta60_bp": 3, "status": "ok", "factor": .75, "reasons": ["测试原因"]}, "errors": []}


class FakeAPI:
    def __init__(self, old=None, conflict=False):
        self.old, self.conflict, self.calls, self.patches = old, conflict, [], 0

    def __call__(self, req, timeout):
        path = req.full_url.split('/touzi', 1)[1]
        data = json.loads(req.data) if req.data else None
        self.calls.append((req.method, path, data))
        if path.startswith('/git/ref/heads/'):
            value = {"object": {"sha": "parent" + str(self.patches)}}
        elif path.startswith('/git/commits/'):
            value = {"tree": {"sha": "tree" + str(self.patches)}}
        elif path.startswith('/contents/'):
            if self.old is None:
                raise urllib.error.HTTPError(req.full_url, 404, 'missing', {}, None)
            value = {"content": base64.b64encode(json.dumps(self.old).encode()).decode()}
        elif path == '/git/blobs':
            value = {"sha": "blob"}
        elif path == '/git/trees':
            value = {"sha": "newtree"}
        elif path == '/git/commits':
            value = {"sha": "commit"}
        elif path.startswith('/git/refs/'):
            self.patches += 1
            if self.conflict and self.patches == 1:
                raise urllib.error.HTTPError(req.full_url, 422, 'race', {}, None)
            value = {}
        else:
            raise AssertionError(path)
        return io.BytesIO(json.dumps(value).encode())


class PublishTests(unittest.TestCase):
    def publish_value(self, value, api):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'investment-context.json'
            target.write_text(json.dumps(value), encoding='utf-8')
            return publish(target, 'test-token', request=api, now=NOW)

    def test_cas_retry_preserves_latest_tree_and_only_one_file(self):
        api = FakeAPI(conflict=True)
        self.assertTrue(self.publish_value(fixture(), api)['published'])
        trees = [x[2] for x in api.calls if x[1] == '/git/trees']
        self.assertEqual([x['base_tree'] for x in trees], ['tree0', 'tree1'])
        self.assertTrue(all([e['path'] for e in x['tree']] == ['investment-context.json'] for x in trees))
        self.assertTrue(all(x[2]['force'] is False for x in api.calls if x[0] == 'PATCH'))
        self.assertEqual([x[1] for x in api.calls if x[1].startswith('/contents/')], ['/contents/investment-context.json?ref=parent0', '/contents/investment-context.json?ref=parent1'])

    def test_newer_remote_blocks_rollback(self):
        old = fixture()
        old['built_at'] = '2026-11-02T18:00:30+08:00'
        api = FakeAPI(old)
        self.assertFalse(self.publish_value(fixture(), api)['published'])
        self.assertFalse(any(x[0] != 'GET' for x in api.calls))

    def test_identical_noop(self):
        self.assertFalse(self.publish_value(fixture(), FakeAPI(fixture()))['published'])

    def test_old_inputs_not_masked_by_new_built_timestamp(self):
        for field in ['observation_date', 'fetched_at']:
            value = fixture()
            value['bond'][field] = '2026-10-12' if field == 'observation_date' else '2026-10-12T18:00:00+08:00'
            api = FakeAPI()
            with self.assertRaises(ValueError):
                self.publish_value(value, api)
            self.assertEqual(api.calls, [])

    def test_invalid_or_private_fields_never_reach_network(self):
        for mutate in [lambda x: x.update(holdings=[]), lambda x: x['macro'].update(position=float('nan')), lambda x: x.update(errors=['抓取失败']), lambda x: x['bond'].update(factor=.8), lambda x: x['macro'].update(position=True)]:
            value = fixture()
            mutate(value)
            api = FakeAPI()
            with self.assertRaises(ValueError):
                self.publish_value(value, api)
            self.assertEqual(api.calls, [])

    def test_future_and_unknown_calendar_fail(self):
        value = fixture()
        value['built_at'] = '2026-11-02T19:00:00+08:00'
        with self.assertRaises(ValueError):
            validate(json.dumps(value).encode(), NOW)
        value = fixture()
        value['trading_calendar']['version'] = 'unknown'
        with self.assertRaises(ValueError):
            validate(json.dumps(value).encode(), NOW)

    def test_same_day_newer_macro_can_publish(self):
        old = fixture()
        old['built_at'] = '2026-11-02T17:00:00+08:00'
        old['macro']['computed_at'] = old['built_at']
        old['bond']['fetched_at'] = old['built_at']
        self.assertTrue(self.publish_value(fixture(), FakeAPI(old))['published'])

    def test_roll_back_bond_observation_is_blocked(self):
        old = fixture()
        old['bond']['observation_date'] = '2026-11-02'
        api = FakeAPI(old)
        self.assertFalse(self.publish_value(fixture(), api)['published'])
        self.assertFalse(any(x[0] != 'GET' for x in api.calls))

    def test_workflow_gates_publish_after_producer(self):
        text = Path('.github/workflows/investment-context.yml').read_text(encoding='utf-8')
        self.assertLess(text.index('python scripts/build_investment.py'), text.index('python scripts/publish_investment.py'))
        self.assertNotIn('continue-on-error', text)
        self.assertNotIn('if: always()', text)


if __name__ == '__main__':
    unittest.main()
