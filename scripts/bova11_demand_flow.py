#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 — Demand Flow (motor v3 transplantado do anexo)
======================================================

Substitui a formulação antiga de ADI/PDI/IDS/DFP pelo score composto do
arquivo `bova_options_demand_indicator_v3.py`, preservando a integração do
dashboard atual:

- descoberta dinâmica por tag/ref-date
- filtro de família do vencimento
- histórico JSON
- HTML auto-contido para o dashboard
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from .bova11_expiry_family import collapse_option_rows_by_strike, filter_expiry_family
except ImportError:
    from bova11_expiry_family import collapse_option_rows_by_strike, filter_expiry_family


_BASEDIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(_BASEDIR, "..", "history", "bova11_demand_flow.json")

WEIGHTS = {
    "skew": 0.15,
    "premium": 0.20,
    "oi": 0.20,
    "iv": 0.15,
    "gex": 0.12,
    "charm": 0.08,
    "vanna": 0.10,
}

VENC_DATES = {
    "27 mar w4": "2026-03-27",
    "2 abr w1": "2026-04-02",
    "10 abr w2": "2026-04-10",
    "17 abr mensal": "2026-04-17",
    "24 abr w2": "2026-04-24",
    "30 abr w5": "2026-04-30",
    "15 mai mensal": "2026-05-15",
}

MONTHS_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def parse_num(x) -> float:
    if pd.isna(x):
        return np.nan
    s = str(x).strip().replace("\xa0", "").replace(" ", "")
    mult = 1.0
    if s.endswith(("k", "K")):
        mult = 1e3
        s = s[:-1]
    elif s.endswith(("M", "m")):
        mult = 1e6
        s = s[:-1]
    if s.endswith("%"):
        mult *= 0.01
        s = s[:-1]
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s) * mult
    except (ValueError, OverflowError):
        return np.nan


