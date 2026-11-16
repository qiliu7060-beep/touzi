"""仅原子推进公开投资环境文件，CAS冲突后重新基于最新树构建。"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from touzi.market_calendar import TZ
from touzi.macro_context import validate_v1 as validate, validate_context, project_v1, input_rollback

REPO = "qiliu7060-beep/touzi"
FILENAME = "investment-context.json"
MAX_BYTES = 65536


def _stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须带时区")
    return result.astimezone(TZ)


def publish(file, token, request=None, now=None, context_file=None):
    raw = Path(file).read_bytes()
    value = validate(raw, now)
    complete_raw = Path(context_file).read_bytes() if context_file is not None else None
    complete = validate_context(complete_raw, now) if complete_raw is not None else None
    if complete is not None and project_v1(complete) != value:
        raise ValueError("完整宏观版本与v1投影不一致")
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
                if value == old and complete is None:
                    return {"published": False, "reason": "相同投资环境，无需重复发布"}
            if complete is not None:
                old_complete = api("GET", "/contents/investment-context-v2.json?ref=" + parent, missing=True)
                if old_complete:
                    old_context = validate_context(base64.b64decode(old_complete["content"]), now, freshness=False)
                    if input_rollback(complete, old_context) or _stamp(complete["built_at"]) < _stamp(old_context["built_at"]):
                        return {"published": False, "reason": "已有更新的逐源宏观版本"}
                    if complete == old_context and previous and value == old:
                        return {"published": False, "reason": "相同完整宏观环境，无需重复发布"}
            assets = [(FILENAME, raw)]
            if complete_raw is not None:
                assets.append(("investment-context-v2.json", complete_raw))
            entries = []
            for name, content in assets:
                blob = api("POST", "/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
                entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            # 两文件同树同parent一次晋升；分支中的收盘资产由base_tree原样保留。
            tree = api("POST", "/git/trees", {"base_tree": base, "tree": entries})
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
    parser.add_argument("--context", help="同轮完整v2环境；提供时两个资产必须一起验证/发布")
    args = parser.parse_args()
    if args.validate_only:
        result = validate(Path(args.file).read_bytes())
        if args.context:
            complete = validate_context(Path(args.context).read_bytes())
            if project_v1(complete) != result:
                raise ValueError("完整宏观版本与v1投影不一致")
    else:
        result = publish(args.file, os.environ.get("GH_TOKEN", ""), context_file=args.context)
    print(json.dumps({"validated": True, "as_of": result["as_of"]} if args.validate_only else result, ensure_ascii=False))


if __name__ == "__main__":
    main()
