"""
BOVA11 — Mapa Gravitacional Completo
======================================
Calcula μ OI, μ GEX, μ DEX, max pain, convergência entre centros,
distribuição estatística, sentimento e gera dashboard HTML.

Dependências: pandas, numpy, scipy
"""

from __future__ import annotations
import glob, json, math, re, sys
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


# ═══════════════════════════════════════════════════════════════
# 1. PARSER B3
# ═══════════════════════════════════════════════════════════════

def _p(raw) -> float:
    if not isinstance(raw, str):
        return float(raw) if raw == raw else 0.0
    s = raw.strip().rstrip("%").replace("\r", "")
    if s in ("", "-", "--"): return 0.0
    m = 1.0
    if s.endswith("k"):   m = 1_000;    s = s[:-1]
    elif s.endswith("M"): m = 1_000_000; s = s[:-1]
    s = s.replace(".", "").replace(",", ".")
    try:    return float(s) * m
    except: return 0.0


def load_b3(path: Path) -> pd.DataFrame:
    raw = path.read_bytes().decode("latin-1")
    rows = []
    for line in raw.splitlines()[1:]:
        c = line.strip().split(";")
        if len(c) < 23: continue
        rows.append({
            "call_ticker": c[0].strip(),
            "strike": _p(c[11]),
            "call_oi": _p(c[2]),  "put_oi": _p(c[20]),
            "call_delta": _p(c[3]), "put_delta": _p(c[19]),
            "call_gamma": _p(c[4]), "put_gamma": _p(c[18]),
            "call_iv": _p(c[7]),   "put_iv": _p(c[15]),
            "call_last": _p(c[1]), "put_last": _p(c[21]),
            "call_theta": _p(c[5]), "put_theta": _p(c[17]),
            "call_vega": _p(c[6]),  "put_vega": _p(c[16]),
            "put_ticker": c[22].strip(),
        })
    return pd.DataFrame(rows)


def load_b3_vol(path: Path) -> pd.DataFrame:
    raw = path.read_bytes().decode("latin-1")
    rows = []
    for line in raw.splitlines()[1:]:
        c = line.strip().split(";")
        if len(c) < 10: continue
        strike = _p(c[4])
        if strike == 0: continue
        rows.append({"strike": strike, "call_vol": _p(c[1]),
                     "put_vol": _p(c[9]) if len(c) > 9 else 0})
    return pd.DataFrame(rows)