def robust_z(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    med = s.median()
    mad = (s - med).abs().median()
    if pd.isna(mad) or mad == 0:
        denom = s.std()
        denom = denom if denom and not pd.isna(denom) else 1.0
        return (s - med) / denom
    return 0.6745 * (s - med) / mad


def classify(score: float) -> str:
    if score >= 0.25:
        return "Bear"
    if score <= -0.25:
        return "Bull"
    return "Base"


def _safe_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _fmt_label(raw: str) -> str:
    meses = {
        "jan": "Jan", "fev": "Fev", "mar": "Mar", "abr": "Abr",
        "mai": "Mai", "jun": "Jun", "jul": "Jul", "ago": "Ago",
        "set": "Set", "out": "Out", "nov": "Nov", "dez": "Dez",
    }
    parts = raw.strip().split()
    out = []
    for part in parts:
        lp = part.lower()
        if lp in meses:
            out.append(meses[lp])
        elif lp.startswith("w") and lp[1:].isdigit():
            out.append("— " + lp.upper())
        elif lp == "mensal":
            out.append("— Mensal")
        else:
            out.append(part.capitalize())
    return " ".join(out)


def _resolve_exp_date(label_raw: str, ref_year: int) -> str:
    exp_date = VENC_DATES.get(label_raw)
    if exp_date:
        return exp_date
    match = re.match(r"(\d{1,2})\s+([a-z]{3})", label_raw)
    if not match:
        return ""
    month = MONTHS_PT.get(match.group(2), 1)
    return f"{ref_year}-{month:02d}-{int(match.group(1)):02d}"


def parse_vencimento_for_sort(venc_str: str) -> tuple:
    match = re.match(r"(\d{1,2})\s+([a-z]{3})", venc_str.lower())
    if not match:
        return (99, 99)
    return (MONTHS_PT.get(match.group(2), 99), int(match.group(1)))


def read_close(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="latin1", sep=";")
    for col in df.columns:
        if col not in ("Ativo", "Ativo.1"):
            df[col] = df[col].map(parse_num)
    return pd.DataFrame({
        "call_ticker": df["Ativo"].astype(str).str.strip(),
        "put_ticker": df["Ativo.1"].astype(str).str.strip(),
        "strike": df["Strike"],
        "call_oi": df["C. Abertos"],
        "call_delta": df["Delta"],
        "call_gamma": df["Gamma"],
        "call_theta": df["Theta"],
        "call_vega": df["Vega"],
        "call_iv": df["Vol Impl"],
        "call_bid": df["Bid"],
        "call_ask": df["Ask"],
        "put_bid": df["Bid.1"],
        "put_ask": df["Ask.1"],
        "put_iv": df["Vol Impl.1"],
        "put_vega": df["Vega.1"],
        "put_theta": df["Theta.1"],
        "put_gamma": df["Gamma.1"],
        "put_delta": df["Delta.1"],
        "put_oi": df["C. Abertos.1"],
    })


def read_volume(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="latin1", sep=";")
    for col in df.columns:
        if col not in ("Ativo", "Ativo.1"):
            df[col] = df[col].map(parse_num)
    out = pd.DataFrame({
        "call_ticker": df.iloc[:, 0].astype(str).str.strip(),
        "put_ticker": df.iloc[:, 10].astype(str).str.strip() if df.shape[1] > 10 else "",
        "strike": df.iloc[:, 5],
        "call_vol": df.iloc[:, 1],
        "put_vol": df.iloc[:, 9],
    })
    return out


def collapse_volume_rows_by_strike(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    grouped = (
        df[df["strike"] > 0]
        .groupby("strike", as_index=False)
        .agg({
            "call_ticker": "first",
            "put_ticker": "first",
            "call_vol": "sum",
            "put_vol": "sum",
        })
        .sort_values("strike")
        .reset_index(drop=True)
    )
    return grouped


def infer_spot(df: pd.DataFrame) -> float:
    valid = df.dropna(subset=["call_delta"])
    if valid.empty:
        return float(df["strike"].median())
    idx = (valid["call_delta"] - 0.5).abs().idxmin()
    return float(valid.loc[idx, "strike"])


def build_day(
    close_path: Path,
    vol_path: Optional[Path],
    expiry_label: str,
    session_date: str,
    exp_date: str,
    exp_type: str,
    spot_override: Optional[float] = None,
) -> pd.DataFrame:
    close_df = read_close(close_path)
    close_df = filter_expiry_family(close_df, exp_date, exp_type)
    close_df = collapse_option_rows_by_strike(close_df)
    if close_df.empty:
        return close_df

    if vol_path and vol_path.exists():
        vol_df = read_volume(vol_path)
        vol_df = filter_expiry_family(vol_df, exp_date, exp_type)
        vol_df = collapse_volume_rows_by_strike(vol_df)
    else:
        vol_df = pd.DataFrame(columns=["strike", "call_vol", "put_vol"])

    df = close_df.merge(vol_df[["strike", "call_vol", "put_vol"]] if not vol_df.empty else vol_df, on="strike", how="left")
    df["call_vol"] = pd.to_numeric(df.get("call_vol"), errors="coerce").fillna(0.0)
    df["put_vol"] = pd.to_numeric(df.get("put_vol"), errors="coerce").fillna(0.0)

    spot = float(spot_override) if spot_override else infer_spot(df)
    df["expiry"] = expiry_label
    df["date"] = session_date
    df["spot_proxy"] = spot
    df["moneyness"] = df["strike"] / max(spot, 1e-9) - 1.0

    for side in ("call", "put"):
        df[f"{side}_mid"] = df[[f"{side}_bid", f"{side}_ask"]].mean(axis=1)
        df[f"{side}_premium_notional"] = df[f"{side}_mid"].fillna(0.0) * df[f"{side}_vol"].fillna(0.0)
        sign = 1.0 if side == "call" else -1.0
        df[f"{side}_net_gex"] = sign * (spot ** 2) * df[f"{side}_gamma"].fillna(0.0) * df[f"{side}_oi"].fillna(0.0)
        df[f"{side}_gex_abs"] = df[f"{side}_net_gex"].abs()
        df[f"{side}_charm_abs"] = df[f"{side}_theta"].abs().fillna(0.0) * df[f"{side}_oi"].fillna(0.0)
        df[f"{side}_vanna_proxy"] = (
            df[f"{side}_vega"].fillna(0.0) *
            df[f"{side}_delta"].abs().fillna(0.0) *
            df[f"{side}_oi"].fillna(0.0)
        )

    df["net_gex"] = df["call_net_gex"] + df["put_net_gex"]
    return df.sort_values("strike").reset_index(drop=True)


def score_day(df: pd.DataFrame) -> Dict[str, object]:
    metrics = pd.DataFrame(index=df.index)
    metrics["skew"] = df["put_mid"] - df["call_mid"]
    metrics["premium"] = df["put_premium_notional"] - df["call_premium_notional"]
    metrics["oi"] = df["put_oi"] - df["call_oi"]
    metrics["iv"] = df["put_iv"] - df["call_iv"]
    metrics["gex"] = -df["net_gex"]
    metrics["charm"] = df["put_charm_abs"] - df["call_charm_abs"]
    metrics["vanna"] = df["put_vanna_proxy"] - df["call_vanna_proxy"]
    metrics = metrics.fillna(0.0)

    z = metrics.apply(robust_z)
    atm_w = np.exp(-10.0 * df["moneyness"].abs())
    total_w = atm_w.sum()
    if total_w > 0:
        atm_w = atm_w / total_w

    components = {col: float((z[col] * atm_w).sum()) for col in z.columns}
    total_score = sum(WEIGHTS[k] * components.get(k, 0.0) for k in WEIGHTS)

    row_score = pd.Series(0.0, index=z.index)
    for key, weight in WEIGHTS.items():
        row_score = row_score + weight * z[key]

    detail = pd.DataFrame({
        "strike": df["strike"].astype(float),
        "moneyness": df["moneyness"].astype(float),
        "atm_weight": atm_w.astype(float),
        "row_score": row_score.astype(float),
        "z_skew": z["skew"].astype(float),
        "z_premium": z["premium"].astype(float),
        "z_oi": z["oi"].astype(float),
        "z_iv": z["iv"].astype(float),
        "z_gex": z["gex"].astype(float),
        "z_charm": z["charm"].astype(float),
        "z_vanna": z["vanna"].astype(float),
    }).sort_values("strike").reset_index(drop=True)

    dominant_component = max(components, key=lambda k: abs(components[k])) if components else "N/A"
    return {
        "score": float(total_score),
        "signal": classify(float(total_score)),
        "components": components,
        "dominant_component": dominant_component,
        "spot_proxy": float(df["spot_proxy"].iloc[0]),
        "net_gex": float(df["net_gex"].sum()),
        "gex_abs": float(df["call_gex_abs"].sum() + df["put_gex_abs"].sum()),
        "strikes": int(len(df)),
        "rows": detail.to_dict("records"),
    }


def discover_expirations_with_volume(data_dir: str, ref_tag: str, ref_year: int) -> List[Dict[str, str]]:
    close_map: Dict[str, Dict[str, str]] = {}
    volume_map: Dict[str, str] = {}
    pattern = re.compile(r"^venc\s+(.+?)\s+fechamento\s+\(([^)]+)\)\.csv$", re.IGNORECASE)

    for name in os.listdir(data_dir):
        if not name.lower().endswith(".csv"):
            continue
        match = pattern.match(name)
        if not match:
            continue
        label_raw = match.group(1).strip().lower()
        token = match.group(2).strip().lower()
        full_path = os.path.join(data_dir, name)
        if token == ref_tag.lower():
            close_map[label_raw] = {
                "label_raw": label_raw,
                "label": _fmt_label(label_raw),
                "exp_date": _resolve_exp_date(label_raw, ref_year),
                "exp_type": "Mensal" if "mensal" in label_raw else "Semanal",
                "close": full_path,
            }
        elif token == f"{ref_tag.lower()} volume":
            volume_map[label_raw] = full_path

    out = []
    for label_raw, item in close_map.items():
        item["volume"] = volume_map.get(label_raw, "")
        out.append(item)
    out.sort(key=lambda item: parse_vencimento_for_sort(item["label_raw"]))
    return out


def load_history() -> Dict[str, Dict[str, object]]:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_history(history: Dict[str, Dict[str, object]]) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)


def upsert_history(history: Dict[str, Dict[str, object]], ref_date: str, expiry: str, result: Dict[str, object]) -> None:
    history.setdefault(ref_date, {})
    history[ref_date][expiry] = {
        "score": round(float(result["score"]), 6),
        "signal": result["signal"],
        "spot_proxy": round(float(result["spot_proxy"]), 4),
        "net_gex": round(float(result["net_gex"]), 4),
        "dominant_component": result["dominant_component"],
        "components": {k: round(float(v), 6) for k, v in result["components"].items()},
    }


def build_payload(results: List[Dict[str, object]], history: Dict[str, Dict[str, object]]) -> Dict[str, object]:
    payload = {}
    for item in results:
        result = item["result"]
        payload[item["expiry"]] = {
            "score": round(float(result["score"]), 6),
            "signal": result["signal"],
            "spot_proxy": round(float(result["spot_proxy"]), 4),
            "net_gex": round(float(result["net_gex"]), 2),
            "gex_abs": round(float(result["gex_abs"]), 2),
            "strikes": int(result["strikes"]),
            "dominant_component": result["dominant_component"],
            "components": {k: round(float(v), 6) for k, v in result["components"].items()},
            "rows": result["rows"],
        }

    if payload:
        scores = [v["score"] for v in payload.values()]
        agg_components = {}
        for key in WEIGHTS:
            agg_components[key] = float(np.mean([v["components"].get(key, 0.0) for v in payload.values()]))
        payload["AGG"] = {
            "score": round(float(np.mean(scores)), 6),
            "signal": classify(float(np.mean(scores))),
            "spot_proxy": round(float(np.mean([v["spot_proxy"] for v in payload.values()])), 4),
            "net_gex": round(float(np.sum([v["net_gex"] for v in payload.values()])), 2),
            "gex_abs": round(float(np.sum([v["gex_abs"] for v in payload.values()])), 2),
            "strikes": int(np.sum([v["strikes"] for v in payload.values()])),
            "dominant_component": max(agg_components, key=lambda k: abs(agg_components[k])),
            "components": {k: round(v, 6) for k, v in agg_components.items()},
            "rows": [],
        }

    hist_rows = {}
    for dt in sorted(history.keys()):
        hist_rows[dt] = {}
        for exp, entry in history[dt].items():
            hist_rows[dt][exp] = entry.get("score")
    return {"demand": payload, "history": hist_rows}


def build_html(results: List[Dict[str, object]], history: Dict[str, Dict[str, object]], ref_date: str, ref_tag: str, spot_d: float, spot_d1: float) -> str:
    payload = build_payload(results, history)
    demand_js = _safe_json(payload["demand"])
    history_js = _safe_json(payload["history"])
    exp_keys = [item["expiry"] for item in results]
    exp_keys_js = _safe_json(exp_keys)
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — Demand Flow v3</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {{
  --bg:#FAFAF8;--bg2:#F0EFEC;--border:#D8D7D4;--t1:#1A1A18;--t2:#4A4A48;--t3:#8A8A88;
  --blu:#0969DA;--red:#CF222E;--grn:#1A7F37;--amber:#B8720A;--card:#FFFFFF;--hdr:#F6F8FA;
}}
[data-theme="dark"] {{
  --bg:#0d1117;--bg2:#161b22;--border:#30363d;--t1:#c9d1d9;--t2:#8b949e;--t3:#6e7681;
  --blu:#58a6ff;--red:#f85149;--grn:#3fb950;--amber:#d29922;--card:#161b22;--hdr:#21262d;
}}
* {{ box-sizing:border-box;margin:0;padding:0; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--t1);font-size:14px; }}
.hdr {{ position:sticky;top:0;z-index:10;background:var(--hdr);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;justify-content:space-between;gap:12px;align-items:center; }}
.hdr-title {{ font-size:1.1rem;font-weight:700; }}
.hdr-sub {{ font-size:.8rem;color:var(--t3);margin-top:2px; }}
.theme-btn {{ background:none;border:1px solid var(--border);border-radius:6px;padding:5px 10px;cursor:pointer;color:var(--t2); }}
.exp-wrap {{ padding:12px 20px 0;background:var(--bg2);border-bottom:1px solid var(--border); }}
.exp-tabs {{ display:flex;flex-wrap:wrap;gap:6px;padding-bottom:12px; }}
.exp-tab {{ padding:5px 12px;border-radius:20px;cursor:pointer;font-size:.8rem;font-weight:500;border:1px solid var(--border);color:var(--t2);background:var(--bg); }}
.exp-tab.on {{ background:var(--blu);border-color:var(--blu);color:#fff; }}
.exp-tab.agg.on {{ background:#8250df;border-color:#8250df; }}
.main {{ padding:20px;max-width:1400px;margin:0 auto; }}
.cards {{ display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px; }}
@media(max-width:900px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }} }}
@media(max-width:520px) {{ .cards {{ grid-template-columns:1fr; }} }}
.card,.panel,.chart-panel {{ background:var(--card);border:1px solid var(--border);border-radius:10px; }}
.card {{ padding:14px 16px; }}
.card-label {{ font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--t3);margin-bottom:6px; }}
.card-val {{ font-size:1.38rem;font-weight:700;font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
.card-sub {{ font-size:.75rem;color:var(--t3);margin-top:6px;line-height:1.35; }}
.pos {{ color:var(--red); }} .neg {{ color:var(--grn); }} .neu {{ color:var(--t2); }} .amber {{ color:var(--amber); }}
.two-col {{ display:grid;grid-template-columns:1.05fr .95fr;gap:16px;margin-bottom:20px; }}
@media(max-width:980px) {{ .two-col {{ grid-template-columns:1fr; }} }}
.panel-hdr {{ background:var(--hdr);border-bottom:1px solid var(--border);padding:10px 14px;font-weight:600;font-size:.85rem; }}
.table-wrap {{ max-height:440px;overflow:auto; }}
table {{ width:100%;border-collapse:collapse;font-size:.82rem; }}
th,td {{ padding:7px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap; }}
th:first-child,td:first-child {{ text-align:left; }}
th {{ position:sticky;top:0;background:var(--hdr);color:var(--t3);font-size:.72rem;text-transform:uppercase; }}
.chart-panel {{ padding:16px;margin-bottom:20px; }}
.chart-wrap {{ height:250px; }}
.foot {{ margin-top:16px;padding-top:12px;border-top:1px solid var(--border);font-size:.78rem;color:var(--t3);text-align:center; }}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <div class="hdr-title">BOVA11 — Demand Flow v3</div>
    <div class="hdr-sub">{ref_date} | {ref_tag} | Spot D: {spot_d:.2f} | Spot D-1: {spot_d1:.2f}</div>
  </div>
  <button class="theme-btn" id="theme-toggle">◐</button>
</div>
<div class="exp-wrap"><div class="exp-tabs" id="expTabs"></div></div>
<main class="main">
  <section class="cards" id="cards"></section>
  <section class="two-col">
    <div class="panel">
      <div class="panel-hdr">Detalhe por strike</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Strike</th><th>Score</th><th>Peso ATM</th><th>Skew</th><th>Premium</th><th>OI</th><th>IV</th><th>GEX</th><th>Charm</th><th>Vanna</th></tr>
          </thead>
          <tbody id="detailBody"></tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <div class="panel-hdr">Componentes do score</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Componente</th><th>Score</th><th>Peso</th></tr></thead>
          <tbody id="componentBody"></tbody>
        </table>
      </div>
    </div>
  </section>
  <section class="chart-panel">
    <div class="panel-hdr" style="margin:-16px -16px 14px -16px;border-radius:10px 10px 0 0;">Histórico do score composto</div>
    <div class="chart-wrap"><canvas id="histChart"></canvas></div>
  </section>
  <div class="foot">Motor transplantado de `bova_options_demand_indicator_v3.py` · Gerado em {generated_at}</div>
</main>
<script>
const DEMAND_DATA = {demand_js};
const HISTORY_DATA = {history_js};
const EXP_KEYS = {exp_keys_js};
let curExp = 'AGG';
let histChart = null;

function toggleTheme() {{
  const root = document.documentElement;
  const dark = root.getAttribute('data-theme') === 'dark';
  root.setAttribute('data-theme', dark ? 'light' : 'dark');
  localStorage.setItem('bova11-theme', dark ? 'light' : 'dark');
  render();
}}

function tone(v) {{
  if (v > 0.25) return 'pos';
  if (v < -0.25) return 'neg';
  return 'neu';
}}

function fmt(v, d=4) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  return Number(v).toLocaleString('pt-BR', {{ minimumFractionDigits:d, maximumFractionDigits:d }});
}}

