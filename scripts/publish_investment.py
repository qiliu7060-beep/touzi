"""仅原子推进公开投资环境文件，CAS冲突后重新基于最新树构建。"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from touzi.market_calendar import CALENDAR, TZ, trading_day, adjacent_day

REPO = "qiliu7060-beep/touzi"
FILENAME = "investment-context.json"
MAX_BYTES = 65536


def _stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须带时区")
    return result.astimezone(TZ)


def validate(raw, now=None, freshness=True):
    now = now or datetime.now(TZ)
    if len(raw) > MAX_BYTES:
        raise ValueError("投资环境大小超限")
    value = json.loads(raw)
    allowed = {"schema_version", "kind", "rule_version", "as_of", "built_at", "trading_calendar", "macro", "bond", "errors"}
    if not isinstance(value, dict) or set(value) != allowed or value["schema_version"] != 1 or value["kind"] != "touzi-investment-v1" or value["rule_version"] != "yield-trend-v1":
        raise ValueError("投资环境格式错误")
    if value["trading_calendar"] != CALENDAR or value["errors"] != []:
        raise ValueError("日历未核验或生产者报告错误")
    macro, bond = value["macro"], value["bond"]
    if not isinstance(macro, dict) or set(macro) != {"score", "position", "computed_at", "status"} or macro["status"] != "ok":
        raise ValueError("宏观环境未核验")
    if not isinstance(bond, dict) or set(bond) != {"yield_pct", "observation_date", "fetched_at", "delta20_bp", "delta60_bp", "status", "factor", "reasons"} or bond["status"] != "ok":
        raise ValueError("国债环境未核验")
    def finite(number):
        return type(number) in (int, float) and math.isfinite(number)
    if not all(finite(x) for x in (macro["score"], macro["position"], bond["yield_pct"], bond["delta20_bp"], bond["delta60_bp"], bond["factor"])) or not 0 <= macro["score"] <= 100 or not 0 <= macro["position"] <= 1 or not 0 <= bond["yield_pct"] <= 100 or bond["factor"] not in (0, .5, .75, 1):
        raise ValueError("环境数值越界")
    if not isinstance(bond["reasons"], list) or not bond["reasons"] or not all(isinstance(x, str) and len(x) <= 500 for x in bond["reasons"]):
        raise ValueError("国债判断原因缺失")
    built, computed, fetched = map(_stamp, (value["built_at"], macro["computed_at"], bond["fetched_at"]))
    observed = datetime.strptime(bond["observation_date"], "%Y-%m-%d").date()
    as_of = datetime.strptime(value["as_of"], "%Y-%m-%d").date()
    if as_of != built.date() or max(built, computed, fetched) > now + timedelta(minutes=1) or computed > built or fetched > built or observed > fetched.date() or trading_day(observed) is not True:
        raise ValueError("环境时间不可信")
    if freshness:
        latest = now.date() if trading_day(now.date()) is True and now.hour >= 15 else adjacent_day(now.date(), -1)
        if trading_day(now.date()) is None or latest is None:
            raise ValueError("当前交易日历未知")
        oldest = adjacent_day(adjacent_day(latest, -1), -1)
        if oldest is None or min(computed.date(), fetched.date(), observed) < oldest:
            raise ValueError("投资环境过期，拒绝晋升")
    return value


def publish(file, token, request=None, now=None):
    raw = Path(file).read_bytes()
    value = validate(raw, now)
    if not token:
        raise ValueError("缺少流水线凭据")
    def api(method, path, data=None, missing=False):
        req = urllib.request.Request("https://api.github.com/repos/" + REPO + path,
            data=json.dumps(data).encode() if data is not None else None, method=method,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "touzi-investment-publisher"})
        try:
            with (request or urllib.request.urlopen)(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            raise
    for attempt in range(3):
        try:
            parent = api("GET", "/git/ref/heads/close-data")["object"]["sha"]
            base = api("GET", "/git/commits/" + parent)["tree"]["sha"]
            # 用不可变提交读取前版，确保回退检查与新树基线属于同一版本。
            previous = api("GET", "/contents/" + FILENAME + "?ref=" + parent, missing=True)
            if previous:
                old = validate(base64.b64decode(previous["content"]), now, freshness=False)
                if any(_stamp(value[key]) < _stamp(old[key]) for key in ("built_at",)) or value["bond"]["observation_date"] < old["bond"]["observation_date"] or _stamp(value["macro"]["computed_at"]) < _stamp(old["macro"]["computed_at"]) or _stamp(value["bond"]["fetched_at"]) < _stamp(old["bond"]["fetched_at"]):
                    return {"published": False, "reason": "已有更新的投资环境"}
                if value == old:
                    return {"published": False, "reason": "相同投资环境，无需重复发布"}
            blob = api("POST", "/git/blobs", {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
            tree = api("POST", "/git/trees", {"base_tree": base, "tree": [{"path": FILENAME, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
            commit = api("POST", "/git/commits", {"message": "Refresh investment context", "tree": tree["sha"], "parents": [parent]})
            try:
                api("PATCH", "/git/refs/heads/close-data", {"sha": commit["sha"], "force": False})
                return {"published": True, "as_of": value["as_of"]}
            except urllib.error.HTTPError as exc:
                if exc.code not in (409, 422) or attempt == 2:
                    raise
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"投资环境发布失败：HTTP {exc.code}；现有数据保持不变") from None
    raise RuntimeError("数据分支并发更新，请重试")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", default=".investment-output/investment-context.json")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    result = validate(Path(args.file).read_bytes()) if args.validate_only else publish(args.file, os.environ.get("GH_TOKEN", ""))
    print(json.dumps({"validated": True, "as_of": result["as_of"]} if args.validate_only else result, ensure_ascii=False))


if __name__ == "__main__":
    main()
