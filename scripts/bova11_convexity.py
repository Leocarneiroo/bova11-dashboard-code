#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
BOVA11 CONVEXITY ANALYZER — Vanna/Gamma/Charm Decomposition
================================================================================
Análise de convexidade de gregas: decomposição do ΔDelta em drivers
(Gamma/Spot, Vanna/IV, Charm/Tempo) para opções BOVA11.

Autor: Auto-generated
Data: 2026-02-25
================================================================================
"""

import os
import re
import math
import glob
import sys
from datetime import datetime, date

from bova11_shared import calc_convexity_decomposition, tag_to_iso

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — Editar conforme necessário
# ═══════════════════════════════════════════════════════════════════════════════

_BASEDIR   = os.path.dirname(os.path.abspath(__file__))
CSV_DIR    = os.path.join(_BASEDIR, '..', 'data')   # Diretório com arquivos CSV
ANO        = str(datetime.now().year)                 # Ano corrente
OUTPUT_DIR = os.path.join(_BASEDIR, '..', 'output') # Diretório de saída

# Parâmetros de análise
EPS_IV = 0.5                     # Tolerância mínima para variação de IV (em %)
EPS_TIE = 0.15                   # Tolerância para empate entre drivers (0.15 = 15%)
TOP_N = 15                       # Número de itens no ranking

# Mapeamento de vencimentos para cálculo de DTE
VENC_DATES = {
    "27 fev W4":     f"{ANO}-02-27",
    "6 mar W1":      f"{ANO}-03-06",
    "13 mar W2":     f"{ANO}-03-13",
    "20 mar Mensal": f"{ANO}-03-20",
    "10 abr W2":     f"{ANO}-04-10",
    "17 abr Mensal": f"{ANO}-04-17",
    "24 abr W2":     f"{ANO}-04-24",
    "30 abr W5":     f"{ANO}-04-30",
    "15 mai Mensal": f"{ANO}-05-15",
}

# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-DISCOVERY DE DATAS
# ═══════════════════════════════════════════════════════════════════════════════

def extract_date_from_filename(filename):
    """
    Extrai a data do nome do arquivo.
    Formatos suportados:
      - venc ... fechamento (DDfev).csv
      - venc ... fechamento (DDfev Volume).csv
      - venc ... fechamento__DDfev_.csv
    Retorna tupla (dia, mes_str) ou (None, None)
    """
    # Pattern para formato com parênteses: (25fev) ou (25fev Volume)
    pattern1 = r'\((\d{1,2})([a-z]{3})\s*(?:Volume)?\)'
    match = re.search(pattern1, filename, re.IGNORECASE)
    if match:
        dia = match.group(1).zfill(2)
        mes_str = match.group(2).lower()
        return dia, mes_str
    
    # Pattern para formato com underscore: __25fev_
    pattern2 = r'__(\d{1,2})([a-z]{3})_'
    match = re.search(pattern2, filename, re.IGNORECASE)
    if match:
        dia = match.group(1).zfill(2)
        mes_str = match.group(2).lower()
        return dia, mes_str
    
    return None, None


def format_date_label(dia, mes_str):
    """Converte dia/mês para label amigável (ex: 25/Fev)."""
    meses = {
        'jan': 'Jan', 'fev': 'Fev', 'mar': 'Mar', 'abr': 'Abr',
        'mai': 'Mai', 'jun': 'Jun', 'jul': 'Jul', 'ago': 'Ago',
        'set': 'Set', 'out': 'Out', 'nov': 'Nov', 'dez': 'Dez'
    }
    mes = meses.get(mes_str, mes_str.capitalize())
    return f"{dia}/{mes}"

def is_primary_fechamento_file(filename):
    lower = filename.lower()
    return (
        lower.endswith('.csv')
        and 'fechamento' in lower
        and 'volume' not in lower
        and ' copy' not in lower
    )


def discover_dates():
    """
    Descobre automaticamente as datas D-1 e D disponíveis nos arquivos.
    Retorna tupla (tag_d1, tag_d, label_d1, label_d) ou (None, None, None, None)
    """
    fechamento_files = []
    
    for fname in os.listdir(CSV_DIR):
        if is_primary_fechamento_file(fname):
            dia, mes_str = extract_date_from_filename(fname)
            if dia and mes_str:
                fechamento_files.append((dia, mes_str, fname))
    
    grouped = {}
    for dia, mes_str, fname in fechamento_files:
        key = (dia, mes_str)
        grouped.setdefault(key, []).append(fname)

    unique_files = [(dia, mes_str, files[0]) for (dia, mes_str), files in grouped.items() if len(files) >= 2]
    if not unique_files:
        unique_files = [(dia, mes_str, files[0]) for (dia, mes_str), files in grouped.items()]
    
    if not unique_files:
        return None, None, None, None
    
    # Ordenar por mês e dia
    meses_ord = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
                 'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
    
    unique_files.sort(key=lambda x: (meses_ord.get(x[1], 99), int(x[0])))
    
    if len(unique_files) >= 2:
        d1_dia, d1_mes, _ = unique_files[-2]
        d_dia, d_mes, _ = unique_files[-1]
    else:
        return None, None, None, None
    
    tag_d1 = f"{int(d1_dia)}{d1_mes}"
    tag_d = f"{int(d_dia)}{d_mes}"
    label_d1 = format_date_label(d1_dia, d1_mes)
    label_d = format_date_label(d_dia, d_mes)
    
    return tag_d1, tag_d, label_d1, label_d


def ask_spot():
    """Solicita o preço spot do BOVA11 ao usuário."""
    while True:
        try:
            spot_input = input("Digite o preço spot do BOVA11 (ou pressione Enter para auto-detectar): ").strip()
            if not spot_input:
                return None
            spot = float(spot_input.replace(',', '.'))
            return spot
        except ValueError:
            print("Valor inválido. Tente novamente.")


# ═══════════════════════════════════════════════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_num(s):
    """Converte string numérica brasileira para float."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if s == '' or s == '-':
        return None
    # Remove pontos de milhar e substitui vírgula decimal
    s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def read_fechamento_wide(filepath):
    """
    Lê arquivo CSV de fechamento no formato wide B3:
    CALL cols (0-10) | STRIKE (11) | PUT cols (12-22)
    
    Formato:
    CALL: Ativo(0), Último(1), C.Abertos(2), Delta(3), Gamma(4), Theta(5), Vega(6), Vol Impl(7), Negócios(8), Bid(9), Ask(10)
    STRIKE: 11
    PUT: Bid(12), Ask(13), Negócios(14), Vol Impl(15), Vega(16), Theta(17), Gamma(18), Delta(19), C.Abertos(20), Último(21), Ativo(22)
    
    Retorna lista de dicionários com dados processados.
    """
    records = []
    
    try:
        with open(filepath, 'r', encoding='latin-1') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Erro ao ler {filepath}: {e}")
        return records
    
    if not lines:
        return records
    
    # Pula cabeçalho
    data_lines = lines[1:]
    
    # Processar linhas de dados
    for line in data_lines:
        line = line.strip()
        if not line:
            continue
        
        parts = line.split(';')
        if len(parts) < 23:  # Precisa ter todas as colunas
            continue
        
        try:
            # Strike está na coluna 11
            strike = parse_num(parts[11])
            if strike is None:
                continue
            
            record = {
                # CALL: colunas 0-10
                'strike': strike,
                'call_code': parts[0],
                'call_price': parse_num(parts[1]),
                'call_oi': parse_num(parts[2]),
                'call_delta': parse_num(parts[3]),
                'call_gamma': parse_num(parts[4]),
                'call_theta': parse_num(parts[5]),
                'call_vega': parse_num(parts[6]),
                'call_iv': parse_num(parts[7]),
                # PUT: colunas 12-22 (ordem invertida em relação ao call)
                'put_code': parts[22],
                'put_price': parse_num(parts[21]),
                'put_oi': parse_num(parts[20]),
                'put_delta': parse_num(parts[19]),
                'put_gamma': parse_num(parts[18]),
                'put_theta': parse_num(parts[17]),
                'put_vega': parse_num(parts[16]),
                'put_iv': parse_num(parts[15]),
            }
            
            records.append(record)
        except Exception as e:
            continue
    
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def safe_diff(val_d, val_d1):
    """Calcula diferença segura entre dois valores."""
    if val_d is None or val_d1 is None:
        return None
    return val_d - val_d1