function fmtInt(v) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  return Math.round(Number(v)).toLocaleString('pt-BR');
}}

function renderTabs() {{
  const host = document.getElementById('expTabs');
  host.innerHTML = '';
  const agg = document.createElement('button');
  agg.className = 'exp-tab agg' + (curExp === 'AGG' ? ' on' : '');
  agg.textContent = 'AGREGADO';
  agg.onclick = () => {{ curExp = 'AGG'; render(); }};
  host.appendChild(agg);
  EXP_KEYS.forEach(exp => {{
    const btn = document.createElement('button');
    btn.className = 'exp-tab' + (curExp === exp ? ' on' : '');
    btn.textContent = exp;
    btn.onclick = () => {{ curExp = exp; render(); }};
    host.appendChild(btn);
  }});
}}

function renderCards() {{
  const d = DEMAND_DATA[curExp];
  const host = document.getElementById('cards');
  const cls = tone(d.score);
  host.innerHTML = [
    ['Score Composto', `<span class="${{cls}}">${{fmt(d.score, 4)}}</span>`, `${{d.signal}} · dominante: ${{d.dominant_component}}`],
    ['Spot Proxy', fmt(d.spot_proxy, 2), `${{fmtInt(d.strikes)}} strikes no modelo`],
    ['Net GEX', `<span class="${{tone(-d.net_gex)}}">${{fmt(d.net_gex, 2)}}</span>`, `GEX absoluto: ${{fmt(d.gex_abs, 2)}}`],
    ['Leitura', `<span class="${{cls}}">${{d.signal}}</span>`, `Bear >= +0,25 | Bull <= -0,25`],
  ].map(([label,val,sub]) => `<div class="card"><div class="card-label">${{label}}</div><div class="card-val">${{val}}</div><div class="card-sub">${{sub}}</div></div>`).join('');
}}

