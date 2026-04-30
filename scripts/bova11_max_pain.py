"""
BOVA11 — Max Pain Relevance Indicator (Multi-Expiration)
=========================================================
Lê dados de opções no formato B3, calcula max pain, GEX sintético,
concentração de OI e gera um dashboard HTML para cada vencimento.

Dependência: pandas
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    from .bova11_expiry_family import collapse_option_rows_by_strike, filter_expiry_family
except ImportError:
    from bova11_expiry_family import collapse_option_rows_by_strike, filter_expiry_family


# ─────────────────────────────────────────────────────────────
# 1.  PARSER – formato B3
# ─────────────────────────────────────────────────────────────

def _parse_br_number(raw: str) -> float:
    """Converte número BR ('1.234,56' / '123,91k' / '8,19M' / '-' / '%')."""
    if not isinstance(raw, str):
        return float(raw) if raw == raw else 0.0   # handle NaN
    s = raw.strip().rstrip("%").replace("\r", "")
    if s in ("", "-", "--"):
        return 0.0
    multiplier = 1.0
    if s.endswith("k"):
        multiplier = 1_000
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1_000_000
        s = s[:-1]
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s) * multiplier
    except ValueError:
        return 0.0


def load_b3_options(path: str | Path) -> pd.DataFrame:
    """
    Lê CSV de opções B3 (fechamento) e retorna DataFrame normalizado com
    colunas: strike, call_oi, put_oi, call_delta, put_delta,
             call_gamma, put_gamma, call_iv, put_iv,
             call_last, put_last, call_bid, call_ask, put_bid, put_ask,
             call_trades, put_trades, call_ticker, put_ticker.
    """
    p = Path(path)
    raw = p.read_bytes().decode("latin-1")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    header = lines[0].split(";")

    rows = []
    for line in lines[1:]:
        cols = line.split(";")
        if len(cols) < 23:
            continue
        rows.append({
            "call_ticker":  cols[0].strip(),
            "call_last":    _parse_br_number(cols[1]),
            "call_oi":      _parse_br_number(cols[2]),
            "call_delta":   _parse_br_number(cols[3]),
            "call_gamma":   _parse_br_number(cols[4]),
            "call_theta":   _parse_br_number(cols[5]),
            "call_vega":    _parse_br_number(cols[6]),
            "call_iv":      _parse_br_number(cols[7]),
            "call_trades":  _parse_br_number(cols[8]),
            "call_bid":     _parse_br_number(cols[9]),
            "call_ask":     _parse_br_number(cols[10]),
            "strike":       _parse_br_number(cols[11]),
            "put_bid":      _parse_br_number(cols[12]),
            "put_ask":      _parse_br_number(cols[13]),
            "put_trades":   _parse_br_number(cols[14]),
            "put_iv":       _parse_br_number(cols[15]),
            "put_vega":     _parse_br_number(cols[16]),
            "put_theta":    _parse_br_number(cols[17]),
            "put_gamma":    _parse_br_number(cols[18]),
            "put_delta":    _parse_br_number(cols[19]),
            "put_oi":       _parse_br_number(cols[20]),
            "put_last":     _parse_br_number(cols[21]),
            "put_ticker":   cols[22].strip(),
        })
    return pd.DataFrame(rows)


def load_b3_volume(path: str | Path) -> pd.DataFrame:
    """Lê CSV de volume B3 e retorna DataFrame com strike, call_vol, put_vol."""
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=["call_ticker", "put_ticker", "strike", "call_vol", "put_vol", "call_oi_v", "put_oi_v"])
    raw = p.read_bytes().decode("latin-1")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    rows = []
    for line in lines[1:]:
        cols = line.split(";")
        if len(cols) < 10:
            continue
        strike = _parse_br_number(cols[5]) if len(cols) > 5 else 0
        if strike == 0:
            continue
        rows.append({
            "call_ticker": cols[0].strip(),
            "put_ticker": cols[10].strip() if len(cols) > 10 else "",
            "strike":   strike,
            "call_vol": _parse_br_number(cols[1]),
            "put_vol":  _parse_br_number(cols[9]) if len(cols) > 9 else 0,
            "call_oi_v": _parse_br_number(cols[2]),
            "put_oi_v":  _parse_br_number(cols[8]) if len(cols) > 8 else 0,
        })
    df = pd.DataFrame(rows)
    if df.empty or "strike" not in df.columns:
        return pd.DataFrame(columns=["call_ticker", "put_ticker", "strike", "call_vol", "put_vol", "call_oi_v", "put_oi_v"])
    # Filter out rows with zero strike (stale W4 entries in monthly files)
    df = df[df["strike"] > 0]
    return df


# ─────────────────────────────────────────────────────────────
# 2.  MAX PAIN
# ─────────────────────────────────────────────────────────────

def calc_max_pain(df: pd.DataFrame) -> tuple[pd.DataFrame, float, float]:
    """
    Calcula max pain a partir de OI e strikes.
    Retorna (DataFrame com curva, strike do max pain, perda mínima).
    """
    strikes = sorted(df["strike"].unique())
    results = []

    for exp_strike in strikes:
        call_loss = 0.0
        put_loss = 0.0
        for _, row in df.iterrows():
            s = row["strike"]
            # Se expira em exp_strike, quanto cada opção perde?
            if s < exp_strike:
                call_loss += (exp_strike - s) * row["call_oi"]
            if s > exp_strike:
                put_loss += (s - exp_strike) * row["put_oi"]
        results.append({
            "strike": exp_strike,
            "call_loss": call_loss,
            "put_loss": put_loss,
            "total_loss": call_loss + put_loss,
        })

    curve = pd.DataFrame(results)
    idx = curve["total_loss"].idxmin()
    return curve, float(curve.loc[idx, "strike"]), float(curve.loc[idx, "total_loss"])


# ─────────────────────────────────────────────────────────────
# 3.  GEX SINTÉTICO (a partir dos Greeks)
# ─────────────────────────────────────────────────────────────

def calc_gex(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    """
    Calcula Gamma Exposure por strike.
    GEX_call = Gamma × OI × Spot² × 0.01  (positivo)
    GEX_put  = Gamma × OI × Spot² × 0.01  (negativo – dealers short puts)
    """
    factor = spot * spot * 0.01
    if df.empty:
        return pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex"])

    gex = df[["strike"]].copy()
    gex["call_gex"] = df["call_gamma"] * df["call_oi"] * factor
    gex["put_gex"]  = -df["put_gamma"] * df["put_oi"] * factor
    gex["net_gex"]  = gex["call_gex"] + gex["put_gex"]
    return (
        gex.groupby("strike", as_index=False)[["call_gex", "put_gex", "net_gex"]]
        .sum()
        .sort_values("strike")
        .reset_index(drop=True)
    )


# ─────────────────────────────────────────────────────────────
# 4.  SCORING ENGINE  (mesmo do SPY, adaptado)
# ─────────────────────────────────────────────────────────────

@dataclass
class CriterionResult:
    name: str
    label: str
    score: int
    max_score: int = 10
    detail: str = ""
    raw_value: float = 0.0

    @property
    def pct(self) -> float:
        return (self.score / self.max_score) * 100 if self.max_score else 0


@dataclass
class ExpirationAnalysis:
    label: str
    exp_date: str
    exp_type: str          # "Semanal" / "Mensal"
    dte: int
    max_pain: float
    spot: float
    criteria: list[CriterionResult] = field(default_factory=list)
    mp_curve: list[dict] = field(default_factory=list)
    gex_data: list[dict] = field(default_factory=list)
    top_call_oi: list[dict] = field(default_factory=list)
    top_put_oi: list[dict] = field(default_factory=list)
    total_call_oi: float = 0
    total_put_oi: float = 0
    total_call_vol: float = 0
    total_put_vol: float = 0
    net_gex: float = 0

    @property
    def total_score(self) -> int:
        return sum(c.score for c in self.criteria)

    @property
    def max_possible(self) -> int:
        return sum(c.max_score for c in self.criteria)

    @property
    def pct_score(self) -> float:
        return (self.total_score / self.max_possible) * 100 if self.max_possible else 0

    @property
    def distance_pct(self) -> float:
        return (self.spot - self.max_pain) / self.spot * 100

    @property
    def direction(self) -> str:
        if self.max_pain > self.spot:
            return "para cima"
        elif self.max_pain < self.spot:
            return "para baixo"
        return "neutro"

    @property
    def verdict(self) -> str:
        p = self.pct_score
        if p >= 75:   return "Forte atração"
        if p >= 55:   return "Atração moderada"
        if p >= 40:   return "Atração fraca"
        return "Irrelevante"

    @property
    def verdict_color(self) -> str:
        p = self.pct_score
        if p >= 75:   return "#1D9E75"
        if p >= 55:   return "#6B9E1D"
        if p >= 40:   return "#D4870E"
        return "#C9403B"


def score_distance(spot: float, mp: float) -> CriterionResult:
    pct = abs(spot - mp) / spot * 100
    thresholds = [(0.5, 10), (1.0, 8), (2.0, 6), (3.0, 4), (5.0, 2)]
    s = 0
    for t, v in thresholds:
        if pct <= t:
            s = v
            break
    interp = "muito próximo" if pct <= 0.5 else "próximo" if pct <= 1.5 else "distante"
    return CriterionResult("distance", "Distância spot → max pain",
                           s, detail=f"{pct:.2f}%", raw_value=pct)


def score_dte(dte: int) -> CriterionResult:
    mapping = [(0, 10), (1, 9), (2, 8), (3, 7), (5, 5), (10, 3)]
    s = 2
    for t, v in mapping:
        if dte <= t:
            s = v
            break
    return CriterionResult("dte", "Dias até expiração (DTE)",
                           s, detail=f"{dte} DTE", raw_value=float(dte))


def score_curvature(curve_df: pd.DataFrame, mp_strike: float, mp_loss: float) -> CriterionResult:
    offsets = [1, 2, 3]
    losses = []
    for off in offsets:
        above = curve_df[curve_df["strike"] == mp_strike + off]
        below = curve_df[curve_df["strike"] == mp_strike - off]
        if not above.empty and not below.empty:
            losses.append((above["total_loss"].values[0] + below["total_loss"].values[0]) / 2)
    if losses and mp_loss > 0:
        curv = ((sum(losses) / len(losses)) - mp_loss) / mp_loss * 100
    else:
        curv = 0
    thresholds = [(20, 10), (10, 8), (5, 6), (2, 4)]
    s = 2
    for t, v in thresholds:
        if curv >= t:
            s = v
            break
    return CriterionResult("curvature", "Curvatura do vale",
                           s, detail=f"{curv:.1f}%", raw_value=curv)


def score_gex(net_gex: float) -> CriterionResult:
    if net_gex > 50_000:      s = 10
    elif net_gex > 10_000:    s = 8
    elif net_gex > 0:         s = 6
    elif net_gex > -10_000:   s = 4
    else:                     s = 2
    regime = "Positivo — estabilização" if net_gex > 0 else "Negativo — desestabilização"
    return CriterionResult("gex", "Regime de GEX",
                           s, detail=regime, raw_value=net_gex)


def score_oi_concentration(df: pd.DataFrame, mp_strike: float, band: float = 2.0) -> CriterionResult:
    total = df["call_oi"].sum() + df["put_oi"].sum()
    near = df[(df["strike"] >= mp_strike - band) & (df["strike"] <= mp_strike + band)]
    near_oi = near["call_oi"].sum() + near["put_oi"].sum()
    pct = (near_oi / total * 100) if total > 0 else 0
    thresholds = [(30, 10), (20, 8), (15, 6), (10, 4)]
    s = 2
    for t, v in thresholds:
        if pct >= t:
            s = v
            break
    return CriterionResult("oi_conc", "Concentração de OI no max pain",
                           s, detail=f"{pct:.1f}%", raw_value=pct)


def score_pcr(call_oi: float, put_oi: float) -> CriterionResult:
    pcr = put_oi / call_oi if call_oi > 0 else 999
    if 0.7 <= pcr <= 1.3:    s = 8
    elif 0.5 <= pcr <= 1.5:  s = 6
    else:                    s = 3
    interp = "equilibrado" if 0.7 <= pcr <= 1.3 else "desequilibrado"
    return CriterionResult("pcr", "Put/Call Ratio (OI)",
                           s, detail=f"{pcr:.2f}", raw_value=pcr)


def score_volume_activity(call_vol: float, put_vol: float, call_oi: float, put_oi: float) -> CriterionResult:
    """Volume total relativo ao OI — mede atividade de hedge."""
    total_vol = call_vol + put_vol
    total_oi = call_oi + put_oi
    ratio = total_vol / total_oi if total_oi > 0 else 0
    if ratio >= 0.5:    s = 9
    elif ratio >= 0.3:  s = 7
    elif ratio >= 0.1:  s = 5
    elif ratio >= 0.05: s = 3
    else:               s = 1
    return CriterionResult("vol_activity", "Atividade de volume vs OI",
                           s, detail=f"{ratio:.2%}", raw_value=ratio)


# ─────────────────────────────────────────────────────────────
# 5.  ORCHESTRATOR
# ─────────────────────────────────────────────────────────────

VENC_DATES = {
    "27 mar w4":     "2026-03-27",
    "2 abr w1":      "2026-04-02",
    "10 abr w2":     "2026-04-10",
    "17 abr mensal": "2026-04-17",
    "24 abr w2":     "2026-04-24",
    "30 abr w5":     "2026-04-30",
    "15 mai mensal": "2026-05-15",
}

_MESES_PT = {"jan":"Jan","fev":"Fev","mar":"Mar","abr":"Abr","mai":"Mai","jun":"Jun",
             "jul":"Jul","ago":"Ago","set":"Set","out":"Out","nov":"Nov","dez":"Dez"}

def _fmt_label(raw: str) -> str:
    parts = raw.strip().split()
    out = []
    for p in parts:
        lp = p.lower()
        if lp in _MESES_PT:                            out.append(_MESES_PT[lp])
        elif lp.startswith("w") and lp[1:].isdigit():  out.append("— " + p.upper())
        elif lp == "mensal":                            out.append("— Mensal")
        else:                                           out.append(p.capitalize())
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

def _discover_expirations(data_dir: Path, ref_tag: str) -> list:
    import glob as _glob, re as _re
    pattern_sp = str(data_dir / f"venc * fechamento ({ref_tag}).csv")
    pattern_us = str(data_dir / f"venc_*_fechamento__{ref_tag}_.csv")
    found = _glob.glob(pattern_sp) + _glob.glob(pattern_us)
    result, seen = [], set()
    for fpath in sorted(found):
        fname = Path(fpath).name
        lower = fname.lower()
        if "volume" in lower or " copy" in lower:
            continue
        m = _re.match(r'venc_(.+?)_fechamento__', fname)
        label_raw = m.group(1).replace("_", " ").lower() if m else ""
        if not label_raw:
            m = _re.match(r'venc (.+?) fechamento', fname)
            label_raw = m.group(1).lower() if m else ""
        if not label_raw or label_raw in seen: continue
        seen.add(label_raw)
        exp_date = _resolve_exp_date(label_raw)
        exp_type = "Mensal" if "mensal" in label_raw else "Semanal"
        vol_fname = fname.replace(f"({ref_tag}).csv", f"({ref_tag} Volume).csv") \
                        .replace(f"__{ref_tag}_.csv", f"__{ref_tag}_Volume_.csv")
        result.append({"label": _fmt_label(label_raw), "exp_date": exp_date,
                        "exp_type": exp_type, "file": fname, "vol_file": vol_fname})
    result.sort(key=lambda x: x["exp_date"] if x["exp_date"] else "9999-99-99")
    return result


def infer_spot(df: pd.DataFrame) -> float:
    """Infere o spot a partir do delta ATM (delta_call ≈ 0.50)."""
    if df.empty:
        return 0.0
    df_grouped = df.groupby("strike", as_index=False).agg({"call_delta": "mean"})
    best = df_grouped.iloc[(df_grouped["call_delta"] - 0.50).abs().argsort()[:1]]
    return float(best["strike"].values[0])


def analyze_expiration(data_dir: Path, exp_cfg: dict, ref_date: date, spot: float = None) -> Optional[ExpirationAnalysis]:
    """Análise completa de uma expiração. Se spot=None, infere do delta ATM."""
    df_raw = load_b3_options(data_dir / exp_cfg["file"])
    if df_raw.empty:
        return None
    df_raw = filter_expiry_family(df_raw, exp_cfg["exp_date"], exp_cfg["exp_type"])
    if df_raw.empty:
        return None
    df = collapse_option_rows_by_strike(df_raw)

    vol_df = load_b3_volume(data_dir / exp_cfg["vol_file"])
    if not vol_df.empty:
        vol_df = filter_expiry_family(vol_df, exp_cfg["exp_date"], exp_cfg["exp_type"])
        vol_df = collapse_option_rows_by_strike(vol_df)

    # Merge volume
    if not vol_df.empty:
        strikes_in_df = set(df["strike"].values)
        vol_df = vol_df[vol_df["strike"].isin(strikes_in_df)]
        df = df.merge(vol_df[["strike", "call_vol", "put_vol"]],
                      on="strike", how="left").fillna(0)
    else:
        df["call_vol"] = 0
        df["put_vol"] = 0

    # Garante sequência crescente de strike para charts/tabelas
    df = df[df["strike"] > 0].sort_values("strike").reset_index(drop=True)

    if spot is None:
        spot = infer_spot(df)
    dte = 0
    if exp_cfg["exp_date"]:
        exp_date = datetime.strptime(exp_cfg["exp_date"], "%Y-%m-%d").date()
        dte = max((exp_date - ref_date).days, 0)

    # Max Pain
    curve, mp_strike, mp_loss = calc_max_pain(df)

    # GEX
    gex_df = calc_gex(df_raw, spot)
    net_gex = float(gex_df["net_gex"].sum())

    # OI totals
    total_call_oi = df["call_oi"].sum()
    total_put_oi = df["put_oi"].sum()
    total_call_vol = df["call_vol"].sum()
    total_put_vol = df["put_vol"].sum()

    # Top OI
    top_calls = (
        df.nlargest(5, "call_oi")[["strike", "call_oi"]]
        .rename(columns={"call_oi": "oi"})
        .to_dict("records")
    )
    top_puts = (
        df.nlargest(5, "put_oi")[["strike", "put_oi"]]
        .rename(columns={"put_oi": "oi"})
        .to_dict("records")
    )

    # Scoring
    criteria = [
        score_distance(spot, mp_strike),
        score_dte(dte),
        score_curvature(curve, mp_strike, mp_loss),
        score_gex(net_gex),
        score_oi_concentration(df, mp_strike),
        score_pcr(total_call_oi, total_put_oi),
        score_volume_activity(total_call_vol, total_put_vol,
                              total_call_oi, total_put_oi),
    ]

    # Prepare chart data
    mp_curve_data = curve.to_dict("records")
    gex_chart = gex_df[["strike", "call_gex", "put_gex", "net_gex"]].to_dict("records")

    return ExpirationAnalysis(
        label=exp_cfg["label"],
        exp_date=exp_cfg["exp_date"],
        exp_type=exp_cfg["exp_type"],
        dte=dte,
        max_pain=mp_strike,
        spot=spot,
        criteria=criteria,
        mp_curve=mp_curve_data,
        gex_data=gex_chart,
        top_call_oi=top_calls,
        top_put_oi=top_puts,
        total_call_oi=total_call_oi,
        total_put_oi=total_put_oi,
        total_call_vol=total_call_vol,
        total_put_vol=total_put_vol,
        net_gex=net_gex,
    )


# ─────────────────────────────────────────────────────────────
# 6.  HTML DASHBOARD
# ─────────────────────────────────────────────────────────────

def _fmt_k(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.0f}k"
    return f"{v:.0f}"


def build_exp_card_html(a: ExpirationAnalysis, idx: int) -> str:
    """Gera o HTML de um card de expiração."""
    arrow = "↑" if a.max_pain > a.spot else "↓" if a.max_pain < a.spot else "—"
    score_pct = round(a.pct_score)
    pcr = a.total_put_oi / a.total_call_oi if a.total_call_oi > 0 else 0

    criteria_rows = ""
    for c in a.criteria:
        color = "#1D9E75" if c.pct >= 70 else "#D4870E" if c.pct >= 40 else "#C9403B"
        criteria_rows += f"""
        <div class="cr">
          <span class="cr-name">{c.label}</span>
          <span class="cr-det">{c.detail}</span>
          <div class="bar-track"><div class="bar-fill" style="width:{c.pct}%;background:{color}"></div></div>
          <span class="cr-sc" style="color:{color}">{c.score}/{c.max_score}</span>
        </div>"""

    mp_json = json.dumps(a.mp_curve, default=float)
    gex_json = json.dumps(a.gex_data, default=float)

    return f"""
    <div class="exp-card" style="animation-delay:{idx*0.08}s">
      <div class="exp-header">
        <div>
          <h2>{a.label}</h2>
          <span class="exp-type-badge {'weekly' if 'Semanal' in a.exp_type else 'monthly'}">{a.exp_type}</span>
          <span class="dte-badge">{a.dte} DTE</span>
        </div>
        <div class="exp-score" style="--sc-color:{a.verdict_color}">
          <svg viewBox="0 0 36 36" width="56" height="56">
            <path d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                  fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="3"/>
            <path d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                  fill="none" stroke="{a.verdict_color}" stroke-width="3"
                  stroke-dasharray="{score_pct}, 100" stroke-linecap="round"/>
            <text x="18" y="20.5" text-anchor="middle" fill="{a.verdict_color}"
                  font-size="9" font-weight="600" font-family="var(--mono)">{score_pct}%</text>
          </svg>
          <span class="verdict-label">{a.verdict}</span>
        </div>
      </div>

      <div class="key-metrics">
        <div class="km"><span class="km-l">Max Pain</span><span class="km-v">R$ {a.max_pain:.0f}</span></div>
        <div class="km"><span class="km-l">Spot</span><span class="km-v">R$ {a.spot:.0f}</span></div>
        <div class="km"><span class="km-l">Distância</span><span class="km-v" style="color:{a.verdict_color}">{a.distance_pct:+.2f}%</span></div>
        <div class="km"><span class="km-l">Direção</span><span class="km-v">{arrow} {a.direction}</span></div>
        <div class="km"><span class="km-l">PCR (OI)</span><span class="km-v">{pcr:.2f}</span></div>
        <div class="km"><span class="km-l">GEX Líq.</span><span class="km-v">{_fmt_k(a.net_gex)}</span></div>
      </div>

      <div class="criteria-box">
        <h3>Critérios</h3>
        {criteria_rows}
      </div>

      <div class="charts-row">
        <div class="mini-chart">
          <h4>Curva de max pain</h4>
          <canvas id="mp_{idx}" height="180"></canvas>
        </div>
        <div class="mini-chart">
          <h4>GEX por strike</h4>
          <canvas id="gex_{idx}" height="180"></canvas>
        </div>
      </div>

      <script>
      (function() {{
        const MP = {mp_json};
        const GEX = {gex_json};
        const MP_S = {a.max_pain};
        const SPOT = {a.spot};
        const VC = '{a.verdict_color}';

        // Max pain chart
        new Chart(document.getElementById('mp_{idx}'), {{
          type: 'line',
          data: {{
            labels: MP.map(d => d.strike),
            datasets: [
              {{ label:'Total', data: MP.map(d => d.total_loss), borderColor: VC,
                 backgroundColor: VC + '18', borderWidth: 1.8, fill: true, tension: 0.3,
                 pointRadius: 0, pointHoverRadius: 3 }},
              {{ label:'Call', data: MP.map(d => d.call_loss), borderColor: '#1D9E75',
                 borderWidth: 1, borderDash:[3,2], fill: false, tension: 0.3, pointRadius: 0 }},
              {{ label:'Put', data: MP.map(d => d.put_loss), borderColor: '#C9403B',
                 borderWidth: 1, borderDash:[3,2], fill: false, tension: 0.3, pointRadius: 0 }},
            ]
          }},
          options: {{
            responsive: true, maintainAspectRatio: false,
            interaction: {{ mode: 'index', intersect: false }},
            plugins: {{ legend: {{ display: true, position: 'top',
                         labels: {{ boxWidth: 8, padding: 8, font: {{ size: 9 }} }} }} }},
            scales: {{
              x: {{ ticks: {{ maxTicksLimit: 6, font: {{ size: 9, family: 'var(--mono)' }} }},
                    grid: {{ display: false }} }},
              y: {{ ticks: {{ callback: v => (v/1e6).toFixed(1)+'M',
                             font: {{ size: 9, family: 'var(--mono)' }} }},
                    grid: {{ color: 'rgba(255,255,255,0.03)' }} }}
            }}
          }},
          plugins: [{{
            afterDraw(chart) {{
              const xS = chart.scales.x, yS = chart.scales.y, ctx = chart.ctx;
              [{{ val: MP_S, color: VC, lbl: 'MP' }},
               {{ val: SPOT, color: '#3B7DD8', lbl: 'SPOT' }}].forEach(m => {{
                const i = MP.findIndex(d => d.strike >= m.val);
                if (i < 0) return;
                const x = xS.getPixelForValue(i);
                ctx.save(); ctx.strokeStyle = m.color; ctx.lineWidth = 1;
                ctx.setLineDash([3,2]); ctx.beginPath();
                ctx.moveTo(x, yS.top); ctx.lineTo(x, yS.bottom); ctx.stroke();
                ctx.fillStyle = m.color; ctx.font = "600 8px 'JetBrains Mono'";
                ctx.textAlign = 'center'; ctx.fillText(m.lbl, x, yS.top - 3);
                ctx.restore();
              }});
            }}
          }}]
        }});

        // GEX chart
        const gexColors = GEX.map(d => d.net_gex >= 0 ? 'rgba(29,158,117,0.7)' : 'rgba(201,64,59,0.7)');
        new Chart(document.getElementById('gex_{idx}'), {{
          type: 'bar',
          data: {{
            labels: GEX.map(d => d.strike),
            datasets: [{{ label:'Net GEX', data: GEX.map(d => d.net_gex),
                          backgroundColor: gexColors, borderRadius: 2, barPercentage: 0.85 }}]
          }},
          options: {{
            responsive: true, maintainAspectRatio: false,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
              x: {{ ticks: {{ maxTicksLimit: 6, font: {{ size: 9, family: 'var(--mono)' }} }},
                    grid: {{ display: false }} }},
              y: {{ ticks: {{ callback: v => {{
                       const a = Math.abs(v);
                       if (a>=1e6) return (v/1e6).toFixed(0)+'M';
                       if (a>=1e3) return (v/1e3).toFixed(0)+'k';
                       return Math.round(v);
                     }}, font: {{ size: 9, family: 'var(--mono)' }} }},
                    grid: {{ color: 'rgba(255,255,255,0.03)' }} }}
            }}
          }}
        }});
      }})();
      </script>
    </div>"""


def generate_dashboard(analyses: list[ExpirationAnalysis], spot_ref: float) -> str:
    """Gera o HTML completo do dashboard multi-expiração."""

    # Summary row data
    summary_json = json.dumps([{
        "label": a.label,
        "mp": a.max_pain,
        "spot": a.spot,
        "dist": round(a.distance_pct, 2),
        "dte": a.dte,
        "score": round(a.pct_score),
        "verdict": a.verdict,
        "color": a.verdict_color,
        "direction": a.direction,
    } for a in analyses])

    cards = "\n".join(build_exp_card_html(a, i) for i, a in enumerate(analyses))

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — Max Pain Relevance Indicator</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
:root {{
  --bg: #FAFAF8;
  --bg2: #F0EFEC;
  --bg3: #E5E4E0;
  --card: #FFFFFF;
  --border: rgba(0,0,0,0.08);
  --border2: rgba(0,0,0,0.12);
  --t1: #1A1A18;
  --t2: #5A5A58;
  --t3: #8A8A88;
  --green: #1D9E75;
  --lime: #6B9E1D;
  --amber: #D4870E;
  --red: #C9403B;
  --blue: #3B7DD8;
  --font: 'Instrument Sans', system-ui, sans-serif;
  --mono: 'JetBrains Mono', monospace;
}}
[data-theme="dark"] {{
  --bg: #08090C;
  --bg2: #0F1116;
  --bg3: #161921;
  --card: #111318;
  --border: rgba(255,255,255,0.055);
  --border2: rgba(255,255,255,0.10);
  --t1: #E4E2DB;
  --t2: #8A877F;
  --t3: #4E4C47;
}}
*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ scroll-behavior: smooth; }}
body {{ background:var(--bg); color:var(--t1); font-family:var(--font); -webkit-font-smoothing:antialiased; }}

/* ── Layout ── */
.shell {{ max-width:1200px; margin:0 auto; padding:48px 28px 80px; }}
.top-bar {{
  display:flex; justify-content:space-between; align-items:flex-end;
  margin-bottom:40px; flex-wrap:wrap; gap:16px;
  border-bottom:1px solid var(--border); padding-bottom:24px;
}}
.top-bar h1 {{ font-size:28px; font-weight:700; letter-spacing:-0.03em; line-height:1.1; }}
.top-bar h1 span {{ color:var(--t3); font-weight:400; }}
.top-bar .meta {{ font-size:12px; color:var(--t3); font-family:var(--mono); text-align:right; }}

/* ── Summary strip ── */
.summary {{
  display:flex; gap:6px; margin-bottom:36px; overflow-x:auto;
  padding-bottom:4px; scrollbar-width:thin;
}}
.summary::-webkit-scrollbar {{ height:3px; }}
.summary::-webkit-scrollbar-thumb {{ background:var(--t3); border-radius:2px; }}
.sum-chip {{
  flex:0 0 auto; padding:10px 16px; border-radius:10px;
  background:var(--card); border:1px solid var(--border);
  cursor:pointer; transition:all 0.2s; min-width:160px;
  text-decoration:none; display:block;
}}
.sum-chip:hover {{ border-color:var(--border2); background:var(--bg3); }}
.sum-chip .sc-label {{ font-size:12px; color:var(--t2); margin-bottom:4px; }}
.sum-chip .sc-row {{ display:flex; align-items:baseline; gap:8px; }}
.sum-chip .sc-score {{ font-size:22px; font-weight:700; font-family:var(--mono); }}
.sum-chip .sc-verdict {{ font-size:11px; font-weight:500; }}
.sum-chip .sc-mp {{ font-size:11px; color:var(--t3); font-family:var(--mono); margin-top:3px; }}

/* ── Cards ── */
.exp-card {{
  background:var(--card); border:1px solid var(--border);
  border-radius:16px; padding:28px; margin-bottom:24px;
  animation: fadeUp 0.5s ease both;
}}
@keyframes fadeUp {{
  from {{ opacity:0; transform:translateY(16px); }}
  to   {{ opacity:1; transform:translateY(0); }}
}}
.exp-header {{
  display:flex; justify-content:space-between; align-items:flex-start;
  margin-bottom:20px; flex-wrap:wrap; gap:12px;
}}
.exp-header h2 {{ font-size:20px; font-weight:600; letter-spacing:-0.02em; margin-bottom:6px; }}
.exp-type-badge,.dte-badge {{
  display:inline-block; font-size:10px; font-weight:600; font-family:var(--mono);
  padding:3px 8px; border-radius:5px; text-transform:uppercase; letter-spacing:0.04em;
}}
.exp-type-badge.weekly {{ background:rgba(59,125,216,0.12); color:var(--blue); }}
.exp-type-badge.monthly {{ background:rgba(29,158,117,0.12); color:var(--green); }}
.dte-badge {{ background:rgba(255,255,255,0.05); color:var(--t2); margin-left:4px; }}
.exp-score {{ display:flex; align-items:center; gap:10px; }}
.verdict-label {{ font-size:13px; font-weight:600; color:var(--sc-color); }}

/* ── Key metrics ── */
.key-metrics {{
  display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:8px; margin-bottom:20px;
}}
.km {{
  background:var(--bg2); border-radius:8px; padding:10px 12px;
  display:flex; flex-direction:column; gap:2px;
}}
.km-l {{ font-size:10px; color:var(--t3); text-transform:uppercase; letter-spacing:0.05em; font-weight:600; }}
.km-v {{ font-size:15px; font-weight:600; font-family:var(--mono); letter-spacing:-0.02em; }}

/* ── Criteria ── */
.criteria-box {{ margin-bottom:20px; }}
.criteria-box h3 {{
  font-size:11px; font-weight:600; color:var(--t3);
  text-transform:uppercase; letter-spacing:0.06em; margin-bottom:10px;
}}
.cr {{
  display:grid; grid-template-columns:1.2fr auto 80px 40px;
  align-items:center; gap:8px; padding:8px 0;
  border-bottom:1px solid var(--border);
}}
.cr:last-child {{ border-bottom:none; }}
.cr-name {{ font-size:12px; }}
.cr-det {{ font-size:10px; color:var(--t2); font-family:var(--mono); text-align:right; }}
.bar-track {{ height:4px; background:var(--bg3); border-radius:2px; overflow:hidden; }}
.bar-fill {{ height:100%; border-radius:2px; transition:width 0.6s ease; }}
.cr-sc {{ font-size:11px; font-weight:600; font-family:var(--mono); text-align:right; }}

/* ── Charts ── */
.charts-row {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
@media(max-width:700px) {{ .charts-row {{ grid-template-columns:1fr; }} }}
.mini-chart {{
  background:var(--bg2); border-radius:10px; padding:14px;
  position:relative; height:220px;
}}
.mini-chart h4 {{
  font-size:10px; color:var(--t3); text-transform:uppercase;
  letter-spacing:0.05em; font-weight:600; margin-bottom:8px;
}}
.mini-chart canvas {{ position:absolute; left:14px; right:14px; bottom:14px; top:40px; }}

/* ── Guide box ── */
.guide-box {{
  margin: 0 0 28px; border: 1px solid var(--border); border-radius: 12px;
  background: var(--card); overflow: hidden;
}}
.guide-box summary {{
  padding: 14px 20px; cursor: pointer; font-size: 13px; font-weight: 600;
  color: var(--t2); list-style: none; user-select: none;
}}
.guide-box summary::-webkit-details-marker {{ display: none }}
.guide-box[open] summary {{ border-bottom: 1px solid var(--border); color: var(--t1); }}
.guide-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1px; background: var(--border);
}}
.gi {{ background: var(--bg2); padding: 13px 17px; font-size: 12px; color: var(--t2); line-height: 1.6; }}
.gi b {{ color: var(--t1); display: block; margin-bottom: 3px; }}

/* ── Theme toggle ── */
.theme-toggle {{
  position: fixed; top: 20px; right: 20px; z-index: 999;
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 12px; cursor: pointer;
  font-size: 14px; font-weight: 600; color: var(--t2);
  transition: all 0.2s; user-select: none;
}}
.theme-toggle:hover {{
  border-color: var(--blue); color: var(--blue); background: var(--bg2);
}}

/* ── Footer ── */
.footer {{
  text-align:center; padding:32px 0 0; margin-top:16px;
  border-top:1px solid var(--border);
  font-size:11px; color:var(--t3); line-height:1.8;
}}
</style>
</head>
<body>
<button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()">◐</button>

<div class="shell">

  <div class="top-bar">
    <div>
      <h1>BOVA11 <span>Max Pain Relevance</span></h1>
    </div>
  </div>

  <!-- Summary strip -->
  <div class="summary" id="summaryStrip"></div>

  <!-- Interpretation guide -->
  <details class="guide-box" open>
    <summary>📖 Como interpretar — Max Pain Score (7 critérios)</summary>
    <div class="guide-grid">
      <div class="gi"><b>1. Distância Spot → Max Pain</b>
        Quanto mais próximo o preço atual está do max pain, maior a probabilidade de ancoragem. Distância &lt;0.5% + DTE baixo = sinal muito forte de pinning. O preço tende a convergir para o max pain conforme o vencimento se aproxima.</div>
      <div class="gi"><b>2. DTE — Dias até expiração</b>
        O efeito de pinning se intensifica nas últimas horas antes do vencimento. DTE = 0 (hoje) = score máximo. DTE &gt; 10 = efeito ainda fraco, outros fatores dominam o movimento do preço.</div>
      <div class="gi"><b>3. Curvatura do vale</b>
        Quão acentuado é o "poço" da curva de max pain. Curvatura alta: o preço "sente" a gravidade nos strikes adjacentes. Curvatura rasa: max pain fraco, o preço pode sair sem resistência significativa.</div>
      <div class="gi"><b>4. Regime de GEX</b>
        GEX positivo: dealers compram quando o preço cai e vendem quando sobe — estabilizam perto do max pain. GEX negativo: dealers amplificam movimentos em vez de frear — o efeito de pinning é enfraquecido.</div>
      <div class="gi"><b>5. Concentração de OI no Max Pain</b>
        Quanto mais OI está concentrado exatamente no max pain (e não disperso por toda a cadeia), maior o campo gravitacional. Mede a % do OI total dentro de ±R$2 do max pain.</div>
      <div class="gi"><b>6. PCR — Put/Call Ratio (OI)</b>
        PCR ≈ 1.0: forças equilibradas dos dois lados, pinning mais robusto. PCR muito alto ou baixo indica um lado dominante, o que pode fazer o preço "escapar" em direção ao desequilíbrio.</div>
      <div class="gi"><b>7. Atividade de volume vs OI</b>
        Volume alto relativo ao OI indica posições sendo abertas/fechadas ativamente — hedge mais agressivo dos dealers, geralmente reforçando o pinning. Volume baixo = posições estacionadas, menos pressão de hedge.</div>
    </div>
  </details>

  <!-- Expiration cards -->
  {cards}

  <div class="footer">
    BOVA11 Max Pain Relevance Indicator — Análise quantitativa de opções B3<br>
    Não constitui recomendação de investimento.
  </div>
</div>

<script>
// Summary chips
const SUM = {summary_json};
const strip = document.getElementById('summaryStrip');
SUM.forEach((s,i) => {{
  const chip = document.createElement('a');
  chip.href = '#';
  chip.className = 'sum-chip';
  chip.onclick = e => {{
    e.preventDefault();
    document.querySelectorAll('.exp-card')[i].scrollIntoView({{ behavior:'smooth', block:'start' }});
  }};
  chip.innerHTML = `
    <div class="sc-label">${{s.label}}</div>
    <div class="sc-row">
      <span class="sc-score" style="color:${{s.color}}">${{s.score}}%</span>
      <span class="sc-verdict" style="color:${{s.color}}">${{s.verdict}}</span>
    </div>
    <div class="sc-mp">MP R$${{s.mp}} · ${{s.dte}} DTE · ${{s.direction}}</div>`;
  strip.appendChild(chip);
}});

// Theme toggle
function toggleTheme() {{
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const next = dark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  const btn = document.getElementById('theme-toggle');
  btn.textContent = '◐';
  try {{ localStorage.setItem('bova11_maxpain_theme', next); }} catch(e) {{}}
}}
(function() {{
  const saved = localStorage.getItem('bova11_maxpain_theme');
  if (saved === 'dark') {{
    document.documentElement.setAttribute('data-theme', 'dark');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '◐';
  }}
}})();

// Chart.js defaults
Chart.defaults.color = 'var(--t3)';
Chart.defaults.borderColor = 'var(--border)';
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
    parser = argparse.ArgumentParser(description="BOVA11 Max Pain Indicator")
    parser.add_argument("--data-dir", default=".", help="Diretório com os CSVs")
    parser.add_argument("--output", default="bova11_dashboard.html")
    parser.add_argument("--ref-date", default="2026-03-24", help="Data de referência (YYYY-MM-DD)")
    parser.add_argument("--ref-tag", default="", help="Tag original do CSV (ex: 25posmar). Se vazio, deriva da data.")
    parser.add_argument("--spot", type=float, default=None, help="Spot price (se vazio, infere do delta ATM)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    ref = datetime.strptime(args.ref_date, "%Y-%m-%d").date()
    _meses = {1:"jan",2:"fev",3:"mar",4:"abr",5:"mai",6:"jun",
              7:"jul",8:"ago",9:"set",10:"out",11:"nov",12:"dez"}
    ref_tag = args.ref_tag if args.ref_tag else f"{ref.day}{_meses[ref.month]}"
    expirations = _discover_expirations(data_dir, ref_tag)

    analyses = []
    for exp in expirations:
        f = data_dir / exp["file"]
        if not f.exists():
            print(f"  ⚠ Arquivo não encontrado: {f}, pulando...")
            continue
        print(f"  → Analisando {exp['label']}...")
        a = analyze_expiration(data_dir, exp, ref, spot=args.spot)
        if a is None:
            print(f"    ⚠ Sem dados, pulando...")
            continue
        analyses.append(a)
        print(f"    MP=R${a.max_pain:.0f}  Spot≈R${a.spot:.0f}  "
              f"Dist={a.distance_pct:+.2f}%  Score={a.pct_score:.0f}%  → {a.verdict}")

    if not analyses:
        print("Nenhum vencimento encontrado. Verifique o diretório.")
        sys.exit(1)

    spot_ref = args.spot if args.spot else analyses[0].spot  # use provided spot or from first expiration
    html = generate_dashboard(analyses, spot_ref)
    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    print(f"\n✓ Dashboard salvo em: {out.resolve()}")


if __name__ == "__main__":
    main()
