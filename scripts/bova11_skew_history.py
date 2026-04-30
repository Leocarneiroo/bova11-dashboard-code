#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Skew History — Evolução da Volatilidade Implícita
=========================================================
Análise histórica do skew de volatilidade ao longo do tempo.

Funcionalidades:
- Calcula média de IV para Calls e Puts por dia
- Filtra por tipo de vencimento (semanal/mensal/todos)
- Geração completa de todo o histórico
- Filtro interativo de datas no HTML
- Gráficos separados para CALL e PUT

Uso:
  python3 bova11_skew_history.py
"""

import os
import re
import glob
import json
import math
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

_BASEDIR     = os.path.dirname(os.path.abspath(__file__))
CSV_DIR      = os.path.join(_BASEDIR, '..', 'data')
OUTPUT_DIR   = os.path.join(_BASEDIR, '..', 'output')
HISTORY_FILE = os.path.join(_BASEDIR, '..', 'history', 'bova11_skew_history.json')

# Filtros de strike
STRIKE_MIN_DEFAULT = 150
STRIKE_MAX_DEFAULT = 200

# ═══════════════════════════════════════════════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_num(s):
    """Converte string numérica brasileira para float."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace('\r', '').replace('%', '')
    if s in ('-', '', '--', 'nan', 'NA'):
        return None
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        parts = s.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None


def extract_date_from_filename(filename):
    """Extrai a data do nome do arquivo."""
    pattern1 = r'\((\d{1,2})([a-z]{3})\)'
    match = re.search(pattern1, filename, re.IGNORECASE)
    if match:
        dia = match.group(1).zfill(2)
        mes_str = match.group(2).lower()
        return dia, mes_str, f"{dia}/{mes_str}"
    
    pattern2 = r'__(\d{1,2})([a-z]{3})_'
    match = re.search(pattern2, filename, re.IGNORECASE)
    if match:
        dia = match.group(1).zfill(2)
        mes_str = match.group(2).lower()
        return dia, mes_str, f"{dia}/{mes_str}"
    
    return None, None, None


def parse_date_to_datetime(dia, mes_str, ano="2025"):
    """Converte dia/mês para objeto datetime."""
    meses = {
        'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
        'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
    }
    mes = meses.get(mes_str, 1)
    return datetime(int(ano), mes, int(dia))


def format_date_label(dia, mes_str):
    """Formata para exibição amigável."""
    meses = {
        'jan': 'Jan', 'fev': 'Fev', 'mar': 'Mar', 'abr': 'Abr',
        'mai': 'Mai', 'jun': 'Jun', 'jul': 'Jul', 'ago': 'Ago',
        'set': 'Set', 'out': 'Out', 'nov': 'Nov', 'dez': 'Dez'
    }
    mes = meses.get(mes_str, mes_str.capitalize())
    return f"{int(dia)}/{mes}"

def is_primary_fechamento_file(filename):
    lower = filename.lower()
    return (
        lower.endswith('.csv')
        and 'fechamento' in lower
        and 'volume' not in lower
        and ' copy' not in lower
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LEITURA DE CSV
# ═══════════════════════════════════════════════════════════════════════════════

def read_fechamento_iv(filepath):
    """Lê arquivo CSV e extrai IV para Calls e Puts."""
    records = []
    
    try:
        with open(filepath, 'r', encoding='latin-1') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"  ⚠️ Erro ao ler {filepath}: {e}")
        return records
    
    if not lines:
        return records
    
    header = lines[0].strip().replace('\r', '').split(';')
    header = [h.strip().upper() for h in header]
    
    strike_idx = None
    call_iv_idx = None
    put_iv_idx = None
    
    for i, h in enumerate(header):
        if 'STRIKE' in h or 'EXER' in h:
            strike_idx = i
        elif 'VOL' in h and 'IMPL' in h:
            if call_iv_idx is None:
                call_iv_idx = i
            else:
                put_iv_idx = i
    
    if strike_idx is None:
        return records
    
    for line in lines[1:]:
        line = line.strip().replace('\r', '')
        if not line:
            continue
        
        parts = line.split(';')
        if len(parts) <= strike_idx:
            continue
        
        strike = parse_num(parts[strike_idx])
        if strike is None or strike == 0:
            continue
        
        call_iv = parse_num(parts[call_iv_idx]) if call_iv_idx and call_iv_idx < len(parts) else None
        put_iv = parse_num(parts[put_iv_idx]) if put_iv_idx and put_iv_idx < len(parts) else None
        
        records.append({
            'strike': strike,
            'call_iv': call_iv,
            'put_iv': put_iv
        })
    
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# CÁLCULO DE IV MÉDIA
# ═══════════════════════════════════════════════════════════════════════════════