def calc_median(values):
    """Calcula mediana de uma lista de valores."""
    valid = [v for v in values if v is not None and not math.isnan(v)]
    if not valid:
        return None
    valid.sort()
    n = len(valid)
    if n % 2 == 1:
        return valid[n // 2]
    return (valid[n // 2 - 1] + valid[n // 2]) / 2


def calc_mad(values, median):
    """Calcula Median Absolute Deviation."""
    valid = [v for v in values if v is not None and not math.isnan(v)]
    if not valid or median is None:
        return None
    abs_devs = [abs(v - median) for v in valid]
    abs_devs.sort()
    n = len(abs_devs)
    if n % 2 == 1:
        return abs_devs[n // 2]
    return (abs_devs[n // 2 - 1] + abs_devs[n // 2]) / 2


# ═══════════════════════════════════════════════════════════════════════════════
# CONVEXITY ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def calc_convexity_metrics(records_d, records_d1, spot_d, spot_d1, venc_label, tag_d1, tag_d):
    """
    Calcula métricas de convexidade decompondo ΔDelta em:
    - Gamma (contribuição do spot)
    - Vanna (contribuição da IV)
    - Charm (residual - tempo e outros)
    
    Retorna lista de dicionários com análise completa.
    """
    results = []
    
    d1_lookup = {}
    for r in records_d1:
        strike = r.get('strike')
        if strike:
            d1_lookup[strike] = r

    delta_spot = safe_diff(spot_d, spot_d1)
    session_date_d = tag_to_iso(tag_d)
    session_date_d1 = tag_to_iso(tag_d1)

    for r_d in records_d:
        strike = r_d.get('strike')
        if not strike:
            continue

        r_d1 = d1_lookup.get(strike)
        if not r_d1:
            continue

        for side, tipo in (('call', 'CALL'), ('put', 'PUT')):
            delta_d = r_d.get(f'{side}_delta')
            delta_d1 = r_d1.get(f'{side}_delta')
            if delta_d is None or delta_d1 is None:
                continue

            iv_d = r_d.get(f'{side}_iv')
            iv_d1 = r_d1.get(f'{side}_iv')
            oi_d = r_d.get(f'{side}_oi') or 0
            oi_d1 = r_d1.get(f'{side}_oi') or 0

            decomp = calc_convexity_decomposition(
                strike=strike,
                option_type=tipo,
                spot_d=spot_d,
                spot_d1=spot_d1,
                delta_d=delta_d,
                delta_d1=delta_d1,
                gamma_d=r_d.get(f'{side}_gamma'),
                gamma_d1=r_d1.get(f'{side}_gamma'),
                iv_d=iv_d,
                iv_d1=iv_d1,
                expiry_label=venc_label,
                session_date_d=session_date_d,
                session_date_d1=session_date_d1,
                r=0.0,
            )

            values = [
                ('GAMMA', decomp['abs_gamma']),
                ('VANNA', decomp['abs_vanna']),
                ('CHARM', decomp['abs_charm']),
                ('RESIDUAL', decomp['abs_residual']),
            ]
            values.sort(key=lambda x: x[1], reverse=True)

            driver = values[0][0]
            max_val = values[0][1]
            second_val = values[1][1] if len(values) > 1 else 0
            if max_val > 0 and second_val >= (1 - EPS_TIE) * max_val:
                driver = 'MIXED'

            oi_avg = (oi_d + oi_d1) / 2 if oi_d is not None and oi_d1 is not None else (oi_d or oi_d1 or 0)
            impact_score = abs(decomp['delta_delta']) * math.sqrt(oi_avg) if oi_avg > 0 else abs(decomp['delta_delta'])

            results.append({
                'vencimento': venc_label,
                'strike': strike,
                'tipo': tipo,
                'code': r_d.get(f'{side}_code', ''),
                'delta_d': delta_d,
                'delta_d1': delta_d1,
                'delta_delta': decomp['delta_delta'],
                'spot_d': spot_d,
                'spot_d1': spot_d1,
                'delta_spot': delta_spot,
                'gamma_avg': decomp['gamma_ref'],
                'gamma_contrib': decomp['gamma_contrib'],
                'iv_d': iv_d,
                'iv_d1': iv_d1,
                'delta_iv': (iv_d - iv_d1) if (iv_d is not None and iv_d1 is not None) else None,
                'vanna_obs': decomp['vanna_ref'],
                'vanna_contrib': decomp['vanna_contrib'],
                'charm_contrib': decomp['charm_contrib'],
                'residual': decomp['residual'],
                'abs_gamma': decomp['abs_gamma'],
                'abs_vanna': decomp['abs_vanna'],
                'abs_charm': decomp['abs_charm'],
                'abs_residual': decomp['abs_residual'],
                'driver': driver,
                'oi_avg': oi_avg,
                'impact_score': impact_score,
            })
    
    return results


def add_volume_data(results, vol_file_d, vol_file_d1):
    """Adiciona dados de volume aos resultados."""
    # Implementação simplificada - pode ser expandida conforme necessidade
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# RANKINGS
# ═══════════════════════════════════════════════════════════════════════════════

def get_top_n(results, key='impact_score', n=TOP_N, reverse=True):
    """Retorna top N resultados ordenados por chave."""
    valid = [r for r in results if r.get(key) is not None]
    valid.sort(key=lambda x: x[key], reverse=reverse)
    return valid[:n]


def format_num(val, decimals=2, pct=False):
    """Formata número para exibição."""
    if val is None:
        return '-'
    if pct:
        return f"{val:.{decimals}f}%"
    if abs(val) >= 1000000:
        return f"{val/1000000:.{decimals}f}M"
    if abs(val) >= 1000:
        return f"{val/1000:.{decimals}f}k"
    return f"{val:.{decimals}f}"


def format_delta(val):
    """Formata delta com sinal e cor."""
    if val is None:
        return '-'
    sign = '+' if val > 0 else ''
    return f"{sign}{val:.4f}"


# ═══════════════════════════════════════════════════════════════════════════════
# HTML GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_html(all_results, stats, tag_d1, tag_d, label_d1, label_d, spot_d, spot_d1):
    """Gera arquivo HTML com análise de convexidade."""
    
    delta_spot = safe_diff(spot_d, spot_d1)
    
    # Separar por driver
    gamma_results = [r for r in all_results if r['driver'] == 'GAMMA']
    vanna_results = [r for r in all_results if r['driver'] == 'VANNA']
    charm_results = [r for r in all_results if r['driver'] == 'CHARM']
    residual_results = [r for r in all_results if r['driver'] == 'RESIDUAL']
    mixed_results = [r for r in all_results if r['driver'] == 'MIXED']
    
    # Top N por categoria
    top_gamma = get_top_n(gamma_results, 'impact_score', TOP_N)
    top_vanna = get_top_n(vanna_results, 'impact_score', TOP_N)
    top_charm = get_top_n(charm_results, 'impact_score', TOP_N)
    top_residual = get_top_n(residual_results, 'impact_score', TOP_N)
    top_mixed = get_top_n(mixed_results, 'impact_score', TOP_N)
    top_overall = get_top_n(all_results, 'impact_score', TOP_N)
    
    def row_html(r, idx):
        driver_colors = {
            'GAMMA': '#4ade80',
            'VANNA': '#38bdf8',
            'CHARM': '#f472b6',
            'RESIDUAL': '#a78bfa',
            'MIXED': '#fbbf24'
        }
        color = driver_colors.get(r['driver'], '#94a3b8')
        
        return f"""
        <tr>
            <td>{idx}</td>
            <td>{r['code']}</td>
            <td>{r['vencimento']}</td>
            <td>{r['strike']:.2f}</td>
            <td>{r['tipo']}</td>
            <td style="color: {color}; font-weight: bold;">{r['driver']}</td>
            <td>{format_delta(r['delta_delta'])}</td>
            <td>{format_num(r['gamma_contrib'], 4)}</td>
            <td>{format_num(r['vanna_contrib'], 4)}</td>
            <td>{format_num(r['charm_contrib'], 4)}</td>
            <td>{format_num(r['residual'], 4)}</td>
            <td>{format_num(r['oi_avg'], 0)}</td>
            <td>{format_num(r['impact_score'], 2)}</td>
        </tr>
        """
    
    def table_html(title, results):
        if not results:
            return f"<h3>{title}</h3><p>Nenhum resultado encontrado.</p>"
        
        rows = ''.join([row_html(r, i+1) for i, r in enumerate(results)])
        
        return f"""
        <h3>{title}</h3>
        <table>
            <thead>
                <tr>
                    <th>#</th>
                    <th>Código</th>
                    <th>Vencimento</th>
                    <th>Strike</th>
                    <th>Tipo</th>
                    <th>Driver</th>
                    <th>ΔDelta</th>
                    <th>Gamma</th>
                    <th>Vanna</th>
                    <th>Charm</th>
                    <th>Residual</th>
                    <th>OI Médio</th>
                    <th>Impact Score</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
        """
    
    # Estatísticas para cards
    total_ops = len(all_results)
    gamma_pct = len(gamma_results) / total_ops * 100 if total_ops > 0 else 0
    vanna_pct = len(vanna_results) / total_ops * 100 if total_ops > 0 else 0
    charm_pct = len(charm_results) / total_ops * 100 if total_ops > 0 else 0
    residual_pct = len(residual_results) / total_ops * 100 if total_ops > 0 else 0
    mixed_pct = len(mixed_results) / total_ops * 100 if total_ops > 0 else 0
    
    html_content = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOVA11 Convexity Analysis — {label_d1} vs {label_d}</title>
    <style>
        :root {{
            --bg-primary: #ffffff;
            --bg-secondary: #f6f8fa;
            --bg-tertiary: #eaeef2;
            --text-primary: #1f2328;
            --text-secondary: #636c76;
            --border: #d0d7de;
            --gamma: #4ade80;
            --vanna: #38bdf8;
            --charm: #f472b6;
            --residual: #a78bfa;
            --mixed: #fbbf24;
        }}

        [data-theme="dark"] {{
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --text-primary: #c9d1d9;
            --text-secondary: #8b949e;
            --border: #30363d;
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        
        header {{
            text-align: center;
            padding: 30px 0;
            border-bottom: 1px solid var(--border);
            margin-bottom: 30px;
        }}
        
        h1 {{
            font-size: 2rem;
            margin-bottom: 10px;
            background: linear-gradient(90deg, var(--gamma), var(--vanna), var(--charm));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        
        .subtitle {{
            color: var(--text-secondary);
            font-size: 1.1rem;
        }}
        
        .spot-info {{
            display: flex;
            justify-content: center;
            gap: 40px;
            margin-top: 20px;
            flex-wrap: wrap;
        }}
        
        .spot-box {{
            background: var(--bg-secondary);
            padding: 15px 25px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        
        .spot-label {{
            font-size: 0.85rem;
            color: var(--text-secondary);
            text-transform: uppercase;
        }}
        
        .spot-value {{
            font-size: 1.5rem;
            font-weight: bold;
            margin-top: 5px;
        }}
        
        .spot-change {{
            font-size: 0.9rem;
            margin-top: 5px;
        }}
        
        .positive {{ color: var(--gamma); }}
        .negative {{ color: #f85149; }}
        
        /* Stats Cards */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }}
        
        .stat-card {{
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 25px;
            border: 1px solid var(--border);
            position: relative;
            overflow: hidden;
        }}
        
        .stat-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
        }}
        
        .stat-card.gamma::before {{ background: var(--gamma); }}
        .stat-card.vanna::before {{ background: var(--vanna); }}
        .stat-card.charm::before {{ background: var(--charm); }}
        .stat-card.residual::before {{ background: var(--residual); }}
        .stat-card.mixed::before {{ background: var(--mixed); }}
        
        .stat-title {{
            font-size: 0.9rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }}
        
        .stat-value {{
            font-size: 2.5rem;
            font-weight: bold;
            margin-bottom: 5px;
        }}
        
        .stat-card.gamma .stat-value {{ color: var(--gamma); }}
        .stat-card.vanna .stat-value {{ color: var(--vanna); }}
        .stat-card.charm .stat-value {{ color: var(--charm); }}
        .stat-card.residual .stat-value {{ color: var(--residual); }}
        .stat-card.mixed .stat-value {{ color: var(--mixed); }}
        
        .stat-desc {{
            font-size: 0.85rem;
            color: var(--text-secondary);
        }}
        
        /* Tables */
        h3 {{
            margin: 40px 0 20px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border);
            color: var(--text-primary);
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--bg-secondary);
            border-radius: 8px;
            overflow: hidden;
            margin-bottom: 40px;
            font-size: 0.9rem;
        }}
        
        th, td {{
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }}
        
        th {{
            background: var(--bg-tertiary);
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            font-size: 0.8rem;
            letter-spacing: 0.5px;
        }}
        
        tr:hover {{
            background: rgba(255,255,255,0.03);
        }}
        
        /* Methodology Section */
        .methodology {{
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 30px;
            border: 1px solid var(--border);
            margin-top: 40px;
        }}
        
        .methodology h3 {{
            margin-top: 0;
            color: var(--vanna);
        }}
        
        .methodology h4 {{
            color: var(--text-primary);
            margin: 20px 0 10px;
        }}
        
        .methodology p, .methodology li {{
            color: var(--text-secondary);
            margin-bottom: 10px;
        }}
        
        .methodology ul {{
            margin-left: 20px;
        }}
        
        .formula {{
            background: var(--bg-tertiary);
            padding: 15px;
            border-radius: 6px;
            font-family: 'Courier New', monospace;
            margin: 15px 0;
            border-left: 3px solid var(--vanna);
        }}
        
        .legend {{
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            margin: 20px 0;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        
        .legend-color {{
            width: 16px;
            height: 16px;
            border-radius: 4px;
        }}
        
        footer {{
            text-align: center;
            padding: 30px 0;
            color: var(--text-secondary);
            font-size: 0.85rem;
            border-top: 1px solid var(--border);
            margin-top: 40px;
        }}
    </style>
</head>
<body>
    <button id="theme-toggle" onclick="toggleTheme()" title="Alternar tema" style="position:fixed;top:12px;right:16px;z-index:9999;background:var(--bg-secondary);border:1px solid var(--border);color:var(--text-primary);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:1rem;line-height:1;transition:all 0.2s;">◐</button>
    <div class="container">
        <header>
            <h1>📊 BOVA11 Convexity Analyzer</h1>
            <p class="subtitle">Decomposição de ΔDelta em Vanna, Gamma e Charm — {label_d1} vs {label_d}</p>
            
            <div class="spot-info">
                <div class="spot-box">
                    <div class="spot-label">Spot D-1 ({label_d1})</div>
                    <div class="spot-value">{spot_d1:.2f}</div>
                </div>
                <div class="spot-box">
                    <div class="spot-label">Spot D ({label_d})</div>
                    <div class="spot-value">{spot_d:.2f}</div>
                </div>
                <div class="spot-box">
                    <div class="spot-label">Variação Spot</div>
                    <div class="spot-value {'positive' if delta_spot and delta_spot > 0 else 'negative'}">{delta_spot:+.2f}</div>
                    <div class="spot-change {'positive' if delta_spot and delta_spot > 0 else 'negative'}">{((delta_spot/spot_d1)*100 if delta_spot and spot_d1 else 0):+.2f}%</div>
                </div>
            </div>
        </header>
        
        <!-- Stats Cards -->
        <div class="stats-grid">
            <div class="stat-card gamma">
                <div class="stat-title">Gamma-Driven</div>
                <div class="stat-value">{gamma_pct:.1f}%</div>
                <div class="stat-desc">{len(gamma_results)} operações dominadas por movimento do spot</div>
            </div>
            <div class="stat-card vanna">
                <div class="stat-title">Vanna-Driven</div>
                <div class="stat-value">{vanna_pct:.1f}%</div>
                <div class="stat-desc">{len(vanna_results)} operações dominadas por variação de IV</div>
            </div>
            <div class="stat-card charm">
                <div class="stat-title">Charm-Driven</div>
                <div class="stat-value">{charm_pct:.1f}%</div>
                <div class="stat-desc">{len(charm_results)} operações dominadas por passagem do tempo</div>
            </div>
            <div class="stat-card residual">
                <div class="stat-title">Residual-Driven</div>
                <div class="stat-value">{residual_pct:.1f}%</div>
                <div class="stat-desc">{len(residual_results)} operações não explicadas pelos drivers modeláveis</div>
            </div>
            <div class="stat-card mixed">
                <div class="stat-title">Mixed Drivers</div>
                <div class="stat-value">{mixed_pct:.1f}%</div>
                <div class="stat-desc">{len(mixed_results)} operações com múltiplos drivers significativos</div>
            </div>
        </div>
        
        <!-- Legend -->
        <div class="legend">
            <div class="legend-item">
                <div class="legend-color" style="background: var(--gamma);"></div>
                <span>GAMMA/SPOT — Movimento do underlying</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background: var(--vanna);"></div>
                <span>VANNA/IV — Variação da volatilidade implícita</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background: var(--charm);"></div>
                <span>CHARM/TIME — Decaimento temporal (theta)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background: var(--residual);"></div>
                <span>RESIDUAL — Parte não explicada por gamma/vanna/charm</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background: var(--mixed);"></div>
                <span>MIXED — Múltiplos drivers com peso similar</span>
            </div>
        </div>
        
        <!-- Top Overall -->
        {table_html('🏆 Top ' + str(TOP_N) + ' — Maior Impacto Geral', top_overall)}
        
        <!-- Top by Driver -->
        {table_html('🟢 Gamma-Driven — Top ' + str(TOP_N), top_gamma)}
        {table_html('🔵 Vanna-Driven — Top ' + str(TOP_N), top_vanna)}
        {table_html('🟣 Charm-Driven — Top ' + str(TOP_N), top_charm)}
        {table_html('🟪 Residual-Driven — Top ' + str(TOP_N), top_residual)}
        {table_html('🟡 Mixed Drivers — Top ' + str(TOP_N), top_mixed)}
        
        <!-- Methodology -->
        <div class="methodology">
            <h3>📐 Metodologia de Decomposição de Convexidade</h3>
            
            <h4>1. Decomposição do ΔDelta</h4>
            <p>A variação do Delta (ΔDelta) de uma opção é decomposta em três componentes modeláveis e um residual explícito:</p>
            
            <div class="formula">
                ΔDelta = Gamma_Contrib + Vanna_Contrib + Charm_Contrib + Residual
            </div>
            
            <h4>2. Componentes</h4>
            <ul>
                <li><strong style="color: var(--gamma);">Gamma (Γ)</strong>: Sensibilidade do Delta ao movimento do spot</li>
                <li><strong style="color: var(--vanna);">Vanna</strong>: Sensibilidade do Delta à variação da volatilidade implícita (IV)</li>
                <li><strong style="color: var(--charm);">Charm</strong>: Sensibilidade do Delta à passagem do tempo (também conhecido como Delta Decay)</li>
                <li><strong style="color: var(--residual);">Residual</strong>: Parcela não explicada pela aproximação local do modelo</li>
            </ul>
            
            <h4>3. Fórmulas de Cálculo</h4>
            <div class="formula">
                GAMMA_CONTRIB = GAMMA_REF × ΔSpot<br><br>
                VANNA_CONTRIB = VANNA_BS × ΔSigma<br>
                CHARM_CONTRIB = CHARM_BS × ΔT<br><br>
                RESIDUAL = ΔDelta − GAMMA_CONTRIB − VANNA_CONTRIB − CHARM_CONTRIB
            </div>
            
            <h4>4. Classificação do Driver</h4>
            <p>O driver dominante é determinado comparando as contribuições absolutas:</p>
            <div class="formula">
                max(|GAMMA_CONTRIB|, |VANNA_CONTRIB|, |CHARM_CONTRIB|, |RESIDUAL|)
            </div>
            <p>Uma operação é classificada como <strong style="color: var(--mixed);">MIXED</strong> quando o segundo maior valor 
            for maior ou igual a (1 − EPS_TIE) × maior valor, onde EPS_TIE = {EPS_TIE} ({EPS_TIE*100:.0f}%).</p>
            
            <h4>5. Impact Score</h4>
            <p>O impact score pondera a variação do Delta pelo Open Interest:</p>
            <div class="formula">
                IMPACT_SCORE = |ΔDelta| × √(OI_MÉDIO)
            </div>
            
            <h4>6. Interpretação</h4>
            <ul>
                <li><strong>GAMMA-driven</strong>: O movimento do spot foi o principal responsável pela mudança no Delta</li>
                <li><strong>VANNA-driven</strong>: Mudanças na volatilidade implícita dominaram a variação</li>
                <li><strong>CHARM-driven</strong>: O decaimento temporal (aproximação do vencimento) foi o fator principal</li>
                <li><strong>RESIDUAL-driven</strong>: Há efeito relevante fora dos três drivers locais modelados</li>
                <li><strong>MIXED</strong>: Múltiplos fatores contribuíram significativamente</li>
            </ul>
        </div>
        
        <footer>
            <p>BOVA11 Convexity Analyzer — Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
            <p>Dados: B3 — Análise para fins educacionais</p>
        </footer>
    </div>
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
</html>"""

    # Salvar arquivo
    output_filename = f"bova11_convexity_{tag_d1}_vs_{tag_d}.html"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Função principal de orquestração."""
    
    print("=" * 70)
    print("BOVA11 CONVEXITY ANALYZER — Vanna/Gamma/Charm Decomposition")
    print("=" * 70)
    
    # Descobrir datas automaticamente
    print("\n📅 Descobrindo datas disponíveis...")
    tag_d1, tag_d, label_d1, label_d = discover_dates()
    
    if not tag_d1 or not tag_d:
        print("❌ Erro: Não foi possível descobrir datas automaticamente.")
        print("   Certifique-se de que os arquivos CSV estão no formato correto.")
        sys.exit(1)
    
    print(f"   D-1: {label_d1} (tag: {tag_d1})")
    print(f"   D:   {label_d} (tag: {tag_d})")
    
    # Solicitar spot
    print("\n💰 Configuração de Preço Spot")
    spot_d = ask_spot()
    spot_d1 = None
    
    # Se spot_d foi fornecido, perguntar spot_d1
    if spot_d is not None:
        while True:
            try:
                spot_input = input(f"Digite o preço spot do BOVA11 para D-1 ({label_d1}): ").strip()
                spot_d1 = float(spot_input.replace(',', '.'))
                break
            except ValueError:
                print("Valor inválido. Tente novamente.")
    else:
        print("   Spot será inferido dos dados (não implementado nesta versão)")
        sys.exit(1)
    
    print(f"   Spot D-1: {spot_d1:.2f}")
    print(f"   Spot D:   {spot_d:.2f}")
    
    # Descobrir vencimentos disponíveis
    print("\n📁 Descobrindo vencimentos...")
    
    vencimentos = []
    for fname in os.listdir(CSV_DIR):
        if is_primary_fechamento_file(fname) and tag_d in fname:
            # Extrair label do vencimento
            # Formato: venc <label> fechamento ...
            match = re.search(r'venc\s+(.+?)\s+fechamento', fname, re.IGNORECASE)
            if match:
                venc_label = match.group(1).strip()
                vencimentos.append((venc_label, fname))
    
    print(f"   Encontrados {len(vencimentos)} vencimentos")
    
    # Processar cada vencimento
    all_results = []
    
    for venc_label, fname_d in vencimentos:
        print(f"\n📊 Processando: {venc_label}")
        
        # Encontrar arquivo D-1 correspondente
        fname_d1 = None
        for f in os.listdir(CSV_DIR):
            if is_primary_fechamento_file(f) and tag_d1 in f:
                match = re.search(r'venc\s+(.+?)\s+fechamento', f, re.IGNORECASE)
                if match and match.group(1).strip() == venc_label:
                    fname_d1 = f
                    break
        
        if not fname_d1:
            print(f"   ⚠️ Arquivo D-1 não encontrado para {venc_label}, pulando...")
            continue
        
        filepath_d = os.path.join(CSV_DIR, fname_d)
        filepath_d1 = os.path.join(CSV_DIR, fname_d1)
        
        print(f"   D:   {fname_d}")
        print(f"   D-1: {fname_d1}")
        
        # Ler arquivos
        records_d = read_fechamento_wide(filepath_d)
        records_d1 = read_fechamento_wide(filepath_d1)
        
        print(f"   Registros D: {len(records_d)}, D-1: {len(records_d1)}")
        
        if not records_d or not records_d1:
            print(f"   ⚠️ Dados insuficientes, pulando...")
            continue
        
        # Calcular métricas de convexidade
        results = calc_convexity_metrics(records_d, records_d1, spot_d, spot_d1, venc_label, tag_d1, tag_d)
        print(f"   Resultados calculados: {len(results)}")
        
        all_results.extend(results)
    
    if not all_results:
        print("\n❌ Nenhum resultado calculado. Verifique os dados de entrada.")
        sys.exit(1)
    
    print(f"\n📈 Total de operações analisadas: {len(all_results)}")
    
    # Estatísticas
    stats = {
        'total': len(all_results),
        'gamma': len([r for r in all_results if r['driver'] == 'GAMMA']),
        'vanna': len([r for r in all_results if r['driver'] == 'VANNA']),
        'charm': len([r for r in all_results if r['driver'] == 'CHARM']),
        'residual': len([r for r in all_results if r['driver'] == 'RESIDUAL']),
        'mixed': len([r for r in all_results if r['driver'] == 'MIXED']),
    }
    
    print(f"   Gamma-driven: {stats['gamma']}")
    print(f"   Vanna-driven: {stats['vanna']}")
    print(f"   Charm-driven: {stats['charm']}")
    print(f"   Residual-driven: {stats['residual']}")
    print(f"   Mixed: {stats['mixed']}")
    
    # Gerar HTML
    print("\n🎨 Gerando relatório HTML...")
    output_path = generate_html(all_results, stats, tag_d1, tag_d, label_d1, label_d, spot_d, spot_d1)
    
    print(f"\n✅ Relatório gerado com sucesso!")
    print(f"   Arquivo: {output_path}")
    print(f"\n   Abra o arquivo no navegador para visualizar a análise completa.")
    print("=" * 70)


if __name__ == "__main__":
    main()
