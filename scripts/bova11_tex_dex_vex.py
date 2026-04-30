#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 GEX / DEX / VEX / TEX / CEX — Perfis por Spot
=====================================================
Calcula e visualiza perfis agregados por spot simulado para
Gamma, Delta, Vanna, Theta e Charm a partir dos dados de OI
e IV dos CSVs B3, agora com modelo de Merton (r, q) por vencimento
via forward implícito de put-call parity, mantendo a página clássica
de GEX separada.

GEX = OI × Gamma × Spot² / 100
DEX = perfil BS por spot (retail e FM)
VEX = OI × Vanna
TEX = OI × Theta diário (perspectiva do vendedor)
CEX = OI × Charm / 252

Uso:
  python3 bova11_tex_dex_vex.py
  (solicita spot D e spot D-1 via stdin)
"""

import os, re, glob, sys, json, math
from datetime import datetime

from bova11_shared import (
    bs_charm_delta,
    bs_delta,
    bs_gamma,
    bs_theta_per_day,
    bs_vanna,
    expiry_label_to_iso,
    normalize_tag,
    tag_to_iso,
)

_BASEDIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(_BASEDIR, '..', 'data')
OUTPUT_DIR = os.path.join(_BASEDIR, '..', 'output')

# Nota de revisão futura:
# - taxa default do modelo fica parametrizável por env var para facilitar replay
# - o módulo foi validado com:
#     python3 -m unittest tests/test_bova11_tex_dex_vex.py
#     printf '182\n181\n' | python3 scripts/bova11_tex_dex_vex.py
DEFAULT_RATE = float(os.environ.get("BOVA11_RISK_FREE_RATE", "0.1375"))

# Nota de revisão futura:
# - forward implícito usa filtro mínimo de 10k no OI total do strike
# - objetivo: evitar contaminar F com linhas ilíquidas / bid-ask ruim
FORWARD_MIN_TOTAL_OI = 10_000.0

# ═══════════════════════════════════════
# AUTO-DISCOVERY DE DATAS
# ═══════════════════════════════════════

def parse_vencimento_for_sort(venc_str):
    """Extrai (mês, dia) de um vencimento para ordenação cronológica.
    Ex: "13 mar W2" -> (3, 13), "2 abr W1" -> (4, 2)
    """
    m = re.match(r'(\d{1,2})\s+([a-z]{3})', venc_str.lower())
    if m:
        meses = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
                'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
        day = int(m.group(1))
        month = meses.get(m.group(2), 1)
        return (month, day)
    return (99, 99)

def parse_date_tag_for_sort(tag):
    """Extrai (mês, dia) de uma tag de data como 2abr ou 25posmar."""
    normalized = re.sub(r'(pos|pre)([a-z]{3})$', r'\2', tag.lower())
    m = re.match(r'(\d{1,2})([a-z]{3})$', normalized)
    if m:
        meses = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
                 'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
        return (meses.get(m.group(2), 99), int(m.group(1)))
    return (99, 99)

def extract_tag_from_filename(filename):
    """Extrai a tag de data do nome do arquivo (ex: '26fev')."""
    m = re.search(r'fechamento__([a-zA-Z0-9]+)_\.csv$', filename)
    if m:
        return m.group(1)
    m = re.search(r'fechamento \(([a-zA-Z0-9]+)\)\.csv$', filename)
    if m:
        return m.group(1)
    return None

def format_date_label(tag):
    """Formata tag como '26fev' para label '26/Fev'."""
    meses = {
        'jan': 'Jan', 'fev': 'Fev', 'mar': 'Mar', 'abr': 'Abr',
        'mai': 'Mai', 'jun': 'Jun', 'jul': 'Jul', 'ago': 'Ago',
        'set': 'Set', 'out': 'Out', 'nov': 'Nov', 'dez': 'Dez'
    }
    m = re.match(r'(\d{1,2})([a-z]{3})$', tag.lower())
    if m:
        dia = m.group(1)
        mes = meses.get(m.group(2), m.group(2).capitalize())
        return f"{dia}/{mes}"
    return tag

def is_primary_fechamento_file(filename):
    lower = filename.lower()
    return (
        lower.endswith('.csv')
        and 'fechamento' in lower
        and 'volume' not in lower
        and ' copy' not in lower
    )

def discover_dates():
    """Descobre as tags D-1 e D a partir dos arquivos de fechamento."""
    fechamento_files = []

    for fpath in glob.glob(os.path.join(DATA_DIR, "venc_*_fechamento__*_.csv")):
        filename = os.path.basename(fpath)
        if not is_primary_fechamento_file(filename):
            continue
        tag = extract_tag_from_filename(filename)
        if tag:
            mtime = os.path.getmtime(fpath)
            fechamento_files.append((tag, mtime))

    for fpath in glob.glob(os.path.join(DATA_DIR, "venc * fechamento (*).csv")):
        filename = os.path.basename(fpath)
        if not is_primary_fechamento_file(filename):
            continue
        tag = extract_tag_from_filename(filename)
        if tag:
            mtime = os.path.getmtime(fpath)
            fechamento_files.append((tag, mtime))

    if not fechamento_files:
        print("  ❌ Nenhum arquivo de fechamento encontrado.")
        sys.exit(1)

    tags_meta = {}
    for tag, mtime in fechamento_files:
        meta = tags_meta.setdefault(tag, {"count": 0, "mtime": 0})
        meta["count"] += 1
        meta["mtime"] = max(meta["mtime"], mtime)

    valid_tags = [tag for tag, meta in tags_meta.items() if meta["count"] >= 2]
    if not valid_tags:
        valid_tags = list(tags_meta.keys())

    tags_sorted = sorted(valid_tags, key=parse_date_tag_for_sort)

    if len(tags_sorted) == 1:
        tag_d   = tags_sorted[-1]
        tag_d1  = None
    else:
        tag_d   = tags_sorted[-1]
        tag_d1  = tags_sorted[-2]

    return tag_d, tag_d1

def ask_spot(label):
    """Solicita o spot price ao usuário."""
    while True:
        raw = input(f"  Spot {label}: ").strip().replace(',', '.')
        try:
            val = float(raw)
            if val > 0:
                return val
            print("  ❌ Deve ser positivo.")
        except ValueError:
            print("  ❌ Valor inválido. Ex: 188.50")

def discover_expirations(tag):
    """Retorna dict {nome_vencimento: filepath} para uma tag de data."""
    found = {}
    for fp in sorted(glob.glob(os.path.join(DATA_DIR, f"venc_*_fechamento__{tag}_.csv"))):
        m = re.match(r'venc_(.+?)_fechamento__', os.path.basename(fp))
        if m:
            found[m.group(1).replace('_', ' ')] = fp
    for fp in sorted(glob.glob(os.path.join(DATA_DIR, f"venc * fechamento ({tag}).csv"))):
        m = re.match(r'venc (.+) fechamento \(', os.path.basename(fp))
        if m and m.group(1) not in found:
            found[m.group(1)] = fp
    return found

# ═══════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════

def parse_num(s):
    """Parser robusto para números em locale BR (k, M, %, vírgula, ponto)."""
    s = str(s).strip().replace('\r', '')
    if s in ('-', '--', ''):
        return None
    mult = 1
    if s.upper().endswith('M'):
        mult = 1e6
        s = s[:-1]
    elif s.upper().endswith('K'):
        mult = 1e3
        s = s[:-1]
    s = s.replace('%', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        parts = s.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
    try:
        return float(s) * mult
    except:
        return None

def parse_csv(fp):
    """Lê CSV B3 23-colunas (fechamento com Greeks)."""
    with open(fp, 'r', encoding='latin-1') as f:
        lines = f.readlines()
    results = []
    for line in lines[1:]:
        p = line.strip().replace('\r', '').split(';')
        if len(p) < 23:
            continue
        strike = parse_num(p[11])
        if strike is None:
            continue

        # Parse IVs (keeping as None if missing)
        c_iv_raw = p[7].strip().replace('%', '')
        p_iv_raw = p[15].strip().replace('%', '')
        c_iv = parse_num(c_iv_raw) if c_iv_raw not in ('-', '', '--') else None
        p_iv = parse_num(p_iv_raw) if p_iv_raw not in ('-', '', '--') else None

        results.append({
            'strike':  strike,
            'c_oi':    parse_num(p[2]) or 0.0,
            'c_delta': parse_num(p[3]) or 0.0,
            'c_theta': parse_num(p[5]) or 0.0,
            'c_vega':  parse_num(p[6]) or 0.0,
            'c_iv':    c_iv,
            'c_bid':   parse_num(p[9]),
            'c_ask':   parse_num(p[10]),
            'p_oi':    parse_num(p[20]) or 0.0,
            'p_delta': parse_num(p[19]) or 0.0,  # Negative by convention
            'p_theta': parse_num(p[17]) or 0.0,
            'p_vega':  parse_num(p[16]) or 0.0,
            'p_iv':    p_iv,
            'p_bid':   parse_num(p[12]),
            'p_ask':   parse_num(p[13]),
        })
    return results

# ═══════════════════════════════════════
# TEX / DEX / VEX CALCULATION
# ═══════════════════════════════════════

def _resolve_session_and_expiry_t(venc_name, tag_d):
    """Calcula T anualizado com piso de 1 dia útil aproximado."""
    session_iso = tag_to_iso(normalize_tag(tag_d), year=datetime.now().year)
    expiry_iso = expiry_label_to_iso(venc_name, year=datetime.now().year)
    if not session_iso or not expiry_iso:
        return 1.0 / 365.0
    try:
        session_date = datetime.strptime(session_iso, "%Y-%m-%d").date()
        expiry_date = datetime.strptime(expiry_iso, "%Y-%m-%d").date()
        return max((expiry_date - session_date).days / 365.0, 1.0 / 365.0)
    except Exception:
        return 1.0 / 365.0


PROFILE_VALUE_KEYS = [
    'gex_call', 'gex_put', 'gex_net',
    'dex_retail_call', 'dex_retail_put', 'dex_retail_net',
    'dex_fm_call', 'dex_fm_put', 'dex_fm_net',
    'vex_call', 'vex_put', 'vex_net',
    'tex_call', 'tex_put', 'tex_net',
    'cex_call', 'cex_put', 'cex_net',
]


def _sigma_from_iv(iv):
    if iv is None:
        return None
    try:
        iv_val = float(iv)
    except Exception:
        return None
    return (iv_val / 100.0) if iv_val > 0 else None


def _mid_from_bid_ask(bid, ask):
    if bid is None or ask is None:
        return None
    try:
        bid_val = float(bid)
        ask_val = float(ask)
    except Exception:
        return None
    if bid_val <= 0 or ask_val <= 0:
        return None
    return (bid_val + ask_val) / 2.0


def derive_forward_from_rows(rows, t_years, rate=DEFAULT_RATE, min_total_oi=FORWARD_MIN_TOTAL_OI):
    """Deriva forward implícito via put-call parity, ponderado por OI."""
    if t_years <= 0:
        return None

    forwards = []
    weights = []
    carry = math.exp(float(rate) * float(t_years))

    for row in rows:
        strike = row.get('strike')
        call_mid = _mid_from_bid_ask(row.get('c_bid'), row.get('c_ask'))
        put_mid = _mid_from_bid_ask(row.get('p_bid'), row.get('p_ask'))
        total_oi = float(row.get('c_oi') or 0.0) + float(row.get('p_oi') or 0.0)

        # Revisitar se precisarmos abrir mais o universo:
        # hoje só usamos strikes com liquidez mínima razoável.
        if strike is None or call_mid is None or put_mid is None or total_oi < float(min_total_oi):
            continue

        try:
            forward = float(strike) + ((call_mid - put_mid) * carry)
        except Exception:
            continue

        if not math.isfinite(forward) or forward <= 0:
            continue

        forwards.append(forward)
        weights.append(total_oi)

    if not forwards or not weights or sum(weights) <= 0:
        return None

    return sum(fwd * wt for fwd, wt in zip(forwards, weights)) / sum(weights)


def resolve_pricing_context(rows, venc_name, tag_d, spot_ref, rate=DEFAULT_RATE):
    """Resolve T, forward implícito e q contínuo por vencimento."""
    t_years = _resolve_session_and_expiry_t(venc_name, tag_d)
    try:
        spot_base = float(spot_ref)
    except Exception:
        spot_base = 0.0

    forward = derive_forward_from_rows(rows, t_years, rate=rate)
    q = 0.0
    if (
        spot_base > 0
        and forward is not None
        and forward > 0
        and t_years > 0
    ):
        try:
            q = float(rate) - (math.log(float(forward) / spot_base) / float(t_years))
        except Exception:
            q = 0.0

    # Revisitar se o curto prazo ficar muito ruidoso:
    # - se bid/ask não ajudar, forward cai no fallback
    # - q=0 preserva estabilidade operacional quando PCP falha
    # - vencimentos muito curtos podem produzir q anualizado estranho por ruído
    #   de microestrutura, então este continua sendo o ponto mais sensível
    #   do modelo.

    return {
        't_years': t_years,
        'forward': forward,
        'q': q,
        'rate': float(rate),
        'spot_ref': spot_base,
    }


def build_spot_grid(rows, spot_ref):
    """Monta eixo de spot simulado com passo de 0.5 usando strikes atuais."""
    strikes = [float(r['strike']) for r in rows if r.get('strike') is not None]
    spot_ref = float(spot_ref)
    if strikes:
        lower = min(min(strikes), spot_ref)
        upper = max(max(strikes), spot_ref)
    else:
        lower = spot_ref * 0.85
        upper = spot_ref * 1.15

    padding = max((upper - lower) * 0.10, 5.0)
    start = max(1.0, math.floor((lower - padding) * 2.0) / 2.0)
    end = math.ceil((upper + padding) * 2.0) / 2.0
    steps = max(int(round((end - start) / 0.5)), 1)
    return [round(start + (i * 0.5), 1) for i in range(steps + 1)]


def _empty_spot_row(spot):
    row = {'spot': round(float(spot), 1)}
    for key in PROFILE_VALUE_KEYS:
        row[key] = 0.0
    return row


def build_unified_spot_profile(rows, venc_name, tag_d, spot_range, spot_ref=None, rate=DEFAULT_RATE):
    """Calcula GEX/DEX/VEX/TEX/CEX por spot simulado para um vencimento."""
    spot_ref = float(spot_ref) if spot_ref is not None else float(spot_range[0] if spot_range else 0.0)
    pricing_ctx = resolve_pricing_context(rows, venc_name, tag_d, spot_ref, rate=rate)
    t_years = pricing_ctx['t_years']
    q = pricing_ctx['q']
    profile = []

    for spot_sim in spot_range:
        spot_sim = float(spot_sim)
        row = _empty_spot_row(spot_sim)

        # Mantemos a escala legada do GEX desta aba para continuidade visual/histórica.
        # Só mudou o gamma de origem: agora ele é recalculado no modelo de Merton.
        gex_factor = (spot_sim ** 2) / 100.0

        for r in rows:
            strike = float(r['strike'])
            c_oi = float(r.get('c_oi') or 0.0)
            p_oi = float(r.get('p_oi') or 0.0)
            c_sigma = _sigma_from_iv(r.get('c_iv'))
            p_sigma = _sigma_from_iv(r.get('p_iv'))

            if c_sigma:
                c_gamma = bs_gamma(spot=spot_sim, strike=strike, sigma=c_sigma, t=t_years, r=rate, q=q) or 0.0
                c_delta = bs_delta(spot=spot_sim, strike=strike, sigma=c_sigma, t=t_years, option_type='call', r=rate, q=q) or 0.0
                c_vanna = bs_vanna(spot=spot_sim, strike=strike, sigma=c_sigma, t=t_years, r=rate, q=q) or 0.0
                c_theta = bs_theta_per_day(spot=spot_sim, strike=strike, sigma=c_sigma, t=t_years, option_type='call', r=rate, q=q) or 0.0
                c_charm_tau = -(bs_charm_delta(spot=spot_sim, strike=strike, sigma=c_sigma, t=t_years, r=rate, q=q, option_type='call') or 0.0)

                row['gex_call'] += c_gamma * c_oi * gex_factor
                row['dex_retail_call'] += c_delta * c_oi
                row['dex_fm_call'] += -c_delta * c_oi
                row['vex_call'] += c_vanna * c_oi
                row['tex_call'] += (-c_theta) * c_oi * 100.0
                row['cex_call'] += (c_charm_tau / 252.0) * c_oi

            if p_sigma:
                p_gamma = bs_gamma(spot=spot_sim, strike=strike, sigma=p_sigma, t=t_years, r=rate, q=q) or 0.0
                p_delta = bs_delta(spot=spot_sim, strike=strike, sigma=p_sigma, t=t_years, option_type='put', r=rate, q=q) or 0.0
                p_vanna = bs_vanna(spot=spot_sim, strike=strike, sigma=p_sigma, t=t_years, r=rate, q=q) or 0.0
                p_theta = bs_theta_per_day(spot=spot_sim, strike=strike, sigma=p_sigma, t=t_years, option_type='put', r=rate, q=q) or 0.0
                p_charm_tau = -(bs_charm_delta(spot=spot_sim, strike=strike, sigma=p_sigma, t=t_years, r=rate, q=q, option_type='put') or 0.0)

                row['gex_put'] += -p_gamma * p_oi * gex_factor
                row['dex_retail_put'] += p_delta * p_oi
                row['dex_fm_put'] += -p_delta * p_oi
                row['vex_put'] += p_vanna * p_oi
                row['tex_put'] += (-p_theta) * p_oi * 100.0
                row['cex_put'] += (p_charm_tau / 252.0) * p_oi

        row['gex_net'] = row['gex_call'] + row['gex_put']
        row['dex_retail_net'] = row['dex_retail_call'] + row['dex_retail_put']
        row['dex_fm_net'] = row['dex_fm_call'] + row['dex_fm_put']
        row['vex_net'] = row['vex_call'] + row['vex_put']
        row['tex_net'] = row['tex_call'] + row['tex_put']
        row['cex_net'] = row['cex_call'] + row['cex_put']
        profile.append(row)

    return profile


def _metric_profile(profile, prefix):
    return [
        {
            'spot': row['spot'],
            f'{prefix}_call': row[f'{prefix}_call'],
            f'{prefix}_put': row[f'{prefix}_put'],
            f'{prefix}_net': row[f'{prefix}_net'],
        }
        for row in profile
    ]


def build_gex_profile(rows, venc_name, tag_d, spot_range, spot_ref=None, rate=DEFAULT_RATE):
    return _metric_profile(build_unified_spot_profile(rows, venc_name, tag_d, spot_range, spot_ref=spot_ref, rate=rate), 'gex')


def build_dex_profiles(rows, venc_name, tag_d, spot_range, spot_ref=None, rate=DEFAULT_RATE):
    profile = build_unified_spot_profile(rows, venc_name, tag_d, spot_range, spot_ref=spot_ref, rate=rate)
    retail = [
        {'spot': row['spot'], 'dex_call': row['dex_retail_call'], 'dex_put': row['dex_retail_put'], 'dex_net': row['dex_retail_net']}
        for row in profile
    ]
    fm = [
        {'spot': row['spot'], 'dex_call': row['dex_fm_call'], 'dex_put': row['dex_fm_put'], 'dex_net': row['dex_fm_net']}
        for row in profile
    ]
    return retail, fm


def build_vex_profile(rows, venc_name, tag_d, spot_range, spot_ref=None, rate=DEFAULT_RATE):
    return _metric_profile(build_unified_spot_profile(rows, venc_name, tag_d, spot_range, spot_ref=spot_ref, rate=rate), 'vex')


def build_tex_profile(rows, venc_name, tag_d, spot_range, spot_ref=None, rate=DEFAULT_RATE):
    return _metric_profile(build_unified_spot_profile(rows, venc_name, tag_d, spot_range, spot_ref=spot_ref, rate=rate), 'tex')


def build_cex_profile(rows, venc_name, tag_d, spot_range, spot_ref=None, rate=DEFAULT_RATE):
    return _metric_profile(build_unified_spot_profile(rows, venc_name, tag_d, spot_range, spot_ref=spot_ref, rate=rate), 'cex')


def aggregate_spot_profiles(per_exp_profiles):
    """Agrega perfis por spot entre múltiplos vencimentos."""
    agg = {}
    for profile in per_exp_profiles:
        for row in profile:
            spot = round(float(row['spot']), 1)
            if spot not in agg:
                agg[spot] = _empty_spot_row(spot)
            for key in PROFILE_VALUE_KEYS:
                agg[spot][key] += float(row.get(key, 0.0))
    return [agg[s] for s in sorted(agg.keys())]

def calc_iv_context(rows_d, rows_d1):
    """Compara IV média entre D e D-1, retorna direção e delta por strike."""
    def mean_iv(rows):
        vals = []
        for r in rows:
            if r.get('c_iv') is not None:
                vals.append(r['c_iv'])
            if r.get('p_iv') is not None:
                vals.append(r['p_iv'])
        return sum(vals) / len(vals) if vals else None

    avg_d = mean_iv(rows_d)
    avg_d1 = mean_iv(rows_d1) if rows_d1 else None

    direction = None
    if avg_d is not None and avg_d1 is not None:
        diff = avg_d - avg_d1
        if diff > 0.5:
            direction = 'rising'
        elif diff < -0.5:
            direction = 'falling'
        else:
            direction = 'stable'

    d1_map = {r['strike']: r for r in rows_d1} if rows_d1 else {}
    per_strike = {}
    for r in rows_d:
        s = r['strike']
        r1 = d1_map.get(s)
        call_d = r.get('c_iv')
        call_d1 = r1.get('c_iv') if r1 else None
        put_d = r.get('p_iv')
        put_d1 = r1.get('p_iv') if r1 else None

        delta_c = (call_d - call_d1) if (call_d is not None and call_d1 is not None) else None
        delta_p = (put_d - put_d1) if (put_d is not None and put_d1 is not None) else None

        if delta_c is not None or delta_p is not None:
            vals = [v for v in [delta_c, delta_p] if v is not None]
            per_strike[s] = sum(vals) / len(vals)

    return {
        'avg_iv_d': avg_d,
        'avg_iv_d1': avg_d1,
        'iv_direction': direction,
        'per_strike_delta_iv': per_strike,
    }

# ═══════════════════════════════════════
# HTML GENERATION
# ═══════════════════════════════════════

def fmt_num(v):
    """Formata número com k/M/B suffix, cortando zeros."""
    if v is None or v == 0:
        return '—'
    sign = '-' if v < 0 else ''
    abs_v = abs(v)

    if abs_v >= 1e9:
        return f"{sign}{abs_v/1e9:.1f}B"
    if abs_v >= 1e6:
        return f"{sign}{abs_v/1e6:.1f}M"
    if abs_v >= 1e3:
        return f"{sign}{abs_v/1e3:.0f}k"
    return f"{sign}{abs_v:.0f}"

def generate_html(agg, iv_ctx, tag_d1, tag_d, label_d1, label_d, spot_d1, spot_d, per_exp_data, exp_keys):
    """Gera o HTML completo com perfis unificados por spot."""

    PALETTE = ["#f85149", "#58a6ff", "#3fb950", "#d29922", "#bc8cff", "#ffa657", "#79c0ff"]

    def closest_row(rows, spot_ref):
        if not rows:
            return {'spot': float(spot_ref)}
        return min(rows, key=lambda r: abs(float(r['spot']) - float(spot_ref)))

    def metric_extrema(rows, key):
        if not rows:
            return 0.0, 0.0
        max_row = max(rows, key=lambda r: float(r[key]))
        min_row = min(rows, key=lambda r: float(r[key]))
        return float(max_row['spot']), float(min_row['spot'])

    def serialize_profile_data(data_dict):
        result = {}
        for venc_name, rows in data_dict.items():
            result[venc_name] = [
                {'spot': round(float(r['spot']), 1), **{k: round(float(r[k]), 6) for k in PROFILE_VALUE_KEYS}}
                for r in rows
            ]
        return result

    agg_serialized = [
        {'spot': round(float(r['spot']), 1), **{k: round(float(r[k]), 6) for k in PROFILE_VALUE_KEYS}}
        for r in agg
    ]
    per_exp_serialized = serialize_profile_data(per_exp_data)

    cur_row = closest_row(agg, spot_d)

    gex_now = float(cur_row.get('gex_net', 0.0)) / 1e6
    dex_fm_now = float(cur_row.get('dex_fm_net', 0.0)) / 1e6
    vex_now = float(cur_row.get('vex_net', 0.0)) / 1e6
    tex_now = float(cur_row.get('tex_net', 0.0)) / 1e6
    cex_now = float(cur_row.get('cex_net', 0.0)) / 1e6

    max_gex_spot, min_gex_spot = metric_extrema(agg, 'gex_net')
    max_dex_spot, min_dex_spot = metric_extrema(agg, 'dex_fm_net')
    max_vex_spot, min_vex_spot = metric_extrema(agg, 'vex_net')
    max_tex_spot, min_tex_spot = metric_extrema(agg, 'tex_net')
    max_cex_spot, min_cex_spot = metric_extrema(agg, 'cex_net')

    # IV Context
    avg_iv_d = iv_ctx['avg_iv_d']
    avg_iv_d1 = iv_ctx['avg_iv_d1']
    iv_direction = iv_ctx['iv_direction'] or 'unknown'
    avg_iv_d_fmt = f"{avg_iv_d:.2f}%" if avg_iv_d is not None else "N/A"
    avg_iv_d1_fmt = f"{avg_iv_d1:.2f}%" if avg_iv_d1 is not None else "N/A"
    iv_delta_display = "N/A"

    if avg_iv_d is None or avg_iv_d1 is None:
        iv_color = '#8b949e'
        iv_symbol = '•'
        iv_text = "IV média indisponível para comparação histórica."
        iv_signal = "Contexto de volatilidade insuficiente"
        iv_signal_color = '#8b949e'
    elif iv_direction == 'rising':
        iv_color = '#f85149'  # red
        iv_symbol = '↑'
        iv_text = f"IV subiu de {avg_iv_d1:.2f}% para {avg_iv_d:.2f}% (+{avg_iv_d - avg_iv_d1:.2f}pp)"
        iv_delta_display = f"+{avg_iv_d - avg_iv_d1:.2f}pp"
        iv_signal = "VENDER volatilidade — IV acima do recente"
        iv_signal_color = '#f85149'
    elif iv_direction == 'falling':
        iv_color = '#3fb950'  # green
        iv_symbol = '↓'
        iv_text = f"IV caiu de {avg_iv_d1:.2f}% para {avg_iv_d:.2f}% ({avg_iv_d - avg_iv_d1:.2f}pp)"
        iv_delta_display = f"{avg_iv_d - avg_iv_d1:.2f}pp"
        iv_signal = "COMPRAR volatilidade — IV abaixo do recente"
        iv_signal_color = '#3fb950'
    else:
        iv_color = '#8b949e'  # gray
        iv_symbol = '→'
        iv_text = f"IV estável: {avg_iv_d:.2f}%"
        iv_delta_display = "0.00pp"
        iv_signal = "Sem sinal claro de volatilidade"
        iv_signal_color = '#8b949e'

    html = f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GEX DEX VEX TEX CEX — {label_d1} vs {label_d}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {{
            --bg:   #ffffff; --bg2:  #f6f8fa; --bg3:  #eaeef2;
            --text: #1f2328; --text2: #636c76; --border: #d0d7de;
            --grn:  #1a7f37; --red:  #cf222e; --blu:  #0969da;
            --yel:  #9a6700; --pur:  #8250df;
        }}
        [data-theme="dark"] {{
            --bg:   #0d1117; --bg2:  #161b22; --bg3:  #21262d;
            --text: #c9d1d9; --text2: #8b949e; --border: #30363d;
            --grn:  #3fb950; --red:  #f85149; --blu:  #58a6ff;
            --yel:  #d29922; --pur:  #bc8cff;
        }}

        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 30px;
            line-height: 1.6;
        }}

        header {{
            margin-bottom: 40px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 20px;
        }}

        h1 {{
            font-size: 2rem;
            margin-bottom: 8px;
            color: #fff;
        }}

        .header-meta {{
            font-size: 0.95rem;
            color: var(--text2);
        }}

        .cards-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 40px;
        }}

        .card {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
            text-align: center;
        }}

        .card-label {{
            font-size: 0.85rem;
            color: var(--text2);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .card-value {{
            font-size: 1.5rem;
            font-weight: bold;
        }}

        .chart-section {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 30px;
        }}

        h2 {{
            font-size: 1.3rem;
            margin-bottom: 8px;
            color: #fff;
        }}

        .legend {{
            font-size: 0.9rem;
            color: var(--text2);
            margin-bottom: 16px;
            font-style: italic;
        }}


        .mechanic-box {{
            background: var(--bg3);
            border-left: 4px solid var(--blu);
            border-radius: 6px;
            padding: 16px;
            margin-bottom: 24px;
            font-size: 0.95rem;
            line-height: 1.7;
        }}

        .mechanic-title {{
            font-weight: bold;
            color: var(--blu);
            margin-bottom: 10px;
        }}

        .mechanic-positive {{
            color: var(--grn);
            font-weight: 500;
        }}

        .mechanic-negative {{
            color: var(--red);
            font-weight: 500;
        }}

        canvas {{
            max-height: 300px;
        }}

        .context-box {{
            background: var(--bg2);
            border: 1px solid var(--border);
            border-left: 4px solid;
            border-radius: 8px;
            padding: 24px;
            margin-top: 30px;
        }}

        .iv-summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin: 16px 0;
        }}

        .iv-item {{
            padding: 12px;
            background: var(--bg3);
            border-radius: 6px;
            text-align: center;
            border: 1px solid var(--border);
        }}

        .iv-item-label {{
            font-size: 0.85rem;
            color: var(--text2);
            margin-bottom: 6px;
        }}

        .iv-item-value {{
            font-size: 1.2rem;
            font-weight: bold;
        }}

        .interpretation {{
            margin: 16px 0;
            padding: 12px;
            background: var(--bg3);
            border-radius: 6px;
            border-left: 3px solid var(--blu);
        }}

        .strategy-chips {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            margin-top: 16px;
        }}

        .chip {{
            padding: 8px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 500;
            display: inline-block;
        }}

        .chip-buy {{
            background: rgba(63, 185, 80, 0.2);
            color: var(--grn);
            border: 1px solid var(--grn);
        }}

        .chip-sell {{
            background: rgba(248, 81, 73, 0.2);
            color: var(--red);
            border: 1px solid var(--red);
        }}

        .chip-neutral {{
            background: rgba(88, 166, 255, 0.2);
            color: var(--blu);
            border: 1px solid var(--blu);
        }}

        .exp-tabs {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 24px;
            padding: 16px;
            background: var(--bg2);
            border-radius: 8px;
            border: 1px solid var(--border);
        }}

        .exp-tab {{
            padding: 8px 14px;
            border: 1px solid var(--border);
            background: var(--card);
            border-radius: 8px;
            color: var(--text2);
            cursor: pointer;
            font-weight: 700;
            font-size: 12px;
            transition: all 0.2s;
        }}

        .exp-tab:hover {{
            border-color: var(--text2);
        }}

        .exp-tab.on {{
            color: #fff;
            border-color: transparent;
            box-shadow: 0 2px 8px rgba(0,0,0,.3);
        }}

        .exp-tab.agg {{
            background: linear-gradient(135deg,#1a237e,#283593);
            color: #fff;
            border-color: #3949ab;
        }}
    </style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()" title="Alternar tema" style="position:fixed;top:12px;right:16px;z-index:9999;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:1rem;line-height:1;transition:all 0.2s;">◐</button>
    <header>
        <h1>GEX | DEX | VEX | TEX | CEX — Perfis por Spot</h1>
        <div class="header-meta">
            📅 {label_d1} vs {label_d}  •  Spot: {spot_d1:.2f} → {spot_d:.2f}  •  Modelo Merton + Forward implícito (put-call parity)
        </div>
    </header>

    <div class="exp-tabs" id="expTabs"></div>

    <div class="cards-grid">
        <div class="card">
            <div class="card-label">GEX Atual</div>
            <div class="card-value" style="color: {'var(--grn)' if gex_now >= 0 else 'var(--red)'}">{fmt_num(gex_now)}</div>
        </div>
        <div class="card">
            <div class="card-label">GEX Spot Max+</div>
            <div class="card-value">R${max_gex_spot:.1f}</div>
        </div>
        <div class="card">
            <div class="card-label">GEX Spot Max−</div>
            <div class="card-value">R${min_gex_spot:.1f}</div>
        </div>

        <div class="card">
            <div class="card-label">DEX FM Atual</div>
            <div class="card-value" style="color: {'var(--grn)' if dex_fm_now >= 0 else 'var(--red)'}">{fmt_num(dex_fm_now)}</div>
        </div>
        <div class="card">
            <div class="card-label">DEX FM Spot Max+</div>
            <div class="card-value">R${max_dex_spot:.1f}</div>
        </div>
        <div class="card">
            <div class="card-label">DEX FM Spot Max−</div>
            <div class="card-value">R${min_dex_spot:.1f}</div>
        </div>

        <div class="card">
            <div class="card-label">VEX Atual</div>
            <div class="card-value" style="color: {'var(--grn)' if vex_now >= 0 else 'var(--red)'}">{fmt_num(vex_now)}</div>
        </div>
        <div class="card">
            <div class="card-label">VEX Spot Max+</div>
            <div class="card-value">R${max_vex_spot:.1f}</div>
        </div>
        <div class="card">
            <div class="card-label">VEX Spot Max−</div>
            <div class="card-value">R${min_vex_spot:.1f}</div>
        </div>

        <div class="card">
            <div class="card-label">TEX Atual</div>
            <div class="card-value" style="color: {'var(--grn)' if tex_now >= 0 else 'var(--red)'}">{fmt_num(tex_now)}</div>
        </div>
        <div class="card">
            <div class="card-label">TEX Spot Max+</div>
            <div class="card-value">R${max_tex_spot:.1f}</div>
        </div>
        <div class="card">
            <div class="card-label">TEX Spot Max−</div>
            <div class="card-value">R${min_tex_spot:.1f}</div>
        </div>

        <div class="card">
            <div class="card-label">CEX Atual</div>
            <div class="card-value" style="color: {'var(--grn)' if cex_now >= 0 else 'var(--red)'}">{fmt_num(cex_now)}</div>
        </div>
        <div class="card">
            <div class="card-label">CEX Spot Max+</div>
            <div class="card-value">R${max_cex_spot:.1f}</div>
        </div>
        <div class="card">
            <div class="card-label">CEX Spot Max−</div>
            <div class="card-value">R${min_cex_spot:.1f}</div>
        </div>
    </div>

    <!-- GEX Section -->
    <div class="chart-section">
        <h2>GEX — Gamma Exposure (Spot Simulado)</h2>
        <div class="mechanic-box">
            <div class="mechanic-title">Mecânica do Gamma</div>
            <div>
                <div><span class="mechanic-positive">GEX Positivo (+)</span></div>
                <ul style="margin: 8px 0 12px 20px; color: var(--text2);">
                    <li>Dealers tendem a <strong>estabilizar</strong> o preço com hedge contracíclico</li>
                    <li>Subidas e quedas encontram mais amortecimento mecânico</li>
                    <li>Curva ajuda a enxergar onde o gamma fica mais denso no cenário de spot</li>
                </ul>
                <div><span class="mechanic-negative">GEX Negativo (−)</span></div>
                <ul style="margin: 8px 0 0 20px; color: var(--text2);">
                    <li>Dealers tendem a <strong>amplificar</strong> o movimento com hedge pró-cíclico</li>
                    <li>O perfil por spot mostra onde o regime pode ficar mais instável</li>
                    <li>Complementa a página clássica de GEX por strike, que continua separada</li>
                </ul>
            </div>
        </div>
        <h3 style="margin-top: 20px; margin-bottom: 12px;">GEX por Spot</h3>
        <canvas id="chartGEX" height="250"></canvas>
    </div>

    <!-- DEX Section -->
    <div class="chart-section">
        <h2>DEX — Delta Exposure (Spot Simulado)</h2>
        <div class="legend">Usando a lógica do código anexado: uma visão do comprador e outra do formador de mercado no hedge.</div>
        <div class="mechanic-box">
            <div class="mechanic-title">Mecânica do Delta Hedging</div>
            <div>
                <div><span class="mechanic-positive">DEX FM Positivo (+)</span></div>
                <ul style="margin: 8px 0 12px 20px; color: var(--text2);">
                    <li>O hedge do formador tende a ficar mais <strong>comprado</strong> no cenário mostrado</li>
                    <li>A curva FM é a leitura principal desta aba para pressão mecânica de hedge</li>
                </ul>
                <div><span class="mechanic-negative">DEX FM Negativo (−)</span></div>
                <ul style="margin: 8px 0 0 20px; color: var(--text2);">
                    <li>O hedge do formador tende a ficar mais <strong>vendido</strong> no cenário mostrado</li>
                    <li>A comparação com a visão retail ajuda a separar exposição líquida de hedge</li>
                </ul>
            </div>
        </div>
        <h3 style="margin-top: 20px; margin-bottom: 12px;">DEX Retail por Spot</h3>
        <canvas id="chartDEXRetail" height="250"></canvas>
        <h3 style="margin-top: 24px; margin-bottom: 12px;">DEX FM por Spot</h3>
        <canvas id="chartDEXFM" height="250"></canvas>
    </div>

    <!-- VEX Section -->
    <div class="chart-section">
        <h2>VEX — Vanna Exposure (Sensibilidade Delta x Vol)</h2>
        <div class="mechanic-box">
            <div class="mechanic-title">Mecânica da Vanna com Contexto Histórico</div>
            <div>
                <div><span class="mechanic-positive">VEX Positivo + IV Baixa vs Histórico</span></div>
                <ul style="margin: 8px 0 12px 20px; color: var(--text2);">
                    <li>Setup ideal para <strong>comprar volatilidade</strong></li>
                    <li>MMs vulneráveis a ajuste de delta com choque de IV</li>
                    <li>IV comprimida vs média histórica</li>
                    <li>Opções relativamente baratas</li>
                    <li><strong>Estratégia:</strong> Long straddles/strangles</li>
                </ul>
                <div><span class="mechanic-negative">VEX Negativo + IV Alta vs Histórico</span></div>
                <ul style="margin: 8px 0 0 20px; color: var(--text2);">
                    <li>Setup ideal para <strong>vender volatilidade</strong></li>
                    <li>MMs mais protegidos para mudanças de IV no delta</li>
                    <li>IV inflada vs média histórica</li>
                    <li>Opções relativamente caras</li>
                    <li><strong>Estratégia:</strong> Short straddles/iron condors</li>
                </ul>
            </div>
        </div>
        <h3 style="margin-top: 20px; margin-bottom: 12px;">VEX por Spot</h3>
        <canvas id="chartVEX" height="250"></canvas>
    </div>

    <!-- TEX Section -->
    <div class="chart-section">
        <h2>TEX — Theta Exposure (Spot Simulado)</h2>
        <div class="mechanic-box">
            <div class="mechanic-title">Mecânica do Theta</div>
            <div>
                <div><span class="mechanic-positive">TEX Positivo (+)</span></div>
                <ul style="margin: 8px 0 12px 20px; color: var(--text2);">
                    <li>Perspectiva do vendedor de opção: tempo trabalha a favor</li>
                    <li>Quanto maior o TEX, maior o carregamento favorável do book naquele spot</li>
                </ul>
                <div><span class="mechanic-negative">TEX Negativo (−)</span></div>
                <ul style="margin: 8px 0 0 20px; color: var(--text2);">
                    <li>Perspectiva do vendedor de opção: tempo vira contra o book naquele cenário</li>
                    <li>O perfil mostra onde o carregamento do theta piora conforme o spot se move</li>
                </ul>
            </div>
        </div>
        <h3 style="margin-top: 20px; margin-bottom: 12px;">TEX por Spot</h3>
        <canvas id="chartTEX" height="250"></canvas>
    </div>

    <!-- CEX Section -->
    <div class="chart-section">
        <h2>CEX — Charm Exposure (Spot Simulado)</h2>
        <div class="legend">Eixo X em spot simulado do BOVA11. Mostra como o delta tende a decair com a passagem do tempo.</div>
        <div class="mechanic-box">
            <div class="mechanic-title">Mecânica do Charm</div>
            <div>
                <div><span class="mechanic-positive">CEX Positivo (+)</span></div>
                <ul style="margin: 8px 0 12px 20px; color: var(--text2);">
                    <li>Com o tempo passando, market makers tendem a <strong>COMPRAR</strong> o ativo para rebalancear delta</li>
                    <li>Indica suporte mecânico de hedging no cenário de spot mostrado</li>
                    <li>Leitura útil para o próximo pregão, não para o strike isolado</li>
                </ul>
                <div><span class="mechanic-negative">CEX Negativo (−)</span></div>
                <ul style="margin: 8px 0 0 20px; color: var(--text2);">
                    <li>Com o tempo passando, market makers tendem a <strong>VENDER</strong> o ativo para rebalancear delta</li>
                    <li>Indica pressão mecânica de hedge mais vendedora no cenário de spot mostrado</li>
                    <li>Extremos do perfil ajudam a localizar regiões sensíveis de decaimento do delta</li>
                </ul>
            </div>
        </div>
        <h3 style="margin-top: 20px; margin-bottom: 12px;">CEX por Spot</h3>
        <canvas id="chartCEX" height="250"></canvas>
    </div>

    <div class="context-box" style="border-left-color: var(--yel);">
        <h3>Notas Técnicas / Revisão Futura</h3>
        <div class="interpretation" style="border-left-color: var(--yel);">
            <strong style="color: var(--yel)">Assunções operacionais do módulo</strong><br>
            Este painel usa modelo de Merton com forward implícito por put-call parity, mas mantém alguns guard rails deliberados para estabilidade.
        </div>
        <ul style="margin: 12px 0 0 20px; color: var(--text2);">
            <li>Forward implícito usa filtro mínimo de <code>{int(FORWARD_MIN_TOTAL_OI):,}</code> contratos no OI total do strike para evitar linhas muito ilíquidas.</li>
            <li>Se não houver <code>bid/ask</code> confiável ou se o forward falhar, o vencimento cai em <code>q = 0</code> como fallback operacional.</li>
            <li>O GEX desta aba mantém a escala histórica <code>gamma × OI × spot² / 100</code>, agora com gamma recalculado em Merton.</li>
            <li>Vencimentos muito curtos ainda são o ponto mais sensível: ruído de <code>bid/ask</code> pode distorcer o <code>q</code> anualizado.</li>
        </ul>
        <div class="strategy-chips" style="margin-top:18px;">
            <span class="chip chip-neutral"><code>python3 -m unittest tests/test_bova11_tex_dex_vex.py</code> OK</span>
            <span class="chip chip-neutral"><code>python3 scripts/bova11_tex_dex_vex.py</code> gera o HTML do módulo</span>
        </div>
    </div>

    <!-- IV Context -->
    <div class="context-box" style="border-left-color: {iv_color}">
        <h3>Contexto de Volatilidade Implícita {iv_symbol}</h3>
        <div class="iv-summary">
            <div class="iv-item">
                <div class="iv-item-label">IV D-1 (média)</div>
                <div class="iv-item-value">{avg_iv_d1_fmt}</div>
            </div>
            <div class="iv-item">
                <div class="iv-item-label">IV D (média)</div>
                <div class="iv-item-value">{avg_iv_d_fmt}</div>
            </div>
            <div class="iv-item" style="border-color: {iv_color}; border-width: 2px;">
                <div class="iv-item-label">Direção</div>
                <div class="iv-item-value" style="color: {iv_color}">{iv_delta_display}</div>
            </div>
        </div>
        <p class="interpretation" style="border-left-color: {iv_signal_color}">
            <strong style="color: {iv_signal_color}">{iv_signal}</strong><br>
            {iv_text}
        </p>
        <div class="strategy-chips">
            {f'<span class="chip chip-buy">📈 LONG VOL (straddle/strangle)</span>' if iv_direction == 'falling' else ''}
            {f'<span class="chip chip-sell">📉 SHORT VOL (iron condor)</span>' if iv_direction == 'rising' else ''}
            {f'<span class="chip chip-neutral">➡️ NEUTRAL</span>' if iv_direction == 'stable' else ''}
        </div>
    </div>

    <script>
        function fmtM(v) {{
            if (v === null || v === undefined) return '';
            const abs = Math.abs(v);
            const sign = v < 0 ? '-' : '';
            if (abs >= 1000) return sign + (abs/1000).toFixed(1) + 'B';
            if (abs >= 1) return sign + abs.toFixed(1) + 'M';
            return sign + (abs * 1000).toFixed(0) + 'k';
        }}

        function makeOpts(yTitle) {{
            return {{
                responsive: true,
                maintainAspectRatio: true,
                interaction: {{mode: 'index', intersect: false}},
                plugins: {{
                    legend: {{
                        display: true,
                        position: 'top',
                        labels: {{color: '#c9d1d9', boxWidth: 14, padding: 16}}
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: ctx => ` ${{ctx.dataset.label}}: ${{fmtM(ctx.parsed.y)}}`
                        }}
                    }}
                }},
                scales: {{
                    x: {{
                        grid: {{color: 'rgba(88,166,255,0.06)'}},
                        ticks: {{color: '#8b949e', maxRotation: 0}},
                    }},
                    y: {{
                        grid: {{color: 'rgba(88,166,255,0.06)'}},
                        ticks: {{
                            color: '#8b949e',
                            callback: (v) => fmtM(v)
                        }},
                        title: {{display: true, text: yTitle, color: '#8b949e', font: {{size: 11}}}},
                    }},
                }},
            }};
        }}

        const PER_EXP = {json.dumps(per_exp_serialized)};
        const EXP_KEYS = {json.dumps(exp_keys)};
        const PALETTE = {json.dumps(PALETTE)};
        const AGG_DATA = {json.dumps(agg_serialized)};
        const SPOT_CURRENT = {round(float(spot_d), 1)};

        let curExp = 'AGG';
        let chartGEX = null;
        let chartDEXRetail = null;
        let chartDEXFM = null;
        let chartVEX = null;
        let chartTEX = null;
        let chartCEX = null;

        function renderExpTabs() {{
            const el = document.getElementById('expTabs');
            el.innerHTML = EXP_KEYS.map((k, i) =>
                `<div class="exp-tab ${{k === curExp ? 'on' : ''}}" style="${{k === curExp ? 'background:' + PALETTE[i % PALETTE.length] : ''}}" data-k="${{k}}">${{k}}</div>`
            ).join('') +
                `<div class="exp-tab agg ${{curExp === 'AGG' ? 'on' : ''}}" data-k="AGG">🔗 AGREGADO</div>`;
            el.querySelectorAll('.exp-tab').forEach(t => t.onclick = () => {{
                curExp = t.dataset.k;
                renderExpTabs();
                updateCharts();
            }});
        }}

        function getDataForExp(expName) {{
            if (expName === 'AGG') return AGG_DATA;
            return PER_EXP[expName] || [];
        }}

        function unpack(data) {{
            return {{
                spots: data.map(r => r.spot.toFixed(1)),
                gexCall: data.map(r => r.gex_call / 1e6),
                gexPut: data.map(r => r.gex_put / 1e6),
                gexNet: data.map(r => r.gex_net / 1e6),
                dexRetailCall: data.map(r => r.dex_retail_call / 1e6),
                dexRetailPut: data.map(r => r.dex_retail_put / 1e6),
                dexRetailNet: data.map(r => r.dex_retail_net / 1e6),
                dexFmCall: data.map(r => r.dex_fm_call / 1e6),
                dexFmPut: data.map(r => r.dex_fm_put / 1e6),
                dexFmNet: data.map(r => r.dex_fm_net / 1e6),
                vexCall: data.map(r => r.vex_call / 1e6),
                vexPut: data.map(r => r.vex_put / 1e6),
                vexNet: data.map(r => r.vex_net / 1e6),
                texCall: data.map(r => r.tex_call / 1e6),
                texPut: data.map(r => r.tex_put / 1e6),
                texNet: data.map(r => r.tex_net / 1e6),
                cexCall: data.map(r => r.cex_call / 1e6),
                cexPut: data.map(r => r.cex_put / 1e6),
                cexNet: data.map(r => r.cex_net / 1e6),
            }};
        }}

        function makeAreaDatasets(callData, putData, netData, callLabel='Call', putLabel='Put', netLabel='Net') {{
            return [
                {{
                    type: 'line',
                    label: callLabel,
                    data: callData,
                    borderColor: 'rgba(63, 185, 80, 1)',
                    backgroundColor: 'rgba(63, 185, 80, 0.18)',
                    borderWidth: 1.8,
                    pointRadius: 0,
                    tension: 0.25,
                    fill: 'origin',
                }},
                {{
                    type: 'line',
                    label: putLabel,
                    data: putData,
                    borderColor: 'rgba(248, 81, 73, 1)',
                    backgroundColor: 'rgba(248, 81, 73, 0.18)',
                    borderWidth: 1.8,
                    pointRadius: 0,
                    tension: 0.25,
                    fill: 'origin',
                }},
                {{
                    type: 'line',
                    label: netLabel,
                    data: netData,
                    borderColor: '#f0c040',
                    backgroundColor: 'rgba(240, 192, 64, 0.12)',
                    borderWidth: 2.5,
                    pointRadius: 0,
                    tension: 0.25,
                    fill: false,
                }},
            ];
        }}

        function updateSummaryCards(data) {{
            if (!data || data.length === 0) return;

            const closest = data.reduce((best, row) =>
                Math.abs(row.spot - SPOT_CURRENT) < Math.abs(best.spot - SPOT_CURRENT) ? row : best,
                data[0]
            );
            const maxSpot = (key) => data.reduce((best, row) => row[key] > best[key] ? row : best, data[0]).spot;
            const minSpot = (key) => data.reduce((best, row) => row[key] < best[key] ? row : best, data[0]).spot;

            const formatNum = (v) => {{
                if (v === 0 || !v) return '—';
                const sign = v < 0 ? '-' : '';
                const absV = Math.abs(v);
                if (absV >= 1e9) return sign + (absV / 1e9).toFixed(1) + 'B';
                if (absV >= 1e6) return sign + (absV / 1e6).toFixed(1) + 'M';
                if (absV >= 1e3) return sign + (absV / 1e3).toFixed(0) + 'k';
                return sign + absV.toFixed(0);
            }};

            const cards = document.querySelectorAll('.card-value');
            const gexNow = closest.gex_net / 1e6;
            const dexFmNow = closest.dex_fm_net / 1e6;
            const vexNow = closest.vex_net / 1e6;
            const texNow = closest.tex_net / 1e6;
            const cexNow = closest.cex_net / 1e6;

            cards[0].textContent = formatNum(gexNow);
            cards[0].style.color = gexNow >= 0 ? 'var(--grn)' : 'var(--red)';
            cards[1].textContent = 'R$' + maxSpot('gex_net').toFixed(1);
            cards[2].textContent = 'R$' + minSpot('gex_net').toFixed(1);

            cards[3].textContent = formatNum(dexFmNow);
            cards[3].style.color = dexFmNow >= 0 ? 'var(--grn)' : 'var(--red)';
            cards[4].textContent = 'R$' + maxSpot('dex_fm_net').toFixed(1);
            cards[5].textContent = 'R$' + minSpot('dex_fm_net').toFixed(1);

            cards[6].textContent = formatNum(vexNow);
            cards[6].style.color = vexNow >= 0 ? 'var(--grn)' : 'var(--red)';
            cards[7].textContent = 'R$' + maxSpot('vex_net').toFixed(1);
            cards[8].textContent = 'R$' + minSpot('vex_net').toFixed(1);

            cards[9].textContent = formatNum(texNow);
            cards[9].style.color = texNow >= 0 ? 'var(--grn)' : 'var(--red)';
            cards[10].textContent = 'R$' + maxSpot('tex_net').toFixed(1);
            cards[11].textContent = 'R$' + minSpot('tex_net').toFixed(1);

            cards[12].textContent = formatNum(cexNow);
            cards[12].style.color = cexNow >= 0 ? 'var(--grn)' : 'var(--red)';
            cards[13].textContent = 'R$' + maxSpot('cex_net').toFixed(1);
            cards[14].textContent = 'R$' + minSpot('cex_net').toFixed(1);
        }}

        function updateCharts() {{
            const data = getDataForExp(curExp);
            const p = unpack(data);

            if (chartGEX) {{
                chartGEX.data.labels = p.spots;
                chartGEX.data.datasets[0].data = p.gexCall;
                chartGEX.data.datasets[1].data = p.gexPut;
                chartGEX.data.datasets[2].data = p.gexNet;
                chartGEX.update();
            }}
            if (chartDEXRetail) {{
                chartDEXRetail.data.labels = p.spots;
                chartDEXRetail.data.datasets[0].data = p.dexRetailCall;
                chartDEXRetail.data.datasets[1].data = p.dexRetailPut;
                chartDEXRetail.data.datasets[2].data = p.dexRetailNet;
                chartDEXRetail.update();
            }}
            if (chartDEXFM) {{
                chartDEXFM.data.labels = p.spots;
                chartDEXFM.data.datasets[0].data = p.dexFmCall;
                chartDEXFM.data.datasets[1].data = p.dexFmPut;
                chartDEXFM.data.datasets[2].data = p.dexFmNet;
                chartDEXFM.update();
            }}
            if (chartVEX) {{
                chartVEX.data.labels = p.spots;
                chartVEX.data.datasets[0].data = p.vexCall;
                chartVEX.data.datasets[1].data = p.vexPut;
                chartVEX.data.datasets[2].data = p.vexNet;
                chartVEX.update();
            }}
            if (chartTEX) {{
                chartTEX.data.labels = p.spots;
                chartTEX.data.datasets[0].data = p.texCall;
                chartTEX.data.datasets[1].data = p.texPut;
                chartTEX.data.datasets[2].data = p.texNet;
                chartTEX.update();
            }}
            if (chartCEX) {{
                chartCEX.data.labels = p.spots;
                chartCEX.data.datasets[0].data = p.cexCall;
                chartCEX.data.datasets[1].data = p.cexPut;
                chartCEX.data.datasets[2].data = p.cexNet;
                chartCEX.update();
            }}

            updateSummaryCards(data);
        }}

        const initial = unpack(AGG_DATA);

        chartGEX = new Chart(document.getElementById('chartGEX').getContext('2d'), {{
            type: 'line',
            data: {{ labels: initial.spots, datasets: makeAreaDatasets(initial.gexCall, initial.gexPut, initial.gexNet, 'Call Gamma', 'Put Gamma', 'GEX Total') }},
            options: makeOpts('Milhões')
        }});

        chartDEXRetail = new Chart(document.getElementById('chartDEXRetail').getContext('2d'), {{
            type: 'line',
            data: {{ labels: initial.spots, datasets: makeAreaDatasets(initial.dexRetailCall, initial.dexRetailPut, initial.dexRetailNet, 'Call Retail', 'Put Retail', 'DEX Retail') }},
            options: makeOpts('M shares')
        }});

        chartDEXFM = new Chart(document.getElementById('chartDEXFM').getContext('2d'), {{
            type: 'line',
            data: {{ labels: initial.spots, datasets: makeAreaDatasets(initial.dexFmCall, initial.dexFmPut, initial.dexFmNet, 'Call FM', 'Put FM', 'DEX FM') }},
            options: makeOpts('M shares')
        }});

        chartVEX = new Chart(document.getElementById('chartVEX').getContext('2d'), {{
            type: 'line',
            data: {{ labels: initial.spots, datasets: makeAreaDatasets(initial.vexCall, initial.vexPut, initial.vexNet, 'Call Vanna', 'Put Vanna', 'VEX Total') }},
            options: makeOpts('Milhões')
        }});

        chartTEX = new Chart(document.getElementById('chartTEX').getContext('2d'), {{
            type: 'line',
            data: {{ labels: initial.spots, datasets: makeAreaDatasets(initial.texCall, initial.texPut, initial.texNet, 'Call Theta', 'Put Theta', 'TEX Total') }},
            options: makeOpts('Milhões')
        }});

        chartCEX = new Chart(document.getElementById('chartCEX').getContext('2d'), {{
            type: 'line',
            data: {{ labels: initial.spots, datasets: makeAreaDatasets(initial.cexCall, initial.cexPut, initial.cexNet, 'Call Charm', 'Put Charm', 'CEX Total') }},
            options: makeOpts('MM Δ/dia')
        }});

        renderExpTabs();
        updateCharts();
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

# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════

def main():
    print()
    print(f"  GEX / DEX / VEX / TEX / CEX — Perfis por Spot | Merton + PCP | r={DEFAULT_RATE:.2%}")
    print()

    # 1) Discover dates
    tag_d, tag_d1 = discover_dates()
    if not tag_d1:
        print("  ❌ Pelo menos duas datas são necessárias (D-1 e D).")
        sys.exit(1)

    label_d = format_date_label(tag_d)
    label_d1 = format_date_label(tag_d1)

    print(f"  📅 D-1: {tag_d1} ({label_d1})")
    print(f"  📅 D:   {tag_d} ({label_d})")
    print()

    # 2) Ask for spot prices (D first, then D-1)
    spot_d = ask_spot(f"D ({label_d})")
    spot_d1 = ask_spot(f"D-1 ({label_d1})")
    print()

    # 3) Discover expirations
    exps_d = discover_expirations(tag_d)
    exps_d1 = discover_expirations(tag_d1)

    if not exps_d:
        print("  ❌ Nenhum arquivo de fechamento encontrado para D.")
        sys.exit(1)

    print(f"  📊 Encontrados {len(exps_d)} vencimentos em D")

    # 4) Process each expiration
    all_rows_d = []
    all_iv_contexts = []
    per_exp_rows_d = {}
    per_exp_data = {}
    exp_keys = []

    for venc_name, fp_d in sorted(exps_d.items()):
        fp_d1 = exps_d1.get(venc_name)

        rows_d = parse_csv(fp_d)
        rows_d1 = parse_csv(fp_d1) if fp_d1 else []

        if rows_d:
            all_rows_d.extend(rows_d)
            per_exp_rows_d[venc_name] = rows_d
            exp_keys.append(venc_name)

            iv_ctx = calc_iv_context(rows_d, rows_d1)
            all_iv_contexts.append(iv_ctx)

    if not all_rows_d:
        print("  ❌ Nenhum dado válido encontrado.")
        sys.exit(1)

    # 4b) Sort exp_keys chronologically by month/day
    exp_keys.sort(key=parse_vencimento_for_sort)

    # 5) Build unified spot profiles
    spot_grid = build_spot_grid(all_rows_d, spot_d)
    for venc_name in exp_keys:
        per_exp_data[venc_name] = build_unified_spot_profile(
            per_exp_rows_d[venc_name],
            venc_name,
            tag_d,
            spot_grid,
            spot_ref=spot_d,
            rate=DEFAULT_RATE,
        )
    agg = aggregate_spot_profiles([per_exp_data[venc_name] for venc_name in exp_keys])

    # 6) Average IV context
    if all_iv_contexts:
        count_iv_d = sum(1 for c in all_iv_contexts if c['avg_iv_d'] is not None)
        count_iv_d1 = sum(1 for c in all_iv_contexts if c['avg_iv_d1'] is not None)
        avg_iv_d = (sum(c['avg_iv_d'] or 0 for c in all_iv_contexts) / count_iv_d) if count_iv_d else None
        avg_iv_d1 = (sum(c['avg_iv_d1'] or 0 for c in all_iv_contexts) / count_iv_d1) if count_iv_d1 else None

        if avg_iv_d is not None and avg_iv_d1 is not None:
            diff = avg_iv_d - avg_iv_d1
            if diff > 0.5:
                direction = 'rising'
            elif diff < -0.5:
                direction = 'falling'
            else:
                direction = 'stable'
        else:
            direction = None

        iv_ctx_final = {
            'avg_iv_d': avg_iv_d,
            'avg_iv_d1': avg_iv_d1,
            'iv_direction': direction,
            'per_strike_delta_iv': {},
        }
    else:
        iv_ctx_final = {
            'avg_iv_d': None,
            'avg_iv_d1': None,
            'iv_direction': None,
            'per_strike_delta_iv': {},
        }

    # 7) Generate HTML
    html_content = generate_html(
        agg,
        iv_ctx_final,
        tag_d1, tag_d,
        label_d1, label_d,
        spot_d1, spot_d,
        per_exp_data, exp_keys
    )

    # 8) Save output
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_file = os.path.join(OUTPUT_DIR, f"bova11_tex_dex_vex_{tag_d1}_vs_{tag_d}.html")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"  ✅ Arquivo gerado: {output_file}")
    print()

if __name__ == '__main__':
    main()
