#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 — GEX History (Market Maker Gamma Exposure Historico)
=============================================================
Le os ultimos 5 dias uteis de CSVs B3, calcula GEX por strike,
gamma flip, regime e gera dashboard HTML interativo.

Dependencias: Python 3 stdlib apenas (os, re, glob, json, math, argparse, datetime)

Uso:
  python3 bova11_gex_history.py --data-dir /path/to/data --output /path/to/out.html --ref-date 2026-03-25
"""

import argparse
import glob
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta

from bova11_shared import calc_gex_components, resolve_spot


# ===============================================================
# 1. PARSER B3
# ===============================================================

def _p(raw) -> float:
    """Converte numero BR ('1.234,56' / '123,91k' / '8,19M' / '-' / '%') -> float."""
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


def load_b3_csv(path: str) -> list:
    """Le CSV de fechamento B3 e retorna lista de dicts por strike."""
    try:
        with open(path, "r", encoding="latin-1") as f:
            lines = [line.rstrip("\n") for line in f.readlines()]
    except Exception:
        return []
    rows = []
    for line in lines[1:]:
        c = line.strip().split(";")
        if len(c) < 23:
            continue
        strike = _p(c[11])
        if strike <= 0:
            continue
        rows.append({
            "strike":     strike,
            "call_oi":    _p(c[2]),
            "call_gamma": _p(c[4]),
            "put_oi":     _p(c[20]),
            "put_gamma":  _p(c[18]),
        })
    return rows


# ===============================================================
# 2. DESCOBERTA DE DATAS
# ===============================================================

MESES_NUM = {
    "jan": "01", "fev": "02", "mar": "03", "abr": "04",
    "mai": "05", "jun": "06", "jul": "07", "ago": "08",
    "set": "09", "out": "10", "nov": "11", "dez": "12",
}

MESES_LABEL = {
    "01": "Jan", "02": "Fev", "03": "Mar", "04": "Abr",
    "05": "Mai", "06": "Jun", "07": "Jul", "08": "Ago",
    "09": "Set", "10": "Out", "11": "Nov", "12": "Dez",
}


def extract_tag(filename: str):
    """Extrai tag de data do nome do arquivo."""
    m = re.search(r'fechamento \(([a-zA-Z0-9]+)\)\.csv$', filename)
    if m:
        return m.group(1)
    m = re.search(r'fechamento__([a-zA-Z0-9]+)_\.csv$', filename)
    if m:
        return m.group(1)
    return None


def normalize_tag(tag: str) -> str:
    """Remove sufixos pos/pre do tag: '25posmar' -> '25mar'."""
    return re.sub(r'(pos|pre)([a-z]{3})$', r'\2', tag.lower())

def tag_sort_key(tag: str) -> tuple:
    """Ordena tags cronologicamente usando a própria data."""
    norm = normalize_tag(tag)
    m = re.match(r'(\d{1,2})(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)$', norm)
    if m:
        return (int(MESES_NUM.get(m.group(2), "99")), int(m.group(1)), norm)
    return (99, 99, str(tag).lower())

def is_primary_fechamento_file(filename: str) -> bool:
    lower = filename.lower()
    return (
        lower.endswith('.csv')
        and 'fechamento' in lower
        and 'volume' not in lower
        and ' copy' not in lower
    )


def tag_to_iso(tag: str) -> str:
    """Converte tag '25mar' -> '2026-03-25' (ano corrente)."""
    norm = normalize_tag(tag)
    m = re.match(r'(\d{1,2})(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)$', norm)
    if m:
        dia = m.group(1).zfill(2)
        mes = MESES_NUM.get(m.group(2), "01")
        ano = str(datetime.now().year)
        return f"{ano}-{mes}-{dia}"
    return tag


def fetch_spot_close(ref_iso: str):
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        ref_day = datetime.strptime(ref_iso, "%Y-%m-%d").date()
    except Exception:
        return None
    start = (ref_day - timedelta(days=7)).isoformat()
    end = (ref_day + timedelta(days=7)).isoformat()
    try:
        hist = yf.Ticker("BOVA11.SA").history(start=start, end=end, interval="1d", auto_adjust=False)
    except Exception:
        return None
    if hist is None or len(hist) == 0:
        return None
    by_day = {}
    for idx, row in hist.iterrows():
        try:
            by_day[idx.date().isoformat()] = float(row["Close"])
        except Exception:
            continue
    if ref_iso in by_day:
        return by_day[ref_iso]
    prev = [k for k in by_day if k <= ref_iso]
    if prev:
        return by_day[sorted(prev)[-1]]
    return None


def tag_to_display(tag: str) -> str:
    """Converte tag '25mar' -> '25/Mar'."""
    norm = normalize_tag(tag)
    m = re.match(r'(\d{1,2})(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)$', norm)
    if m:
        dia = m.group(1)
        mes_abbr = MESES_NUM.get(m.group(2), "01")
        return f"{dia}/{MESES_LABEL.get(mes_abbr, m.group(2).capitalize())}"
    return tag


def discover_date_tags(data_dir: str, max_dates: int = 5) -> list:
    """
    Descobre as ultimas N tags de data unicas dos CSVs em data_dir,
    ordenadas cronologicamente pela tag (mais recente primeiro).
    Retorna lista de tags originais (strings).
    """
    pattern_space = os.path.join(data_dir, "venc * fechamento (*).csv")
    pattern_under = os.path.join(data_dir, "venc_*_fechamento__*_.csv")

    tag_map = {}  # tag_normalizado -> tag_original
    for fpath in glob.glob(pattern_space) + glob.glob(pattern_under):
        filename = os.path.basename(fpath)
        if not is_primary_fechamento_file(filename):
            continue
        tag = extract_tag(filename)
        if not tag:
            continue
        norm = normalize_tag(tag)
        if norm not in tag_map:
            tag_map[norm] = tag

    sorted_tags = sorted(tag_map.values(), key=tag_sort_key, reverse=True)
    return sorted_tags[:max_dates]


# ===============================================================
# 3. CALCULO DE GEX
# ===============================================================

def compute_gex_for_tag(data_dir: str, tag: str, spot: float) -> dict:
    """
    Carrega todos os CSVs de fechamento para o tag dado (todos os vencimentos),
    agrega GEX por strike e retorna dict com resultados completos.
    """
    norm_tag = normalize_tag(tag)

    # Buscar arquivos com esse tag exato
    pattern_space = os.path.join(data_dir, f"venc * fechamento ({tag}).csv")
    pattern_under = os.path.join(data_dir, f"venc_*_fechamento__{tag}_.csv")
    all_files = glob.glob(pattern_space) + glob.glob(pattern_under)

    # Fallback: buscar pelo tag normalizado caso o tag original nao encontre nada
    if not all_files:
        for fpath in glob.glob(os.path.join(data_dir, "venc * fechamento (*).csv")):
            if "Volume" in os.path.basename(fpath):
                continue
            t = extract_tag(os.path.basename(fpath))
            if t and normalize_tag(t) == norm_tag:
                all_files.append(fpath)
        for fpath in glob.glob(os.path.join(data_dir, "venc_*_fechamento__*_.csv")):
            if "Volume" in os.path.basename(fpath):
                continue
            t = extract_tag(os.path.basename(fpath))
            if t and normalize_tag(t) == norm_tag:
                all_files.append(fpath)

    # Deduplicar e remover volumes
    seen = set()
    files_clean = []
    for f in all_files:
        if "Volume" in os.path.basename(f):
            continue
        key = os.path.abspath(f)
        if key not in seen:
            seen.add(key)
            files_clean.append(f)

    if not files_clean:
        return {}

    # Agregar GEX por strike (soma de todos os vencimentos)
    gex_by_strike = {}  # strike -> {gex_call, gex_put, gex_net}

    for fpath in files_clean:
        rows = load_b3_csv(fpath)
        for row in rows:
            s = row["strike"]
            gex_call, gex_put, gex_net = calc_gex_components(
                call_gamma=row["call_gamma"],
                put_gamma=row["put_gamma"],
                call_oi=row["call_oi"],
                put_oi=row["put_oi"],
                spot=spot,
            )

            if s not in gex_by_strike:
                gex_by_strike[s] = {"gex_call": 0.0, "gex_put": 0.0, "gex_net": 0.0}
            gex_by_strike[s]["gex_call"] += gex_call
            gex_by_strike[s]["gex_put"]  += gex_put
            gex_by_strike[s]["gex_net"]  += gex_net

    if not gex_by_strike:
        return {}

    # Ordenar por strike ascendente
    strikes_sorted = sorted(gex_by_strike.keys())
    gex_net_list   = [gex_by_strike[s]["gex_net"] for s in strikes_sorted]

    # GEX total
    total_gex = sum(gex_net_list)

    # GEX Descoberto: soma de |gex_net| dos strikes com sinal oposto ao regime
    if total_gex >= 0:
        gex_descoberto = sum(abs(v) for v in gex_net_list if v < 0)
    else:
        gex_descoberto = sum(abs(v) for v in gex_net_list if v > 0)

    # GEX Cumulativo (running sum, strike mais baixo para mais alto)
    cum_gex = []
    acc = 0.0
    for v in gex_net_list:
        acc += v
        cum_gex.append(acc)

    # Gamma Flip: strike onde o GEX cumulativo cruza zero (interpolacao linear)
    gamma_flip = None
    for i in range(len(cum_gex) - 1):
        if cum_gex[i] == 0.0:
            gamma_flip = strikes_sorted[i]
            break
        if (cum_gex[i] < 0) != (cum_gex[i + 1] < 0):
            s0, s1 = strikes_sorted[i], strikes_sorted[i + 1]
            c0, c1 = cum_gex[i], cum_gex[i + 1]
            if c1 != c0:
                gamma_flip = s0 + (s1 - s0) * (-c0 / (c1 - c0))
            else:
                gamma_flip = (s0 + s1) / 2.0
            break

    # Regime
    regime = "Long Gamma" if total_gex >= 0 else "Short Gamma"

    # Top 5 strikes por |gex_net|
    top5 = sorted(
        [{"strike": s, "gex_net": gex_by_strike[s]["gex_net"]} for s in strikes_sorted],
        key=lambda x: abs(x["gex_net"]),
        reverse=True
    )[:5]

    return {
        "tag":              tag,
        "iso_date":         tag_to_iso(tag),
        "display":          tag_to_display(tag),
        "spot":             round(float(spot), 2),
        "strikes":          strikes_sorted,
        "gex_call":         [gex_by_strike[s]["gex_call"] for s in strikes_sorted],
        "gex_put":          [gex_by_strike[s]["gex_put"]  for s in strikes_sorted],
        "gex_net":          gex_net_list,
        "cum_gex":          cum_gex,
        "gamma_flip":       gamma_flip,
        "total_gex_b":      total_gex / 1e9,
        "gex_descoberto_b": gex_descoberto / 1e9,
        "regime":           regime,
        "top5":             top5,
    }


# ===============================================================
# 4. HELPERS DE FORMATACAO
# ===============================================================

def fmt_b(val: float) -> str:
    """Formata valor em bilhoes/milhoes com sinal (ex: +1.23B, -456.7M)."""
    if val == 0:
        return "0"
    abs_v = abs(val)
    sign = "+" if val > 0 else "-"
    if abs_v >= 1.0:
        return f"{sign}{abs_v:.2f}B"
    else:
        return f"{sign}{abs_v * 1000:.1f}M"


# ===============================================================
# 5. GERACAO DO HTML
# ===============================================================

def build_html(dates_data: list, ref_date_iso: str) -> str:
    """Constroi o HTML completo auto-contido com todos os dados embutidos."""

    if not dates_data:
        return "<html><body><p>Sem dados disponíveis.</p></body></html>"

    # Data mais recente = indice 0 (lista ja ordenada descendente por mtime)
    latest = dates_data[0]

    n_dias       = len(dates_data)
    regime_atual = latest["regime"]
    regime_badge = "long" if regime_atual == "Long Gamma" else "short"

    # Variacao de GEX Descoberto entre periodo mais antigo e mais recente
    if len(dates_data) >= 2:
        oldest   = dates_data[-1]
        gd_new   = latest["gex_descoberto_b"]
        gd_old   = oldest["gex_descoberto_b"]
        if gd_old != 0:
            var_gd_pct = (gd_new - gd_old) / abs(gd_old) * 100
            var_gd_str = f"{var_gd_pct:+.1f}%"
        else:
            var_gd_str = "N/A"

        gf_new = latest["gamma_flip"]
        gf_old = oldest["gamma_flip"]
        if gf_new is not None and gf_old is not None and gf_old != 0:
            var_gf_pct = (gf_new - gf_old) / gf_old * 100
            var_gf_str = f"{gf_old:.1f} &rarr; {gf_new:.1f} ({var_gf_pct:+.1f}%)"
        elif gf_new is not None:
            var_gf_str = f"{gf_new:.1f}"
        else:
            var_gf_str = "N/A"
    else:
        var_gd_str = "N/A"
        gf_new     = latest["gamma_flip"]
        var_gf_str = f"{gf_new:.1f}" if gf_new is not None else "N/A"

    # Mudancas de regime entre datas consecutivas
    regime_changes = []
    for i in range(1, len(dates_data)):
        prev = dates_data[i]    # mais antigo
        curr = dates_data[i-1]  # mais recente
        if prev["regime"] != curr["regime"]:
            regime_changes.append({
                "date":   curr["display"],
                "from_r": prev["regime"],
                "to_r":   curr["regime"],
            })

    # HTML: Top 5 strikes
    top5_rows = []
    for item in latest["top5"]:
        direction  = "LONG" if item["gex_net"] > 0 else "SHORT"
        dir_style  = "color:var(--green)" if direction == "LONG" else "color:var(--red)"
        val_str    = fmt_b(item["gex_net"] / 1e9)
        top5_rows.append(
            f'<div class="top5-item">'
            f'<span class="top5-strike">K {item["strike"]:.0f}</span>'
            f'<span class="top5-dir" style="{dir_style}">MORE {direction}</span>'
            f'<span class="top5-val">{val_str}</span>'
            f'</div>'
        )
    top5_html = "\n".join(top5_rows)

    # HTML: mudancas de regime
    if regime_changes:
        rc_rows = []
        for rc in regime_changes:
            fs = "color:var(--green)" if rc["from_r"] == "Long Gamma" else "color:var(--red)"
            ts = "color:var(--green)" if rc["to_r"]   == "Long Gamma" else "color:var(--red)"
            rc_rows.append(
                f'<div class="regime-change-item">'
                f'<span class="rc-date">{rc["date"]}</span>'
                f'<span style="{fs}">{rc["from_r"]}</span>'
                f'&nbsp;&rarr;&nbsp;'
                f'<span style="{ts}">{rc["to_r"]}</span>'
                f'</div>'
            )
        rc_html = "\n".join(rc_rows)
    else:
        rc_html = '<div class="regime-change-item" style="color:var(--t3)">Nenhuma mudança no período</div>'

    gf_display       = f"{latest['gamma_flip']:.1f}" if latest["gamma_flip"] is not None else "N/A"
    total_gex_str    = fmt_b(latest["total_gex_b"])
    gex_desc_str     = fmt_b(latest["gex_descoberto_b"])
    spot_label       = latest["display"]
    total_sign_class = "long" if latest["total_gex_b"] >= 0 else "short"

    # HTML: dots da timeline
    tl_dots = []
    for i, d in enumerate(dates_data):
        active = "active" if i == 0 else ""
        tl_dots.append(
            f'<div class="tl-dot {active}" data-idx="{i}" onclick="selectDate({i})">'
            f'<div class="tl-dot-circle"></div>'
            f'<div class="tl-dot-label">{d["display"]}</div>'
            f'</div>'
        )
    timeline_html = "\n".join(tl_dots)

    # Dados serializados para JS
    js_data = json.dumps(dates_data, ensure_ascii=False)

    # ------------------------------------------------------------------
    # HTML final
    # IMPORTANTE: dentro de f-string Python, todas as chaves CSS/JS
    # literais usam {{ }} para escapar.
    # ------------------------------------------------------------------
    html = f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BOVA11 — GEX History</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
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
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{
      font-family: var(--font);
      background: var(--bg);
      color: var(--t1);
      min-height: 100vh;
      padding: 24px 16px 48px;
    }}
    #theme-toggle {{
      position: fixed; top: 16px; right: 16px; z-index: 999;
      background: var(--card); border: 1px solid var(--border2);
      border-radius: 8px; padding: 6px 10px; cursor: pointer;
      font-size: 16px; line-height: 1;
      box-shadow: 0 2px 8px rgba(0,0,0,0.12);
    }}
    .page-wrapper {{ max-width: 1100px; margin: 0 auto; }}
    h1 {{ font-size: 1.6rem; font-weight: 700; color: var(--t1); margin-bottom: 4px; }}
    .subtitle {{ color: var(--t2); font-size: 0.9rem; margin-bottom: 28px; }}

    /* ── Cards ── */
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 14px;
      margin-bottom: 28px;
    }}
    .card {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 16px 18px;
    }}
    .card-label {{
      font-size: 0.75rem; color: var(--t3);
      text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px;
    }}
    .card-value {{
      font-size: 1.4rem; font-weight: 700;
      font-family: var(--mono); color: var(--t1);
    }}
    .card-value.long {{ color: var(--green); }}
    .card-value.short {{ color: var(--red); }}
    .badge {{
      display: inline-block; padding: 3px 10px; border-radius: 20px;
      font-size: 0.8rem; font-weight: 600;
    }}
    .badge.long  {{ background: rgba(20,138,99,0.15);  color: var(--green); }}
    .badge.short {{ background: rgba(179,53,48,0.15); color: var(--red); }}

    /* ── Insights ── */
    .insights-section {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 20px 22px; margin-bottom: 28px;
    }}
    .insights-section h2 {{
      font-size: 1rem; font-weight: 600; margin-bottom: 14px; color: var(--t1);
    }}
    .insights-grid {{
      display: grid; grid-template-columns: 1fr 1fr; gap: 16px 28px;
    }}
    @media (max-width: 600px) {{ .insights-grid {{ grid-template-columns: 1fr; }} }}
    .insight-row {{ display: flex; flex-direction: column; gap: 4px; }}
    .insight-label {{
      font-size: 0.75rem; color: var(--t3);
      text-transform: uppercase; letter-spacing: 0.04em;
    }}
    .insight-value {{ font-size: 0.95rem; font-weight: 500; color: var(--t1); }}

    /* ── Top 5 ── */
    .top5-list {{ display: flex; flex-direction: column; gap: 6px; }}
    .top5-item {{
      display: flex; align-items: center; gap: 10px;
      background: var(--bg2); border-radius: 8px; padding: 7px 12px;
    }}
    .top5-strike {{
      font-family: var(--mono); font-size: 0.9rem;
      font-weight: 600; min-width: 60px;
    }}
    .top5-dir {{
      font-size: 0.75rem; font-weight: 700;
      text-transform: uppercase; flex: 1;
    }}
    .top5-val {{ font-family: var(--mono); font-size: 0.88rem; color: var(--t2); }}

    /* ── Regime changes ── */
    .regime-changes-list {{ display: flex; flex-direction: column; gap: 6px; }}
    .regime-change-item {{
      display: flex; align-items: center; gap: 8px;
      font-size: 0.88rem; padding: 5px 0;
    }}
    .rc-date {{ font-weight: 600; min-width: 50px; }}

    /* ── Timeline ── */
    .timeline-section {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 20px 22px; margin-bottom: 28px;
    }}
    .timeline-section h2 {{
      font-size: 1rem; font-weight: 600; margin-bottom: 16px; color: var(--t1);
    }}
    .timeline-strip {{
      display: flex; align-items: flex-start;
      position: relative; padding: 8px 0 24px;
    }}
    .timeline-strip::before {{
      content: '';
      position: absolute; top: 16px; left: 0; right: 0; height: 2px;
      background: var(--border2);
    }}
    .tl-dot {{
      flex: 1; display: flex; flex-direction: column; align-items: center;
      cursor: pointer; position: relative; z-index: 1;
    }}
    .tl-dot-circle {{
      width: 14px; height: 14px; border-radius: 50%;
      background: var(--bg3); border: 2px solid var(--border2);
      transition: all 0.2s;
    }}
    .tl-dot:hover .tl-dot-circle {{ background: var(--blue); border-color: var(--blue); }}
    .tl-dot.active .tl-dot-circle {{
      background: var(--blue); border-color: var(--blue);
      width: 18px; height: 18px; margin-top: -2px;
    }}
    .tl-dot-label {{
      margin-top: 8px; font-size: 0.78rem; color: var(--t2);
      font-weight: 500; text-align: center;
    }}
    .tl-dot.active .tl-dot-label {{ color: var(--blue); font-weight: 700; }}

    /* ── Charts ── */
    .chart-section {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 20px 22px; margin-bottom: 24px;
    }}
    .chart-section h2 {{
      font-size: 1rem; font-weight: 600; margin-bottom: 14px; color: var(--t1);
    }}
    canvas {{ max-height: 340px; }}
    .charts-row {{
      display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
    }}
    @media (max-width: 700px) {{ .charts-row {{ grid-template-columns: 1fr; }} }}
    .chart-col {{ display: flex; flex-direction: column; }}
    .chart-col h3 {{
      font-size: 0.88rem; color: var(--t2); margin-bottom: 10px; font-weight: 600;
    }}
    .info-chip {{
      display: inline-flex; align-items: center; gap: 6px;
      background: var(--bg2); border: 1px solid var(--border);
      border-radius: 20px; padding: 4px 12px;
      font-size: 0.8rem; color: var(--t2); margin-bottom: 12px;
    }}
    .info-chip .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
  </style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()">◐</button>
<script>
(function(){{ var t=localStorage.getItem('bova11-theme')||'light'; if(t==='dark'){{document.documentElement.setAttribute('data-theme','dark');document.getElementById('theme-toggle').textContent='◐';}} }})();
function toggleTheme(){{ var btn=document.getElementById('theme-toggle'); if(document.documentElement.getAttribute('data-theme')==='dark'){{document.documentElement.removeAttribute('data-theme');localStorage.setItem('bova11-theme','light');btn.textContent='◐';}}else{{document.documentElement.setAttribute('data-theme','dark');localStorage.setItem('bova11-theme','dark');btn.textContent='◐';}} }}
</script>

<div class="page-wrapper">

  <h1>BOVA11 Market Maker — Histórico GEX</h1>
  <p class="subtitle">Gamma Exposure por strike · últimos {n_dias} dias úteis · referência {spot_label}</p>

  <!-- Summary Cards -->
  <div class="cards-grid">
    <div class="card">
      <div class="card-label">Referência</div>
      <div class="card-value" id="card-ref">{spot_label}</div>
    </div>
    <div class="card">
      <div class="card-label">Gamma Flip</div>
      <div class="card-value" id="card-gf">{gf_display}</div>
    </div>
    <div class="card">
      <div class="card-label">GEX Total</div>
      <div class="card-value {total_sign_class}" id="card-gex-total">{total_gex_str}</div>
    </div>
    <div class="card">
      <div class="card-label">GEX Descoberto</div>
      <div class="card-value" id="card-gex-desc">{gex_desc_str}</div>
    </div>
    <div class="card">
      <div class="card-label">Regime</div>
      <div class="card-value">
        <span class="badge {regime_badge}" id="card-regime">{regime_atual}</span>
      </div>
    </div>
  </div>

  <!-- Insights -->
  <div class="insights-section">
    <h2>Análise do Período</h2>
    <div class="insights-grid">
      <div class="insight-row">
        <span class="insight-label">Período Analisado</span>
        <span class="insight-value">{n_dias} dias úteis</span>
      </div>
      <div class="insight-row">
        <span class="insight-label">Regime Atual</span>
        <span class="insight-value">
          <span class="badge {regime_badge}">{regime_atual}</span>
        </span>
      </div>
      <div class="insight-row">
        <span class="insight-label">Variação GEX Descoberto</span>
        <span class="insight-value">{var_gd_str}</span>
      </div>
      <div class="insight-row">
        <span class="insight-label">Gamma Flip no Período</span>
        <span class="insight-value" style="font-family:var(--mono)">{var_gf_str}</span>
      </div>
      <div class="insight-row" style="grid-column:1 / -1">
        <span class="insight-label">Top 5 Strikes Mais Impactados (data mais recente)</span>
        <div class="top5-list" style="margin-top:8px">
          {top5_html}
        </div>
      </div>
      <div class="insight-row" style="grid-column:1 / -1">
        <span class="insight-label">Mudanças de Regime</span>
        <div class="regime-changes-list" style="margin-top:8px">
          {rc_html}
        </div>
      </div>
    </div>
  </div>

  <!-- Timeline -->
  <div class="timeline-section">
    <h2>Selecionar Data</h2>
    <div class="timeline-strip" id="timeline">
      {timeline_html}
    </div>
  </div>

  <!-- Charts -->
  <div class="chart-section">
    <h2>GEX por Strike — <span id="chart-date-label">{latest["display"]}</span></h2>
    <div id="regime-chips" style="margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap;"></div>
    <div class="charts-row">
      <div class="chart-col">
        <h3>GEX Líquido por Strike</h3>
        <canvas id="gexBarChart"></canvas>
      </div>
      <div class="chart-col">
        <h3>GEX Cumulativo</h3>
        <canvas id="gexCumChart"></canvas>
      </div>
    </div>
  </div>

</div><!-- /page-wrapper -->

<script>
// ── Dados embutidos ──────────────────────────────────────────
const ALL_DATA = {js_data};

// ── Estado ───────────────────────────────────────────────────
let currentIdx = 0;
let barChart   = null;
let cumChart   = null;

// ── Helpers ──────────────────────────────────────────────────
function isDark() {{
  return document.documentElement.getAttribute('data-theme') === 'dark';
}}
function gridColor() {{
  return isDark() ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.07)';
}}
function tickColor() {{
  return isDark() ? '#8b949e' : '#6B6960';
}}
function fmtRaw(val) {{
  if (val === 0) return '0';
  const abs  = Math.abs(val);
  const sign = val > 0 ? '+' : '-';
  if (abs >= 1e9) return sign + (abs / 1e9).toFixed(2) + 'B';
  if (abs >= 1e6) return sign + (abs / 1e6).toFixed(1) + 'M';
  return sign + abs.toFixed(0);
}}
function fmtBilhoes(val) {{
  if (val === 0) return '0';
  const abs  = Math.abs(val);
  const sign = val > 0 ? '+' : '-';
  if (abs >= 1) return sign + abs.toFixed(2) + 'B';
  return sign + (abs * 1000).toFixed(1) + 'M';
}}

// ── Plugin: linha vertical do Gamma Flip ─────────────────────
function makeFlipPlugin(d) {{
  return {{
    id: 'flipLine',
    afterDraw(chart) {{
      const gammaFlip = d.gamma_flip;
      if (gammaFlip === null || gammaFlip === undefined) return;
      const ctx    = chart.ctx;
      const xAxis  = chart.scales.x;
      const yAxis  = chart.scales.y;
      const sarr   = d.strikes;
      let xPx = null;
      for (let i = 0; i < sarr.length - 1; i++) {{
        if (gammaFlip >= sarr[i] && gammaFlip <= sarr[i + 1]) {{
          const t  = (gammaFlip - sarr[i]) / (sarr[i + 1] - sarr[i]);
          const x0 = xAxis.getPixelForValue(i);
          const x1 = xAxis.getPixelForValue(i + 1);
          xPx = x0 + t * (x1 - x0);
          break;
        }}
      }}
      if (xPx === null) {{
        const idx2 = sarr.findIndex(s => Math.abs(s - gammaFlip) < 0.5);
        if (idx2 >= 0) xPx = xAxis.getPixelForValue(idx2);
      }}
      if (xPx === null) return;
      const dark = isDark();
      ctx.save();
      ctx.beginPath();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = dark ? '#d29922' : '#B8720A';
      ctx.lineWidth   = 2;
      ctx.moveTo(xPx, yAxis.top);
      ctx.lineTo(xPx, yAxis.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle  = dark ? '#d29922' : '#B8720A';
      ctx.font       = 'bold 11px monospace';
      ctx.textAlign  = 'center';
      ctx.fillText('Flip ' + gammaFlip.toFixed(1), xPx, yAxis.top - 4);
      ctx.restore();
    }}
  }};
}}

// ── Renderizar charts ────────────────────────────────────────
function renderCharts(idx) {{
  const d = ALL_DATA[idx];
  if (!d) return;

  const strikes = d.strikes.map(s => s.toFixed(0));
  const gexNet  = d.gex_net;
  const cumGex  = d.cum_gex;
  const dark    = isDark();

  const barColors = gexNet.map(v => v >= 0
    ? (dark ? 'rgba(63,185,80,0.85)'  : 'rgba(20,138,99,0.85)')
    : (dark ? 'rgba(248,81,73,0.85)'  : 'rgba(179,53,48,0.85)')
  );
  const barBorders = gexNet.map(v => v >= 0
    ? (dark ? '#3fb950' : '#148A63')
    : (dark ? '#f85149' : '#B33530')
  );

  if (barChart) {{ barChart.destroy(); barChart = null; }}
  if (cumChart) {{ cumChart.destroy(); cumChart = null; }}

  const flipPlugin = makeFlipPlugin(d);

  // Bar chart — GEX liquido por strike
  const barCtx = document.getElementById('gexBarChart').getContext('2d');
  barChart = new Chart(barCtx, {{
    type: 'bar',
    data: {{
      labels: strikes,
      datasets: [{{
        label: 'GEX Net',
        data: gexNet,
        backgroundColor: barColors,
        borderColor: barBorders,
        borderWidth: 1,
        borderRadius: 2,
      }}]
    }},
    options: {{
      responsive: true,
      animation: {{ duration: 300 }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => 'GEX: ' + fmtRaw(ctx.raw)
          }}
        }}
      }},
      scales: {{
        x: {{
          ticks: {{ color: tickColor(), font: {{ size: 10 }}, maxRotation: 45 }},
          grid: {{ color: gridColor() }}
        }},
        y: {{
          ticks: {{ color: tickColor(), callback: v => fmtRaw(v) }},
          grid: {{ color: gridColor() }}
        }}
      }}
    }},
    plugins: [flipPlugin]
  }});

  // Line chart — GEX cumulativo
  const cumCtx = document.getElementById('gexCumChart').getContext('2d');
  cumChart = new Chart(cumCtx, {{
    type: 'line',
    data: {{
      labels: strikes,
      datasets: [
        {{
          label: 'GEX Cumulativo',
          data: cumGex,
          borderColor: dark ? '#58a6ff' : '#2E6BBF',
          backgroundColor: 'transparent',
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 2,
          tension: 0.3,
          fill: false,
        }},
        {{
          label: 'Zero',
          data: new Array(strikes.length).fill(0),
          borderColor: dark ? 'rgba(255,255,255,0.2)' : 'rgba(0,0,0,0.15)',
          borderWidth: 1,
          borderDash: [4, 4],
          pointRadius: 0,
          fill: false,
        }}
      ]
    }},
    options: {{
      responsive: true,
      animation: {{ duration: 300 }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => ctx.datasetIndex === 0 ? 'Cum: ' + fmtRaw(ctx.raw) : ''
          }}
        }}
      }},
      scales: {{
        x: {{
          ticks: {{ color: tickColor(), font: {{ size: 10 }}, maxRotation: 45 }},
          grid: {{ color: gridColor() }}
        }},
        y: {{
          ticks: {{ color: tickColor(), callback: v => fmtRaw(v) }},
          grid: {{ color: gridColor() }}
        }}
      }}
    }},
    plugins: [flipPlugin]
  }});
}}

// ── Atualizar cards e chips ───────────────────────────────────
function updateCards(idx) {{
  const d  = ALL_DATA[idx];
  if (!d) return;

  const gf = (d.gamma_flip !== null && d.gamma_flip !== undefined)
    ? d.gamma_flip.toFixed(1) : 'N/A';

  document.getElementById('card-ref').textContent  = d.display;
  document.getElementById('card-gf').textContent   = gf;

  const totalEl = document.getElementById('card-gex-total');
  totalEl.textContent = fmtBilhoes(d.total_gex_b);
  totalEl.className   = 'card-value ' + (d.total_gex_b >= 0 ? 'long' : 'short');

  document.getElementById('card-gex-desc').textContent = fmtBilhoes(d.gex_descoberto_b);

  const regEl = document.getElementById('card-regime');
  regEl.textContent = d.regime;
  regEl.className   = 'badge ' + (d.regime === 'Long Gamma' ? 'long' : 'short');

  document.getElementById('chart-date-label').textContent = d.display;

  // Chip de regime
  const chipsDiv = document.getElementById('regime-chips');
  chipsDiv.innerHTML = '';
  const dotColor = d.regime === 'Long Gamma' ? '#3fb950' : '#f85149';
  const chip = document.createElement('div');
  chip.className = 'info-chip';
  chip.innerHTML =
    '<span class="dot" style="background:' + dotColor + '"></span>'
    + d.regime + ' \u2014 Gamma Flip: ' + gf;
  chipsDiv.appendChild(chip);
}}

// ── Selecao de data na timeline ───────────────────────────────
function selectDate(idx) {{
  currentIdx = idx;
  document.querySelectorAll('.tl-dot').forEach((el, i) => {{
    el.classList.toggle('active', i === idx);
  }});
  updateCards(idx);
  renderCharts(idx);
}}

// ── Re-render ao trocar tema (re-escreve toggleTheme global) ──
const _origToggle = window.toggleTheme;
window.toggleTheme = function() {{
  _origToggle();
  setTimeout(() => renderCharts(currentIdx), 50);
}};

// ── Inicializacao ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {{
  updateCards(0);
  renderCharts(0);
}});
</script>
</body>
</html>"""

    return html


