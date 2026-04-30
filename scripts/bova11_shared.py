#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Helpers compartilhados para o ecossistema BOVA11.

Objetivos:
- normalizar parsing BR / tags de data
- persistir e resolver spot manual
- centralizar fórmulas canônicas de exposições
- evitar proxies silenciosos quando o preço não estiver disponível
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple


MESES_NUM = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def parse_br_number(raw, none_on_blank: bool = False):
    """Converte número BR com suporte a k/M/%; opcionalmente preserva blanks."""
    if raw is None:
        return None if none_on_blank else 0.0

    if not isinstance(raw, str):
        try:
            if raw != raw:
                return None if none_on_blank else 0.0
            return float(raw)
        except Exception:
            return None if none_on_blank else 0.0

    s = raw.strip().replace("\r", "").rstrip("%")
    if s in ("", "-", "--", "None", "nan"):
        return None if none_on_blank else 0.0

    mult = 1.0
    sl = s.lower()
    if sl.endswith("k"):
        mult = 1_000.0
        s = s[:-1]
    elif sl.endswith("m"):
        mult = 1_000_000.0
        s = s[:-1]

    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s) * mult
    except Exception:
        return None if none_on_blank else 0.0


def normalize_tag(tag: str) -> str:
    if not tag:
        return ""
    return re.sub(r"(pos|pre)([a-z]{3})$", r"\2", str(tag).lower())


def tag_sort_key(tag: str) -> Tuple[int, int, str]:
    norm = normalize_tag(tag)
    m = re.match(r"(\d{1,2})([a-z]{3})$", norm)
    if not m:
        return (99, 99, norm)
    return (MESES_NUM.get(m.group(2), 99), int(m.group(1)), norm)


def tag_to_iso(tag: str, year: Optional[int] = None) -> Optional[str]:
    norm = normalize_tag(tag)
    m = re.match(r"(\d{1,2})([a-z]{3})$", norm)
    if not m:
        return None
    use_year = int(year or datetime.now().year)
    month = MESES_NUM.get(m.group(2))
    if month is None:
        return None
    return f"{use_year}-{month:02d}-{int(m.group(1)):02d}"


