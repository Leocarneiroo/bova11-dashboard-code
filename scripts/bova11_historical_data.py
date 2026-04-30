#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Historical Data
----------------------
Consolida métricas históricas diárias a partir dos CSVs de fechamento/volume
(agregado entre vencimentos), persiste em history/bova11_market_history.json
e gera um dashboard HTML estilo histórico com filtros de janela.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple

from bova11_shared import (
    calc_dex_components,
    calc_gex_components,
    calc_max_pain,
    load_json,
    resolve_spot,
)


MESES_NUM = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
MESES_LABEL = {
    1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez",
}


def _p(raw: str) -> float:
    s = str(raw).strip().replace("%", "")
    if s in ("", "-", "--", "None"):
        return 0.0
    mult = 1.0
    if s.endswith("k"):
        s = s[:-1]
        mult = 1_000.0
    elif s.endswith("M"):
        s = s[:-1]
        mult = 1_000_000.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s) * mult
    except Exception:
        return 0.0


def normalize_tag(tag: str) -> str:
    return re.sub(r"(pos|pre)([a-z]{3})$", r"\2", tag.lower())


def tag_sort_key(tag: str) -> Tuple[int, int, str]:
    t = normalize_tag(tag)
    m = re.match(r"(\d{1,2})([a-z]{3})$", t)
    if not m:
        return (99, 99, t)
    return (MESES_NUM.get(m.group(2), 99), int(m.group(1)), t)


def tag_to_iso(tag: str, year: int) -> str:
    t = normalize_tag(tag)
    m = re.match(r"(\d{1,2})([a-z]{3})$", t)
    if not m:
        return f"{year}-01-01"
    d = int(m.group(1))
    mo = MESES_NUM.get(m.group(2), 1)
    return f"{year}-{mo:02d}-{d:02d}"


def iso_to_display(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return f"{dt.day}/{MESES_LABEL.get(dt.month, dt.month)}"


def is_primary_close(filename: str) -> bool:
    lower = filename.lower()
    return (
        lower.endswith(".csv")
        and "fechamento" in lower
        and "volume" not in lower
        and " copy" not in lower
    )


def extract_tag(filename: str) -> Optional[str]:
    m = re.search(r"fechamento__([a-zA-Z0-9]+)_\.csv$", filename)
    if m:
        return m.group(1)
    m = re.search(r"fechamento \(([a-zA-Z0-9]+)\)\.csv$", filename)
    if m:
        return m.group(1)
    return None


def extract_label(filename: str) -> str:
    m = re.match(r"venc_(.+?)_fechamento__", filename)
    if m:
        return m.group(1).replace("_", " ")
    m = re.match(r"venc (.+?) fechamento", filename)
    if m:
        return m.group(1)
    return ""


def is_ticker_match(label: str, ticker: str) -> bool:
    if not label or not ticker:
        return True
    label_lower = label.lower()
    months = {
        "jan": "A", "fev": "B", "mar": "C", "abr": "D", "mai": "E", "jun": "F",
        "jul": "G", "ago": "H", "set": "I", "out": "J", "nov": "K", "dez": "L",
    }
    exp_month = None
    for m, letter in months.items():
        if m in label_lower:
            exp_month = letter
            break
    if not exp_month or len(ticker) < 5:
        return True
    if ticker[4].upper() != exp_month:
        return False
    if "mensal" in label_lower:
        return re.search(r"W\d+$", ticker.upper()) is None
    return True


def discover_tags(data_dir: str) -> List[str]:
    tags = {}
    patterns = [
        os.path.join(data_dir, "venc * fechamento (*).csv"),
        os.path.join(data_dir, "venc_*_fechamento__*_.csv"),
    ]
    for pat in patterns:
        for fpath in glob.glob(pat):
            fn = os.path.basename(fpath)
            if not is_primary_close(fn):
                continue
            tag = extract_tag(fn)
            if not tag:
                continue
            n = normalize_tag(tag)
            tags.setdefault(n, tag)
    return sorted(tags.values(), key=tag_sort_key)


def discover_expiries_for_tag(data_dir: str, tag: str) -> List[dict]:
    out = []
    target = normalize_tag(tag)
    patterns = [
        os.path.join(data_dir, "venc * fechamento (*).csv"),
        os.path.join(data_dir, "venc_*_fechamento__*_.csv"),
    ]
    seen = set()
    for pat in patterns:
        for close_path in glob.glob(pat):
            fn = os.path.basename(close_path)
            if not is_primary_close(fn):
                continue
            ftag = extract_tag(fn)
            if not ftag or normalize_tag(ftag) != target:
                continue
            label = extract_label(fn)
            if not label:
                continue
            key = (label.lower(), os.path.abspath(close_path))
            if key in seen:
                continue
            seen.add(key)

            vol_path = None
            if "fechamento (" in fn:
                vol_name = fn.replace(f"({ftag}).csv", f"({ftag} Volume).csv")
            else:
                vol_name = fn.replace(f"__{ftag}_.csv", f"__{ftag}_Volume_.csv")
            candidate = os.path.join(data_dir, vol_name)
            if os.path.exists(candidate):
                vol_path = candidate

            out.append({"label": label, "close": close_path, "volume": vol_path})
    return out


def parse_close_csv(path: str, label: str) -> List[dict]:
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, "r", encoding="latin-1") as f:
        lines = f.readlines()
    if not lines:
        return rows

    for line in lines[1:]:
        line = line.strip().replace("\r", "")
        if not line:
            continue
        c = line.split(";")
        if len(c) < 23:
            continue
        if c and not is_ticker_match(label, c[0]):
            continue
        strike = _p(c[11])
        if strike <= 0:
            continue
        rows.append({
            "strike": strike,
            "call_oi": _p(c[2]),
            "put_oi": _p(c[20]),
            "call_gamma": _p(c[4]),
            "put_gamma": _p(c[18]),
            "call_delta": _p(c[3]),
            "put_delta": _p(c[19]),
            "call_iv": _p(c[7]),
            "put_iv": _p(c[15]),
        })
    return rows