def calc_iv_stats(records, strike_min, strike_max):
    """Calcula estatísticas de IV filtrando por range de strike."""
    filtered = [r for r in records if strike_min <= r['strike'] <= strike_max]
    
    if not filtered:
        return None
    
    call_ivs = [r['call_iv'] for r in filtered if r['call_iv'] is not None]
    put_ivs = [r['put_iv'] for r in filtered if r['put_iv'] is not None]
    
    stats = {
        'count': len(filtered),
        'call_iv_mean': round(sum(call_ivs) / len(call_ivs), 2) if call_ivs else None,
        'put_iv_mean': round(sum(put_ivs) / len(put_ivs), 2) if put_ivs else None,
        'call_iv_min': round(min(call_ivs), 2) if call_ivs else None,
        'call_iv_max': round(max(call_ivs), 2) if call_ivs else None,
        'put_iv_min': round(min(put_ivs), 2) if put_ivs else None,
        'put_iv_max': round(max(put_ivs), 2) if put_ivs else None,
    }
    
    if stats['put_iv_mean'] and stats['call_iv_mean']:
        stats['skew'] = round(stats['put_iv_mean'] - stats['call_iv_mean'], 2)
    else:
        stats['skew'] = None
    
    return stats


# ═══════════════════════════════════════════════════════════════════════════════
# HISTÓRICO (JSON)
# ═══════════════════════════════════════════════════════════════════════════════

def load_history():
    """Carrega dados históricos do arquivo JSON."""
    filepath = os.path.join(OUTPUT_DIR, HISTORY_FILE)
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_history(history):
    """Salva dados históricos no arquivo JSON."""
    filepath = os.path.join(OUTPUT_DIR, HISTORY_FILE)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def update_history(history, date_str, vencimento, stats):
    """Atualiza o histórico com novos dados."""
    if date_str not in history:
        history[date_str] = {}
    
    history[date_str][vencimento] = {
        'call_iv_mean': stats['call_iv_mean'],
        'put_iv_mean': stats['put_iv_mean'],
        'skew': stats['skew'],
        'count': stats['count'],
        'timestamp': datetime.now().isoformat()
    }


def prune_history(history, valid_date_keys):
    """Remove datas persistidas que não correspondem às datas snapshot atuais."""
    valid = set(valid_date_keys)
    stale_keys = [key for key in history.keys() if key not in valid]
    for key in stale_keys:
        history.pop(key, None)
    return stale_keys


# ═══════════════════════════════════════════════════════════════════════════════
# INTERFACE DO USUÁRIO
# ═══════════════════════════════════════════════════════════════════════════════

def ask_filter_type():
    """Retorna sempre 'all' para processar todos os vencimentos."""
    return 'all', None


def ask_strike_range():
    """Pergunta o range de strikes para filtrar."""
    print("\n" + "="*65)
    print("  RANGE DE STRIKES")
    print("="*65)
    print(f"  Padrão: {STRIKE_MIN_DEFAULT} a {STRIKE_MAX_DEFAULT}")
    
    try:
        min_str = input(f"  Strike mínimo [{STRIKE_MIN_DEFAULT}]: ").strip()
        max_str = input(f"  Strike máximo [{STRIKE_MAX_DEFAULT}]: ").strip()
        
        min_val = int(min_str) if min_str else STRIKE_MIN_DEFAULT
        max_val = int(max_str) if max_str else STRIKE_MAX_DEFAULT
        
        return min_val, max_val
    except:
        print("  ⚠️ Entrada inválida, usando padrão")
        return STRIKE_MIN_DEFAULT, STRIKE_MAX_DEFAULT


# ═══════════════════════════════════════════════════════════════════════════════
# GERAÇÃO DE HTML COM FILTRO INTERATIVO
# ═══════════════════════════════════════════════════════════════════════════════

