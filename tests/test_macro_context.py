"""完整宏观合同、兼容投影与原子发布，全部使用确定性样本。"""
from copy import deepcopy
from datetime import datetime
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from touzi.macro_context import canonical_bytes, snapshot_id, project_v1, validate_context, input_rollback
from touzi.market_calendar import TZ
from scripts.publish_investment import publish, validate
from scripts.build_investment import write_contexts

NOW = datetime(2026, 11, 16, 18, tzinfo=TZ)
FIXTURE = Path(__file__).parent / "fixtures/investment-context-v2.json"


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def resign(value):
    value["snapshot_id"] = snapshot_id(value)
    return value


class PairAPI:
    def __init__(self, previous=None, conflict=False):
        self.previous, self.conflict = previous, conflict
        self.calls, self.patches = [], 0

    def __call__(self, request, timeout):
        path = request.full_url.split("/touzi", 1)[1]
        data = json.loads(request.data) if request.data else None
        self.calls.append((request.method, path, data))
        if path.startswith("/git/ref/heads/"):
            result = {"object": {"sha": f"parent{self.patches}"}}
        elif path.startswith("/git/commits/"):
            result = {"tree": {"sha": f"base{self.patches}"}}
        elif path.startswith("/contents/"):
            if self.previous is None:
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            value = self.previous if "v2.json" in path else project_v1(self.previous)
            result = {"content": base64.b64encode(json.dumps(value).encode()).decode()}
        elif path in ("/git/blobs", "/git/trees", "/git/commits"):
            result = {"sha": "new"}
        elif path.startswith("/git/refs/"):
            self.patches += 1
            if self.conflict and self.patches == 1:
                raise urllib.error.HTTPError(request.full_url, 422, "race", {}, None)
            result = {}
        else:
            raise AssertionError(path)
        return io.BytesIO(json.dumps(result).encode())