function renderComponents() {{
  const d = DEMAND_DATA[curExp];
  const tbody = document.getElementById('componentBody');
  const rows = Object.entries(d.components || {{}}).sort((a,b) => Math.abs(b[1]) - Math.abs(a[1]));
  tbody.innerHTML = rows.map(([k,v]) => `<tr><td>${{k}}</td><td class="${{tone(v)}}">${{fmt(v,4)}}</td><td>${{fmt(({{skew:0.15,premium:0.20,oi:0.20,iv:0.15,gex:0.12,charm:0.08,vanna:0.10}}[k]||0),2)}}</td></tr>`).join('');
}}

function renderDetail() {{
  const d = DEMAND_DATA[curExp];
  const tbody = document.getElementById('detailBody');
  if (!d.rows || !d.rows.length) {{
    tbody.innerHTML = '<tr><td colspan="10">Sem detalhe por strike no agregado.</td></tr>';
    return;
  }}
  tbody.innerHTML = d.rows.map(r => `
    <tr>
      <td><strong>${{fmt(r.strike,0)}}</strong></td>
      <td class="${{tone(r.row_score)}}">${{fmt(r.row_score,3)}}</td>
      <td>${{fmt(r.atm_weight,3)}}</td>
      <td>${{fmt(r.z_skew,3)}}</td>
      <td>${{fmt(r.z_premium,3)}}</td>
      <td>${{fmt(r.z_oi,3)}}</td>
      <td>${{fmt(r.z_iv,3)}}</td>
      <td>${{fmt(r.z_gex,3)}}</td>
      <td>${{fmt(r.z_charm,3)}}</td>
      <td>${{fmt(r.z_vanna,3)}}</td>
    </tr>`).join('');
}}

