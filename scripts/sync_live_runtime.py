"""同步独立发布仓库使用的 Worker 运行时副本；--check 只读检查。"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = ("live-scoring.mts", "live-model-validation.mts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="检查副本一致性，不写入文件")
    args = parser.parse_args()
    errors = []
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
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Live runtime copies match Worker sources" if args.check else "Live runtime copies synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
