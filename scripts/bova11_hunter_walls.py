#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Hunter Walls — Análise de Volume por Strike (Módulo 13)
==============================================================
Lê os CSVs de volume do dia para todos os vencimentos disponíveis,
agrega call/put volume por strike e gera um dashboard HTML interativo
com butterfly bar charts (calls à esquerda, puts à direita).

Dependências: Python 3 stdlib apenas (os, re, glob, json, math, argparse, datetime)

Uso via CLI (argparse):
  python3 bova11_hunter_walls.py \\
      --data-dir /path/to/data \\
      --output   /path/to/output/bova11_hunter_walls.html \\
      --ref-date 2026-03-25 \\
      --ref-tag  25posmar \\
      --spot     185.50
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════
# 1. PARSER B3 — formato número brasileiro
# ═══════════════════════════════════════════════════════════════

def _p(raw) -> float:
    """Converte número BR ('1.234,56' / '123,91k' / '8,19M' / '-') → float."""
    if not isinstance(raw, str):
        return float(raw) if raw == raw else 0.0
    s = raw.strip().rstrip("%").replace("\r", "")
    if s in ("", "-", "--"):
        return 0.0
    m = 1.0
    if s.endswith("k"):
        m = 1_000
        s = s[:-1]
    elif s.endswith("M"):
        m = 1_000_000
        s = s[:-1]
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s) * m
    except Exception:
        return 0.0


def load_volume_csv(path: str) -> List[Dict]:
    """
    Lê CSV de volume B3.
    Colunas: 0=call_ativo, 1=call_vol, 2=call_oi, 3=call_bid, 4=call_ask,
             5=strike, 6=put_bid, 7=put_ask, 8=put_oi, 9=put_vol, 10=put_ativo
    """
    rows = []
    try:
        with open(path, encoding="latin-1") as fh:
            lines = [l.strip() for l in fh.readlines() if l.strip()]
    except Exception as e:
        print(f"  ⚠️  Erro ao ler {path}: {e}")
        return rows

    for line in lines[1:]:   # skip header
        cols = line.split(";")
        if len(cols) < 10:
            continue
        strike = _p(cols[5])
        if strike <= 0:
            continue
        rows.append({
            "strike":   strike,
            "call_vol": _p(cols[1]),
            "put_vol":  _p(cols[9]),
        })
    return rows


# ═══════════════════════════════════════════════════════════════
# 2. TAG NORMALIZATION & FILE DISCOVERY
# ═══════════════════════════════════════════════════════════════

def normalize_tag(tag: str) -> str:
    """Remove prefixos pos/pre de tags como '25posmar' → '25mar'."""
    return re.sub(r"(pos|pre)([a-z]{3})$", r"\2", tag.lower())


