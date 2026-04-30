#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Flow History — Histórico de Entradas e Saídas de OI por Strike
======================================================================
Monitora quais strikes tiveram mais movimentação de dealers ao longo do tempo.

Funcionalidades:
- Variação de OI (dOI) como métrica primária de entrada/saída
- Volume como métrica secundária
- Ranking de strikes por período filtrado
- Breakdown por vencimento
- Filtro interativo de datas em formato calendário

Uso:
  python3 bova11_flow_history.py
"""

import os
import re
import glob
import json
import sys
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

_BASEDIR      = os.path.dirname(os.path.abspath(__file__))
CSV_DIR       = os.path.join(_BASEDIR, '..', 'data')
OUTPUT_DIR    = os.path.join(_BASEDIR, '..', 'output')
HISTORY_FILE  = os.path.join(_BASEDIR, '..', 'history', 'bova11_flow_history.json')

# ═══════════════════════════════════════════════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_num(s):
    """Converte string BR (1.234,56 / 12,34k / 1,2M) → float."""
    s = str(s).strip().replace('%', '')
    if s in ('-', '', '0', 'None', '--'):
        return 0.0
    mult = 1
    if s.endswith('M'):
        s = s[:-1]; mult = 1_000_000
    elif s.endswith('k'):
        s = s[:-1]; mult = 1_000
    s = s.replace('.', '').replace(',', '.')
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def extract_date_from_filename(filename):
    """Extrai a tag de data do nome do arquivo."""
    # Padrão: venc_<label>_fechamento__<tag>_.csv
    m = re.search(r'fechamento__([a-zA-Z0-9]+)_\.csv$', filename)
    if m:
        return m.group(1)
    # Padrão: venc <label> fechamento (<tag>).csv
    m = re.search(r'fechamento \(([a-zA-Z0-9]+)\)\.csv$', filename)
    if m:
        return m.group(1)
    return None


def tag_to_label(tag):
    """Converte tag '13mar' para label '13/mar' (lowercase)."""
    m = re.match(r'(\d{1,2})([a-z]{3})$', tag.lower())
    if m:
        dia = m.group(1)
        mes = m.group(2).lower()
        return f"{int(dia)}/{mes}"
    return tag


def date_label_sort_key(label):
    """Ordena labels como 31/mar, 1/abr e 25posmar em ordem cronológica."""
    m = re.match(r'(\d{1,2})(?:/|pos)?([a-z]{3})$', str(label).lower())
    if m:
        meses = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
                 'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
        return (meses.get(m.group(2), 99), int(m.group(1)))
    return (99, 99)

def tag_sort_key(tag):
    """Ordena tags cronologicamente a partir do próprio texto da tag."""
    normalized = re.sub(r'(pos|pre)([a-z]{3})$', r'\2', str(tag).lower())
    m = re.match(r'(\d{1,2})([a-z]{3})$', normalized)
    if m:
        meses = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
                 'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
        return (meses.get(m.group(2), 99), int(m.group(1)), normalized)
    return (99, 99, str(tag).lower())

def is_primary_fechamento_file(filename):
    lower = filename.lower()
    return (
        lower.endswith('.csv')
        and 'fechamento' in lower
        and 'volume' not in lower
        and ' copy' not in lower
    )


def is_ticker_match(label, ticker):
    """Verifica se o ticker corresponde à expiração esperada."""
    if not label or not ticker: return True
    label_lower = label.lower()
    months = {'jan': 'A', 'fev': 'B', 'mar': 'C', 'abr': 'D', 'mai': 'E', 'jun': 'F',
              'jul': 'G', 'ago': 'H', 'set': 'I', 'out': 'J', 'nov': 'K', 'dez': 'L'}
    exp_month = None
    for m, letter in months.items():
        if m in label_lower:
            exp_month = letter; break
    if not exp_month or len(ticker) < 5: return True
    if ticker[4].upper() != exp_month: return False
    if 'mensal' in label_lower and re.search(r'W\d+$', ticker.upper()): return False
    return True


def parse_fech_csv(filepath):
    """Parse arquivo de fechamento (OI + Greeks)."""
    if not filepath or not os.path.exists(filepath):
        return []

    filename = os.path.basename(filepath)
    label = ""
    m = re.match(r'venc_(.+?)_fechamento__', filename)
    if m: label = m.group(1).replace('_', ' ')
    else:
        m = re.match(r'venc (.+?) fechamento', filename)
        if m: label = m.group(1)

    with open(filepath, 'r', encoding='latin-1') as f:
        lines = f.readlines()
    if not lines:
        return []

    header = lines[0].strip().replace('\r', '').split(';')
    ncols = len(header)
    results = []
    seen_strikes = set()

    for line in lines[1:]:
        line = line.strip().replace('\r', '')
        if not line:
            continue
        p = line.split(';')

        # Filtro de Ticker
        if len(p) > 0 and not is_ticker_match(label, p[0]):
            continue

        if ncols >= 20 and len(p) >= 23:
            strike = parse_num(p[11])
            if strike in seen_strikes: continue
            seen_strikes.add(strike)

            results.append({
                'strike':  strike,
                'c_oi': int(parse_num(p[2])),
                'p_oi': int(parse_num(p[20])),
            })
        elif len(p) >= 11:
            strike = parse_num(p[5])
            if strike in seen_strikes: continue
            seen_strikes.add(strike)

            results.append({
                'strike':  strike,
                'c_oi': int(parse_num(p[2])),
                'p_oi': int(parse_num(p[8])),
            })

    return results


def parse_vol_csv(filepath):
    """Parse arquivo de volume."""
    if not filepath or not os.path.exists(filepath):
        return {}

    with open(filepath, 'r', encoding='latin-1') as f:
        lines = f.readlines()

    if not lines:
        return {}

    header = lines[0].strip().replace('\r', '').split(';')
    strike_col = 4
    for i, h in enumerate(header):
        if 'strike' in h.lower():
            strike_col = i; break

    results = {}
    for line in lines[1:]:
        line = line.strip().replace('\r', '')
        if not line:
            continue
        p = line.split(';')
        if len(p) < 10:
            continue
        strike = parse_num(p[strike_col])
        results[strike] = {'cv': int(parse_num(p[1])), 'pv': int(parse_num(p[9]))}

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# DATE DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def discover_all_date_tags():
    """Retorna list de (tag, None) para todas as datas únicas, em ordem cronológica."""
    seen = set()

    for pattern in ["venc_*_fechamento__*_.csv", "venc * fechamento (*).csv"]:
        for fpath in glob.glob(os.path.join(CSV_DIR, pattern)):
            filename = os.path.basename(fpath)
            if not is_primary_fechamento_file(filename):
                continue
            tag = extract_date_from_filename(filename)
            if tag:
                seen.add(tag)

    return [(tag, None) for tag in sorted(seen, key=tag_sort_key)]


def discover_expirations_for_tag(tag_d, tag_d1):
    """Descobrir expirações para um par de datas."""
    exps = []

    # Padrão underscores
    for fpath in sorted(glob.glob(os.path.join(CSV_DIR, f"venc_*_fechamento__{tag_d}_.csv"))):
        fn = os.path.basename(fpath)
        m = re.match(r'venc_(.+?)_fechamento__' + re.escape(tag_d) + r'_\.csv$', fn)
        if not m: continue
        label = m.group(1).strip().replace('_', ' ')

        exps.append({
            "label": label,
            "fech_d": fn,
            "vol_d":   f"venc_{m.group(1)}_fechamento__{tag_d}_Volume_.csv",
            "fech_d1": f"venc_{m.group(1)}_fechamento__{tag_d1}_.csv" if tag_d1 else None,
            "vol_d1":  f"venc_{m.group(1)}_fechamento__{tag_d1}_Volume_.csv" if tag_d1 else None,
        })

    # Padrão espaços
    for fpath in sorted(glob.glob(os.path.join(CSV_DIR, f"venc * fechamento ({tag_d}).csv"))):
        fn = os.path.basename(fpath)
        m = re.match(r'venc (.+) fechamento \(' + re.escape(tag_d) + r'\)\.csv$', fn)
        if not m: continue
        label = m.group(1).strip()
        if any(e['label'] == label for e in exps): continue

        exps.append({
            "label": label,
            "fech_d": fn,
            "vol_d":   f"venc {label} fechamento ({tag_d} Volume).csv",
            "fech_d1": f"venc {label} fechamento ({tag_d1}).csv" if tag_d1 else None,
            "vol_d1":  f"venc {label} fechamento ({tag_d1} Volume).csv" if tag_d1 else None,
        })

    # Verificar existência dos opcionais
    for e in exps:
        for k in ('vol_d', 'fech_d1', 'vol_d1'):
            if e[k] and not os.path.exists(os.path.join(CSV_DIR, e[k])):
                e[k] = None

    return exps


# ═══════════════════════════════════════════════════════════════════════════════
# FLOW CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

def compute_strike_flows(exp_info, tag_d, tag_d1):
    """Computa OI flows (entradas/saídas) para um par de datas e uma expiração."""
    fech_d_path  = os.path.join(CSV_DIR, exp_info['fech_d'])
    fech_d1_path = os.path.join(CSV_DIR, exp_info['fech_d1']) if exp_info['fech_d1'] else None
    vol_d_path   = os.path.join(CSV_DIR, exp_info['vol_d'])   if exp_info['vol_d']   else None
    vol_d1_path  = os.path.join(CSV_DIR, exp_info['vol_d1'])  if exp_info['vol_d1']  else None

    rows_d  = parse_fech_csv(fech_d_path)
    rows_d1 = parse_fech_csv(fech_d1_path) if fech_d1_path else []
    vol_d   = parse_vol_csv(vol_d_path)   if vol_d_path   else {}
    vol_d1  = parse_vol_csv(vol_d1_path)  if vol_d1_path  else {}

    d1_map = {r['strike']: r for r in rows_d1}
    result = {}

    for r in rows_d:
        s = r['strike']
        r1 = d1_map.get(s)
        snap = (r1 is None)
        if snap:
            r1 = r  # Sem dado anterior, trata como snap (sem mudança)

        co1 = int(r1['c_oi'])
        co2 = int(r['c_oi'])
        po1 = int(r1['p_oi'])
        po2 = int(r['p_oi'])

        v_d  = vol_d.get(s,  {'cv': 0, 'pv': 0})
        v_d1 = vol_d1.get(s, {'cv': 0, 'pv': 0})

        result[str(int(s))] = {
            'co1': co1, 'co2': co2, 'dco': co2 - co1,
            'po1': po1, 'po2': po2, 'dpo': po2 - po1,
            'cv':  int(v_d.get('cv', 0)),
            'pv':  int(v_d.get('pv', 0)),
            'cv1': int(v_d1.get('cv', 0)),
            'pv1': int(v_d1.get('pv', 0)),
            'snap': snap,
        }

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def load_history():
    """Carrega histórico do arquivo JSON."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_history(history):
    """Salva histórico no arquivo JSON."""
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def update_history_for_date(history, date_label, expiry_label, strike_flows):
    """Upsert entry no histórico."""
    if date_label not in history:
        history[date_label] = {}
    history[date_label][expiry_label] = strike_flows


