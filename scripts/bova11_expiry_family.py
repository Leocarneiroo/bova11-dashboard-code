"""
Helpers para limpar CSVs B3 contaminados com múltiplas maturidades.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd


MONTH_CODE_BY_MONTH = {
    1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F",
    7: "G", 8: "H", 9: "I", 10: "J", 11: "K", 12: "L",
}

SUM_COLUMNS = {
    "call_oi", "put_oi",
    "call_vol", "put_vol",
    "call_trades", "put_trades",
    "call_oi_v", "put_oi_v",
}

MEAN_COLUMNS = {
    "call_last", "put_last",
    "call_bid", "call_ask", "put_bid", "put_ask",
    "call_delta", "put_delta",
    "call_gamma", "put_gamma",
    "call_theta", "put_theta",
    "call_vega", "put_vega",
    "call_iv", "put_iv",
}

TICKER_FAMILY_RE = re.compile(
    r"^[A-Z]+(?P<month>[A-Z])\d+(?P<suffix>W[1-5])?$",
    re.IGNORECASE,
)


def expected_ticker_family(exp_date: str, exp_type: str) -> Tuple[str, str]:
    """Retorna a família esperada do ticker como (letra_do_mês, sufixo)."""
    if not exp_date:
        return "", ""
    try:
        exp = datetime.strptime(exp_date, "%Y-%m-%d").date()
    except ValueError:
        return "", ""

    month_code = MONTH_CODE_BY_MONTH.get(exp.month, "")
    if not month_code:
        return "", ""

    if exp_type and exp_type.lower().startswith("semanal"):
        return month_code, f"W{math.ceil(exp.day / 7)}"
    return month_code, "MENSAL"


def parse_ticker_family(ticker: str) -> Optional[Tuple[str, str]]:
    """Extrai (letra_do_mês, sufixo) de um ticker B3."""
    if not isinstance(ticker, str):
        return None
    match = TICKER_FAMILY_RE.match(ticker.strip().upper())
    if not match:
        return None
    return match.group("month"), (match.group("suffix") or "MENSAL").upper()


def matches_expiry_family(ticker: str, exp_date: str, exp_type: str) -> bool:
    """Confere se o ticker pertence à família esperada para o vencimento."""
    expected = expected_ticker_family(exp_date, exp_type)
    if not expected[0]:
        return True
    parsed = parse_ticker_family(ticker)
    if parsed is None:
        return False
    return parsed == expected


def filter_expiry_family(
    df: pd.DataFrame,
    exp_date: str,
    exp_type: str,
    call_col: str = "call_ticker",
    put_col: str = "put_ticker",
) -> pd.DataFrame:
    """Mantém apenas linhas da família correta do vencimento."""
    if df.empty:
        return df.copy()

    expected = expected_ticker_family(exp_date, exp_type)
    if not expected[0]:
        return df.copy()

    ticker_cols = [col for col in (call_col, put_col) if col in df.columns]
    if not ticker_cols:
        return df.copy()

    mask = pd.Series(False, index=df.index)
    for col in ticker_cols:
        mask = mask | df[col].fillna("").map(
            lambda ticker: matches_expiry_family(ticker, exp_date, exp_type)
        )
    return df.loc[mask].copy()


def collapse_option_rows_by_strike(df: pd.DataFrame) -> pd.DataFrame:
    """Consolida linhas repetidas por strike mantendo somas e médias consistentes."""
    if df.empty or "strike" not in df.columns:
        return df.copy()

    base = df[df["strike"] > 0].copy()
    if base.empty:
        return base

    agg_map = {}
    for col in base.columns:
        if col == "strike":
            continue
        if col in SUM_COLUMNS:
            agg_map[col] = "sum"
        elif col in MEAN_COLUMNS:
            agg_map[col] = "mean"
        elif col.endswith("_ticker"):
            agg_map[col] = "first"
        elif pd.api.types.is_numeric_dtype(base[col]):
            agg_map[col] = "mean"
        else:
            agg_map[col] = "first"

    collapsed = base.groupby("strike", as_index=False).agg(agg_map)
    return collapsed.sort_values("strike").reset_index(drop=True)