def load_json(path: str, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_spot_history(path: str) -> Dict[str, dict]:
    payload = load_json(path, {})
    return payload if isinstance(payload, dict) else {}


def upsert_spot_history(
    path: str,
    ref_date: str,
    ref_tag: str,
    spot: float,
    source: str = "runner_manual",
    timestamp: Optional[str] = None,
) -> Dict[str, dict]:
    history = load_spot_history(path)
    now_iso = timestamp or datetime.now().isoformat(timespec="seconds")
    history[str(ref_date)] = {
        "date": str(ref_date),
        "tag": str(ref_tag),
        "spot": float(spot),
        "source": str(source),
        "timestamp": now_iso,
    }
    save_json(path, history)
    return history


def get_spot_record(
    history: Dict[str, dict],
    ref_date: Optional[str] = None,
    ref_tag: Optional[str] = None,
) -> Optional[dict]:
    if ref_date:
        rec = history.get(str(ref_date))
        if isinstance(rec, dict):
            return rec

    if ref_tag:
        tag_norm = normalize_tag(ref_tag)
        candidates = []
        for rec in history.values():
            if not isinstance(rec, dict):
                continue
            if normalize_tag(rec.get("tag", "")) == tag_norm:
                candidates.append(rec)
        if candidates:
            candidates.sort(key=lambda x: x.get("timestamp", ""))
            return candidates[-1]

    return None


def resolve_spot(
    spot: Optional[float] = None,
    spot_history_file: Optional[str] = None,
    ref_date: Optional[str] = None,
    ref_tag: Optional[str] = None,
    stored_spot: Optional[float] = None,
    fetcher: Optional[Callable[[str], Optional[float]]] = None,
) -> Tuple[Optional[float], str, Optional[str]]:
    """Resolve spot sem inventar proxies silenciosos."""
    if spot is not None and float(spot) > 0:
        return float(spot), "cli", None

    history = load_spot_history(spot_history_file) if spot_history_file else {}
    rec = get_spot_record(history, ref_date=ref_date, ref_tag=ref_tag)
    if rec and rec.get("spot") is not None:
        try:
            val = float(rec["spot"])
            if val > 0:
                return val, f"spot_history:{rec.get('source', 'manual')}", None
        except Exception:
            pass

    if stored_spot is not None:
        try:
            val = float(stored_spot)
            if val > 0:
                return val, "stored_history", None
        except Exception:
            pass

    if fetcher and ref_date:
        try:
            fetched = fetcher(ref_date)
        except Exception as exc:
            fetched = None
            warn = f"Falha ao consultar preço externo: {exc}"
        else:
            warn = None
        if fetched is not None:
            try:
                val = float(fetched)
                if val > 0:
                    return val, "yfinance", warn
            except Exception:
                pass
        return None, "unresolved", warn or f"Preço indisponível para {ref_date}"

    return None, "unresolved", "Preço indisponível — informe spot manual ou forneça histórico confiável."


def build_manual_spot_map(path: Optional[str]) -> Dict[str, float]:
    history = load_spot_history(path) if path else {}
    out: Dict[str, float] = {}
    for key, rec in history.items():
        if not isinstance(rec, dict):
            continue
        try:
            val = float(rec.get("spot"))
        except Exception:
            continue
        if val > 0:
            out[str(key)] = val
    return out


def calc_max_pain(strike_map: Dict[float, dict]) -> Tuple[list, Optional[float], Optional[float]]:
    strikes = sorted(float(s) for s in strike_map.keys() if float(s) > 0)
    if not strikes:
        return [], None, None

    curve = []
    best_strike = None
    best_loss = None
    for test in strikes:
        call_loss = 0.0
        put_loss = 0.0
        for s in strikes:
            row = strike_map[s]
            call_loss += float(row.get("call_oi", 0.0)) * max(test - s, 0.0)
            put_loss += float(row.get("put_oi", 0.0)) * max(s - test, 0.0)
        total_loss = call_loss + put_loss
        curve.append({
            "strike": test,
            "call_loss": call_loss,
            "put_loss": put_loss,
            "total_loss": total_loss,
        })
        if best_loss is None or total_loss < best_loss:
            best_loss = total_loss
            best_strike = test

    return curve, best_strike, best_loss


def calc_gex_components(call_gamma, put_gamma, call_oi, put_oi, spot: float) -> Tuple[float, float, float]:
    factor = (float(spot) ** 2) / 100.0
    call = float(call_gamma or 0.0) * float(call_oi or 0.0) * factor
    put = -float(put_gamma or 0.0) * float(put_oi or 0.0) * factor
    return call, put, call + put


def calc_dex_components(call_delta, put_delta, call_oi, put_oi, spot: float) -> Tuple[float, float, float]:
    call = float(call_delta or 0.0) * float(call_oi or 0.0) * float(spot)
    put = float(put_delta or 0.0) * float(put_oi or 0.0) * float(spot)
    return call, put, call + put


def calc_tex_components(call_theta, put_theta, call_oi, put_oi) -> Tuple[float, float, float]:
    call = float(call_theta or 0.0) * float(call_oi or 0.0) * 100.0
    put = float(put_theta or 0.0) * float(put_oi or 0.0) * 100.0
    return call, put, call + put


def calc_vex_components(call_vega, put_vega, call_oi, put_oi) -> Tuple[float, float, float]:
    call = float(call_vega or 0.0) * float(call_oi or 0.0) * 100.0
    put = float(put_vega or 0.0) * float(put_oi or 0.0) * 100.0
    return call, put, call + put


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expiry_label_to_iso(label: str, year: Optional[int] = None) -> Optional[str]:
    if not label:
        return None
    m = re.search(r"(\d{1,2})\s+([a-z]{3})", str(label).lower())
    if not m:
        return None
    month = MESES_NUM.get(m.group(2))
    if month is None:
        return None
    use_year = int(year or datetime.now().year)
    return f"{use_year}-{month:02d}-{int(m.group(1)):02d}"


def _date_from_iso(iso_date: Optional[str]):
    if not iso_date:
        return None
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").date()
    except Exception:
        return None


def _bs_d1_d2(spot: float, strike: float, sigma: float, t: float, r: float = 0.0, q: float = 0.0):
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        return None, None
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + ((r - q) + 0.5 * sigma * sigma) * t) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    return d1, d2


