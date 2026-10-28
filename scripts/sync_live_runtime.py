"""同步独立发布仓库使用的 Worker 运行时副本；--check 只读检查。"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = ("live-scoring.mts", "live-model-validation.mts", "market-calendar.mjs", "financial-coverage.mjs", "pick-financial.mjs", "close-browser.mjs", "close-score-validation.mts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="检查副本一致性，不写入文件")
    args = parser.parse_args()
    errors = []
    calendar = json.loads((ROOT / "config/trading-calendar.json").read_text(encoding="utf-8"))
    calendar_source = ROOT / "webapp/lib/market-calendar.mjs"
    raw = calendar_source.read_text(encoding="utf-8")
    generated = re.sub(r"export const MARKET_CALENDAR = .*?;", "export const MARKET_CALENDAR = " + json.dumps(calendar, ensure_ascii=False, separators=(",", ":")) + ";", raw, count=1)
    if args.check:
        if raw != generated:
            errors.append("Calendar constant differs from verified config")
    else:
        calendar_source.write_text(generated, encoding="utf-8", newline="\n")
    snippet = generated.replace("export ", "") + "\n" + (ROOT / "webapp/lib/financial-coverage.mjs").read_text(encoding="utf-8").replace("export ", "")
    snippet += "\n" + (ROOT / "webapp/lib/pick-financial.mjs").read_text(encoding="utf-8").replace("export ", "")
    snippet += "\n" + (ROOT / "webapp/lib/close-browser.mjs").read_text(encoding="utf-8").replace("export ", "")
    block = "  // MARKET_CALENDAR_START (generated)\n" + snippet + "  // MARKET_CALENDAR_END\n"
    for relative in ("scripts/static/app.js", "webapp/public/legacy/app.js"):
        path = ROOT / relative
        source = path.read_text(encoding="utf-8")
        if "// MARKET_CALENDAR_START" in source:
            updated = re.sub(r"  // MARKET_CALENDAR_START.*?  // MARKET_CALENDAR_END\n", lambda _: block, source, count=1, flags=re.S)
        else:
            updated = source.replace("  'use strict';\n", "  'use strict';\n" + block, 1)
        if args.check:
            if source != updated:
                errors.append(f"Browser calendar differs: {relative}")
        else:
            path.write_text(updated, encoding="utf-8", newline="\n")
    for name in FILES:
        source = ROOT / "webapp" / "lib" / name
        target = ROOT / "scripts" / name
        if not source.is_file():
            errors.append(f"Worker source unavailable: {source}")
            continue
        # UTF-8 文本规范化换行，避免 Windows checkout 的 CRLF 导致假差异。
        content = source.read_text(encoding="utf-8")
        if args.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                errors.append(f"Runtime copy differs: {name}; run python scripts/sync_live_runtime.py")
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
    for source_name, target_name, module_name in (
        ("test-market-calendar.mjs", "test_market_calendar.mjs", "market-calendar.mjs"),
        ("test-financial-coverage.mjs", "test_financial_coverage.mjs", "financial-coverage.mjs"),
    ):
        source_test = ROOT / "webapp/scripts" / source_name
        target_test = ROOT / "tests" / target_name
        test_text = source_test.read_text(encoding="utf-8").replace(f"../lib/{module_name}", f"../scripts/{module_name}")
        if args.check:
            if not target_test.is_file() or target_test.read_text(encoding="utf-8") != test_text:
                errors.append(f"Public contract test mirror differs: {target_name}")
        else:
            target_test.write_text(test_text, encoding="utf-8", newline="\n")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Live runtime copies match Worker sources" if args.check else "Live runtime copies synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