def parse_volume_csv(path: Optional[str], label: str) -> Dict[float, dict]:
    agg: Dict[float, dict] = {}
    if not path or not os.path.exists(path):
        return agg
    with open(path, "r", encoding="latin-1") as f:
        lines = f.readlines()
    if not lines:
        return agg

    header = lines[0].strip().replace("\r", "").split(";")
    strike_col = 4
    for i, h in enumerate(header):
        if "strike" in h.lower():
            strike_col = i
            break

    for line in lines[1:]:
        line = line.strip().replace("\r", "")
        if not line:
            continue
        p = line.split(";")
        if len(p) < 10:
            continue
        if p and not is_ticker_match(label, p[0]):
            continue
        strike = _p(p[strike_col])
        if strike <= 0:
            continue
        if strike not in agg:
            agg[strike] = {"cv": 0.0, "pv": 0.0}
        agg[strike]["cv"] += _p(p[1])
        agg[strike]["pv"] += _p(p[9])
    return agg


def fetch_spot_close_map(iso_dates: List[str]) -> Dict[str, float]:
    if not iso_dates:
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}

    dts = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in iso_dates)
    start = dts[0] - timedelta(days=7)
    end = dts[-1] + timedelta(days=7)
    try:
        hist = yf.Ticker("BOVA11.SA").history(start=start.isoformat(), end=end.isoformat())
    except Exception:
        return {}
    if hist is None or len(hist) == 0:
        return {}

    by_day = {}
    for idx, row in hist.iterrows():
        day = idx.date().isoformat()
        try:
            by_day[day] = float(row["Close"])
        except Exception:
            pass

    out = {}
    for d in dts:
        key = d.isoformat()
        if key in by_day:
            out[key] = by_day[key]
            continue
        # fallback: último fechamento anterior disponível
        prev = [k for k in by_day.keys() if k <= key]
        if prev:
            out[key] = by_day[sorted(prev)[-1]]
    return out


