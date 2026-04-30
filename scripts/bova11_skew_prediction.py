#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 IV Skew Signal — motor institucional transplantado
=========================================================

Mantém a integração com o dashboard atual (`--data-dir`, `--output`,
`--spot-history-file`) e substitui a formulação anterior pelo motor do
arquivo `sinal_iv_skew_v2_2.py`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bova11_shared import build_manual_spot_map


_BASEDIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(_BASEDIR, "..", "data")
DEFAULT_OUTPUT_FILE = os.path.join(_BASEDIR, "..", "output", "bova11_skew_prediction.html")
DEFAULT_HISTORY_FILE = os.path.join(_BASEDIR, "..", "history", "bova11_skew_prediction.json")
DEFAULT_SPOT_HISTORY_FILE = os.path.join(_BASEDIR, "..", "history", "bova11_spot_history.json")

MESES_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

EXPIRY_SUFFIX_PATTERN = re.compile(r"(W\d+)$")


@dataclass
class Config:
    min_volume_total: float = 50.0
    min_dte_short: int = 2
    min_dte_monthly: int = 0
    dte_penalty_short: int = 5
    dte_penalty_monthly: int = 3
    monthly_min_factor: float = 0.60

    atm_delta: float = 0.50
    rr_delta: float = 0.25
    atm_range_strikes: float = 3.0

    z_window: int = 20
    smooth_window: int = 3
    rv_short_window: int = 5
    rv_long_window: int = 20
    atr_window: int = 14

    weight_surface: float = 0.30
    weight_flow: float = 0.30
    weight_greeks: float = 0.20
    weight_regime: float = 0.20

    s_rr_change: float = 0.35
    s_rr_level: float = 0.25
    s_bf_change: float = 0.20
    s_ts_slope: float = 0.20

    f_delta_flow: float = 0.30
    f_vega_flow: float = 0.25
    f_put_call_oi_change: float = 0.25
    f_put_call_voi: float = 0.20

    g_dist_flip: float = 0.25

    r_iv_rank: float = 0.35
    r_rv_spread: float = 0.25
    r_ret_1d: float = 0.20
    r_gap: float = 0.20

    target_horizon: int = 1
    normalize_iv: bool = False


def safe_float(x, default=np.nan) -> float:
    try:
        if pd.isna(x):
            return default
        s = str(x).strip()
        if re.match(r"^\d{1,3}(\.\d{3})+,\d+", s):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")
        s = s.replace("%", "").strip()
        if s in {"", "-", "nan", "None"}:
            return default
        return float(s)
    except Exception:
        return default


