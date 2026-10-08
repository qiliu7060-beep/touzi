"""端到端跑一遍：宏观择时 → 个股打分 → 回测 → 报告与看板。

最小可用：
    set PYTHONIOENCODING=utf-8
    F:\\ana\\python.exe scripts\\run_all.py --limit 60          # 只算 60 只，先看通路
全量：
    F:\\ana\\python.exe scripts\\run_all.py --limit 600 --backtest-limit 400 --quarters 16

实时模式（默认关闭）：
    F:\\ana\\python.exe scripts\\run_all.py --live --limit 60   # 每次都重抓行情，决策时点=今天
    F:\\ana\\python.exe scripts\\run_all.py --live --watch 300  # 每 5 分钟重算一次，看板自动刷新

产出（全部落在工作区的 output/ 下）：
    output/dashboard.html          单文件看板，离线可开
    output/report.md               同内容的 Markdown 报告
    output/scores.csv / .xlsx      个股打分明细（含逐项理由）
    output/macro_context.json      宏观背景分与目标仓位
    output/backtest_*.csv/json     两个回测的结果
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from touzi.config import load_config  # noqa: E402
from touzi.util import Cache, is_live, resolve_ttls, setup_logging  # noqa: E402


def _fmt_seconds(s: float) -> str:
    return f"{s / 60:.1f} 分钟" if s >= 60 else f"{s:.1f} 秒"


def _load_backtest(cfg, out_dir: Path) -> tuple[dict | None, dict | None]:
    """复用磁盘上的回测结果（--reuse-backtest）。"""
    saved = json.loads((out_dir / "backtest_metrics.json").read_text(encoding="utf-8"))
    macro_bt = {"summary": saved.get("macro", {}).get("summary"),
                "tables": saved.get("macro", {}).get("tables", {})}
    port_bt = None
    if (out_dir / "backtest_portfolio.csv").exists():
        port_bt = {"periods": pd.read_csv(out_dir / "backtest_portfolio.csv"),
                   "metrics": saved.get("portfolio", {}),
                   "top_n": saved.get("portfolio", {}).get("top_n"),
                   "cost_rate": cfg.backtest.get("cost_rate", 0.0015),
                   "start": saved.get("portfolio", {}).get("start")}
    return macro_bt, port_bt


def _load_stock_backtest(cfg, out_dir: Path) -> dict | None:
    """复用磁盘上的回测C（个股逐笔）结果。"""
    tpath = out_dir / "backtest_stocks_trades.csv"
    spath = out_dir / "backtest_stocks_stats.csv"
    if not tpath.exists():
        return None
    saved = {}
    mpath = out_dir / "backtest_metrics.json"
    if mpath.exists():
        try:
            saved = (json.loads(mpath.read_text(encoding="utf-8")) or {}).get("stocks") or {}
        except Exception:  # noqa: BLE001
            saved = {}
    return {
        "trades": pd.read_csv(tpath, dtype={"代码": str}),
        "stocks": pd.read_csv(spath, dtype={"代码": str}) if spath.exists() else pd.DataFrame(),
        "metrics": saved,
    }


def run_once(cfg, args, cache: Cache, proxy: str | None, out_dir: Path) -> dict:
    """跑一轮完整流程，返回本轮的关键产出（供 --watch 循环复用）。"""
    from touzi import report as R
    from touzi.backtest import backtest_portfolio, backtest_stocks, validate_macro_rules
    from touzi.regime import build_macro_context, data_freshness
    from touzi.screener import Screener
    from touzi.tracker import Tracker, ensure_holdings_template

    t0 = time.time()
    as_of = pd.Timestamp(args.as_of) if args.as_of else None
    if is_live(cfg) and not args.as_of:
        # 实时模式下决策时点必须跟着「现在」走，不能用缓存里的旧日期
        as_of = pd.Timestamp.today().normalize()

    # ---------------- 1. 宏观背景分 ----------------
    macro = None
    if not args.skip_macro:
        print("[1/5] 计算宏观背景分与目标仓位 …")
        macro = build_macro_context(cfg, cache=cache, proxy=proxy, as_of=as_of)
        (out_dir / "macro_context.json").write_text(
            json.dumps(macro.as_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        as_of = macro.as_of
        print(f"      宏观背景分 {macro.macro_score:.1f} → 目标仓位 {macro.position * 100:.0f}%"
              f"（{macro.regime}）")
        for k, r in macro.results.items():
            print(f"      · {k:4s} {r.score:6.1f}  {r.state}")
        if macro.unavailable:
            print(f"      缺失模块：{macro.unavailable}")

    # ---------------- 2. 个股打分 ----------------
    print(f"[2/5] 个股打分（候选上限 {args.limit}，{args.workers} 线程）…")
    screener = Screener(cfg, cache=cache, proxy=proxy, as_of=as_of)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    scores = screener.run(limit=args.limit, codes=codes, workers=args.workers)
    if len(scores):
        scores.to_csv(out_dir / "scores.csv", index=False, encoding="utf-8-sig")
        try:
            with pd.ExcelWriter(out_dir / "scores.xlsx", engine="openpyxl") as xw:
                scores.to_excel(xw, sheet_name="打分", index=False)
                if macro is not None:
                    macro.to_frame().to_excel(xw, sheet_name="宏观背景", index=False)
        except Exception as exc:  # noqa: BLE001
            print(f"      写 Excel 失败（不影响其它产出）：{exc}")
    print(f"      完成 {len(scores)} 只；前 5 名："
          + ", ".join(f"{r['名称']}({r['总分']:.1f})" for _, r in scores.head(5).iterrows())
          if len(scores) else "      无结果")

    explain_map = {s.code: s.explain_text() for s in screener.results}

    # ---------------- 3. 个股买卖决策与跟踪 ----------------
    print("[3/5] 按 [plan] 的买卖规则生成跟踪表 …")
    track = None
    try:
        hp = ensure_holdings_template(cfg)
        tracker = Tracker(cfg, macro_score=(macro.macro_score if macro is not None else float("nan")))
        track = tracker.build(screener.results, top=None)
        tpath = tracker.save(track)
        counts = track["建议动作"].value_counts().to_dict()
        print(f"      持仓台账 {hp}（{len(tracker.holdings)} 条）")
        print("      动作分布：" + "、".join(f"{k} {v}" for k, v in counts.items()))
        if len(track):
            buy_now = track[track["建议动作"].isin(["建仓", "接近建仓"])]
            if len(buy_now):
                print("      可操作名单：" + "、".join(
                    f"{r['名称']}({r['建议动作']})" for _, r in buy_now.head(10).iterrows()))
        print(f"      跟踪表：{tpath}")
    except Exception as exc:  # noqa: BLE001
        print(f"      跟踪表生成失败（不影响其它产出）：{type(exc).__name__}: {exc}")

    # ---------------- 3b. 我的持仓（录入 → 盈亏 → 卖出提醒）----------------
    my_port = None
    try:
        from touzi.portfolio import Portfolio

        pf = Portfolio(cfg)
        my_port = pf.build(tracking=track)
        ppath = pf.save(my_port)
        s = my_port.attrs.get("summary", {}) or {}
        if len(my_port):
            print(f"[3b] 我的持仓：{s.get('持仓只数', 0)} 只，"
                  f"总成本(含费) ¥{s.get('总成本(含费)')}，总市值 ¥{s.get('总市值')}，"
                  f"浮动盈亏 ¥{s.get('总浮动盈亏')}（{s.get('总浮动盈亏%')}%）；"
                  f"需要动手 {s.get('需要动手', 0)} 只、留意 {s.get('留意', 0)} 只")
            act = my_port[my_port["_level"] == "act"] if "_level" in my_port.columns else my_port.iloc[0:0]
            for _, r in act.iterrows():
                print(f"      📣 {r.get('代码')} {r.get('名称')}：{r.get('提醒')}")
        else:
            print("[3b] 我的持仓：台账为空（config/holdings.csv 只有表头），"
                  "在网页点「我的持仓」录入后重跑即可")
        print(f"      持仓表：{ppath}")
    except Exception as exc:  # noqa: BLE001
        print(f"      持仓表生成失败（不影响其它产出）：{type(exc).__name__}: {exc}")

    # ---------------- 4. 回测 ----------------
    macro_bt = port_bt = stock_bt = None
    if args.reuse_backtest and (out_dir / "backtest_metrics.json").exists():
        print("[4/5] 复用已有回测结果 output/backtest_metrics.json")
        macro_bt, port_bt = _load_backtest(cfg, out_dir)
        if not args.skip_stocks_backtest:
            stock_bt = _load_stock_backtest(cfg, out_dir)
    elif not args.skip_backtest:
        print("[4/5] 回测A（宏观规则）…")
        macro_bt = validate_macro_rules(cfg, cache=cache, proxy=proxy, end=as_of)
        print(f"      {macro_bt.get('summary')}")

        if not args.skip_portfolio:
            print(f"      回测B（组合，候选上限 {args.backtest_limit}，{args.quarters} 期）"
                  "—— 每期都要重打分，最慢的一步 …")
            port_bt = backtest_portfolio(cfg, cache=cache, proxy=proxy,
                                         limit=args.backtest_limit, quarters=args.quarters,
                                         workers=args.workers)
            periods = port_bt.get("periods")
            if periods is not None and len(periods):
                periods.to_csv(out_dir / "backtest_portfolio.csv", index=False, encoding="utf-8-sig")
            print("      " + json.dumps(port_bt.get("metrics", {}), ensure_ascii=False))

        if not args.skip_stocks_backtest:
            mth = int(args.stocks_months or cfg.get_path("backtest.stocks_months", 48) or 48)
            print(f"      回测C（个股，候选上限 {args.backtest_limit}，最近 {mth} 个月）"
                  "—— 逐月对每只股票重打分，最慢的一步 …")
            try:
                stock_bt = backtest_stocks(cfg, cache=cache, proxy=proxy,
                                           limit=args.backtest_limit, months=mth,
                                           workers=args.workers, screener=screener)
                trades = stock_bt.get("trades")
                stats = stock_bt.get("stocks")
                if trades is not None and len(trades):
                    trades.to_csv(out_dir / "backtest_stocks_trades.csv",
                                  index=False, encoding="utf-8-sig")
                if stats is not None and len(stats):
                    stats.to_csv(out_dir / "backtest_stocks_stats.csv",
                                 index=False, encoding="utf-8-sig")
                print("      " + json.dumps(stock_bt.get("metrics", {}).get("汇总", {}),
                                            ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                print(f"      回测C 失败（不影响其它产出）：{type(exc).__name__}: {exc}")
                stock_bt = None

        if macro_bt:
            macro_bt["table"].to_csv(out_dir / "backtest_macro_rules.csv",
                                     index=False, encoding="utf-8-sig")
            macro_bt["detail"].to_csv(out_dir / "backtest_macro_detail.csv", encoding="utf-8-sig")

        def _records(v):
            # validate_macro_rules 的 tables 既可能是 DataFrame，也可能已经是 list[dict]
            return v.to_dict("records") if hasattr(v, "to_dict") else list(v)

        # metrics 本身不含 top_n/start/cost_rate（它们是 backtest_portfolio 的兄弟键），
        # 这里合并进去，复用路径才能原样还原「每期持仓数」等信息
        port_meta = dict((port_bt or {}).get("metrics", {}) or {})
        for k in ("top_n", "start", "cost_rate"):
            if port_meta.get(k) is None:
                port_meta[k] = (port_bt or {}).get(k)

        (out_dir / "backtest_metrics.json").write_text(
            json.dumps({"macro": {"summary": (macro_bt or {}).get("summary"),
                                  "tables": {k: _records(v)
                                             for k, v in ((macro_bt or {}).get("tables") or {}).items()}},
                        "portfolio": port_meta,
                        "stocks": (stock_bt or {}).get("metrics")},
                       ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    else:
        print("[4/5] 跳过回测")

    # 若 tables 的值是 list[dict]（从磁盘复用时如此），还原成 DataFrame 供渲染
    if macro_bt and any(isinstance(v, list) for v in (macro_bt.get("tables") or {}).values()):
        macro_bt["tables"] = {k: (pd.DataFrame(v) if isinstance(v, list) else v)
                              for k, v in macro_bt["tables"].items()}

    # ---------------- 5. 报告与看板 ----------------
    print("[5/5] 生成报告与看板 …")
    from touzi.signals.policy import load_industry_policy, load_policy_events
    try:
        ind = load_industry_policy(cfg)
    except Exception:  # noqa: BLE001
        ind = None
    try:
        ev = load_policy_events(cfg)
    except Exception:  # noqa: BLE001
        ev = None

    try:
        fresh = data_freshness(cfg, cache, macro=macro,
                               quotes_at=getattr(screener, "quotes_at", None))
    except Exception as exc:  # noqa: BLE001
        print(f"      数据新鲜度体检失败（不影响其它产出）：{exc}")
        fresh = None

    top = args.top or int((cfg.get_path("report", {}) or {}).get("top_display", 50) or 50)
    flags: dict = {}
    if macro is not None:
        flags["staleness"] = macro.staleness
        flags["unavailable"] = macro.unavailable
    if len(scores):
        flags["scored"] = int(len(scores))
        flags["as_of"] = str(pd.Timestamp(as_of).date()) if as_of is not None else None
    flags["live"] = is_live(cfg)

    dash = R.build_dashboard(
        cfg, out_dir / (cfg.get_path("report.dashboard_file", "dashboard.html") or "dashboard.html"),
        macro=macro, scores=scores, macro_bt=macro_bt, port_bt=port_bt,
        policy_events=ev, industry_policy=ind, explain_map=explain_map, flags=flags,
        freshness=fresh, track=track, stock_bt=stock_bt, my_port=my_port,
    )
    md = R.build_markdown(
        cfg, out_dir / (cfg.get_path("report.report_file", "report.md") or "report.md"),
        macro=macro, scores=scores, macro_bt=macro_bt, port_bt=port_bt, flags=flags, top=top,
        freshness=fresh, track=track, stock_bt=stock_bt, my_port=my_port,
    )
    elapsed = time.time() - t0
    print(f"      看板：{dash}")
    print(f"      报告：{md}")
    print(f"总耗时 {_fmt_seconds(elapsed)}")
    # 返回值刻意给全：serve.py 的网站各页都要用同一批对象渲染，
    # 否则网站就得自己再读一遍 CSV/JSON，出现「第二套口径」。
    return {
        "macro": macro,
        "scores": scores,
        "dash": dash,
        "md": md,
        "my_port": my_port,
        "track": track,
        "freshness": fresh,
        "macro_bt": macro_bt,
        "port_bt": port_bt,
        "stock_bt": stock_bt,
        "policy_events": ev,
        "industry_policy": ind,
        "explain_map": explain_map,
        "flags": flags,
        "cfg": cfg,
        "elapsed": elapsed,
    }


def build_parser() -> argparse.ArgumentParser:
    """命令行参数定义。

    ⚠️ 这里是**唯一**的定义处。serve.py 也用这个 parser 生成默认参数，
    所以新增参数只需要改这一处——历史上 serve.py 曾把参数表抄了一份，
    run_all 加了 `--skip-stocks-backtest` 之后它就报
    `AttributeError: 'Namespace' object has no attribute ...` 而整站构建失败。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "settings.toml"))
    ap.add_argument("--limit", type=int, default=300, help="打分候选股数量上限（按流通市值降序）")
    ap.add_argument("--top", type=int, default=None, help="看板/报告展示条数，默认读配置")
    ap.add_argument("--codes", default="", help="只算指定代码，逗号分隔（如 600519,000651）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--as-of", default="", help="决策时点 YYYY-MM-DD，默认用配置/今天")
    ap.add_argument("--backtest-limit", type=int, default=300)
    ap.add_argument("--quarters", type=int, default=12)
    ap.add_argument("--skip-backtest", action="store_true")
    ap.add_argument("--skip-stocks-backtest", action="store_true",
                    help="跳过回测C（个股逐笔买卖），它逐月重打分，最慢")
    ap.add_argument("--stocks-months", type=int, default=0,
                    help="回测C 回看多少个月，默认读配置 backtest.stocks_months")
    ap.add_argument("--skip-macro", action="store_true")
    ap.add_argument("--skip-portfolio", action="store_true")
    ap.add_argument("--reuse-backtest", action="store_true",
                    help="复用 output/backtest_metrics.json，不重跑回测")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="实时模式：行情缓存压到分钟级，决策时点=今天")
    mode.add_argument("--offline", action="store_true",
                      help="离线模式：完全复用缓存（覆盖配置里的 realtime.enabled）")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                    help="每隔 N 秒重跑一轮（配合 --live 即为盘中刷新；0=只跑一次）")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    setup_logging()
    # --watch 通常把输出重定向到文件，默认块缓冲会让人以为脚本卡死了；改行缓冲
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:  # noqa: BLE001
            pass
    cfg = load_config(args.config)
    if args.live:
        cfg["general"]["live"] = True
    elif args.offline:
        cfg["general"]["live"] = False

    live = is_live(cfg)
    ttls = resolve_ttls(cfg)
    cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    out_dir = cfg.resolve(cfg.general.get("output_dir", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    proxy = cfg.general.get("proxy") or None

    print("=" * 68)
    print(f"模式：{'实时（live）' if live else '离线（offline）'}　"
          f"决策时点会取：{args.as_of or ('今天' if live else '今天（但数据多为缓存）')}")
    print("缓存刷新周期：" + "　".join(f"{k}={v * 1440:.0f}分钟" if v < 1 else f"{k}={v:g}天"
                                        for k, v in ttls.items()))
    if live:
        print("提示：宏观（PMI/CPI/PPI/GDP）是月度/季度公布数据，实时模式也无法让它更快；")
        print("      真正跟着盘中变的是行情快照、指数、国债收益率与美股/美元。")
    print("=" * 68)

    if args.watch <= 0:
        run_once(cfg, args, cache, proxy, out_dir)
        return 0

    if not live:
        print("注意：--watch 通常配合 --live 使用；当前是离线模式，每轮复用缓存，")
        print("      结果不会变化。如需盘中刷新请加 --live。")
    round_no = 0
    while True:
        round_no += 1
        print(f"\n########## 第 {round_no} 轮　{time.strftime('%Y-%m-%d %H:%M:%S')} ##########")
        try:
            run_once(cfg, args, cache, proxy, out_dir)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            # 单轮失败不能中断循环：网络抖动、接口偶发超时都很常见
            print(f"本轮失败（{type(exc).__name__}: {exc}）；{args.watch} 秒后重试。", file=sys.stderr)
        try:
            print(f"---- 休眠 {args.watch} 秒（Ctrl+C 退出）----")
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n已停止实时刷新。")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
