"""
BOVA11 — Distribuição Estatística do OI em torno do Max Pain
==============================================================
Calcula métricas de concentração, assimetria, curtose,
entropia, Herfindahl (HHI), Gini, ajuste de distribuição
normal e gera dashboard HTML interativo.

Dependências: pandas, numpy, scipy
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from scipy.optimize import curve_fit

try:
    from .bova11_expiry_family import collapse_option_rows_by_strike, filter_expiry_family
except ImportError:
    from bova11_expiry_family import collapse_option_rows_by_strike, filter_expiry_family


# ─────────────────────────────────────────────────────────────
# 1.  PARSER B3  (reutilizado)
# ─────────────────────────────────────────────────────────────

def _p(raw) -> float:
    if not isinstance(raw, str):
        return float(raw) if raw == raw else 0.0
    s = raw.strip().rstrip("%").replace("\r", "")
    if s in ("", "-", "--"):
        return 0.0
    m = 1.0
    if s.endswith("k"):   m = 1_000;    s = s[:-1]
    elif s.endswith("M"): m = 1_000_000; s = s[:-1]
    s = s.replace(".", "").replace(",", ".")
    try:    return float(s) * m
    except: return 0.0


def load_b3(path: Path) -> pd.DataFrame:
    raw = path.read_bytes().decode("latin-1")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    rows = []
    for line in lines[1:]:
        c = line.split(";")
        if len(c) < 23:
            continue
        rows.append({
            "call_ticker": c[0].strip(),
            "strike":     _p(c[11]),
            "call_oi":    _p(c[2]),
            "put_oi":     _p(c[20]),
            "call_delta": _p(c[3]),
            "put_delta":  _p(c[19]),
            "call_gamma": _p(c[4]),
            "put_gamma":  _p(c[18]),
            "call_iv":    _p(c[7]),
            "put_iv":     _p(c[15]),
            "call_last":  _p(c[1]),
            "put_last":   _p(c[21]),
            "put_ticker": c[22].strip(),
        })
    return pd.DataFrame(rows)


def load_b3_vol(path: Path) -> pd.DataFrame:
    raw = path.read_bytes().decode("latin-1")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    rows = []
    for line in lines[1:]:
        c = line.split(";")
        if len(c) < 10:
            continue
        strike = _p(c[4])
        if strike == 0:
            continue
        rows.append({"strike": strike, "call_vol": _p(c[1]),
                     "put_vol": _p(c[9]) if len(c) > 9 else 0})
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────
# 2.  MAX PAIN
# ─────────────────────────────────────────────────────────────

def calc_max_pain(df: pd.DataFrame) -> tuple[pd.DataFrame, float, float]:
    strikes = sorted(df["strike"].unique())
    results = []
    for exp in strikes:
        cl = sum(max(exp - r["strike"], 0) * r["call_oi"] for _, r in df.iterrows())
        pl = sum(max(r["strike"] - exp, 0) * r["put_oi"] for _, r in df.iterrows())
        results.append({"strike": exp, "call_loss": cl, "put_loss": pl,
                        "total_loss": cl + pl})
    curve = pd.DataFrame(results)
    idx = curve["total_loss"].idxmin()
    return curve, float(curve.loc[idx, "strike"]), float(curve.loc[idx, "total_loss"])


# ─────────────────────────────────────────────────────────────
# 3.  STATISTICAL ENGINE
# ─────────────────────────────────────────────────────────────

@dataclass
class OIDistribution:
    """Métricas estatísticas da distribuição de OI."""
    # Básicas
    strikes: list[float] = field(default_factory=list)
    call_oi: list[float] = field(default_factory=list)
    put_oi: list[float] = field(default_factory=list)
    total_oi: list[float] = field(default_factory=list)
    total_oi_sum: float = 0.0

    # Distribuição ponderada
    oi_weighted_mean: float = 0.0       # centro de massa do OI
    oi_weighted_std: float = 0.0        # dispersão ponderada
    call_weighted_mean: float = 0.0
    put_weighted_mean: float = 0.0

    # Momentos
    skewness: float = 0.0              # assimetria (>0 = cauda à direita)
    kurtosis: float = 0.0             # curtose (>3 = leptocúrtica, pico acentuado)
    excess_kurtosis: float = 0.0       # curtose - 3

    # Concentração
    hhi: float = 0.0                   # Herfindahl-Hirschman (0-1, mais alto = mais concentrado)
    hhi_normalized: float = 0.0
    gini: float = 0.0                  # Gini (0 = perfeita igualdade, 1 = concentração total)
    entropy: float = 0.0              # Entropia de Shannon (bits)
    max_entropy: float = 0.0
    entropy_ratio: float = 0.0        # 0 = concentrado, 1 = uniforme

    # Percentis de concentração
    pct_within_1std: float = 0.0       # % do OI dentro de ±1σ do max pain
    pct_within_2std: float = 0.0
    pct_within_1pt: float = 0.0        # % dentro de ±1 strike
    pct_within_2pt: float = 0.0        # % dentro de ±2 strikes

    # Ajuste Normal
    normal_mu: float = 0.0
    normal_sigma: float = 0.0
    normal_amplitude: float = 0.0
    normal_r_squared: float = 0.0      # R² do fit
    normal_fit_y: list[float] = field(default_factory=list)
    residuals: list[float] = field(default_factory=list)

    # Testes
    ks_statistic: float = 0.0          # Kolmogorov-Smirnov
    ks_pvalue: float = 0.0
    is_normal: bool = False            # p > 0.05?

    # Max pain context
    max_pain: float = 0.0
    spot: float = 0.0
    mp_vs_oi_mean: float = 0.0        # distância max pain → centro de massa

    # Interpretation
    concentration_grade: str = ""      # "alta", "moderada", "baixa", "dispersa"
    shape_desc: str = ""               # descrição da forma


def compute_oi_stats(df: pd.DataFrame, max_pain: float, spot: float) -> OIDistribution:
    """Calcula todas as métricas estatísticas da distribuição de OI."""
    d = OIDistribution()
    d.max_pain = max_pain
    d.spot = spot

    # Mantém todos os vetores do dashboard alinhados em ordem crescente de strike.
    df = df[df["strike"] > 0].sort_values("strike").reset_index(drop=True)

    d.strikes = df["strike"].tolist()
    d.call_oi = df["call_oi"].tolist()
    d.put_oi = df["put_oi"].tolist()
    d.total_oi = (df["call_oi"] + df["put_oi"]).tolist()
    d.total_oi_sum = sum(d.total_oi)

    strikes = np.array(d.strikes)
    oi = np.array(d.total_oi)
    call_oi = np.array(d.call_oi)
    put_oi = np.array(d.put_oi)
    n = len(strikes)

    if d.total_oi_sum == 0 or n < 3:
        return d

    # ── Pesos normalizados ──
    w = oi / d.total_oi_sum
    w_call = call_oi / call_oi.sum() if call_oi.sum() > 0 else np.zeros(n)
    w_put = put_oi / put_oi.sum() if put_oi.sum() > 0 else np.zeros(n)

    # ── Média ponderada (centro de massa) ──
    d.oi_weighted_mean = float(np.average(strikes, weights=oi))
    d.call_weighted_mean = float(np.average(strikes, weights=call_oi)) if call_oi.sum() > 0 else 0
    d.put_weighted_mean = float(np.average(strikes, weights=put_oi)) if put_oi.sum() > 0 else 0

    # ── Desvio padrão ponderado ──
    variance = float(np.average((strikes - d.oi_weighted_mean) ** 2, weights=oi))
    d.oi_weighted_std = math.sqrt(variance) if variance > 0 else 0

    # ── Momentos (skewness, kurtosis) ──
    if d.oi_weighted_std > 0:
        z = (strikes - d.oi_weighted_mean) / d.oi_weighted_std
        d.skewness = float(np.average(z ** 3, weights=oi))
        d.kurtosis = float(np.average(z ** 4, weights=oi))
        d.excess_kurtosis = d.kurtosis - 3.0

    # ── Herfindahl-Hirschman Index ──
    d.hhi = float(np.sum(w ** 2))
    d.hhi_normalized = (d.hhi - 1 / n) / (1 - 1 / n) if n > 1 else 0

    # ── Gini Coefficient ──
    sorted_oi = np.sort(oi)
    cum = np.cumsum(sorted_oi)
    d.gini = float(1 - 2 * np.sum(cum) / (n * cum[-1]) + 1 / n) if cum[-1] > 0 else 0

    # ── Shannon Entropy ──
    w_pos = w[w > 0]
    d.entropy = float(-np.sum(w_pos * np.log2(w_pos)))
    d.max_entropy = math.log2(n) if n > 1 else 0
    d.entropy_ratio = d.entropy / d.max_entropy if d.max_entropy > 0 else 0

    # ── Concentração em torno do Max Pain ──
    if d.oi_weighted_std > 0:
        mask_1s = np.abs(strikes - max_pain) <= d.oi_weighted_std
        mask_2s = np.abs(strikes - max_pain) <= 2 * d.oi_weighted_std
        d.pct_within_1std = float(oi[mask_1s].sum() / d.total_oi_sum * 100)
        d.pct_within_2std = float(oi[mask_2s].sum() / d.total_oi_sum * 100)

    mask_1pt = np.abs(strikes - max_pain) <= 1
    mask_2pt = np.abs(strikes - max_pain) <= 2
    d.pct_within_1pt = float(oi[mask_1pt].sum() / d.total_oi_sum * 100)
    d.pct_within_2pt = float(oi[mask_2pt].sum() / d.total_oi_sum * 100)

    # ── Ajuste de Distribuição Normal ──
    def gaussian(x, amp, mu, sigma):
        return amp * np.exp(-0.5 * ((x - mu) / sigma) ** 2)

    try:
        p0 = [oi.max(), d.oi_weighted_mean, d.oi_weighted_std]
        popt, _ = curve_fit(gaussian, strikes, oi, p0=p0, maxfev=5000)
        d.normal_amplitude = float(popt[0])
        d.normal_mu = float(popt[1])
        d.normal_sigma = float(abs(popt[2]))
        fitted = gaussian(strikes, *popt)
        d.normal_fit_y = fitted.tolist()
        d.residuals = (oi - fitted).tolist()

        ss_res = np.sum((oi - fitted) ** 2)
        ss_tot = np.sum((oi - oi.mean()) ** 2)
        d.normal_r_squared = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0
    except Exception:
        d.normal_fit_y = [0] * n
        d.residuals = oi.tolist()
        d.normal_r_squared = 0
        d.normal_mu = d.oi_weighted_mean
        d.normal_sigma = d.oi_weighted_std

    # ── KS Test contra Normal ──
    try:
        expanded = np.repeat(strikes, np.round(oi / oi.min()).astype(int).clip(1))
        if len(expanded) > 5:
            ks_stat, ks_p = sp_stats.kstest(
                expanded, 'norm',
                args=(d.oi_weighted_mean, d.oi_weighted_std)
            )
            d.ks_statistic = float(ks_stat)
            d.ks_pvalue = float(ks_p)
            d.is_normal = ks_p > 0.05
    except Exception:
        pass

    # ── Distância max pain ↔ centro de massa ──
    d.mp_vs_oi_mean = d.max_pain - d.oi_weighted_mean

    # ── Interpretações ──
    # Concentração
    if d.hhi_normalized >= 0.25 or d.gini >= 0.6:
        d.concentration_grade = "Alta concentração"
    elif d.hhi_normalized >= 0.15 or d.gini >= 0.45:
        d.concentration_grade = "Concentração moderada"
    elif d.hhi_normalized >= 0.08 or d.gini >= 0.30:
        d.concentration_grade = "Levemente concentrado"
    else:
        d.concentration_grade = "Distribuição dispersa"

    # Forma
    parts = []
    if abs(d.skewness) < 0.3:
        parts.append("simétrica")
    elif d.skewness > 0:
        parts.append(f"assimétrica à direita (skew {d.skewness:+.2f})")
    else:
        parts.append(f"assimétrica à esquerda (skew {d.skewness:+.2f})")

    if d.excess_kurtosis > 1:
        parts.append("pico acentuado (leptocúrtica)")
    elif d.excess_kurtosis < -1:
        parts.append("achatada (platicúrtica)")
    else:
        parts.append("perfil mesocúrtico")

    d.shape_desc = ", ".join(parts)

    return d


# ─────────────────────────────────────────────────────────────
# 4.  EXPIRATION CONFIG
# ─────────────────────────────────────────────────────────────

VENC_DATES = {
    "27 mar w4":     "2026-03-27",
    "2 abr w1":      "2026-04-02",
    "10 abr w2":     "2026-04-10",
    "17 abr mensal": "2026-04-17",
    "15 mai mensal": "2026-05-15",
}

_MESES_PT = {"jan":"Jan","fev":"Fev","mar":"Mar","abr":"Abr","mai":"Mai","jun":"Jun",
             "jul":"Jul","ago":"Ago","set":"Set","out":"Out","nov":"Nov","dez":"Dez"}

def _fmt_label(raw: str) -> str:
    parts = raw.strip().split()
    out = []
    for p in parts:
        lp = p.lower()
        if lp in _MESES_PT:       out.append(_MESES_PT[lp])
        elif lp.startswith("w") and lp[1:].isdigit(): out.append("— " + p.upper())
        elif lp == "mensal":       out.append("— Mensal")
        else:                      out.append(p.capitalize())
    return " ".join(out)

def _discover_expirations(data_dir: Path, ref_tag: str) -> list:
    import glob as _glob, re as _re
    pattern_sp = str(data_dir / f"venc * fechamento ({ref_tag}).csv")
    pattern_us = str(data_dir / f"venc_*_fechamento__{ref_tag}_.csv")
    found = _glob.glob(pattern_sp) + _glob.glob(pattern_us)
    result, seen = [], set()
    for fpath in sorted(found):
        fname = Path(fpath).name
        m = _re.match(r'venc_(.+?)_fechamento__', fname)
        label_raw = m.group(1).replace("_"," ").lower() if m else ""
        if not label_raw:
            m = _re.match(r'venc (.+?) fechamento', fname)
            label_raw = m.group(1).lower() if m else ""
        if not label_raw or label_raw in seen: continue
        seen.add(label_raw)
        exp_date = VENC_DATES.get(label_raw, "")
        exp_type = "Mensal" if "mensal" in label_raw else "Semanal"
        vol_fname = fname.replace(f"({ref_tag}).csv", f"({ref_tag} Volume).csv") \
                        .replace(f"__{ref_tag}_.csv", f"__{ref_tag}_Volume_.csv")
        result.append({"label": _fmt_label(label_raw), "exp_date": exp_date,
                        "type": exp_type, "file": fname, "vol": vol_fname})
    return result


def infer_spot(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    idx = (df["call_delta"] - 0.50).abs().argsort().iloc[0]
    return float(df.loc[idx, "strike"])


# ─────────────────────────────────────────────────────────────
# 5.  ORCHESTRATOR
# ─────────────────────────────────────────────────────────────

@dataclass
class ExpResult:
    label: str
    exp_date: str
    exp_type: str
    dte: int
    dist: OIDistribution = field(default_factory=OIDistribution)
    mp_curve: list[dict] = field(default_factory=list)


def analyze_one(data_dir: Path, cfg: dict, ref: date) -> Optional[ExpResult]:
    df = load_b3(data_dir / cfg["file"])
    if df.empty:
        return None
    df = filter_expiry_family(df, cfg["exp_date"], cfg["type"])
    if df.empty:
        return None
    df = collapse_option_rows_by_strike(df)
    spot = infer_spot(df)
    exp_d = datetime.strptime(cfg["exp_date"], "%Y-%m-%d").date()
    dte = max((exp_d - ref).days, 0)

    curve, mp, mp_loss = calc_max_pain(df)
    dist = compute_oi_stats(df, mp, spot)

    return ExpResult(
        label=cfg["label"],
        exp_date=cfg["exp_date"],
        exp_type=cfg["type"],
        dte=dte,
        dist=dist,
        mp_curve=curve.to_dict("records"),
    )


# ─────────────────────────────────────────────────────────────
# 6.  HTML GENERATOR
# ─────────────────────────────────────────────────────────────

def _fk(v):
    if abs(v) >= 1e6: return f"{v/1e6:.1f}M"
    if abs(v) >= 1e3: return f"{v/1e3:.0f}k"
    return f"{v:.0f}"


def _stat_card(label, value, sub="", color=""):
    cs = f'style="color:{color}"' if color else ""
    sub_html = f'<div class="st-sub">{sub}</div>' if sub else ""
    return f"""<div class="st-card">
      <div class="st-label">{label}</div>
      <div class="st-value" {cs}>{value}</div>{sub_html}</div>"""


def build_exp_html(r: ExpResult, idx: int) -> str:
    d = r.dist
    mp = d.max_pain
    spot = d.spot

    # Colors for concentration grade
    gc = {"Alta concentração": "#1D9E75", "Concentração moderada": "#6B9E1D",
          "Levemente concentrado": "#D4870E", "Distribuição dispersa": "#C9403B"}
    grade_color = gc.get(d.concentration_grade, "#8A877F")

    # R² color
    r2c = "#1D9E75" if d.normal_r_squared >= 0.7 else "#D4870E" if d.normal_r_squared >= 0.4 else "#C9403B"

    # Chart data
    chart_data = json.dumps({
        "strikes": d.strikes,
        "call_oi": d.call_oi,
        "put_oi": d.put_oi,
        "total_oi": d.total_oi,
        "normal_fit": d.normal_fit_y,
        "residuals": d.residuals,
        "mp": mp,
        "spot": spot,
        "mu": d.oi_weighted_mean,
        "sigma": d.oi_weighted_std,
        "call_mean": d.call_weighted_mean,
        "put_mean": d.put_weighted_mean,
    })
    mp_curve_json = json.dumps(r.mp_curve)

    return f"""
    <div class="exp-block" id="exp{idx}" style="animation-delay:{idx*0.06}s">
      <div class="exp-top">
        <div>
          <h2>{r.label}</h2>
          <span class="tag {'tw' if r.exp_type=='Semanal' else 'tm'}">{r.exp_type}</span>
          <span class="tag tg">{r.dte} DTE</span>
        </div>
        <div class="grade-pill" style="--gc:{grade_color}">
          <span class="gp-dot"></span>{d.concentration_grade}
        </div>
      </div>

      <!-- Stat cards row 1: Posição -->
      <div class="stat-section-title">Posição</div>
      <div class="stats-grid">
        {_stat_card("Max Pain", f"R$ {mp:.0f}")}
        {_stat_card("Spot", f"R$ {spot:.0f}")}
        {_stat_card("Distância MP→Spot", f"{(spot-mp)/spot*100:+.2f}%",
                     color="#1D9E75" if abs(spot-mp)/spot*100 < 1 else "#D4870E")}
        {_stat_card("OI Total", _fk(d.total_oi_sum))}
      </div>

      <!-- Stat cards row 2: Centro de massa -->
      <div class="stat-section-title">Centro de massa do OI</div>
      <div class="stats-grid">
        {_stat_card("Média ponderada (μ)", f"R$ {d.oi_weighted_mean:.2f}",
                     f"Centro de massa total")}
        {_stat_card("σ ponderado", f"R$ {d.oi_weighted_std:.2f}",
                     f"Dispersão em torno de μ")}
        {_stat_card("μ Calls", f"R$ {d.call_weighted_mean:.2f}",
                     f"Centro de massa das calls")}
        {_stat_card("μ Puts", f"R$ {d.put_weighted_mean:.2f}",
                     f"Centro de massa das puts")}
        {_stat_card("MP vs μ OI", f"{d.mp_vs_oi_mean:+.2f}",
                     "Max pain acima" if d.mp_vs_oi_mean > 0 else "Max pain abaixo")}
      </div>

      <!-- Stat cards row 3: Forma -->
      <div class="stat-section-title">Forma da distribuição</div>
      <div class="stats-grid">
        {_stat_card("Assimetria (Skew)", f"{d.skewness:+.3f}",
                     "Simétrica" if abs(d.skewness)<0.3 else
                     "Cauda à direita" if d.skewness>0 else "Cauda à esquerda",
                     color="#1D9E75" if abs(d.skewness)<0.3 else "#D4870E")}
        {_stat_card("Curtose", f"{d.kurtosis:.3f}",
                     f"Excesso: {d.excess_kurtosis:+.3f}",
                     color="#1D9E75" if d.excess_kurtosis>0.5 else "#D4870E")}
        {_stat_card("KS Estatística", f"{d.ks_statistic:.4f}",
                     f"p-valor: {d.ks_pvalue:.4f}")}
        {_stat_card("Normalidade?",
                     "Sim ✓" if d.is_normal else "Não ✗",
                     "KS test p > 0.05" if d.is_normal else "Rejeita H₀ de normalidade",
                     color="#1D9E75" if d.is_normal else "#C9403B")}
      </div>

      <!-- Stat cards row 4: Concentração -->
      <div class="stat-section-title">Métricas de concentração</div>
      <div class="stats-grid">
        {_stat_card("HHI", f"{d.hhi:.4f}",
                     f"Normalizado: {d.hhi_normalized:.4f}")}
        {_stat_card("Gini", f"{d.gini:.3f}",
                     "0 = uniforme, 1 = concentrado",
                     color=grade_color)}
        {_stat_card("Entropia", f"{d.entropy:.2f} bits",
                     f"Máx: {d.max_entropy:.2f} · Ratio: {d.entropy_ratio:.2%}")}
        {_stat_card("OI em ±1σ do MP", f"{d.pct_within_1std:.1f}%",
                     f"Normal teórico: 68.3%",
                     color="#1D9E75" if d.pct_within_1std>60 else "#D4870E")}
        {_stat_card("OI em ±2σ do MP", f"{d.pct_within_2std:.1f}%",
                     f"Normal teórico: 95.4%")}
        {_stat_card("OI em ±R$2 do MP", f"{d.pct_within_2pt:.1f}%",
                     "Contratos perto do max pain")}
      </div>

      <!-- Ajuste normal -->
      <div class="stat-section-title">Ajuste Gaussiano</div>
      <div class="stats-grid">
        {_stat_card("μ (fit)", f"R$ {d.normal_mu:.2f}")}
        {_stat_card("σ (fit)", f"R$ {d.normal_sigma:.2f}")}
        {_stat_card("R²", f"{d.normal_r_squared:.4f}",
                     "Bom ajuste" if d.normal_r_squared>=0.7 else
                     "Ajuste moderado" if d.normal_r_squared>=0.4 else "Ajuste ruim",
                     color=r2c)}
        {_stat_card("Forma", d.shape_desc)}
      </div>

      <!-- Charts -->
      <div class="charts-2col">
        <div class="ch-card">
          <h4>Distribuição do OI vs Ajuste Normal</h4>
          <div class="ch-wrap"><canvas id="oi_{idx}"></canvas></div>
        </div>
        <div class="ch-card">
          <h4>Curva de max pain (perda total)</h4>
          <div class="ch-wrap"><canvas id="mp_{idx}"></canvas></div>
        </div>
      </div>
      <div class="charts-2col">
        <div class="ch-card">
          <h4>Calls vs Puts — distribuição separada</h4>
          <div class="ch-wrap"><canvas id="cp_{idx}"></canvas></div>
        </div>
        <div class="ch-card">
          <h4>Resíduos do ajuste (OI real − Normal)</h4>
          <div class="ch-wrap"><canvas id="res_{idx}"></canvas></div>
        </div>
      </div>

      <script>
      (function() {{
        const D = {chart_data};
        const MPC = {mp_curve_json};

        // ── OI Distribution + Normal Fit ──
        new Chart(document.getElementById('oi_{idx}'), {{
          type: 'bar',
          data: {{
            labels: D.strikes,
            datasets: [
              {{ label: 'OI Total', data: D.total_oi, backgroundColor: 'rgba(107,158,29,0.5)',
                 borderColor: 'rgba(107,158,29,0.8)', borderWidth: 1, borderRadius: 2,
                 barPercentage: 0.8, order: 2 }},
              {{ label: 'Ajuste Normal', data: D.normal_fit, type: 'line',
                 borderColor: '#D4870E', borderWidth: 2, pointRadius: 0,
                 tension: 0.4, fill: false, order: 1 }},
            ]
          }},
          options: {{
            responsive: true, maintainAspectRatio: false,
            interaction: {{ mode: 'index', intersect: false }},
            plugins: {{
              legend: {{ position: 'top', labels: {{ boxWidth: 8, padding: 10, font: {{ size: 9 }} }} }},
            }},
            scales: {{
              x: {{ ticks: {{ font: {{ size: 9, family: 'var(--mono)' }} }}, grid: {{ display: false }} }},
              y: {{ ticks: {{ callback: v => {{
                     if (Math.abs(v)>=1e6) return (v/1e6).toFixed(1)+'M';
                     if (Math.abs(v)>=1e3) return (v/1e3).toFixed(0)+'k';
                     return Math.round(v);
                   }}, font: {{ size: 9, family: 'var(--mono)' }} }},
                   grid: {{ color: 'rgba(255,255,255,0.03)' }} }},
            }}
          }},
          plugins: [{{
            afterDraw(chart) {{
              const xs = chart.scales.x, ys = chart.scales.y, ctx = chart.ctx;
              // Max pain line
              const mpI = D.strikes.indexOf(D.mp);
              if (mpI >= 0) {{
                const x = xs.getPixelForValue(mpI);
                ctx.save(); ctx.strokeStyle='#D4870E'; ctx.lineWidth=1;
                ctx.setLineDash([3,2]); ctx.beginPath();
                ctx.moveTo(x,ys.top); ctx.lineTo(x,ys.bottom); ctx.stroke();
                ctx.fillStyle='#D4870E'; ctx.font="600 8px var(--mono)";
                ctx.textAlign='center'; ctx.fillText('MP',x,ys.top-4); ctx.restore();
              }}
              // μ line
              const muI = D.strikes.reduce((a,v,i) => Math.abs(v-D.mu)<Math.abs(D.strikes[a]-D.mu)?i:a, 0);
              const mx = xs.getPixelForValue(muI);
              ctx.save(); ctx.strokeStyle='#3B7DD8'; ctx.lineWidth=1;
              ctx.setLineDash([3,2]); ctx.beginPath();
              ctx.moveTo(mx,ys.top); ctx.lineTo(mx,ys.bottom); ctx.stroke();
              ctx.fillStyle='#3B7DD8'; ctx.font="600 8px var(--mono)";
              ctx.textAlign='center'; ctx.fillText('μ',mx,ys.top-4); ctx.restore();
              // Spot line
              const spI = D.strikes.reduce((a,v,i) => Math.abs(v-D.spot)<Math.abs(D.strikes[a]-D.spot)?i:a, 0);
              const sx = xs.getPixelForValue(spI);
              ctx.save(); ctx.strokeStyle='#8A877F'; ctx.lineWidth=1;
              ctx.setLineDash([2,3]); ctx.beginPath();
              ctx.moveTo(sx,ys.top); ctx.lineTo(sx,ys.bottom); ctx.stroke();
              ctx.fillStyle='#8A877F'; ctx.font="600 8px var(--mono)";
              ctx.textAlign='center'; ctx.fillText('SPOT',sx,ys.top-4); ctx.restore();
            }}
          }}]
        }});

        // ── Max Pain Curve ──
        new Chart(document.getElementById('mp_{idx}'), {{
          type: 'line',
          data: {{
            labels: MPC.map(d=>d.strike),
            datasets: [
              {{ label:'Perda total', data: MPC.map(d=>d.total_loss), borderColor:'#D4870E',
                 backgroundColor:'rgba(212,135,14,0.1)', borderWidth:1.8, fill:true,
                 tension:0.3, pointRadius:0 }},
              {{ label:'Call loss', data: MPC.map(d=>d.call_loss), borderColor:'#1D9E75',
                 borderWidth:1, borderDash:[3,2], fill:false, tension:0.3, pointRadius:0 }},
              {{ label:'Put loss', data: MPC.map(d=>d.put_loss), borderColor:'#C9403B',
                 borderWidth:1, borderDash:[3,2], fill:false, tension:0.3, pointRadius:0 }},
            ]
          }},
          options: {{
            responsive:true, maintainAspectRatio:false,
            interaction: {{ mode:'index', intersect:false }},
            plugins: {{ legend: {{ position:'top', labels:{{ boxWidth:8, padding:10, font:{{size:9}} }} }} }},
            scales: {{
              x: {{ ticks:{{ font:{{size:9,family:'var(--mono)'}}}}, grid:{{display:false}} }},
              y: {{ ticks:{{ callback:v=>(v/1e6).toFixed(1)+'M', font:{{size:9,family:'var(--mono)'}} }},
                   grid:{{ color:'rgba(255,255,255,0.03)' }} }}
            }}
          }}
        }});

        // ── Calls vs Puts ──
        new Chart(document.getElementById('cp_{idx}'), {{
          type: 'bar',
          data: {{
            labels: D.strikes,
            datasets: [
              {{ label:'Call OI', data:D.call_oi, backgroundColor:'rgba(29,158,117,0.55)',
                 borderRadius:2, barPercentage:0.9 }},
              {{ label:'Put OI', data:D.put_oi, backgroundColor:'rgba(201,64,59,0.55)',
                 borderRadius:2, barPercentage:0.9 }},
            ]
          }},
          options: {{
            responsive:true, maintainAspectRatio:false,
            plugins: {{ legend:{{ position:'top', labels:{{ boxWidth:8, padding:10, font:{{size:9}} }} }} }},
            scales: {{
              x: {{ stacked:false, ticks:{{ font:{{size:9,family:'var(--mono)'}}}}, grid:{{display:false}} }},
              y: {{ ticks:{{ callback:v=> {{
                     if(Math.abs(v)>=1e6) return (v/1e6).toFixed(1)+'M';
                     if(Math.abs(v)>=1e3) return (v/1e3).toFixed(0)+'k';
                     return Math.round(v);
                   }}, font:{{size:9, family:'var(--mono)'}} }},
                   grid:{{ color:'rgba(255,255,255,0.03)' }} }}
            }}
          }},
          plugins: [{{
            afterDraw(chart) {{
              const xs=chart.scales.x, ys=chart.scales.y, ctx=chart.ctx;
              // Call mean
              const ci = D.strikes.reduce((a,v,i)=>Math.abs(v-D.call_mean)<Math.abs(D.strikes[a]-D.call_mean)?i:a,0);
              const cx = xs.getPixelForValue(ci);
              ctx.save(); ctx.strokeStyle='#1D9E75'; ctx.lineWidth=1; ctx.setLineDash([3,2]);
              ctx.beginPath(); ctx.moveTo(cx,ys.top); ctx.lineTo(cx,ys.bottom); ctx.stroke();
              ctx.fillStyle='#1D9E75'; ctx.font="600 8px var(--mono)";
              ctx.textAlign='center'; ctx.fillText('μ Call',cx,ys.top-4); ctx.restore();
              // Put mean
              const pi = D.strikes.reduce((a,v,i)=>Math.abs(v-D.put_mean)<Math.abs(D.strikes[a]-D.put_mean)?i:a,0);
              const px = xs.getPixelForValue(pi);
              ctx.save(); ctx.strokeStyle='#C9403B'; ctx.lineWidth=1; ctx.setLineDash([3,2]);
              ctx.beginPath(); ctx.moveTo(px,ys.top); ctx.lineTo(px,ys.bottom); ctx.stroke();
              ctx.fillStyle='#C9403B'; ctx.font="600 8px var(--mono)";
              ctx.textAlign='center'; ctx.fillText('μ Put',px,ys.top-4); ctx.restore();
            }}
          }}]
        }});

        // ── Residuals ──
        const resColors = D.residuals.map(v => v >= 0 ? 'rgba(107,158,29,0.6)' : 'rgba(201,64,59,0.6)');
        new Chart(document.getElementById('res_{idx}'), {{
          type: 'bar',
          data: {{
            labels: D.strikes,
            datasets: [{{ label:'Resíduo', data:D.residuals, backgroundColor:resColors,
                          borderRadius:2, barPercentage:0.8 }}]
          }},
          options: {{
            responsive:true, maintainAspectRatio:false,
            plugins: {{ legend:{{ display:false }} }},
            scales: {{
              x: {{ ticks:{{ font:{{size:9,family:'var(--mono)'}}}}, grid:{{display:false}} }},
              y: {{ ticks:{{ callback:v=> {{
                     if(Math.abs(v)>=1e6) return (v/1e6).toFixed(1)+'M';
                     if(Math.abs(v)>=1e3) return (v/1e3).toFixed(0)+'k';
                     return Math.round(v);
                   }}, font:{{size:9, family:'var(--mono)'}} }},
                   grid:{{ color:'rgba(255,255,255,0.03)' }} }}
            }}
          }}
        }});
      }})();
      </script>
    </div>"""


def generate_dashboard(results: list[ExpResult]) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    spot_ref = results[0].dist.spot if results else 0

    nav_items = ""
    for i, r in enumerate(results):
        gc = {"Alta concentração": "#1D9E75", "Concentração moderada": "#6B9E1D",
              "Levemente concentrado": "#D4870E", "Distribuição dispersa": "#C9403B"}
        c = gc.get(r.dist.concentration_grade, "#8A877F")
        nav_items += f"""
        <a class="nav-chip" href="#" onclick="event.preventDefault();
           document.getElementById('exp{i}').scrollIntoView({{behavior:'smooth'}})">
          <div class="nc-label">{r.label}</div>
          <div class="nc-row">
            <span class="nc-mp">MP R${r.dist.max_pain:.0f}</span>
            <span class="nc-grade" style="color:{c}">{r.dist.concentration_grade.split()[0]}</span>
          </div>
          <div class="nc-sub">R²={r.dist.normal_r_squared:.2f} · Gini={r.dist.gini:.2f} · {r.dte}DTE</div>
        </a>"""

    cards_html = "\n".join(build_exp_html(r, i) for i, r in enumerate(results))

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — Distribuição Estatística do OI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
:root {{
  --bg:#08090C; --bg2:#0F1116; --bg3:#161921; --card:#111318;
  --border:rgba(255,255,255,0.055); --border2:rgba(255,255,255,0.10);
  --t1:#E4E2DB; --t2:#8A877F; --t3:#4E4C47;
  --green:#1D9E75; --lime:#6B9E1D; --amber:#D4870E; --red:#C9403B; --blue:#3B7DD8;
  --font:'Instrument Sans',system-ui,sans-serif;
  --mono:'JetBrains Mono',monospace;
}}
*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ scroll-behavior:smooth; }}
body {{ background:var(--bg); color:var(--t1); font-family:var(--font); -webkit-font-smoothing:antialiased; }}

.shell {{ max-width:1200px; margin:0 auto; padding:48px 28px 80px; }}

/* Header */
.hdr {{ display:flex; justify-content:space-between; align-items:flex-end;
        margin-bottom:36px; border-bottom:1px solid var(--border); padding-bottom:24px;
        flex-wrap:wrap; gap:16px; }}
.hdr h1 {{ font-size:26px; font-weight:700; letter-spacing:-0.03em; }}
.hdr h1 span {{ color:var(--t3); font-weight:400; font-size:20px; }}
.hdr .meta {{ font-size:11px; color:var(--t3); font-family:var(--mono); text-align:right; line-height:1.7; }}

/* Nav strip */
.nav-strip {{
  display:flex; gap:6px; overflow-x:auto; margin-bottom:36px;
  padding-bottom:4px; scrollbar-width:thin;
}}
.nav-strip::-webkit-scrollbar {{ height:3px; }}
.nav-strip::-webkit-scrollbar-thumb {{ background:var(--t3); border-radius:2px; }}
.nav-chip {{
  flex:0 0 auto; padding:10px 14px; border-radius:10px;
  background:var(--card); border:1px solid var(--border);
  cursor:pointer; transition:all 0.2s; text-decoration:none; display:block;
  min-width:170px;
}}
.nav-chip:hover {{ border-color:var(--border2); background:var(--bg3); }}
.nc-label {{ font-size:12px; font-weight:600; color:var(--t1); margin-bottom:3px; }}
.nc-row {{ display:flex; align-items:baseline; gap:8px; }}
.nc-mp {{ font-size:13px; font-family:var(--mono); color:var(--t2); }}
.nc-grade {{ font-size:11px; font-weight:600; }}
.nc-sub {{ font-size:10px; color:var(--t3); font-family:var(--mono); margin-top:3px; }}

/* Expiration blocks */
.exp-block {{
  background:var(--card); border:1px solid var(--border);
  border-radius:16px; padding:28px; margin-bottom:28px;
  animation: fadeUp 0.5s ease both;
}}
@keyframes fadeUp {{
  from {{ opacity:0; transform:translateY(16px); }}
  to   {{ opacity:1; transform:translateY(0); }}
}}
.exp-top {{
  display:flex; justify-content:space-between; align-items:flex-start;
  margin-bottom:20px; flex-wrap:wrap; gap:12px;
}}
.exp-top h2 {{ font-size:20px; font-weight:600; letter-spacing:-0.02em; margin-bottom:6px; }}
.tag {{
  display:inline-block; font-size:10px; font-weight:600; font-family:var(--mono);
  padding:3px 8px; border-radius:5px; text-transform:uppercase; letter-spacing:0.04em;
}}
.tw {{ background:rgba(59,125,216,0.12); color:var(--blue); }}
.tm {{ background:rgba(29,158,117,0.12); color:var(--green); }}
.tg {{ background:rgba(255,255,255,0.05); color:var(--t2); margin-left:4px; }}
.grade-pill {{
  display:flex; align-items:center; gap:6px; font-size:12px; font-weight:600;
  color:var(--gc); padding:6px 14px; border-radius:20px;
  background:color-mix(in srgb, var(--gc) 10%, transparent);
  border:1px solid color-mix(in srgb, var(--gc) 25%, transparent);
}}
.gp-dot {{ width:6px; height:6px; border-radius:50%; background:var(--gc); animation:pulse 2s infinite; }}
@keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.35}} }}

/* Section titles */
.stat-section-title {{
  font-size:10px; font-weight:600; color:var(--t3);
  text-transform:uppercase; letter-spacing:0.07em;
  margin:20px 0 8px; padding-left:2px;
}}

/* Stat cards */
.stats-grid {{
  display:grid; grid-template-columns:repeat(auto-fill, minmax(155px,1fr));
  gap:8px; margin-bottom:4px;
}}
.st-card {{
  background:var(--bg2); border-radius:8px; padding:10px 12px;
}}
.st-label {{
  font-size:10px; color:var(--t3); text-transform:uppercase;
  letter-spacing:0.05em; font-weight:600; margin-bottom:4px;
}}
.st-value {{
  font-size:15px; font-weight:600; font-family:var(--mono);
  letter-spacing:-0.02em; line-height:1.3;
}}
.st-sub {{
  font-size:10px; color:var(--t3); margin-top:3px; line-height:1.4;
}}

/* Charts */
.charts-2col {{
  display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:16px;
}}
@media(max-width:750px) {{ .charts-2col {{ grid-template-columns:1fr; }} }}
.ch-card {{
  background:var(--bg2); border-radius:10px; padding:14px;
}}
.ch-card h4 {{
  font-size:10px; color:var(--t3); text-transform:uppercase;
  letter-spacing:0.05em; font-weight:600; margin-bottom:8px;
}}
.ch-wrap {{ position:relative; height:220px; }}
.ch-wrap canvas {{ position:absolute; inset:0; }}

/* Glossary */
.glossary {{
  margin:32px 0 24px; border:1px solid var(--border); border-radius:12px;
  background:var(--bg2); overflow:hidden;
}}
.glossary summary {{
  padding:14px 20px; cursor:pointer; font-size:13px; font-weight:600;
  color:var(--t2); list-style:none; user-select:none;
}}
.glossary summary::-webkit-details-marker {{ display:none }}
.glossary[open] summary {{ border-bottom:1px solid var(--border); color:var(--t1); }}
.gl-grid {{
  display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
  gap:1px; background:var(--border);
}}
.gl-item {{ background:var(--bg2); padding:14px 18px; }}
.gl-title {{ font-size:11px; font-weight:700; color:var(--t1); margin-bottom:5px; letter-spacing:0.02em; }}
.gl-body {{ font-size:11px; color:var(--t2); line-height:1.6; }}

/* Footer */
.footer {{
  text-align:center; padding:32px 0 0; margin-top:16px;
  border-top:1px solid var(--border);
  font-size:11px; color:var(--t3); line-height:1.8;
}}
</style>
</head>
<body>
<div class="shell">
  <div class="hdr">
    <div><h1>BOVA11 <span>Distribuição estatística do OI</span></h1></div>
    <div class="meta">Referência: 24/Mar/2026<br>Spot ≈ R$ {spot_ref:.2f}<br>Gerado: {now}</div>
  </div>

  <div class="nav-strip">{nav_items}</div>

  {cards_html}

  <details class="glossary">
    <summary>📖 Glossário — o que cada métrica estatística significa</summary>
    <div class="gl-grid">
      <div class="gl-item">
        <div class="gl-title">μ — Média ponderada (centro de massa)</div>
        <div class="gl-body">O strike onde, se você "equilibrasse" toda a distribuição como uma régua, ela ficaria nivelada. Quando μ coincide com o max pain, a maior parte da exposição dos dealers está centrada no ponto de mínima perda, reforçando o efeito gravitacional. Quando μ diverge do max pain, existe assimetria de posicionamento que pode puxar o preço para um lado.</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">σ — Desvio padrão ponderado (dispersão)</div>
        <div class="gl-body">Mede o quanto o OI está espalhado. σ baixo = OI extremamente concentrado em poucos strikes (efeito pinning forte). σ alto = distribuição espalhada por toda a cadeia, sem ponto de gravidade claro.</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">Skewness — Assimetria</div>
        <div class="gl-body">Zero = distribuição simétrica. Negativo = cauda pesada à esquerda (mais OI em puts OTM profundas — proteção contra queda, viés bearish). Positivo = cauda à direita (mais OI em calls OTM — posicionamento para alta ou operações de financiamento).</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">Kurtosis — Concentração de pico</div>
        <div class="gl-body">A distribuição normal tem curtose 3 (excesso 0). Acima de 3 (leptocúrtica) = pico concentrado com caudas finas, OI agrupado em poucos strikes. Abaixo de 3 (platicúrtica) = distribuição achatada e uniforme, sem concentração clara em nenhum ponto.</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">HHI — Índice Herfindahl-Hirschman</div>
        <div class="gl-body">Índice de concentração que vai de 1/N (OI perfeitamente uniforme entre todos os strikes) a 1.0 (todo o OI em um único strike). Acima de 0.25 indica concentração alta — poucos strikes dominam o posicionamento.</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">Gini — Desigualdade de distribuição</div>
        <div class="gl-body">0 = todos os strikes têm exatamente o mesmo OI. 1 = todo o OI está em um único strike. Valores acima de 0.5 indicam concentração significativa, com a maioria dos strikes tendo OI negligenciável comparado ao pico.</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">Entropia de Shannon — Desordem informacional</div>
        <div class="gl-body">Máxima quando o OI está uniformemente distribuído (máxima incerteza sobre onde o preço "quer ir"). Mínima quando está concentrado em um único strike. O ratio entropia/máximo expressa o percentual de uniformidade — 100% = totalmente uniforme, 0% = totalmente concentrado.</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">R² Gaussiano — Ajuste à curva normal</div>
        <div class="gl-body">Quanto a distribuição real do OI se parece com uma distribuição normal. R²≈1.0 = comportamento gaussiano previsível, com pico central e caudas simétricas. R²≈0 = distribuição irregular, com picos isolados, formas bimodais ou concentrações atípicas que não seguem padrão clássico.</div>
      </div>
      <div class="gl-item">
        <div class="gl-title">Teste KS — Kolmogorov-Smirnov</div>
        <div class="gl-body">Formaliza estatisticamente o teste de normalidade. Se p &gt; 0.05, não se rejeita a hipótese de que o OI segue uma distribuição normal. Complementa o R²: R² mede qualidade do ajuste, KS testa formalmente a hipótese.</div>
      </div>
    </div>
  </details>

  <div class="footer">
    BOVA11 OI Distribution Analysis — Ajuste gaussiano, concentração e forma<br>
    Métricas: HHI, Gini, Entropia de Shannon, Skewness, Kurtosis, KS Test, R²<br>
    Não constitui recomendação de investimento.
  </div>
</div>

<script>
Chart.defaults.color = '#4E4C47';
Chart.defaults.borderColor = 'rgba(255,255,255,0.03)';
Chart.defaults.font.family = "'Instrument Sans', sans-serif";
Chart.defaults.font.size = 10;
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# 7.  MAIN
# ─────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="BOVA11 OI Distribution Dashboard")
    ap.add_argument("--data-dir", default=".")
    ap.add_argument("--output", default="bova11_oi_stats.html")
    ap.add_argument("--ref-date", default="2026-03-24")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    ref = datetime.strptime(args.ref_date, "%Y-%m-%d").date()
    _meses = {1:"jan",2:"fev",3:"mar",4:"abr",5:"mai",6:"jun",
              7:"jul",8:"ago",9:"set",10:"out",11:"nov",12:"dez"}
    ref_tag = f"{ref.day}{_meses[ref.month]}"
    expirations = _discover_expirations(data_dir, ref_tag)

    results = []
    for cfg in expirations:
        f = data_dir / cfg["file"]
        if not f.exists():
            print(f"  ⚠ {f} não encontrado, pulando...")
            continue
        print(f"  → {cfg['label']}...")
        r = analyze_one(data_dir, cfg, ref)
        if r is None:
            print(f"    ⚠ Sem dados compatíveis com a família do vencimento, pulando...")
            continue
        d = r.dist
        print(f"    MP=R${d.max_pain:.0f}  μ=R${d.oi_weighted_mean:.2f}  σ=R${d.oi_weighted_std:.2f}")
        print(f"    Skew={d.skewness:+.3f}  Kurt={d.kurtosis:.3f}  R²={d.normal_r_squared:.4f}")
        print(f"    HHI={d.hhi:.4f}  Gini={d.gini:.3f}  Entropy={d.entropy:.2f}b")
        print(f"    {d.concentration_grade} · {d.shape_desc}")
        results.append(r)

    if not results:
        print("Nenhum arquivo encontrado.")
        sys.exit(1)

    html = generate_dashboard(results)
    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    print(f"\n✓ Dashboard salvo em: {out.resolve()}")


if __name__ == "__main__":
    main()