def infer_spot(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    valid = df.dropna(subset=["call_delta"])
    if valid.empty:
        return float(df["strike"].median())
    idx = (valid["call_delta"] - 0.50).abs().idxmin()
    return float(valid.loc[idx, "strike"])


# ═══════════════════════════════════════════════════════════════
# 2. MAX PAIN
# ═══════════════════════════════════════════════════════════════

def calc_max_pain(df: pd.DataFrame) -> tuple[pd.DataFrame, float, float]:
    """Implementação vetorizada v2 do anexo."""
    strikes = np.array(sorted(df["strike"].unique()))
    call_oi = df.groupby("strike")["call_oi"].sum().reindex(strikes).fillna(0).values
    put_oi = df.groupby("strike")["put_oi"].sum().reindex(strikes).fillna(0).values

    diff = strikes[:, None] - strikes[None, :]
    call_loss = (np.maximum(diff, 0) * call_oi).sum(axis=1)
    put_loss = (np.maximum(-diff, 0) * put_oi).sum(axis=1)
    total_loss = call_loss + put_loss

    idx = int(np.argmin(total_loss))
    curve = pd.DataFrame({
        "strike": strikes,
        "call_loss": call_loss,
        "put_loss": put_loss,
        "total_loss": total_loss,
    })
    return curve, float(strikes[idx]), float(total_loss[idx])


# ═══════════════════════════════════════════════════════════════
# 3. GEX / DEX CALCULATION
# ═══════════════════════════════════════════════════════════════

def calc_exposures(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Calcula GEX e DEX por strike."""
    if df.empty:
        return pd.DataFrame(
            columns=[
                "strike", "call_gex", "put_gex", "net_gex", "abs_gex",
                "call_dex", "put_dex", "net_dex", "abs_dex",
            ]
        )

    out = df[["strike"]].copy()
    factor_g = spot * spot * 0.01  # GEX scaling

    # GEX: call_gex positivo (dealers long gamma), put_gex negativo
    out["call_gex"] = df["call_gamma"] * df["call_oi"] * factor_g
    out["put_gex"]  = -df["put_gamma"] * df["put_oi"] * factor_g
    out["net_gex"]  = out["call_gex"] + out["put_gex"]
    out["abs_gex"]  = out["net_gex"].abs()

    # DEX: delta × OI × spot (para escala em $)
    out["call_dex"] = df["call_delta"] * df["call_oi"] * spot
    out["put_dex"]  = df["put_delta"] * df["put_oi"] * spot  # put_delta já é negativo
    out["net_dex"]  = out["call_dex"] + out["put_dex"]
    out = (
        out.groupby("strike", as_index=False)[
            ["call_gex", "put_gex", "net_gex", "call_dex", "put_dex", "net_dex"]
        ]
        .sum()
        .sort_values("strike")
        .reset_index(drop=True)
    )
    out["abs_gex"] = out["net_gex"].abs()
    out["abs_dex"] = out["net_dex"].abs()

    return out


def calc_mu_gex(exp_df: pd.DataFrame) -> float:
    """Centro de massa ponderado por |GEX|."""
    w = exp_df["abs_gex"].values
    if w.sum() == 0: return 0.0
    return float(np.average(exp_df["strike"].values, weights=w))


def calc_mu_dex(exp_df: pd.DataFrame) -> float:
    """Centro de massa ponderado por |DEX|."""
    w = exp_df["abs_dex"].values
    if w.sum() == 0: return 0.0
    return float(np.average(exp_df["strike"].values, weights=w))


def find_gex_flip(exp_df: pd.DataFrame, spot: float) -> Optional[float]:
    """Versão v2 do anexo: detecta ambos os sentidos com interpolação linear."""
    near = (
        exp_df[
            (exp_df["strike"] >= spot - 15) &
            (exp_df["strike"] <= spot + 15)
        ]
        .sort_values("strike")
        .reset_index(drop=True)
    )
    if len(near) < 2:
        return None

    best_flip = None
    best_dist = float("inf")
    for i in range(1, len(near)):
        y1 = near.loc[i - 1, "net_gex"]
        y2 = near.loc[i, "net_gex"]
        if y1 == 0:
            candidate = float(near.loc[i - 1, "strike"])
        elif y1 * y2 < 0:
            x1 = near.loc[i - 1, "strike"]
            x2 = near.loc[i, "strike"]
            candidate = x1 - y1 * (x2 - x1) / (y2 - y1)
        else:
            continue
        dist = abs(candidate - spot)
        if dist < best_dist:
            best_dist = dist
            best_flip = candidate
    return best_flip


# ═══════════════════════════════════════════════════════════════
# 4. STATISTICAL DISTRIBUTION
# ═══════════════════════════════════════════════════════════════

@dataclass
class FullAnalysis:
    """Resultado completo de um vencimento."""
    label: str = ""
    exp_date: str = ""
    exp_type: str = ""
    dte: int = 0
    spot: float = 0

    # Centers
    max_pain: float = 0
    mu_oi: float = 0
    mu_oi_call: float = 0
    mu_oi_put: float = 0
    mu_gex: float = 0
    mu_dex: float = 0
    gex_flip: Optional[float] = None

    # Convergence
    convergence_score: float = 0   # 0-100
    convergence_label: str = ""
    convergence_detail: str = ""
    max_spread: float = 0          # max distance between any two centers

    # Distribution stats
    sigma_oi: float = 0
    skewness: float = 0
    kurtosis: float = 0
    excess_kurtosis: float = 0
    hhi: float = 0
    gini: float = 0
    entropy: float = 0
    entropy_ratio: float = 0
    normal_r2: float = 0
    normal_fit_y: list = field(default_factory=list)
    pct_1std: float = 0
    pct_2std: float = 0

    # Sentiment
    sentiment: str = ""          # "Bullish" / "Bearish" / "Neutro"
    sentiment_score: float = 0   # -100 to +100
    sentiment_detail: str = ""

    # Attraction
    attraction_direction: str = "" # "para cima" / "para baixo" / "pinning"
    attraction_coherent: bool = False
    attraction_detail: str = ""

    # Totals
    total_oi: float = 0
    net_gex: float = 0
    net_dex: float = 0

    # Chart data
    strikes: list = field(default_factory=list)
    call_oi: list = field(default_factory=list)
    put_oi: list = field(default_factory=list)
    total_oi_arr: list = field(default_factory=list)
    gex_data: list = field(default_factory=list)
    dex_data: list = field(default_factory=list)
    mp_curve: list = field(default_factory=list)
    residuals: list = field(default_factory=list)


def gaussian(x, amp, mu, sigma):
    return amp * np.exp(-0.5 * ((x - mu) / sigma) ** 2)


def run_full_analysis(df: pd.DataFrame, spot: float, mp: float, mp_loss: float,
                      curve: pd.DataFrame, exp_df: pd.DataFrame,
                      label: str, exp_date: str, exp_type: str, dte: int) -> FullAnalysis:
    a = FullAnalysis(label=label, exp_date=exp_date, exp_type=exp_type, dte=dte, spot=spot)
    a.max_pain = mp

    # Ordem consistente por strike para métricas e charts
    df = df.sort_values("strike").reset_index(drop=True)
    exp_df = exp_df.sort_values("strike").reset_index(drop=True)
    curve = curve.sort_values("strike").reset_index(drop=True)

    strikes = df["strike"].values
    call_oi = df["call_oi"].values
    put_oi = df["put_oi"].values
    total_oi = call_oi + put_oi
    n = len(strikes)

    a.strikes = strikes.tolist()
    a.call_oi = call_oi.tolist()
    a.put_oi = put_oi.tolist()
    a.total_oi_arr = total_oi.tolist()
    a.total_oi = float(total_oi.sum())
    a.mp_curve = curve.to_dict("records")

    if a.total_oi == 0 or n < 3:
        return a

    w = total_oi / a.total_oi

    # ── Centers of mass ──
    a.mu_oi = float(np.average(strikes, weights=total_oi))
    a.mu_oi_call = float(np.average(strikes, weights=call_oi)) if call_oi.sum() > 0 else 0
    a.mu_oi_put = float(np.average(strikes, weights=put_oi)) if put_oi.sum() > 0 else 0
    a.mu_gex = calc_mu_gex(exp_df)
    a.mu_dex = calc_mu_dex(exp_df)
    a.gex_flip = find_gex_flip(exp_df, spot)
    a.net_gex = float(exp_df["net_gex"].sum())
    a.net_dex = float(exp_df["net_dex"].sum())

    # GEX / DEX chart data
    a.gex_data = exp_df[["strike","call_gex","put_gex","net_gex"]].to_dict("records")
    a.dex_data = exp_df[["strike","call_dex","put_dex","net_dex"]].to_dict("records")

    # ── Distribution stats ──
    var = float(np.average((strikes - a.mu_oi) ** 2, weights=total_oi))
    a.sigma_oi = math.sqrt(var) if var > 0 else 0.01

    if a.sigma_oi > 0:
        z = (strikes - a.mu_oi) / a.sigma_oi
        a.skewness = float(np.average(z**3, weights=total_oi))
        a.kurtosis = float(np.average(z**4, weights=total_oi))
        a.excess_kurtosis = a.kurtosis - 3.0

    a.hhi = float(np.sum(w**2))
    sorted_oi = np.sort(total_oi)
    cum = np.cumsum(sorted_oi)
    a.gini = float(1 - 2 * np.sum(cum) / (n * cum[-1]) + 1/n) if cum[-1] > 0 else 0
    w_pos = w[w > 0]
    a.entropy = float(-np.sum(w_pos * np.log2(w_pos)))
    max_e = math.log2(n) if n > 1 else 1
    a.entropy_ratio = a.entropy / max_e

    mask1 = np.abs(strikes - mp) <= a.sigma_oi
    mask2 = np.abs(strikes - mp) <= 2 * a.sigma_oi
    a.pct_1std = float(total_oi[mask1].sum() / a.total_oi * 100)
    a.pct_2std = float(total_oi[mask2].sum() / a.total_oi * 100)

    # Normal fit
    try:
        popt, _ = curve_fit(gaussian, strikes, total_oi,
                            p0=[total_oi.max(), a.mu_oi, a.sigma_oi], maxfev=5000)
        fitted = gaussian(strikes, *popt)
        a.normal_fit_y = fitted.tolist()
        a.residuals = (total_oi - fitted).tolist()
        ss_res = np.sum((total_oi - fitted)**2)
        ss_tot = np.sum((total_oi - total_oi.mean())**2)
        a.normal_r2 = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0
    except:
        a.normal_fit_y = [0] * n
        a.residuals = total_oi.tolist()

    # ── CONVERGENCE ──
    centers = [c for c in [mp, a.mu_oi, a.mu_gex, a.mu_dex] if c > 0]
    if len(centers) >= 3:
        spread = max(centers) - min(centers)
        a.max_spread = spread
        # Score: 100 when all centers within R$0.5, 0 when spread > R$8
        a.convergence_score = max(0, min(100, (1 - spread / 8) * 100))
        if a.convergence_score >= 75:
            a.convergence_label = "Forte convergência"
        elif a.convergence_score >= 50:
            a.convergence_label = "Convergência moderada"
        elif a.convergence_score >= 25:
            a.convergence_label = "Convergência fraca"
        else:
            a.convergence_label = "Divergência"

        parts = []
        if abs(mp - a.mu_oi) <= 1.5:
            parts.append("MP ≈ μOI")
        if abs(mp - a.mu_gex) <= 1.5:
            parts.append("MP ≈ μGEX")
        if abs(a.mu_oi - a.mu_gex) <= 1.5:
            parts.append("μOI ≈ μGEX")
        a.convergence_detail = " · ".join(parts) if parts else f"Spread de R${spread:.1f} entre centros"

    # ── SENTIMENT ──
    # Composite: skewness + PCR + mu_call vs mu_put position + net_dex sign
    pcr = put_oi.sum() / call_oi.sum() if call_oi.sum() > 0 else 1
    sent_points = 0.0

    # Skewness: negative = bearish puts heavy
    sent_points += -a.skewness * 20  # skew -1 → +20 bearish

    # PCR: > 1.2 bearish, < 0.8 bullish
    if pcr > 1.2:   sent_points += min((pcr - 1) * 15, 30)
    elif pcr < 0.8: sent_points -= min((1 - pcr) * 15, 30)

    # mu_call vs mu_put relative to spot
    call_above = (a.mu_oi_call - spot) / spot * 100
    put_below = (spot - a.mu_oi_put) / spot * 100
    if put_below > call_above + 1:
        sent_points += 10  # puts more OTM = hedging = bearish tilt
    elif call_above > put_below + 1:
        sent_points -= 10

    # Net DEX: negative = net short delta = bearish pressure
    if a.net_dex < 0: sent_points += 10
    elif a.net_dex > 0: sent_points -= 10

    a.sentiment_score = max(-100, min(100, sent_points))
    if a.sentiment_score > 15:
        a.sentiment = "Bearish"
    elif a.sentiment_score < -15:
        a.sentiment = "Bullish"
    else:
        a.sentiment = "Neutro"

    a.sentiment_detail = (f"Skew {a.skewness:+.2f} · PCR {pcr:.2f} · "
                          f"μCall R${a.mu_oi_call:.0f} · μPut R${a.mu_oi_put:.0f}")

    # ── ATTRACTION ──
    mp_above_spot = mp > spot
    if abs(mp - spot) / spot * 100 < 0.3:
        a.attraction_direction = "pinning"
    elif mp_above_spot:
        a.attraction_direction = "para cima"
    else:
        a.attraction_direction = "para baixo"

    # Coherent if sentiment and mp direction align, or if neutral
    if a.sentiment == "Neutro":
        a.attraction_coherent = True
        a.attraction_detail = "Sentimento neutro — sem resistência ao max pain"
    elif a.sentiment == "Bearish" and not mp_above_spot:
        a.attraction_coherent = True
        a.attraction_detail = "Sentimento bear + MP abaixo = forças alinhadas ↓"
    elif a.sentiment == "Bullish" and mp_above_spot:
        a.attraction_coherent = True
        a.attraction_detail = "Sentimento bull + MP acima = forças alinhadas ↑"
    else:
        a.attraction_coherent = False
        if a.sentiment == "Bearish":
            a.attraction_detail = "Sentimento bear vs MP acima — forças opostas"
        else:
            a.attraction_detail = "Sentimento bull vs MP abaixo — forças opostas"

    return a


# ═══════════════════════════════════════════════════════════════
# 5. CONFIG & ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

# Mapeamento vencimento → data de expiração (atualizar quando novos vencimentos aparecerem)
VENC_DATES = {
    "27 mar w4":     "2026-03-27",
    "2 abr w1":      "2026-04-02",
    "10 abr w2":     "2026-04-10",
    "17 abr mensal": "2026-04-17",
    "24 abr w2":     "2026-04-24",
    "30 abr w5":     "2026-04-30",
    "15 mai mensal": "2026-05-15",
}

MESES_PT = {
    "jan": "Jan", "fev": "Fev", "mar": "Mar", "abr": "Abr",
    "mai": "Mai", "jun": "Jun", "jul": "Jul", "ago": "Ago",
    "set": "Set", "out": "Out", "nov": "Nov", "dez": "Dez",
}


def _tag_from_filename(fname: str) -> str:
    m = re.search(r'fechamento__([a-zA-Z0-9]+)_(?:Volume_)?\.csv$', fname)
    if m: return m.group(1)
    m = re.search(r'fechamento \(([a-zA-Z0-9]+)(?:\s+Volume)?\)\.csv$', fname)
    if m: return m.group(1)
    return ""


def _label_from_filename(fname: str) -> str:
    m = re.match(r'venc_(.+?)_fechamento__', fname)
    if m: return m.group(1).replace("_", " ").lower()
    m = re.match(r'venc (.+?) fechamento', fname)
    if m: return m.group(1).lower()
    return ""


def _exp_type(label: str) -> str:
    return "Mensal" if "mensal" in label.lower() else "Semanal"


def _fmt_label(raw: str) -> str:
    """'27 mar w4' → '27 Mar — W4', '17 abr mensal' → '17 Abr — Mensal'"""
    parts = raw.strip().split()
    out = []
    for p in parts:
        lp = p.lower()
        if lp in MESES_PT:
            out.append(MESES_PT[lp])
        elif lp.startswith("w") and lp[1:].isdigit():
            out.append("— " + p.upper())
        elif lp == "mensal":
            out.append("— Mensal")
        else:
            out.append(p.capitalize())
    return " ".join(out)

def _resolve_exp_date(label_raw: str) -> str:
    exp_date = VENC_DATES.get(label_raw, "")
    if exp_date:
        return exp_date
    m = re.match(r'(\d{1,2})\s+([a-z]{3})', label_raw)
    if not m:
        return ""
    meses = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
             'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}
    day = int(m.group(1))
    month = meses.get(m.group(2), 1)
    return f"{datetime.now().year}-{month:02d}-{day:02d}"


def discover_expirations(data_dir: Path, ref_tag: str) -> list[dict]:
    """Descobre vencimentos disponíveis para a tag de data ref_tag (ex: '24mar')."""
    pattern_us  = str(data_dir / f"venc_*_fechamento__{ref_tag}_.csv")
    pattern_sp  = str(data_dir / f"venc * fechamento ({ref_tag}).csv")
    found = glob.glob(pattern_us) + glob.glob(pattern_sp)

    expirations = []
    seen = set()
    for fpath in sorted(found):
        fname = Path(fpath).name
        label_raw = _label_from_filename(fname)
        if not label_raw or label_raw in seen:
            continue
        seen.add(label_raw)

        # Tentar achar data de expiração no mapa; se não tiver, pular DTE (usa 0)
        exp_date = _resolve_exp_date(label_raw)

        # Monta nome do arquivo de volume correspondente
        vol_fname = ""
        if fname.startswith("venc_"):
            vol_fname = fname.replace(f"__{ref_tag}_.csv", f"__{ref_tag}_Volume_.csv")
        else:
            vol_fname = fname.replace(f"({ref_tag}).csv", f"({ref_tag} Volume).csv")

        expirations.append({
            "label":    _fmt_label(label_raw),
            "exp_date": exp_date,
            "type":     _exp_type(label_raw),
            "file":     fname,
            "vol":      vol_fname,
        })

    expirations.sort(key=lambda x: x["exp_date"] if x["exp_date"] else "9999-99-99")
    return expirations


def analyze_all(data_dir: Path, ref: date, ref_tag: str, spot_override: Optional[float] = None) -> list[FullAnalysis]:
    expirations = discover_expirations(data_dir, ref_tag)
    if not expirations:
        print(f"  ⚠ Nenhum CSV encontrado para tag '{ref_tag}' em {data_dir}")
        return []
    results = []
    for cfg in expirations:
        f = data_dir / cfg["file"]
        if not f.exists():
            print(f"  ⚠ {f} não encontrado"); continue
        print(f"  → {cfg['label']}...")
        df_raw = load_b3(f)
        if df_raw.empty:
            print(f"    ⚠ Sem dados, pulando...")
            continue
        df_raw = filter_expiry_family(df_raw, cfg["exp_date"], cfg["type"])
        if df_raw.empty:
            print(f"    ⚠ Nenhuma linha compatível com a família do vencimento, pulando...")
            continue
        df = collapse_option_rows_by_strike(df_raw)
        if df.empty:
            print(f"    ⚠ Sem strikes válidos após consolidar, pulando...")
            continue
        spot = spot_override if spot_override is not None else infer_spot(df)
        if cfg["exp_date"]:
            dte = max((datetime.strptime(cfg["exp_date"], "%Y-%m-%d").date() - ref).days, 0)
        else:
            dte = 0
        curve, mp, mp_loss = calc_max_pain(df)
        exp_df = calc_exposures(df_raw, spot)

        a = run_full_analysis(df, spot, mp, mp_loss, curve, exp_df,
                              cfg["label"], cfg["exp_date"], cfg["type"], dte)

        print(f"    MP=R${a.max_pain:.0f}  μOI=R${a.mu_oi:.1f}  μGEX=R${a.mu_gex:.1f}  μDEX=R${a.mu_dex:.1f}")
        print(f"    Convergência: {a.convergence_score:.0f}% ({a.convergence_label})")
        print(f"    Sentimento: {a.sentiment} ({a.sentiment_score:+.0f}) · {a.attraction_detail}")
        results.append(a)
    return results


# ═══════════════════════════════════════════════════════════════
# 6. HTML DASHBOARD
# ═══════════════════════════════════════════════════════════════

def _fk(v):
    if abs(v)>=1e6: return f"{v/1e6:.1f}M"
    if abs(v)>=1e3: return f"{v/1e3:.0f}k"
    return f"{v:.0f}"


def _sc(label, value, sub="", color=""):
    cs = f'style="color:{color}"' if color else ""
    sh = f'<div class="st-sub">{sub}</div>' if sub else ""
    return f'<div class="st-card"><div class="st-label">{label}</div><div class="st-value" {cs}>{value}</div>{sh}</div>'


def _center_color(name):
    m = {"MP":"#B8720A","μOI":"#2E6BBF","μGEX":"#148A63","μDEX":"#8C5CC4","Spot":"#6B6960","Flip":"#B33530"}
    return m.get(name, "#6B6960")


def build_card(a: FullAnalysis, idx: int) -> str:
    # Convergence color
    cc = "#148A63" if a.convergence_score>=75 else "#5A8518" if a.convergence_score>=50 else "#B8720A" if a.convergence_score>=25 else "#B33530"
    # Sentiment color
    sc_c = "#B33530" if a.sentiment=="Bearish" else "#148A63" if a.sentiment=="Bullish" else "#6B6960"
    # Attraction
    ac = "#148A63" if a.attraction_coherent else "#B33530"

    chart_json = json.dumps({
        "strikes": a.strikes, "call_oi": a.call_oi, "put_oi": a.put_oi,
        "total_oi": a.total_oi_arr, "fit": a.normal_fit_y, "residuals": a.residuals,
        "gex": a.gex_data, "dex": a.dex_data, "mp_curve": a.mp_curve,
        "mp": a.max_pain, "spot": a.spot,
        "mu_oi": a.mu_oi, "mu_gex": a.mu_gex, "mu_dex": a.mu_dex,
        "mu_call": a.mu_oi_call, "mu_put": a.mu_oi_put,
        "gex_flip": a.gex_flip,
    })

    # Centers bar visualization data
    centers = [
        ("Spot", a.spot, "#6B6960"),
        ("MP", a.max_pain, "#B8720A"),
        ("μOI", a.mu_oi, "#2E6BBF"),
        ("μGEX", a.mu_gex, "#148A63"),
        ("μDEX", a.mu_dex, "#8C5CC4"),
    ]
    if a.gex_flip:
        centers.append(("Flip", a.gex_flip, "#B33530"))

    all_vals = [c[1] for c in centers if c[1] > 0]
    cmin = min(all_vals) - 1 if all_vals else 170
    cmax = max(all_vals) + 1 if all_vals else 190
    crange = cmax - cmin if cmax > cmin else 1

    center_markers = ""
    for name, val, color in centers:
        if val <= 0: continue
        pct = (val - cmin) / crange * 100
        pct = max(2, min(98, pct))
        center_markers += f"""
        <div class="cm-mark" style="left:{pct}%">
          <div class="cm-dot" style="background:{color}"></div>
          <div class="cm-label" style="color:{color}">{name}<br>R${val:.1f}</div>
        </div>"""

    return f"""
    <div class="exp-block" id="exp{idx}" style="animation-delay:{idx*0.06}s">
      <div class="exp-top">
        <div>
          <h2>{a.label}</h2>
          <span class="tag {'tw' if a.exp_type=='Semanal' else 'tm'}">{a.exp_type}</span>
          <span class="tag tg">{a.dte} DTE</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <div class="pill" style="--pc:{sc_c}">{a.sentiment} ({a.sentiment_score:+.0f})</div>
          <div class="pill" style="--pc:{cc}">Converg. {a.convergence_score:.0f}%</div>
          <div class="pill" style="--pc:{ac}">{'✓ Coerente' if a.attraction_coherent else '✗ Divergente'}</div>
        </div>
      </div>

      <!-- CENTER MAP -->
      <div class="center-map-section">
        <div class="stat-section-title">Mapa gravitacional — centros sobrepostos</div>
        <div class="center-map">
          <div class="cm-track"></div>
          {center_markers}
        </div>
        <div class="cm-legend">
          {''.join(f'<span style="color:{c}">● {n} R${v:.1f}</span>' for n,v,c in centers if v>0)}
        </div>
      </div>

      <!-- CENTERS -->
      <div class="stat-section-title">Centros de massa</div>
      <div class="stats-grid">
        {_sc("Max Pain", f"R$ {a.max_pain:.0f}", "Min. perda total dos compradores")}
        {_sc("μ OI (centro de massa)", f"R$ {a.mu_oi:.2f}", f"σ = R$ {a.sigma_oi:.2f}", "#2E6BBF")}
        {_sc("μ GEX (centro de gamma)", f"R$ {a.mu_gex:.2f}", "Onde o hedge é mais denso", "#148A63")}
        {_sc("μ DEX (centro de delta)", f"R$ {a.mu_dex:.2f}", "Maior exposição direcional", "#8C5CC4")}
        {_sc("GEX Flip", f"R$ {a.gex_flip:.0f}" if a.gex_flip else "N/A",
             "Fronteira GEX neg→pos", "#B33530" if a.gex_flip else "")}
        {_sc("Spot", f"R$ {a.spot:.0f}")}
      </div>

      <!-- CONVERGENCE -->
      <div class="stat-section-title">Convergência entre centros</div>
      <div class="stats-grid">
        {_sc("Score", f"{a.convergence_score:.0f}%", a.convergence_label, cc)}
        {_sc("Spread máx.", f"R$ {a.max_spread:.1f}", a.convergence_detail)}
        {_sc("MP vs μOI", f"R$ {a.max_pain - a.mu_oi:+.2f}",
             "Alinhados" if abs(a.max_pain-a.mu_oi)<=1.5 else "Desalinhados")}
        {_sc("MP vs μGEX", f"R$ {a.max_pain - a.mu_gex:+.2f}",
             "Alinhados" if abs(a.max_pain-a.mu_gex)<=1.5 else "Desalinhados")}
        {_sc("μOI vs μGEX", f"R$ {a.mu_oi - a.mu_gex:+.2f}",
             "Alinhados" if abs(a.mu_oi-a.mu_gex)<=1.5 else "Desalinhados")}
      </div>

      <!-- SENTIMENT -->
      <div class="stat-section-title">Sentimento e direção</div>
      <div class="stats-grid">
        {_sc("Sentimento", a.sentiment, a.sentiment_detail, sc_c)}
        {_sc("Direção MP", a.attraction_direction,
             f"MP {'acima' if a.max_pain>a.spot else 'abaixo'} do spot")}
        {_sc("Coerência", "Forças alinhadas ✓" if a.attraction_coherent else "Forças opostas ✗",
             a.attraction_detail, ac)}
        {_sc("μ Calls", f"R$ {a.mu_oi_call:.1f}",
             f"{(a.mu_oi_call-a.spot)/a.spot*100:+.1f}% do spot")}
        {_sc("μ Puts", f"R$ {a.mu_oi_put:.1f}",
             f"{(a.mu_oi_put-a.spot)/a.spot*100:+.1f}% do spot")}
      </div>

      <!-- DISTRIBUTION -->
      <div class="stat-section-title">Distribuição estatística</div>
      <div class="stats-grid">
        {_sc("Skew", f"{a.skewness:+.3f}",
             "Simétrica" if abs(a.skewness)<0.3 else "Cauda " + ("direita" if a.skewness>0 else "esquerda"))}
        {_sc("Curtose", f"{a.kurtosis:.2f}",
             f"Excesso: {a.excess_kurtosis:+.2f}")}
        {_sc("HHI", f"{a.hhi:.4f}")}
        {_sc("Gini", f"{a.gini:.3f}")}
        {_sc("Entropia", f"{a.entropy:.2f} bits", f"Ratio: {a.entropy_ratio:.1%}")}
        {_sc("R² (Normal)", f"{a.normal_r2:.4f}",
             "Bom ajuste" if a.normal_r2>=0.7 else "Ajuste fraco",
             "#148A63" if a.normal_r2>=0.7 else "#B8720A" if a.normal_r2>=0.4 else "#B33530")}
        {_sc("OI ±1σ do MP", f"{a.pct_1std:.1f}%", "Normal: 68.3%")}
        {_sc("OI ±2σ do MP", f"{a.pct_2std:.1f}%", "Normal: 95.4%")}
      </div>

      <!-- CHARTS -->
      <div class="charts-2col">
        <div class="ch-card"><h4>OI + ajuste normal + centros</h4>
          <div class="ch-wrap"><canvas id="oi_{idx}"></canvas></div></div>
        <div class="ch-card"><h4>GEX por strike</h4>
          <div class="ch-wrap"><canvas id="gex_{idx}"></canvas></div></div>
      </div>
      <div class="charts-2col">
        <div class="ch-card"><h4>DEX por strike</h4>
          <div class="ch-wrap"><canvas id="dex_{idx}"></canvas></div></div>
        <div class="ch-card"><h4>Curva de max pain</h4>
          <div class="ch-wrap"><canvas id="mp_{idx}"></canvas></div></div>
      </div>

      <script>
      (function(){{
        const D={chart_json};
        function vline(ctx,xs,ys,val,arr,color,lbl){{
          const i=arr.reduce((a,v,j)=>Math.abs(v-val)<Math.abs(arr[a]-val)?j:a,0);
          const x=xs.getPixelForValue(i);
          ctx.save();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([5,4]);
          ctx.beginPath();ctx.moveTo(x,ys.top);ctx.lineTo(x,ys.bottom);ctx.stroke();
          ctx.fillStyle=color;ctx.font="600 9px var(--mono)";ctx.textAlign='center';
          ctx.textBaseline='bottom';ctx.fillText(lbl,x,ys.top+8);ctx.restore();
        }}
        function yfmt(v){{
          const a=Math.abs(v);
          if(a>=1e6)return(v/1e6).toFixed(1)+'M';
          if(a>=1e3)return(v/1e3).toFixed(0)+'k';
          return Math.round(v);
        }}
        const tOpt={{font:{{size:9,family:'var(--mono)'}}}};
        const gOpt={{color:'rgba(100,100,100,0.1)'}};

        // OI + Normal + Centers
        new Chart(document.getElementById('oi_{idx}'),{{
          type:'bar',
          data:{{labels:D.strikes,datasets:[
            {{label:'OI Total',data:D.total_oi,backgroundColor:'rgba(90,133,24,0.5)',
              borderColor:'rgba(90,133,24,0.8)',borderWidth:1,borderRadius:2,barPercentage:0.8,order:2}},
            {{label:'Normal Fit',data:D.fit,type:'line',borderColor:'#B8720A',
              borderWidth:2,pointRadius:0,tension:0.4,fill:false,order:1}}
          ]}},
          options:{{responsive:true,maintainAspectRatio:false,
            interaction:{{mode:'index',intersect:false}},
            plugins:{{legend:{{position:'top',labels:{{boxWidth:8,padding:10,font:{{size:9}}}}}}}},
            layout:{{padding:{{top:16}}}},
            scales:{{x:{{ticks:tOpt,grid:{{display:false}}}},y:{{ticks:{{callback:yfmt,...tOpt}},grid:gOpt}}}}
          }},
          plugins:[{{afterDraw(c){{
            const xs=c.scales.x,ys=c.scales.y,ctx=c.ctx;
            vline(ctx,xs,ys,D.mp,D.strikes,'#F5A623','MP');
            vline(ctx,xs,ys,D.mu_oi,D.strikes,'#4A9EFF','μOI');
            vline(ctx,xs,ys,D.mu_gex,D.strikes,'#2ECC71','μGEX');
            vline(ctx,xs,ys,D.mu_dex,D.strikes,'#BB86FC','μDEX');
            vline(ctx,xs,ys,D.spot,D.strikes,'#FF9500','SPOT');
          }}}}]
        }});

        // GEX
        new Chart(document.getElementById('gex_{idx}'),{{
          type:'bar',
          data:{{labels:D.gex.map(d=>d.strike),datasets:[
            {{label:'Net GEX',data:D.gex.map(d=>d.net_gex),
              backgroundColor:D.gex.map(d=>d.net_gex>=0?'rgba(20,138,99,0.6)':'rgba(179,53,48,0.55)'),
              borderRadius:2,barPercentage:0.85}}
          ]}},
          options:{{responsive:true,maintainAspectRatio:false,
            plugins:{{legend:{{display:false}}}},
            layout:{{padding:{{top:16}}}},
            scales:{{x:{{ticks:tOpt,grid:{{display:false}}}},y:{{ticks:{{callback:yfmt,...tOpt}},grid:gOpt}}}}
          }},
          plugins:[{{afterDraw(c){{
            const xs=c.scales.x,ys=c.scales.y,ctx=c.ctx;
            vline(ctx,xs,ys,D.mu_gex,D.gex.map(d=>d.strike),'#2ECC71','μGEX');
            if(D.gex_flip) vline(ctx,xs,ys,D.gex_flip,D.gex.map(d=>d.strike),'#FF6B6B','FLIP');
          }}}}]
        }});

        // DEX
        new Chart(document.getElementById('dex_{idx}'),{{
          type:'bar',
          data:{{labels:D.dex.map(d=>d.strike),datasets:[
            {{label:'Net DEX',data:D.dex.map(d=>d.net_dex),
              backgroundColor:D.dex.map(d=>d.net_dex>=0?'rgba(20,138,99,0.6)':'rgba(179,53,48,0.55)'),
              borderRadius:2,barPercentage:0.85}}
          ]}},
          options:{{responsive:true,maintainAspectRatio:false,
            plugins:{{legend:{{display:false}}}},
            layout:{{padding:{{top:16}}}},
            scales:{{x:{{ticks:tOpt,grid:{{display:false}}}},y:{{ticks:{{callback:yfmt,...tOpt}},grid:gOpt}}}}
          }},
          plugins:[{{afterDraw(c){{
            const xs=c.scales.x,ys=c.scales.y,ctx=c.ctx;
            vline(ctx,xs,ys,D.mu_dex,D.dex.map(d=>d.strike),'#BB86FC','μDEX');
          }}}}]
        }});

        // Max Pain Curve
        new Chart(document.getElementById('mp_{idx}'),{{
          type:'line',
          data:{{labels:D.mp_curve.map(d=>d.strike),datasets:[
            {{label:'Perda total',data:D.mp_curve.map(d=>d.total_loss),borderColor:'#B8720A',
              backgroundColor:'rgba(184,114,10,0.1)',borderWidth:1.8,fill:true,tension:0.3,pointRadius:0}},
            {{label:'Call loss',data:D.mp_curve.map(d=>d.call_loss),borderColor:'#148A63',
              borderWidth:1,borderDash:[3,2],fill:false,tension:0.3,pointRadius:0}},
            {{label:'Put loss',data:D.mp_curve.map(d=>d.put_loss),borderColor:'#B33530',
              borderWidth:1,borderDash:[3,2],fill:false,tension:0.3,pointRadius:0}}
          ]}},
          options:{{responsive:true,maintainAspectRatio:false,
            interaction:{{mode:'index',intersect:false}},
            plugins:{{legend:{{position:'top',labels:{{boxWidth:8,padding:10,font:{{size:9}}}}}}}},
            scales:{{x:{{ticks:tOpt,grid:{{display:false}}}},y:{{ticks:{{callback:v=>(v/1e6).toFixed(1)+'M',...tOpt}},grid:gOpt}}}}
          }}
        }});
      }})();
      </script>
    </div>"""


def generate_html(results: list[FullAnalysis], ref: date) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    ref_str = ref.strftime("%d/%b/%Y")
    spot = results[0].spot if results else 0

    nav = ""
    for i, a in enumerate(results):
        cc = "#148A63" if a.convergence_score>=75 else "#5A8518" if a.convergence_score>=50 else "#B8720A" if a.convergence_score>=25 else "#B33530"
        sc_c = "#B33530" if a.sentiment=="Bearish" else "#148A63" if a.sentiment=="Bullish" else "#6B6960"
        nav += f"""
        <a class="nav-chip" href="#" onclick="event.preventDefault();
           document.getElementById('exp{i}').scrollIntoView({{behavior:'smooth'}})">
          <div class="nc-top">{a.label} <span class="nc-dte">{a.dte}D</span></div>
          <div class="nc-mid">
            <span style="color:{cc}">{a.convergence_score:.0f}%</span>
            <span style="color:{sc_c}">{a.sentiment}</span>
          </div>
          <div class="nc-bot">MP R${a.max_pain:.0f} · μGEX R${a.mu_gex:.1f}</div>
        </a>"""

    cards = "\n".join(build_card(a, i) for i, a in enumerate(results))

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — Mapa Gravitacional</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
:root {{
  --bg:#FAFAF8;--bg2:#F2F1EE;--bg3:#E8E7E3;--card:#FFFFFF;
  --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
  --t1:#1A1A18;--t2:#6B6960;--t3:#9C9A91;
  --green:#148A63;--lime:#5A8518;--amber:#B8720A;--red:#B33530;--blue:#2E6BBF;--purple:#8C5CC4;
  --font:'Instrument Sans',system-ui,sans-serif;--mono:'JetBrains Mono',monospace;
}}
[data-theme="dark"] {{
  --bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--card:#21262d;
  --border:rgba(255,255,255,0.1);--border2:rgba(255,255,255,0.16);
  --t1:#c9d1d9;--t2:#8b949e;--t3:#636c76;
  --green:#3fb950;--lime:#7ee787;--amber:#d29922;--red:#f85149;--blue:#58a6ff;--purple:#bc8cff;
}}
#theme-toggle{{position:fixed;top:16px;right:16px;z-index:999;background:var(--card);
  border:1px solid var(--border2);border-radius:8px;padding:6px 10px;cursor:pointer;
  font-size:16px;line-height:1;box-shadow:0 2px 8px rgba(0,0,0,0.12);transition:all 0.2s}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
body{{background:var(--bg);color:var(--t1);font-family:var(--font);-webkit-font-smoothing:antialiased}}
.shell{{max-width:1200px;margin:0 auto;padding:48px 28px 80px}}

.hdr{{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:36px;
      border-bottom:1px solid var(--border);padding-bottom:24px;flex-wrap:wrap;gap:16px}}
.hdr h1{{font-size:26px;font-weight:700;letter-spacing:-0.03em}}
.hdr h1 span{{color:var(--t3);font-weight:400;font-size:18px}}
.hdr .meta{{font-size:11px;color:var(--t3);font-family:var(--mono);text-align:right;line-height:1.7}}

.nav-strip{{display:flex;gap:6px;overflow-x:auto;margin-bottom:36px;padding-bottom:4px}}
.nav-chip{{flex:0 0 auto;padding:10px 14px;border-radius:10px;background:var(--card);
           border:1px solid var(--border);cursor:pointer;transition:all 0.2s;
           text-decoration:none;display:block;min-width:165px;
           box-shadow:0 1px 2px rgba(0,0,0,0.03)}}
.nav-chip:hover{{border-color:var(--border2);box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
.nc-top{{font-size:12px;font-weight:600;color:var(--t1);margin-bottom:4px}}
.nc-dte{{font-size:10px;color:var(--t3);font-family:var(--mono);font-weight:400}}
.nc-mid{{display:flex;gap:8px;font-size:14px;font-weight:700;font-family:var(--mono)}}
.nc-bot{{font-size:10px;color:var(--t3);font-family:var(--mono);margin-top:3px}}

.exp-block{{background:var(--card);border:1px solid var(--border);border-radius:16px;
            padding:28px;margin-bottom:28px;
            box-shadow:0 1px 3px rgba(0,0,0,0.04),0 4px 12px rgba(0,0,0,0.03);
            animation:fadeUp 0.5s ease both}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:translateY(0)}}}}
.exp-top{{display:flex;justify-content:space-between;align-items:flex-start;
          margin-bottom:20px;flex-wrap:wrap;gap:12px}}
.exp-top h2{{font-size:20px;font-weight:600;letter-spacing:-0.02em;margin-bottom:6px}}
.tag{{display:inline-block;font-size:10px;font-weight:600;font-family:var(--mono);
     padding:3px 8px;border-radius:5px;text-transform:uppercase;letter-spacing:0.04em}}
.tw{{background:rgba(46,107,191,0.1);color:var(--blue)}}
.tm{{background:rgba(20,138,99,0.1);color:var(--green)}}
.tg{{background:rgba(0,0,0,0.04);color:var(--t2);margin-left:4px}}
.pill{{font-size:11px;font-weight:600;font-family:var(--mono);padding:5px 12px;
       border-radius:16px;color:var(--pc);
       background:color-mix(in srgb,var(--pc) 8%,transparent);
       border:1px solid color-mix(in srgb,var(--pc) 20%,transparent)}}

.stat-section-title{{font-size:10px;font-weight:600;color:var(--t3);
                     text-transform:uppercase;letter-spacing:0.07em;margin:22px 0 8px}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:8px;margin-bottom:4px}}
.st-card{{background:var(--bg2);border-radius:8px;padding:10px 12px}}
.st-label{{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:0.05em;
           font-weight:600;margin-bottom:4px}}
.st-value{{font-size:15px;font-weight:600;font-family:var(--mono);letter-spacing:-0.02em;line-height:1.3}}
.st-sub{{font-size:10px;color:var(--t3);margin-top:3px;line-height:1.4}}

/* Center map */
.center-map-section{{margin-bottom:8px}}
.center-map{{position:relative;height:56px;margin:12px 20px 8px}}
.cm-track{{position:absolute;top:20px;left:0;right:0;height:3px;background:var(--bg3);border-radius:2px}}
.cm-mark{{position:absolute;top:0;transform:translateX(-50%)}}
.cm-dot{{width:10px;height:10px;border-radius:50%;margin:15px auto 0;
         box-shadow:0 0 0 3px rgba(255,255,255,0.8)}}
.cm-label{{font-size:9px;font-family:var(--mono);font-weight:600;text-align:center;
           line-height:1.3;margin-top:6px;white-space:nowrap}}
.cm-legend{{display:flex;flex-wrap:wrap;gap:12px;font-size:10px;font-family:var(--mono);
            padding:0 20px;color:var(--t2)}}

.charts-2col{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}}
@media(max-width:750px){{.charts-2col{{grid-template-columns:1fr}}}}
.ch-card{{background:var(--bg2);border-radius:10px;padding:14px}}
.ch-card h4{{font-size:10px;color:var(--t3);text-transform:uppercase;
             letter-spacing:0.05em;font-weight:600;margin-bottom:8px}}
.ch-wrap{{position:relative;height:220px}}
.ch-wrap canvas{{position:absolute;inset:0}}

.guide-box{{margin:0 0 28px;border:1px solid var(--border);border-radius:12px;background:var(--card);overflow:hidden}}
.guide-box summary{{padding:14px 20px;cursor:pointer;font-size:13px;font-weight:600;
  color:var(--t2);list-style:none;user-select:none}}
.guide-box summary::-webkit-details-marker{{display:none}}
.guide-box[open] summary{{border-bottom:1px solid var(--border);color:var(--t1)}}
.guide-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1px;background:var(--border)}}
.gi{{background:var(--bg2);padding:13px 17px;font-size:12px;color:var(--t2);line-height:1.6}}
.gi b{{color:var(--t1);display:block;margin-bottom:3px}}

.footer{{text-align:center;padding:32px 0 0;margin-top:16px;border-top:1px solid var(--border);
         font-size:11px;color:var(--t3);line-height:1.8}}
</style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()">◐</button>
<div class="shell">
  <div class="hdr">
    <div><h1>BOVA11 <span>Mapa gravitacional</span></h1></div>
    <div class="meta">Referência: {ref_str}<br>Spot ≈ R$ {spot:.2f}<br>Gerado: {now}</div>
  </div>
  <div class="nav-strip">{nav}</div>

  <details class="guide-box" open>
    <summary>📖 Como interpretar — Mapa gravitacional (μOI · μGEX · μDEX)</summary>
    <div class="guide-grid">
      <div class="gi"><b>μOI — Centro de massa volumétrico</b>
        Strike onde a maioria do OI está concentrado, ponderado por quantidade de contratos. Âncora de posicionamento geral — indica onde o mercado "estacionou" mais opções, mas não está diretamente ligado ao hedge behavior dos dealers.</div>
      <div class="gi"><b>μGEX — Centro de gamma (o mais importante)</b>
        Onde os dealers têm maior exposição a gamma. Como dealers hedgeiam gamma continuamente (vendem quando sobe, compram quando cai em GEX positivo), esse centro cria um ponto de atração forte. É o indicador mais preditivo para movimento de preço intraday.</div>
      <div class="gi"><b>μDEX — Centro de delta exposure</b>
        Onde está concentrada a exposição direcional líquida. Indica para qual lado os dealers têm mais exposição de delta e onde precisam hedgear direcionalmente. Sinal direcional de médio prazo.</div>
      <div class="gi"><b>Convergência entre centros</b>
        Quando MP, μOI e μGEX estão próximos (spread &lt; R$2), existe um campo gravitacional forte — múltiplas forças apontam para o mesmo ponto. Score ≥ 75% = zona de pinning muito robusta. Score &lt; 25% = centros divergentes, mercado sem âncora clara.</div>
      <div class="gi"><b>GEX Flip</b>
        O strike onde o GEX muda de negativo para positivo. Abaixo do flip: dealers amplificam movimentos (compram quando sobe, vendem quando cai). Acima: dealers estabilizam. É uma fronteira crítica para entender o comportamento esperado do market maker.</div>
      <div class="gi"><b>Sentimento composto</b>
        Calculado a partir de Skewness + PCR + posição relativa de μCalls vs μPuts + sinal do Net DEX. Bearish: mais puts OTM pesadas (hedge de queda). Bullish: mais calls OTM (posicionamento para alta). Neutro: forças equilibradas.</div>
      <div class="gi"><b>Coerência das forças</b>
        Se o sentimento (bull/bear) aponta na mesma direção do max pain, as forças estão alinhadas — sinal mais confiável. Se opostas, há tensão entre o posicionamento direcional e a gravidade de pinning: o resultado é mais incerto.</div>
    </div>
  </details>

  {cards}
  <div class="footer">
    BOVA11 Mapa Gravitacional — MP · μOI · μGEX · μDEX · Convergência · Sentimento<br>
    Não constitui recomendação de investimento.
  </div>
</div>
<script>
Chart.defaults.color='#9C9A91';
Chart.defaults.borderColor='rgba(0,0,0,0.06)';
Chart.defaults.font.family="'Instrument Sans',sans-serif";
Chart.defaults.font.size=10;
</script>
<script>
(function(){{
  var t=localStorage.getItem('bova11-theme')||'light';
  if(t==='dark'){{document.documentElement.setAttribute('data-theme','dark');document.getElementById('theme-toggle').textContent='◐';}}
}})();
function toggleTheme(){{
  var btn=document.getElementById('theme-toggle');
  if(document.documentElement.getAttribute('data-theme')==='dark'){{
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('bova11-theme','light');
    btn.textContent='◐';
  }}else{{
    document.documentElement.setAttribute('data-theme','dark');
    localStorage.setItem('bova11-theme','dark');
    btn.textContent='◐';
  }}
}}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# 7. MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=".")
    ap.add_argument("--output", default="bova11_gravity_map.html")
    ap.add_argument("--ref-date", default="2026-03-24")
    ap.add_argument("--ref-tag", default="", help="Tag original do CSV (ex: 25posmar). Se vazio, deriva da data.")
    ap.add_argument("--spot", type=float, default=None, help="Spot price (optional, uses infer_spot if not provided)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    ref = datetime.strptime(args.ref_date, "%Y-%m-%d").date()
    # Converte '2026-03-24' → '24mar' para localizar os CSVs
    meses_abbr = {1:"jan",2:"fev",3:"mar",4:"abr",5:"mai",6:"jun",
                  7:"jul",8:"ago",9:"set",10:"out",11:"nov",12:"dez"}
    ref_tag = args.ref_tag if args.ref_tag else f"{ref.day}{meses_abbr[ref.month]}"

    results = analyze_all(data_dir, ref, ref_tag, spot_override=args.spot)
    if not results:
        print("Nenhum arquivo encontrado."); sys.exit(1)

    html = generate_html(results, ref)
    Path(args.output).write_text(html, encoding="utf-8")
    print(f"\n✓ Dashboard salvo em: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
