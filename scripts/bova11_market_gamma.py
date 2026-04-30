#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Market Gamma
-------------------
Gera a curva de gamma líquida por strike no dia de referência,
com visão agregada e por vencimento.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bova11_shared import calc_gex_components, resolve_spot


MESES_NUM = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
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


def tag_to_iso(tag: str, year: int) -> str:
    t = normalize_tag(tag)
    m = re.match(r"(\d{1,2})([a-z]{3})$", t)
    if not m:
        return f"{year}-01-01"
    return f"{year}-{MESES_NUM.get(m.group(2), 1):02d}-{int(m.group(1)):02d}"


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


def discover_expiries_for_tag(data_dir: str, ref_tag: str) -> List[dict]:
    out = []
    target = normalize_tag(ref_tag)
    patterns = [
        os.path.join(data_dir, "venc * fechamento (*).csv"),
        os.path.join(data_dir, "venc_*_fechamento__*_.csv"),
    ]
    seen = set()
    for pat in patterns:
        for path in glob.glob(pat):
            fn = os.path.basename(path)
            if not is_primary_close(fn):
                continue
            tag = extract_tag(fn)
            if not tag or normalize_tag(tag) != target:
                continue
            label = extract_label(fn)
            if not label:
                continue
            key = (label.lower(), os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": label, "path": path})
    out.sort(key=lambda x: x["label"].lower())
    return out


def parse_gamma_curve(path: str, label: str, spot: float) -> Dict[float, dict]:
    curve: Dict[float, dict] = {}
    with open(path, "r", encoding="latin-1") as f:
        lines = f.readlines()
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
        c_oi = _p(c[2])
        p_oi = _p(c[20])
        c_gamma = _p(c[4])
        p_gamma = _p(c[18])

        call_g, put_g, net_g = calc_gex_components(
            call_gamma=c_gamma,
            put_gamma=p_gamma,
            call_oi=c_oi,
            put_oi=p_oi,
            spot=spot,
        )

        if strike not in curve:
            curve[strike] = {"call": 0.0, "put": 0.0, "net": 0.0}
        curve[strike]["call"] += call_g
        curve[strike]["put"] += put_g
        curve[strike]["net"] += net_g
    return curve


def find_flip(strikes: List[float], nets: List[float]) -> Optional[float]:
    if len(strikes) < 2:
        return None
    for i in range(len(strikes) - 1):
        s0, s1 = strikes[i], strikes[i + 1]
        n0, n1 = nets[i], nets[i + 1]
        if n0 == 0:
            return s0
        if (n0 < 0 and n1 > 0) or (n0 > 0 and n1 < 0):
            if n1 == n0:
                return round((s0 + s1) / 2.0, 2)
            return round(s0 + (s1 - s0) * abs(n0) / (abs(n0) + abs(n1)), 2)
    return None


def fetch_spot_close(ref_iso: str) -> Optional[float]:
    try:
        import yfinance as yf
    except Exception:
        return None
    d = datetime.strptime(ref_iso, "%Y-%m-%d").date()
    start = (d - timedelta(days=7)).isoformat()
    end = (d + timedelta(days=7)).isoformat()
    try:
        hist = yf.Ticker("BOVA11.SA").history(start=start, end=end)
    except Exception:
        return None
    if hist is None or len(hist) == 0:
        return None
    by_day = {}
    for idx, row in hist.iterrows():
        try:
            by_day[idx.date().isoformat()] = float(row["Close"])
        except Exception:
            pass
    if ref_iso in by_day:
        return by_day[ref_iso]
    prev = [k for k in by_day.keys() if k <= ref_iso]
    if prev:
        return by_day[sorted(prev)[-1]]
    return None


def to_curve_payload(curve: Dict[float, dict], spot: float) -> dict:
    strikes = sorted(curve.keys())
    calls = [curve[s]["call"] for s in strikes]
    puts = [curve[s]["put"] for s in strikes]
    nets = [curve[s]["net"] for s in strikes]

    if not strikes:
        return {
            "strikes": [], "call": [], "put": [], "net": [],
            "spot": spot, "flip": None, "gamma_atual": 0.0,
            "gamma_min_neg": None, "gamma_max_pos": None, "gamma_score": 0.0,
        }

    near_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    gamma_atual = nets[near_idx]
    max_abs = max((abs(v) for v in nets), default=1.0)
    gamma_score = gamma_atual / max_abs if max_abs > 0 else 0.0

    min_neg = None
    neg_points = [(s, v) for s, v in zip(strikes, nets) if v < 0]
    if neg_points:
        min_neg = min(neg_points, key=lambda x: x[1])

    max_pos = None
    pos_points = [(s, v) for s, v in zip(strikes, nets) if v > 0]
    if pos_points:
        max_pos = max(pos_points, key=lambda x: x[1])

    return {
        "strikes": strikes,
        "call": calls,
        "put": puts,
        "net": nets,
        "spot": spot,
        "flip": find_flip(strikes, nets),
        "gamma_atual": gamma_atual,
        "gamma_min_neg": {"strike": min_neg[0], "value": min_neg[1]} if min_neg else None,
        "gamma_max_pos": {"strike": max_pos[0], "value": max_pos[1]} if max_pos else None,
        "gamma_score": gamma_score,
    }


def build_html(payload: dict) -> str:
    js = json.dumps(payload, ensure_ascii=False)
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Market Gamma</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg:#0f172a; --surface:#111c31; --surface2:#1a2a45; --text:#e7edf7; --muted:#9fb0c8;
      --line:#314a70; --blue:#2f81f7; --green:#22c55e; --red:#ef4444; --gold:#facc15;
      --font:'Instrument Sans',system-ui,sans-serif; --mono:'JetBrains Mono',monospace;
    }
    [data-theme="light"] {
      --bg:#f4f6fb; --surface:#ffffff; --surface2:#eef2fa; --text:#111827; --muted:#5b6577;
      --line:#d4ddeb; --blue:#2563eb; --green:#15803d; --red:#b91c1c; --gold:#b45309;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:var(--bg);color:var(--text);font-family:var(--font);padding:20px}
    #theme-toggle{position:fixed;top:12px;right:12px;border:1px solid var(--line);background:var(--surface);color:var(--text);padding:8px 10px;border-radius:10px;cursor:pointer}
    .page{max-width:1360px;margin:0 auto}
    h1{text-align:center;font-size:3rem;letter-spacing:-.04em;margin:10px 0 14px}
    .bar{display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap;margin-bottom:14px}
    .sel{border:1px solid var(--line);background:var(--surface);color:var(--text);padding:10px 12px;border-radius:10px;font-weight:600}
    .chart-wrap{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:14px}
    canvas{max-height:480px}
    .cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:14px}
    .card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:12px}
    .card .k{font:.72rem var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
    .card .v{margin-top:6px;font:700 1.4rem var(--mono)}
    @media(max-width:1100px){.cards{grid-template-columns:1fr 1fr}}
    @media(max-width:680px){h1{font-size:2.2rem}.cards{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <button id="theme-toggle" onclick="toggleTheme()">Tema</button>
  <div class="page">
    <h1>Market Gamma</h1>
    <div class="bar">
      <select id="mode" class="sel"></select>
      <div id="meta" class="sel"></div>
    </div>
    <div class="chart-wrap">
      <canvas id="gamma"></canvas>
    </div>
    <div class="cards">
      <div class="card"><div class="k">Gamma Score [σ]</div><div class="v" id="c-score">-</div></div>
      <div class="card"><div class="k">Gamma Atual</div><div class="v" id="c-atual">-</div></div>
      <div class="card"><div class="k">Gamma Mínimo Negativo</div><div class="v" id="c-min">-</div></div>
      <div class="card"><div class="k">Flip</div><div class="v" id="c-flip">-</div></div>
      <div class="card"><div class="k">Gamma Máximo Positivo</div><div class="v" id="c-max">-</div></div>
    </div>
  </div>

<script>
const DATA = __PAYLOAD__;
let chart = null;

(function(){
  const saved = localStorage.getItem('bova11-theme');
  if (saved === 'light') document.documentElement.setAttribute('data-theme', 'light');
})();

function toggleTheme(){
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  if (light) {
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('bova11-theme','dark');
  } else {
    document.documentElement.setAttribute('data-theme','light');
    localStorage.setItem('bova11-theme','light');
  }
}

function fmtNum(v){
  if (v === null || v === undefined) return '-';
  const a = Math.abs(v);
  if (a >= 1e9) return (v/1e9).toFixed(2)+'B';
  if (a >= 1e6) return (v/1e6).toFixed(1)+'M';
  if (a >= 1e3) return (v/1e3).toFixed(1)+'k';
  return v.toFixed(0);
}

function draw(mode){
  const c = DATA.curves[mode];
  if (!c) return;
  document.getElementById('meta').textContent = `${DATA.asset} · ${DATA.ref_display} · Spot R$ ${c.spot.toFixed(2)}`;

  document.getElementById('c-score').textContent = c.gamma_score.toFixed(2);
  document.getElementById('c-atual').textContent = fmtNum(c.gamma_atual);
  document.getElementById('c-min').textContent = c.gamma_min_neg ? `R$ ${c.gamma_min_neg.strike.toFixed(2)} · ${fmtNum(c.gamma_min_neg.value)}` : '-';
  document.getElementById('c-flip').textContent = c.flip ? `R$ ${c.flip.toFixed(2)}` : '-';
  document.getElementById('c-max').textContent = c.gamma_max_pos ? `R$ ${c.gamma_max_pos.strike.toFixed(2)} · ${fmtNum(c.gamma_max_pos.value)}` : '-';

  const pos = c.net.map(v => v > 0 ? v : null);
  const neg = c.net.map(v => v < 0 ? v : null);

  const spotIdx = c.strikes.length ? c.strikes.reduce((best, s, i) => Math.abs(s-c.spot) < Math.abs(c.strikes[best]-c.spot) ? i : best, 0) : 0;
  const flipIdx = (c.flip && c.strikes.length) ? c.strikes.reduce((best, s, i) => Math.abs(s-c.flip) < Math.abs(c.strikes[best]-c.flip) ? i : best, 0) : null;

  const vlinePlugin = {
    id: 'vline',
    afterDatasetsDraw(ch){
      const {ctx, chartArea:{top,bottom}, scales:{x}} = ch;
      ctx.save();
      if (c.strikes.length) {
        const xSpot = x.getPixelForValue(spotIdx);
        ctx.strokeStyle = '#2f81f7'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(xSpot, top); ctx.lineTo(xSpot, bottom); ctx.stroke();
      }
      if (flipIdx !== null) {
        const xFlip = x.getPixelForValue(flipIdx);
        ctx.strokeStyle = '#facc15'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(xFlip, top); ctx.lineTo(xFlip, bottom); ctx.stroke();
      }
      ctx.restore();
    }
  };

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('gamma'), {
    type: 'line',
    data: {
      labels: c.strikes.map(s => s.toFixed(2)),
      datasets: [
        {label:'Positivo', data:pos, borderColor:'#22c55e', backgroundColor:'rgba(34,197,94,.25)', fill:true, pointRadius:0, tension:.25},
        {label:'Negativo', data:neg, borderColor:'#ef4444', backgroundColor:'rgba(239,68,68,.25)', fill:true, pointRadius:0, tension:.25},
        {label:'Net Gamma', data:c.net, borderColor:'#facc15', pointRadius:0, tension:.25, fill:false},
      ]
    },
    options: {
      responsive:true,
      maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{color:getComputedStyle(document.documentElement).getPropertyValue('--text')}}},
      scales:{
        x:{ticks:{color:getComputedStyle(document.documentElement).getPropertyValue('--muted')}},
        y:{ticks:{color:getComputedStyle(document.documentElement).getPropertyValue('--muted')}}
      }
    },
    plugins:[vlinePlugin]
  });
}

