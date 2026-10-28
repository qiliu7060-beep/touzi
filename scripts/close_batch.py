"""每日收盘流水线：冻结全市场、有限进程精算、聚合并原子发布公开分片。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

from touzi.close_snapshot import (MAX_CODES, clean, close_target, export_snapshot,
                                  json_bytes, public_entry, validate_close_time, write_snapshot)
from touzi.config import load_config
from touzi.market_calendar import TZ, trading_day
from touzi.diagnostics import DiagnosticRecorder, emit_diagnostic, read_diagnostics

DATA_ROOT = "https://raw.githubusercontent.com/qiliu7060-beep/touzi/close-data/"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(json_bytes(value))
    temp.replace(path)


def bound_http():
    """AkShare部分requests调用没有timeout，统一补上连接/读取边界。"""
    import requests
    import socket
    socket.setdefaulttimeout(20)
    original = requests.sessions.Session.request
    def request(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (5, 15)
        return original(self, *args, **kwargs)
    requests.sessions.Session.request = request


def config():
    cfg = load_config()
    cfg.setdefault("realtime", {})["enabled"] = True
    return cfg


def prior_manifest():
    request = urllib.request.Request(DATA_ROOT + "close-scores.json", headers={"User-Agent": "touzi-close-publisher"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("上次收盘清单超限")
        return json.loads(data)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def prepare(folder, batch_size=100, now=None, previous=None):
    import pandas as pd
    from touzi.screener import Screener, _merge_live_quotes
    from touzi.data.stock import fetch_realtime_quote
    from touzi.regime import build_macro_context
    from touzi.util import Cache

    # 固定now供确定性测试；真实运行在慢请求返回后重新读取时钟。
    fixed_now = now
    clock = lambda: fixed_now if fixed_now is not None else datetime.now(TZ)
    now = clock().astimezone(TZ)
    day = now.astimezone(TZ).date()
    # 定时重试只允许当天真正收盘之后，不盘中重算昨日评分。
    if trading_day(day) is not True or (now.hour, now.minute) < (15, 0):
        return {"run": False, "reason": "非核验交易日收盘时段"}
    previous = prior_manifest() if previous is None else previous
    if previous and previous.get("score_date") == day.isoformat():
        validate_close_time(previous["score_date"], previous["quote_at"], previous["built_at"], now)
        if previous.get("kind") == "touzi-close-v1" and previous.get("universe", {}).get("scored", 0) > 0:
            return {"run": False, "reason": "当天已有合格收盘评分"}
    bound_http()
    cfg = config()
    cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    screener = Screener(cfg, cache=cache, as_of=pd.Timestamp(now.replace(tzinfo=None)))
    screener.capture_live_inputs = False
    universe = screener.load_universe()
    # 清单必须为完整全市场底表，不能传入已经按市值截取的表。
    if universe.empty or len(universe) > MAX_CODES or universe["code"].duplicated().any():
        raise ValueError("全市场股票底表为空、重复或超限")
    eligible, rejected = screener.prefilter(universe)
    if eligible.empty:
        raise ValueError("没有通过粗筛的候选，不覆盖历史合格结果")
    codes = sorted(eligible["code"].astype(str))
    quotes = fetch_realtime_quote(codes, screener.proxy)
    quote_checked_at = clock().astimezone(TZ)
    frozen, quote_at, coverage = _merge_live_quotes(eligible, quotes)
    if quote_at is None or coverage < .9:
        raise ValueError("全市场候选完整收盘行情覆盖不足90%")
    # 每条都要求同一真实收盘日，失败的代码保留在候选集合并计入失败。
    accepted = {}
    if quotes is not None and len(quotes):
        for _, quote in quotes.iterrows():
            try:
                stamp = pd.Timestamp(quote["quote_ts"])
                stamp = stamp.tz_localize(TZ) if stamp.tzinfo is None else stamp.tz_convert(TZ)
                validate_close_time(day.isoformat(), stamp.isoformat(), quote_checked_at.isoformat(), quote_checked_at)
                if math.isfinite(float(quote["price"])) and float(quote["price"]) > 0:
                    accepted[str(quote["code"])] = stamp.isoformat()
            except (KeyError, TypeError, ValueError):
                continue
    if len(set(codes) & set(accepted)) / len(codes) < .9:
        raise ValueError("真实收盘日期/时间覆盖不足90%")
    timestamps = sorted(accepted[c] for c in codes if c in accepted)
    # 分片各条保留自己的原始时间；manifest使用本批最晚真实收盘时间。
    final_quote = timestamps[-1]
    macro = build_macro_context(cfg, cache=cache, proxy=screener.proxy, as_of=pd.Timestamp(now.replace(tzinfo=None)))
    if not math.isfinite(float(macro.macro_score)):
        raise ValueError("宏观背景分不可用，不发布伪完整收盘结果")
    # 所有批次共用截面底表、宏观、股息概览和债券基准，不按小批次重新排名。
    context = {"schema_version": 1, "score_date": day.isoformat(), "quote_at": final_quote,
               "prepared_at": clock().astimezone(TZ).isoformat(), "universe": universe.to_dict("records"),
               "eligible": frozen.to_dict("records"), "codes": codes,
               "quote_times": {c: accepted[c] for c in codes if c in accepted},
               "macro_score": float(macro.macro_score),
               "div_summary": screener._div_summary_map(), "bond": screener._bond_yield(),
               "market_total": len(universe), "eligible_total": len(codes), "excluded": len(rejected)}
    folder = Path(folder)
    atomic_json(folder / "context.json", context)
    matrix = [{"batch": i // batch_size, "codes": codes[i:i + batch_size]} for i in range(0, len(codes), batch_size)]
    atomic_json(folder / "batches.json", matrix)
    return {"run": True, "matrix": {"include": [{"batch": row["batch"]} for row in matrix]},
            "score_date": day.isoformat(), "eligible": len(codes)}


def worker(folder, code, output):
    diagnostic = DiagnosticRecorder(Path(output).with_suffix(".diagnostics.json"))
    started = time.monotonic()
    emit_diagnostic(diagnostic, "worker", "start")
    import pandas as pd
    from touzi.screener import Screener
    from touzi.util import Cache
    bound_http()
    context = json.loads((Path(folder) / "context.json").read_text(encoding="utf-8"))
    cfg = config()
    screener = Screener(cfg, cache=Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache"))),
                        as_of=pd.Timestamp(context["prepared_at"]).tz_convert(TZ).tz_localize(None))
    screener.capture_live_inputs = False
    screener.diagnostic = diagnostic
    screener._universe = pd.DataFrame(context["universe"])
    screener._div_summary = context["div_summary"]
    screener._bond = context["bond"]
    meta = next(row for row in context["eligible"] if row["code"] == code)
    if code not in context["quote_times"]:
        raise ValueError("该股票未返回有效完整收盘行情")
    raw = screener._fetch_one(code)
    statements = raw.get("statements") or {}
    has_statement = any(frame is not None and len(frame) > 0 for frame in statements.values())
    if not has_statement and all(raw.get(key) is None or len(raw[key]) == 0 for key in ("pe_hist", "pb_hist", "div", "fin")):
        raise ValueError("该股票必要明细全部不可用")
    score_started = time.monotonic()
    emit_diagnostic(diagnostic, "score", "start")
    score = screener.score_one(meta, raw)
    emit_diagnostic(diagnostic, "score", "ok", score_started)
    if not math.isfinite(float(score.total)):
        raise ValueError("未得到有效评分")
    export_started = time.monotonic()
    emit_diagnostic(diagnostic, "export", "start")
    entry = public_entry(score, cfg, context["macro_score"], context["quote_times"][code])
    atomic_json(output, entry)
    emit_diagnostic(diagnostic, "export", "ok", export_started)
    emit_diagnostic(diagnostic, "worker", "ok", started)


def score_batch(folder, batch, output, workers=6, stock_timeout=90):
    folder, output = Path(folder), Path(output)
    context = json.loads((folder / "context.json").read_text(encoding="utf-8"))
    batches = json.loads((folder / "batches.json").read_text(encoding="utf-8"))
    row = next(row for row in batches if row["batch"] == batch)
    entries, failures, diagnostics = {}, {}, {}
    def one(code):
        # 独立进程预算避免任何上游库永久阻塞线程退出；只终止本脚本创建的子进程。
        result_path = output / f"entry-{code}.json"
        # 先覆盖本轮起点，避免重试在worker启动前失败时误读上轮侧车。
        emit_diagnostic(DiagnosticRecorder(result_path.with_suffix(".diagnostics.json")), "worker", "start")
        try:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker", "--folder", str(folder),
                            "--code", code, "--output", str(result_path)],
                           check=True, timeout=stock_timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return code, json.loads(result_path.read_text(encoding="utf-8")), None
        except subprocess.TimeoutExpired:
            return code, None, "个股取数/评分超过进程预算"
        except (subprocess.CalledProcessError, OSError, ValueError):
            return code, None, "个股必要数据或评分不可用"
    output.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        for future in as_completed(pool.submit(one, code) for code in row["codes"]):
            code, entry, error = future.result()
            if error:
                failures[code] = error
            else:
                entries[code] = entry
            diagnostics[code] = read_diagnostics((output / f"entry-{code}.json").with_suffix(".diagnostics.json"))
            # 每完成一只就持久化，任务终止仍可诊断真实进度。
            atomic_json(output / "batch.json", {"batch": batch, "score_date": context["score_date"],
                        "expected": row["codes"], "entries": entries, "failures": failures,
                        "diagnostics": diagnostics})


def assemble(folder, results, output, now=None):
    folder = Path(folder)
    context = json.loads((folder / "context.json").read_text(encoding="utf-8"))
    batches = json.loads((folder / "batches.json").read_text(encoding="utf-8"))
    flat = [code for batch in batches for code in batch["codes"]]
    if len(flat) != len(set(flat)) or set(flat) != set(context["codes"]):
        raise ValueError("冻结批次与全市场候选集合不一致")
    found = {}
    for path in Path(results).rglob("batch.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item["batch"] in found:
            raise ValueError("批次结果重复")
        found[item["batch"]] = item
    if set(found) != {b["batch"] for b in batches}:
        raise ValueError("仍有批次没有完成，不覆盖上次收盘评分")
    entries, failed, seen = {}, {}, set()
    for batch in batches:
        result = found[batch["batch"]]
        actual = set(result["entries"]) | set(result["failures"])
        if (result["score_date"] != context["score_date"] or actual != set(batch["codes"])
                or set(result["entries"]) & set(result["failures"]) or actual & seen):
            raise ValueError("批次日期、代码或完成计数不一致")
        seen |= actual
        entries.update(result["entries"])
        failed.update(result["failures"])
    # 局部失败必须明确计数，覆盖过低则保持上一合格版。
    if len(entries) / len(context["codes"]) < .9:
        raise ValueError("全市场评分成功覆盖不足90%，保留上次合格版")
    counts = {k: context[k] for k in ("market_total", "eligible_total", "excluded")}
    counts.update(attempted=len(seen), scored=len(entries), failed=len(failed))
    manifest, files = export_snapshot(entries, config(), context["score_date"], context["quote_at"], counts,
                                      (now or datetime.now(TZ)).isoformat())
    write_snapshot(Path(output), files)
    atomic_json(Path(output) / "close-failures.json", {"score_date": context["score_date"], "failures": failed})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "worker", "score", "assemble"))
    parser.add_argument("--folder", default=".close-input")
    parser.add_argument("--output", default=".close-output")
    parser.add_argument("--results", default=".close-results")
    parser.add_argument("--batch", type=int, default=0)
    parser.add_argument("--code")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--stock-timeout", type=int, default=90)
    parser.add_argument("--github-output")
    args = parser.parse_args()
    if args.command == "prepare":
        value = prepare(args.folder)
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as stream:
                stream.write("run=" + str(value["run"]).lower() + "\n")
                stream.write("matrix=" + json.dumps(value.get("matrix", {"include": []})) + "\n")
        print(json.dumps(value, ensure_ascii=False))
    elif args.command == "worker":
        worker(args.folder, args.code, args.output)
    elif args.command == "score":
        score_batch(args.folder, args.batch, args.output, args.workers, args.stock_timeout)
    else:
        value = assemble(args.folder, args.results, args.output)
        print(json.dumps(value["universe"], ensure_ascii=False))


if __name__ == "__main__":
    main()
