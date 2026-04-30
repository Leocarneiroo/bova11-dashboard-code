#!/usr/bin/env python3
"""
BOVA11 Trade Score & Institutional Bias — CORRIGIDO (com Auto-Detect de Data)
Usa dados de OI, Volume, IV, Delta, Gamma dos CSVs para calcular:
  - Trade Score (0-100) = 50% Institucional (OI) + 30% Flow (Volume) + 20% Convexity
  - Institutional Bias: SELL_VOL, BUY_VOL, DIRECTIONAL, NEUTRAL, AVOID
  - Conviction: STRONG, MODERATE, WEAK

Detecta automaticamente as datas D-1 e D a partir dos arquivos CSV no diretório.
Solicita os spots de D-1 e D via input interativo.
"""
import os, glob, json, math, re, sys
from datetime import datetime

from bova11_shared import calc_convexity_decomposition, tag_to_iso

DIR        = os.path.dirname(os.path.abspath(__file__)) or "."
DATA_DIR   = os.path.join(DIR, '..', 'data')
OUTPUT_DIR = os.path.join(DIR, '..', 'output')

# ═══════════════════════════════════════
# AUTO-DISCOVERY DE DATAS
# ═══════════════════════════════════════
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

def tag_sort_key(tag):
    meses = {
        'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
        'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
    }
    normalized = re.sub(r'(pos|pre)([a-z]{3})$', r'\2', tag.lower())
    m = re.match(r'(\d{1,2})([a-z]{3})$', normalized)
    if not m:
        return (99, 99, tag.lower())
    return (meses.get(m.group(2), 99), int(m.group(1)), normalized)

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
    Descobre as tags D-1 e D a partir dos arquivos de fechamento no diretório.
    Ordena pelos mtimes para identificar o mais recente (D) e o anterior (D-1).
    """
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
        print("❌ Nenhum arquivo de fechamento encontrado no diretório.")
        sys.exit(1)

    tags_meta = {}
    for tag, mtime in fechamento_files:
        meta = tags_meta.setdefault(tag, {"count": 0, "mtime": 0})
        meta["count"] += 1
        meta["mtime"] = max(meta["mtime"], mtime)

    valid_tags = [tag for tag, meta in tags_meta.items() if meta["count"] >= 2]
    if not valid_tags:
        valid_tags = list(tags_meta.keys())

    tags_sorted = sorted(valid_tags, key=tag_sort_key)

    if len(tags_sorted) == 1:
        tag_d   = tags_sorted[-1]
        tag_d1  = None
    else:
        tag_d   = tags_sorted[-1]
        tag_d1  = tags_sorted[-2]

    return tag_d, tag_d1

def ask_spot(label):
    """Solicita o spot price ao usuário para um determinado dia."""
    while True:
        raw = input(f"  Digite o spot price do BOVA11 para {label} (ex: 188.50): ").strip().replace(',', '.')
        try:
            val = float(raw)
            if val > 0:
                return val
            print("  ❌ O spot deve ser maior que zero.")
        except ValueError:
            print("  ❌ Valor inválido. Use formato: 188.50")

# ═══════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════
def parse_num(s):
    s = str(s).strip().replace('\r', '')
    if s in ('-', '--', ''):
        return None
    mult = 1
    if s.upper().endswith('M'):
        mult = 1e6; s = s[:-1]
    elif s.upper().endswith('K'):
        mult = 1e3; s = s[:-1]
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
    with open(fp, 'r', encoding='latin-1') as f:
        lines = f.readlines()
    results = []
    for line in lines[1:]:
        p = line.strip().replace('\r', '').split(';')
        if len(p) >= 23:
            strike = parse_num(p[11])
            if strike is None:
                continue
            results.append({
                'strike':  strike,
                'c_oi':    int(parse_num(p[2]) or 0),
                'c_delta': parse_num(p[3]) or 0,
                'c_gamma': parse_num(p[4]) or 0,
                'c_vega':  parse_num(p[6]) or 0,
                'c_iv':    parse_num(p[7].replace('%', '')),
                'c_bid':   parse_num(p[9]) or 0,
                'c_ask':   parse_num(p[10]) or 0,
                'p_oi':    int(parse_num(p[20]) or 0),
                'p_delta': parse_num(p[19]) or 0,
                'p_gamma': parse_num(p[18]) or 0,
                'p_vega':  parse_num(p[16]) or 0,
                'p_iv':    parse_num(p[15].replace('%', '')),
                'p_bid':   parse_num(p[12]) or 0,
                'p_ask':   parse_num(p[13]) or 0,
            })
        elif len(p) >= 11:
            strike = parse_num(p[5])
            if strike is None:
                continue
            results.append({
                'strike':  strike,
                'c_oi':    int(parse_num(p[2]) or 0),
                'c_delta': 0, 'c_gamma': 0, 'c_vega': 0,
                'c_iv':    None,
                'c_bid':   parse_num(p[3]) or 0,
                'c_ask':   parse_num(p[4]) or 0,
                'p_oi':    int(parse_num(p[8]) or 0),
                'p_delta': 0, 'p_gamma': 0, 'p_vega': 0,
                'p_iv':    None,
                'p_bid':   parse_num(p[6]) or 0,
                'p_ask':   parse_num(p[7]) or 0,
            })
    return results

def parse_vol(fp):
    if not fp or not os.path.exists(fp):
        return {}

    filename = os.path.basename(fp)
    label = ""
    m = re.match(r'venc_(.+?)_fechamento__', filename)
    if m:
        label = m.group(1).replace('_', ' ')
    else:
        m = re.match(r'venc (.+?) fechamento', filename)
        if m:
            label = m.group(1)

    with open(fp, 'r', encoding='latin-1') as f:
        lines = f.readlines()

    if not lines:
        return {}

    header = lines[0].strip().replace('\r', '').split(';')
    strike_col = 5
    for i, h in enumerate(header):
        if 'strike' in h.lower():
            strike_col = i
            break

    r = {}
    for line in lines[1:]:
        p = line.strip().replace('\r', '').split(';')
        if len(p) < 10:
            continue

        if len(p) > 0 and not is_ticker_match(label, p[0]):
            continue

        strike = parse_num(p[strike_col])
        if strike is None or strike <= 0:
            continue

        if strike not in r:
            r[strike] = {'c_vol': 0, 'p_vol': 0}

        r[strike]['c_vol'] += int(parse_num(p[1]) or 0)
        r[strike]['p_vol'] += int(parse_num(p[9]) or 0)
    return r

# ═══════════════════════════════════════
# DISCOVERY DE VENCIMENTOS
# ═══════════════════════════════════════
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


def is_ticker_match(label, ticker):
    if not label or not ticker:
        return True
    label_lower = label.lower()
    months = {
        "jan": "A", "fev": "B", "mar": "C", "abr": "D", "mai": "E", "jun": "F",
        "jul": "G", "ago": "H", "set": "I", "out": "J", "nov": "K", "dez": "L",
    }
    exp_month = None
    for month, letter in months.items():
        if month in label_lower:
            exp_month = letter
            break
    if not exp_month or len(ticker) < 5:
        return True
    if ticker[4].upper() != exp_month:
        return False
    if 'mensal' in label_lower:
        return re.search(r'W\d+$', ticker.upper()) is None
    return True


def find_volume_file(close_path):
    if not close_path:
        return None
    filename = os.path.basename(close_path)
    tag = extract_tag_from_filename(filename)
    if not tag:
        return None
    if 'fechamento (' in filename:
        volume_name = filename.replace(f'({tag}).csv', f'({tag} Volume).csv')
    else:
        volume_name = filename.replace(f'__{tag}_.csv', f'__{tag}_Volume_.csv')
    candidate = os.path.join(os.path.dirname(close_path), volume_name)
    return candidate if os.path.exists(candidate) else None

# ═══════════════════════════════════════
# CÁLCULO DO TRADE SCORE (lógica james)
# ═══════════════════════════════════════
def calc_trade_scores(d1_rows, d_rows, d1_vol, d_vol, spot_d1, spot_d, venc_name, tag_d1, tag_d):
    delta_spot = spot_d - spot_d1
    d1_map = {r['strike']: r for r in d1_rows}
    max_oi = max((r['c_oi'] + r['p_oi']) for r in d_rows) or 1
    max_vol_d = max(
        (d_vol.get(r['strike'], {}).get('c_vol', 0) + d_vol.get(r['strike'], {}).get('p_vol', 0))
        for r in d_rows
    ) or 1

    results = []
    for r_d in d_rows:
        strike = r_d['strike']
        r_d1 = d1_map.get(strike)

        for side in ['c', 'p']:
            oi_d  = r_d[f'{side}_oi']
            oi_d1 = r_d1[f'{side}_oi'] if r_d1 else 0
            d_oi  = oi_d - oi_d1

            vol_d   = d_vol.get(strike, {}).get(f'{side}_vol', 0)
            vol_d1  = d1_vol.get(strike, {}).get(f'{side}_vol', 0)
            d_vol_v = vol_d - vol_d1

            iv_d  = r_d[f'{side}_iv']
            iv_d1 = r_d1[f'{side}_iv'] if r_d1 else None
            d_iv  = (iv_d - iv_d1) if (iv_d is not None and iv_d1 is not None) else None

            delta_d = r_d[f'{side}_delta']
            gamma_d = r_d[f'{side}_gamma']
            vega_d  = r_d[f'{side}_vega']

            # === INSTITUTIONAL SCORE (0-50) ===
            # OI absolute size (0-15)
            oi_size_sc = 15 * (oi_d / max_oi) if max_oi > 0 else 0

            # OI change magnitude (0-20)
            if oi_d1 > 0:
                oi_chg_pct = abs(d_oi) / oi_d1
                oi_chg_sc = min(20, 20 * oi_chg_pct / 0.3)  # 30% change = full score
            else:
                oi_chg_sc = 10 if abs(d_oi) > 1000 else 0

            # OI direction clarity (0-10) - opening vs closing
            if d_oi > 0 and vol_d > oi_d * 0.1:
                oi_dir_sc = 10   # Clear opening
            elif d_oi < 0 and vol_d > abs(d_oi) * 0.5:
                oi_dir_sc = 8    # Clear closing/rolling
            else:
                oi_dir_sc = 3

            # Proximity to spot (0-5)
            dist = abs(strike - spot_d)
            prox_sc = max(0, 5 * (1 - dist / 8))

            inst_score = oi_size_sc + oi_chg_sc + oi_dir_sc + prox_sc

            # === FLOW SCORE (0-30) ===
            # Volume relative (0-15)
            vol_rel_sc = 15 * (vol_d / max_vol_d) if max_vol_d > 0 else 0

            # Volume vs OI ratio (0-10)
            voi = vol_d / max(oi_d, 1)
            if 0.1 < voi < 0.5:
                voi_sc = 10
            elif 0.05 < voi <= 0.1 or 0.5 <= voi < 2:
                voi_sc = 6
            else:
                voi_sc = 2

            # Volume change (0-5)
            if vol_d1 > 0:
                vol_chg_sc = min(5, 5 * abs(d_vol_v) / max(vol_d1, 1) / 2)
            else:
                vol_chg_sc = 3 if vol_d > 1000 else 0

            flow_score = vol_rel_sc + voi_sc + vol_chg_sc

            # === CONVEXITY SCORE (0-20) ===
            # IV change magnitude (0-10)
            if d_iv is not None and abs(d_iv) > 0.1:
                iv_sc = min(10, 10 * abs(d_iv) / 3)
            else:
                iv_sc = 0

            # Gamma exposure (0-5)
            gamma_sc = min(5, 5 * abs(gamma_d) / 0.05) if gamma_d != 0 else 0

            # Bid/Ask tightness (0-5)
            bid = r_d[f'{side}_bid']
            ask = r_d[f'{side}_ask']
            mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
            spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 100
            ba_sc = max(0, 5 * (1 - spread_pct / 15))

            conv_score = iv_sc + gamma_sc + ba_sc

            # === TOTAL SCORE ===
            total_score = inst_score + flow_score + conv_score

            # === BIAS DETERMINATION ===
            is_opening  = d_oi > 0
            is_closing  = d_oi < 0
            iv_falling  = d_iv is not None and d_iv < -0.5
            iv_rising   = d_iv is not None and d_iv > 0.5
            is_atm      = dist < 3
            is_put      = side == 'p'
            is_call     = side == 'c'

            decomp = calc_convexity_decomposition(
                strike=strike,
                option_type='CALL' if is_call else 'PUT',
                spot_d=spot_d,
                spot_d1=spot_d1,
                delta_d=delta_d,
                delta_d1=(r_d1[f'{side}_delta'] if r_d1 else delta_d),
                gamma_d=gamma_d,
                gamma_d1=(r_d1[f'{side}_gamma'] if r_d1 else gamma_d),
                iv_d=iv_d,
                iv_d1=iv_d1,
                expiry_label=venc_name,
                session_date_d=tag_to_iso(tag_d),
                session_date_d1=tag_to_iso(tag_d1),
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
            if values[0][1] > 0 and values[1][1] >= values[0][1] * 0.85:
                driver = 'MIXED'

            # Bias logic
            if driver in ('CHARM', 'RESIDUAL') and not is_atm:
                bias = 'AVOID'
            elif driver == 'RESIDUAL':
                bias = 'AVOID'
            elif iv_falling and is_opening and driver in ('VANNA', 'MIXED'):
                bias = 'SELL_VOL'
            elif iv_rising and is_opening and driver in ('VANNA', 'MIXED'):
                bias = 'BUY_VOL'
            elif iv_falling and is_closing:
                bias = 'SELL_VOL'
            elif driver == 'GAMMA' and is_opening:
                if (is_call and d_oi > 0) or (is_put and d_oi > 0):
                    bias = 'DIRECTIONAL'
                else:
                    bias = 'NEUTRAL'
            elif is_opening and is_atm:
                bias = 'NEUTRAL'
            elif total_score > 40:
                if iv_falling:
                    bias = 'SELL_VOL'
                elif iv_rising:
                    bias = 'BUY_VOL'
                else:
                    bias = 'NEUTRAL'
            else:
                bias = 'AVOID'

            # Conviction
            inst_agrees = inst_score > 25
            flow_agrees = flow_score > 15
            if inst_agrees and flow_agrees:
                conviction = 'STRONG'
            elif inst_agrees or flow_agrees:
                conviction = 'MODERATE'
            else:
                conviction = 'WEAK'

            results.append({
                'k': strike, 'side': 'CALL' if is_call else 'PUT',
                'venc': venc_name,
                'score': round(total_score, 1),
                'inst_sc': round(inst_score, 1),
                'flow_sc': round(flow_score, 1),
                'conv_sc': round(conv_score, 1),
                'bias': bias, 'conviction': conviction, 'driver': driver,
                'd_oi': d_oi, 'd_vol': d_vol_v, 'vol_d': vol_d,
                'd_iv': round(d_iv, 2) if d_iv is not None else None,
                'oi_d': oi_d, 'spread_pct': round(spread_pct, 1),
                'delta': round(delta_d, 4), 'gamma': round(gamma_d, 6),
                'gamma_contrib': round(decomp['gamma_contrib'], 6),
                'vanna_contrib': round(decomp['vanna_contrib'], 6),
                'charm_contrib': round(decomp['charm_contrib'], 6),
                'residual': round(decomp['residual'], 6),
            })

    return results

# ═══════════════════════════════════════
# GERAÇÃO DO HTML
# ═══════════════════════════════════════
def gen_html(all_results, filename, data_d1, data_d, spot_d1, spot_d):
    all_r = [r for v in all_results.values() for r in v]
    all_r.sort(key=lambda x: x['score'], reverse=True)

    total   = len(all_r)
    sell_n  = sum(1 for r in all_r if r['bias'] == 'SELL_VOL')
    buy_n   = sum(1 for r in all_r if r['bias'] == 'BUY_VOL')
    dir_n   = sum(1 for r in all_r if r['bias'] == 'DIRECTIONAL')
    neut_n  = sum(1 for r in all_r if r['bias'] == 'NEUTRAL')
    avoid_n = sum(1 for r in all_r if r['bias'] == 'AVOID')
    strong_n = sum(1 for r in all_r if r['conviction'] == 'STRONG')
    mod_n    = sum(1 for r in all_r if r['conviction'] == 'MODERATE')
    weak_n   = sum(1 for r in all_r if r['conviction'] == 'WEAK')

    top60 = [r for r in all_r if r['score'] >= 50][:12]

    def bias_badge(b):
        cls = {
            'SELL_VOL':    'bias-sell-vol',
            'BUY_VOL':     'bias-buy-vol',
            'DIRECTIONAL': 'bias-directional',
            'NEUTRAL':     'bias-neutral',
            'AVOID':       'bias-avoid',
        }
        c = cls.get(b, 'bias-neutral')
        return f'<span class="{c}">{b}</span>'

    def conv_badge(c):
        cls = {
            'STRONG':   'conv-strong',
            'MODERATE': 'conv-moderate',
            'WEAK':     'conv-weak',
        }
        k = cls.get(c, 'conv-weak')
        return f'<span class="{k}">{c}</span>'

    def driver_badge(d):
        m = {
            'GAMMA': ('#4ade80', 'SPOT'),
            'VANNA': ('#38bdf8', 'IV'),
            'CHARM': ('#f472b6', 'TIME'),
            'MIXED': ('#fbbf24', 'MIX'),
        }
        c, lbl = m.get(d, ('#8b949e', '?'))
        return f'<span style="color:{c};font-weight:700;font-size:.85em">{d}/{lbl}</span>'

    def score_color(s):
        if s >= 60: return '#4ade80'
        if s >= 40: return '#fbbf24'
        if s >= 25: return '#ffab70'
        return '#f85149'

    def fmt_oi(v):
        if abs(v) >= 1e6: return f'{v/1e6:+.1f}M'
        if abs(v) >= 1e3: return f'{v/1e3:+.0f}k'
        return f'{v:+.0f}'

    # Top opportunities cards
    opp_html = ''
    if top60:
        for r in top60:
            sc = score_color(r['score'])
            div_str = f"{r['d_iv']:+.2f}pp" if r['d_iv'] is not None else "N/A"
            opp_html += f'''<div style="background:#21262d;border:1px solid #30363d;border-radius:8px;padding:16px;border-top:3px solid {sc}">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
<span style="font-size:1.3em;font-weight:700">K{r['k']:.0f}</span>
<span style="background:{sc};color:#000;padding:4px 12px;border-radius:20px;font-weight:700">{r['score']:.0f}</span>
</div>
<div style="margin-bottom:6px">{bias_badge(r['bias'])} {conv_badge(r['conviction'])}</div>
<div style="font-size:.85em;color:#8b949e">
<div>{r['side']} · {r['venc']} · {driver_badge(r['driver'])}</div>
<div>ΔOI: <span style="color:{'#3fb950' if r['d_oi']>0 else '#f85149'}">{fmt_oi(r['d_oi'])}</span> · ΔIV: <span style="color:{'#f85149' if r['d_iv'] and r['d_iv']<0 else '#3fb950'}">{div_str}</span></div>
<div>Inst:{r['inst_sc']:.0f} · Flow:{r['flow_sc']:.0f} · Conv:{r['conv_sc']:.0f}</div>
</div></div>'''
    else:
        opp_html = '<p style="color:#8b949e">Nenhuma oportunidade com score ≥ 50</p>'

    # Full table
    tbl = ''
    for i, r in enumerate(all_r[:120], 1):
        sc = score_color(r['score'])
        div_str = f"{r['d_iv']:+.2f}" if r['d_iv'] is not None else "N/A"
        div_col = '#f85149' if (r['d_iv'] and r['d_iv'] < 0) else '#3fb950' if (r['d_iv'] and r['d_iv'] > 0) else '#8b949e'
        side_col = '#38bdf8' if r['side'] == 'CALL' else '#f472b6'
        tbl += f'''<tr>
<td>{i}</td><td><strong>K{r['k']:.0f}</strong></td>
<td><span style="color:{side_col};font-weight:700">{r['side']}</span></td>
<td>{r['venc']}</td><td>{driver_badge(r['driver'])}</td>
<td style="color:{sc};font-weight:700;font-size:1.1em">{r['score']:.1f}</td>
<td>{bias_badge(r['bias'])}</td><td>{conv_badge(r['conviction'])}</td>
<td>{r['inst_sc']:.0f}</td><td>{r['flow_sc']:.0f}</td><td>{r['conv_sc']:.0f}</td>
<td style="color:{'#3fb950' if r['d_oi']>0 else '#f85149' if r['d_oi']<0 else '#8b949e'}">{fmt_oi(r['d_oi'])}</td>
<td>{fmt_oi(r['vol_d']) if r['vol_d'] else '0'}</td>
<td style="color:{div_col}">{div_str}</td>
<td>{r['spread_pct']:.0f}%</td></tr>'''

    html = f'''<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOVA11 Trade Score & Bias — {data_d1} vs {data_d}</title>
<style>
:root{{--bg:#ffffff;--bg2:#f6f8fa;--bg3:#eaeef2;--text:#1f2328;--text2:#636c76;--brd:#d0d7de}}
[data-theme="dark"]{{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--text:#c9d1d9;--text2:#8b949e;--brd:#30363d}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:20px;line-height:1.6}}
[data-theme="dark"] .bias-sell-vol{{color:#3fb950!important;background:#0d2818!important;border-color:#3fb950!important}}
[data-theme="dark"] .bias-buy-vol{{color:#38bdf8!important;background:#0c2d48!important;border-color:#38bdf8!important}}
[data-theme="dark"] .bias-directional{{color:#fbbf24!important;background:#3d3310!important;border-color:#fbbf24!important}}
[data-theme="dark"] .bias-neutral{{color:#8b949e!important;background:#21262d!important;border-color:#8b949e!important}}
[data-theme="dark"] .bias-avoid{{color:#f85149!important;background:#3d1418!important;border-color:#f85149!important}}
[data-theme="dark"] .conv-strong{{color:#4ade80!important;background:#0d2818!important;border-color:#4ade80!important}}
[data-theme="dark"] .conv-moderate{{color:#38bdf8!important;background:#0c2d48!important;border-color:#38bdf8!important}}
[data-theme="dark"] .conv-weak{{color:#6b7280!important;background:#1f2937!important;border-color:#6b7280!important}}
.bias-sell-vol{{color:#1a7f37;background:#d1f7dd;padding:3px 8px;border-radius:4px;border:1px solid #1a7f37;font-size:.8em;font-weight:600}}
.bias-buy-vol{{color:#0969da;background:#cce5ff;padding:3px 8px;border-radius:4px;border:1px solid #0969da;font-size:.8em;font-weight:600}}
.bias-directional{{color:#9a6700;background:#fff3cd;padding:3px 8px;border-radius:4px;border:1px solid #9a6700;font-size:.8em;font-weight:600}}
.bias-neutral{{color:#636c76;background:#eaeef2;padding:3px 8px;border-radius:4px;border:1px solid #636c76;font-size:.8em;font-weight:600}}
.bias-avoid{{color:#cf222e;background:#ffdce0;padding:3px 8px;border-radius:4px;border:1px solid #cf222e;font-size:.8em;font-weight:600}}
.conv-strong{{color:#1a7f37;background:#d1f7dd;padding:2px 6px;border-radius:4px;border:1px solid #1a7f37;font-size:.75em}}
.conv-moderate{{color:#0969da;background:#cce5ff;padding:2px 6px;border-radius:4px;border:1px solid #0969da;font-size:.75em}}
.conv-weak{{color:#636c76;background:#eaeef2;padding:2px 6px;border-radius:4px;border:1px solid #636c76;font-size:.75em}}
.hdr{{background:linear-gradient(135deg,var(--bg2),var(--bg3));border:1px solid var(--brd);border-radius:12px;padding:24px;margin-bottom:24px}}
.hdr h1{{font-size:1.8em;margin-bottom:8px}} .hdr .meta{{color:var(--text2)}}
.sg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:24px}}
.sc{{background:var(--bg2);border:1px solid var(--brd);border-radius:8px;padding:14px;text-align:center}}
.sc .l{{color:var(--text2);font-size:.82em;margin-bottom:3px}} .sc .v{{font-size:1.5em;font-weight:700}}
.sec{{background:var(--bg2);border:1px solid var(--brd);border-radius:12px;padding:20px;margin-bottom:24px}}
.sec h2{{font-size:1.3em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--brd)}}
.og{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:.85em}}
th,td{{padding:10px 8px;text-align:center;border-bottom:1px solid var(--brd)}}
th{{background:var(--bg3);font-weight:600;color:var(--text2);position:sticky;top:0;font-size:.75em;text-transform:uppercase}}
tr:hover{{background:var(--bg3)}}
.meth{{background:var(--bg3);border-left:4px solid #38bdf8;padding:16px;border-radius:0 8px 8px 0;margin-top:24px}}
.meth h3{{margin-bottom:12px;color:#38bdf8}} .meth ul{{margin-left:20px;color:var(--text2)}} .meth li{{margin:8px 0}}
</style></head><body>
<button id="theme-toggle" onclick="toggleTheme()" title="Alternar tema" style="position:fixed;top:12px;right:16px;z-index:9999;background:var(--bg2);border:1px solid var(--brd);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:1rem;line-height:1;transition:all 0.2s;">◐</button>
<div class="hdr"><h1>📊 BOVA11 Trade Score & Institutional Bias</h1>
<div class="meta">{data_d1} vs {data_d} | Spot: <strong style="color:#38bdf8">{spot_d1:.2f} → {spot_d:.2f}</strong> (Δ{spot_d-spot_d1:+.2f})</div></div>

<div class="sg">
<div class="sc"><div class="l">Total</div><div class="v" style="color:#7dd3fc">{total}</div></div>
<div class="sc"><div class="l">SELL VOL</div><div class="v" style="color:#3fb950">{sell_n}</div></div>
<div class="sc"><div class="l">BUY VOL</div><div class="v" style="color:#38bdf8">{buy_n}</div></div>
<div class="sc"><div class="l">DIRECTIONAL</div><div class="v" style="color:#fbbf24">{dir_n}</div></div>
<div class="sc"><div class="l">NEUTRAL</div><div class="v" style="color:#8b949e">{neut_n}</div></div>
<div class="sc"><div class="l">AVOID</div><div class="v" style="color:#f85149">{avoid_n}</div></div>
</div>
<div class="sg">
<div class="sc"><div class="l">Convicção STRONG</div><div class="v" style="color:#4ade80">{strong_n}</div></div>
<div class="sc"><div class="l">Convicção MODERATE</div><div class="v" style="color:#38bdf8">{mod_n}</div></div>
<div class="sc"><div class="l">Convicção WEAK</div><div class="v" style="color:#6b7280">{weak_n}</div></div>
</div>

<div class="sec"><h2>🎯 Top Oportunidades (Score ≥ 50)</h2><div class="og">{opp_html}</div></div>

<div class="sec"><h2>📋 Ranking Completo (Top 120)</h2>
<div style="overflow-x:auto;max-height:700px;overflow-y:auto">
<table><thead><tr>
<th>#</th><th>Strike</th><th>Tipo</th><th>Venc.</th><th>Driver</th>
<th>Score</th><th>Bias</th><th>Conv.</th>
<th>Inst</th><th>Flow</th><th>Conv</th>
<th>ΔOI</th><th>Vol</th><th>ΔIV</th><th>Sprd%</th>
</tr></thead><tbody>{tbl}</tbody></table></div></div>

<div class="meth"><h3>📖 Metodologia</h3><ul>
<li><strong>SCORE (0-100):</strong> Inst(0-50) + Flow(0-30) + Convexity(0-20)</li>
<li><strong>Inst Score:</strong> OI size(15) + OI change(20) + Direction clarity(10) + Proximity(5)</li>
<li><strong>Flow Score:</strong> Volume relative(15) + Vol/OI ratio(10) + Volume change(5)</li>
<li><strong>Convexity:</strong> IV change(10) + Gamma(5) + Bid/Ask spread(5)</li>
<li><strong>SELL_VOL:</strong> IV caindo + abertura de posição + driver Vanna/Mixed</li>
<li><strong>BUY_VOL:</strong> IV subindo + abertura + driver Vanna/Mixed</li>
<li><strong>DIRECTIONAL:</strong> Driver Gamma + abertura clara</li>
<li><strong>AVOID:</strong> Charm dominante (perto do vencimento) ou score baixo</li>
<li><strong>Conviction:</strong> STRONG (Inst>25 E Flow>15), MODERATE (um dos dois), WEAK (nenhum)</li>
</ul></div>
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
</body></html>'''

    out_path = os.path.join(OUTPUT_DIR, filename)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  ✅ HTML gerado: ./{filename}")

# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════
if __name__ == '__main__':
    print("=" * 65)
    print("  BOVA11 Trade Score & Institutional Bias — Auto-Detect")
    print("=" * 65)

    # 1) Auto-detect de datas
    TAG_D, TAG_D1 = discover_dates()
    DATA_D  = format_date_label(TAG_D)
    DATA_D1 = format_date_label(TAG_D1) if TAG_D1 else "N/A"

    print(f"\n  📅 Datas detectadas:")
    print(f"     D-1 : {TAG_D1 or 'não encontrado'} ({DATA_D1})")
    print(f"     D   : {TAG_D} ({DATA_D})")

    if not TAG_D1:
        print("\n  ⚠️  Apenas uma data encontrada. Não é possível calcular deltas.")
        sys.exit(1)

    # 2) Input de spot
    print()
    SPOT_D1 = ask_spot(f"D-1 ({DATA_D1})")
    SPOT_D  = ask_spot(f"D   ({DATA_D})")

    # 3) Descobrir vencimentos
    exps_d1 = discover_expirations(TAG_D1)
    exps_d  = discover_expirations(TAG_D)

    all_results = {}

    for name, fp_d in sorted(exps_d.items()):
        fp_d1 = exps_d1.get(name)
        if not fp_d1:
            print(f"  ⚠️  Vencimento '{name}' sem D-1 correspondente — pulando.")
            continue

        rows_d1 = parse_csv(fp_d1)
        rows_d  = parse_csv(fp_d)

        vol_fp_d1 = find_volume_file(fp_d1)
        vol_fp_d  = find_volume_file(fp_d)
        vol_d1 = parse_vol(vol_fp_d1) if vol_fp_d1 else {}
        vol_d  = parse_vol(vol_fp_d)  if vol_fp_d  else {}

        results = calc_trade_scores(rows_d1, rows_d, vol_d1, vol_d, SPOT_D1, SPOT_D, name, TAG_D1, TAG_D)
        all_results[name] = results

        biases = {r['bias'] for r in results}
        print(f"  ✓ {name:24s} | {len(results)} entries | Biases: {', '.join(sorted(biases))}")

    if not all_results:
        print("\n  ❌ Nenhum vencimento processado.")
        sys.exit(1)

    # 4) Gerar HTML
    fname = f"bova11_trade_score_{TAG_D1}_vs_{TAG_D}.html"
    gen_html(all_results, fname, DATA_D1, DATA_D, SPOT_D1, SPOT_D)
    print("\n  CONCLUÍDO ✅")
