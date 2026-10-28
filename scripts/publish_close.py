"""把已验证的收盘分片一次性提交至独立数据分支；失败不移动现有分支。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from touzi.close_snapshot import validate_close_time, validate_counts

REPOSITORY = "qiliu7060-beep/touzi"
BRANCH = "close-data"


def validate_folder(folder):
    root = Path(folder).resolve()
    raw = (root / "close-scores.json").read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("清单超限")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "touzi-close-v1":
        raise ValueError("清单版本错误")
    validate_close_time(manifest["score_date"], manifest["quote_at"], manifest["built_at"])
    validate_counts(manifest["universe"], len(manifest["index"]))
    files, codes = {"close-scores.json": raw}, set()
    by_code = {item["code"]: item for item in manifest["index"]}
    if len(by_code) != len(manifest["index"]):
        raise ValueError("索引代码重复")
    for shard in manifest["shards"]:
        name = shard["path"]
        if not re.fullmatch(r"close-score-parts/\d{4}-\d{2}-\d{2}-[a-f0-9]{16}-\d{3}\.json", name):
            raise ValueError("分片路径非法")
        data = (root / name).read_bytes()
        if len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != shard["sha256"]:
            raise ValueError("分片大小或哈希不符")
        part = json.loads(data)
        entries = part["entries"]
        if (part.get("schema_version") != 1 or part.get("kind") != "touzi-close-part-v1"
                or part.get("score_date") != manifest["score_date"]
                or len(entries) > 100 or not entries or set(entries) != set(shard["codes"])
                or len(set(shard["codes"])) != len(shard["codes"]) or set(entries) & codes):
            raise ValueError("分片日期或代码覆盖不一致")
        for code, entry in entries.items():
            item = by_code.get(code, {})
            if item.get("shard") != name or entry.get("code") != code or any(entry.get(k) != item.get(k) for k in ("name", "action", "total")):
                raise ValueError("分片与索引不一致")
            if any(key in entry for key in ("holding", "holdings", "owner", "user_id", "cost", "shares")):
                raise ValueError("公开快照不得包含私人台账")
        codes.update(entries)
        files[name] = data
    if codes != set(by_code):
        raise ValueError("索引覆盖不完整")
    return manifest, files


def publish(folder, token, request=None):
    manifest, files = validate_folder(folder)
    if not token:
        raise ValueError("缺少流水线仓库写入凭据")
    def api(method, path, value=None, missing=False):
        payload = json.dumps(value).encode() if value is not None else None
        req = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + path,
              data=payload, method=method, headers={"Authorization": "Bearer " + token,
              "Accept": "application/vnd.github+json", "User-Agent": "touzi-close-publisher"})
        try:
            with (request or urllib.request.urlopen)(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            # 不输出请求头、token或原始远端报文。
            raise RuntimeError(f"数据分支发布失败：HTTP {exc.code}") from None
    old = api("GET", "/git/ref/heads/" + BRANCH, missing=True)
    parent = old["object"]["sha"] if old else None
    base = api("GET", "/git/commits/" + parent)["tree"]["sha"] if parent else None
    if old:
        previous = api("GET", "/contents/close-scores.json?ref=" + BRANCH, missing=True)
        if previous:
            previous = json.loads(base64.b64decode(previous["content"]))
            # 不能回退交易日，也不能第二次覆盖同日成功快照。
            if previous.get("score_date", "") >= manifest["score_date"]:
                return {"published": False, "reason": "该交易日或更新日期已有合格评分"}
    elements = []
    for name, data in files.items():
        blob = api("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        elements.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree_data = {"tree": elements}
    if base:
        tree_data["base_tree"] = base
    tree = api("POST", "/git/trees", tree_data)
    commit = api("POST", "/git/commits", {"message": "Publish close scores " + manifest["score_date"],
                 "tree": tree["sha"], "parents": [parent] if parent else []})
    if parent:
        api("PATCH", "/git/refs/heads/" + BRANCH, {"sha": commit["sha"], "force": False})
    else:
        api("POST", "/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": commit["sha"]})
    return {"published": True, "sha": commit["sha"], "score_date": manifest["score_date"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default=".close-output")
    parser.add_argument("--validate-only", action="store_true")
    options = parser.parse_args()
    if options.validate_only:
        data, _ = validate_folder(options.folder)
        print(json.dumps(data["universe"], ensure_ascii=False))
    else:
        print(json.dumps(publish(options.folder, os.environ.get("GH_TOKEN", "")), ensure_ascii=False))
