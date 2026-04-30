#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Insights Generator — Versão Automatizada
================================================
Auto-descobre as datas a partir dos arquivos CSV no diretório.
Solicita apenas o spot price via input no terminal.

Uso:
  python3 bova11_insights_auto.py
  
O script irá:
  1. Detectar automaticamente os arquivos CSV no diretório
  2. Identificar as datas (D-1 e D) baseado na data de modificação
  3. Solicitar o spot price do dia
  4. Gerar o HTML de insights
"""

import os, json, math, re, glob, sys
from datetime import datetime, timedelta

# ═══════════════════════════════════════
# CONFIG — Apenas ajustes finos
# ═══════════════════════════════════════
_BASEDIR       = os.path.dirname(os.path.abspath(__file__))
CSV_DIR        = os.path.join(_BASEDIR, '..', 'data')
ANO            = str(datetime.now().year)
GEX_STRIKE_MIN = 160
GEX_STRIKE_MAX = 200
OUTPUT_DIR     = os.path.join(_BASEDIR, '..', 'output')

# Mapeamento de vencimentos → data real para cálculo de DTE
# Atualize conforme necessário
VENC_DATES = {
    "13 fev W2":     "2025-02-13",
    "20 fev Mensal": "2025-02-20",
    "27 fev W4":     "2025-02-27",
    "6 mar W2":      "2025-03-06",
    "20 mar Mensal": "2025-03-20",
}

# ═══════════════════════════════════════
# AUTO-DISCOVERY DE DATAS
# ═══════════════════════════════════════
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

def parse_date_tag(tag):
    """Converte tag como '12fev' ou '13fev' para data."""
    meses = {
        'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
        'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
    }
    m = re.match(r'(\d{1,2})([a-z]{3})$', tag.lower())
    if m:
        dia = int(m.group(1))
        mes = meses.get(m.group(2), 1)
        return datetime(int(ANO), mes, dia)
    return None

def format_date_label(tag):
    """Formata tag como '12fev' para label '12/Fev'."""
    meses = {
        'jan': 'Jan', 'fev': 'Fev', 'mar': 'Mar', 'abr': 'Abr', 'mai': 'Mai', 'jun': 'Jun',
        'jul': 'Jul', 'ago': 'Ago', 'set': 'Set', 'out': 'Out', 'nov': 'Nov', 'dez': 'Dez'
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
    """Descobre as datas D-1 e D a partir dos arquivos no diretório."""
    # Encontra todos os arquivos de fechamento
    fechamento_files = []
    
    # Padrão underscore
    for fpath in glob.glob(os.path.join(CSV_DIR, "venc_*_fechamento__*_.csv")):
        filename = os.path.basename(fpath)
        if not is_primary_fechamento_file(filename):
            continue
        tag = extract_date_from_filename(filename)
        if tag:
            mtime = os.path.getmtime(fpath)
            fechamento_files.append((tag, mtime, fpath))
    
    # Padrão espaço
    for fpath in glob.glob(os.path.join(CSV_DIR, "venc * fechamento (*).csv")):
        filename = os.path.basename(fpath)
        if not is_primary_fechamento_file(filename):
            continue
        tag = extract_date_from_filename(filename)
        if tag:
            mtime = os.path.getmtime(fpath)
            fechamento_files.append((tag, mtime, fpath))
    
    if not fechamento_files:
        print("❌ Nenhum arquivo de fechamento encontrado no diretório.")
        print("   Esperado: venc_*_fechamento__<tag>_.csv ou 'venc * fechamento (<tag>).csv'")
        sys.exit(1)
    
    tags_meta = {}
    for tag, mtime, _fpath in fechamento_files:
        meta = tags_meta.setdefault(tag, {"count": 0, "mtime": 0})
        meta["count"] += 1
        meta["mtime"] = max(meta["mtime"], mtime)

    tags = [tag for tag, meta in tags_meta.items() if meta["count"] >= 2]
    if not tags:
        tags = list(tags_meta.keys())
    
    if len(tags) < 1:
        print("❌ Não foi possível identificar datas nos arquivos.")
        sys.exit(1)
    
    # Se só tem uma tag, usa ela como D (snapshot mode)
    if len(tags) == 1:
        print(f"⚠️  Apenas uma data encontrada: {tags[0]}")
        print("   Será usado modo snapshot (apenas D, sem comparação D-1)")
        return tags[0], None
    
    # Ordena por data de modificação (mais antigo = D-1, mais novo = D)
    tags_sorted = sorted(tags, key=tag_sort_key)
    
    # A maior tag cronológica é D; a anterior é D-1
    tag_d = tags_sorted[-1]
    tag_d1 = tags_sorted[-2] if len(tags_sorted) >= 2 else None
    
    return tag_d, tag_d1

def ask_spot():
    """Solicita o spot price ao usuário."""
    print()
    print("=" * 65)
    print("  CONFIGURAÇÃO DO SPOT")
    print("=" * 65)
    
    while True:
        spot_input = input("  Digite o spot price do BOVA11 (ex: 182.98): ").strip().replace(',', '.')
        try:
            spot = float(spot_input)
            if spot > 0:
                return spot
            else:
                print("  ❌ O spot deve ser maior que zero.")
        except ValueError:
            print("  ❌ Valor inválido. Use formato: 182.98")

# ═══════════════════════════════════════
# PARSERS (idênticos ao original)
# ═══════════════════════════════════════
def parse_num(s):
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


def is_ticker_match(label, ticker):
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
    import os, re
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
        
        # Filtro de Ticker para evitar sujeira de múltiplas maturidades no mesmo CSV
        if len(p) > 0 and not is_ticker_match(label, p[0]):
            continue
            
        if ncols >= 20 and len(p) >= 23:
            strike = parse_num(p[11])
            if strike in seen_strikes: continue
            seen_strikes.add(strike)
            
            c_iv_raw = p[7].strip().replace('%', '')
            p_iv_raw = p[15].strip().replace('%', '')
            results.append({
                'strike':  strike,
                'c_last':  parse_num(p[1]),  'c_oi': int(parse_num(p[2])),
                'c_delta': parse_num(p[3]),  'c_gamma': parse_num(p[4]),
                'c_theta': parse_num(p[5]),  'c_vega':  parse_num(p[6]),
                'c_iv':    (parse_num(c_iv_raw) if c_iv_raw not in ('-', '', '--') else None),
                'c_bid':   parse_num(p[9]),  'c_ask': parse_num(p[10]),
                'p_bid':   parse_num(p[12]), 'p_ask': parse_num(p[13]),
                'p_iv':    (parse_num(p_iv_raw) if p_iv_raw not in ('-', '', '--') else None),
                'p_vega':  parse_num(p[16]), 'p_theta': parse_num(p[17]),
                'p_gamma': parse_num(p[18]), 'p_delta': parse_num(p[19]),
                'p_oi':    int(parse_num(p[20])), 'p_last': parse_num(p[21]),
            })
        elif len(p) >= 11:
            strike = parse_num(p[5])
            if strike in seen_strikes: continue
            seen_strikes.add(strike)
            
            c_bid = parse_num(p[3])
            c_ask = parse_num(p[4])
            p_bid = parse_num(p[6])
            p_ask = parse_num(p[7])
            results.append({
                'strike':  strike,
                'c_last':  round((c_bid + c_ask) / 2, 2) if (c_bid + c_ask) > 0 else 0.0,
                'c_oi':    int(parse_num(p[2])),
                'c_delta': 0.0, 'c_gamma': 0.0, 'c_theta': 0.0, 'c_vega': 0.0,
                'c_iv':    None,
                'c_bid':   c_bid, 'c_ask': c_ask,
                'p_bid':   p_bid, 'p_ask': p_ask,
                'p_iv':    None,
                'p_vega':  0.0, 'p_theta': 0.0, 'p_gamma': 0.0, 'p_delta': 0.0,
                'p_oi':    int(parse_num(p[8])),
                'p_last':  round((p_bid + p_ask) / 2, 2) if (p_bid + p_ask) > 0 else 0.0,
            })
    return results

def parse_vol_csv(filepath):
    if not filepath or not os.path.exists(filepath):
        return {}
    filename = os.path.basename(filepath)
    label = ""
    m = re.match(r'venc_(.+?)_fechamento__', filename)
    if m:
        label = m.group(1).replace('_', ' ')
    else:
        m = re.match(r'venc (.+?) fechamento', filename)
        if m:
            label = m.group(1)
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
        # Mesmo filtro de ticker do fechamento para evitar mistura de vencimentos.
        if len(p) > 0 and not is_ticker_match(label, p[0]):
            continue
        strike = parse_num(p[strike_col])
        if strike <= 0:
            continue
        if strike not in results:
            results[strike] = {'cv': 0, 'pv': 0}
        # Soma duplicatas por strike (em vez de sobrescrever).
        results[strike]['cv'] += int(parse_num(p[1]))
        results[strike]['pv'] += int(parse_num(p[9]))
    return results

# ═══════════════════════════════════════
# HELPERS (idênticos ao original)
# ═══════════════════════════════════════
def infer_spot(fech_rows):
    best_diff, best_strike = None, None
    for row in fech_rows:
        c = row.get('c_last', 0) or 0
        p = row.get('p_last', 0) or 0
        if c <= 0 or p <= 0:
            continue
        diff = abs(c - p)
        if best_diff is None or diff < best_diff:
            best_diff = diff; best_strike = row['strike']
    return float(best_strike) if best_strike is not None else 180.0

def calc_max_pain(strikes_data):
    best_s, best_pain = 0, float('inf')
    for test in strikes_data:
        pain = sum(s['co2'] * max(test['s'] - s['s'], 0) + s['po2'] * max(s['s'] - test['s'], 0)
                   for s in strikes_data)
        if pain < best_pain:
            best_pain = pain; best_s = test['s']
    return best_s

def calc_gex(spot, fech_rows):
    r = {}
    for row in fech_rows:
        sk = row['strike']
        cg =  spot**2 * (row['c_gamma'] or 0) * (row['c_oi'] or 0) / 100.0
        pg = -spot**2 * (row['p_gamma'] or 0) * (row['p_oi'] or 0) / 100.0
        r[sk] = {'c': cg, 'p': pg, 'n': cg + pg}
    return r

def find_flip(spot, agg_list):
    for i in range(len(agg_list) - 1):
        s1, n1 = agg_list[i]['s'], agg_list[i]['n']
        s2, n2 = agg_list[i+1]['s'], agg_list[i+1]['n']
        if abs(s1 - spot) > 12:
            continue
        if (n1 < 0 and n2 > 0) or (n1 > 0 and n2 < 0):
            return round(s1 + (s2 - s1) * abs(n1) / (abs(n1) + abs(n2)), 1)
    return None

def safe_diff(v2, v1):
    if v2 is None or v1 is None: return None
    return round(v2 - v1, 6)

def safe_pct(v2, v1):
    if v1 is None or v2 is None: return None
    if v1 == 0: return 0.0 if v2 == 0 else 100.0
    return round(((v2 - v1) / abs(v1)) * 100, 2)

def fI(n):
    """Formata inteiro grande → 1.2M / 345k / 123"""
    if n is None: return '–'
    a = abs(n)
    s = '' if n >= 0 else '−'
    if a >= 1_000_000:
        return f"{s}{a/1e6:.1f}M"
    elif a >= 1_000:
        return f"{s}{a/1e3:.0f}k"
    else:
        return f"{s}{round(a)}"

def fP(n):
    """Formata com sinal +/-"""
    if n is None: return '–'
    sign = '+' if n >= 0 else ''
    return f"{sign}{fI(n)}"

def fPct(n):
    if n is None: return '–'
    sign = '+' if n >= 0 else ''
    return f"{sign}{n:.1f}%"

def css_class(n):
    if n is None: return 'neut'
    return 'pos' if n >= 0 else 'neg'

def get_dte(label, DATA_D):
    """Calcula DTE a partir do label do vencimento."""
    mon_map = {
        'jan': '01', 'fev': '02', 'mar': '03', 'abr': '04', 'mai': '05', 'jun': '06',
        'jul': '07', 'ago': '08', 'set': '09', 'out': '10', 'nov': '11', 'dez': '12'
    }
    parts = DATA_D.split('/')
    day = parts[0]
    month = mon_map.get(parts[1].lower(), '01') if len(parts) > 1 else '01'
    today = datetime.strptime(f"{ANO}-{month}-{day}", "%Y-%m-%d")

    match = re.search(r'(\d{1,2})\s+([a-z]{3})', label.lower())
    if match:
        exp_day = int(match.group(1))
        exp_month = mon_map.get(match.group(2), '01')
        exp_date = datetime.strptime(f"{ANO}-{exp_month}-{exp_day:02d}", "%Y-%m-%d")
        if exp_date < today:
            exp_date = exp_date.replace(year=exp_date.year + 1)
        return (exp_date - today).days

    for k, d in VENC_DATES.items():
        if k.lower() == label.lower():
            exp_date = datetime.strptime(d, "%Y-%m-%d")
            if exp_date < today:
                exp_date = exp_date.replace(year=exp_date.year + 1)
            return (exp_date - today).days

    return None

# ═══════════════════════════════════════
# DISCOVERY DE VENCIMENTOS
# ═══════════════════════════════════════
def discover_expirations(TAG_D, TAG_D1):
    exps = []
    for fpath in sorted(glob.glob(os.path.join(CSV_DIR, f"venc_*_fechamento__{TAG_D}_.csv"))):
        fn = os.path.basename(fpath)
        m = re.match(r'venc_(.+?)_fechamento__' + re.escape(TAG_D) + r'_\.csv$', fn)
        if not m: continue
        label = m.group(1).strip()
        exps.append({
            "label": label.replace('_', ' '),
            "fech_d": fn,
            "vol_d":   f"venc_{label}_fechamento__{TAG_D}_Volume_.csv",
            "fech_d1": f"venc_{label}_fechamento__{TAG_D1}_.csv" if TAG_D1 else None,
            "vol_d1":  f"venc_{label}_fechamento__{TAG_D1}_Volume_.csv" if TAG_D1 else None,
        })
    for fpath in sorted(glob.glob(os.path.join(CSV_DIR, f"venc * fechamento ({TAG_D}).csv"))):
        fn = os.path.basename(fpath)
        m = re.match(r'venc (.+) fechamento \(' + re.escape(TAG_D) + r'\)\.csv$', fn)
        if not m: continue
        label = m.group(1).strip()
        if any(e['label'] == label for e in exps): continue
        exps.append({
            "label": label,
            "fech_d": fn,
            "vol_d":   f"venc {label} fechamento ({TAG_D} Volume).csv",
            "fech_d1": f"venc {label} fechamento ({TAG_D1}).csv" if TAG_D1 else None,
            "vol_d1":  f"venc {label} fechamento ({TAG_D1} Volume).csv" if TAG_D1 else None,
        })
    for e in exps:
        for k in ('vol_d', 'fech_d1', 'vol_d1'):
            if e[k] and not os.path.exists(os.path.join(CSV_DIR, e[k])):
                e[k] = None
    return exps

# ═══════════════════════════════════════
# BUILD DATA POR VENCIMENTO
# ═══════════════════════════════════════
def build_exp_data(vinfo, rows_d, rows_d1, volmap_d, volmap_d1, spot):
    d1_map = {r['strike']: r for r in rows_d1} if rows_d1 else {}
    strikes_out = []
    tc_oi = tp_oi = tc_vol = tp_vol = 0
    tc_oi1 = tp_oi1 = tc_vol1 = tp_vol1 = 0

    for r in rows_d:
        s = r['strike']
        r1 = d1_map.get(s)
        v_d = volmap_d.get(s, {'cv': 0, 'pv': 0})
        v_1 = volmap_d1.get(s, {'cv': 0, 'pv': 0}) if volmap_d1 else {'cv': 0, 'pv': 0}
        cv, pv   = int(v_d.get('cv', 0)), int(v_d.get('pv', 0))
        cv1, pv1 = int(v_1.get('cv', 0)), int(v_1.get('pv', 0))
        snap = r1 is None
        if r1 is None:
            r1 = r

        cp_pct = safe_pct(r['c_last'], r1['c_last']) or 0.0
        pp_pct = safe_pct(r['p_last'], r1['p_last']) or 0.0
        co1, co2 = int(r1['c_oi']), int(r['c_oi'])
        po1, po2 = int(r1['p_oi']), int(r['p_oi'])

        tc_oi += co2; tp_oi += po2; tc_oi1 += co1; tp_oi1 += po1
        tc_vol += cv; tp_vol += pv; tc_vol1 += cv1; tp_vol1 += pv1

        strikes_out.append({
            's': s, 'snap': snap,
            'c_last1': r1['c_last'], 'c_last2': r['c_last'], 'cp_pct': cp_pct,
            'p_last1': r1['p_last'], 'p_last2': r['p_last'], 'pp_pct': pp_pct,
            'co1': co1, 'co2': co2, 'dco': co2 - co1,
            'po1': po1, 'po2': po2, 'dpo': po2 - po1,
            'c_iv1': r1.get('c_iv'), 'c_iv2': r.get('c_iv'), 'c_ivd': safe_diff(r.get('c_iv'), r1.get('c_iv')),
            'p_iv1': r1.get('p_iv'), 'p_iv2': r.get('p_iv'), 'p_ivd': safe_diff(r.get('p_iv'), r1.get('p_iv')),
            'c_delta1': r1['c_delta'], 'c_delta2': r['c_delta'], 'dc_delta': round(r['c_delta'] - r1['c_delta'], 4),
            'c_gamma1': r1['c_gamma'], 'c_gamma2': r['c_gamma'], 'dc_gamma': round(r['c_gamma'] - r1['c_gamma'], 6),
            'c_theta1': r1['c_theta'], 'c_theta2': r['c_theta'], 'dc_theta': round(r['c_theta'] - r1['c_theta'], 4),
            'c_vega1':  r1['c_vega'],  'c_vega2':  r['c_vega'],  'dc_vega':  round(r['c_vega']  - r1['c_vega'], 4),
            'p_delta1': r1['p_delta'], 'p_delta2': r['p_delta'], 'dp_delta': round(r['p_delta'] - r1['p_delta'], 4),
            'p_gamma1': r1['p_gamma'], 'p_gamma2': r['p_gamma'], 'dp_gamma': round(r['p_gamma'] - r1['p_gamma'], 6),
            'p_theta1': r1['p_theta'], 'p_theta2': r['p_theta'], 'dp_theta': round(r['p_theta'] - r1['p_theta'], 4),
            'p_vega1':  r1['p_vega'],  'p_vega2':  r['p_vega'],  'dp_vega':  round(r['p_vega']  - r1['p_vega'], 4),
            'cv1': cv1, 'pv1': pv1, 'cv': cv, 'pv': pv,
            'dcv': cv - cv1, 'dpv': pv - pv1,
            'tv1': cv1 + pv1, 'tv': cv + pv, 'dtv': (cv + pv) - (cv1 + pv1),
        })

    mp = calc_max_pain(strikes_out) if strikes_out else 0
    def avg_iv(key, radius=2):
        vals = [x[key] for x in strikes_out if x[key] is not None and abs(x['s'] - spot) <= radius]
        return round(sum(vals)/len(vals), 2) if vals else 0.0
    iv_c = avg_iv('c_iv2'); iv_c1 = avg_iv('c_iv1')
    iv_p = avg_iv('p_iv2'); iv_p1 = avg_iv('p_iv1')
    pcr_oi  = round(tp_oi / max(tc_oi, 1), 2)
    pcr_oi1 = round(tp_oi1 / max(tc_oi1, 1), 2)
    pcr_vol = round(tp_vol / max(tc_vol, 1), 2)
    pcr_vol1 = round(tp_vol1 / max(tc_vol1, 1), 2)

    return {
        'name': vinfo['name'], 'color': vinfo['color'], 'strikes': strikes_out, 'mp': mp,
        'iv_c_atm': iv_c, 'iv_c_atm1': iv_c1, 'div_c_atm': round(iv_c - iv_c1, 2),
        'iv_p_atm': iv_p, 'iv_p_atm1': iv_p1, 'div_p_atm': round(iv_p - iv_p1, 2),
        'pcr_oi': pcr_oi, 'pcr_oi1': pcr_oi1, 'dpcr_oi': round(pcr_oi - pcr_oi1, 2),
        'pcr_vol': pcr_vol, 'pcr_vol1': pcr_vol1, 'dpcr_vol': round(pcr_vol - pcr_vol1, 2),
        'coi': tc_oi, 'poi': tp_oi, 'coi1': tc_oi1, 'poi1': tp_oi1,
        'dcoi': tc_oi - tc_oi1, 'dpoi': tp_oi - tp_oi1,
        'cvol': tc_vol, 'pvol': tp_vol, 'cvol1': tc_vol1, 'pvol1': tp_vol1,
        'd_cvol': tc_vol - tc_vol1, 'd_pvol': tp_vol - tp_vol1,
        'd_tvol': (tc_vol + tp_vol) - (tc_vol1 + tp_vol1),
    }

# ═══════════════════════════════════════
# GERAÇÃO DE INSIGHTS POR VENCIMENTO
# ═══════════════════════════════════════
def generate_insights(rankings, gex_data, spot, DATA_D):
    """
    Gera lista de dicts com insights textuais e métricas para cada vencimento.
    """
    insights = []
    for key, r in rankings.items():
        g = gex_data[key]
        st = r['strikes']
        dte = get_dte(r['name'], DATA_D)

        # Top strikes
        top_vol = sorted(st, key=lambda x: x['tv'], reverse=True)[:5]
        top_doi_c = sorted(st, key=lambda x: abs(x['dco']), reverse=True)[:3]
        top_doi_p = sorted(st, key=lambda x: abs(x['dpo']), reverse=True)[:3]
        top_div_c = sorted([s for s in st if s['c_ivd'] is not None], key=lambda x: abs(x['c_ivd']), reverse=True)[:3]
        top_cp = sorted(st, key=lambda x: abs(x['cp_pct']), reverse=True)[:3]
        top_pp = sorted(st, key=lambda x: abs(x['pp_pct']), reverse=True)[:3]

        # Top OI entry combined
        oi_combined = sorted(st, key=lambda x: (x['dco'] + x['dpo']), reverse=True)[:5]

        # Call com maior alta / baixa
        calls_up = sorted([s for s in st if s['cp_pct'] > 0], key=lambda x: x['cp_pct'], reverse=True)
        calls_down = sorted([s for s in st if s['cp_pct'] < 0], key=lambda x: x['cp_pct'])
        puts_up = sorted([s for s in st if s['pp_pct'] > 0], key=lambda x: x['pp_pct'], reverse=True)
        puts_down = sorted([s for s in st if s['pp_pct'] < 0], key=lambda x: x['pp_pct'])

        # Skew ATM
        skew_atm = round(r['iv_p_atm'] - r['iv_c_atm'], 2) if r['iv_p_atm'] and r['iv_c_atm'] else None

        # Volume D total e variação %
        vol_total = r['cvol'] + r['pvol']
        vol_total1 = r['cvol1'] + r['pvol1']
        vol_pct = round(((vol_total - vol_total1) / max(vol_total1, 1)) * 100, 1) if vol_total1 > 0 else 0.0

        # Regime
        flip = g['flip']
        regime = "Long Gamma" if (flip is None or spot >= (flip or 0)) else "Short Gamma"

        # Narrative generation
        narrative = build_narrative(r, g, spot, dte, top_vol, top_doi_c, top_doi_p, 
                                     calls_up, calls_down, puts_up, puts_down,
                                     skew_atm, vol_pct, regime, flip)

        insights.append({
            'key': key, 'name': r['name'], 'color': r['color'],
            'dte': dte,
            'iv_c_atm': r['iv_c_atm'], 'div_c_atm': r['div_c_atm'],
            'iv_p_atm': r['iv_p_atm'], 'div_p_atm': r['div_p_atm'],
            'mp': r['mp'],
            'pcr_oi': r['pcr_oi'], 'dpcr_oi': r['dpcr_oi'],
            'pcr_vol': r['pcr_vol'],
            'gex_net': g['net'], 'flip': flip, 'regime': regime,
            'dom_s': g['dom_s'], 'dom_n': g['dom_n'],
            'coi': r['coi'], 'poi': r['poi'],
            'dcoi': r['dcoi'], 'dpoi': r['dpoi'],
            'cvol': r['cvol'], 'pvol': r['pvol'],
            'd_cvol': r['d_cvol'], 'd_pvol': r['d_pvol'], 'd_tvol': r['d_tvol'],
            'vol_total': vol_total, 'vol_pct': vol_pct,
            'skew_atm': skew_atm,
            'top_vol': top_vol,
            'top_doi_c': top_doi_c, 'top_doi_p': top_doi_p,
            'top_div_c': top_div_c,
            'calls_up': calls_up[:2] if calls_up else [],
            'calls_down': calls_down[:2] if calls_down else [],
            'puts_up': puts_up[:2] if puts_up else [],
            'puts_down': puts_down[:2] if puts_down else [],
            'oi_combined': oi_combined,
            'narrative': narrative,
        })
    return insights


def build_narrative(r, g, spot, dte, top_vol, top_doi_c, top_doi_p,
                    calls_up, calls_down, puts_up, puts_down,
                    skew_atm, vol_pct, regime, flip):
    """Gera texto narrativo automático para cada vencimento."""
    parts = []

    # DTE context
    if dte is not None:
        if dte <= 5:
            parts.append(f'Vencimento muito <span class="highlight">curto ({dte} DTE)</span>')
        elif dte <= 15:
            parts.append(f'Vencimento de <span class="highlight">médio prazo ({dte} DTE)</span>')
        else:
            parts.append(f'Vencimento mais longo (<span class="highlight">{dte} DTE</span>)')

    # IV
    div = r['div_c_atm']
    if abs(div) >= 1.5:
        parts.append(f'com <span class="highlight red">queda acentuada de IV</span> ({div:+.2f}pp call, {r["div_p_atm"]:+.2f}pp put) — típico theta crush de fim de semana')
    elif abs(div) >= 0.5:
        parts.append(f'com <span class="highlight">IV em queda moderada</span> ({div:+.2f}pp call, {r["div_p_atm"]:+.2f}pp put)')
    else:
        parts.append(f'com <span class="highlight">IV relativamente estável</span> ({div:+.2f}pp call)')

    # OI major moves
    if top_doi_c:
        best = top_doi_c[0]
        if abs(best['dco']) >= 1_000_000:
            direction = "explodiu em OI" if best['dco'] > 0 else "teve saída massiva de OI"
            parts.append(f'O <span class="highlight">K{best["s"]:.0f} {direction}</span> em calls ({fP(best["dco"])})')
    if top_doi_p:
        best = top_doi_p[0]
        if abs(best['dpo']) >= 1_000_000:
            direction = "ganhou OI massivo" if best['dpo'] > 0 else "perdeu OI massivo"
            parts.append(f'e <span class="highlight">K{best["s"]:.0f} {direction}</span> em puts ({fP(best["dpo"])})')

    # PCR
    dpcr = r['dpcr_oi']
    if abs(dpcr) >= 0.1:
        if dpcr < 0:
            parts.append(f'O <span class="highlight">PCR OI caiu de {r["pcr_oi1"]:.2f} para {r["pcr_oi"]:.2f}</span>, mostrando que calls ganharam participação relativa')
        else:
            parts.append(f'O <span class="highlight">PCR OI subiu de {r["pcr_oi1"]:.2f} para {r["pcr_oi"]:.2f}</span>, indicando aumento de puts relativo')

    # Volume
    if abs(vol_pct) >= 15:
        if vol_pct > 0:
            parts.append(f'O <span class="highlight grn">volume subiu {vol_pct:.0f}%</span> vs D-1 ({fP(r["d_tvol"])})')
        else:
            parts.append(f'O <span class="highlight red">volume caiu {abs(vol_pct):.0f}%</span> vs D-1 ({fP(r["d_tvol"])})')

    # Top volume strike
    if top_vol:
        parts.append(f'com <span class="highlight">K{top_vol[0]["s"]:.0f} como o mais negociado ({fI(top_vol[0]["tv"])})</span>')

    # MaxPain distance
    mp_dist = abs(spot - r['mp'])
    if mp_dist >= 5:
        parts.append(f'Com <span class="highlight">MaxPain em K{r["mp"]:.0f}</span> e spot em {spot:.0f}, há uma <span class="highlight red">distância significativa ({mp_dist:.0f} pontos)</span>')
    else:
        parts.append(f'<span class="highlight">MaxPain em K{r["mp"]:.0f}</span> próximo do spot ({spot:.0f})')

    # GEX / Regime
    gex_str = fP(g['net'])
    if g['net'] >= 0:
        parts.append(f'O <span class="highlight grn">GEX positivo ({gex_str})</span> com flip em {flip} confirma <span class="highlight grn">regime {regime}</span> — mercado tende a ficar contido')
    else:
        parts.append(f'O <span class="highlight red">GEX negativo ({gex_str})</span> com flip em {flip} indica <span class="highlight red">regime {regime}</span> — movimentos podem ser amplificados')

    # Skew
    if skew_atm is not None and abs(skew_atm) >= 1.0:
        parts.append(f'O <span class="highlight org">skew put-call ATM de {skew_atm:+.2f}pp</span> mostra que o mercado precifica {"mais risco de queda" if skew_atm > 0 else "mais demanda por calls"}')

    return '. '.join(parts) + '.'


# ═══════════════════════════════════════
# HTML TEMPLATE
# ═══════════════════════════════════════
def generate_html(insights, spot, agg_gex_total, agg_flip, agg_regime, total_oi, total_vol, DATA_D1, DATA_D):
    """Gera HTML completo de insights."""
    COLORS = {
        'red': ('var(--red)', 'rgba(207,34,46,.08)'),
        'blu': ('var(--blu)', 'rgba(9,105,218,.08)'),
        'grn': ('var(--grn)', 'rgba(26,127,55,.08)'),
        'yel': ('var(--yel)', 'rgba(154,103,0,.08)'),
        'pur': ('var(--pur)', 'rgba(130,80,223,.08)'),
        'org': ('var(--org)', 'rgba(188,76,0,.08)'),
        'cyn': ('var(--cyn)', 'rgba(9,105,218,.08)'),
    }

    def gf(n):
        return f"+{n/1e6:.1f}M" if n >= 0 else f"−{abs(n)/1e6:.1f}M"

    avg_iv = sum(i['iv_c_atm'] for i in insights) / len(insights) if insights else 0
    total_dcoi = sum(i['dcoi'] for i in insights)
    total_dpoi = sum(i['dpoi'] for i in insights)
    total_doi = total_dcoi + total_dpoi
    total_dvol = sum(i['d_tvol'] for i in insights)
    regime_color = "var(--grn)" if agg_regime == "Long Gamma" else "var(--red)"
    regime_class = "pos" if agg_regime == "Long Gamma" else "neg"

    def metric_card(label, value, detail, tone=""):
        return (
            f'<div class="summary-card"><div class="sc-label">{label}</div>'
            f'<div class="sc-value {tone}">{value}</div><div class="sc-detail">{detail}</div></div>'
        )

    summary_cards_html = "".join([
        metric_card("Regime agregado", agg_regime, f"Flip {agg_flip if agg_flip is not None else '–'} · Spot {spot:.2f}", regime_class),
        metric_card("GEX agregado", gf(agg_gex_total), "Exposição líquida consolidada", css_class(agg_gex_total)),
        metric_card("OI total", fI(total_oi), f"Call {fI(sum(i['coi'] for i in insights))} · Put {fI(sum(i['poi'] for i in insights))}"),
        metric_card("ΔOI líquido", fP(total_doi), f"ΔCall {fP(total_dcoi)} · ΔPut {fP(total_dpoi)}", css_class(total_doi)),
        metric_card("ΔVolume", fP(total_dvol), f"Volume D {fI(total_vol)} · IV média {avg_iv:.1f}%", css_class(total_dvol)),
    ])

    selector_items = []
    exp_sections = []
    for idx, ins in enumerate(insights):
        color_main, color_bg = COLORS.get(ins['color'], ('var(--blu)', 'rgba(9,105,218,.08)'))
        dte_str = f"{ins['dte']} DTE" if ins['dte'] is not None else "DTE n/d"
        mp_gap = abs(spot - ins['mp'])
        gap_note = "max pain próximo do spot" if mp_gap <= 3 else f"max pain afastado em {mp_gap:.0f} pts"
        headline = f"{ins['regime']} com dominante em K{ins['dom_s']:.0f}; {gap_note}."

        selector_items.append(
            f'<button class="exp-chip{" active" if idx == 0 else ""}" data-target="exp-{idx}">'
            f'<strong>{ins["name"]}</strong><span>{dte_str}</span></button>'
        )

        oi_rows = [
            f'<div class="detail-row"><span>OI Call</span><strong>{fI(ins["coi"])} <em class="{css_class(ins["dcoi"])}">{fP(ins["dcoi"])}</em></strong></div>',
            f'<div class="detail-row"><span>OI Put</span><strong>{fI(ins["poi"])} <em class="{css_class(ins["dpoi"])}">{fP(ins["dpoi"])}</em></strong></div>',
        ]
        if ins['top_doi_c']:
            base = ins['top_doi_c'][0]
            oi_rows.append(
                f'<div class="detail-row"><span>{"Entrada OI Call" if base["dco"] >= 0 else "Saída OI Call"}</span>'
                f'<strong class="{css_class(base["dco"])}">K{base["s"]:.0f} · {fP(base["dco"])}</strong></div>'
            )
        if ins['top_doi_p']:
            base = ins['top_doi_p'][0]
            oi_rows.append(
                f'<div class="detail-row"><span>{"Entrada OI Put" if base["dpo"] >= 0 else "Saída OI Put"}</span>'
                f'<strong class="{css_class(base["dpo"])}">K{base["s"]:.0f} · {fP(base["dpo"])}</strong></div>'
            )

        vol_rows = [
            f'<div class="detail-row"><span>Volume total</span><strong>{fI(ins["vol_total"])} <em class="{css_class(ins["d_tvol"])}">{fP(ins["d_tvol"])}</em></strong></div>',
            f'<div class="detail-row"><span>Volume call</span><strong>{fI(ins["cvol"])} <em class="{css_class(ins["d_cvol"])}">{fP(ins["d_cvol"])}</em></strong></div>',
            f'<div class="detail-row"><span>Volume put</span><strong>{fI(ins["pvol"])} <em class="{css_class(ins["d_pvol"])}">{fP(ins["d_pvol"])}</em></strong></div>',
        ]
        if ins['top_vol']:
            vol_rows.append(
                f'<div class="detail-row"><span>Strike mais negociado</span><strong>K{ins["top_vol"][0]["s"]:.0f} · {fI(ins["top_vol"][0]["tv"])}</strong></div>'
            )

        iv_rows = [
            f'<div class="detail-row"><span>IV Call ATM</span><strong>{ins["iv_c_atm"]}% <em class="{css_class(ins["div_c_atm"])}">{ins["div_c_atm"]:+.2f}pp</em></strong></div>',
            f'<div class="detail-row"><span>IV Put ATM</span><strong>{ins["iv_p_atm"]}% <em class="{css_class(ins["div_p_atm"])}">{ins["div_p_atm"]:+.2f}pp</em></strong></div>',
        ]
        if ins['skew_atm'] is not None:
            iv_rows.append(
                f'<div class="detail-row"><span>Skew ATM</span><strong class="{css_class(ins["skew_atm"])}">{ins["skew_atm"]:+.2f}pp</strong></div>'
            )
        if ins['top_div_c']:
            base = ins['top_div_c'][0]
            if base['c_ivd'] is not None:
                iv_rows.append(
                    f'<div class="detail-row"><span>Maior variação IV Call</span><strong class="{css_class(base["c_ivd"])}">K{base["s"]:.0f} · {base["c_ivd"]:+.2f}pp</strong></div>'
                )

        price_rows = []
        if ins['calls_up']:
            base = ins['calls_up'][0]
            price_rows.append(f'<div class="detail-row"><span>Call em alta</span><strong class="pos">K{base["s"]:.0f} · {fPct(base["cp_pct"])}</strong></div>')
        if ins['calls_down']:
            base = ins['calls_down'][0]
            price_rows.append(f'<div class="detail-row"><span>Call em baixa</span><strong class="neg">K{base["s"]:.0f} · {fPct(base["cp_pct"])}</strong></div>')
        if ins['puts_up']:
            base = ins['puts_up'][0]
            price_rows.append(f'<div class="detail-row"><span>Put em alta</span><strong class="pos">K{base["s"]:.0f} · {fPct(base["pp_pct"])}</strong></div>')
        if ins['puts_down']:
            base = ins['puts_down'][0]
            price_rows.append(f'<div class="detail-row"><span>Put em baixa</span><strong class="neg">K{base["s"]:.0f} · {fPct(base["pp_pct"])}</strong></div>')
        if not price_rows:
            price_rows.append('<div class="detail-row"><span>Preço</span><strong>Sem destaque relevante</strong></div>')

        volume_focus_html = ""
        if ins['top_vol']:
            max_tv = max(ins['top_vol'][0]['tv'], 1)
            bars = []
            for tv in ins['top_vol'][:5]:
                width = round(tv['tv'] / max_tv * 100)
                bars.append(
                    f'<div class="bar-row"><span>K{tv["s"]:.0f}</span><div class="bar-track">'
                    f'<div class="bar-fill" style="width:{width}%;background:{color_main}"></div></div>'
                    f'<strong>{fI(tv["tv"])}</strong></div>'
                )
            volume_focus_html = (
                '<div class="detail-card detail-wide"><h4>Top volume por strike</h4>'
                f'<div class="bar-list">{"".join(bars)}</div></div>'
            )

        exp_sections.append(
            f'''
  <article class="exp-panel{" active" if idx == 0 else ""}" id="exp-{idx}">
    <div class="exp-head">
      <div>
        <div class="exp-eyebrow" style="color:{color_main}">{dte_str}</div>
        <h3>{ins["name"]}</h3>
        <p class="exp-headline">{headline}</p>
      </div>
      <div class="exp-meta">
        <div class="mini-stat"><span>GEX</span><strong class="{css_class(ins["gex_net"])}">{gf(ins["gex_net"])}</strong></div>
        <div class="mini-stat"><span>Flip</span><strong>{ins["flip"] if ins["flip"] is not None else "–"}</strong></div>
        <div class="mini-stat"><span>Max Pain</span><strong>K{ins["mp"]:.0f}</strong></div>
        <div class="mini-stat"><span>PCR OI</span><strong>{ins["pcr_oi"]}</strong></div>
      </div>
    </div>

    <div class="brief-grid">
      <div class="brief-card">
        <div class="brief-label">Regime</div>
        <div class="brief-value {css_class(ins["gex_net"])}">{ins["regime"]}</div>
        <div class="brief-detail">Dominante em K{ins["dom_s"]:.0f}</div>
      </div>
      <div class="brief-card">
        <div class="brief-label">IV Call ATM</div>
        <div class="brief-value {css_class(ins["div_c_atm"])}">{ins["iv_c_atm"]}%</div>
        <div class="brief-detail">Δ {ins["div_c_atm"]:+.2f}pp</div>
      </div>
      <div class="brief-card">
        <div class="brief-label">OI líquido</div>
        <div class="brief-value {css_class(ins["dcoi"] + ins["dpoi"])}">{fP(ins["dcoi"] + ins["dpoi"])}</div>
        <div class="brief-detail">Call {fP(ins["dcoi"])} · Put {fP(ins["dpoi"])}</div>
      </div>
      <div class="brief-card">
        <div class="brief-label">Volume</div>
        <div class="brief-value {css_class(ins["d_tvol"])}">{fI(ins["vol_total"])}</div>
        <div class="brief-detail">Δ {fP(ins["d_tvol"])}</div>
      </div>
    </div>

    <details class="exp-details">
      <summary>Ver leitura completa</summary>
      <div class="details-grid">
        <div class="detail-card"><h4>Open interest</h4>{"".join(oi_rows)}</div>
        <div class="detail-card"><h4>Volume</h4>{"".join(vol_rows)}</div>
        <div class="detail-card"><h4>Volatilidade implícita</h4>{"".join(iv_rows)}</div>
        <div class="detail-card"><h4>Preço</h4>{"".join(price_rows)}</div>
        {volume_focus_html}
        <div class="detail-card detail-wide">
          <h4>Leitura do vencimento</h4>
          <p class="narrative">{ins["narrative"]}</p>
        </div>
      </div>
    </details>
  </article>'''
        )

    dom_exp = max(insights, key=lambda x: abs(x['gex_net']))
    dom_pct = round(abs(dom_exp['gex_net']) / max(abs(agg_gex_total), 1) * 100)
    key_levels = sorted({level for ins in insights for level in (ins['mp'], ins['dom_s'])})
    levels_str = ", ".join(f"K{level:.0f}" for level in key_levels)

    avg_div = sum(i['div_c_atm'] for i in insights) / len(insights) if insights else 0
    min_div = min(i['div_c_atm'] for i in insights)
    max_div = max(i['div_c_atm'] for i in insights)
    max_name = max(insights, key=lambda x: abs(x['div_c_atm']))['name']
    building = [i for i in insights if i['dcoi'] > 0 and i['dpoi'] > 0 and i['key'] != dom_exp['key']]

    conclusion_points = [
        f'Regime agregado em <b class="{regime_class}">{agg_regime}</b>, com spot {spot:.2f} {"acima" if spot >= (agg_flip or 0) else "abaixo"} do flip {agg_flip if agg_flip is not None else "–"}.',
        f'IV call ATM média em {avg_iv:.1f}% e variação média de {avg_div:+.1f}pp; o extremo do dia ficou em <b>{max_name}</b>.',
        f'Vencimento dominante: <b>{dom_exp["name"]}</b>, concentrando {dom_pct}% do GEX agregado.',
        f'Faixa de níveis a monitorar: <b>{levels_str}</b>.',
    ]
    if building:
        conclusion_points.insert(3, f'Construção simultânea de OI em <b>{", ".join(i["name"] for i in building)}</b>, sugerindo rolagem ou abertura nova.')

    conclusion_html = "".join(f"<li>{item}</li>" for item in conclusion_points)
    exp_html = "\n".join(exp_sections)
    selector_html = "".join(selector_items)

    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — Insights __DATA_D1__ → __DATA_D__ __ANO__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script>
(function(){
  const saved = localStorage.getItem('bova11-theme');
  if(saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
})();
</script>
<style>
:root{
  --bg:#f3f5f7; --surface:#ffffff; --surface-2:#f7f9fb; --surface-3:#eef3f8;
  --border:#dce2e8; --text:#111827; --muted:#5f6b7a; --soft:#7a8696;
  --blu:#2563eb; --grn:#15803d; --red:#c2413b; --yel:#b7791f; --pur:#8250df; --org:#bc4c00; --cyn:#0f7b9b;
  --font:'Instrument Sans',system-ui,sans-serif; --mono:'JetBrains Mono',monospace;
  --shadow:0 18px 45px rgba(17,24,39,.06);
}
[data-theme="dark"]{
  --bg:#0f141b; --surface:#141b23; --surface-2:#19212b; --surface-3:#1f2933;
  --border:#273240; --text:#e5ebf3; --muted:#a3afbf; --soft:#7d8a9b;
  --blu:#6ea8ff; --grn:#4ade80; --red:#f87171; --yel:#f6c768; --pur:#b388ff; --org:#ff8a65; --cyn:#67e8f9;
  --shadow:0 22px 50px rgba(0,0,0,.28);
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font);-webkit-font-smoothing:antialiased}
button{font:inherit}
.page{max-width:1320px;margin:0 auto;padding:28px 24px 44px}
.page-header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:18px}
.page-copy{max-width:72ch}
.page-kicker{font:.72rem var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--soft);margin-bottom:10px}
.page-header h1{font-size:1.72rem;letter-spacing:-.05em;line-height:1.05}
.page-desc{margin-top:8px;color:var(--muted);line-height:1.55}
.theme-toggle{border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:999px;padding:9px 14px;cursor:pointer;transition:border-color .2s,color .2s}
.theme-toggle:hover{border-color:var(--blu);color:var(--blu)}
.theme-toggle.hidden{display:none}
.summary-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:14px}
.summary-card,.conclusion-box,.selector-wrap,.exp-panel{background:var(--surface);border:1px solid var(--border);border-radius:22px;box-shadow:var(--shadow)}
.summary-card{padding:14px 16px}
.sc-label{font:.68rem var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--soft)}
.sc-value{margin-top:8px;font-size:1.28rem;font-weight:700;letter-spacing:-.04em}
.sc-detail{margin-top:6px;font-size:.8rem;color:var(--muted);line-height:1.45}
.conclusion-box{padding:18px 20px;margin-bottom:14px}
.conclusion-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-end;margin-bottom:12px}
.conclusion-head h2{font-size:1.06rem;letter-spacing:-.03em}
.conclusion-head span{font:.72rem var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--soft)}
.conclusion-list{display:grid;gap:10px;padding-left:18px}
.conclusion-list li{color:var(--muted);line-height:1.55}
.selector-wrap{padding:16px 18px;margin-bottom:14px}
.selector-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-end;margin-bottom:12px}
.selector-head h3{font-size:1.02rem;letter-spacing:-.03em}
.selector-head p{font-size:.82rem;color:var(--muted)}
.exp-selector{display:flex;gap:8px;flex-wrap:wrap}
.exp-chip{border:1px solid var(--border);background:var(--surface);color:var(--muted);border-radius:999px;padding:10px 14px;cursor:pointer;display:flex;flex-direction:column;gap:3px;transition:border-color .2s,background .2s,color .2s}
.exp-chip strong{font-size:.9rem;color:var(--text);font-weight:600}
.exp-chip span{font:.72rem var(--mono);color:var(--soft)}
.exp-chip.active{background:rgba(37,99,235,.08);border-color:rgba(37,99,235,.2)}
.exp-panel{display:none;padding:18px 20px}
.exp-panel.active{display:block}
.exp-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;padding-bottom:16px;border-bottom:1px solid var(--border)}
.exp-eyebrow{font:.72rem var(--mono);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px}
.exp-head h3{font-size:1.2rem;letter-spacing:-.04em}
.exp-headline{margin-top:6px;color:var(--muted);line-height:1.55;max-width:62ch}
.exp-meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;min-width:min(520px,100%)}
.mini-stat{padding:10px 12px;border:1px solid var(--border);border-radius:16px;background:var(--surface-2)}
.mini-stat span{display:block;font:.68rem var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--soft)}
.mini-stat strong{display:block;margin-top:6px;font-size:.95rem}
.brief-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}
.brief-card{padding:14px;border:1px solid var(--border);border-radius:18px;background:var(--surface-2)}
.brief-label{font:.68rem var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--soft)}
.brief-value{margin-top:8px;font-size:1.08rem;font-weight:700;letter-spacing:-.03em}
.brief-detail{margin-top:6px;font-size:.8rem;color:var(--muted);line-height:1.45}
.exp-details{border-top:1px solid var(--border);padding-top:14px}
.exp-details summary{cursor:pointer;list-style:none;font-weight:600;color:var(--text)}
.exp-details summary::-webkit-details-marker{display:none}
.details-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
.detail-card{padding:16px;border:1px solid var(--border);border-radius:18px;background:var(--surface-2)}
.detail-card.detail-wide{grid-column:1 / -1}
.detail-card h4{font:.78rem var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--soft);margin-bottom:10px}
.detail-row{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-top:1px solid var(--border)}
.detail-row:first-child{padding-top:0;border-top:none}
.detail-row span{color:var(--muted);font-size:.82rem}
.detail-row strong{font:600 .85rem var(--mono)}
.detail-row em{font-style:normal;margin-left:6px}
.bar-list{display:grid;gap:8px}
.bar-row{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center}
.bar-row span,.bar-row strong{font:.8rem var(--mono)}
.bar-track{height:10px;border-radius:999px;background:var(--surface-3);overflow:hidden}
.bar-fill{height:100%;border-radius:999px}
.narrative{color:var(--muted);line-height:1.65;font-size:.92rem}
.pos{color:var(--grn)} .neg{color:var(--red)} .neut{color:var(--muted)}
.disclaimer{text-align:center;color:var(--soft);font:.72rem var(--mono);margin-top:26px}
@media(max-width:1180px){.summary-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:980px){.exp-meta,.brief-grid,.details-grid{grid-template-columns:1fr 1fr}}
@media(max-width:760px){
  .page{padding:20px 14px 32px}
  .page-header,.exp-head{flex-direction:column}
  .summary-grid,.exp-meta,.brief-grid,.details-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="page">
  <header class="page-header">
    <div class="page-copy">
      <div class="page-kicker">Hoje / Insights</div>
      <h1>Insights analíticos</h1>
      <p class="page-desc">Comparativo entre __DATA_D1__ e __DATA_D__/__ANO__ · Spot __SPOT__ · leitura rápida do dia no topo e detalhe por vencimento sob demanda.</p>
    </div>
    <div><button id="theme-toggle" class="theme-toggle" onclick="toggleTheme()">Tema</button></div>
  </header>

  <section class="summary-grid">__SUMMARY_CARDS__</section>

  <section class="conclusion-box">
    <div class="conclusion-head">
      <div><span>Conclusão global</span><h2>O que merece atenção hoje</h2></div>
      <div class="page-kicker">Spot __SPOT__</div>
    </div>
    <ul class="conclusion-list">__CONCLUSION_LIST__</ul>
  </section>

  <section class="selector-wrap">
    <div class="selector-head">
      <div><h3>Por vencimento</h3><p>Selecione um vencimento para manter a leitura focada.</p></div>
    </div>
    <div class="exp-selector">__SELECTOR_HTML__</div>
  </section>

  __EXP_PANELS__

  <div class="disclaimer">Dados de fechamento B3 · Geração automática · Não constitui recomendação de investimento</div>
</div>

<script>
document.querySelectorAll('.exp-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const target = chip.dataset.target;
    document.querySelectorAll('.exp-chip').forEach(item => item.classList.remove('active'));
    document.querySelectorAll('.exp-panel').forEach(panel => panel.classList.remove('active'));
    chip.classList.add('active');
    const panel = document.getElementById(target);
    if (panel) panel.classList.add('active');
  });
});

(function(){
  const embedded = window.self !== window.top;
  const btn = document.getElementById('theme-toggle');
  if (embedded && btn) btn.classList.add('hidden');
})();

function toggleTheme(){
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  if(dark){
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('bova11-theme','light');
  } else {
    document.documentElement.setAttribute('data-theme','dark');
    localStorage.setItem('bova11-theme','dark');
  }
}
</script>
</body>
</html>"""

    replacements = {
        "__DATA_D1__": DATA_D1,
        "__DATA_D__": DATA_D,
        "__ANO__": ANO,
        "__SPOT__": f"{spot:.2f}",
        "__SUMMARY_CARDS__": summary_cards_html,
        "__CONCLUSION_LIST__": conclusion_html,
        "__SELECTOR_HTML__": selector_html,
        "__EXP_PANELS__": exp_html,
    }
    for key, value in replacements.items():
        html = html.replace(key, str(value))

    out = os.path.join(OUTPUT_DIR, f"bova11_insights_{DATA_D1.replace('/', '')}_vs_{DATA_D.replace('/', '')}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n  ✅ HTML (Insights): {out}")


# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════
def main():
    print("=" * 65)
    print("  BOVA11 Insights — Versão Automatizada")
    print("=" * 65)
    print()
    
    # Auto-descobre as datas
    print("  🔍 Detectando datas nos arquivos CSV...")
    TAG_D, TAG_D1 = discover_dates()
    
    DATA_D = format_date_label(TAG_D)
    DATA_D1 = format_date_label(TAG_D1) if TAG_D1 else DATA_D
    
    print(f"  📅 D-1 detectado: {DATA_D1} (tag: {TAG_D1 or 'N/A'})")
    print(f"  📅 D detectado:   {DATA_D} (tag: {TAG_D})")
    print()
    
    # Solicita o spot
    SPOT = ask_spot()
    
    print()
    print("=" * 65)
    print("  PROCESSANDO DADOS")
    print("=" * 65)
    print(f"  D-1 : {DATA_D1}/{ANO}")
    print(f"  D   : {DATA_D}/{ANO}")
    print(f"  Dir : {os.path.abspath(CSV_DIR)}")
    print(f"  Spot: {SPOT:.2f}")
    print()

    exp_files = discover_expirations(TAG_D, TAG_D1)
    if not exp_files:
        print(f"⚠ Nenhum vencimento encontrado para TAG_D='{TAG_D}' em '{CSV_DIR}'.")
        sys.exit(1)

    mon_map = {'jan':1,'fev':2,'mar':3,'abr':4,'mai':5,'jun':6,'jul':7,'ago':8,'set':9,'out':10,'nov':11,'dez':12}
    def exp_sort_key(label):
        m = re.search(r'(\d{1,2})\s*([A-Za-zç]{3})', label.lower())
        if m: return (mon_map.get(m.group(2)[:3], 99), int(m.group(1)), label)
        return (99, 99, label)
    exp_files.sort(key=lambda e: exp_sort_key(e["label"]))

    PALETTE = ["red", "blu", "grn", "yel", "pur", "org", "cyn"]
    VENCIMENTOS = []
    for i, e in enumerate(exp_files):
        key = f"V{i+1}"
        VENCIMENTOS.append({
            "key": key, "name": e["label"],
            "fech_csv":    os.path.join(CSV_DIR, e["fech_d"]),
            "vol_csv":     os.path.join(CSV_DIR, e["vol_d"]) if e["vol_d"] else None,
            "fech_csv_d1": os.path.join(CSV_DIR, e["fech_d1"]) if e["fech_d1"] else None,
            "vol_csv_d1":  os.path.join(CSV_DIR, e["vol_d1"]) if e["vol_d1"] else None,
            "color": PALETTE[i % len(PALETTE)],
        })

    def has_valid_volume(vol_map):
        if not vol_map:
            return False
        return any((v.get('cv', 0) + v.get('pv', 0)) > 0 for v in vol_map.values())

    fech_d, vol_d, fech_d1, vol_d1 = {}, {}, {}, {}
    valid_vencimentos = []
    for v in VENCIMENTOS:
        k = v["key"]
        fech_d[k]  = parse_fech_csv(v["fech_csv"])
        vol_d[k]   = parse_vol_csv(v["vol_csv"]) if v.get("vol_csv") else {}
        fech_d1[k] = parse_fech_csv(v["fech_csv_d1"]) if v.get("fech_csv_d1") else []
        vol_d1[k]  = parse_vol_csv(v["vol_csv_d1"]) if v.get("vol_csv_d1") else {}
        s1 = f"OK ({len(fech_d1[k])} strikes)" if fech_d1[k] else "N/A (snapshot)"
        vol_state = "OK" if has_valid_volume(vol_d[k]) else "sem volume válido"
        if fech_d[k] and has_valid_volume(vol_d[k]):
            valid_vencimentos.append(v)
            print(f"  ✓ {k}: {v['name']:20s} | D: {len(fech_d[k]):3d} | D-1: {s1} | Vol: {vol_state}")
        else:
            print(f"  ⚠ {k}: {v['name']:20s} | D: {len(fech_d[k]):3d} | D-1: {s1} | Vol: {vol_state} — removido")

    if not valid_vencimentos:
        print("\n  ERRO: nenhum vencimento com fechamento e volume válidos.")
        sys.exit(1)

    VENCIMENTOS = valid_vencimentos

    print(f"\n  Spot (fornecido): {SPOT:.2f}")

    rankings = {}
    for v in VENCIMENTOS:
        k = v['key']
        rankings[k] = build_exp_data(v, fech_d[k], fech_d1[k], vol_d[k], vol_d1[k], SPOT)

    # GEX
    gex_by_exp = {v['key']: calc_gex(SPOT, fech_d[v['key']]) for v in VENCIMENTOS}
    all_sk = sorted(set(sk for g in gex_by_exp.values() for sk in g))
    all_sk = [s for s in all_sk if GEX_STRIKE_MIN <= s <= GEX_STRIKE_MAX]
    agg_gex = []
    for sk in all_sk:
        c = sum(gex_by_exp[k].get(sk, {}).get('c', 0) for k in gex_by_exp)
        p = sum(gex_by_exp[k].get(sk, {}).get('p', 0) for k in gex_by_exp)
        agg_gex.append({'s': sk, 'c': round(c), 'p': round(p), 'n': round(c + p)})
    agg_flip = find_flip(SPOT, agg_gex)
    agg_regime = "Long Gamma" if (agg_flip is None or SPOT >= (agg_flip or 0)) else "Short Gamma"
    gt_all = sum(round(sum(vv['n'] for vv in gex_by_exp[k].values())) for k in gex_by_exp)
    print(f"  GEX Agregado: {'+' if gt_all >= 0 else ''}{gt_all/1e6:.1f}M | Flip: {agg_flip} | Regime: {agg_regime}")

    # GEX data per expiration
    gex_data = {}
    for v in VENCIMENTOS:
        k = v['key']
        gex_map = gex_by_exp[k]
        strikes_sorted = sorted([s for s in gex_map if GEX_STRIKE_MIN <= s <= GEX_STRIKE_MAX])
        gex_strikes = [{'s': s, 'c': round(gex_map[s]['c']), 'p': round(gex_map[s]['p']), 'n': round(gex_map[s]['n'])} for s in strikes_sorted]
        exp_flip = find_flip(SPOT, gex_strikes)
        exp_dom = max(gex_strikes, key=lambda x: abs(x['n'])) if gex_strikes else {'s': 0, 'n': 0}
        gex_data[k] = {
            'net': round(sum(vv['n'] for vv in gex_map.values())),
            'flip': exp_flip,
            'dom_s': exp_dom['s'],
            'dom_n': exp_dom['n'],
        }

    # Generate insights
    insights = generate_insights(rankings, gex_data, SPOT, DATA_D)

    # Totals
    total_oi = sum(r['coi'] + r['poi'] for r in rankings.values())
    total_vol = sum(r['cvol'] + r['pvol'] for r in rankings.values())

    # Generate HTML
    generate_html(insights, SPOT, gt_all, agg_flip, agg_regime, total_oi, total_vol, DATA_D1, DATA_D)
    print("\n  CONCLUÍDO ✅")


if __name__ == "__main__":
    main()