def build_snapshot(
    data_dir: str,
    tag: str,
    year: int,
    spot_close_map: Dict[str, float],
    spot_history_file: str,
    stored_history: Dict[str, dict],
) -> Optional[dict]:
    expiries = discover_expiries_for_tag(data_dir, tag)
    if not expiries:
        return None

    strike_map: Dict[float, dict] = {}
    volume_map: Dict[float, dict] = {}

    for exp in expiries:
        rows = parse_close_csv(exp["close"], exp["label"])
        vols = parse_volume_csv(exp.get("volume"), exp["label"])

        for r in rows:
            s = r["strike"]
            if s not in strike_map:
                strike_map[s] = {
                    "call_oi": 0.0,
                    "put_oi": 0.0,
                    "call_gamma": 0.0,
                    "put_gamma": 0.0,
                    "call_delta": 0.0,
                    "put_delta": 0.0,
                    "call_iv_wsum": 0.0,
                    "call_iv_w": 0.0,
                    "put_iv_wsum": 0.0,
                    "put_iv_w": 0.0,
                }
            row = strike_map[s]
            row["call_oi"] += r["call_oi"]
            row["put_oi"] += r["put_oi"]
            row["call_gamma"] += r["call_gamma"] * r["call_oi"]
            row["put_gamma"] += r["put_gamma"] * r["put_oi"]
            row["call_delta"] += r["call_delta"] * r["call_oi"]
            row["put_delta"] += r["put_delta"] * r["put_oi"]
            if r["call_iv"] > 0:
                w = max(r["call_oi"], 1.0)
                row["call_iv_wsum"] += r["call_iv"] * w
                row["call_iv_w"] += w
            if r["put_iv"] > 0:
                w = max(r["put_oi"], 1.0)
                row["put_iv_wsum"] += r["put_iv"] * w
                row["put_iv_w"] += w

        for s, v in vols.items():
            if s not in volume_map:
                volume_map[s] = {"cv": 0.0, "pv": 0.0}
            volume_map[s]["cv"] += v["cv"]
            volume_map[s]["pv"] += v["pv"]

    if not strike_map:
        return None

    iso_date = tag_to_iso(tag, year)
    total_oi = 0.0
    total_call_oi = 0.0
    total_put_oi = 0.0

    for row in strike_map.values():
        total_call_oi += row["call_oi"]
        total_put_oi += row["put_oi"]
        total_oi += row["call_oi"] + row["put_oi"]
    stored_entry = stored_history.get(iso_date, {}) if isinstance(stored_history, dict) else {}
    stored_spot = stored_entry.get("spot_close") if isinstance(stored_entry, dict) else None
    spot_close, spot_source, spot_warning = resolve_spot(
        spot_history_file=spot_history_file,
        ref_date=iso_date,
        ref_tag=tag,
        stored_spot=stored_spot,
        fetcher=lambda ref_iso: spot_close_map.get(ref_iso),
    )

    gex_net = None
    dex_net = None
    if spot_close is not None and spot_close > 0:
        gex_total = 0.0
        dex_total = 0.0
        for row in strike_map.values():
            c_gamma_avg = (row["call_gamma"] / row["call_oi"]) if row["call_oi"] > 0 else 0.0
            p_gamma_avg = (row["put_gamma"] / row["put_oi"]) if row["put_oi"] > 0 else 0.0
            c_delta_avg = (row["call_delta"] / row["call_oi"]) if row["call_oi"] > 0 else 0.0
            p_delta_avg = (row["put_delta"] / row["put_oi"]) if row["put_oi"] > 0 else 0.0
            _, _, gex_part = calc_gex_components(
                call_gamma=c_gamma_avg,
                put_gamma=p_gamma_avg,
                call_oi=row["call_oi"],
                put_oi=row["put_oi"],
                spot=spot_close,
            )
            _, _, dex_part = calc_dex_components(
                call_delta=c_delta_avg,
                put_delta=p_delta_avg,
                call_oi=row["call_oi"],
                put_oi=row["put_oi"],
                spot=spot_close,
            )
            gex_total += gex_part
            dex_total += dex_part
        gex_net = gex_total
        dex_net = dex_total

    total_call_vol = sum(v["cv"] for v in volume_map.values())
    total_put_vol = sum(v["pv"] for v in volume_map.values())
    total_vol = total_call_vol + total_put_vol

    iv_atm = None
    if spot_close is not None and spot_close > 0:
        nearest_strike = min(strike_map.keys(), key=lambda s: abs(s - spot_close))
        atm_row = strike_map[nearest_strike]
        call_iv = (atm_row["call_iv_wsum"] / atm_row["call_iv_w"]) if atm_row["call_iv_w"] > 0 else 0.0
        put_iv = (atm_row["put_iv_wsum"] / atm_row["put_iv_w"]) if atm_row["put_iv_w"] > 0 else 0.0
        if call_iv > 0 or put_iv > 0:
            iv_atm = ((call_iv + put_iv) / 2.0) / 100.0

    _, mp, _ = calc_max_pain(strike_map)

    return {
        "date": iso_date,
        "tag": tag,
        "display": iso_to_display(iso_date),
        "spot_close": round(float(spot_close), 2) if spot_close is not None else None,
        "oi_call": round(total_call_oi),
        "oi_put": round(total_put_oi),
        "oi_total": round(total_oi),
        "oi_pcr": round(total_put_oi / max(total_call_oi, 1.0), 2),
        "vol_call": round(total_call_vol),
        "vol_put": round(total_put_vol),
        "vol_total": round(total_vol),
        "vol_pcr": round(total_put_vol / max(total_call_vol, 1.0), 2),
        "iv_atm": round(iv_atm, 4) if iv_atm is not None else None,
        "iv_rank": None,
        "iv_percentile": None,
        "max_pain": round(mp, 2) if mp is not None else None,
        "gex_net": round(gex_net) if gex_net is not None else None,
        "dex_net": round(dex_net) if dex_net is not None else None,
        "spot_source": spot_source,
        "warnings": [spot_warning] if spot_warning else [],
    }