def extract_expiry_from_filename(filename: str) -> Optional[str]:
    """
    Extrai o nome do vencimento do filename.
    Ex: 'venc 17 abr Mensal fechamento (25posmar Volume).csv' → '17 abr Mensal'
    Ex: 'venc 10 abr W2 fechamento (25posmar Volume).csv'     → '10 abr W2'
    """
    m = re.match(
        r"venc\s+(.+?)\s+fechamento\s+\(",
        os.path.basename(filename),
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    # underscore variant
    m = re.match(
        r"venc_(.+?)_fechamento__",
        os.path.basename(filename),
        re.IGNORECASE,
    )
    if m:
        return m.group(1).replace("_", " ").strip()
    return None


def find_volume_files(data_dir: str, ref_tag: str) -> List[Tuple[str, str]]:
    """
    Encontra todos os CSVs de volume para a tag de referência.
    Tenta o tag original e o tag normalizado.
    Retorna lista de (expiry_name, filepath).
    """
    tags_to_try = set()
    tags_to_try.add(ref_tag)
    tags_to_try.add(normalize_tag(ref_tag))

    found: List[Tuple[str, str]] = []
    seen_expiries: set = set()

    for tag in sorted(tags_to_try):
        # Padrão com espaços: venc * fechamento (TAG Volume).csv
        pattern1 = os.path.join(data_dir, f"venc * fechamento ({tag} Volume).csv")
        # Padrão com underscores: venc_*_fechamento__TAG_Volume_.csv  (menos comum)
        pattern2 = os.path.join(data_dir, f"venc_*_fechamento__{tag}_Volume_.csv")

        for pattern in (pattern1, pattern2):
            for fpath in sorted(glob.glob(pattern)):
                expiry = extract_expiry_from_filename(fpath)
                if expiry and expiry not in seen_expiries:
                    found.append((expiry, fpath))
                    seen_expiries.add(expiry)

    return found


# ═══════════════════════════════════════════════════════════════
# 3. EXPIRY SORT — ordena por data de expiração
# ═══════════════════════════════════════════════════════════════

_MESES = {
    "jan": 1,  "fev": 2,  "mar": 3,  "abr": 4,
    "mai": 5,  "jun": 6,  "jul": 7,  "ago": 8,
    "set": 9,  "out": 10, "nov": 11, "dez": 12,
}


def expiry_sort_key(expiry: str) -> Tuple[int, int]:
    """Extrai (mês, dia) de string como '10 abr W2' ou '17 abr Mensal'."""
    m = re.match(r"(\d{1,2})\s+([a-z]{3})", expiry.lower())
    if m:
        day   = int(m.group(1))
        month = _MESES.get(m.group(2), 1)
        return (month, day)
    return (99, 99)


# ═══════════════════════════════════════════════════════════════
# 4. AGGREGATION
# ═══════════════════════════════════════════════════════════════

def aggregate_by_strike(rows: List[Dict]) -> Dict[float, Dict[str, float]]:
    """Agrega call_vol e put_vol por strike (soma em caso de duplicatas)."""
    agg: Dict[float, Dict[str, float]] = {}
    for r in rows:
        s = r["strike"]
        if s not in agg:
            agg[s] = {"call_vol": 0.0, "put_vol": 0.0}
        agg[s]["call_vol"] += r["call_vol"]
        agg[s]["put_vol"]  += r["put_vol"]
    return agg


def compute_expiry_stats(agg: Dict[float, Dict[str, float]]) -> Dict:
    """Calcula totais, PCR e top-3 por vencimento."""
    total_call = sum(v["call_vol"] for v in agg.values())
    total_put  = sum(v["put_vol"]  for v in agg.values())
    pcr        = (total_put / total_call) if total_call > 0 else 0.0

    sorted_strikes = sorted(agg.keys())

    # Top-3 calls and puts by volume
    top3_calls = sorted(agg.items(), key=lambda x: x[1]["call_vol"], reverse=True)[:3]
    top3_puts  = sorted(agg.items(), key=lambda x: x[1]["put_vol"],  reverse=True)[:3]

    return {
        "total_call":  total_call,
        "total_put":   total_put,
        "pcr":         pcr,
        "strikes":     sorted_strikes,
        "top3_calls":  [(s, d["call_vol"]) for s, d in top3_calls],
        "top3_puts":   [(s, d["put_vol"])  for s, d in top3_puts],
        "agg":         agg,
    }


def fmt_vol(v: float) -> str:
    """Formata volume para exibição: M / k / inteiro."""
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}k"
    return f"{v:.0f}"


# ═══════════════════════════════════════════════════════════════
# 5. HTML GENERATION
# ═══════════════════════════════════════════════════════════════

