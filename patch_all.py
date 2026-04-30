import glob
import re

ROBUST_CODE = """
def is_ticker_match(label, ticker):
    if not label or not ticker: return True
    label_lower = label.lower()
    months = {'jan': 'A', 'fev': 'B', 'mar': 'C', 'abr': 'D', 'mai': 'E', 'jun': 'F',
              'jul': 'G', 'ago': 'H', 'set': 'I', 'out': 'J', 'nov': 'K', 'dez': 'L'}
    exp_month = None
    for m, letter in months.items():
        if m in label_lower:
            exp_month = letter; break
    exp_suffix = ""
    for w in ['w1', 'w2', 'w3', 'w4', 'w5']:
        if w in label_lower:
            exp_suffix = w.upper(); break
    if not exp_month or len(ticker) < 5: return True
    if ticker[4].upper() != exp_month: return False
    if exp_suffix:
        if not ticker.upper().endswith(exp_suffix): return False
    else:
        if ticker[-2:].upper() in ['W1', 'W2', 'W3', 'W4', 'W5']: return False
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
    header = lines[0].strip().replace('\\r', '').split(';')
    ncols = len(header)
    results = []
    
    seen_strikes = set()
    
    for line in lines[1:]:
        line = line.strip().replace('\\r', '')
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
"""

scripts = [
    "scripts/bova11_auto.py",
    "scripts/bova11_insights_auto.py",
    "scripts/bova11_skew_history.py",
    "scripts/bova11_convexity.py",
    "scripts/bova11_trade_score_james.py"
]

for script in scripts:
    with open(script, 'r', encoding='utf-8') as f:
        content = f.read()
        
    if 'def is_ticker_match' in content:
        continue
        
    # We find where def parse_fech_csv(filepath): starts
    # and replace the whole function block.
    # It usually ends just before def parse_vol_csv(filepath):
    
    start_idx = content.find("def parse_fech_csv(filepath):")
    if start_idx == -1:
        continue
        
    end_idx = content.find("def parse_vol_csv(filepath):", start_idx)
    if end_idx == -1:
        # Some scripts might not have parse_vol_csv (trade score?)
        end_idx = content.find("def ", start_idx + 10)
        
    if end_idx == -1:
        print(f"Could not find end of parse_fech_csv in {script}")
        continue
        
    # Replace the block
    new_content = content[:start_idx] + ROBUST_CODE + "\n" + content[end_idx:]
    
    with open(script, 'w', encoding='utf-8') as f:
        f.write(new_content)
        
    print(f"Patched {script}")