function buildHistoryChart() {{
  const ctx = document.getElementById('histChart').getContext('2d');
  const dates = Object.keys(HISTORY_DATA).sort();
  if (histChart) histChart.destroy();
  const expSeries = curExp === 'AGG'
    ? [{{
        label: 'Agregado',
        data: dates.map(dt => {{
          const values = Object.values(HISTORY_DATA[dt] || {{}}).map(v => v.score).filter(v => v !== null && v !== undefined);
          if (!values.length) return null;
          return values.reduce((a,b) => a + b, 0) / values.length;
        }}),
        borderColor: '#8250df',
        backgroundColor: 'rgba(130,80,223,.15)',
      }}]
    : [{{
        label: curExp,
        data: dates.map(dt => HISTORY_DATA[dt] && HISTORY_DATA[dt][curExp] ? HISTORY_DATA[dt][curExp].score : null),
        borderColor: '#0969DA',
        backgroundColor: 'rgba(9,105,218,.12)',
      }}];

  histChart = new Chart(ctx, {{
    type: 'line',
    data: {{ labels: dates, datasets: expSeries.map(s => Object.assign(s, {{ tension:0.25, pointRadius:3, spanGaps:true, fill:true }})) }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ labels: {{ color: getComputedStyle(document.documentElement).getPropertyValue('--t2') }} }} }},
      scales: {{
        x: {{ ticks: {{ color: getComputedStyle(document.documentElement).getPropertyValue('--t3') }}, grid: {{ color: 'rgba(128,128,128,.10)' }} }},
        y: {{ ticks: {{ color: getComputedStyle(document.documentElement).getPropertyValue('--t3') }}, grid: {{ color: 'rgba(128,128,128,.10)' }} }},
      }},
    }},
  }});
}}

