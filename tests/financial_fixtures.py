"""财报确定性样本：数值不代表任何实际企业。"""
import pandas as pd


def statement_tables(profit=100., capex=25., equity=100., liabilities=80., fetched="2026-10-23T09:00:00+08:00"):
    common = {"fetched_at": fetched, "source_updated_at": "2026-03-20T12:00:00+08:00"}
    return {
        "lrb": pd.DataFrame([{**common, "report_date": f"{y}-12-31", "net_profit": profit,
                              "revenue": 1000., "operating_profit": 150.} for y in range(2021, 2026)]),
        "llb": pd.DataFrame([{**common, "report_date": f"{y}-12-31", "capex": capex,
                              "operating_cash_flow": 120.} for y in range(2021, 2026)]),
        "fzb": pd.DataFrame([{**common, "report_date": "2026-06-30", "total_equity": equity,
                              "total_liabilities": liabilities}]),
    }