def parse_km_number(x) -> float:
    if pd.isna(x):
        return 0.0
    s = str(x).strip().replace(" ", "")
    if s in {"", "-", "nan", "None"}:
        return 0.0
    s = s.replace(",", ".")
    try:
        if s.lower().endswith("k"):
            return float(s[:-1]) * 1_000.0
        if s.lower().endswith("m"):
            return float(s[:-1]) * 1_000_000.0
        return float(s)
    except Exception:
        return 0.0


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    min_periods = min(max(5, window // 4), window)
    mean = series.rolling(window, min_periods=min_periods).mean()
    std = series.rolling(window, min_periods=min_periods).std()
    return (series - mean) / std.replace(0, np.nan)


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _safe_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def parse_session_date_from_filename(filename: str, default_year: Optional[int] = None) -> Optional[date]:
    text = filename.lower()
    m = re.search(r"fechamento__(\d{1,2})([a-z]{3})_", text)
    if not m:
        m = re.search(r"fechamento \((\d{1,2})(?:pos|pre)?([a-z]{3})(?: volume)?\)\.csv$", text)
    if not m:
        return None
    day = int(m.group(1))
    month = MESES_PT.get(m.group(2))
    if not month:
        return None
    year = default_year or datetime.now().year
    candidates = []
    for y in (year - 1, year, year + 1):
        try:
            candidates.append(date(y, month, day))
        except Exception:
            pass
    if not candidates:
        return None
    reference = date(year, 7, 1)
    return min(candidates, key=lambda d: abs((d - reference).days))


def parse_expiry_from_filename(filename: str, session_dt: Optional[date] = None, default_year: Optional[int] = None) -> Tuple[str, Optional[date], str]:
    lower = filename.lower()
    m = re.search(r"venc_(\d+)_([a-z]{3})_([a-z0-9]+)_fechamento", lower)
    if not m:
        m = re.search(r"venc (\d+) ([a-z]{3}) ([a-z0-9]+) fechamento", lower)
    if not m:
        return ("desconhecido", None, "UNK")

    day = int(m.group(1))
    month_str = m.group(2)
    kind = m.group(3)
    month = MESES_PT.get(month_str)
    label = f"{day} {month_str} {kind}".strip()
    if not month:
        return (label, None, kind)

    year = default_year or (session_dt.year if session_dt else datetime.now().year)
    candidates = []
    for y in (year - 1, year, year + 1):
        try:
            candidates.append(date(y, month, day))
        except Exception:
            pass
    if not candidates:
        return (label, None, kind)
    if session_dt is None:
        return (label, min(candidates, key=lambda d: abs((d - date(year, month, day)).days)), kind)
    non_past = [c for c in candidates if c >= session_dt - timedelta(days=7)]
    best = min(non_past, default=min(candidates, key=lambda d: abs((d - session_dt).days)))
    return (label, best, kind)


def expected_suffix_for_kind(kind: str) -> str:
    kind_u = str(kind).upper()
    if kind_u.startswith("W"):
        return kind_u
    if kind_u == "MENSAL":
        return "Mensal"
    return kind


def find_matching_volume_file(iv_filename: str, folder: str) -> Optional[str]:
    base = iv_filename.replace(".csv", "")
    candidates = [
        base + "Volume_.csv",
        base.rstrip("_") + "_Volume_.csv",
        base + "_Volume_.csv",
        iv_filename.replace(".csv", "").replace(").", " Volume)."),
    ]
    for cand in candidates:
        path = os.path.join(folder, os.path.basename(cand))
        if os.path.exists(path):
            return path
    for name in os.listdir(folder):
        if "volume" not in name.lower() or not name.lower().endswith(".csv"):
            continue
        if name.lower().replace(" volume", "").replace("_volume_", "_") == iv_filename.lower():
            return os.path.join(folder, name)
    return None


def _detect_col(columns: List[str], candidates: List[str]) -> Optional[str]:
    normalized = {c.lower().strip(): c for c in columns}
    for cand in candidates:
        if cand.lower() in normalized:
            return normalized[cand.lower()]
    for col in columns:
        lc = col.lower().strip()
        for cand in candidates:
            if cand.lower() in lc:
                return col
    return None


def _filter_chain_by_suffix(df: pd.DataFrame, target_suffix: str, ativo_col: Optional[str] = None) -> pd.DataFrame:
    if ativo_col is None:
        ativo_col = df.columns[0]
    if target_suffix.upper() == "MENSAL":
        mask = ~df[ativo_col].astype(str).str.contains(r"W\d+", regex=True, na=False)
    else:
        mask = df[ativo_col].astype(str).str.contains(target_suffix, regex=False, na=False)
    return df.loc[mask].copy()


def read_option_chain(filepath: str, expiry_suffix: Optional[str] = None) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(filepath, sep=";", encoding="latin1")
    except Exception:
        return None
    if df.empty:
        return None
    if expiry_suffix:
        df = _filter_chain_by_suffix(df, expiry_suffix)
        if df.empty:
            return None

    cols = list(df.columns)
    strike_col = _detect_col(cols, ["strike", "exercicio", "preco exercicio"]) or cols[len(cols) // 2]
    strike_idx = cols.index(strike_col)
    call_map: Dict[str, str] = {}
    put_map: Dict[str, str] = {}
    for i, col in enumerate(cols):
        if i == strike_idx:
            continue
        lc = col.lower().strip()
        target = call_map if i < strike_idx else put_map
        suffix = "call" if i < strike_idx else "put"
        if "vol impl" in lc:
            target[f"iv_{suffix}"] = col
        elif "delta" in lc:
            target[f"delta_{suffix}"] = col
        elif "gamma" in lc:
            target[f"gamma_{suffix}"] = col
        elif "theta" in lc:
            target[f"theta_{suffix}"] = col
        elif "vega" in lc:
            target[f"vega_{suffix}"] = col
        elif "neg" in lc or "trades" in lc:
            target[f"neg_{suffix}"] = col
        elif "bid" in lc:
            target[f"bid_{suffix}"] = col
        elif "ask" in lc:
            target[f"ask_{suffix}"] = col
        elif "ultimo" in lc or "último" in lc or "last" in lc:
            target[f"price_{suffix}"] = col

    col_map = {**call_map, **put_map}
    if "iv_call" not in col_map and "iv_put" not in col_map:
        return None

    rows = []
    for _, row in df.iterrows():
        strike = safe_float(row[strike_col])
        if np.isnan(strike):
            continue
        item = {"strike": strike}
        for canon, orig in col_map.items():
            item[canon] = parse_km_number(row.get(orig)) if canon.startswith("neg_") else safe_float(row.get(orig))
        for col in [
            "iv_call", "iv_put", "delta_call", "delta_put", "gamma_call", "gamma_put",
            "theta_call", "theta_put", "vega_call", "vega_put", "bid_call", "ask_call",
            "bid_put", "ask_put", "price_call", "price_put", "neg_call", "neg_put",
        ]:
            if col not in item:
                item[col] = 0.0 if col.startswith("neg_") else np.nan
        rows.append(item)
    if not rows:
        return None
    out = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)
    out["mid_call"] = (out["bid_call"] + out["ask_call"]) / 2.0
    out["mid_put"] = (out["bid_put"] + out["ask_put"]) / 2.0
    out["price_call"] = out["price_call"].fillna(out["mid_call"])
    out["price_put"] = out["price_put"].fillna(out["mid_put"])
    return out


def read_volume_oi(filepath: Optional[str], expiry_suffix: Optional[str] = None) -> Optional[pd.DataFrame]:
    if not filepath:
        return None
    try:
        df = pd.read_csv(filepath, sep=";", encoding="latin1")
    except Exception:
        return None
    if df.empty or len(df.columns) < 10:
        return None
    if expiry_suffix:
        df = _filter_chain_by_suffix(df, expiry_suffix)
        if df.empty:
            return None

    rows = []
    for _, row in df.iterrows():
        strike = safe_float(row.iloc[5])
        if np.isnan(strike):
            continue
        rows.append({
            "strike": strike,
            "vol_call": parse_km_number(row.iloc[1]),
            "oi_call": parse_km_number(row.iloc[2]),
            "bid_call": safe_float(row.iloc[3]),
            "ask_call": safe_float(row.iloc[4]),
            "bid_put": safe_float(row.iloc[6]),
            "ask_put": safe_float(row.iloc[7]),
            "oi_put": parse_km_number(row.iloc[8]),
            "vol_put": parse_km_number(row.iloc[9]),
        })
    if not rows:
        return None
    out = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)
    out["mid_call"] = (out["bid_call"] + out["ask_call"]) / 2.0
    out["mid_put"] = (out["bid_put"] + out["ask_put"]) / 2.0
    return out


def load_manual_spot_df(path: Optional[str]) -> Optional[pd.DataFrame]:
    mapping = build_manual_spot_map(path)
    rows = []
    for iso_date, value in mapping.items():
        try:
            rows.append({"date": pd.Timestamp(iso_date).date(), "close": float(value)})
        except Exception:
            pass
    if not rows:
        return None
    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    out["open"] = np.nan
    out["high"] = np.nan
    out["low"] = np.nan
    return out


def interpolate_iv_by_delta(df_chain: pd.DataFrame, target_delta: float, option_type: str) -> float:
    delta_col = f"delta_{option_type}"
    iv_col = f"iv_{option_type}"
    tmp = df_chain[["strike", delta_col, iv_col]].copy().dropna()
    if tmp.empty:
        return np.nan
    if option_type == "put":
        tmp[delta_col] = tmp[delta_col].abs()
    tmp = tmp.sort_values(delta_col)
    x = tmp[delta_col].values.astype(float)
    y = tmp[iv_col].values.astype(float)
    if len(np.unique(x)) < 2:
        return float(y[np.argmin(np.abs(x - target_delta))])
    return float(np.interp(target_delta, x, y, left=y[0], right=y[-1]))


def estimate_atm_strike(df_chain: pd.DataFrame, atm_delta: float = 0.50) -> float:
    tmp = df_chain[["strike", "delta_call"]].copy().dropna()
    if tmp.empty:
        return float(df_chain["strike"].median()) if not df_chain.empty else np.nan
    idx = (tmp["delta_call"] - atm_delta).abs().idxmin()
    return float(tmp.loc[idx, "strike"])


def fit_smile_params(df_chain: pd.DataFrame, forward_proxy: float) -> Tuple[float, float]:
    tmp_call = df_chain[["strike", "iv_call"]].rename(columns={"iv_call": "iv"}).dropna()
    tmp_put = df_chain[["strike", "iv_put"]].rename(columns={"iv_put": "iv"}).dropna()
    tmp = pd.concat([tmp_call, tmp_put], ignore_index=True)
    if tmp.empty or forward_proxy <= 0:
        return np.nan, np.nan
    tmp["m"] = np.log(tmp["strike"] / forward_proxy)
    tmp = tmp.replace([np.inf, -np.inf], np.nan).dropna()
    if len(tmp) < 5:
        return np.nan, np.nan
    X = np.column_stack([np.ones(len(tmp)), tmp["m"].values, tmp["m"].values ** 2])
    y = tmp["iv"].values
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    return float(beta[1]), float(beta[2])


def compute_surface_metrics(df_chain: pd.DataFrame, session_date: date, expiry_date: Optional[date], cfg: Config) -> Dict[str, float]:
    atm_iv_call = interpolate_iv_by_delta(df_chain, cfg.atm_delta, "call")
    atm_iv_put = interpolate_iv_by_delta(df_chain, cfg.atm_delta, "put")
    rr25_call = interpolate_iv_by_delta(df_chain, cfg.rr_delta, "call")
    rr25_put = interpolate_iv_by_delta(df_chain, cfg.rr_delta, "put")
    atm_values = [v for v in (atm_iv_call, atm_iv_put) if not np.isnan(v)]
    atm_iv = float(np.mean(atm_values)) if atm_values else np.nan
    rr25 = (rr25_put - rr25_call) if not np.isnan(rr25_put) and not np.isnan(rr25_call) else np.nan
    bf25 = (((rr25_put + rr25_call) / 2) - atm_iv) if not np.isnan(rr25) and not np.isnan(atm_iv) else np.nan
    atm_strike = estimate_atm_strike(df_chain, cfg.atm_delta)
    slope, curvature = fit_smile_params(df_chain, atm_strike if not np.isnan(atm_strike) else 1.0)
    dte = (expiry_date - session_date).days if expiry_date else np.nan
    iv_divisor = 100.0 if cfg.normalize_iv else 1.0
    return {
        "atm_strike": atm_strike,
        "atm_iv_call": atm_iv_call / iv_divisor,
        "atm_iv_put": atm_iv_put / iv_divisor,
        "atm_iv": atm_iv / iv_divisor,
        "rr25": rr25 / iv_divisor if not np.isnan(rr25) else np.nan,
        "bf25": bf25 / iv_divisor if not np.isnan(bf25) else np.nan,
        "smile_slope": slope,
        "smile_curvature": curvature,
        "dte": dte,
    }


def classify_aggression(price: float, bid: float, ask: float) -> float:
    return 0.0


def merge_chain_and_volume(df_chain: pd.DataFrame, df_vol: Optional[pd.DataFrame]) -> pd.DataFrame:
    out = df_chain.copy()
    if df_vol is not None and not df_vol.empty:
        out = out.merge(df_vol, on="strike", how="left", suffixes=("", "_vol"))
    else:
        for c in ["vol_call", "oi_call", "vol_put", "oi_put"]:
            if c not in out.columns:
                out[c] = np.nan
    return out


def _safe_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col in df.columns:
        return df[col].fillna(default)
    return pd.Series(default, index=df.index)


def compute_notional_and_flows(df: pd.DataFrame, multiplier: float = 1.0) -> Dict[str, float]:
    tmp = df.copy()
    for side in ["call", "put"]:
        tmp[f"price_{side}"] = _safe_series(tmp, f"price_{side}").fillna(_safe_series(tmp, f"mid_{side}"))
        tmp[f"aggr_{side}"] = [
            classify_aggression(p, b, a)
            for p, b, a in zip(tmp[f"price_{side}"], _safe_series(tmp, f"bid_{side}", np.nan), _safe_series(tmp, f"ask_{side}", np.nan))
        ]
        tmp[f"volume_{side}"] = _safe_series(tmp, f"vol_{side}").fillna(_safe_series(tmp, f"neg_{side}"))
        tmp[f"notional_{side}"] = tmp[f"volume_{side}"] * tmp[f"price_{side}"].fillna(0.0) * multiplier
        tmp[f"delta_flow_{side}"] = _safe_series(tmp, f"delta_{side}") * tmp[f"volume_{side}"] * tmp[f"aggr_{side}"]
        tmp[f"vega_flow_{side}"] = _safe_series(tmp, f"vega_{side}") * tmp[f"volume_{side}"] * tmp[f"aggr_{side}"]

    vol_call = tmp["volume_call"].sum(skipna=True)
    vol_put = tmp["volume_put"].sum(skipna=True)
    oi_call = _safe_series(tmp, "oi_call").sum()
    oi_put = _safe_series(tmp, "oi_put").sum()

    return {
        "notional_call": tmp["notional_call"].sum(skipna=True),
        "notional_put": tmp["notional_put"].sum(skipna=True),
        "delta_flow": tmp["delta_flow_call"].sum(skipna=True) - tmp["delta_flow_put"].sum(skipna=True),
        "vega_flow": tmp["vega_flow_call"].sum(skipna=True) - tmp["vega_flow_put"].sum(skipna=True),
        "vol_call": vol_call,
        "vol_put": vol_put,
        "oi_call": oi_call,
        "oi_put": oi_put,
        "put_call_voi": (vol_put / max(oi_put, 1.0)) - (vol_call / max(oi_call, 1.0)),
    }


def compute_atm_voi(df_vol: Optional[pd.DataFrame], atm_strike: float, atm_range: float) -> Tuple[float, float]:
    if df_vol is None or df_vol.empty or np.isnan(atm_strike):
        return np.nan, np.nan
    tmp = df_vol[(df_vol["strike"] >= atm_strike - atm_range) & (df_vol["strike"] <= atm_strike + atm_range)]
    if tmp.empty:
        return np.nan, np.nan
    voi_call = tmp["vol_call"].sum() / max(tmp["oi_call"].sum(), 1.0)
    voi_put = tmp["vol_put"].sum() / max(tmp["oi_put"].sum(), 1.0)
    return voi_call, voi_put


def compute_gex_dex(df: pd.DataFrame, spot: float) -> Dict[str, float]:
    tmp = df.copy()
    tmp["gex_call"] = (spot ** 2) * _safe_series(tmp, "gamma_call") * _safe_series(tmp, "oi_call")
    tmp["gex_put"] = -(spot ** 2) * _safe_series(tmp, "gamma_put") * _safe_series(tmp, "oi_put")
    tmp["dex_call"] = spot * _safe_series(tmp, "delta_call") * _safe_series(tmp, "oi_call")
    tmp["dex_put"] = -spot * _safe_series(tmp, "delta_put").abs() * _safe_series(tmp, "oi_put")
    tmp["net_gex_strike"] = tmp["gex_call"] + tmp["gex_put"]
    tmp["net_dex_strike"] = tmp["dex_call"] + tmp["dex_put"]
    by_strike = tmp.groupby("strike", as_index=False)[["net_gex_strike", "net_dex_strike"]].sum().sort_values("strike")
    net_gex = by_strike["net_gex_strike"].sum()
    net_dex = by_strike["net_dex_strike"].sum()

    flip = np.nan
    vals = by_strike["net_gex_strike"].values
    strikes = by_strike["strike"].values
    for i in range(1, len(vals)):
        if vals[i - 1] == 0:
            flip = strikes[i - 1]
            break
        if vals[i - 1] * vals[i] < 0:
            x1, x2 = strikes[i - 1], strikes[i]
            y1, y2 = vals[i - 1], vals[i]
            flip = x1 - y1 * (x2 - x1) / (y2 - y1)
            break
    dist_flip = (spot - flip) if not np.isnan(flip) else np.nan
    theta_decay_proxy = ((_safe_series(tmp, "theta_call").abs() * _safe_series(tmp, "oi_call")).sum() + (_safe_series(tmp, "theta_put").abs() * _safe_series(tmp, "oi_put")).sum())
    return {
        "net_gex": net_gex,
        "net_dex": net_dex,
        "gamma_flip": flip,
        "dist_flip_raw": dist_flip,
        "vanna_charm_proxy": theta_decay_proxy,
    }


def compute_expiry_weight(volume_total: float, dte: float, kind: str, cfg: Config) -> float:
    if np.isnan(volume_total) or volume_total < cfg.min_volume_total:
        return 0.0
    is_monthly = "mensal" in str(kind).lower()
    if is_monthly:
        if not np.isnan(dte) and dte < cfg.min_dte_monthly:
            return 0.0
        if np.isnan(dte):
            factor = 1.0
        elif dte <= cfg.dte_penalty_monthly:
            factor = cfg.monthly_min_factor + (1.0 - cfg.monthly_min_factor) * (dte / max(cfg.dte_penalty_monthly, 1))
        else:
            factor = 1.0
    else:
        if not np.isnan(dte) and dte < cfg.min_dte_short:
            return 0.0
        if np.isnan(dte):
            factor = 1.0
        elif dte <= cfg.dte_penalty_short:
            factor = dte / max(cfg.dte_penalty_short, 1)
        else:
            factor = 1.0
    return float(volume_total * factor)


def process_folder(folder: str, spot_df: Optional[pd.DataFrame], cfg: Config) -> pd.DataFrame:
    files = [f for f in os.listdir(folder) if f.lower().endswith(".csv")]
    iv_files = [f for f in files if "volume" not in f.lower()]
    sessions: Dict[date, List[str]] = {}
    for name in iv_files:
        sess_dt = parse_session_date_from_filename(name)
        if sess_dt is None:
            continue
        sessions.setdefault(sess_dt, []).append(name)
    if not sessions:
        raise RuntimeError("Nenhuma sessão válida encontrada.")

    all_rows: List[Dict[str, float]] = []
    prev_daily = None
    spot_lookup = dict(zip(spot_df["date"], spot_df["close"])) if spot_df is not None and not spot_df.empty else {}

    for sess_dt in sorted(sessions.keys()):
        per_expiry = []
        for iv_name in sorted(sessions[sess_dt]):
            iv_path = os.path.join(folder, iv_name)
            label, expiry_dt, kind = parse_expiry_from_filename(iv_name, session_dt=sess_dt)
            expected_suffix = expected_suffix_for_kind(kind)
            chain = read_option_chain(iv_path, expiry_suffix=expected_suffix)
            if chain is None or chain.empty:
                continue
            surf = compute_surface_metrics(chain, sess_dt, expiry_dt, cfg)
            vol_file = find_matching_volume_file(iv_name, folder)
            vol_df = read_volume_oi(vol_file, expiry_suffix=expected_suffix) if vol_file else None
            merged = merge_chain_and_volume(chain, vol_df)
            flow = compute_notional_and_flows(merged)
            voi_call_atm, voi_put_atm = compute_atm_voi(vol_df, surf["atm_strike"], cfg.atm_range_strikes)
            spot = spot_lookup.get(sess_dt, surf["atm_strike"])
            if spot is None or np.isnan(spot):
                spot = surf["atm_strike"]
            greeks = compute_gex_dex(merged, float(spot) if not np.isnan(spot) else 1.0)
            volume_total = flow["vol_call"] + flow["vol_put"]
            weight = compute_expiry_weight(volume_total, surf["dte"], kind, cfg)
            if weight <= 0:
                continue
            per_expiry.append({
                "date": sess_dt,
                "expiry_label": label,
                "expiry_kind": kind,
                "expiry_date": expiry_dt,
                "weight": weight,
                **surf,
                **flow,
                "voi_call_atm": voi_call_atm,
                "voi_put_atm": voi_put_atm,
                **greeks,
            })

        if not per_expiry:
            continue

        exp_df = pd.DataFrame(per_expiry)
        ts_short = np.nan
        ts_long = np.nan
        if exp_df["dte"].notna().any():
            short_idx = exp_df["dte"].replace(0, np.nan).idxmin()
            long_idx = exp_df["dte"].idxmax()
            ts_short = exp_df.loc[short_idx, "atm_iv"]
            ts_long = exp_df.loc[long_idx, "atm_iv"]
        term_slope = (ts_short - ts_long) if not np.isnan(ts_short) and not np.isnan(ts_long) else np.nan

        w = exp_df["weight"].values.astype(float)
        daily = {
            "date": sess_dt,
            "n_expiries": len(exp_df),
            "spot": float(spot_lookup.get(sess_dt, np.nan)),
            "atm_iv": np.average(exp_df["atm_iv"].fillna(exp_df["atm_iv"].median()), weights=w),
            "rr25": np.average(exp_df["rr25"].fillna(exp_df["rr25"].median()), weights=w),
            "bf25": np.average(exp_df["bf25"].fillna(exp_df["bf25"].median()), weights=w),
            "smile_slope": np.average(exp_df["smile_slope"].fillna(0.0), weights=w),
            "smile_curvature": np.average(exp_df["smile_curvature"].fillna(0.0), weights=w),
            "term_slope": term_slope,
            "notional_call": exp_df["notional_call"].sum(),
            "notional_put": exp_df["notional_put"].sum(),
            "delta_flow": exp_df["delta_flow"].sum(),
            "vega_flow": exp_df["vega_flow"].sum(),
            "vol_call": exp_df["vol_call"].sum(),
            "vol_put": exp_df["vol_put"].sum(),
            "oi_call": exp_df["oi_call"].sum(),
            "oi_put": exp_df["oi_put"].sum(),
            "voi_call_atm": np.average(exp_df["voi_call_atm"].fillna(0.0), weights=w),
            "voi_put_atm": np.average(exp_df["voi_put_atm"].fillna(0.0), weights=w),
            "put_call_voi": np.average(exp_df["put_call_voi"].fillna(0.0), weights=w),
            "net_gex": exp_df["net_gex"].sum(),
            "net_dex": exp_df["net_dex"].sum(),
            "gamma_flip": np.average(exp_df["gamma_flip"].fillna(exp_df["atm_strike"].fillna(0.0)), weights=w),
            "dist_flip_raw": np.average(exp_df["dist_flip_raw"].fillna(0.0), weights=w),
            "vanna_charm_proxy": exp_df["vanna_charm_proxy"].sum(),
        }
        if prev_daily is not None:
            daily["delta_oi_call"] = daily["oi_call"] - prev_daily.get("oi_call", np.nan)
            daily["delta_oi_put"] = daily["oi_put"] - prev_daily.get("oi_put", np.nan)
        else:
            daily["delta_oi_call"] = np.nan
            daily["delta_oi_put"] = np.nan
        all_rows.append(daily)
        prev_daily = daily

    out = pd.DataFrame(all_rows).sort_values("date").reset_index(drop=True)
    if spot_df is not None and not spot_df.empty and not out.empty:
        out = out.merge(spot_df, on="date", how="left", suffixes=("", "_spot"))
        out["spot"] = out["spot"].fillna(out.get("close", pd.Series(dtype=float)))
    return out


def build_features(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    out = df.copy().sort_values("date").reset_index(drop=True)
    out["rr25_change"] = out["rr25"].diff()
    out["bf25_change"] = out["bf25"].diff()
    out["atm_iv_change"] = out["atm_iv"].diff()
    out["term_slope_change"] = out["term_slope"].diff()
    out["put_call_oi_change"] = out["delta_oi_put"] - out["delta_oi_call"]
    out["put_call_notional_diff"] = out["notional_put"] - out["notional_call"]

    if "close" in out.columns and out["close"].notna().any():
        out["ret_1d"] = np.log(out["close"] / out["close"].shift(1))
        out["rv_short"] = out["ret_1d"].rolling(cfg.rv_short_window).std() * np.sqrt(252)
        out["rv_long"] = out["ret_1d"].rolling(cfg.rv_long_window).std() * np.sqrt(252)
        out["rv_spread"] = out["rv_short"] - out["rv_long"]
        out["gap"] = np.nan
        if {"high", "low", "close"}.issubset(set(out.columns)):
            tr1 = out["high"] - out["low"]
            tr2 = (out["high"] - out["close"].shift(1)).abs()
            tr3 = (out["low"] - out["close"].shift(1)).abs()
            out["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1, skipna=True)
            out["atr"] = pd.Series(out["tr"]).rolling(cfg.atr_window).mean()
        else:
            out["atr"] = np.nan
    else:
        for col in ["ret_1d", "rv_short", "rv_long", "rv_spread", "gap", "atr"]:
            out[col] = np.nan

    out["iv_min"] = out["atm_iv"].rolling(252, min_periods=20).min()
    out["iv_max"] = out["atm_iv"].rolling(252, min_periods=20).max()
    out["iv_rank"] = (out["atm_iv"] - out["iv_min"]) / (out["iv_max"] - out["iv_min"])
    out["dist_flip"] = out["dist_flip_raw"] / out["atr"]
    out["dist_flip"] = out["dist_flip"].replace([np.inf, -np.inf], np.nan)

    zcols = [
        "rr25_change", "rr25", "bf25_change", "term_slope",
        "delta_flow", "vega_flow", "put_call_oi_change", "put_call_voi",
        "net_gex", "dist_flip", "net_dex", "vanna_charm_proxy",
        "iv_rank", "rv_spread", "ret_1d", "gap",
    ]
    for col in zcols:
        out[f"z_{col}"] = rolling_zscore(out[col], cfg.z_window) if col in out.columns else np.nan

    out["score_surface"] = (
        cfg.s_rr_change * out["z_rr25_change"].fillna(0.0) +
        cfg.s_rr_level * out["z_rr25"].fillna(0.0) +
        cfg.s_bf_change * out["z_bf25_change"].fillna(0.0) +
        cfg.s_ts_slope * out["z_term_slope"].fillna(0.0)
    )
    out["score_flow"] = (
        cfg.f_delta_flow * (-out["z_delta_flow"].fillna(0.0)) +
        cfg.f_vega_flow * (-out["z_vega_flow"].fillna(0.0)) +
        cfg.f_put_call_oi_change * out["z_put_call_oi_change"].fillna(0.0) +
        cfg.f_put_call_voi * out["z_put_call_voi"].fillna(0.0)
    )
    out["score_greeks"] = (
        0.45 * out["z_net_gex"].fillna(0.0) +
        cfg.g_dist_flip * out["z_dist_flip"].fillna(0.0) +
        0.30 * out["z_net_dex"].fillna(0.0)
    )
    out["score_regime"] = (
        cfg.r_iv_rank * out["z_iv_rank"].fillna(0.0) +
        cfg.r_rv_spread * out["z_rv_spread"].fillna(0.0) +
        cfg.r_ret_1d * out["z_ret_1d"].fillna(0.0) +
        cfg.r_gap * out["z_gap"].fillna(0.0)
    )
    out["score_final_raw"] = (
        cfg.weight_surface * out["score_surface"] +
        cfg.weight_flow * out["score_flow"] +
        cfg.weight_greeks * out["score_greeks"] +
        cfg.weight_regime * out["score_regime"]
    )
    warmup_ready = out["z_rr25"].notna() & out["z_net_gex"].notna() & out["z_put_call_voi"].notna()
    out["score_final_raw"] = out["score_final_raw"].where(warmup_ready, other=np.nan)
    out["score_final"] = out["score_final_raw"].ewm(span=cfg.smooth_window, min_periods=1, adjust=False).mean().where(warmup_ready, other=np.nan)
    out["prob_bear"] = sigmoid(out["score_final"])
    out["prob_bull"] = 1.0 - out["prob_bear"]
    out["target_down"] = (out["close"].shift(-cfg.target_horizon) < out["close"]).astype(float) if "close" in out.columns else np.nan
    out["signal_label"] = out["prob_bear"].apply(classify_probability_signal)
    out["main_driver"] = out.apply(infer_main_driver, axis=1)
    return out


def classify_probability_signal(prob_bear: float) -> str:
    if pd.isna(prob_bear):
        return "SEM DADOS"
    if prob_bear > 0.65:
        return "BEAR FORTE"
    if prob_bear > 0.55:
        return "BEAR MODERADO"
    if prob_bear >= 0.45:
        return "NEUTRO"
    if prob_bear >= 0.35:
        return "BULL MODERADO"
    return "BULL FORTE"


def infer_main_driver(row: pd.Series) -> str:
    blocks = {
        "surface": row.get("score_surface", 0.0),
        "flow": row.get("score_flow", 0.0),
        "greeks": row.get("score_greeks", 0.0),
        "regime": row.get("score_regime", 0.0),
    }
    key = max(blocks, key=lambda x: abs(blocks[x]))
    direction = "bearish" if blocks[key] > 0 else "bullish"
    return f"{key}:{direction}"


def run_simple_backtest(df: pd.DataFrame) -> pd.DataFrame:
    tmp = df[["date", "prob_bear", "target_down", "signal_label"]].copy().dropna()
    if tmp.empty:
        return pd.DataFrame()
    tmp["pred_down"] = (tmp["prob_bear"] > 0.50).astype(float)
    tmp["hit"] = (tmp["pred_down"] == tmp["target_down"]).astype(int)
    return tmp


def backtest_summary(bt: pd.DataFrame) -> Dict[str, float]:
    if bt.empty:
        return {"n": 0, "accuracy": np.nan, "bear_precision": np.nan, "bull_precision": np.nan}
    return {
        "n": int(len(bt)),
        "accuracy": float(bt["hit"].mean()),
        "bear_precision": float(bt.loc[bt["pred_down"] == 1, "hit"].mean()) if (bt["pred_down"] == 1).any() else np.nan,
        "bull_precision": float(bt.loc[bt["pred_down"] == 0, "hit"].mean()) if (bt["pred_down"] == 0).any() else np.nan,
    }


def build_rows_for_frontend(df: pd.DataFrame) -> List[Dict[str, object]]:
    ordered = df.copy().sort_values("date", ascending=False).reset_index(drop=True)
    rows = []
    for _, row in ordered.iterrows():
        rows.append({
            "date": row["date"].strftime("%d/%m/%Y"),
            "atm_iv": None if pd.isna(row.get("atm_iv")) else round(float(row.get("atm_iv")), 4),
            "rr25": None if pd.isna(row.get("rr25")) else round(float(row.get("rr25")), 4),
            "bf25": None if pd.isna(row.get("bf25")) else round(float(row.get("bf25")), 4),
            "term_slope": None if pd.isna(row.get("term_slope")) else round(float(row.get("term_slope")), 4),
            "delta_flow": None if pd.isna(row.get("delta_flow")) else round(float(row.get("delta_flow")), 2),
            "vega_flow": None if pd.isna(row.get("vega_flow")) else round(float(row.get("vega_flow")), 2),
            "net_gex": None if pd.isna(row.get("net_gex")) else round(float(row.get("net_gex")), 2),
            "net_dex": None if pd.isna(row.get("net_dex")) else round(float(row.get("net_dex")), 2),
            "score_surface": None if pd.isna(row.get("score_surface")) else round(float(row.get("score_surface")), 4),
            "score_flow": None if pd.isna(row.get("score_flow")) else round(float(row.get("score_flow")), 4),
            "score_greeks": None if pd.isna(row.get("score_greeks")) else round(float(row.get("score_greeks")), 4),
            "score_regime": None if pd.isna(row.get("score_regime")) else round(float(row.get("score_regime")), 4),
            "score_final": None if pd.isna(row.get("score_final")) else round(float(row.get("score_final")), 4),
            "prob_bear": None if pd.isna(row.get("prob_bear")) else round(float(row.get("prob_bear")), 4),
            "prob_bull": None if pd.isna(row.get("prob_bull")) else round(float(row.get("prob_bull")), 4),
            "signal_label": row.get("signal_label", "SEM DADOS"),
            "main_driver": row.get("main_driver", "N/A"),
            "close": None if pd.isna(row.get("close")) else round(float(row.get("close")), 4),
            "next_close": None if pd.isna(row.get("next_close")) else round(float(row.get("next_close")), 4),
            "hit": None if pd.isna(row.get("hit")) else int(row.get("hit")),
        })
    return rows


def build_html(rows: List[Dict[str, object]], cfg: Config, bt_summary: Dict[str, float], ref_output: str) -> str:
    last = rows[0]
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    rows_json = _safe_json(rows)
    cfg_json = _safe_json(asdict(cfg))
    cfg_help_json = _safe_json({
        "min_volume_total": "Volume total minimo para considerar a sessao valida no modelo.",
        "min_dte_short": "Dias uteis minimos aceitos para vencimentos curtos.",
        "min_dte_monthly": "Dias uteis minimos aceitos para vencimentos mensais.",
        "dte_penalty_short": "Penalidade aplicada quando o vencimento curto fica perto demais do prazo final.",
        "dte_penalty_monthly": "Penalidade equivalente para a curva mensal.",
        "monthly_min_factor": "Peso minimo garantido para a perna mensal na agregacao.",
        "atm_delta": "Delta alvo usado para interpolar a vol ATM.",
        "rr_delta": "Delta alvo usado no risk reversal e no butterfly.",
        "atm_range_strikes": "Faixa de strikes ao redor do ATM usada em filtros locais.",
        "z_window": "Janela do z-score rolling para normalizacao dos sinais.",
        "smooth_window": "Janela de suavizacao EWM do score final.",
        "rv_short_window": "Janela curta da volatilidade realizada.",
        "rv_long_window": "Janela longa da volatilidade realizada.",
        "atr_window": "Janela do ATR usada nos blocos de regime.",
        "weight_surface": "Peso do bloco de superficie no score final.",
        "weight_flow": "Peso do bloco de fluxo no score final.",
        "weight_greeks": "Peso do bloco de gregas no score final.",
        "weight_regime": "Peso do bloco de regime no score final.",
        "s_rr_change": "Peso da variacao do risk reversal no bloco de superficie.",
        "s_rr_level": "Peso do nivel absoluto do risk reversal.",
        "s_bf_change": "Peso da variacao do butterfly.",
        "s_ts_slope": "Peso da inclinacao da estrutura a termo.",
        "f_delta_flow": "Peso do fluxo direcional via delta ajustado.",
        "f_vega_flow": "Peso do fluxo de vega no bloco de fluxo.",
        "f_put_call_oi_change": "Peso da mudanca relativa de OI entre puts e calls.",
        "f_put_call_voi": "Peso do volume relativo entre puts e calls.",
        "g_dist_flip": "Peso da distancia para o gamma flip no bloco de gregas.",
        "r_iv_rank": "Peso do IV Rank no bloco de regime.",
        "r_rv_spread": "Peso do spread entre RV curta e longa.",
        "r_ret_1d": "Peso do retorno diario no bloco de regime.",
        "r_gap": "Peso do gap diario no bloco de regime.",
        "target_horizon": "Horizonte do alvo usado no backtest.",
        "normalize_iv": "Se verdadeiro, normaliza IV em escala percentual antes do score.",
    })
    signal_tone = "bear" if "BEAR" in str(last["signal_label"]) else ("bull" if "BULL" in str(last["signal_label"]) else "neu")

    return f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — IV Skew Signal</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {{
  --bg:#FAFAF8;--bg2:#F0EFEC;--card:#FFFFFF;--border:#D8D7D4;--text:#1A1A18;--text2:#4A4A48;--text3:#8A8A88;
  --blue:#0969DA;--green:#1A7F37;--red:#CF222E;--amber:#B8720A;
}}
[data-theme="dark"] {{
  --bg:#0d1117;--bg2:#161b22;--card:#161b22;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--text3:#6e7681;
  --blue:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922;
}}
* {{ box-sizing:border-box;margin:0;padding:0; }}
body {{ background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px; }}
.page {{ max-width:1540px;margin:0 auto;padding:28px 20px 36px; }}
.hdr {{ display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px; }}
.kicker {{ color:var(--text3);text-transform:uppercase;letter-spacing:.08em;font-size:.78rem;font-weight:800; }}
h1 {{ margin:.35rem 0 .45rem;font-size:clamp(1.8rem,3vw,2.8rem);line-height:1.05; }}
.sub {{ color:var(--text2);max-width:860px;line-height:1.45; }}
.theme-btn {{ border:1px solid var(--border);background:var(--card);color:var(--text);border-radius:999px;padding:7px 10px;cursor:pointer; }}
.cards {{ display:grid;gap:12px;margin:18px 0; }}
.cards.primary {{ grid-template-columns:repeat(5,minmax(0,1fr)); }}
.cards.components {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
@media(max-width:1180px) {{ .cards.primary,.cards.components {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
@media(max-width:560px) {{ .cards.primary,.cards.components {{ grid-template-columns:1fr; }} }}
.card,.panel {{ background:var(--card);border:1px solid var(--border);border-radius:12px; }}
.card {{ padding:16px;min-height:112px; }}
.label {{ color:var(--text3);font-size:.74rem;font-weight:800;text-transform:uppercase;letter-spacing:.05em; }}
.value {{ font-size:1.45rem;font-weight:850;margin-top:8px;line-height:1.15; }}
.mini {{ color:var(--text3);font-size:.8rem;margin-top:6px;line-height:1.35; }}
.bear {{ color:var(--red); }} .bull {{ color:var(--green); }} .neu {{ color:var(--text2); }}
.stack {{ display:grid;grid-template-columns:1fr;gap:16px;margin-top:6px; }}
.panel-h {{ padding:12px 14px;border-bottom:1px solid var(--border);background:var(--bg2);font-weight:700;font-size:.9rem; }}
.chart-wrap {{ height:300px;padding:14px; }}
.chart-wrap.tall {{ height:390px; }}
.table-wrap {{ overflow:auto;max-height:560px; }}
table {{ width:100%;border-collapse:collapse;font-size:.84rem; }}
th,td {{ padding:8px 9px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap; }}
th:first-child,td:first-child {{ text-align:left; }}
th {{ position:sticky;top:0;background:var(--bg2);color:var(--text3);font-size:.72rem;text-transform:uppercase; }}
.config-box details {{ border-radius:12px;overflow:hidden; }}
.config-box summary {{ list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:var(--bg2);font-weight:700; }}
.config-box summary::-webkit-details-marker {{ display:none; }}
.config-box summary::after {{ content:'+';color:var(--text3);font-size:1.1rem; }}
.config-box details[open] summary::after {{ content:'−'; }}
.config-box .cfg-intro {{ padding:0 16px 14px;color:var(--text2);font-size:.83rem;line-height:1.45;border-bottom:1px solid var(--border); }}
.cfg-grid {{ display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:16px; }}
.cfg-item {{ border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--card); }}
.cfg-top {{ display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:6px; }}
.cfg-key {{ font-weight:700;line-height:1.25; }}
.cfg-val {{ color:var(--blue);font-weight:700;text-align:right;white-space:nowrap; }}
.cfg-desc {{ color:var(--text2);font-size:.8rem;line-height:1.45; }}
@media(max-width:900px) {{ .cfg-grid {{ grid-template-columns:1fr; }} }}
.foot {{ margin-top:16px;padding-top:12px;border-top:1px solid var(--border);color:var(--text3);font-size:.78rem;text-align:center; }}
</style>
</head>
<body>
<main class="page">
  <header class="hdr">
    <div>
      <div class="kicker">IV Skew Signal</div>
      <h1>Motor institucional</h1>
      <div class="sub">Modelo probabilistico por superficie, flow, greeks e regime, integrado ao dashboard atual.</div>
    </div>
    <button class="theme-btn" id="theme-toggle">◐</button>
  </header>

  <section class="cards primary">
    <div class="card"><div class="label">Último Sinal</div><div class="value {signal_tone}">{last["signal_label"]}</div><div class="mini">Driver: {last["main_driver"]}</div></div>
    <div class="card"><div class="label">Prob Bear</div><div class="value bear">{(last["prob_bear"] * 100.0 if last["prob_bear"] is not None else float('nan')):.1f}%</div><div class="mini">Probabilidade de viés baixista</div></div>
    <div class="card"><div class="label">Prob Bull</div><div class="value bull">{(last["prob_bull"] * 100.0 if last["prob_bull"] is not None else float('nan')):.1f}%</div><div class="mini">Complementar ao bear</div></div>
    <div class="card"><div class="label">Score Final</div><div class="value">{last["score_final"] if last["score_final"] is not None else "N/A"}</div><div class="mini">Suavização EWM do score bruto</div></div>
    <div class="card"><div class="label">Backtest</div><div class="value">{bt_summary.get("accuracy", float("nan")):.1%}</div><div class="mini">N={bt_summary.get("n", 0)} | Bear={bt_summary.get("bear_precision", float("nan")):.1%} | Bull={bt_summary.get("bull_precision", float("nan")):.1%}</div></div>
  </section>

  <section class="cards components">
    <div class="card"><div class="label">Surface</div><div class="value">{last["score_surface"] if last["score_surface"] is not None else "N/A"}</div><div class="mini">ATM IV {last["atm_iv"]} | RR25 {last["rr25"]}</div></div>
    <div class="card"><div class="label">Flow</div><div class="value">{last["score_flow"] if last["score_flow"] is not None else "N/A"}</div><div class="mini">ΔFlow {last["delta_flow"]} | Vega {last["vega_flow"]}</div></div>
    <div class="card"><div class="label">Greeks</div><div class="value">{last["score_greeks"] if last["score_greeks"] is not None else "N/A"}</div><div class="mini">Net GEX {last["net_gex"]} | Net DEX {last["net_dex"]}</div></div>
    <div class="card"><div class="label">Regime</div><div class="value">{last["score_regime"] if last["score_regime"] is not None else "N/A"}</div><div class="mini">Term slope {last["term_slope"]} | BF25 {last["bf25"]}</div></div>
  </section>

  <section class="stack">
    <div class="panel">
      <div class="panel-h">Histórico de score e probabilidade</div>
      <div class="chart-wrap tall"><canvas id="scoreChart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-h">Blocos do modelo</div>
      <div class="chart-wrap"><canvas id="blocksChart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-h">Tabela diária</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Data</th><th>Sinal</th><th>Prob Bear</th><th>Score</th><th>ATM IV</th><th>RR25</th><th>BF25</th><th>ΔFlow</th><th>Vega</th><th>Net GEX</th><th>Net DEX</th><th>Hit</th></tr>
          </thead>
          <tbody id="rowsBody"></tbody>
        </table>
      </div>
    </div>
    <div class="panel config-box">
      <details>
        <summary>Configuração ativa</summary>
        <div class="cfg-intro">Parâmetros usados pelo motor nesta execução. Abra para ver o valor de cada item e o papel dele dentro do modelo.</div>
        <div class="cfg-grid" id="cfgBody"></div>
      </details>
    </div>
  </section>

  <div class="foot">Gerado em {generated_at}</div>
</main>
<script>
const ROWS = {rows_json};
const CFG = {cfg_json};
const CFG_HELP = {cfg_help_json};

function isDark() {{
  return document.documentElement.getAttribute('data-theme') === 'dark';
}}
function txt2() {{
  return getComputedStyle(document.documentElement).getPropertyValue('--text2');
}}
function gridColor() {{
  return isDark() ? 'rgba(139,148,158,.18)' : 'rgba(74,74,72,.14)';
}}
function fmt(v, d=4) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  return Number(v).toLocaleString('pt-BR', {{ minimumFractionDigits:d, maximumFractionDigits:d }});
}}
function fmtPct(v) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  return (Number(v) * 100).toLocaleString('pt-BR', {{ minimumFractionDigits:1, maximumFractionDigits:1 }}) + '%';
}}
function tone(signal) {{
  if (String(signal).includes('BEAR')) return 'bear';
  if (String(signal).includes('BULL')) return 'bull';
  return 'neu';
}}
function parsePtDate(value) {{
  const [dd, mm, yyyy] = String(value).split('/').map(Number);
  return new Date(yyyy, (mm || 1) - 1, dd || 1).getTime();
}}

let scoreChart = null;
let blocksChart = null;

function buildCharts() {{
  const chartRows = [...ROWS].sort((a, b) => parsePtDate(a.date) - parsePtDate(b.date));
  const labels = chartRows.map(r => r.date);
  if (scoreChart) scoreChart.destroy();
  if (blocksChart) blocksChart.destroy();

  scoreChart = new Chart(document.getElementById('scoreChart'), {{
    type:'line',
    data:{{
      labels,
      datasets:[
        {{ label:'Score Final', data:chartRows.map(r => r.score_final), borderColor:'#0969DA', backgroundColor:'rgba(9,105,218,.10)', fill:false, tension:.25, pointRadius:2 }},
        {{ label:'Prob Bear', data:chartRows.map(r => r.prob_bear), borderColor:'#CF222E', backgroundColor:'rgba(207,34,46,.08)', fill:false, tension:.25, pointRadius:2 }},
        {{ label:'Prob Bull', data:chartRows.map(r => r.prob_bull), borderColor:'#1A7F37', backgroundColor:'rgba(26,127,55,.08)', fill:false, tension:.25, pointRadius:2 }},
      ],
    }},
    options:{{
      responsive:true,
      maintainAspectRatio:false,
      plugins:{{ legend:{{ labels:{{ color:txt2() }} }} }},
      interaction:{{ mode:'index', intersect:false }},
      scales:{{
        x:{{ reverse:false, ticks:{{ color:txt2(), maxTicksLimit:10 }}, grid:{{ color:gridColor() }} }},
        y:{{ ticks:{{ color:txt2() }}, grid:{{ color:gridColor() }} }},
      }},
    }},
  }});

  blocksChart = new Chart(document.getElementById('blocksChart'), {{
    type:'line',
    data:{{
      labels,
      datasets:[
        {{ label:'Surface', data:chartRows.map(r => r.score_surface), borderColor:'#7c3aed', tension:.25, pointRadius:2 }},
        {{ label:'Flow', data:chartRows.map(r => r.score_flow), borderColor:'#CF222E', tension:.25, pointRadius:2 }},
        {{ label:'Greeks', data:chartRows.map(r => r.score_greeks), borderColor:'#B8720A', tension:.25, pointRadius:2 }},
        {{ label:'Regime', data:chartRows.map(r => r.score_regime), borderColor:'#1A7F37', tension:.25, pointRadius:2 }},
      ],
    }},
    options:{{
      responsive:true,
      maintainAspectRatio:false,
      plugins:{{ legend:{{ labels:{{ color:txt2() }} }} }},
      interaction:{{ mode:'index', intersect:false }},
      scales:{{
        x:{{ reverse:false, ticks:{{ color:txt2(), maxTicksLimit:10 }}, grid:{{ color:gridColor() }} }},
        y:{{ ticks:{{ color:txt2() }}, grid:{{ color:gridColor() }} }},
      }},
    }},
  }});
}}

function renderRows() {{
  document.getElementById('rowsBody').innerHTML = ROWS.map(r => `
    <tr>
      <td><strong>${{r.date}}</strong></td>
      <td class="${{tone(r.signal_label)}}">${{r.signal_label}}</td>
      <td>${{fmtPct(r.prob_bear)}}</td>
      <td>${{fmt(r.score_final)}}</td>
      <td>${{fmt(r.atm_iv)}}</td>
      <td>${{fmt(r.rr25)}}</td>
      <td>${{fmt(r.bf25)}}</td>
      <td>${{fmt(r.delta_flow,2)}}</td>
      <td>${{fmt(r.vega_flow,2)}}</td>
      <td>${{fmt(r.net_gex,2)}}</td>
      <td>${{fmt(r.net_dex,2)}}</td>
      <td>${{r.hit === null ? 'N/A' : r.hit}}</td>
    </tr>`).join('');
}}

function renderCfg() {{
  document.getElementById('cfgBody').innerHTML = Object.entries(CFG).map(([k,v]) => `
    <article class="cfg-item">
      <div class="cfg-top">
        <div class="cfg-key">${{k}}</div>
        <div class="cfg-val">${{typeof v === 'number' ? fmt(v,4) : String(v)}}</div>
      </div>
      <div class="cfg-desc">${{CFG_HELP[k] || 'Sem descrição cadastrada.'}}</div>
    </article>`).join('');
}}

function toggleTheme() {{
  const dark = isDark();
  document.documentElement.setAttribute('data-theme', dark ? 'light' : 'dark');
  localStorage.setItem('bova11-theme', dark ? 'light' : 'dark');
  buildCharts();
}}

(function() {{
  const saved = localStorage.getItem('bova11-theme') || 'light';
  if (saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
  document.getElementById('theme-toggle').onclick = toggleTheme;
  renderRows();
  renderCfg();
  buildCharts();
}})();
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="IV Skew Signal — HTML")
    parser.add_argument("--data-dir", type=str, default=DEFAULT_DATA_DIR, help="Diretório com CSVs")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT_FILE, help="HTML de saída")
    parser.add_argument("--pasta", type=str, default=None, help="Alias legado para --data-dir")
    parser.add_argument("--spot-history-file", type=str, default=DEFAULT_SPOT_HISTORY_FILE, help="Histórico manual de spot")
    parser.add_argument("--history-json", type=str, default=DEFAULT_HISTORY_FILE, help="JSON histórico")
    parser.add_argument("--vol-minimo", type=float, default=50.0, help="Volume mínimo")
    parser.add_argument("--z-window", type=int, default=20, help="Janela z-score")
    parser.add_argument("--smooth-window", type=int, default=3, help="Janela EWM")
    args, _unknown = parser.parse_known_args()

    data_dir = args.pasta if args.pasta else args.data_dir
    if not os.path.isdir(data_dir):
        print(f"[ERRO] Diretório não encontrado: {data_dir}")
        sys.exit(1)

    cfg = Config(min_volume_total=args.vol_minimo, z_window=args.z_window, smooth_window=args.smooth_window)
    spot_df = load_manual_spot_df(args.spot_history_file)
    daily = process_folder(data_dir, spot_df=spot_df, cfg=cfg)
    if daily.empty:
        print("[ERRO] Nenhum dado válido após filtros.")
        sys.exit(1)

    feats = build_features(daily, cfg=cfg)
    if feats.empty:
        print("[ERRO] Sem features válidas.")
        sys.exit(1)

    if "close" in feats.columns and feats["close"].notna().any():
        feats["next_close"] = feats["close"].shift(-1)
    else:
        feats["next_close"] = np.nan
    bt = run_simple_backtest(feats)
    hit_map = {}
    if not bt.empty:
        for _, row in bt.iterrows():
            hit_map[row["date"]] = row["hit"]
    feats["hit"] = feats["date"].map(hit_map)

    rows = build_rows_for_frontend(feats)
    html = build_html(rows, cfg, backtest_summary(bt), args.output)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.makedirs(os.path.dirname(args.history_json), exist_ok=True)
    with open(args.history_json, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)

    print(f"✅ IV Skew Signal gerado: {args.output}")
    print(f"   Sessões: {len(rows)} | Último sinal: {rows[0]['signal_label']} | Driver: {rows[0]['main_driver']}")


if __name__ == "__main__":
    main()