def bs_gamma(spot: float, strike: float, sigma: float, t: float, r: float = 0.0, q: float = 0.0) -> Optional[float]:
    d1, _ = _bs_d1_d2(spot, strike, sigma, t, r, q)
    if d1 is None:
        return None
    return math.exp(-q * t) * norm_pdf(d1) / (spot * sigma * math.sqrt(t))


def bs_delta(
    spot: float,
    strike: float,
    sigma: float,
    t: float,
    option_type: str,
    r: float = 0.0,
    q: float = 0.0,
) -> Optional[float]:
    d1, _ = _bs_d1_d2(spot, strike, sigma, t, r, q)
    if d1 is None:
        return None
    eq = math.exp(-q * t)
    if str(option_type).lower() == "call":
        return eq * norm_cdf(d1)
    return eq * (norm_cdf(d1) - 1.0)


def bs_vanna(spot: float, strike: float, sigma: float, t: float, r: float = 0.0, q: float = 0.0) -> Optional[float]:
    d1, d2 = _bs_d1_d2(spot, strike, sigma, t, r, q)
    if d1 is None or d2 is None or sigma <= 0:
        return None
    return -math.exp(-q * t) * norm_pdf(d1) * d2 / sigma


def bs_theta_per_day(
    spot: float,
    strike: float,
    sigma: float,
    t: float,
    option_type: str,
    r: float = 0.0,
    q: float = 0.0,
    day_basis: float = 365.0,
) -> Optional[float]:
    d1, d2 = _bs_d1_d2(spot, strike, sigma, t, r, q)
    if d1 is None or d2 is None or sigma <= 0 or t <= 0 or day_basis <= 0:
        return None

    eq = math.exp(-q * t)
    front = -(spot * eq * norm_pdf(d1) * sigma) / (2.0 * math.sqrt(t))
    if str(option_type).lower() == "call":
        theta_annual = front - (q * spot * eq * norm_cdf(d1)) - (r * strike * math.exp(-r * t) * norm_cdf(d2))
    else:
        theta_annual = front + (q * spot * eq * norm_cdf(-d1)) + (r * strike * math.exp(-r * t) * norm_cdf(-d2))
    return theta_annual / day_basis


def bs_charm_delta(
    spot: float,
    strike: float,
    sigma: float,
    t: float,
    r: float = 0.0,
    q: float = 0.0,
    option_type: Optional[str] = None,
) -> Optional[float]:
    d1, d2 = _bs_d1_d2(spot, strike, sigma, t, r, q)
    if d1 is None or d2 is None or sigma <= 0 or t <= 0:
        return None
    root_t = math.sqrt(t)
    eq = math.exp(-q * t)
    charm_base = norm_pdf(d1) * ((2.0 * (r - q) * t) - (d2 * sigma * root_t)) / (2.0 * t * sigma * root_t)
    opt = str(option_type or "").lower()
    if opt == "call":
        return -eq * ((-q * norm_cdf(d1)) + charm_base)
    if opt == "put":
        return -eq * ((-q * (norm_cdf(d1) - 1.0)) + charm_base)
    return -eq * charm_base