def enrich_iv_metrics(series: List[dict]) -> None:
    ivs = []
    for item in series:
        iv = item.get("iv_atm")
        if isinstance(iv, (int, float)) and iv > 0:
            ivs.append(float(iv))
        valid = [v for v in ivs if v > 0]
        if len(valid) < 2 or not isinstance(iv, (int, float)) or iv <= 0:
            item["iv_rank"] = None
            item["iv_percentile"] = None
            continue
        lo, hi = min(valid), max(valid)
        if hi > lo:
            item["iv_rank"] = round((iv - lo) / (hi - lo) * 100.0, 2)
        else:
            item["iv_rank"] = 50.0
        item["iv_percentile"] = round(sum(1 for v in valid if v < iv) / len(valid) * 100.0, 2)


def build_html(data_series: List[dict], ref_date: str) -> str:
    payload = json.dumps(data_series, ensure_ascii=False)
    latest = data_series[-1] if data_series else None
    latest_date = latest["display"] if latest else "N/A"
    latest_spot = f"R$ {latest['spot_close']:.2f}" if latest and latest.get("spot_close") is not None else "N/A"

    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>BOVA11 — Histórico de Mercado</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg:#f3f5f7; --surface:#ffffff; --surface2:#f7f9fb; --border:#dce2e8;
      --text:#111827; --muted:#5f6b7a; --soft:#7a8696;
      --blue:#2563eb; --green:#148a63; --red:#b33530; --amber:#b8720a; --purple:#8250df;
      --font:'Instrument Sans',system-ui,sans-serif; --mono:'JetBrains Mono',monospace;
    }
    [data-theme="dark"] {
      --bg:#0f141b; --surface:#141b23; --surface2:#19212b; --border:#273240;
      --text:#e5ebf3; --muted:#a3afbf; --soft:#7d8a9b;
      --blue:#6ea8ff; --green:#3fb950; --red:#f87171; --amber:#f6c768; --purple:#b388ff;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:var(--font);background:var(--bg);color:var(--text);padding:22px}
    #theme-toggle{position:fixed;top:16px;right:16px;z-index:9;border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:10px;padding:8px 10px;cursor:pointer}
    .page{max-width:1280px;margin:0 auto}
    .head{display:flex;justify-content:space-between;gap:12px;align-items:flex-end;margin-bottom:16px}
    .head h1{font-size:1.75rem;letter-spacing:-.03em}
    .sub{color:var(--muted);font-size:.92rem;line-height:1.4}
    .toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
    .btn{border:1px solid var(--border);background:var(--surface);color:var(--muted);padding:8px 12px;border-radius:10px;cursor:pointer;font-weight:600}
    .btn.active{border-color:var(--blue);color:var(--blue);background:color-mix(in srgb, var(--blue) 10%, transparent)}
    .grid{display:grid;gap:12px;grid-template-columns:repeat(2,minmax(0,1fr));margin-bottom:14px}
    .card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:14px}
    .card h3{font-size:.86rem;color:var(--soft);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
    .card .v{font:700 1.3rem var(--mono)}
    .chart{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:12px}
    .chart + .chart{margin-top:12px}
    .chart h2{font-size:.95rem;margin-bottom:8px}
    canvas{max-height:260px}
    .table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:12px;margin-top:12px;overflow:auto}
    table{width:100%;border-collapse:collapse;min-width:980px}
    th,td{padding:8px 9px;border-bottom:1px solid var(--border);font-size:.82rem;text-align:right}
    th{position:sticky;top:0;background:var(--surface2);color:var(--soft);font:.72rem var(--mono);text-transform:uppercase;letter-spacing:.06em}
    th:first-child,td:first-child{text-align:left}
    @media(max-width:900px){.grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <button id="theme-toggle" onclick="toggleTheme()">Tema</button>
  <div class="page">
    <div class="head">
      <div>
        <h1>Histórico de Mercado</h1>
        <div class="sub">Base diária consolidada de OI, volume, IV, Max Pain, GEX e DEX. Referência atual: __LATEST_DATE__ · Spot __LATEST_SPOT__.</div>
      </div>
      <div class="sub mono">Ref date: __REF_DATE__</div>
    </div>
    <div class="toolbar" id="range-buttons">
      <button class="btn" data-range="5D">5D</button>
      <button class="btn active" data-range="1M">1M</button>
      <button class="btn" data-range="3M">3M</button>
      <button class="btn" data-range="YTD">YTD</button>
      <button class="btn" data-range="MAX">Max</button>
    </div>
    <div class="grid" id="cards"></div>
    <div class="chart"><h2>Open Interest</h2><canvas id="c1"></canvas></div>
    <div class="chart"><h2>Option Volume</h2><canvas id="c2"></canvas></div>
    <div class="chart"><h2>IV ATM & IV Rank</h2><canvas id="c3"></canvas></div>
    <div class="chart"><h2>Max Pain, Spot, GEX e DEX</h2><canvas id="c4"></canvas></div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th><th>Close</th><th>Vol Total</th><th>Vol PCR</th><th>OI Total</th><th>OI PCR</th>
            <th>IV ATM</th><th>IV Rank</th><th>Max Pain</th><th>GEX Net</th><th>DEX Net</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
<script>
const FULL = __PAYLOAD__;
let charts = [];
(function(){
  const saved = localStorage.getItem('bova11-theme');
  if (saved === 'dark') document.documentElement.setAttribute('data-theme','dark');
})();
function toggleTheme(){
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  if (dark) { document.documentElement.removeAttribute('data-theme'); localStorage.setItem('bova11-theme','light'); }
  else { document.documentElement.setAttribute('data-theme','dark'); localStorage.setItem('bova11-theme','dark'); }
}
function hasNum(v){ return typeof v === 'number' && Number.isFinite(v); }
function fmtInt(n){ return hasNum(n) ? Math.round(n).toLocaleString('pt-BR') : 'N/A'; }
function fmtPrice(v){ return hasNum(v) ? `R$ ${v.toFixed(2)}` : 'N/A'; }
function fmtRatio(v){ return hasNum(v) ? v.toFixed(2) : 'N/A'; }
function fmtPct(v){ return hasNum(v) ? `${v.toFixed(2)}%` : 'N/A'; }
function fmtScaledPct(v, scale){ return hasNum(v) ? `${(v*scale).toFixed(2)}%` : 'N/A'; }
function fmtB(v){
  if (!hasNum(v)) return 'N/A';
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v/1e9).toFixed(2)+'B';
  if (abs >= 1e6) return (v/1e6).toFixed(1)+'M';
  return Math.round(v).toLocaleString('pt-BR');
}
function cutByRange(arr, range){
  if (!arr.length) return arr;
  if (range === 'MAX') return arr;
  if (range === '5D') return arr.slice(-5);
  if (range === '1M') return arr.slice(-21);
  if (range === '3M') return arr.slice(-63);
  if (range === 'YTD') {
    const year = arr[arr.length-1].date.slice(0,4);
    return arr.filter(x => x.date.startsWith(year));
  }
  return arr;
}
function destroyCharts(){ charts.forEach(c=>c.destroy()); charts=[]; }
function render(range){
  const data = cutByRange(FULL, range);
  const labels = data.map(x=>x.display);
  const latest = data[data.length-1] || null;
  const cards = document.getElementById('cards');
  cards.innerHTML = latest ? `
    <div class="card"><h3>Data</h3><div class="v">${latest.display}</div></div>
    <div class="card"><h3>Spot</h3><div class="v">${fmtPrice(latest.spot_close)}</div></div>
    <div class="card"><h3>OI Put/Call</h3><div class="v">${fmtRatio(latest.oi_pcr)}</div></div>
    <div class="card"><h3>Vol Put/Call</h3><div class="v">${fmtRatio(latest.vol_pcr)}</div></div>
    <div class="card"><h3>IV ATM</h3><div class="v">${fmtScaledPct(latest.iv_atm, 100)}</div></div>
    <div class="card"><h3>Max Pain</h3><div class="v">${fmtPrice(latest.max_pain)}</div></div>` : '';
  document.querySelectorAll('#range-buttons .btn').forEach(btn => btn.classList.toggle('active', btn.dataset.range === range));
  destroyCharts();
  const common = {responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false}};
  charts.push(new Chart(document.getElementById('c1'), {
    type:'line', data:{labels, datasets:[
      {label:'OI Put-Call Ratio', data:data.map(x=>x.oi_pcr), borderColor:'#2E6BBF', yAxisID:'y'},
      {label:'Call OI', data:data.map(x=>x.oi_call), borderColor:'#148A63', yAxisID:'y1'},
      {label:'Put OI', data:data.map(x=>x.oi_put), borderColor:'#B33530', yAxisID:'y1'},
    ]}, options:{...common, scales:{ y:{position:'left'}, y1:{position:'right', grid:{drawOnChartArea:false}} }}
  }));
  charts.push(new Chart(document.getElementById('c2'), {
    type:'line', data:{labels, datasets:[
      {label:'Volume Put-Call Ratio', data:data.map(x=>x.vol_pcr), borderColor:'#2563eb', yAxisID:'y'},
      {label:'Call Volume', data:data.map(x=>x.vol_call), borderColor:'#15803d', yAxisID:'y1'},
      {label:'Put Volume', data:data.map(x=>x.vol_put), borderColor:'#c2413b', yAxisID:'y1'},
    ]}, options:{...common, scales:{ y:{position:'left'}, y1:{position:'right', grid:{drawOnChartArea:false}} }}
  }));
  charts.push(new Chart(document.getElementById('c3'), {
    type:'line', data:{labels, datasets:[
      {label:'IV ATM', data:data.map(x=>hasNum(x.iv_atm) ? x.iv_atm*100 : null), borderColor:'#d946ef', yAxisID:'y'},
      {label:'IV Rank', data:data.map(x=>x.iv_rank), borderColor:'#7c3aed', yAxisID:'y1'},
    ]}, options:{...common, scales:{ y:{position:'left'}, y1:{position:'right', min:0, max:100, grid:{drawOnChartArea:false}} }}
  }));
  charts.push(new Chart(document.getElementById('c4'), {
    data:{labels, datasets:[
      {type:'line', label:'Spot', data:data.map(x=>x.spot_close), borderColor:'#111827', yAxisID:'price'},
      {type:'line', label:'Max Pain', data:data.map(x=>x.max_pain), borderColor:'#b8720a', yAxisID:'price'},
      {type:'bar', label:'GEX Net', data:data.map(x=>x.gex_net), backgroundColor:'rgba(37,99,235,.35)', yAxisID:'flow'},
      {type:'bar', label:'DEX Net', data:data.map(x=>x.dex_net), backgroundColor:'rgba(148,163,184,.35)', yAxisID:'flow'},
    ]}, options:{...common, scales:{ price:{position:'left'}, flow:{position:'right', grid:{drawOnChartArea:false}} }}
  }));
  const rows = document.getElementById('rows');
  rows.innerHTML = data.slice().reverse().map(x => `
    <tr><td>${x.date}</td><td>${fmtPrice(x.spot_close)}</td><td>${fmtInt(x.vol_total)}</td><td>${fmtRatio(x.vol_pcr)}</td>
    <td>${fmtInt(x.oi_total)}</td><td>${fmtRatio(x.oi_pcr)}</td><td>${fmtScaledPct(x.iv_atm, 100)}</td><td>${fmtPct(x.iv_rank)}</td>
    <td>${hasNum(x.max_pain) ? x.max_pain.toFixed(2) : '-'}</td><td>${fmtB(x.gex_net)}</td><td>${fmtB(x.dex_net)}</td></tr>`).join('');
}
document.querySelectorAll('#range-buttons .btn').forEach(btn => btn.addEventListener('click', () => render(btn.dataset.range)));
render('1M');
</script>
</body>
</html>"""
    return (
        html.replace("__PAYLOAD__", payload)
        .replace("__LATEST_DATE__", latest_date)
        .replace("__LATEST_SPOT__", latest_spot)
        .replace("__REF_DATE__", ref_date)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="BOVA11 Historical Data")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ref-date", required=True)
    parser.add_argument("--ref-tag", required=True)
    parser.add_argument("--history-file", default=os.path.join(os.path.dirname(__file__), "..", "history", "bova11_market_history.json"))
    parser.add_argument("--spot-history-file", default=os.path.join(os.path.dirname(__file__), "..", "history", "bova11_spot_history.json"))
    args = parser.parse_args()

    year = datetime.now().year
    tags = discover_tags(args.data_dir)
    if not tags:
        raise SystemExit("Nenhuma tag válida encontrada em data-dir")

    iso_dates = [tag_to_iso(t, year) for t in tags]
    spot_map = fetch_spot_close_map(iso_dates)

    hist = load_json(args.history_file, {})
    if not isinstance(hist, dict):
        hist = {}

    computed = []
    spot_warnings = []
    for t in tags:
        snap = build_snapshot(
            args.data_dir,
            t,
            year,
            spot_map,
            args.spot_history_file,
            hist,
        )
        if snap:
            computed.append(snap)
            if snap.get("warnings"):
                spot_warnings.extend(snap["warnings"])

    computed.sort(key=lambda x: x["date"])
    enrich_iv_metrics(computed)

    for s in computed:
        hist[s["date"]] = {**hist.get(s["date"], {}), **s}

    ordered = [hist[k] for k in sorted(hist.keys())]
    enrich_iv_metrics(ordered)

    os.makedirs(os.path.dirname(args.history_file), exist_ok=True)
    with open(args.history_file, "w", encoding="utf-8") as f:
        json.dump({item["date"]: item for item in ordered}, f, ensure_ascii=False, indent=2)

    html = build_html(ordered, args.ref_date)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ Histórico consolidado: {args.output}")
    print(f"✅ Persistido em: {args.history_file}")
    if spot_warnings:
        print(f"⚠️ {len(spot_warnings)} aviso(s) de spot durante a consolidação histórica")


if __name__ == "__main__":
    main()