def build_html(
    ref_date: str,
    ref_tag: str,
    spot: float,
    expiries_data: List[Tuple[str, Dict]],  # [(expiry_name, stats), ...]
) -> str:
    """Gera o HTML completo do Hunter Walls."""

    # ── Aggregate totals across all expiries ──────────────────
    grand_call  = sum(s["total_call"] for _, s in expiries_data)
    grand_put   = sum(s["total_put"]  for _, s in expiries_data)
    grand_pcr   = (grand_put / grand_call) if grand_call > 0 else 0.0
    grand_total = grand_call + grand_put

    # ── Top-5 calls / puts across all expiries ────────────────
    all_calls: List[Tuple[str, float, float]] = []  # (expiry, strike, vol)
    all_puts:  List[Tuple[str, float, float]] = []
    for expiry, stats in expiries_data:
        for strike, vol in stats["agg"].items():
            all_calls.append((expiry, strike, vol["call_vol"]))
            all_puts.append((expiry,  strike, vol["put_vol"]))

    top5_calls = sorted(all_calls, key=lambda x: x[2], reverse=True)[:5]
    top5_puts  = sorted(all_puts,  key=lambda x: x[2], reverse=True)[:5]

    # ── Build per-expiry chart blocks ─────────────────────────
    chart_blocks_html = ""
    chart_js_blocks   = ""

    for idx, (expiry, stats) in enumerate(expiries_data):
        strikes    = stats["strikes"]
        agg        = stats["agg"]
        total_call = stats["total_call"]
        total_put  = stats["total_put"]
        pcr        = stats["pcr"]

        if not strikes:
            continue

        # calls → negative (go left), puts → positive (go right)
        call_vals = [-agg[s]["call_vol"] for s in strikes]
        put_vals  = [ agg[s]["put_vol"]  for s in strikes]

        # Chart height: min 300px, ~22px per strike
        chart_height = max(300, len(strikes) * 22)

        pcr_class = "pcr-neutral"
        if pcr > 1.2:
            pcr_class = "pcr-bearish"
        elif pcr < 0.8:
            pcr_class = "pcr-bullish"

        # Spot annotation for this chart
        if spot > 0:
            spot_annotation_js = f"""
              spotLine: {{
                type: 'line',
                scaleID: 'y',
                value: {spot},
                borderColor: 'rgba(255, 215, 0, 0.85)',
                borderWidth: 2,
                borderDash: [6, 4],
                label: {{
                  display: true,
                  content: 'Spot {spot:.2f}',
                  position: 'end',
                  color: '#ffd700',
                  font: {{ size: 11, weight: '600' }}
                }}
              }}"""
        else:
            spot_annotation_js = ""

        chart_blocks_html += f"""
        <div class="expiry-section">
          <div class="expiry-header">
            <span class="expiry-name">{expiry}</span>
            <div class="expiry-meta">
              <span class="meta-pill calls-pill">Calls {fmt_vol(total_call)}</span>
              <span class="meta-pill puts-pill">Puts {fmt_vol(total_put)}</span>
              <span class="meta-pill pcr-pill {pcr_class}">PCR {pcr:.2f}</span>
            </div>
          </div>
          <div class="chart-wrapper" style="height:{chart_height}px">
            <canvas id="chart_{idx}"></canvas>
          </div>
        </div>
"""

        chart_js_blocks += f"""
  // ── Chart {idx}: {expiry} ──
  (function() {{
    var ctx = document.getElementById('chart_{idx}').getContext('2d');
    var strikes  = {json.dumps(strikes)};
    var callVals = {json.dumps(call_vals)};
    var putVals  = {json.dumps(put_vals)};

    new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: strikes,
        datasets: [
          {{
            label: 'Calls',
            data: callVals,
            backgroundColor: 'rgba(46, 107, 191, 0.75)',
            borderColor: 'rgba(46, 107, 191, 1)',
            borderWidth: 1,
          }},
          {{
            label: 'Puts',
            data: putVals,
            backgroundColor: 'rgba(179, 53, 48, 0.75)',
            borderColor: 'rgba(179, 53, 48, 1)',
            borderWidth: 1,
          }},
        ],
      }},
      options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{ mode: 'y', intersect: false }},
        plugins: {{
          legend: {{
            display: true,
            position: 'top',
            labels: {{ color: chartTextColor(), font: {{ size: 12 }} }},
          }},
          tooltip: {{
            callbacks: {{
              label: function(ctx) {{
                var raw = ctx.raw;
                var abs = Math.abs(raw);
                var lbl = ctx.dataset.label;
                if (abs >= 1000000) return lbl + ': ' + (abs/1000000).toFixed(2) + 'M';
                if (abs >= 1000)    return lbl + ': ' + (abs/1000).toFixed(1) + 'k';
                return lbl + ': ' + abs.toFixed(0);
              }},
            }},
          }},
          annotation: {{
            annotations: {{
              zeroLine: {{
                type: 'line',
                scaleID: 'x',
                value: 0,
                borderColor: 'rgba(150,150,150,0.45)',
                borderWidth: 1,
              }},{spot_annotation_js}
            }},
          }},
        }},
        scales: {{
          x: {{
            stacked: false,
            grid: {{ color: 'rgba(150,150,150,0.15)' }},
            ticks: {{
              color: chartTextColor(),
              callback: function(value) {{
                var abs = Math.abs(value);
                if (abs >= 1000000) return (abs/1000000).toFixed(1) + 'M';
                if (abs >= 1000)    return (abs/1000).toFixed(0) + 'k';
                return abs.toFixed(0);
              }},
            }},
          }},
          y: {{
            stacked: false,
            grid: {{ color: 'rgba(150,150,150,0.15)' }},
            ticks: {{ color: chartTextColor(), font: {{ size: 11 }} }},
          }},
        }},
      }},
    }});
  }})();
"""

    # ── Summary table rows ────────────────────────────────────
    def table_rows(items: List[Tuple[str, float, float]], side: str) -> str:
        html = ""
        max_vol = items[0][2] if items else 1.0
        for rank, (expiry, strike, vol) in enumerate(items, 1):
            bar_pct = (vol / max_vol * 100) if max_vol > 0 else 0
            html += f"""
            <tr>
              <td class="rank-cell">{rank}</td>
              <td class="strike-cell">{strike:.0f}</td>
              <td class="expiry-cell">{expiry}</td>
              <td class="vol-cell">
                <div class="vol-bar-wrap">
                  <div class="vol-bar-track">
                    <div class="vol-bar {side}-bar" style="width:{bar_pct:.1f}%"></div>
                  </div>
                  <span class="vol-label">{fmt_vol(vol)}</span>
                </div>
              </td>
            </tr>"""
        return html

    calls_table_rows = table_rows(top5_calls, "calls")
    puts_table_rows  = table_rows(top5_puts,  "puts")

    # ── PCR card colour ───────────────────────────────────────
    pcr_card_class = "pcr-neutral"
    if grand_pcr > 1.2:
        pcr_card_class = "pcr-bearish"
    elif grand_pcr < 0.8:
        pcr_card_class = "pcr-bullish"

    pcr_label = (
        "Bearish (> 1.2)" if grand_pcr > 1.2
        else "Bullish (< 0.8)" if grand_pcr < 0.8
        else "Neutro (0.8 – 1.2)"
    )

    # ── calls/puts pct of total ───────────────────────────────
    calls_pct = f"{grand_call/grand_total*100:.1f}% do volume total" if grand_total > 0 else ""
    puts_pct  = f"{grand_put/grand_total*100:.1f}% do volume total"  if grand_total > 0 else ""

    # ── Format ref_date for display ───────────────────────────
    try:
        dt = datetime.strptime(ref_date, "%Y-%m-%d")
        date_display = dt.strftime("%d/%m/%Y")
    except Exception:
        date_display = ref_date

    spot_meta = f" &nbsp;|&nbsp; Spot: R$ {spot:.2f}" if spot > 0 else ""

    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BOVA11 Hunter Walls — {date_display}</title>
  <link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-annotation/3.0.1/chartjs-plugin-annotation.min.js"></script>
  <style>
    :root {{
      --bg:#FAFAF8; --bg2:#F2F1EE; --bg3:#E8E7E3; --card:#FFFFFF;
      --border:rgba(0,0,0,0.08); --border2:rgba(0,0,0,0.14);
      --t1:#1A1A18; --t2:#6B6960; --t3:#9C9A91;
      --green:#148A63; --amber:#B8720A; --red:#B33530; --blue:#2E6BBF;
      --font:'Instrument Sans',system-ui,sans-serif;
      --mono:'JetBrains Mono',monospace;
    }}
    [data-theme="dark"] {{
      --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --card:#21262d;
      --border:rgba(255,255,255,0.1); --border2:rgba(255,255,255,0.16);
      --t1:#c9d1d9; --t2:#8b949e; --t3:#636c76;
      --green:#3fb950; --amber:#d29922; --red:#f85149; --blue:#58a6ff;
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: var(--font);
      background: var(--bg);
      color: var(--t1);
      min-height: 100vh;
      padding: 24px 20px 60px;
    }}

    /* ── Theme toggle ───────────────────────────────────────── */
    #theme-toggle {{
      position: fixed;
      top: 16px;
      right: 20px;
      background: var(--card);
      border: 1px solid var(--border2);
      border-radius: 8px;
      padding: 6px 12px;
      cursor: pointer;
      font-size: 1rem;
      color: var(--t1);
      z-index: 999;
      transition: background 0.2s, border-color 0.2s;
    }}
    #theme-toggle:hover {{ border-color: var(--blue); }}

    /* ── Page header ────────────────────────────────────────── */
    .page-header {{
      max-width: 1200px;
      margin: 0 auto 28px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border2);
    }}
    .page-header h1 {{
      font-size: 1.75rem;
      font-weight: 700;
      color: var(--t1);
      margin-bottom: 4px;
    }}
    .page-header .subtitle {{
      font-size: 0.95rem;
      color: var(--t2);
    }}
    .page-header .meta {{
      font-family: var(--mono);
      font-size: 0.82rem;
      color: var(--t3);
      margin-top: 6px;
    }}

    /* ── Stat cards ─────────────────────────────────────────── */
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 14px;
      max-width: 1200px;
      margin: 0 auto 32px;
    }}
    .stat-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
    }}
    .stat-card .label {{
      font-size: 0.78rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--t3);
      margin-bottom: 8px;
    }}
    .stat-card .value {{
      font-family: var(--mono);
      font-size: 1.5rem;
      font-weight: 700;
      color: var(--t1);
      line-height: 1;
    }}
    .stat-card .sub {{
      font-size: 0.8rem;
      color: var(--t2);
      margin-top: 5px;
    }}
    .stat-card.calls-card  .value {{ color: var(--blue); }}
    .stat-card.puts-card   .value {{ color: var(--red); }}
    .stat-card.pcr-bullish .value {{ color: var(--green); }}
    .stat-card.pcr-bearish .value {{ color: var(--red); }}
    .stat-card.pcr-neutral .value {{ color: var(--amber); }}

    /* ── Expiry sections ────────────────────────────────────── */
    .expiry-section {{
      max-width: 1200px;
      margin: 0 auto 28px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
    }}
    .expiry-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      padding: 14px 20px;
      background: var(--bg2);
      border-bottom: 1px solid var(--border);
    }}
    .expiry-name {{
      font-size: 1rem;
      font-weight: 700;
      color: var(--t1);
    }}
    .expiry-meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .meta-pill {{
      font-family: var(--mono);
      font-size: 0.78rem;
      font-weight: 500;
      padding: 3px 10px;
      border-radius: 20px;
      border: 1px solid var(--border2);
    }}
    .calls-pill  {{ color: var(--blue); border-color: var(--blue); }}
    .puts-pill   {{ color: var(--red);  border-color: var(--red); }}
    .pcr-bullish {{ color: var(--green); border-color: var(--green); }}
    .pcr-bearish {{ color: var(--red);   border-color: var(--red); }}
    .pcr-neutral {{ color: var(--amber); border-color: var(--amber); }}

    .chart-wrapper {{
      padding: 16px 20px;
      position: relative;
    }}

    /* ── Summary tables ─────────────────────────────────────── */
    .summary-section {{
      max-width: 1200px;
      margin: 0 auto 32px;
    }}
    .summary-section h2 {{
      font-size: 1.1rem;
      font-weight: 700;
      color: var(--t1);
      margin-bottom: 14px;
    }}
    .tables-row {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
    }}
    @media (max-width: 680px) {{
      .tables-row {{ grid-template-columns: 1fr; }}
    }}
    .table-block {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
    }}
    .table-block h3 {{
      font-size: 0.82rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 12px 14px 10px;
      background: var(--bg2);
      border-bottom: 1px solid var(--border);
    }}
    .table-block.calls-block h3 {{ color: var(--blue); }}
    .table-block.puts-block  h3 {{ color: var(--red); }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
    }}
    .data-table th {{
      background: var(--bg2);
      color: var(--t2);
      font-weight: 600;
      text-align: left;
      padding: 8px 12px;
      border-bottom: 1px solid var(--border2);
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .data-table td {{
      padding: 8px 12px;
      border-bottom: 1px solid var(--border);
      color: var(--t1);
      vertical-align: middle;
    }}
    .data-table tr:last-child td {{ border-bottom: none; }}
    .rank-cell   {{ font-family: var(--mono); color: var(--t3); width: 32px; text-align: center; }}
    .strike-cell {{ font-family: var(--mono); font-weight: 600; }}
    .expiry-cell {{ color: var(--t2); font-size: 0.82rem; }}
    .vol-cell    {{ min-width: 130px; }}
    .vol-bar-wrap {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .vol-bar-track {{
      width: 90px;
      flex-shrink: 0;
    }}
    .vol-bar {{
      height: 6px;
      border-radius: 3px;
      min-width: 4px;
    }}
    .calls-bar {{ background: var(--blue); }}
    .puts-bar  {{ background: var(--red); }}
    .vol-label {{ font-family: var(--mono); font-size: 0.82rem; color: var(--t2); white-space: nowrap; }}

    /* ── Footer ─────────────────────────────────────────────── */
    .footer {{
      max-width: 1200px;
      margin: 0 auto;
      text-align: center;
      font-size: 0.78rem;
      color: var(--t3);
      padding-top: 12px;
      border-top: 1px solid var(--border);
    }}
  </style>
</head>
<body>

<button id="theme-toggle" onclick="toggleTheme()">◐</button>

<!-- Header -->
<div class="page-header">
  <h1>🎯 BOVA11 Hunter Walls</h1>
  <p class="subtitle">Análise de Volume por Strike — Identificação de Muros de Opções</p>
  <p class="meta">Data de referência: {date_display} &nbsp;|&nbsp; Tag: {ref_tag}{spot_meta}</p>
</div>

<!-- Stat cards -->
<div class="cards-grid">
  <div class="stat-card">
    <div class="label">Volume Total</div>
    <div class="value">{fmt_vol(grand_total)}</div>
    <div class="sub">Calls + Puts — todos vencimentos</div>
  </div>
  <div class="stat-card calls-card">
    <div class="label">Calls Total</div>
    <div class="value">{fmt_vol(grand_call)}</div>
    <div class="sub">{calls_pct}</div>
  </div>
  <div class="stat-card puts-card">
    <div class="label">Puts Total</div>
    <div class="value">{fmt_vol(grand_put)}</div>
    <div class="sub">{puts_pct}</div>
  </div>
  <div class="stat-card {pcr_card_class}">
    <div class="label">PCR Geral</div>
    <div class="value">{grand_pcr:.2f}</div>
    <div class="sub">{pcr_label}</div>
  </div>
</div>

<!-- Per-expiry butterfly charts -->
{chart_blocks_html}

<!-- Summary tables -->
<div class="summary-section">
  <h2>📊 Top 5 por Volume — Todos os Vencimentos</h2>
  <div class="tables-row">
    <div class="table-block calls-block">
      <h3>Calls — Maiores Volumes</h3>
      <table class="data-table">
        <thead>
          <tr><th>#</th><th>Strike</th><th>Vencimento</th><th>Volume</th></tr>
        </thead>
        <tbody>{calls_table_rows}</tbody>
      </table>
    </div>
    <div class="table-block puts-block">
      <h3>Puts — Maiores Volumes</h3>
      <table class="data-table">
        <thead>
          <tr><th>#</th><th>Strike</th><th>Vencimento</th><th>Volume</th></tr>
        </thead>
        <tbody>{puts_table_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<div class="footer">
  BOVA11 Hunter Walls &middot; Módulo 13 &middot; Gerado em {generated_at}
</div>

<script>
// ── Theme ──────────────────────────────────────────────────────
(function(){{
  var t = localStorage.getItem('bova11-theme') || 'light';
  if (t === 'dark') {{
    document.documentElement.setAttribute('data-theme', 'dark');
    document.getElementById('theme-toggle').textContent = '◐';
  }}
}})();

function toggleTheme() {{
  var btn = document.getElementById('theme-toggle');
  if (document.documentElement.getAttribute('data-theme') === 'dark') {{
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('bova11-theme', 'light');
    btn.textContent = '◐';
  }} else {{
    document.documentElement.setAttribute('data-theme', 'dark');
    localStorage.setItem('bova11-theme', 'dark');
    btn.textContent = '◐';
  }}
}}

function chartTextColor() {{
  return document.documentElement.getAttribute('data-theme') === 'dark'
    ? '#8b949e'
    : '#6B6960';
}}

// ── Per-expiry charts ──────────────────────────────────────────
{chart_js_blocks}
</script>

</body>
</html>"""

    return html


# ═══════════════════════════════════════════════════════════════
# 6. MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="BOVA11 Hunter Walls — Análise de Volume por Strike (Módulo 13)"
    )
    parser.add_argument("--data-dir",  required=True,
                        help="Diretório com os CSVs de dados B3")
    parser.add_argument("--output",    required=True,
                        help="Caminho do arquivo HTML de saída")
    parser.add_argument("--ref-date",  required=True,
                        help="Data de referência ISO, ex: 2026-03-25")
    parser.add_argument("--ref-tag",   required=True,
                        help="Tag original do CSV, ex: 25posmar ou 25mar")
    parser.add_argument("--spot",      type=float, default=0.0,
                        help="Preço spot atual do BOVA11 (opcional, para anotação nos gráficos)")
    args = parser.parse_args()

    data_dir = args.data_dir
    output   = args.output
    ref_date = args.ref_date
    ref_tag  = args.ref_tag
    spot     = args.spot

    print(f"\n  BOVA11 Hunter Walls — Módulo 13/13")
    print(f"  Data: {ref_date}  |  Tag: {ref_tag}  |  Spot: {spot if spot > 0 else 'N/A'}")
    print(f"  Data dir: {data_dir}")

    # ── Find volume files ─────────────────────────────────────
    volume_files = find_volume_files(data_dir, ref_tag)

    if not volume_files:
        print(f"\n  AVISO: Nenhum arquivo de volume encontrado para tag '{ref_tag}'.")
        norm = normalize_tag(ref_tag)
        if norm != ref_tag.lower():
            print(f"         Também tentado tag normalizado: '{norm}'")
        print(f"         Padrão esperado: venc * fechamento ({ref_tag} Volume).csv")
        print(f"         Verifique se os arquivos estão em: {data_dir}")
        sys.exit(1)

    print(f"\n  Arquivos de volume encontrados: {len(volume_files)}")
    for expiry, fpath in volume_files:
        print(f"    [{expiry}]  {os.path.basename(fpath)}")

    # ── Load and process each expiry ─────────────────────────
    expiries_data: List[Tuple[str, dict]] = []

    for expiry, fpath in volume_files:
        rows = load_volume_csv(fpath)
        if not rows:
            print(f"  AVISO: Sem dados válidos em {os.path.basename(fpath)}, pulando.")
            continue
        agg   = aggregate_by_strike(rows)
        stats = compute_expiry_stats(agg)
        expiries_data.append((expiry, stats))
        print(f"  OK  {expiry}: {len(agg)} strikes | "
              f"Calls {fmt_vol(stats['total_call'])} | "
              f"Puts {fmt_vol(stats['total_put'])} | "
              f"PCR {stats['pcr']:.2f}")

    if not expiries_data:
        print("\n  ERRO: Nenhum dado de volume válido encontrado após leitura dos CSVs.")
        sys.exit(1)

    # ── Sort expiries by calendar date ────────────────────────
    expiries_data.sort(key=lambda x: expiry_sort_key(x[0]))

    # ── Generate HTML ─────────────────────────────────────────
    html = build_html(ref_date, ref_tag, spot, expiries_data)

    out_dir = os.path.dirname(os.path.abspath(output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"\n  HTML gerado: {output}")
    sys.exit(0)


if __name__ == "__main__":
    main()