def generate_html(history_data, all_dates, filter_type, filter_value, strike_range):
    """Gera HTML com gráficos separados para CALL e PUT e filtro de datas."""
    
    # Preparar dados completos para JavaScript
    dates_data = []
    
    for dia, mes, label in all_dates:
        date_key = f"{dia}/{mes}"
        
        # Agregar dados de todos os vencimentos para esta data
        day_call_ivs = []
        day_put_ivs = []
        day_skews = []
        
        if date_key in history_data:
            for venc, stats in history_data[date_key].items():
                # Aplicar filtro
                if filter_type == 'weekly' and 'W' not in venc:
                    continue
                elif filter_type == 'monthly' and 'Mensal' not in venc:
                    continue
                elif filter_type == 'specific' and venc != filter_value:
                    continue
                
                if stats['call_iv_mean']:
                    day_call_ivs.append(stats['call_iv_mean'])
                if stats['put_iv_mean']:
                    day_put_ivs.append(stats['put_iv_mean'])
                if stats['skew']:
                    day_skews.append(stats['skew'])
        
        call_mean = round(sum(day_call_ivs)/len(day_call_ivs), 2) if day_call_ivs else None
        put_mean = round(sum(day_put_ivs)/len(day_put_ivs), 2) if day_put_ivs else None
        skew_mean = round(sum(day_skews)/len(day_skews), 2) if day_skews else None
        
        dates_data.append({
            'label': label,
            'date_key': date_key,
            'call_iv': call_mean,
            'put_iv': put_mean,
            'skew': skew_mean
        })
    
    # Ordenar por data
    dates_data.sort(key=lambda x: parse_date_to_datetime(*x['date_key'].split('/')))
    
    # Título do filtro
    if filter_type == 'all':
        filter_title = "Todos os Vencimentos"
    elif filter_type == 'weekly':
        filter_title = "Vencimentos Semanais"
    elif filter_type == 'monthly':
        filter_title = "Vencimentos Mensais"
    else:
        filter_title = f"Vencimento: {filter_value}"
    
    html = f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOVA11 Skew History — {filter_title}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 20px;
        }}
        .header {{
            background: linear-gradient(135deg, var(--bg2), var(--bg3));
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
        }}
        .header h1 {{ font-size: 1.8em; margin-bottom: 8px; }}
        .header .meta {{ color: var(--text2); font-size: 0.95em; }}
        .header .filter {{ color: var(--blu); font-weight: bold; }}
        .header .range {{ color: var(--grn); }}
        
        .controls {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
        }}
        .controls-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .controls-header h3 {{ color: var(--text); font-size: 1em; margin: 0; }}
        .controls-info {{ color: var(--text2); font-size: 0.85em; }}

        /* ── Calendar ── */
        .cal-wrap {{
            display: flex;
            gap: 16px;
            overflow-x: auto;
            padding-bottom: 8px;
            scrollbar-width: thin;
            scrollbar-color: var(--border) transparent;
        }}
        .cal-month {{
            flex: 0 0 auto;
            min-width: 200px;
        }}
        .cal-month-title {{
            text-align: center;
            font-weight: 700;
            font-size: 0.9em;
            color: var(--blu);
            letter-spacing: 0.05em;
            text-transform: uppercase;
            margin-bottom: 8px;
            padding: 6px;
            background: rgba(88,166,255,0.08);
            border-radius: 6px;
        }}
        .cal-grid {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 3px;
        }}
        .cal-dow {{
            text-align: center;
            font-size: 0.7em;
            color: var(--text2);
            padding: 4px 2px;
            font-weight: 600;
        }}
        .cal-day {{
            aspect-ratio: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.82em;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.15s;
            user-select: none;
            position: relative;
        }}
        .cal-day.has-data {{
            background: var(--bg3);
            border: 1px solid var(--border);
            color: var(--text);
        }}
        .cal-day.has-data:hover {{ background: rgba(88,166,255,0.15); border-color: var(--blu); }}
        .cal-day.selected {{
            background: var(--blu) !important;
            color: #000 !important;
            border-color: var(--blu) !important;
            font-weight: 700;
        }}
        .cal-day.empty {{ cursor: default; }}
        .cal-day.in-range {{
            background: rgba(88,166,255,0.18);
            border-radius: 2px;
        }}
        .cal-day.range-start {{ border-radius: 6px 2px 2px 6px; }}
        .cal-day.range-end   {{ border-radius: 2px 6px 6px 2px; }}

        .cal-actions {{
            display: flex;
            gap: 8px;
            margin-top: 14px;
            flex-wrap: wrap;
        }}
        .btn {{
            background: var(--blu);
            color: #000;
            border: none;
            border-radius: 6px;
            padding: 8px 16px;
            font-weight: 600;
            cursor: pointer;
            font-size: 0.85em;
            transition: opacity 0.15s;
        }}
        .btn:hover {{ opacity: 0.85; }}
        .btn.secondary {{ background: var(--bg3); color: var(--text); border: 1px solid var(--border); }}
        .btn.danger    {{ background: rgba(248,81,73,0.15); color: var(--red); border: 1px solid rgba(248,81,73,0.3); }}
        .range-hint {{ font-size: 0.78em; color: var(--text2); margin-top: 8px; }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
            text-align: center;
        }}
        .stat-card .label {{ color: var(--text2); font-size: 0.85em; margin-bottom: 8px; }}
        .stat-card .value {{ font-size: 1.6em; font-weight: bold; }}
        .stat-card .sub {{ font-size: 0.8em; color: var(--text2); margin-top: 4px; }}
        
        .chart-container {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
            height: 380px;
        }}
        .chart-title {{
            font-size: 1.1em;
            margin-bottom: 16px;
            color: var(--text2);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .chart-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
            gap: 24px;
        }}
        
        .data-table {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9em;
        }}
        th, td {{
            padding: 10px;
            text-align: center;
            border-bottom: 1px solid var(--border);
        }}
        th {{ color: var(--text2); font-weight: 600; }}
        tr:hover {{ background: var(--bg3); }}
        .positive {{ color: var(--grn); }}
        .negative {{ color: var(--red); }}
        
        @media (max-width: 768px) {{
            .chart-grid {{ grid-template-columns: 1fr; }}
            .date-filters {{ max-height: 200px; overflow-y: auto; }}
        }}
    </style>
