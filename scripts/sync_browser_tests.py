"""将框架无关浏览器回归从 Sites 源同步到独立 Python 仓库；--check 只读核对。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("browser-test-runtime.mjs", "browser-test-sync.mjs", "test-holdings-actions.mjs", "test-financial-ui.mjs", "test-pick-financial.mjs", "test-close-browser.mjs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source_dir = ROOT / "webapp/scripts"
    target_dir = ROOT / "tests/browser"
    if not source_dir.is_dir():
        parser.error("同步需完整工作区；独立 Python 仓库直接运行 node scripts/check-browser-release.mjs")
    errors = []
    manifest = {}
    for name in FILES:
        source = source_dir / name
        target = target_dir / name
        content = source.read_text(encoding="utf-8")
        manifest[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if args.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                errors.append(name)
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    for directory in (source_dir, target_dir):
        path = directory / "browser-test-manifest.json"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != manifest_text:
                errors.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(manifest_text, encoding="utf-8", newline="\n")
    if errors:
        print("Browser regression copies differ: " + ", ".join(errors))
        return 1
    print("Browser regression copies match" if args.check else "Browser regression copies synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
