"""Backtest B only: fail-closed historical inputs and a self-financing close ledger.

``available_at`` is the publication time of THIS VERSION, not the report period.
Date-only publications become usable the following day (unknown release hour).
Importers must preserve original versions; adding a date to restated data is invalid.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class HistoricalDataError(RuntimeError):
    """No publishable result; audit retains every checked signal/candidate."""

    def __init__(self, message, audit=None):
        super().__init__(message)
        self.audit = audit if audit is not None else pd.DataFrame()


def disclosed(frame, signal, period_col=None):
    if frame is None or not isinstance(frame, pd.DataFrame):
        return pd.DataFrame()
    if "available_at" not in frame:
        return frame.iloc[:0].copy()
    # Conservative next-day availability also for timestamped publications.
    published = pd.to_datetime(frame["available_at"], errors="coerce", format="mixed")
    if published.dt.tz is not None:
        raise ValueError("Publications require naive China-local timestamps")
    mask = published.notna() & (published.dt.normalize() < pd.Timestamp(signal).normalize())
    if period_col:
        periods = pd.to_datetime(frame[period_col], errors="coerce", format="mixed")
        if periods.dt.tz is not None:
            raise ValueError("Report/event dates require naive China-local dates")
        periods = periods.dt.normalize()
        mask &= periods.notna() & (periods <= signal)
    out = frame.loc[mask].copy()
    # Use the parsed values for BOTH filtering and version selection. Lexical
    # sorting would put '2022-9-1' after '2022-10-1'.
    out["available_at"] = published.loc[mask]
    if period_col:
        out[period_col] = periods.loc[mask]
        keys = [period_col, "available_at"]
        for _, versions in out.groupby(keys):
            if len(versions.drop_duplicates()) > 1:
                raise ValueError("Conflicting historical versions at the same publication time")
        out = out.sort_values("available_at", kind="stable").drop_duplicates(period_col, keep="last")
        out = out.sort_values(period_col)
    return out


def dated(series, signal):
    if series is None or not isinstance(series.index, pd.DatetimeIndex):
        return pd.Series(dtype=float)
    if series.index.has_duplicates:
        raise ValueError("Historical daily observations must have unique dates")
    return series.loc[series.index <= signal].sort_index()


def history_last(series):
    if series is None:
        return float("nan")
    values = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    return float(values.iloc[-1]) if len(values) else float("nan")


def strict_inputs(raw, signal):
    out = dict(raw)
    for key in ("pe_hist", "pb_hist", "price_hist", "bond_hist"):
        out[key] = dated(raw.get(key), signal)
    if not raw.get("valuation_vintage_verified", False):
        out["pe_hist"] = pd.Series(dtype=float)
        out["pb_hist"] = pd.Series(dtype=float)
    if not raw.get("bond_vintage_verified", False):
        out["bond_hist"] = pd.Series(dtype=float)
    else:
        # Daily observation date is not necessarily the publication timestamp.
        out["bond_hist"] = dated(raw.get("bond_hist"), pd.Timestamp(signal) - pd.Timedelta(days=1))
    out["fin"] = disclosed(raw.get("fin"), signal, "report_date")
    # Without archived original financial versions, dates alone cannot undo restatements.
    if not raw.get("fin_vintage_verified", False):
        out["fin"] = out["fin"].iloc[:0]
    out["div"] = disclosed(raw.get("div"), signal, "ex_date")
    if not raw.get("div_vintage_verified", False):
        out["div"] = out["div"].iloc[:0]
    div_source = raw.get("div")
    div_schema_ok = (isinstance(div_source, pd.DataFrame)
                     and {"available_at", "ex_date", "dps"}.issubset(div_source.columns)
                     and pd.to_datetime(div_source["available_at"], errors="coerce").notna().all()
                     and pd.to_datetime(div_source["ex_date"], errors="coerce").notna().all()
                     and pd.to_numeric(div_source["dps"], errors="coerce").notna().all())
    out["div_history_complete"] = bool(raw.get("div_history_complete", False)
                                       and raw.get("div_vintage_verified", False) and div_schema_ok)
    out["statements"] = None
    return out


def simulate_closes(prices, benchmark, targets, cost=0.0015, max_wait_days=15):
    """Daily close execution; targets form AFTER that close, fill strictly later.

    Target quantities are fixed at signal time; unavailable buys retain cash,
    unavailable sells remain held. Pending orders expire or are replaced at the
    next signal. Same-name rebalancing charges actual notional, including drift.
    Prices must share one consistent adjusted-price basis; this is a price-index
    simulation, not a cash-dividend/reinvestment or limit-order-book model.
    """
    if not 0 <= cost < 1 or max_wait_days < 0:
        raise ValueError("Invalid transaction cost or order lifetime")
    benchmark = pd.to_numeric(benchmark, errors="coerce").sort_index()
    if benchmark.index.has_duplicates or not isinstance(benchmark.index, pd.DatetimeIndex):
        raise ValueError("Benchmark requires unique dated closes")
    if benchmark.isna().any() or (benchmark <= 0).any():
        raise ValueError("Missing/invalid benchmark close")
    targets = {pd.Timestamp(t): list(codes) for t, codes in targets.items()}
    signals = sorted(targets)
    if not signals:
        raise ValueError("No historical signals")
    frames = {}
    for code, frame in prices.items():
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates:
            raise HistoricalDataError(f"{code}: unique dated prices required", pd.DataFrame([
                {"code": code, "eligible": False, "missing": "return_price_dates",
                 "stage": "return_prices"}]))
        frames[code] = frame.sort_index()
    cash, units, marks, pending = 1.0, {}, {}, {}
    rows, trades, events = [], [], []
    first = signals[0]
    calendar = benchmark.loc[benchmark.index >= first].index
    if first not in calendar:
        raise ValueError("Signal date must be an actual benchmark session")
    if len(calendar) < 2:
        raise ValueError("No session after signal")
    # Both accounts start in cash. Benchmark buys at the first post-signal close.
    b_start = calendar[1]
    b0 = float(benchmark.loc[b_start])
    for day in calendar:
        def missing_price(code, reason):
            raise HistoricalDataError(
                f"回测B：{code} {day.date()} 收益价格/证券状态不足，未计算收益：{reason}",
                pd.DataFrame([{"date": day, "code": code, "eligible": False,
                               "missing": reason, "stage": "return_prices"}]))

        # Expired, unheld orders no longer require market data.
        for code, (_, issued) in list(pending.items()):
            if (day - issued).days > max_wait_days:
                events.append({"date": day, "code": code, "event": "expired"})
                del pending[code]
        required = ({c for c, q in units.items() if q > 0}
                    | set(pending) | set(targets.get(day, [])))
        for code in sorted(required):
            frame = frames.get(code)
            if frame is None or day not in frame.index:
                missing_price(code, "unexplained_return_price_gap")
            r = frame.loc[day]
            status = r.get("security_status", "")
            if not isinstance(status, str) or status not in ("", "trading", "suspended"):
                # Delisting needs a verified settlement path; never freeze the
                # last quote forever or invent a liquidation value.
                missing_price(code, "unsupported_or_unknown_security_status")
            suspended = status == "suspended"
            if suspended and not (isinstance(r.get("status_source"), str)
                                  and r["status_source"].strip()):
                missing_price(code, "suspension_without_source")
            close = pd.to_numeric(r.get("close"), errors="coerce")
            volume = pd.to_numeric(r.get("volume"), errors="coerce")
            valid_close = np.isfinite(close) and close > 0
            valid_volume = np.isfinite(volume) and volume >= 0
            if not suspended and not (valid_close and valid_volume):
                missing_price(code, "invalid_return_close_or_volume")
            if suspended and not valid_close:
                if code not in marks:
                    missing_price(code, "suspension_without_prior_mark")
                events.append({"date": day, "code": code, "event": "suspended_mark",
                               "source": r["status_source"]})
        tradable = {}
        for code, frame in frames.items():
            if day not in frame.index:
                continue
            r = frame.loc[day]
            close = pd.to_numeric(r.get("close"), errors="coerce")
            volume = pd.to_numeric(r.get("volume"), errors="coerce")
            if np.isfinite(close) and close > 0:
                marks[code] = float(close)
                # Missing volume is unknown, never proof of tradability.
                if (np.isfinite(volume) and volume > 0 and bool(r.get("tradable", True))
                        and r.get("security_status", "") != "suspended"):
                    tradable[code] = float(close)
        orders = [(c, goal, issued) for c, (goal, issued) in pending.items()
                  if issued < day and c in tradable]
        # Sell first. Side-specific exchange restrictions can be supplied per row.
        for selling in (True, False):
            for code, goal, issued in sorted(orders):
                delta = goal - units.get(code, 0.0)
                if (delta < 0) != selling:
                    continue
                r = frames[code].loc[day]
                if bool(r.get("sell_blocked" if selling else "buy_blocked", False)):
                    continue
                px = tradable[code]
                if delta > 0:
                    delta = min(delta, max(0.0, cash) / (px * (1 + cost)))
                notional = delta * px
                fee = abs(notional) * cost
                cash -= notional + fee
                units[code] = units.get(code, 0.0) + delta
                if abs(delta) > 1e-12:
                    trades.append({"signal_date": issued, "date": day, "code": code,
                                   "quantity": delta, "price": px, "fee": fee})
                # Cash-limited remainder waits until expiry or replacement.
                if abs(units[code] - goal) < 1e-10:
                    pending.pop(code, None)
        nav = cash + sum(q * marks[c] for c, q in units.items() if q > 0)
        rows.append({"date": day, "nav": nav, "benchmark_nav": 1.0 if day <= b_start else float(benchmark.loc[day]) / b0,
                     "cash": cash, "pending": len(pending)})
        if day in targets:
            chosen = targets[day]
            if len(chosen) != len(set(chosen)):
                raise ValueError("Duplicate target code")
            # New signal supersedes old pending orders; a cash target is explicit [].
            events.extend({"date": day, "code": c, "event": "superseded"} for c in pending)
            pending = {}
            # Reserve only the estimated fees of ACTUAL signal-time turnover.
            # Solve V + c*sum(|target_value(V)-held_value|) = NAV.
            lo, hi = 0.0, nav
            all_codes = set(units) | set(chosen)
            for _ in range(64):
                investable = (lo + hi) / 2
                target_value = investable / len(chosen) if chosen else 0.0
                turnover = sum(abs((target_value if c in chosen else 0.0)
                                   - units.get(c, 0.0) * marks.get(c, 0.0)) for c in all_codes)
                if investable + cost * turnover > nav:
                    hi = investable
                else:
                    lo = investable
            budget = lo / len(chosen) if chosen else 0.0
            for code in set(units) | set(chosen):
                if code in chosen and code not in marks:
                    raise ValueError(f"{code}: no signal-time price")
                goal = budget / marks[code] if code in chosen else 0.0
                if abs(goal - units.get(code, 0.0)) > 1e-10:
                    pending[code] = (goal, day)
    events.extend({"date": calendar[-1], "code": c, "event": "unfilled_at_end"} for c in pending)
    return {"daily": pd.DataFrame(rows).set_index("date"),
            "trades": pd.DataFrame(trades), "events": pd.DataFrame(events), "units": units}