# ===============================================================
# 6. MAIN
# ===============================================================

def main():
    parser = argparse.ArgumentParser(
        description="BOVA11 GEX History — Historico de Gamma Exposure (modulo 14)"
    )
    parser.add_argument("--data-dir",  required=True, help="Diretório com os CSVs B3")
    parser.add_argument("--output",    required=True, help="Caminho do HTML de saída")
    parser.add_argument("--ref-date",  required=True, help="Data de referência ISO (ex: 2026-03-25)")
    parser.add_argument("--spot", type=float, default=None, help="Spot manual para a data de referência")
    parser.add_argument("--spot-history-file", default=os.path.join(os.path.dirname(__file__), "..", "history", "bova11_spot_history.json"))
    args = parser.parse_args()

    data_dir    = args.data_dir
    output_path = args.output
    ref_date    = args.ref_date

    print(f"[GEX History] Data dir : {data_dir}")
    print(f"[GEX History] Output   : {output_path}")
    print(f"[GEX History] Ref date : {ref_date}")

    # 1. Descobrir as ultimas 5 tags de data
    tags = discover_date_tags(data_dir, max_dates=5)
    if not tags:
        print(f"[GEX History] ERRO: Nenhum arquivo CSV encontrado em {data_dir}")
        return 1

    print(f"[GEX History] Tags encontradas: {tags}")

    # 2. Computar GEX para cada tag
    dates_data = []
    for tag in tags:
        print(f"[GEX History] Processando tag: {tag} ...")
        iso_date = tag_to_iso(tag)
        explicit_spot = args.spot if iso_date == ref_date else None
        spot, spot_source, spot_warning = resolve_spot(
            spot=explicit_spot,
            spot_history_file=args.spot_history_file,
            ref_date=iso_date,
            ref_tag=tag,
            fetcher=fetch_spot_close,
        )
        if spot is None or spot <= 0:
            print(f"[GEX History] AVISO: Spot indisponível para tag '{tag}' ({spot_warning or 'sem detalhe'}), pulando.")
            continue
        result = compute_gex_for_tag(data_dir, tag, float(spot))
        if result:
            result["spot_source"] = spot_source
            dates_data.append(result)
        else:
            print(f"[GEX History] AVISO: Sem dados para tag '{tag}', pulando.")

    if not dates_data:
        print("[GEX History] ERRO: Nenhum dado GEX calculado. Verifique os CSVs.")
        return 1

    print(f"[GEX History] {len(dates_data)} data(s) com dados calculados.")

    # 3. Gerar HTML
    html = build_html(dates_data, ref_date)

    # 4. Salvar
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[GEX History] HTML gerado com sucesso: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
