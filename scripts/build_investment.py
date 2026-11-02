"""轻量刷新宏观与国债环境，不重新计算全市场股票。"""
import sys, json, argparse
from pathlib import Path
from datetime import datetime
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "vendor"), str(ROOT)]
from touzi.config import load_config
from touzi.util import Cache
from touzi.regime import build_macro_context
from touzi.data.macro_cn import fetch_cn_bond_10y
from touzi.investment import bond_signal
from touzi.market_calendar import TZ, CALENDAR


def build(now=None):
    now = now or datetime.now(TZ)
    cfg = load_config()
    cache = Cache(cfg.resolve(cfg.general["cache_dir"]))
    # 必要输入抓取失败即停止发布；缓存观测日期由消费者再次核验。
    macro = build_macro_context(cfg, cache=cache, as_of=now.date()).as_dict()
    if macro["unavailable"] or macro["position"] is None or macro["macro_score"] is None:
        raise ValueError("宏观必要模块缺失，不发布假成功")
    bond = bond_signal(fetch_cn_bond_10y(cache, cfg.general.get("proxy") or None, ttl_days=0), now, cfg.investment)
    return {"schema_version": 1, "kind": "touzi-investment-v1", "rule_version": "yield-trend-v1",
            "as_of": str(now.date()), "built_at": now.isoformat(), "trading_calendar": CALENDAR,
            "macro": {"score": macro["macro_score"], "position": macro["position"], "computed_at": now.isoformat(), "status": "ok"},
            "bond": bond, "errors": []}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".investment-output/investment-context.json")
    args = parser.parse_args()
    result = build()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