</head>
<body>
    <button id="theme-toggle" onclick="toggleTheme()" title="Alternar tema" style="position:fixed;top:12px;right:16px;z-index:9999;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:1rem;line-height:1;transition:all 0.2s;">◐</button>
    <div class="header">
        <h1>📊 BOVA11 Skew History</h1>
        <div class="meta">
            Filtro: <span class="filter">{filter_title}</span> | 
            Strikes: <span class="range">{strike_range[0]} - {strike_range[1]}</span> | 
            Total de datas: <span id="total-dates">{len(dates_data)}</span> | 
            Selecionadas: <span id="selected-count">{len(dates_data)}</span>
        </div>
    </div>
    
    <div class="controls">
        <div class="controls-header">
            <h3>📅 Filtrar por Data</h3>
            <span class="controls-info"><span id="selected-count">0</span> de <span id="total-dates">{len(dates_data)}</span> datas selecionadas</span>
        </div>
        <div class="cal-wrap" id="calWrap"></div>
        <div class="cal-actions">
            <button class="btn" onclick="selectAll()">✓ Selecionar Todas</button>
            <button class="btn secondary" onclick="selectLast30()">Últimos 30 dias</button>
            <button class="btn secondary" onclick="selectLast7()">Últimos 7 dias</button>
            <button class="btn danger" onclick="deselectAll()">✕ Limpar</button>
            <button class="btn" onclick="applyFilter()">▶ Aplicar</button>
        </div>
        <div class="range-hint">Dica: clique em uma data para selecioná-la · arraste ou clique + Shift para selecionar um intervalo</div>
    </div>
    
    <div class="stats-grid" id="statsGrid">
        <div class="stat-card">
            <div class="label">IV Média Calls (último)</div>
            <div class="value" id="statCallIv" style="color: var(--grn)">-</div>
            <div class="sub" id="statCallChange"></div>
        </div>
        <div class="stat-card">
            <div class="label">IV Média Puts (último)</div>
            <div class="value" id="statPutIv" style="color: var(--red)">-</div>
            <div class="sub" id="statPutChange"></div>
        </div>
        <div class="stat-card">
            <div class="label">Skew (Put - Call)</div>
            <div class="value" id="statSkew" style="color: var(--yel)">-</div>
            <div class="sub" id="statSkewChange"></div>
        </div>
        <div class="stat-card">
            <div class="label">Variação Skew (1d)</div>
            <div class="value" id="statSkewDelta">-</div>
        </div>
    </div>
    
    <div class="chart-grid">
        <div class="chart-container">
            <div class="chart-title">
                <span>📈 CALL — Evolução da Volatilidade Implícita</span>
                <span style="font-size: 0.8em; color: var(--grn)">▼ Média filtrada</span>
            </div>
            <canvas id="callChart"></canvas>
        </div>
        <div class="chart-container">
            <div class="chart-title">
                <span>📉 PUT — Evolução da Volatilidade Implícita</span>
                <span style="font-size: 0.8em; color: var(--red)">▼ Média filtrada</span>
            </div>
            <canvas id="putChart"></canvas>
        </div>
    </div>
    
    <div class="chart-container" style="height: 320px;">
        <div class="chart-title">📊 SKEW — Diferença Put vs Call</div>
        <canvas id="skewChart"></canvas>
    </div>
    
    <div class="data-table">
        <h3 style="margin-bottom: 16px; color: var(--text2)">📋 Dados Detalhados</h3>
        <table id="dataTable">
            <thead>
                <tr>
                    <th>Data</th>
                    <th>IV Call (%)</th>
                    <th>IV Put (%)</th>
                    <th>Skew (%)</th>
                    <th>Δ Call</th>
                    <th>Δ Put</th>
                    <th>Δ Skew</th>
                </tr>
            </thead>
            <tbody id="tableBody"></tbody>
        </table>
        <div style="text-align:center; margin-top:16px;">
            <button id="btnExpandTable" onclick="toggleTableExpand()"
                style="background:var(--bg3); border:1px solid var(--border); color:var(--text2);
                       padding:8px 20px; border-radius:8px; cursor:pointer; font-size:0.85rem;
                       transition: all 0.2s; display:none;">
            </button>
        </div>
    </div>
    
    <script>
        // Dados completos do servidor
        const allData = {json.dumps(dates_data)};
        let selectedDates = allData.map(d => d.label);
        let charts = {{}};
        
        // ══════════════════════════════════════════
        // CALENDAR ENGINE
        // ══════════════════════════════════════════

        // Map label ("2/Fev") → {{year, month (0-idx), day}}
        const MONTHS_PT = {{'Jan':0,'Fev':1,'Mar':2,'Abr':3,'Mai':4,'Jun':5,'Jul':6,'Ago':7,'Set':8,'Out':9,'Nov':10,'Dez':11}};
        const MONTHS_NAME = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];

        function labelToDate(label) {{
            const [d, m] = label.split('/');
            // pick year: if month is before current month in a year-wrap context use next year cautiously
            const mo = MONTHS_PT[m];
            return new Date(2025, mo, parseInt(d));  // year fixed to data year
        }}

        function dateToKey(dt) {{
            return dt.getFullYear() + '-' + dt.getMonth() + '-' + dt.getDate();
        }}

        // Build lookup: dateKey → label
        const keyToLabel = {{}};
        const labelToKey = {{}};
        allData.forEach(d => {{
            const dt = labelToDate(d.label);
            const k  = dateToKey(dt);
            keyToLabel[k] = d.label;
            labelToKey[d.label] = k;
        }});

        // ── State ──
        let isDragging   = false;
        let dragStart    = null;  // dateKey
        let lastAnchor   = null;  // for shift-click

        function renderCalendar() {{
            const wrap = document.getElementById('calWrap');
            wrap.innerHTML = '';

            // Group available dates by year+month
            const months = {{}};
            allData.forEach(d => {{
                const dt  = labelToDate(d.label);
                const key = dt.getFullYear() + '-' + dt.getMonth();
                if (!months[key]) months[key] = {{ year: dt.getFullYear(), month: dt.getMonth(), days: [] }};
                months[key].days.push({{ day: dt.getDate(), label: d.label, k: dateToKey(dt) }});
            }});

            const DOW = ['D','S','T','Q','Q','S','S'];

            Object.values(months).sort((a,b) => a.year - b.year || a.month - b.month).forEach(m => {{
                const col = document.createElement('div');
                col.className = 'cal-month';

                const title = document.createElement('div');
                title.className = 'cal-month-title';
                title.textContent = MONTHS_NAME[m.month] + ' ' + m.year;
                col.appendChild(title);

                const grid = document.createElement('div');
                grid.className = 'cal-grid';

                // Day-of-week headers
                DOW.forEach(h => {{
                    const dh = document.createElement('div');
                    dh.className = 'cal-dow';
                    dh.textContent = h;
                    grid.appendChild(dh);
                }});

                // Offset: first day of month
                const firstDow = new Date(m.year, m.month, 1).getDay();
                for (let i = 0; i < firstDow; i++) {{
                    const empty = document.createElement('div');
                    empty.className = 'cal-day empty';
                    grid.appendChild(empty);
                }}

                // Days of month
                const daysInMonth = new Date(m.year, m.month + 1, 0).getDate();
                const dayMap = {{}};
                m.days.forEach(d => {{ dayMap[d.day] = d; }});

                for (let d = 1; d <= daysInMonth; d++) {{
                    const cell = document.createElement('div');
                    const info = dayMap[d];
                    if (info) {{
                        cell.className = 'cal-day has-data' + (selectedDates.includes(info.label) ? ' selected' : '');
                        cell.textContent = d;
                        cell.dataset.k = info.k;
                        cell.dataset.label = info.label;

                        // Mouse events for drag-select
                        cell.addEventListener('mousedown', e => {{
                            if (e.shiftKey && lastAnchor) {{
                                rangeSelect(lastAnchor, info.k);
                            }} else {{
                                isDragging = true;
                                dragStart  = info.k;
                                lastAnchor = info.k;
                                toggleLabel(info.label);
                                cell.classList.toggle('selected', selectedDates.includes(info.label));
                            }}
                            updateCount();
                            e.preventDefault();
                        }});
                        cell.addEventListener('mouseenter', e => {{
                            if (isDragging) {{
                                const sel = !selectedDates.includes(dragStart ? keyToLabel[dragStart] : info.label);
                                if (sel && !selectedDates.includes(info.label)) {{
                                    selectedDates.push(info.label);
                                }} else if (!sel && selectedDates.includes(info.label)) {{
                                    selectedDates = selectedDates.filter(l => l !== info.label);
                                }}
                                cell.classList.toggle('selected', selectedDates.includes(info.label));
                                updateCount();
                            }}
                        }});
                    }} else {{
                        cell.className = 'cal-day empty';
                        cell.textContent = d;
                        cell.style.color = 'var(--text2)';
                        cell.style.opacity = '0.25';
                    }}
                    grid.appendChild(cell);
                }}
                col.appendChild(grid);
                wrap.appendChild(col);
            }});

            document.addEventListener('mouseup', () => {{ isDragging = false; }}, {{ once: false }});
        }}

        function toggleLabel(label) {{
            if (selectedDates.includes(label))
                selectedDates = selectedDates.filter(l => l !== label);
            else
                selectedDates.push(label);
        }}

        function rangeSelect(fromKey, toKey) {{
            // Collect all available sorted dates in range
            const sorted = allData.map(d => ({{ label: d.label, dt: labelToDate(d.label) }})).sort((a,b) => a.dt - b.dt);
            const fromDt = new Date(...fromKey.split('-').map((v,i)=>i===1?+v:+v));
            const toDt   = new Date(...toKey.split('-').map((v,i)=>i===1?+v:+v));
            const [lo, hi] = fromDt <= toDt ? [fromDt, toDt] : [toDt, fromDt];
            sorted.forEach(item => {{
                if (item.dt >= lo && item.dt <= hi && !selectedDates.includes(item.label))
                    selectedDates.push(item.label);
            }});
            renderCalendar();
        }}

        function updateCount() {{
            document.getElementById('selected-count').textContent = selectedDates.length;
        }}

        function selectAll() {{
            selectedDates = allData.map(d => d.label);
            renderCalendar();
            updateCount();
        }}

        function deselectAll() {{
            selectedDates = [];
            renderCalendar();
            updateCount();
        }}

        function selectLast(n) {{
            const sorted = allData.map(d => ({{ label: d.label, dt: labelToDate(d.label) }})).sort((a,b) => b.dt - a.dt);
            selectedDates = sorted.slice(0, n).map(d => d.label);
            renderCalendar();
            updateCount();
        }}
        function selectLast7()  {{ selectLast(7);  }}
        function selectLast30() {{ selectLast(30); }}

        function applyFilter() {{
            updateDashboard();
        }}

        // Obter dados filtrados
        function getFilteredData() {{
            // Return in chronological order
            return allData.filter(d => selectedDates.includes(d.label));
        }}
        
        // Atualizar dashboard
        function updateDashboard() {{
            const data = getFilteredData();
            
            if (data.length === 0) return;
            
            // Atualizar estatísticas
            const last = data[data.length - 1];
            const prev = data.length > 1 ? data[data.length - 2] : null;
            
            document.getElementById('statCallIv').textContent = last.call_iv ? last.call_iv.toFixed(2) + '%' : 'N/A';
            document.getElementById('statPutIv').textContent = last.put_iv ? last.put_iv.toFixed(2) + '%' : 'N/A';
            document.getElementById('statSkew').textContent = last.skew ? last.skew.toFixed(2) + '%' : 'N/A';
            
            // Variações
            if (prev && last.call_iv && prev.call_iv) {{
                const change = last.call_iv - prev.call_iv;
                const el = document.getElementById('statCallChange');
                el.textContent = (change >= 0 ? '+' : '') + change.toFixed(2) + '%';
                el.className = 'sub ' + (change >= 0 ? 'positive' : 'negative');
            }}
            
            if (prev && last.put_iv && prev.put_iv) {{
                const change = last.put_iv - prev.put_iv;
                const el = document.getElementById('statPutChange');
                el.textContent = (change >= 0 ? '+' : '') + change.toFixed(2) + '%';
                el.className = 'sub ' + (change >= 0 ? 'positive' : 'negative');
            }}
            
            if (prev && last.skew && prev.skew) {{
                const change = last.skew - prev.skew;
                const el = document.getElementById('statSkewChange');
                el.textContent = (change >= 0 ? '+' : '') + change.toFixed(2) + '%';
                el.className = 'sub ' + (change >= 0 ? 'positive' : 'negative');
            }}
            
            if (prev && last.skew && prev.skew) {{
                const delta = last.skew - prev.skew;
                const el = document.getElementById('statSkewDelta');
                el.textContent = (delta >= 0 ? '+' : '') + delta.toFixed(2) + '%';
                el.style.color = delta >= 0 ? 'var(--red)' : 'var(--grn)';
            }}
            
            // Atualizar gráficos
            updateCharts(data);
            
            // Atualizar tabela
            updateTable(data);
        }}
        
        // Atualizar gráficos
        function updateCharts(data) {{
            const labels = data.map(d => d.label);
            const callData = data.map(d => d.call_iv);
            const putData = data.map(d => d.put_iv);
            const skewData = data.map(d => d.skew);
            
            // Destruir gráficos anteriores
            Object.values(charts).forEach(c => c.destroy());
            
            // CALL Chart
            charts.call = new Chart(document.getElementById('callChart'), {{
                type: 'line',
                data: {{
                    labels: labels,
                    datasets: [{{
                        label: 'IV Call (%)',
                        data: callData,
                        borderColor: '#3fb950',
                        backgroundColor: 'rgba(63, 185, 80, 0.1)',
                        tension: 0.3,
                        fill: true,
                        pointRadius: 4,
                        pointHoverRadius: 7
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: false }}
                    }},
                    scales: {{
                        x: {{ ticks: {{ color: '#8b949e', maxRotation: 45 }}, grid: {{ color: '#30363d' }} }},
                        y: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#30363d' }}, title: {{ display: true, text: 'IV (%)', color: '#8b949e' }} }}
                    }}
                }}
            }});
            
            // PUT Chart
            charts.put = new Chart(document.getElementById('putChart'), {{
                type: 'line',
                data: {{
                    labels: labels,
                    datasets: [{{
                        label: 'IV Put (%)',
                        data: putData,
                        borderColor: '#f85149',
                        backgroundColor: 'rgba(248, 81, 73, 0.1)',
                        tension: 0.3,
                        fill: true,
                        pointRadius: 4,
                        pointHoverRadius: 7
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: false }}
                    }},
                    scales: {{
                        x: {{ ticks: {{ color: '#8b949e', maxRotation: 45 }}, grid: {{ color: '#30363d' }} }},
                        y: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#30363d' }}, title: {{ display: true, text: 'IV (%)', color: '#8b949e' }} }}
                    }}
                }}
            }});
            
            // SKEW Chart
            charts.skew = new Chart(document.getElementById('skewChart'), {{
                type: 'line',
                data: {{
                    labels: labels,
                    datasets: [{{
                        label: 'Skew (Put - Call) %',
                        data: skewData,
                        borderColor: '#d29922',
                        backgroundColor: 'rgba(210, 153, 34, 0.15)',
                        tension: 0.3,
                        fill: true,
                        pointRadius: 5,
                        pointHoverRadius: 8
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: false }}
                    }},
                    scales: {{
                        x: {{ ticks: {{ color: '#8b949e', maxRotation: 45 }}, grid: {{ color: '#30363d' }} }},
                        y: {{ 
                            ticks: {{ color: '#8b949e' }}, 
                            grid: {{ color: '#30363d' }}, 
                            title: {{ display: true, text: 'Skew (%)', color: '#8b949e' }},
                            suggestedMin: -2,
                            suggestedMax: 5
                        }}
                    }}
                }}
            }});
        }}
        
        // Atualizar tabela
        const TABLE_DEFAULT_ROWS = 7;
        let tableShowAll = false;
        let lastTableData = [];

        function toggleTableExpand() {{
            tableShowAll = !tableShowAll;
            renderTable(lastTableData);
        }}

        function renderTable(data) {{
            lastTableData = data;
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';

            // Mais recente primeiro
            const reversed = [...data].reverse();
            const visible = tableShowAll ? reversed : reversed.slice(0, TABLE_DEFAULT_ROWS);

            visible.forEach((row, i) => {{
                const origIdx = data.indexOf(row);
                const prev = origIdx > 0 ? data[origIdx - 1] : null;

                const tr = document.createElement('tr');

                let dCall = '-', dPut = '-', dSkew = '-';
                let cCall = '', cPut = '', cSkew = '';

                if (prev) {{
                    if (row.call_iv && prev.call_iv) {{
                        const d = row.call_iv - prev.call_iv;
                        dCall = (d >= 0 ? '+' : '') + d.toFixed(2) + '%';
                        cCall = d >= 0 ? 'positive' : 'negative';
                    }}
                    if (row.put_iv && prev.put_iv) {{
                        const d = row.put_iv - prev.put_iv;
                        dPut = (d >= 0 ? '+' : '') + d.toFixed(2) + '%';
                        cPut = d >= 0 ? 'positive' : 'negative';
                    }}
                    if (row.skew && prev.skew) {{
                        const d = row.skew - prev.skew;
                        dSkew = (d >= 0 ? '+' : '') + d.toFixed(2) + '%';
                        cSkew = d >= 0 ? 'positive' : 'negative';
                    }}
                }}

                tr.innerHTML = `
                    <td><strong>${{row.label}}</strong></td>
                    <td>${{row.call_iv ? row.call_iv.toFixed(2) : '-'}}</td>
                    <td>${{row.put_iv ? row.put_iv.toFixed(2) : '-'}}</td>
                    <td>${{row.skew ? row.skew.toFixed(2) : '-'}}</td>
                    <td class="${{cCall}}">${{dCall}}</td>
                    <td class="${{cPut}}">${{dPut}}</td>
                    <td class="${{cSkew}}">${{dSkew}}</td>
                `;
                tbody.appendChild(tr);
            }});

            // Botão expandir
            const btn = document.getElementById('btnExpandTable');
            if (data.length <= TABLE_DEFAULT_ROWS) {{
                btn.style.display = 'none';
            }} else {{
                btn.style.display = 'inline-block';
                btn.textContent = tableShowAll
                    ? '▲ Mostrar menos'
                    : `▼ Ver todos os ${{data.length}} dias`;
            }}
        }}

        function updateTable(data) {{
            tableShowAll = false;
            renderTable(data);
        }}
        
        // Iniciar
        renderCalendar();
        updateCount();
        updateDashboard();
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
</html>'''
    
    return html


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 65)
    print("  BOVA11 Skew History — Evolução da Volatilidade")
    print("=" * 65)
    
    # Carregar histórico existente (persiste mesmo se CSVs forem apagados)
    history = load_history()
    print(f"\n  📁 Histórico carregado: {len(history)} datas")
    print(f"  💡 O arquivo '{HISTORY_FILE}' mantém todos os dados mesmo após apagar os CSVs")
    
    # Descobrir todos os arquivos de fechamento disponíveis
    print("\n  🔍 Escaneando arquivos CSV...")
    all_files = []
    
    for fname in os.listdir(CSV_DIR):
        if is_primary_fechamento_file(fname):
            dia, mes, label = extract_date_from_filename(fname)
            if dia and mes:
                all_files.append((dia, mes, label, fname))
    
    if not all_files:
        print("  ❌ Nenhum arquivo de fechamento encontrado")
        return
    
    print(f"  ✓ {len(all_files)} arquivos encontrados")
    
    # Extrair datas únicas e ordenar
    unique_dates = list(set([(dia, mes, label) for dia, mes, label, _ in all_files]))
    unique_dates.sort(key=lambda x: parse_date_to_datetime(*x[:2]))
    valid_date_keys = [f"{dia}/{mes}" for dia, mes, _ in unique_dates]

    stale_dates = prune_history(history, valid_date_keys)
    if stale_dates:
        stale_dates.sort(key=lambda x: parse_date_to_datetime(*x.split('/')))
        print(f"  🧹 Removendo datas inválidas do histórico: {', '.join(stale_dates)}")

    print(f"\n  📅 Período disponível:")
    print(f"     De: {format_date_label(*unique_dates[0][:2])}")
    print(f"     Até: {format_date_label(*unique_dates[-1][:2])}")
    print(f"     Total: {len(unique_dates)} datas")
    
    # Configurações (sempre todos os vencimentos)
    filter_type, filter_value = 'all', None
    strike_min, strike_max = ask_strike_range()
    
    # Processar TODOS os arquivos (geração completa)
    print("\n" + "=" * 65)
    print("  PROCESSANDO TODOS OS DADOS")
    print("=" * 65)
    
    vencimentos_processados = set()
    
    for dia, mes, label in unique_dates:
        date_key = f"{dia}/{mes}"
        print(f"\n  📅 {label}")
        
        # Encontrar arquivos para esta data
        date_files = [f for d, m, l, f in all_files if d == dia and m == mes]
        
        for fname in date_files:
            # Extrair nome do vencimento
            venc_match = re.search(r'venc\s+(.+?)\s+fechamento', fname, re.IGNORECASE)
            if not venc_match:
                continue
            
            vencimento = venc_match.group(1).strip()
            
            # Processar TODOS os vencimentos (sem filtro)
            # Ler e processar
            filepath = os.path.join(CSV_DIR, fname)
            records = read_fechamento_iv(filepath)
            
            if records:
                stats = calc_iv_stats(records, strike_min, strike_max)
                if stats:
                    update_history(history, date_key, vencimento, stats)
                    vencimentos_processados.add(vencimento)
                    call_str = f"{stats['call_iv_mean']:.2f}" if stats['call_iv_mean'] is not None else "N/A"
                    put_str = f"{stats['put_iv_mean']:.2f}" if stats['put_iv_mean'] is not None else "N/A"
                    skew_str = f"{stats['skew']:.2f}" if stats['skew'] is not None else "N/A"
                    print(f"     ✓ {vencimento}: Call={call_str}%, Put={put_str}%, Skew={skew_str}%")
    
    # Salvar histórico atualizado (acumula dados, nunca apaga)
    save_history(history)
    print(f"\n  💾 Histórico salvo: {HISTORY_FILE}")
    print(f"  📊 Vencimentos processados nesta execução: {len(vencimentos_processados)}")
    print(f"  📈 Total de datas no histórico: {len(history)}")
    print(f"  🔒 Dados preservados mesmo se os CSVs forem apagados")
    
    # Gerar HTML com TODAS as datas do histórico (não só dos CSVs atuais)
    print("\n  📝 Gerando HTML interativo...")
    
    # Criar lista de todas as datas do histórico para o HTML
    all_history_dates = []
    for date_key in history.keys():
        dia, mes = date_key.split('/')
        label = format_date_label(dia, mes)
        all_history_dates.append((dia, mes, label))
    
    # Ordenar por data
    all_history_dates.sort(key=lambda x: parse_date_to_datetime(*x[:2]))
    
    print(f"     📊 Gerando visualização com {len(all_history_dates)} datas do histórico")
    html = generate_html(history, all_history_dates, filter_type, filter_value, (strike_min, strike_max))
    
    output_file = "bova11_skew_history.html"
    
    with open(os.path.join(OUTPUT_DIR, output_file), 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"  ✅ HTML gerado: ./{output_file}")
    print(f"\n  📈 Funcionalidades do HTML:")
    print(f"     • Filtro interativo de datas (clique para selecionar)")
    print(f"     • Gráfico separado para CALL (verde)")
    print(f"     • Gráfico separado para PUT (vermelho)")
    print(f"     • Gráfico de SKEW (amarelo)")
    print(f"     • Tabela com deltas dia a dia")
    print(f"     • Estatísticas atualizadas em tempo real")
    
    print("\n" + "=" * 65)
    print("  CONCLUÍDO ✅")
    print("=" * 65)


if __name__ == '__main__':
    main()