function render() {{
  renderTabs();
  renderCards();
  renderComponents();
  renderDetail();
  buildHistoryChart();
}}

(function() {{
  const saved = localStorage.getItem('bova11-theme') || 'light';
  if (saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
  document.getElementById('theme-toggle').onclick = toggleTheme;
  render();
}})();
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="BOVA11 Demand Flow v3")
    parser.add_argument("--data-dir", required=True, help="Diretório com CSVs B3")
    parser.add_argument("--output", required=True, help="HTML de saída")
    parser.add_argument("--ref-date", required=True, help="Data ISO de referência")
    parser.add_argument("--ref-tag", required=True, help="Tag do CSV (ex: 28abr)")
    parser.add_argument("--spot-d", required=True, type=float, help="Spot D")
    parser.add_argument("--spot-d1", required=True, type=float, help="Spot D-1")
    args = parser.parse_args()

    ref_year = datetime.strptime(args.ref_date, "%Y-%m-%d").year
    expiries = discover_expirations_with_volume(args.data_dir, args.ref_tag, ref_year)
    if not expiries:
        print(f"[ERRO] Nenhum CSV encontrado para tag {args.ref_tag} em {args.data_dir}")
        sys.exit(1)

    results: List[Dict[str, object]] = []
    for exp in expiries:
        day_df = build_day(
            Path(exp["close"]),
            Path(exp["volume"]) if exp.get("volume") else None,
            exp["label"],
            args.ref_date,
            exp["exp_date"],
            exp["exp_type"],
            spot_override=args.spot_d,
        )
        if day_df.empty:
            print(f"  ⚠ {exp['label']}: sem dados válidos após filtro")
            continue
        result = score_day(day_df)
        results.append({"expiry": exp["label"], "result": result})
        print(f"  {exp['label']}: score={result['score']:+.4f} | sinal={result['signal']} | spot={result['spot_proxy']:.2f}")

    if not results:
        print("[ERRO] Nenhum vencimento processado.")
        sys.exit(1)

    history = load_history()
    for item in results:
        upsert_history(history, args.ref_date, item["expiry"], item["result"])
    save_history(history)

    html = build_html(results, history, args.ref_date, args.ref_tag, args.spot_d, args.spot_d1)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  HTML gerado: {args.output}")


if __name__ == "__main__":
    main()
