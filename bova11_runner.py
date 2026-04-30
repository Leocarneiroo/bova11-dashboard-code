#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Runner — Orquestrador de todos os scripts
=================================================
Roda na sequência correta:
  1. bova11_auto.py          → Dashboard principal (OI, GEX, MaxPain)
  2. bova11_insights_auto.py → Insights narrativos por vencimento
  3. bova11_skew_history.py  → Histórico de IV/Skew (acumulativo)
  4. bova11_convexity.py     → Decomposição Vanna/Gamma/Charm
  5. bova11_trade_score_james.py → Trade Score & Institutional Bias

Pede os dados de spot UMA única vez e repassa para todos os scripts.

Uso:
  python3 bova11_runner.py
"""

import os
import sys
import re
import glob
import json
import html
import subprocess
import importlib.util

DIR         = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(DIR, 'scripts')
DATA_DIR    = os.path.join(DIR, 'data')
OUTPUT_DIR  = os.path.join(DIR, 'output')
HISTORY_DIR = os.path.join(DIR, 'history')
SPOT_HISTORY_FILE = os.path.join(HISTORY_DIR, 'bova11_spot_history.json')

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from bova11_shared import upsert_spot_history

# ═══════════════════════════════════════
# LISTA DE SCRIPTS (em ordem de execução)
# ═══════════════════════════════════════
SCRIPTS = [
    ("1/22",  "bova11_auto.py",                  "Dashboard Principal (OI, GEX, MaxPain)"),
    ("2/22",  "bova11_insights_auto.py",         "Insights Narrativos por Vencimento"),
    ("3/22",  "bova11_skew_history.py",          "Histórico de IV / Skew"),
    ("4/22",  "bova11_convexity.py",             "Decomposição de Convexidade (Vanna/Gamma/Charm)"),
    ("5/22",  "bova11_trade_score_james.py",     "Trade Score & Institutional Bias"),
    ("6/22",  "bova11_quant_institucional.py",   "Quant Institucional (RevL / MoM / Força Relativa)"),
    ("7/22",  "bova11_tex_dex_vex.py",           "GEX / DEX / VEX / TEX / CEX — Perfis por Spot"),
    ("8/22",  "bova11_flow_history.py",          "Flow History — Entradas e Saídas de OI"),
    ("9/22",  "bova11_skew_prediction.py",       "IV Skew Signal — Superfície / Flow / Greeks / Regime"),
    ("10/22", "bova11_oi_stats_light.py",        "OI Stats — Distribuição Estatística"),
    ("11/22", "bova11_max_pain.py",              "Max Pain Relevance Indicator"),
    ("12/22", "bova11_gravity.py",               "Mapa Gravitacional — μOI / μGEX / μDEX / Convergência"),
    ("13/22", "bova11_hunter_walls.py",          "Hunter Walls — Volume por Strike"),
    ("14/22", "bova11_gex_history.py",           "GEX History — Evolução Histórica do Gamma Exposure"),
    ("15/22", "bova11_historical_data.py",       "Histórico de Mercado — OI/Volume/IV/MaxPain/GEX/DEX"),
    ("16/22", "bova11_market_gamma.py",          "Market Gamma — Curva Líquida Agregada e por Vencimento"),
    ("17/22", "bova11_diagnostico_4d.py",        "Diagnóstico 4D — Cenário GEX/DEX/VEX/TEX"),
    ("18/22", "bova11_bandas_vol.py",            "Bandas de Volatilidade — GARCH + IV Implícita"),
    ("19/22", "bova11_demand_flow.py",           "Demand Score — Score composto institucional por vencimento"),
    ("20/22", "bova11_tape_flow.py",             "Tape Flow — Times & Trades BOVA11"),
    ("21/22", "bova11_options_tape_flow.py",     "Option Tape Flow — Agressão por Strike"),
    ("22/22", "bova11_arquivos.py",              "Arquivos — Relatório Markdown + Histórico para IA"),
]

# ═══════════════════════════════════════
# AUTO-DISCOVERY DE DATAS (igual aos outros scripts)
# ═══════════════════════════════════════
def extract_tag_from_filename(filename):
    m = re.search(r'fechamento__([a-zA-Z0-9]+)_\.csv$', filename)
    if m:
        return m.group(1)
    m = re.search(r'fechamento \(([a-zA-Z0-9]+)\)\.csv$', filename)
    if m:
        return m.group(1)
    return None

def tag_to_iso_date(tag):
    """Converte tag tipo '24mar' ou '25posmar' para '2026-03-24' (ano corrente)."""
    meses_num = {
        'jan': '01', 'fev': '02', 'mar': '03', 'abr': '04',
        'mai': '05', 'jun': '06', 'jul': '07', 'ago': '08',
        'set': '09', 'out': '10', 'nov': '11', 'dez': '12'
    }
    # Remove sufixos conhecidos: 'pos' (pós-mercado), 'pre' (pré-mercado)
    normalized = re.sub(r'(pos|pre)([a-z]{3})$', r'\2', tag.lower())
    m = re.match(r'(\d{1,2})([a-z]{3})$', normalized)
    if m:
        from datetime import datetime
        dia = m.group(1).zfill(2)
        mes = meses_num.get(m.group(2), '01')
        ano = str(datetime.now().year)
        return f"{ano}-{mes}-{dia}"
    return tag

def tag_sort_key(tag):
    """Retorna chave cronológica estável para tags tipo 24mar / 25posmar."""
    meses_num = {
        'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4,
        'mai': 5, 'jun': 6, 'jul': 7, 'ago': 8,
        'set': 9, 'out': 10, 'nov': 11, 'dez': 12
    }
    normalized = re.sub(r'(pos|pre)([a-z]{3})$', r'\2', tag.lower())
    m = re.match(r'(\d{1,2})([a-z]{3})$', normalized)
    if not m:
        return (99, 99, tag.lower())
    return (meses_num.get(m.group(2), 99), int(m.group(1)), normalized)

def is_primary_fechamento_file(filename):
    """Aceita apenas CSVs de fechamento principais, excluindo volume e cópias."""
    lower = filename.lower()
    return (
        lower.endswith('.csv')
        and 'fechamento' in lower
        and 'volume' not in lower
        and ' copy' not in lower
    )

def find_missing_deps():
    required = {
        "numpy": [
            "bova11_skew_prediction.py",
            "bova11_oi_stats_light.py",
            "bova11_gravity.py",
            "bova11_bandas_vol.py",
            "bova11_quant_institucional.py",
        ],
        "pandas": [
            "bova11_skew_prediction.py",
            "bova11_oi_stats_light.py",
            "bova11_max_pain.py",
            "bova11_gravity.py",
            "bova11_bandas_vol.py",
            "bova11_quant_institucional.py",
        ],
        "scipy": [
            "bova11_oi_stats_light.py",
            "bova11_gravity.py",
        ],
        "yfinance": [
            "bova11_quant_institucional.py",
            "bova11_bandas_vol.py",
            "bova11_skew_prediction.py",
        ],
    }
    missing = {}
    for pkg, scripts in required.items():
        if importlib.util.find_spec(pkg) is None:
            missing[pkg] = scripts
    return missing

def format_date_label(tag):
    meses = {
        'jan': 'Jan', 'fev': 'Fev', 'mar': 'Mar', 'abr': 'Abr',
        'mai': 'Mai', 'jun': 'Jun', 'jul': 'Jul', 'ago': 'Ago',
        'set': 'Set', 'out': 'Out', 'nov': 'Nov', 'dez': 'Dez'
    }
    normalized = re.sub(r'(pos|pre)([a-z]{3})$', r'\2', tag.lower())
    m = re.match(r'(\d{1,2})([a-z]{3})$', normalized)
    if m:
        dia = m.group(1)
        mes = meses.get(m.group(2), m.group(2).capitalize())
        return f"{dia}/{mes}"
    return tag

def discover_dates():
    fechamento_files = []
    for fpath in glob.glob(os.path.join(DATA_DIR, "venc_*_fechamento__*_.csv")):
        filename = os.path.basename(fpath)
        if not is_primary_fechamento_file(filename):
            continue
        tag = extract_tag_from_filename(filename)
        if tag:
            fechamento_files.append((tag, os.path.getmtime(fpath)))
    for fpath in glob.glob(os.path.join(DATA_DIR, "venc * fechamento (*).csv")):
        filename = os.path.basename(fpath)
        if not is_primary_fechamento_file(filename):
            continue
        tag = extract_tag_from_filename(filename)
        if tag:
            fechamento_files.append((tag, os.path.getmtime(fpath)))

    if not fechamento_files:
        return None, None

    tags_meta = {}
    for tag, mtime in fechamento_files:
        meta = tags_meta.setdefault(tag, {"count": 0, "mtime": 0})
        meta["count"] += 1
        meta["mtime"] = max(meta["mtime"], mtime)

    valid_tags = [tag for tag, meta in tags_meta.items() if meta["count"] >= 2]
    if not valid_tags:
        valid_tags = list(tags_meta.keys())

    tags_sorted = sorted(valid_tags, key=tag_sort_key)
    tag_d  = tags_sorted[-1]
    tag_d1 = tags_sorted[-2] if len(tags_sorted) >= 2 else None
    return tag_d, tag_d1

def discover_times_file():
    """Encontra o arquivo mais recente de Times & Trades do BOVA11."""
    priority_candidates = []
    fallback_candidates = []
    search_dirs = [DATA_DIR]
    downloads = os.path.expanduser("~/Downloads")
    if os.path.isdir(downloads):
        search_dirs.append(downloads)

    patterns = [
        "*times*bova11*.xlsx", "*bova11*times*.xlsx", "*times*.xlsx", "*bova11*.xlsx",
        "*times*bova11*.csv", "*bova11*times*.csv", "*times*.csv",
    ]
    seen = set()
    for base in search_dirs:
        for pattern in patterns:
            for path in glob.glob(os.path.join(base, pattern)):
                if path in seen or not os.path.isfile(path):
                    continue
                seen.add(path)
                name = os.path.basename(path).lower()
                if "times" in name:
                    priority_candidates.append(path)
                elif base == DATA_DIR and "bova11" in name:
                    fallback_candidates.append(path)
    candidates = priority_candidates or fallback_candidates
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)

# ═══════════════════════════════════════
# INPUT DE SPOT
# ═══════════════════════════════════════
def ask_spot(prompt):
    while True:
        raw = input(f"  {prompt}: ").strip().replace(',', '.')
        try:
            val = float(raw)
            if val > 0:
                return val
            print("  ❌ O spot deve ser positivo.")
        except ValueError:
            print("  ❌ Valor inválido. Use formato: 188.50")

def ask_strike_range():
    """Para o skew_history: pede range de strikes (Enter = padrão)."""
    default_min, default_max = 150, 200
    print(f"\n  Range de strikes para o Skew History (padrão: {default_min} a {default_max})")
    try:
        min_str = input(f"    Strike mínimo [{default_min}]: ").strip()
        max_str = input(f"    Strike máximo [{default_max}]: ").strip()
        min_val = int(min_str) if min_str else default_min
        max_val = int(max_str) if max_str else default_max
        return min_val, max_val
    except Exception:
        print("  ⚠️ Entrada inválida, usando padrão.")
        return default_min, default_max

# ═══════════════════════════════════════
# EXECUÇÃO DE SCRIPT
# ═══════════════════════════════════════
def run_script(script_path, stdin_input):
    """Executa um script Python passando stdin_input como entrada."""
    result = subprocess.run(
        [sys.executable, script_path],
        input=stdin_input,
        cwd=DIR,
        text=True,
        capture_output=False,  # mostra output diretamente no terminal
    )
    return result.returncode

def run_script_with_args(script_path, args):
    """Executa um script Python passando argumentos CLI (argparse)."""
    result = subprocess.run(
        [sys.executable, script_path] + args,
        cwd=DIR,
        text=True,
        capture_output=False,
    )
    return result.returncode

# ═══════════════════════════════════════
# GERADOR DO DASHBOARD UNIFICADO (index.html)
# ═══════════════════════════════════════
def build_dashboard_modules(tag_d1, tag_d, data_d1, data_d):
    """Retorna a lista unica de HTMLs expostos no dashboard."""
    str_d1 = data_d1.replace('/', '')
    str_d  = data_d.replace('/', '')

    modules = [
        {
            "title": "Rankings & Overview",
            "file": f"bova11_rankings_pro_{tag_d1}_vs_{tag_d}.html",
            "group": "Hoje",
            "priority": 1,
            "description": "Panorama agregado de OI, IV e volume por vencimento.",
            "quick": True,
        },
        {
            "title": "GEX & MaxPain",
            "file": f"bova11_gex_pro_{tag_d1}_vs_{tag_d}.html",
            "group": "Hoje",
            "priority": 2,
            "description": "Regime gamma, flip e strikes dominantes do dia.",
            "quick": True,
        },
        {
            "title": "Insights Analíticos",
            "file": f"bova11_insights_{str_d1}_vs_{str_d}.html",
            "group": "Hoje",
            "priority": 3,
            "description": "Leitura operacional por vencimento com foco no que mudou.",
            "quick": True,
        },
        {
            "title": "Tape Flow",
            "file": "bova11_tape_flow.html",
            "group": "Hoje",
            "priority": 4,
            "description": "Times & Trades, VWAP, CVD, blocos e corretoras.",
            "quick": True,
        },
        {
            "title": "Option Tape Flow",
            "file": "bova11_options_tape_flow.html",
            "group": "Hoje",
            "priority": 5,
            "description": "Agressão de opções por strike, CVD, VWAP e pressão calls/puts.",
            "quick": True,
        },
        {
            "title": "Trade Score & Bias",
            "file": f"bova11_trade_score_{tag_d1}_vs_{tag_d}.html",
            "group": "Hoje",
            "priority": 6,
            "description": "Score por strike e viés institucional estimado.",
            "quick": False,
        },
        {
            "title": "OI Stats",
            "file": "bova11_oi_stats_light.html",
            "group": "Estrutura",
            "priority": 1,
            "description": "Distribuição estatística do OI e concentração por vencimento.",
            "quick": False,
        },
        {
            "title": "GEX DEX VEX TEX CEX",
            "file": f"bova11_tex_dex_vex_{tag_d1}_vs_{tag_d}.html",
            "group": "Estrutura",
            "priority": 2,
            "description": "Perfis por spot para GEX, DEX, VEX, TEX e CEX com Merton + forward implícito via put-call parity.",
            "quick": False,
        },
        {
            "title": "Max Pain Relevance",
            "file": "bova11_max_pain.html",
            "group": "Estrutura",
            "priority": 3,
            "description": "Força relativa do pinning considerando prazo e OI.",
            "quick": False,
        },
        {
            "title": "Mapa Gravitacional",
            "file": "bova11_gravity_map.html",
            "group": "Estrutura",
            "priority": 4,
            "description": "Centros de massa de OI, GEX e DEX no mesmo painel.",
            "quick": False,
        },
        {
            "title": "Hunter Walls",
            "file": "bova11_hunter_walls.html",
            "group": "Estrutura",
            "priority": 5,
            "description": "Paredes de volume e concentração por strike.",
            "quick": False,
        },
        {
            "title": "Demand Score",
            "file": "bova11_demand_flow.html",
            "group": "Estrutura",
            "priority": 6,
            "description": "Score composto institucional e decomposição por componente.",
            "quick": False,
        },
        {
            "title": "Market Gamma",
            "file": "bova11_market_gamma.html",
            "group": "Estrutura",
            "priority": 7,
            "description": "Curva gamma líquida agregada e por vencimento.",
            "quick": False,
        },
        {
            "title": "Skew & IV History",
            "file": "bova11_skew_history.html",
            "group": "Histórico",
            "priority": 1,
            "description": "Evolução histórica de skew e IV média.",
            "quick": False,
        },
        {
            "title": "Flow History",
            "file": "bova11_flow_history.html",
            "group": "Histórico",
            "priority": 2,
            "description": "Entradas e saídas acumuladas de OI por sessão.",
            "quick": False,
        },
        {
            "title": "GEX Histórico",
            "file": "bova11_gex_history.html",
            "group": "Histórico",
            "priority": 3,
            "description": "Série temporal do gamma exposure agregado.",
            "quick": False,
        },
        {
            "title": "Quant Institucional",
            "file": "bova11_quant_institucional.html",
            "group": "Histórico",
            "priority": 4,
            "description": "Força relativa, momentum e leitura institucional.",
            "quick": False,
        },
        {
            "title": "Bandas de Vol",
            "file": "bova11_bandas_vol.html",
            "group": "Histórico",
            "priority": 5,
            "description": "Vol implícita versus bandas de referência histórica.",
            "quick": False,
        },
        {
            "title": "Histórico de Mercado",
            "file": "bova11_historical_data.html",
            "group": "Histórico",
            "priority": 6,
            "description": "Séries de OI, volume, IV, max pain, GEX e DEX.",
            "quick": False,
        },
        {
            "title": "Convexity",
            "file": f"bova11_convexity_{tag_d1}_vs_{tag_d}.html",
            "group": "Laboratório",
            "priority": 1,
            "description": "Decomposição de gamma, vanna e charm.",
            "quick": False,
        },
        {
            "title": "IV Skew Signal",
            "file": "bova11_skew_prediction.html",
            "group": "Laboratório",
            "priority": 2,
            "description": "Modelo probabilístico por superfície, flow, greeks e regime.",
            "quick": False,
        },
        {
            "title": "Diagnóstico 4D",
            "file": "bova11_diagnostico_4d.html",
            "group": "Laboratório",
            "priority": 3,
            "description": "Leitura cruzada entre GEX, DEX, VEX e TEX.",
            "quick": False,
        },
        {
            "title": "Arquivos",
            "file": "bova11_arquivos.html",
            "group": "Laboratório",
            "priority": 4,
            "description": "Saída auxiliar para revisão e histórico em markdown.",
            "quick": False,
        },
    ]
    return modules


def gerar_index_html(tag_d1, tag_d, data_d1, data_d, update_label):
    """Cria o index.html com o menu lateral para agrupar todos os reports."""
    modules = build_dashboard_modules(tag_d1, tag_d, data_d1, data_d)

    group_order = ["Hoje", "Estrutura", "Histórico", "Laboratório"]
    first_module = modules[0]
    first_file = first_module["file"]
    first_title = html.escape(first_module["title"])
    first_desc = html.escape(first_module["description"])

    menu_sections = []
    for group in group_order:
        group_id = re.sub(r'[^a-z0-9]+', '-', group.lower()).strip('-')
        group_modules = sorted(
            [m for m in modules if m["group"] == group],
            key=lambda item: item["priority"],
        )
        expanded = group == "Hoje"
        items_html = []
        for module in group_modules:
            active_cls = " active" if module["file"] == first_file else ""
            items_html.append(
                f'''
                <button class="menu-item{active_cls}" data-file="{module["file"]}" onclick="loadPage(this.dataset.file)">
                    <span class="mi-title">{html.escape(module["title"])}</span>
                    <span class="mi-desc">{html.escape(module["description"])}</span>
                </button>'''
            )
        menu_sections.append(
            f'''
            <section class="menu-group">
                <button class="group-toggle{" expanded" if expanded else ""}" data-group="{group_id}" onclick="toggleGroup('{group_id}')" aria-expanded="{"true" if expanded else "false"}">
                    <span>{html.escape(group)}</span>
                    <span class="group-count">{len(group_modules)}</span>
                </button>
                <div class="group-items{" expanded" if expanded else ""}" id="group-{group_id}">
                    {"".join(items_html)}
                </div>
            </section>'''
        )

    quick_links = []
    for module in modules:
        if not module["quick"]:
            continue
        active_cls = " active" if module["file"] == first_file else ""
        quick_links.append(
            f'<button class="quick-link{active_cls}" data-file="{module["file"]}" onclick="loadPage(this.dataset.file)">{html.escape(module["title"])}</button>'
        )

    modules_json = json.dumps(modules, ensure_ascii=False)

    html_content = f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOVA11 Dashboard - {data_d1} vs {data_d}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #f3f5f7;
            --surface: #ffffff;
            --surface-2: #f7f9fb;
            --surface-3: #eef3f8;
            --border: #dce2e8;
            --border-strong: #c9d3dd;
            --text: #111827;
            --muted: #5f6b7a;
            --soft: #7a8696;
            --accent: #2563eb;
            --accent-soft: rgba(37, 99, 235, 0.08);
            --danger: #c33c31;
            --shadow: 0 18px 45px rgba(17, 24, 39, 0.06);
            --sidebar-w: 330px;
            --topbar-h: 56px;
            --font: 'Instrument Sans', system-ui, sans-serif;
            --mono: 'JetBrains Mono', monospace;
        }}
        [data-theme="dark"] {{
            --bg: #0f141b;
            --surface: #141b23;
            --surface-2: #19212b;
            --surface-3: #1f2933;
            --border: #273240;
            --border-strong: #334155;
            --text: #e5ebf3;
            --muted: #a3afbf;
            --soft: #7d8a9b;
            --accent: #6ea8ff;
            --accent-soft: rgba(110, 168, 255, 0.12);
            --danger: #f87171;
            --shadow: 0 22px 50px rgba(0, 0, 0, 0.28);
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{ height: 100%; }}
        body {{
            font-family: var(--font);
            background: var(--bg);
            color: var(--text);
            display: flex;
            min-height: 100vh;
            height: 100dvh;
            overflow: hidden;
        }}
        button {{ font: inherit; }}
        .mono {{ font-family: var(--mono); }}
        .topbar {{
            display: none;
            align-items: center;
            gap: 12px;
            min-height: var(--topbar-h);
            padding: 12px 16px;
            background: var(--surface);
            border-bottom: 1px solid var(--border);
            position: relative;
            z-index: 180;
        }}
        .hamburger, .theme-toggle {{
            border: 1px solid var(--border);
            background: var(--surface);
            color: var(--text);
            border-radius: 999px;
            padding: 9px 12px;
            cursor: pointer;
            transition: border-color 0.2s ease, color 0.2s ease, background 0.2s ease;
        }}
        .hamburger:hover, .theme-toggle:hover {{
            border-color: var(--accent);
            color: var(--accent);
        }}
        .topbar-title {{
            min-width: 0;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .topbar-title strong {{
            font-size: 0.98rem;
            line-height: 1.2;
        }}
        .topbar-title span {{
            font-size: 0.72rem;
            color: var(--muted);
            line-height: 1.2;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .overlay {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(15, 23, 42, 0.4);
            backdrop-filter: blur(2px);
            z-index: 190;
        }}
        .overlay.open {{ display: block; }}
        .sidebar {{
            width: var(--sidebar-w);
            background: var(--surface);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            min-height: 100%;
            box-shadow: inset -1px 0 0 rgba(255,255,255,0.02);
        }}
        .brand {{
            padding: 28px 24px 20px;
            border-bottom: 1px solid var(--border);
        }}
        .brand-kicker {{
            font-size: 0.7rem;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: var(--soft);
            margin-bottom: 10px;
            font-family: var(--mono);
        }}
        .brand h1 {{
            font-size: 1.18rem;
            letter-spacing: -0.03em;
            margin-bottom: 6px;
        }}
        .brand p {{
            font-size: 0.88rem;
            color: var(--muted);
            line-height: 1.5;
            max-width: 28ch;
        }}
        .menu {{
            padding: 18px 16px 22px;
            overflow-y: auto;
            flex: 1;
            min-height: 0;
        }}
        .menu-group + .menu-group {{
            margin-top: 10px;
        }}
        .group-toggle {{
            width: 100%;
            border: none;
            background: transparent;
            color: var(--text);
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 10px 10px 12px;
            border-radius: 12px;
            cursor: pointer;
            font-weight: 600;
            letter-spacing: -0.01em;
        }}
        .group-toggle:hover {{
            background: var(--surface-2);
        }}
        .group-toggle::after {{
            content: '+';
            color: var(--muted);
            font-weight: 500;
        }}
        .group-toggle.expanded::after {{
            content: '−';
        }}
        .group-count {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 28px;
            height: 22px;
            padding: 0 8px;
            border-radius: 999px;
            border: 1px solid var(--border);
            color: var(--muted);
            font-size: 0.74rem;
            font-family: var(--mono);
        }}
        .group-items {{
            display: none;
            gap: 6px;
            padding: 6px 0 4px;
        }}
        .group-items.expanded {{
            display: grid;
        }}
        .menu-item {{
            border: 1px solid transparent;
            background: transparent;
            width: 100%;
            text-align: left;
            padding: 12px 14px;
            border-radius: 14px;
            cursor: pointer;
            display: grid;
            gap: 4px;
            color: var(--text);
            transition: background 0.2s ease, border-color 0.2s ease, transform 0.2s ease;
        }}
        .menu-item:hover {{
            background: var(--surface-2);
            border-color: var(--border);
            transform: translateY(-1px);
        }}
        .menu-item.active {{
            background: var(--accent-soft);
            border-color: rgba(37, 99, 235, 0.2);
        }}
        .mi-title {{
            font-size: 0.92rem;
            font-weight: 600;
            letter-spacing: -0.01em;
        }}
        .mi-desc {{
            font-size: 0.76rem;
            line-height: 1.45;
            color: var(--muted);
        }}
        .content {{
            flex: 1;
            min-width: 0;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}
        .content-shell {{
            display: flex;
            flex-direction: column;
            gap: 16px;
            padding: 18px 20px 20px;
            height: 100%;
            min-height: 0;
        }}
        .context-bar {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 18px 20px;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 18px;
            box-shadow: var(--shadow);
        }}
        .context-meta {{
            min-width: 0;
            display: grid;
            gap: 8px;
        }}
        .context-kicker {{
            font-size: 0.72rem;
            font-family: var(--mono);
            letter-spacing: 0.1em;
            text-transform: uppercase;
            color: var(--soft);
        }}
        .context-head {{
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 12px;
        }}
        .context-head h2 {{
            font-size: 1.38rem;
            letter-spacing: -0.04em;
            line-height: 1.1;
        }}
        .context-desc {{
            font-size: 0.92rem;
            color: var(--muted);
            line-height: 1.5;
            max-width: 68ch;
        }}
        .context-time {{
            font-size: 0.76rem;
            color: var(--soft);
            white-space: nowrap;
        }}
        .context-side {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 10px;
            flex-shrink: 0;
        }}
        .update-badge {{
            font-size: 0.72rem;
            color: var(--soft);
            padding: 7px 10px;
            border-radius: 999px;
            background: var(--surface-2);
            border: 1px solid var(--border);
            white-space: nowrap;
        }}
        .quick-switch {{
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding-bottom: 2px;
        }}
        .quick-link {{
            flex: 0 0 auto;
            border: 1px solid var(--border);
            background: var(--surface);
            color: var(--muted);
            border-radius: 999px;
            padding: 10px 14px;
            cursor: pointer;
            transition: border-color 0.2s ease, color 0.2s ease, background 0.2s ease;
        }}
        .quick-link:hover {{
            border-color: var(--accent);
            color: var(--text);
        }}
        .quick-link.active {{
            background: var(--accent-soft);
            border-color: rgba(37, 99, 235, 0.2);
            color: var(--accent);
        }}
        .frame-shell {{
            flex: 1;
            min-height: 0;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 24px;
            overflow: hidden;
            box-shadow: var(--shadow);
            display: flex;
            flex-direction: column;
        }}
        .frame-shell iframe {{
            border: none;
            width: 100%;
            height: 100%;
            flex: 1;
            background: var(--surface);
        }}
        .missing-file {{
            display: none;
            height: 100%;
            padding: 48px 28px;
            color: var(--danger);
            text-align: center;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            gap: 10px;
        }}
        .missing-file h2 {{
            font-size: 1.1rem;
        }}
        .missing-file p {{
            max-width: 42ch;
            color: var(--muted);
            line-height: 1.5;
        }}
        @media (max-width: 980px) {{
            body {{
                flex-direction: column;
            }}
            .topbar {{
                display: flex;
            }}
            .sidebar {{
                position: fixed;
                top: 0;
                left: 0;
                bottom: 0;
                transform: translateX(-100%);
                transition: transform 0.24s ease;
                z-index: 200;
                width: min(92vw, var(--sidebar-w));
                box-shadow: var(--shadow);
            }}
            .sidebar.open {{
                transform: translateX(0);
            }}
            .content {{
                height: calc(100dvh - var(--topbar-h));
            }}
            .content-shell {{
                padding: 14px 14px 16px;
            }}
            .context-bar {{
                flex-direction: column;
                align-items: stretch;
            }}
            .context-side {{
                align-items: stretch;
            }}
            .update-badge {{
                white-space: normal;
            }}
        }}
        @media (max-width: 720px) {{
            .context-head h2 {{
                font-size: 1.12rem;
            }}
            .quick-link {{
                padding: 9px 12px;
                font-size: 0.84rem;
            }}
        }}
    </style>
</head>
<body>
    <div class="topbar">
        <button class="hamburger" onclick="toggleSidebar()" aria-label="Abrir navegação">Menu</button>
        <div class="topbar-title">
            <strong id="topbar-title">{first_title}</strong>
            <span id="topbar-desc">{first_desc}</span>
        </div>
        <button class="theme-toggle" onclick="toggleTheme()" title="Alternar tema">Tema</button>
    </div>

    <div class="overlay" id="overlay" onclick="closeSidebar()"></div>

    <aside class="sidebar" id="sidebar">
        <div class="brand">
            <div class="brand-kicker">BOVA11 Analytics</div>
            <h1>Painel quantitativo unificado</h1>
            <p>Leitura rápida do dia, estrutura por vencimento e módulos de apoio organizados por tarefa.</p>
        </div>
        <div class="menu">
            {"".join(menu_sections)}
        </div>
    </aside>

    <main class="content">
        <div class="content-shell">
            <section class="context-bar">
                <div class="context-meta">
                    <div class="context-kicker" id="context-group">Hoje</div>
                    <div class="context-head">
                        <h2 id="context-title">{first_title}</h2>
                        <span class="context-time mono">Atualizado em {update_label}</span>
                    </div>
                    <p class="context-desc" id="context-desc">{first_desc}</p>
                </div>
                <div class="context-side">
                    <div class="update-badge mono" title="Momento em que os dados do dashboard foram gerados">{update_label}</div>
                    <button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()" title="Alternar tema">Tema</button>
                </div>
            </section>

            <div class="quick-switch">
                {"".join(quick_links)}
            </div>

            <section class="frame-shell">
                <iframe id="main-frame" src="{first_file}" onload="checkIframe()"></iframe>
                <div id="error-msg" class="missing-file">
                    <h2>Arquivo não encontrado</h2>
                    <p>Este módulo pode não ter sido processado ainda ou o nome do arquivo gerado está incorreto.</p>
                </div>
            </section>
        </div>
    </main>

    <script>
        const MODULES = {modules_json};
        const MODULE_MAP = Object.fromEntries(MODULES.map(module => [module.file, module]));

        function setActiveModule(url) {{
            document.querySelectorAll('.menu-item, .quick-link').forEach(el => {{
                el.classList.toggle('active', el.dataset.file === url);
            }});
        }}

        function updateContext(url) {{
            const module = MODULE_MAP[url];
            if (!module) return;
            document.title = `BOVA11 - ${{module.title}}`;
            document.getElementById('context-group').textContent = module.group;
            document.getElementById('context-title').textContent = module.title;
            document.getElementById('context-desc').textContent = module.description;
            document.getElementById('topbar-title').textContent = module.title;
            document.getElementById('topbar-desc').textContent = module.description;
        }}

        function loadPage(url) {{
            const frame = document.getElementById('main-frame');
            document.getElementById('error-msg').style.display = 'none';
            frame.style.display = 'block';
            frame.src = url;
            setActiveModule(url);
            updateContext(url);
            closeSidebar();
        }}

        function toggleGroup(groupId) {{
            const toggle = document.querySelector(`.group-toggle[data-group="${{groupId}}"]`);
            const group = document.getElementById(`group-${{groupId}}`);
            if (!toggle || !group) return;
            const expanded = group.classList.toggle('expanded');
            toggle.classList.toggle('expanded', expanded);
            toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        }}

        function toggleSidebar() {{
            const sidebar = document.getElementById('sidebar');
            const open = sidebar.classList.toggle('open');
            document.getElementById('overlay').classList.toggle('open', open);
        }}

        function closeSidebar() {{
            document.getElementById('sidebar').classList.remove('open');
            document.getElementById('overlay').classList.remove('open');
        }}

        function checkIframe() {{
            const iframe = document.getElementById('main-frame');
            try {{
                const doc = iframe.contentDocument || iframe.contentWindow.document;
                if (!doc || !doc.body || doc.body.innerHTML.trim() === '') {{
                    iframe.style.display = 'none';
                    document.getElementById('error-msg').style.display = 'flex';
                    return;
                }}
                doc.querySelectorAll('#theme-toggle, #theme-btn, button.theme-toggle, button.theme-btn').forEach(btn => {{
                    btn.style.display = 'none';
                }});
            }} catch(e) {{
                // Ignorar quando o contexto não permitir inspeção do iframe.
            }}
        }}

        function refreshCurrentFrame() {{
            const iframe = document.getElementById('main-frame');
            if (!iframe || iframe.style.display === 'none') return;
            const currentSrc = iframe.getAttribute('src');
            if (!currentSrc) return;
            iframe.setAttribute('src', currentSrc);
        }}

        (function applySavedTheme() {{
            try {{
                const saved = localStorage.getItem('bova11-theme');
                if (saved === 'dark') {{
                    document.documentElement.setAttribute('data-theme', 'dark');
                }}
            }} catch(e) {{}}
        }})();

        function toggleTheme() {{
            const html = document.documentElement;
            const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
            if (next === 'light') html.removeAttribute('data-theme');
            else html.setAttribute('data-theme', 'dark');
            try {{ localStorage.setItem('bova11-theme', next); }} catch(e) {{}}
            refreshCurrentFrame();
        }}
    </script>
</body>
</html>'''

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    index_path = os.path.join(OUTPUT_DIR, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    return index_path

# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════
def main():
    print()
    print("=" * 65)
    print("  BOVA11 RUNNER — Orquestrador de Scripts")
    print("=" * 65)

    # 1) Auto-detect de datas
    print("\n  🔍 Detectando datas nos arquivos CSV...")
    TAG_D, TAG_D1 = discover_dates()

    if not TAG_D:
        print("  ❌ Nenhum arquivo de fechamento encontrado no diretório.")
        sys.exit(1)

    DATA_D  = format_date_label(TAG_D)
    DATA_D1 = format_date_label(TAG_D1) if TAG_D1 else "N/A"

    print(f"  📅 D-1 : {TAG_D1 or 'N/A'} ({DATA_D1})")
    print(f"  📅 D   : {TAG_D} ({DATA_D})")

    if not TAG_D1:
        print("\n  ⚠️  Apenas uma data encontrada. São necessários D-1 e D.")
        sys.exit(1)

    missing_deps = find_missing_deps()
    if missing_deps:
        print()
        print("  ⚠️  Dependências Python ausentes neste interpretador:")
        for pkg, scripts in sorted(missing_deps.items()):
            print(f"     - {pkg}: {', '.join(scripts)}")
        print("  💡 Instale no mesmo Python do runner:")
        print(f"     {sys.executable} -m pip install numpy pandas scipy yfinance")

    # 2) Coletar todos os inputs de uma vez
    print()
    print("=" * 65)
    print("  CONFIGURAÇÃO DE SPOT")
    print("=" * 65)
    SPOT_D1 = ask_spot(f"Spot de D-1 ({DATA_D1})")
    SPOT_D  = ask_spot(f"Spot de D   ({DATA_D})")

    strike_min, strike_max = ask_strike_range()

    print()
    print("=" * 65)
    print(f"  Configuração confirmada:")
    print(f"    D-1 : {DATA_D1}  |  Spot D-1 : {SPOT_D1:.2f}")
    print(f"    D   : {DATA_D}   |  Spot D   : {SPOT_D:.2f}")
    print(f"    Strikes Skew   : {strike_min} – {strike_max}")
    print("=" * 65)

    upsert_spot_history(SPOT_HISTORY_FILE, ref_date=tag_to_iso_date(TAG_D1), ref_tag=TAG_D1, spot=SPOT_D1)
    upsert_spot_history(SPOT_HISTORY_FILE, ref_date=tag_to_iso_date(TAG_D), ref_tag=TAG_D, spot=SPOT_D)
    print(f"    Spot history   : {SPOT_HISTORY_FILE}")
    print("=" * 65)

    # 3) Definir stdin de cada script
    #
    # bova11_auto.py          → pede: spot D (único valor)
    # bova11_insights_auto.py → pede: spot D (único valor)
    # bova11_skew_history.py  → pede: strike min (Enter), strike max (Enter)
    # bova11_convexity.py     → pede: spot D (Enter p/ auto), depois spot D-1
    #                           OBS: ask_spot aceita Enter (retorna None) mas
    #                           se None → sys.exit(1), então passamos spot_D
    # bova11_trade_score_james.py → pede: spot D-1, depois spot D
    #
    stdin_map = {
        "bova11_auto.py":                   f"{SPOT_D}\n",
        "bova11_insights_auto.py":          f"{SPOT_D}\n",
        "bova11_skew_history.py":           f"{strike_min}\n{strike_max}\n",
        "bova11_convexity.py":              f"{SPOT_D}\n{SPOT_D1}\n",
        "bova11_trade_score_james.py":      f"{SPOT_D1}\n{SPOT_D}\n",
        "bova11_quant_institucional.py":    "",  # autônomo, sem input
        "bova11_tex_dex_vex.py":            f"{SPOT_D}\n{SPOT_D1}\n",
        "bova11_flow_history.py":           "",  # autônomo, sem input
        "bova11_skew_prediction.py":        "",  # autônomo, sem input
    }

    # Scripts que usam argparse CLI (não stdin)
    ref_date = tag_to_iso_date(TAG_D)
    times_file = discover_times_file()
    if times_file:
        print(f"    Times & Trades: {times_file}")
    else:
        print("    Times & Trades: não encontrado (Tape Flow gerará placeholder)")

    current_modules = build_dashboard_modules(TAG_D1, TAG_D, DATA_D1, DATA_D)
    current_html_files = [
        module["file"]
        for module in current_modules
        if module.get("file") and module["file"] != "bova11_arquivos.html"
    ]
    args_map = {
        "bova11_oi_stats_light.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_oi_stats_light.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
        ],
        "bova11_max_pain.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_max_pain.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot={SPOT_D}",
        ],
        "bova11_gravity.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_gravity_map.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot={SPOT_D}",
        ],
        "bova11_hunter_walls.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_hunter_walls.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot={SPOT_D}",
        ],
        "bova11_gex_history.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_gex_history.html')}",
            f"--ref-date={ref_date}",
            f"--spot={SPOT_D}",
            f"--spot-history-file={SPOT_HISTORY_FILE}",
        ],
        "bova11_historical_data.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_historical_data.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot-history-file={SPOT_HISTORY_FILE}",
        ],
        "bova11_market_gamma.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_market_gamma.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot={SPOT_D}",
            f"--spot-history-file={SPOT_HISTORY_FILE}",
        ],
        "bova11_diagnostico_4d.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_diagnostico_4d.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot-d={SPOT_D}",
            f"--spot-d1={SPOT_D1}",
        ],
        "bova11_bandas_vol.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_bandas_vol.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot={SPOT_D}",
        ],
        "bova11_demand_flow.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_demand_flow.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot-d={SPOT_D}",
            f"--spot-d1={SPOT_D1}",
        ],
        "bova11_tape_flow.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_tape_flow.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot={SPOT_D}",
            "--min-qty=1000",
        ] + ([f"--times-file={times_file}"] if times_file else []),
        "bova11_options_tape_flow.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_options_tape_flow.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--spot={SPOT_D}",
            "--min-qty=1000",
        ],
        "bova11_skew_prediction.py": [
            f"--data-dir={DATA_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_skew_prediction.html')}",
            f"--spot-history-file={SPOT_HISTORY_FILE}",
            "--bull-threshold=-0.04",
            "--bear-threshold=0.35",
            "--hybrid-enabled=on",
            "--hybrid-bear-delta-skew=0.15",
            "--hybrid-bull-signal=0.05",
            "--hybrid-precedence=bull-first",
            "--hybrid-bear-fallback=off",
        ],
        "bova11_arquivos.py": [
            f"--data-dir={DATA_DIR}",
            f"--output-dir={OUTPUT_DIR}",
            f"--output={os.path.join(OUTPUT_DIR, 'bova11_arquivos.html')}",
            f"--ref-date={ref_date}",
            f"--ref-tag={TAG_D}",
            f"--ref-tag-d1={TAG_D1}",
            f"--html-files={','.join(current_html_files)}",
            f"--spot-d={SPOT_D}",
            f"--spot-d1={SPOT_D1}",
        ],
    }

    # 4) Rodar cada script em sequência
    errors = []
    for step, script_name, description in SCRIPTS:
        script_path = os.path.join(SCRIPTS_DIR, script_name)

        print()
        print(f"  {'─' * 60}")
        print(f"  🚀 [{step}] {script_name}")
        print(f"       {description}")
        print(f"  {'─' * 60}")

        if not os.path.exists(script_path):
            print(f"  ❌ Arquivo não encontrado: {script_path}")
            errors.append(script_name)
            continue

        if script_name in args_map:
            returncode = run_script_with_args(script_path, args_map[script_name])
        else:
            stdin_input = stdin_map.get(script_name, "")
            returncode = run_script(script_path, stdin_input)

        if returncode == 0:
            print(f"\n  ✅ [{step}] {script_name} — CONCLUÍDO")
        else:
            print(f"\n  ❌ [{step}] {script_name} — ERRO (código {returncode})")
            errors.append(script_name)

    # 5) Resumo final
    from datetime import datetime
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    update_label = generated_at if not errors else f"{generated_at} · atualização parcial"
    index_path = gerar_index_html(TAG_D1, TAG_D, DATA_D1, DATA_D, update_label)

    print()
    print("=" * 65)
    if not errors:
        print("  ✅ TODOS OS SCRIPTS CONCLUÍDOS COM SUCESSO!")
        print()
        print("  🌟 DASHBOARD UNIFICADO GERADO!")
        print(f"  ➡️ Abra o arquivo local: ./output/index.html")
        print("=" * 65)
        print()
        resp = input("  🌐 Deseja atualizar o Dashboard na internet para a galera ver? (s/n): ")
        if resp.strip().lower() in ('s', 'sim', 'y', 'yes'):
            print("     Enviando para o GitHub Pages...")
            import subprocess
            from datetime import datetime
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                subprocess.run(["git", "add", "."], cwd=OUTPUT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "commit", "-m", f"Auto-update: {now_str}"], cwd=OUTPUT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "push", "origin", "main"], cwd=OUTPUT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("     ✅ Publicado com sucesso!")
                print("     🌐 Link: https://Leocarneiroo.github.io/bova11-dashboard/")
                print("     (A atualização pode levar ~1 minuto para aparecer)")
            except Exception as e:
                print(f"     ❌ Erro ao publicar: {e}")
    else:
        print(f"  ⚠️  CONCLUÍDO COM ERROS NOS SCRIPTS: {', '.join(errors)}")
        print(f"  🌟 DASHBOARD UNIFICADO ATUALIZADO MESMO ASSIM!")
        print(f"  ➡️ Abra o arquivo local: ./output/index.html")
        print("=" * 65)

if __name__ == '__main__':
    main()