def calc_convexity_decomposition(
    *,
    strike: float,
    option_type: str,
    spot_d: float,
    spot_d1: float,
    delta_d: float,
    delta_d1: float,
    gamma_d: Optional[float] = None,
    gamma_d1: Optional[float] = None,
    iv_d: Optional[float] = None,
    iv_d1: Optional[float] = None,
    expiry_label: Optional[str] = None,
    session_date_d: Optional[str] = None,
    session_date_d1: Optional[str] = None,
    r: float = 0.0,
):
    """Decompõe ΔDelta em contribuições de gamma, vanna, charm e residual."""
    def _clean(v: Optional[float], eps: float = 1e-12) -> float:
        if v is None:
            return 0.0
        v = float(v)
        return 0.0 if abs(v) < eps else v

    delta_delta = float(delta_d) - float(delta_d1)
    delta_spot = float(spot_d) - float(spot_d1)

    expiry_iso = expiry_label_to_iso(expiry_label)
    expiry_date = _date_from_iso(expiry_iso)
    sess_d = _date_from_iso(session_date_d)
    sess_d1 = _date_from_iso(session_date_d1)

    dte_d = None
    dte_d1 = None
    if expiry_date and sess_d:
        dte_d = max((expiry_date - sess_d).days, 0) / 365.0
    if expiry_date and sess_d1:
        dte_d1 = max((expiry_date - sess_d1).days, 0) / 365.0

    valid_dtes = [v for v in (dte_d, dte_d1) if v is not None and v > 0]
    t_ref = max(sum(valid_dtes) / len(valid_dtes), 1.0 / 365.0) if valid_dtes else None
    delta_t = (dte_d - dte_d1) if (dte_d is not None and dte_d1 is not None) else None

    valid_sigmas = [v / 100.0 for v in (iv_d, iv_d1) if v is not None and v > 0]
    sigma_ref = sum(valid_sigmas) / len(valid_sigmas) if valid_sigmas else None
    delta_sigma = ((iv_d - iv_d1) / 100.0) if (iv_d is not None and iv_d1 is not None) else None

    spot_ref = max((float(spot_d) + float(spot_d1)) / 2.0, 1e-9)
    gamma_ref = None
    valid_gammas = [g for g in (gamma_d, gamma_d1) if g is not None]
    if valid_gammas:
        gamma_ref = sum(valid_gammas) / len(valid_gammas)
    elif sigma_ref is not None and t_ref is not None:
        gamma_ref = bs_gamma(spot_ref, float(strike), sigma_ref, t_ref, r)

    vanna_ref = None
    charm_ref = None
    if sigma_ref is not None and t_ref is not None:
        vanna_ref = bs_vanna(spot_ref, float(strike), sigma_ref, t_ref, r)
        charm_ref = bs_charm_delta(spot_ref, float(strike), sigma_ref, t_ref, r)

    gamma_contrib = _clean((gamma_ref or 0.0) * delta_spot)
    vanna_contrib = _clean((vanna_ref or 0.0) * delta_sigma if delta_sigma is not None else 0.0)
    charm_contrib = _clean((charm_ref or 0.0) * delta_t if delta_t is not None else 0.0)
    residual = _clean(delta_delta - gamma_contrib - vanna_contrib - charm_contrib)

    return {
        "option_type": str(option_type).upper(),
        "delta_delta": delta_delta,
        "delta_spot": delta_spot,
        "delta_sigma": delta_sigma,
        "delta_t": delta_t,
        "dte_d": dte_d,
        "dte_d1": dte_d1,
        "t_ref": t_ref,
        "gamma_ref": _clean(gamma_ref),
        "vanna_ref": _clean(vanna_ref),
        "charm_ref": _clean(charm_ref),
        "gamma_contrib": gamma_contrib,
        "vanna_contrib": vanna_contrib,
        "charm_contrib": charm_contrib,
        "residual": residual,
        "abs_gamma": abs(gamma_contrib),
        "abs_vanna": abs(vanna_contrib),
        "abs_charm": abs(charm_contrib),
        "abs_residual": abs(residual),
    }