const sel = document.getElementById('mode');
Object.keys(DATA.curves).forEach((k, i) => {
  const o = document.createElement('option');
  o.value = k;
  o.textContent = i === 0 ? 'Visão Completa (Agregado)' : k;
  sel.appendChild(o);
});
sel.value = 'AGREGADO';
sel.addEventListener('change', () => draw(sel.value));
draw('AGREGADO');
</script>
</body>
</html>
"""
    return html.replace("__PAYLOAD__", js)


def main() -> None:
    parser = argparse.ArgumentParser(description="BOVA11 Market Gamma")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ref-date", required=True)
    parser.add_argument("--ref-tag", required=True)
    parser.add_argument("--spot", type=float, default=None)
    parser.add_argument("--spot-history-file", default=os.path.join(os.path.dirname(__file__), "..", "history", "bova11_spot_history.json"))
    args = parser.parse_args()

    year = datetime.now().year
    ref_iso = args.ref_date
    spot, spot_source, spot_warning = resolve_spot(
        spot=args.spot,
        spot_history_file=args.spot_history_file,
        ref_date=ref_iso,
        ref_tag=args.ref_tag,
        fetcher=fetch_spot_close,
    )

    expiries = discover_expiries_for_tag(args.data_dir, args.ref_tag)
    if not expiries:
        raise SystemExit(f"Nenhum vencimento encontrado para tag {args.ref_tag}")
    if spot is None or spot <= 0:
        raise SystemExit(spot_warning or f"Spot indisponível para {ref_iso}. Informe --spot ou use histórico manual.")

    curves = {}
    agg: Dict[float, dict] = {}

    for e in expiries:
        c = parse_gamma_curve(e["path"], e["label"], float(spot))
        payload = to_curve_payload(c, float(spot))
        if payload["strikes"]:
            curves[e["label"]] = payload
        for s, vals in c.items():
            if s not in agg:
                agg[s] = {"call": 0.0, "put": 0.0, "net": 0.0}
            agg[s]["call"] += vals["call"]
            agg[s]["put"] += vals["put"]
            agg[s]["net"] += vals["net"]

    curves = {"AGREGADO": to_curve_payload(agg, float(spot)), **curves}

    payload = {
        "asset": "BOVA11",
        "ref_date": ref_iso,
        "ref_display": datetime.strptime(ref_iso, "%Y-%m-%d").strftime("%d/%m/%Y"),
        "curves": curves,
    }

    html = build_html(payload)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    if spot_warning:
        print(f"⚠️ {spot_warning}")
    print(f"✅ Market Gamma: {args.output} | spot={float(spot):.2f} ({spot_source})")


if __name__ == "__main__":
    main()