def prune_history(history, valid_date_labels):
    """Remove datas persistidas que não existem mais no conjunto válido atual."""
    valid = set(valid_date_labels)
    stale_keys = [key for key in history.keys() if key not in valid]
    for key in stale_keys:
        history.pop(key, None)
    return stale_keys


# ═══════════════════════════════════════════════════════════════════════════════
# HTML GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_html(history):
    """Gera HTML com todos os dados embarcados como JS."""

    # Preparar dados para JS
    all_dates = sorted(history.keys(), key=date_label_sort_key)
    all_expiries_set = set()
    for date_data in history.values():
        all_expiries_set.update(date_data.keys())

    # Ordenar vencimentos por data (day/month ascending)
    def parse_vencimento_for_sort(venc_str):
        # Extrai data (ex: "13 mar W2" -> (3, 13), "2 abr W1" -> (4, 2))
        m = re.match(r'(\d{1,2})\s+([a-z]{3})', venc_str.lower())
        if m:
            meses = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
                    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
            day = int(m.group(1))
            month = meses.get(m.group(2), 1)
            return (month, day)
        return (99, 99)

    all_expiries = sorted(all_expiries_set, key=parse_vencimento_for_sort)

    data_js = json.dumps(history, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOVA11 — Flow History</title>
    <style>
        :root {{
            --bg: #ffffff;
            --bg2: #f6f8fa;
            --bg3: #eaeef2;
            --text: #1f2328;
            --text2: #636c76;
            --border: #d0d7de;
            --red: #cf222e;
            --grn: #1a7f37;
            --blu: #0969da;
            --yel: #9a6700;
            --pur: #8250df;
        }}

        [data-theme="dark"] {{
            --bg: #0d1117;
            --bg2: #161b22;
            --bg3: #21262d;
            --text: #c9d1d9;
            --text2: #8b949e;
            --border: #30363d;
            --red: #f85149;
            --grn: #3fb950;
            --blu: #58a6ff;
            --yel: #d29922;
            --pur: #bc8cff;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 20px;
        }}

        .container {{
            max-width: 1600px;
            margin: 0 auto;
        }}

        h1 {{
            font-size: 28px;
            margin-bottom: 5px;
            color: var(--blu);
        }}

        .subtitle {{
            font-size: 13px;
            color: var(--text2);
            margin-bottom: 30px;
        }}

        .controls {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 20px;
            margin-bottom: 30px;
        }}

        .control-group {{
            margin-bottom: 25px;
        }}

        .control-group:last-child {{
            margin-bottom: 0;
        }}

        .control-label {{
            font-size: 13px;
            font-weight: 600;
            color: var(--text);
            margin-bottom: 12px;
            display: block;
        }}

        .pill-group {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}

        .pill {{
            padding: 6px 12px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: var(--bg);
            color: var(--text);
            cursor: pointer;
            font-size: 12px;
            transition: all 0.2s;
        }}

        .pill:hover {{
            border-color: var(--blu);
            background: var(--bg2);
        }}

        .pill.active {{
            background: var(--blu);
            color: var(--bg);
            border-color: var(--blu);
        }}

        .cal-month-title {{
            font-size: 13px;
            font-weight: 600;
            color: var(--text);
            margin: 15px 0 8px 0;
            text-transform: uppercase;
        }}

        .calendar {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 4px;
            margin-bottom: 15px;
            max-width: 300px;
        }}

        .cal-day {{
            aspect-ratio: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            border: 1px solid var(--border);
            border-radius: 3px;
            background: var(--bg);
            color: var(--text);
            cursor: pointer;
            font-size: 11px;
            font-weight: 500;
            transition: all 0.2s;
            min-height: 32px;
        }}

        .cal-day:hover {{
            border-color: var(--blu);
            background: var(--bg2);
        }}

        .cal-day.selected {{
            background: var(--blu);
            color: var(--bg);
            border-color: var(--blu);
        }}

        .cal-day.empty {{
            background: transparent;
            border: none;
            cursor: default;
        }}

        .cal-day.empty:hover {{
            background: transparent;
            border: none;
        }}

        .cal-buttons {{
            display: flex;
            gap: 8px;
            margin-top: 10px;
        }}

        .cal-btn {{
            padding: 6px 12px;
            border: 1px solid var(--border);
            border-radius: 4px;
            background: var(--bg);
            color: var(--text2);
            cursor: pointer;
            font-size: 11px;
            transition: all 0.2s;
        }}

        .cal-btn:hover {{
            border-color: var(--blu);
            color: var(--blu);
        }}

        .section {{
            margin-bottom: 40px;
        }}

        .section-title {{
            font-size: 16px;
            font-weight: 600;
            color: var(--text);
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border);
        }}

        .grid-2col {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }}

        .grid-1row-2col {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }}

        .table-wrapper {{
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 6px;
            overflow: hidden;
        }}

        .table-title {{
            font-size: 12px;
            font-weight: 600;
            color: var(--text);
            background: var(--bg2);
            padding: 10px 12px;
            border-bottom: 1px solid var(--border);
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }}

        th {{
            background: var(--bg2);
            border-bottom: 1px solid var(--border);
            padding: 8px 12px;
            text-align: left;
            font-weight: 600;
            color: var(--text2);
        }}

        td {{
            padding: 8px 12px;
            border-bottom: 1px solid var(--border);
        }}

        tr:last-child td {{
            border-bottom: none;
        }}

        .strike-cell {{
            font-weight: 600;
            color: var(--blu);
        }}

        .number-cell {{
            text-align: right;
            font-family: 'Monaco', 'Courier New', monospace;
        }}

        .entry {{
            color: var(--grn);
        }}

        .exit {{
            color: var(--red);
        }}

        .bar {{
            height: 10px;
            border-radius: 2px;
            display: inline-block;
        }}

        .bar-entry {{
            background: var(--grn);
        }}

        .bar-exit {{
            background: var(--red);
        }}

        .error {{
            background: var(--bg2);
            border: 1px solid var(--red);
            border-radius: 6px;
            padding: 15px;
            color: var(--red);
            margin: 20px 0;
        }}

        @media (max-width: 1200px) {{
            .grid-2col {{
                grid-template-columns: 1fr;
            }}
            .grid-1row-2col {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <button id="theme-toggle" onclick="toggleTheme()" title="Alternar tema" style="position:fixed;top:12px;right:16px;z-index:9999;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:1rem;line-height:1;transition:all 0.2s;">◐</button>
    <div class="container">
        <h1>📦 Flow History</h1>
        <div class="subtitle">Histórico de Entradas e Saídas de OI por Strike</div>

        <div class="controls">
            <div class="control-group">
                <label class="control-label">Vencimento</label>
                <div class="pill-group" id="expiry-pills"></div>
            </div>

            <div class="control-group">
                <label class="control-label">Datas</label>
                <div id="calendar-container"></div>
                <div class="cal-buttons">
                    <button class="cal-btn" onclick="selectAllDates()">Selecionar Todos</button>
                </div>
            </div>
        </div>

        <div class="section">
            <div class="section-title">Ranking de Entradas e Saídas (período selecionado)</div>
            <div id="cumulative-content"></div>
        </div>
    </div>

    <script>
        const DATA = {data_js};
        const ALL_DATES = {json.dumps(all_dates)};
        const ALL_EXPIRIES = {json.dumps(all_expiries)};

        let selectedDates = new Set(ALL_DATES);
        let selectedExpiries = new Set(ALL_EXPIRIES);

        // ─── INITIALIZATION ───

        function init() {{
            renderExpiryPills();
            renderCalendar();
            renderAll();
        }}

        function renderExpiryPills() {{
            const container = document.getElementById('expiry-pills');
            container.innerHTML = '';

            const allPill = document.createElement('div');
            allPill.className = 'pill active';
            allPill.textContent = 'Todas';
            allPill.onclick = () => {{
                if (selectedExpiries.size === ALL_EXPIRIES.length) {{
                    selectedExpiries = new Set();
                }} else {{
                    selectedExpiries = new Set(ALL_EXPIRIES);
                }}
                renderExpiryPills();
                renderAll();
            }};
            container.appendChild(allPill);

            for (const exp of ALL_EXPIRIES) {{
                const p = document.createElement('div');
                p.className = 'pill' + (selectedExpiries.has(exp) ? ' active' : '');
                p.textContent = exp;
                p.onclick = () => {{
                    if (selectedExpiries.has(exp)) {{
                        selectedExpiries.delete(exp);
                    }} else {{
                        selectedExpiries.add(exp);
                    }}
                    renderExpiryPills();
                    renderAll();
                }};
                container.appendChild(p);
            }}
        }}

        function renderCalendar() {{
            const container = document.getElementById('calendar-container');
            container.innerHTML = '';

            // Group dates by month
            const datesByMonth = {{}};
            for (const date of ALL_DATES) {{
                const [day, month] = date.split('/');
                if (!datesByMonth[month]) datesByMonth[month] = [];
                datesByMonth[month].push({{day: parseInt(day), full: date}});
            }}

            // Order months
            const months_order = ['fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez', 'jan'];
            const sortedMonths = Object.keys(datesByMonth).sort((a, b) => {{
                return months_order.indexOf(a) - months_order.indexOf(b);
            }});

            for (const month of sortedMonths) {{
                const monthDates = datesByMonth[month].sort((a, b) => a.day - b.day);

                const monthTitle = document.createElement('div');
                monthTitle.className = 'cal-month-title';
                monthTitle.textContent = month.toUpperCase() + ' 2025';
                container.appendChild(monthTitle);

                const cal = document.createElement('div');
                cal.className = 'calendar';

                // Add empty spaces for days before first date
                const minDay = Math.min(...monthDates.map(d => d.day));

                // Add days
                for (const dateObj of monthDates) {{
                    const day = document.createElement('div');
                    day.className = 'cal-day' + (selectedDates.has(dateObj.full) ? ' selected' : '');
                    day.textContent = dateObj.day;
                    day.onclick = () => {{
                        if (selectedDates.has(dateObj.full)) {{
                            selectedDates.delete(dateObj.full);
                        }} else {{
                            selectedDates.add(dateObj.full);
                        }}
                        renderCalendar();
                        renderAll();
                    }};
                    cal.appendChild(day);
                }}

                container.appendChild(cal);
            }}
        }}

        function selectAllDates() {{
            if (selectedDates.size === ALL_DATES.length) {{
                selectedDates = new Set();
            }} else {{
                selectedDates = new Set(ALL_DATES);
            }}
            renderCalendar();
            renderAll();
        }}

        // ─── DATA FILTERING ───

        function getFilteredData() {{
            const result = {{}};

            for (const date of selectedDates) {{
                if (!DATA[date]) continue;
                for (const exp of selectedExpiries) {{
                    if (!DATA[date][exp]) continue;

                    for (const strike_str in DATA[date][exp]) {{
                        const entry = DATA[date][exp][strike_str];
                        if (entry.snap) continue;

                        if (!result[strike_str]) {{
                            result[strike_str] = {{dco_sum: 0, dpo_sum: 0, cv_sum: 0, pv_sum: 0}};
                        }}
                        result[strike_str].dco_sum += entry.dco || 0;
                        result[strike_str].dpo_sum += entry.dpo || 0;
                        result[strike_str].cv_sum += entry.cv || 0;
                        result[strike_str].pv_sum += entry.pv || 0;
                    }}
                }}
            }}

            return result;
        }}

        function getTopN(strikeMap, field, direction) {{
            const entries = Object.entries(strikeMap)
                .map(([strike, data]) => ({{strike: parseInt(strike), value: data[field]}}))
                .filter(e => direction === 'entry' ? e.value > 0 : e.value < 0);

            if (direction === 'entry') {{
                entries.sort((a, b) => b.value - a.value);
            }} else {{
                entries.sort((a, b) => a.value - b.value);
            }}

            return entries;
        }}

        // ─── RENDERING ───

        function renderCumulativeRanking() {{
            const filtered = getFilteredData();
            if (Object.keys(filtered).length === 0) {{
                document.getElementById('cumulative-content').innerHTML = '<div class="error">Nenhum dado para o período selecionado</div>';
                return;
            }}

            const callEntries = getTopN(filtered, 'dco_sum', 'entry');
            const callExits = getTopN(filtered, 'dco_sum', 'exit');
            const putEntries = getTopN(filtered, 'dpo_sum', 'entry');
            const putExits = getTopN(filtered, 'dpo_sum', 'exit');

            const callMaxEntry = Math.max(...callEntries.map(e => e.value), 1);
            const callMaxExit = Math.max(...callExits.map(e => Math.abs(e.value)), 1);
            const putMaxEntry = Math.max(...putEntries.map(e => e.value), 1);
            const putMaxExit = Math.max(...putExits.map(e => Math.abs(e.value)), 1);

            let html = '<div class="grid-1row-2col">';

            html += '<div>';
            html += renderTable('Calls — Entradas Acumuladas', callEntries, 'entry', callMaxEntry);
            html += '</div>';

            html += '<div>';
            html += renderTable('Puts — Entradas Acumuladas', putEntries, 'entry', putMaxEntry);
            html += '</div>';

            html += '</div>';

            html += '<div class="grid-1row-2col" style="margin-top: 20px;">';

            html += '<div>';
            html += renderTable('Calls — Saídas Acumuladas', callExits, 'exit', callMaxExit);
            html += '</div>';

            html += '<div>';
            html += renderTable('Puts — Saídas Acumuladas', putExits, 'exit', putMaxExit);
            html += '</div>';

            html += '</div>';

            document.getElementById('cumulative-content').innerHTML = html;
        }}

        function renderTable(title, entries, type, maxValue) {{
            if (entries.length === 0) {{
                return '<div class="table-wrapper"><div class="table-title">' + title + ' (vazio)</div></div>';
            }}

            let html = '<div class="table-wrapper">';
            html += '<div class="table-title">' + title + ' — ' + entries.length + ' strikes</div>';
            html += '<table>';
            html += '<tr><th>Strike</th><th>OI Δ</th><th>Vol Call</th><th>Vol Put</th><th>Gráfico</th></tr>';

            for (const entry of entries) {{
                const strike = entry.strike;
                const value = entry.value;
                const barWidth = Math.min(200, Math.abs(value) / maxValue * 200);
                const color = type === 'entry' ? '#3fb950' : '#f85149';
                const formattedValue = value > 0 ? '+' + value.toLocaleString('pt-BR') : value.toLocaleString('pt-BR');

                // Get volume data for this strike from filtered data
                const filtered = getFilteredData();
                const volData = filtered[strike] || {{cv_sum: 0, pv_sum: 0}};
                const volCall = Math.round(volData.cv_sum || 0);
                const volPut = Math.round(volData.pv_sum || 0);
                const volCallFormatted = volCall > 0 ? (volCall / 1000000).toFixed(2) + 'M' : (volCall / 1000).toFixed(1) + 'k';
                const volPutFormatted = volPut > 0 ? (volPut / 1000000).toFixed(2) + 'M' : (volPut / 1000).toFixed(1) + 'k';

                html += '<tr>';
                html += '<td class="strike-cell">' + strike + '</td>';
                html += '<td class="number-cell ' + (type === 'entry' ? 'entry' : 'exit') + '">' + formattedValue + '</td>';
                html += '<td class="number-cell" style="color: #58a6ff;">' + volCallFormatted + '</td>';
                html += '<td class="number-cell" style="color: #bc8cff;">' + volPutFormatted + '</td>';
                html += '<td><div class="bar bar-' + type + '" style="width:' + barWidth + 'px;"></div></td>';
                html += '</tr>';
            }}

            html += '</table>';
            html += '</div>';
            return html;
        }}

        function renderAll() {{
            renderCumulativeRanking();
        }}

        // Initialize on load
        init();
    </script>
    <script>
    (function(){{
      const saved = localStorage.getItem('bova11-theme');
      const btn = document.getElementById('theme-toggle');
      if(saved === 'dark'){{
        document.documentElement.setAttribute('data-theme','dark');
        if(btn) btn.textContent = '◐';
      }}
    }})();
    function toggleTheme(){{
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
      const btn = document.getElementById('theme-toggle');
      if(isDark){{
        document.documentElement.removeAttribute('data-theme');
        localStorage.setItem('bova11-theme','light');
        btn.textContent = '◐';
      }} else {{
        document.documentElement.setAttribute('data-theme','dark');
        localStorage.setItem('bova11-theme','dark');
        btn.textContent = '◐';
      }}
    }}
    </script>
</body>
</html>
"""
    return html


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "="*70)
    print("  BOVA11 Flow History — Histórico de Entradas e Saídas de OI")
    print("="*70 + "\n")

    print("🔍 Discovering dates...")
    all_tags = discover_all_date_tags()

    if not all_tags:
        print("❌ Nenhuma data encontrada no diretório data/")
        return

    print(f"✓ {len(all_tags)} datas encontradas")

    history = load_history()
    print(f"✓ Histórico carregado ({len(history)} datas no arquivo)")

    valid_date_labels = [tag_to_label(tag) for tag, _ in all_tags]
    stale_dates = prune_history(history, valid_date_labels)
    if stale_dates:
        print(f"🧹 Removendo datas inválidas do histórico: {', '.join(sorted(stale_dates, key=date_label_sort_key))}")

    for i, (tag_d, mtime_d) in enumerate(all_tags):
        tag_d1 = all_tags[i-1][0] if i > 0 else None
        date_label = tag_to_label(tag_d)

        print(f"\n  Processando {date_label}...")

        exps = discover_expirations_for_tag(tag_d, tag_d1)

        for exp in exps:
            flows = compute_strike_flows(exp, tag_d, tag_d1)
            update_history_for_date(history, date_label, exp['label'], flows)
            print(f"    ✓ {exp['label']} ({len(flows)} strikes)")

    print("\n💾 Salvando histórico...")
    save_history(history)

    print("\n📄 Gerando HTML...")
    html = generate_html(history)
    output_path = os.path.join(OUTPUT_DIR, 'bova11_flow_history.html')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"✓ Arquivo gerado: {output_path}")
    print("\n✅ Flow History concluído com sucesso!")


if __name__ == "__main__":
    main()
