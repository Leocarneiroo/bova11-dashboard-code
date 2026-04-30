import os, glob, re

def get_is_ticker_match_code():
    return """
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
"""

def patch_file(fpath):
    with open(fpath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Se já tem a função, não faz nada
    if 'def is_ticker_match' in content: return

    # Injetar a função logo antes de parse_fech_csv
    content = content.replace('def parse_fech_csv(', get_is_ticker_match_code() + '\ndef parse_fech_csv(')

    # Adicionar extração de label e filtro no loop do parse_fech_csv
    # O loop padrão é:
    #     for line in lines[1:]:
    #         line = line.strip().replace('\r', '')
    #         if not line:
    #             continue
    #         p = line.split(';')

    # Extração de label no início de parse_fech_csv:
    label_extraction = """
    filename = os.path.basename(filepath)
    label = ""
    m = re.match(r'venc_(.+?)_fechamento__', filename)
    if m: label = m.group(1).replace('_', ' ')
    else:
        m = re.match(r'venc (.+?) fechamento', filename)
        if m: label = m.group(1)
"""
    
    # precisamos achar o ínicio do "def parse_fech_csv" até "results = []" 
    # e colocar a extração do label lá.
    if "results = []" in content and "def parse_fech_csv" in content:
        # A lógica vai ser: dentro do for, se tiver p[0], checa is_ticker_match
        pass

    # Actually, a safer way is to rewrite parse_fech_csv completely using regex replace.
    # Mas as colunas variam um pouco.
    print(f"Patched {fpath}")

for script in glob.glob("/Users/leonardocarneiro/Desktop/files/scripts/*.py"):
    if 'bova' in script:
        # patch_file(script)
        pass
