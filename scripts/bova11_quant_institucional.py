#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Quant Institucional — Dashboard de Indicadores Quantitativos
====================================================================
Baixa dados ao vivo do BOVA11 via yfinance e gera um HTML com:
  - Cards de resumo do último fechamento (RevL, MoM_Z, R_pct, Mode)
  - Gráfico histórico de 3 anos com 4 painéis interativos via Chart.js

Uso:
  python3 bova11_quant_institucional.py

Sem input via stdin — script completamente autônomo.

Dependências:
  pip install yfinance pandas numpy
"""

import os
import sys
import json
from datetime import date, timedelta

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError as e:
    print(f"❌ Dependência faltando: {e}")
    print("   Execute: pip3 install yfinance pandas numpy")
    sys.exit(1)

_BASEDIR   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_BASEDIR, '..', 'output')

TICKER = "BOVA11.SA"
BENCH  = "^BVSP"

# ═══════════════════════════════════════
# 1. DOWNLOAD DE DADOS
# ═══════════════════════════════════════

def download_data():
    start = date.today() - timedelta(days=1095)  # 3 anos
    end   = date.today()

    print(f"  📡 Baixando {TICKER} e IBOV ({start} → {end})...")

    bova, ibov = pd.DataFrame(), pd.DataFrame()
    for attempt in range(3):
        try:
            bova = yf.download(TICKER, start=start, end=end, progress=False)
            ibov = yf.download(BENCH,  start=start, end=end, progress=False)
            if not bova.empty:
                break
        except Exception:
            pass
        if attempt < 2:
            import time; time.sleep(3)

    if bova.empty:
        print("  ⚠️ Falha ao baixar dados do BOVA11. Gerando dashboard em modo degradado.")
        return pd.DataFrame()

    # Suporte a MultiIndex (yfinance >= 0.2)
    try:
        df = pd.DataFrame({
            "Close":      bova[('Close', TICKER)],
            "High":       bova[('High',  TICKER)],
            "Low":        bova[('Low',   TICKER)],
            "IBOV_Close": ibov[('Close', BENCH)],
        })
    except KeyError:
        df = pd.DataFrame({
            "Close":      bova['Close'],
            "High":       bova['High'],
            "Low":        bova['Low'],
            "IBOV_Close": ibov['Close'],
        })

    df.dropna(inplace=True)
    print(f"  ✅ {len(df)} pregões carregados.")
    return df

# ═══════════════════════════════════════
# 2. CÁLCULO DOS INDICADORES
# ═══════════════════════════════════════

def calcular_indicadores(df):
    # ATR14
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14, min_periods=14).mean()
    df["ATR20"] = tr.rolling(20, min_periods=20).mean()

    # RevL — Regressão linear 60d - 0.5 * ATR14
    def rolling_reg_last(y):
        n = len(y)
        x = np.arange(n)
        slope, intercept = np.polyfit(x, y, 1)
        return intercept + slope * (n - 1)

    df["Reg60"] = df["Close"].rolling(60, min_periods=60).apply(rolling_reg_last, raw=True)
    df["RevL"]  = df["Reg60"] - 0.5 * df["ATR14"]

    # MoM_Z — Momentum Z-Score
    df["MoM_raw"]     = df["Close"] / df["Close"].shift(20) - 1
    df["MoM_mean_60"] = df["MoM_raw"].rolling(60, min_periods=40).mean()
    df["MoM_std_60"]  = df["MoM_raw"].rolling(60, min_periods=40).std()
    df["MoM_Z"]       = (df["MoM_raw"] - df["MoM_mean_60"]) / df["MoM_std_60"]

    # R_pct — Força Relativa vs IBOV (percentil 252d)
    ret_bova = df["Close"]      / df["Close"].shift(20) - 1
    ret_ibov = df["IBOV_Close"] / df["IBOV_Close"].shift(20) - 1
    df["R_raw"] = ret_bova - ret_ibov

    def rolling_percentile_last(values):
        arr = np.array(values)
        return 100 * np.mean(arr <= arr[-1])

    df["R_pct"] = df["R_raw"].rolling(252, min_periods=80).apply(rolling_percentile_last, raw=True)

    # EMA50 / EMA200 → Trend
    df["EMA50"]  = df["Close"].ewm(span=50,  adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    df["Trend"]  = np.where(df["EMA50"] > df["EMA200"], "Bull", "Bear")

    # VolRegime
    df["ATR20_mean_1y"] = df["ATR20"].rolling(252, min_periods=80).mean()
    df["VolRegime"] = np.where(df["ATR20"] > df["ATR20_mean_1y"], "HighVol", "LowVol")

    # Mode
    def mode_row(row):
        if pd.isna(row["RevL"]) or pd.isna(row["MoM_Z"]):
            return None
        if row["Close"] > row["RevL"] and row["MoM_Z"] > 0:
            return "Verde"
        elif row["Close"] > row["RevL"] and row["MoM_Z"] <= 0:
            return "Verde Claro"
        elif row["Close"] <= row["RevL"] and row["MoM_Z"] <= 0:
            return "Vermelho"
        else:
            return "Rosa"

    df["Mode"] = df.apply(mode_row, axis=1)

    return df

# ═══════════════════════════════════════
# 3. GERAR HTML
# ═══════════════════════════════════════

MODE_CSS = {
    "Verde":       ("--grn",  "#3fb950"),
    "Verde Claro": ("#7ee787", "#7ee787"),
    "Vermelho":    ("--red",  "#f85149"),
    "Rosa":        ("#ff9999", "#ff9999"),
}


def gerar_html_sem_dados(mensagem):
    html = f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOVA11 Quant Institucional</title>
    <style>
        :root {{
            --bg:#ffffff; --bg2:#f6f8fa; --border:#d0d7de; --text:#1f2328; --text2:#636c76;
        }}
        [data-theme="dark"] {{
            --bg:#0d1117; --bg2:#161b22; --border:#30363d; --text:#c9d1d9; --text2:#8b949e;
        }}
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:20px; }}
        .wrap {{ max-width:980px; margin:0 auto; }}
        .box {{ background:var(--bg2); border:1px solid var(--border); border-radius:12px; padding:24px; margin-top:20px; }}
        h1 {{ font-size:1.4rem; margin-bottom:8px; }}
        p {{ color:var(--text2); line-height:1.6; }}
    </style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()" title="Alternar tema" style="position:fixed;top:12px;right:16px;z-index:9999;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:1rem;line-height:1;transition:all 0.2s;">◐</button>
<div class="wrap">
    <h1>📊 Quant Institucional — BOVA11</h1>
    <div class="box">
        <p>{mensagem}</p>
        <p style="margin-top:12px;">Os cards e gráficos que dependem de histórico externo ficam em <strong>N/A</strong> até o próximo pull válido do mercado.</p>
    </div>
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
</html>'''
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, 'bova11_quant_institucional.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return out_path

def gerar_html(df):
    if df is None or df.empty:
        return gerar_html_sem_dados("Não foi possível obter histórico do BOVA11/IBOV via yfinance.")

    plot_data = df.dropna(subset=["RevL", "MoM_Z", "R_pct", "EMA200"]).copy()
    if plot_data.empty:
        return gerar_html_sem_dados("Os dados históricos chegaram incompletos para calcular RevL/MoM/Força Relativa.")

    last = plot_data.iloc[-1]
    last_date = plot_data.index[-1].strftime("%d/%m/%Y")

    mode_val  = last["Mode"] if last["Mode"] else "—"
    mode_color = MODE_CSS.get(mode_val, ("--text", "#c9d1d9"))[1]

    trend_icon = "▲ Alta" if last["Trend"] == "Bull" else "▼ Baixa"
    vol_icon   = "🔴 Alta Vol" if last["VolRegime"] == "HighVol" else "🟢 Baixa Vol"

    # Séries para Chart.js (formato ISO para labels)
    labels = [d.strftime("%Y-%m-%d") for d in plot_data.index]

    def to_js_list(series):
        return json.dumps([round(float(v), 4) if not np.isnan(v) else None for v in series])

    js_labels  = json.dumps(labels)
    js_close   = to_js_list(plot_data["Close"])
    js_revl    = to_js_list(plot_data["RevL"])
    js_ema50   = to_js_list(plot_data["EMA50"])
    js_ema200  = to_js_list(plot_data["EMA200"])
    js_momz    = to_js_list(plot_data["MoM_Z"])
    js_rpct    = to_js_list(plot_data["R_pct"])
    js_atr14   = to_js_list(plot_data["ATR14"])

    html = f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOVA11 Quant Institucional</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
    <style>
        :root {{
            --bg: #ffffff; --bg2: #f6f8fa; --bg3: #eaeef2;
            --text: #1f2328; --text2: #636c76;
            --border: #d0d7de;
            --grn: #1a7f37; --red: #cf222e; --blu: #0969da;
            --yel: #9a6700; --pur: #8250df;
        }}
        [data-theme="dark"] {{
            --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
            --text: #c9d1d9; --text2: #8b949e;
            --border: #30363d;
            --grn: #3fb950; --red: #f85149; --blu: #58a6ff;
            --yel: #d29922; --pur: #bc8cff;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 20px; }}
        h1 {{ font-size: 1.3rem; color: #fff; margin-bottom: 4px; }}
        .subtitle {{ font-size: 0.85rem; color: var(--text2); margin-bottom: 20px; }}
        .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }}
        .card {{
            background: var(--bg2); border: 1px solid var(--border);
            border-radius: 10px; padding: 14px 20px; min-width: 130px; flex: 1;
        }}
        .card-label {{ font-size: 0.72rem; color: var(--text2); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }}
        .card-value {{ font-size: 1.35rem; font-weight: 700; }}
        .card-sub   {{ font-size: 0.78rem; color: var(--text2); margin-top: 4px; }}
        .chart-block {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
        .chart-title {{ font-size: 0.82rem; color: var(--text2); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }}
        .chart-title span {{ font-size: 0.72rem; color: var(--text2); }}
        canvas {{ width: 100% !important; }}
        .reset-btn {{
            background: var(--bg3); border: 1px solid var(--border); color: var(--text2);
            padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 0.72rem;
        }}
        .reset-btn:hover {{ color: var(--text); border-color: var(--blu); }}
    </style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()" title="Alternar tema" style="position:fixed;top:12px;right:16px;z-index:9999;background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:1rem;line-height:1;transition:all 0.2s;">◐</button>
    <h1>📊 Quant Institucional — BOVA11</h1>
    <div class="subtitle">Dados ao vivo · Último fechamento: {last_date}</div>

    <div class="cards">
        <div class="card">
            <div class="card-label">Preço</div>
            <div class="card-value">R$ {last["Close"]:.2f}</div>
            <div class="card-sub">RevL: R$ {last["RevL"]:.2f}</div>
        </div>
        <div class="card">
            <div class="card-label">Momentum (Z)</div>
            <div class="card-value" style="color:{'var(--grn)' if last['MoM_Z'] >= 0 else 'var(--red)'}">
                {last["MoM_Z"]:+.2f}
            </div>
            <div class="card-sub">Z-Score 20d/60d</div>
        </div>
        <div class="card">
            <div class="card-label">Força Relativa</div>
            <div class="card-value" style="color:{'var(--grn)' if last['R_pct'] >= 50 else 'var(--red)'}">
                {last["R_pct"]:.1f}%
            </div>
            <div class="card-sub">Percentil vs IBOV</div>
        </div>
        <div class="card">
            <div class="card-label">Tendência</div>
            <div class="card-value" style="color:{'var(--grn)' if last['Trend']=='Bull' else 'var(--red)'}; font-size:1.1rem;">
                {trend_icon}
            </div>
            <div class="card-sub">EMA50 vs EMA200</div>
        </div>
        <div class="card">
            <div class="card-label">Volatilidade</div>
            <div class="card-value" style="font-size:1rem;">{vol_icon}</div>
            <div class="card-sub">ATR20 vs média 252d</div>
        </div>
        <div class="card">
            <div class="card-label">Mode</div>
            <div class="card-value" style="color:{mode_color}; font-size:1.1rem;">{mode_val}</div>
            <div class="card-sub">RevL + MoM_Z</div>
        </div>
    </div>

    <div class="chart-block">
        <div class="chart-title">Preço vs RevL / EMA50 / EMA200 <span>🖱️ Scroll = zoom · Drag = pan</span><button class="reset-btn" onclick="resetAll()">Reset Zoom</button></div>
        <canvas id="chartPrice" height="220"></canvas>
    </div>
    <div class="chart-block">
        <div class="chart-title">Momentum — Z-Score (20d normalizado 60d)</div>
        <canvas id="chartMom" height="120"></canvas>
    </div>
    <div class="chart-block">
        <div class="chart-title">Força Relativa vs IBOV — Percentil 252d</div>
        <canvas id="chartRS" height="100"></canvas>
    </div>
    <div class="chart-block">
        <div class="chart-title">Volatilidade — ATR14</div>
        <canvas id="chartATR" height="100"></canvas>
    </div>

    <script>
    const LABELS = {js_labels};
    const CLOSE  = {js_close};
    const REVL   = {js_revl};
    const EMA50  = {js_ema50};
    const EMA200 = {js_ema200};
    const MOMZ   = {js_momz};
    const RPCT   = {js_rpct};
    const ATR14  = {js_atr14};

    // Referências dos 4 charts para sincronização
    const ALL_CHARTS = [];

    function syncZoom(ctx) {{
        const src = ctx.chart;
        const {{min, max}} = src.scales.x;
        ALL_CHARTS.forEach(c => {{
            if (c === src) return;
            c.zoomScale('x', {{min, max}}, 'none');
        }});
    }}

    function resetAll() {{
        ALL_CHARTS.forEach(c => c.resetZoom());
    }}

    const ZOOM_PLUGIN = {{
        zoom: {{
            wheel: {{ enabled: true }},
            pinch: {{ enabled: true }},
            mode: 'x',
            onZoom: syncZoom,
        }},
        pan: {{
            enabled: true,
            mode: 'x',
            onPan: syncZoom,
        }}
    }};

    const COMMON_OPTS = {{
        responsive: true,
        animation: false,
        plugins: {{
            legend: {{ labels: {{ color: '#c9d1d9', boxWidth: 12, font: {{ size: 11 }} }} }},
            tooltip: {{ mode: 'index', intersect: false, backgroundColor: '#161b22', titleColor: '#c9d1d9', bodyColor: '#8b949e', borderColor: '#30363d', borderWidth: 1 }},
            zoom: ZOOM_PLUGIN
        }},
        scales: {{
            x: {{
                ticks: {{ color: '#8b949e', maxTicksLimit: 12, maxRotation: 0, font: {{ size: 10 }} }},
                grid: {{ color: '#21262d' }}
            }},
            y: {{
                ticks: {{ color: '#8b949e', font: {{ size: 10 }} }},
                grid: {{ color: '#21262d' }}
            }}
        }}
    }};

    // Gráfico 1: Preço
    ALL_CHARTS.push(new Chart(document.getElementById('chartPrice'), {{
        type: 'line',
        data: {{
            labels: LABELS,
            datasets: [
                {{ label: 'Preço',  data: CLOSE,  borderColor: '#c9d1d9', borderWidth: 1.5, pointRadius: 0, tension: 0.2 }},
                {{ label: 'RevL',   data: REVL,   borderColor: '#58a6ff', borderWidth: 1.5, borderDash: [5,3], pointRadius: 0, tension: 0.2 }},
                {{ label: 'EMA50',  data: EMA50,  borderColor: '#3fb950', borderWidth: 1,   pointRadius: 0, opacity: 0.7 }},
                {{ label: 'EMA200', data: EMA200, borderColor: '#f85149', borderWidth: 1,   pointRadius: 0, opacity: 0.7 }},
            ]
        }},
        options: {{
            ...COMMON_OPTS,
            plugins: {{
                ...COMMON_OPTS.plugins,
                zoom: ZOOM_PLUGIN
            }}
        }}
    }}));

    // Gráfico 2: Momentum (barras)
    const momColors = MOMZ.map(v => v === null ? '#30363d' : v >= 0 ? '#3fb950' : '#f85149');
    ALL_CHARTS.push(new Chart(document.getElementById('chartMom'), {{
        type: 'bar',
        data: {{
            labels: LABELS,
            datasets: [{{ label: 'MoM Z-Score', data: MOMZ, backgroundColor: momColors, borderWidth: 0 }}]
        }},
        options: {{
            ...COMMON_OPTS,
            plugins: {{
                ...COMMON_OPTS.plugins,
                zoom: ZOOM_PLUGIN
            }},
            scales: {{
                ...COMMON_OPTS.scales,
                y: {{
                    ...COMMON_OPTS.scales.y,
                    min: -3.5, max: 3.5
                }}
            }}
        }}
    }}));

    // Gráfico 3: Força Relativa
    ALL_CHARTS.push(new Chart(document.getElementById('chartRS'), {{
        type: 'line',
        data: {{
            labels: LABELS,
            datasets: [{{ label: 'R% vs IBOV', data: RPCT, borderColor: '#d29922', borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false }}]
        }},
        options: {{
            ...COMMON_OPTS,
            plugins: {{
                ...COMMON_OPTS.plugins,
                zoom: ZOOM_PLUGIN
            }},
            scales: {{
                ...COMMON_OPTS.scales,
                y: {{ ...COMMON_OPTS.scales.y, min: 0, max: 100 }}
            }}
        }}
    }}));

    // Gráfico 4: ATR14
    ALL_CHARTS.push(new Chart(document.getElementById('chartATR'), {{
        type: 'line',
        data: {{
            labels: LABELS,
            datasets: [{{ label: 'ATR14', data: ATR14, borderColor: '#bc8cff', borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false }}]
        }},
        options: {{
            ...COMMON_OPTS,
            plugins: {{
                ...COMMON_OPTS.plugins,
                zoom: ZOOM_PLUGIN
            }}
        }}
    }}));
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

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, 'bova11_quant_institucional.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return out_path

# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════

def main():
    print()
    print("=" * 60)
    print("  BOVA11 Quant Institucional — RevL / MoM / Força Relativa")
    print("=" * 60)

    df = download_data()
    if df.empty:
        out_path = gerar_html_sem_dados("Não foi possível obter histórico do BOVA11/IBOV via yfinance.")
        print(f"\n  ⚠️ Dashboard gerado em modo degradado: {os.path.relpath(out_path)}")
        print("=" * 60)
        print()
        return

    df = calcular_indicadores(df)

    last = df.dropna(subset=["RevL", "MoM_Z", "R_pct"]).iloc[-1]
    print(f"\n  📅 Último pregão : {last.name.date()}")
    print(f"  💰 Close         : R$ {last['Close']:.2f}")
    print(f"  📐 RevL          : R$ {last['RevL']:.2f}")
    print(f"  📈 MoM Z-Score   : {last['MoM_Z']:+.2f}")
    print(f"  🔀 Força Rel     : {last['R_pct']:.1f}%")
    print(f"  📊 Trend         : {last['Trend']}")
    print(f"  🌡️  VolRegime     : {last['VolRegime']}")
    print(f"  🎨 Mode          : {last['Mode']}")

    out_path = gerar_html(df)
    print(f"\n  ✅ HTML gerado: {os.path.relpath(out_path)}")
    print("=" * 60)
    print()

if __name__ == '__main__':
    main()
