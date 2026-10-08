"""信号层。"""

from __future__ import annotations

from .macro import RuleResult, score_cpi, score_gdp, score_pmi, score_ppi

__all__ = ["RuleResult", "score_pmi", "score_cpi", "score_ppi", "score_gdp"]