class MacroContextTests(unittest.TestCase):
    def test_golden_identity_and_exact_legacy_projection(self):
        value = validate_context(FIXTURE.read_bytes(), NOW)
        self.assertEqual(value["snapshot_id"], "8f793554e2102aec980b25792d9ec2596dccbb3aac8c437353a3cfd70f788eb5")
        legacy = project_v1(value)
        self.assertEqual(validate(json.dumps(legacy).encode(), NOW), legacy)
        self.assertEqual(set(legacy["macro"]), {"score", "position", "computed_at", "status"})
        self.assertEqual(legacy["macro"]["score"], value["macro"]["score"])

    def test_canonical_binary64_is_order_and_number_format_independent(self):
        self.assertEqual(canonical_bytes({"a": 1, "b": -0.0, "c": 1e-7}), canonical_bytes({"c": .0000001, "b": 0, "a": 1.0}))
        self.assertIn(b'3e7ad7f29abcaf48', canonical_bytes(1e-7))
        for bad in (float("nan"), float("inf"), 2 ** 54):
            with self.assertRaises(ValueError):
                canonical_bytes(bad)

    def test_incomplete_rules_and_source_evidence_rejected(self):
        for group, key in [("rules", "cpi"), ("weights", "fed"), ("inputs", "gdp")]:
            value = fixture()
            value["macro"][group].pop(key)
            with self.assertRaises(ValueError):
                validate_context(resign(value), NOW)
        value = fixture()
        for key in ("policy_rate", "us_10y", "dollar"):
            value["macro"]["inputs"].pop(key)
        with self.assertRaises(ValueError):
            validate_context(resign(value), NOW)

    def test_unknown_release_time_is_honest_and_new_time_does_not_revive_cache(self):
        value = fixture()
        self.assertIsNone(validate_context(value, NOW)["macro"]["inputs"]["cpi"]["release_at"])
        for mutate in (
            lambda x: x.update(fetch_status="stale_fallback"),
            lambda x: x.update(fetched_at="2026-10-12T18:00:00+08:00"),
            lambda x: x.update(fetched_at="2026-11-16T18:00:01+08:00"),
            lambda x: x.update(fetched_at="2026-11-16T17:58:00"),
            lambda x: x.update(observation_date="2026-12-31"),
            lambda x: x.update(source="untrusted:source"),
        ):
            value = fixture()
            mutate(value["macro"]["inputs"]["cpi"])
            with self.assertRaises(ValueError):
                validate_context(resign(value), NOW)

    def test_nonexistent_timestamp_dates_and_24_hour_clock_are_not_normalized(self):
        for observed, fetched in [
            ("2026-02-28", "2026-02-30T17:58:00+08:00"),
            ("2026-10-31", "2026-11-15T24:00:00+08:00"),
        ]:
            value = fixture()
            value["macro"]["inputs"]["cpi"].update(observation_date=observed, fetched_at=fetched)
            with self.assertRaises(ValueError):
                validate_context(resign(value), NOW, freshness=False)

    def test_monthly_observation_is_not_daily_stale_and_history_keeps_original_dates(self):
        value = fixture()
        self.assertEqual(validate_context(value, NOW)["macro"]["inputs"]["cpi"]["observation_date"], "2026-10-31")
        later = datetime(2026, 11, 23, 18, tzinfo=TZ)
        with self.assertRaises(ValueError):
            validate_context(value, later)
        self.assertEqual(validate_context(value, later, freshness=False)["built_at"], "2026-11-16T18:00:00+08:00")

    def test_no_private_or_unversioned_extra_fields_and_weighted_total_matches(self):
        for mutate in (
            lambda x: x.update(holdings=[]),
            lambda x: x["macro"].update(score=0),
            lambda x: x["macro"]["inputs"]["cpi"].update(cost=1),
            lambda x: x["macro"]["weights"].update(cpi=.5),
            lambda x: x["macro"]["rules"]["cpi"].update(score=True),
            lambda x: x.update(schema_version=True),
            lambda x: x["bond"].update(fetched_at="2026-11-16T17:59:00+08:00"),
        ):
            value = fixture()
            mutate(value)
            with self.assertRaises(ValueError):
                validate_context(resign(value), NOW)

    def test_digest_change_and_optional_source_rollback(self):
        value = fixture()
        value["macro"]["regime"] = "changed"
        with self.assertRaises(ValueError):
            validate_context(value, NOW)
        old, new = fixture(), fixture()
        old["macro"]["inputs"]["cpi"]["observation_date"] = "2026-11-30"
        self.assertTrue(input_rollback(new, old))
        old = fixture()
        old["macro"]["inputs"]["balance_sheet"] = deepcopy(old["macro"]["inputs"]["policy_rate"])
        self.assertFalse(input_rollback(new, old))

    def publish_pair(self, value, api, legacy=None):
        with tempfile.TemporaryDirectory() as folder:
            one, two = write_contexts(value, Path(folder) / "investment-context.json")
            if legacy is not None:
                one.write_text(json.dumps(legacy), encoding="utf-8")
            return publish(one, "synthetic-test-token", request=api, now=NOW, context_file=two)

    def test_paired_publication_is_one_tree_and_conflict_rebases_to_latest_tree(self):
        api = PairAPI(conflict=True)
        self.assertTrue(self.publish_pair(fixture(), api)["published"])
        trees = [data for _, path, data in api.calls if path == "/git/trees"]
        self.assertEqual([tree["base_tree"] for tree in trees], ["base0", "base1"])
        for tree in trees:
            self.assertEqual([row["path"] for row in tree["tree"]], ["investment-context.json", "investment-context-v2.json"])
        self.assertTrue(all(data["force"] is False for method, _, data in api.calls if method == "PATCH"))

    def test_bad_pair_never_contacts_remote_and_newer_source_blocks_rollback(self):
        wrong = project_v1(fixture())
        wrong["macro"]["score"] = 30
        api = PairAPI()
        with self.assertRaises(ValueError):
            self.publish_pair(fixture(), api, wrong)
        self.assertEqual(api.calls, [])
        previous = fixture()
        previous["macro"]["inputs"]["cpi"]["observation_date"] = "2026-11-01"
        resign(previous)
        api = PairAPI(previous)
        self.assertFalse(self.publish_pair(fixture(), api)["published"])
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_workflow_requires_both_validated_assets(self):
        workflow = Path(".github/workflows/investment-context.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count("--context .investment-output/investment-context-v2.json"), 2)
        self.assertIn("if: github.event_name != 'pull_request'", workflow)
        self.assertIn("tests.test_macro_context", workflow)


if __name__ == "__main__":
    unittest.main()
