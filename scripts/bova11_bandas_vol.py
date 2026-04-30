#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 — Bandas de Volatilidade (Módulo 16)
============================================
Combina IV implícita dos CSVs da B3 com dados históricos do yfinance
e modelagem GARCH(1,1) para gerar bandas de volatilidade esperada,
análise de regime e calculadora de movimento esperado.

Dependências obrigatórias : yfinance, pandas, numpy
Dependência opcional      : arch  (fallback EWMA se ausente)

Uso:
  python3 bova11_bandas_vol.py \\
    --data-dir /path/to/data \\
    --output   /path/to/output/bova11_bandas_vol.html \\
    --ref-date 2026-03-25 \\
    --ref-tag  25posmar \\
    --spot     188.50
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────
# 1. PARSER B3
# ─────────────────────────────────────────────────────────────

def _p(raw) -> float:
    """Converte número BR (com pontos de milhar e vírgula decimal) para float."""
    if not isinstance(raw, str):
        return float(raw) if raw == raw else 0.0
    s = raw.strip().rstrip("%").replace("\r", "")
    if s in ("", "-", "--"):
        return 0.0
    m = 1.0
    if s.endswith("k"):
        m = 1_000
        s = s[:-1]
    elif s.endswith("M"):
        m = 1_000_000
        s = s[:-1]
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s) * m
    except Exception:
        return 0.0


def load_b3(path: Path) -> list[dict]:
    """Carrega CSV B3 e retorna lista de dicts com strike, IV, OI."""
    raw = path.read_bytes().decode("latin-1")
    rows = []
    for line in raw.splitlines()[1:]:
        c = line.strip().split(";")
        if len(c) < 23:
            continue
        strike = _p(c[11])
        if strike == 0:
            continue
        rows.append({
            "strike":  strike,
            "call_iv": _p(c[7]),
            "put_iv":  _p(c[15]),
            "call_oi": _p(c[2]),
            "put_oi":  _p(c[20]),
        })
    return rows


# ─────────────────────────────────────────────────────────────
# 2. DESCOBERTA DE CSVs
# ─────────────────────────────────────────────────────────────

def _normalize_tag(tag: str) -> str:
    """Remove prefixos pos/pre de um tag de data."""
    return re.sub(r"(?i)(pos|pre)([a-z]{3})$", r"\2", tag)


def find_csvs_for_tag(data_dir: str, ref_tag: str) -> list[Path]:
    """Encontra todos os CSVs de fechamento (sem Volume) para um ref_tag."""
    norm = _normalize_tag(ref_tag.lower())
    candidates = []

    # Padrão com underscores
    for fp in glob.glob(os.path.join(data_dir, "venc_*_fechamento__*_.csv")):
        bn = os.path.basename(fp)
        m = re.search(r"fechamento__([a-zA-Z0-9]+)_\.csv$", bn)
        if not m:
            continue
        t = m.group(1).lower()
        if t == ref_tag.lower() or _normalize_tag(t) == norm:
            if "volume" not in bn.lower() and "vol" not in bn.lower():
                candidates.append(Path(fp))

    # Padrão com espaços
    for fp in glob.glob(os.path.join(data_dir, "venc * fechamento (*).csv")):
        bn = os.path.basename(fp)
        m = re.search(r"fechamento \(([a-zA-Z0-9]+)\)\.csv$", bn)
        if not m:
            continue
        t = m.group(1).lower()
        if t == ref_tag.lower() or _normalize_tag(t) == norm:
            if "volume" not in bn.lower():
                candidates.append(Path(fp))

    return candidates


# ─────────────────────────────────────────────────────────────
# 3. EXTRAIR LABEL DE VENCIMENTO DO NOME DO ARQUIVO
# ─────────────────────────────────────────────────────────────

def venc_label_from_path(path: Path) -> str:
    """Extrai o label de vencimento do nome do arquivo B3."""
    bn = path.stem
    # Padrão underscore: venc_6_mar_W1_fechamento__5mar_
    m = re.match(r"venc_(.+?)_fechamento", bn, re.IGNORECASE)
    if m:
        return m.group(1).replace("_", " ").strip()
    # Padrão espaço: "venc 6 mar W1 fechamento (5mar)"
    m = re.match(r"venc\s+(.+?)\s+fechamento", bn, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return bn


# ─────────────────────────────────────────────────────────────
# 4. CALCULAR IV ATM E SMILE
# ─────────────────────────────────────────────────────────────

def compute_atm_iv(rows: list[dict], spot: float) -> tuple[float, float]:
    """
    Retorna (atm_iv_decimal, total_oi) para os dados de um vencimento.
    ATM IV = média ponderada por OI de call_iv e put_iv na strike mais próxima do spot.
    IV em percent nos CSVs (e.g. 26.23) — divide por 100 para obter decimal.
    """
    if not rows:
        return 0.0, 0.0

    # Strike mais próxima do spot
    atm_row = min(rows, key=lambda r: abs(r["strike"] - spot))

    call_iv = atm_row["call_iv"] / 100.0
    put_iv  = atm_row["put_iv"]  / 100.0
    call_oi = atm_row["call_oi"]
    put_oi  = atm_row["put_oi"]

    total_oi = call_oi + put_oi
    if total_oi > 0:
        atm_iv = (call_iv * call_oi + put_iv * put_oi) / total_oi
    elif call_iv > 0 and put_iv > 0:
        atm_iv = (call_iv + put_iv) / 2.0
    elif call_iv > 0:
        atm_iv = call_iv
    else:
        atm_iv = put_iv

    return atm_iv, total_oi


def compute_smile(rows: list[dict]) -> tuple[list[float], list[float], list[float]]:
    """
    Retorna (strikes, call_ivs, put_ivs) para plotar o smile de IV.
    Filtra apenas strikes com pelo menos alguma IV válida.
    """
    strikes, call_ivs, put_ivs = [], [], []
    for r in sorted(rows, key=lambda x: x["strike"]):
        if r["call_iv"] > 0 or r["put_iv"] > 0:
            strikes.append(r["strike"])
            call_ivs.append(round(r["call_iv"], 2))
            put_ivs.append(round(r["put_iv"], 2))
    return strikes, call_ivs, put_ivs


# ─────────────────────────────────────────────────────────────
# 5. DTE — dias até vencimento
# ─────────────────────────────────────────────────────────────

_MESES_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4,
    "mai": 5, "jun": 6, "jul": 7, "ago": 8,
    "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def dte_from_label(label: str, ref_date: date) -> int:
    """
    Tenta extrair DTE a partir do label de vencimento.
    Aceita padrões como '6 mar W1', '17 abr Mensal'.
    Retorna 30 como fallback.
    """
    label_l = label.lower()
    for mes_pt, mes_num in _MESES_PT.items():
        if mes_pt in label_l:
            m = re.search(r"(\d{1,2})\s*" + mes_pt, label_l)
            if m:
                dia = int(m.group(1))
                ano = ref_date.year
                try:
                    exp_date = date(ano, mes_num, dia)
                    if exp_date < ref_date:
                        exp_date = date(ano + 1, mes_num, dia)
                    delta = (exp_date - ref_date).days
                    return max(delta, 1)
                except ValueError:
                    pass
    return 30


# ─────────────────────────────────────────────────────────────
# 6. ANÁLISE PRINCIPAL (IV + yfinance + GARCH)
# ─────────────────────────────────────────────────────────────

def run_analysis(data_dir: str, ref_tag: str, spot: float, ref_date: date) -> dict:
    """
    Executa todas as etapas de análise e retorna um dict com todos os resultados.
    """
    result: dict = {
        "spot": spot,
        "ref_date": str(ref_date),
        "ref_tag": ref_tag,
        "atm_iv": None,
        "iv_rank": None,
        "iv_percentile": None,
        "iv_tendency_pct": None,
        "garch_vol": None,
        "garch_available": False,
        "iv_premium": None,
        "hist_vol_30d": None,
        "recent_vol_10d": None,
        "sma_20": None,
        "sma_30": None,
        "bands": {},
        "distances": {},
        "price_history_dates": [],
        "price_history_prices": [],
        "smile_by_venc": {},
        "signal": "",
        "signal_color": "blue",
        "oportunidade": 50,
        "half": "",
        "sugestao": "",
        "prob_reversao": 50,
        "nivel_risco": "MODERADO",
        "nivel_risco_color": "amber",
        "warnings": [],
        "expiries": [],
    }

    # ── 6.1 Carregar CSVs e calcular IV ATM ──────────────────────
    csvs = find_csvs_for_tag(data_dir, ref_tag)
    if not csvs:
        result["warnings"].append(
            f"Nenhum CSV encontrado para ref_tag='{ref_tag}' em '{data_dir}'"
        )
        result["atm_iv"] = 0.20  # fallback
    else:
        atm_ivs_per_venc = []

        for csv_path in csvs:
            label = venc_label_from_path(csv_path)
            rows  = load_b3(csv_path)
            if not rows:
                continue

            atm_iv_dec, total_oi = compute_atm_iv(rows, spot)
            dte = dte_from_label(label, ref_date)

            # Peso inverso do DTE (quanto mais próximo, maior o peso)
            weight = 1.0 / max(dte, 1)
            atm_ivs_per_venc.append((atm_iv_dec, total_oi, weight, label, dte))

            # Smile para este vencimento
            sm_strikes, sm_calls, sm_puts = compute_smile(rows)
            result["smile_by_venc"][label] = {
                "strikes": sm_strikes,
                "call_ivs": sm_calls,
                "put_ivs": sm_puts,
                "dte": dte,
            }

            result["expiries"].append({
                "label": label,
                "atm_iv_pct": round(atm_iv_dec * 100, 2),
                "dte": dte,
                "total_oi": int(total_oi),
            })

        # Smile agregado (média por strike quando há múltiplos vencimentos)
        all_smile_data: dict[float, dict[str, list]] = {}
        for label, sm in result["smile_by_venc"].items():
            for s, c, p in zip(sm["strikes"], sm["call_ivs"], sm["put_ivs"]):
                if s not in all_smile_data:
                    all_smile_data[s] = {"calls": [], "puts": []}
                if c > 0:
                    all_smile_data[s]["calls"].append(c)
                if p > 0:
                    all_smile_data[s]["puts"].append(p)

        if all_smile_data:
            agg_s = sorted(all_smile_data.keys())
            agg_c = [
                round(sum(all_smile_data[s]["calls"]) / len(all_smile_data[s]["calls"]), 2)
                if all_smile_data[s]["calls"] else 0.0
                for s in agg_s
            ]
            agg_p = [
                round(sum(all_smile_data[s]["puts"]) / len(all_smile_data[s]["puts"]), 2)
                if all_smile_data[s]["puts"] else 0.0
                for s in agg_s
            ]
            result["smile_by_venc"]["__aggregated__"] = {
                "strikes": agg_s,
                "call_ivs": agg_c,
                "put_ivs": agg_p,
                "dte": 0,
            }

        # IV ATM ponderada pelo inverso do DTE
        if atm_ivs_per_venc:
            total_weight = sum(w for _, _, w, _, _ in atm_ivs_per_venc)
            if total_weight > 0:
                weighted_iv = sum(iv * w for iv, _, w, _, _ in atm_ivs_per_venc) / total_weight
            else:
                weighted_iv = sum(iv for iv, _, _, _, _ in atm_ivs_per_venc) / len(atm_ivs_per_venc)
            result["atm_iv"] = weighted_iv
        else:
            result["atm_iv"] = 0.20
            result["warnings"].append("Não foi possível calcular IV ATM — usando 20% de fallback.")

    current_iv: float = result["atm_iv"]  # type: ignore[assignment]

    # ── 6.2 Dados históricos via yfinance ─────────────────────────
    import numpy as np

    try:
        import yfinance as yf
        import pandas as pd

        ticker = yf.Ticker("BOVA11.SA")
        hist   = ticker.history(period="1y")

        if hist.empty:
            raise ValueError("yfinance retornou DataFrame vazio para BOVA11.SA")

        prices  = hist["Close"].dropna()
        returns = prices.pct_change().dropna()

        if len(prices) < 30:
            raise ValueError("Histórico insuficiente (< 30 dias)")

        # Volatilidade histórica realizada (janela de 30 dias)
        hist_vol_30d_series = returns.rolling(30).std() * np.sqrt(252)
        hist_vol_30d_clean  = hist_vol_30d_series.dropna()

        result["hist_vol_30d"] = float(hist_vol_30d_clean.iloc[-1])

        # IV Rank (0-100): onde está a IV atual vs min/max do 1Y
        iv_1y_min = float(hist_vol_30d_clean.min())
        iv_1y_max = float(hist_vol_30d_clean.max())
        if iv_1y_max > iv_1y_min:
            result["iv_rank"] = round(
                (current_iv - iv_1y_min) / (iv_1y_max - iv_1y_min) * 100, 1
            )
        else:
            result["iv_rank"] = 50.0

        # IV Percentile: % dos dias do último ano com HV < IV atual
        result["iv_percentile"] = round(
            float((hist_vol_30d_clean < current_iv).sum()) / len(hist_vol_30d_clean) * 100, 1
        )

        # IV Tendency: prêmio da IV sobre a vol realizada recente (10 dias)
        recent_vol = float(returns.tail(10).std() * np.sqrt(252))
        result["recent_vol_10d"] = recent_vol
        if recent_vol > 0:
            result["iv_tendency_pct"] = round(
                (current_iv - recent_vol) / recent_vol * 100, 1
            )
        else:
            result["iv_tendency_pct"] = 0.0

        # SMA 20 e SMA 30
        result["sma_20"] = float(prices.tail(20).mean())
        result["sma_30"] = float(prices.tail(30).mean())

        # Últimos 60 dias para o gráfico
        prices_60 = prices.tail(60)
        result["price_history_dates"]  = [str(d.date()) for d in prices_60.index]
        result["price_history_prices"] = [round(float(v), 2) for v in prices_60.values]

        prices_arr = np.array(prices.values, dtype=float)

        # ── 6.3 GARCH(1,1) ──────────────────────────────────────
        log_returns = np.log(prices_arr[1:] / prices_arr[:-1]) * 100.0

        try:
            from arch import arch_model
            model    = arch_model(log_returns, vol="GARCH", p=1, q=1, dist="normal")
            res_fit  = model.fit(disp="off", show_warning=False)
            forecast = res_fit.forecast(horizon=1)
            garch_vol_daily  = float(np.sqrt(forecast.variance.values[-1, 0])) / 100.0
            garch_vol_annual = garch_vol_daily * math.sqrt(252)
            result["garch_available"] = True
            result["garch_vol"] = round(garch_vol_annual, 4)
        except ImportError:
            # Fallback EWMA (RiskMetrics lambda=0.94)
            ewma_lambda = 0.94
            sq_log_ret  = (log_returns / 100.0) ** 2
            ewma_var = float(sq_log_ret[0])
            for r2 in sq_log_ret[1:]:
                ewma_var = ewma_lambda * ewma_var + (1 - ewma_lambda) * float(r2)
            garch_vol_annual = math.sqrt(ewma_var * 252)
            result["garch_available"] = False
            result["garch_vol"] = round(garch_vol_annual, 4)
            result["warnings"].append(
                "Pacote 'arch' não instalado — usando EWMA (lambda=0.94) como fallback do GARCH."
            )
        except Exception as e:
            result["garch_available"] = False
            result["garch_vol"] = result["hist_vol_30d"]
            result["warnings"].append(f"GARCH falhou: {e}. Usando HV 30d como fallback.")

        if result["garch_vol"] and result["garch_vol"] > 0:
            result["iv_premium"] = round(
                (current_iv - result["garch_vol"]) / result["garch_vol"] * 100, 1
            )

    except Exception as e:
        result["warnings"].append(
            f"yfinance falhou ({e}). Usando apenas IV dos CSVs — sem dados históricos."
        )
        # Fallbacks quando yfinance não disponível
        result["sma_20"]          = spot
        result["sma_30"]          = spot
        result["hist_vol_30d"]    = current_iv
        result["recent_vol_10d"]  = current_iv
        result["iv_rank"]         = 50.0
        result["iv_percentile"]   = 50.0
        result["iv_tendency_pct"] = 0.0
        result["garch_vol"]       = current_iv
        result["iv_premium"]      = 0.0

    # ── 6.4 Bandas de Volatilidade ─────────────────────────────
    sma_30 = result["sma_30"] or spot
    t30    = math.sqrt(30.0 / 252.0)

    result["bands"] = {
        "central":  round(sma_30, 2),
        "upper_1s": round(sma_30 * (1 + 1 * current_iv * t30), 2),
        "lower_1s": round(sma_30 * (1 - 1 * current_iv * t30), 2),
        "upper_2s": round(sma_30 * (1 + 2 * current_iv * t30), 2),
        "lower_2s": round(sma_30 * (1 - 2 * current_iv * t30), 2),
        "upper_4s": round(sma_30 * (1 + 4 * current_iv * t30), 2),
        "lower_4s": round(sma_30 * (1 - 4 * current_iv * t30), 2),
    }

    # Distâncias percentuais do spot para cada banda
    bands = result["bands"]
    result["distances"] = {
        k: round((spot / v - 1) * 100, 2) if v and v != 0 else 0.0
        for k, v in bands.items()
    }

    # ── 6.5 Sinal / Zona atual ─────────────────────────────────
    sma_20     = result["sma_20"] or spot
    t20        = math.sqrt(20.0 / 252.0)
    upper_zone = sma_20 * (1 + current_iv * t20)
    lower_zone = sma_20 * (1 - current_iv * t20)

    if spot > upper_zone:
        result["signal"]       = "ZONA DE SOBRECOMPRA"
        result["signal_color"] = "red"
        result["oportunidade"] = 30
        result["half"]         = "METADE SUPERIOR"
        result["sugestao"]     = (
            "Considere redução de posições compradas. "
            "IV elevada favorece venda de calls cobertas."
        )
        result["prob_reversao"] = 70
    elif spot < lower_zone:
        result["signal"]       = "ZONA DE SOBREVENDA"
        result["signal_color"] = "green"
        result["oportunidade"] = 80
        result["half"]         = "METADE INFERIOR"
        result["sugestao"]     = (
            "Região de suporte por bandas. "
            "IV elevada favorece venda de puts cash-secured ou compra de calls."
        )
        result["prob_reversao"] = 65
    elif spot > sma_20:
        result["signal"]       = "METADE SUPERIOR — Bullish"
        result["signal_color"] = "blue"
        result["oportunidade"] = 45
        result["half"]         = "METADE SUPERIOR"
        result["sugestao"]     = (
            "Tendência de alta moderada. "
            "Manter posições com stop abaixo da SMA20."
        )
        result["prob_reversao"] = 40
    else:
        result["signal"]       = "METADE INFERIOR — Bearish"
        result["signal_color"] = "amber"
        result["oportunidade"] = 65
        result["half"]         = "METADE INFERIOR"
        result["sugestao"]     = (
            "Abaixo da SMA20. Cautela — aguardar reconstrução acima da "
            "média antes de compras direcionais."
        )
        result["prob_reversao"] = 55

    # Nível de risco baseado em IV Rank
    iv_rank_val = result["iv_rank"] or 50.0
    if iv_rank_val >= 80:
        result["nivel_risco"]       = "ALTO"
        result["nivel_risco_color"] = "red"
    elif iv_rank_val >= 50:
        result["nivel_risco"]       = "MODERADO"
        result["nivel_risco_color"] = "amber"
    else:
        result["nivel_risco"]       = "BAIXO"
        result["nivel_risco_color"] = "green"

    return result


# ─────────────────────────────────────────────────────────────
# 7. GERAÇÃO DO HTML
# ─────────────────────────────────────────────────────────────

def _color_hex(color_key: str) -> str:
    mapping = {
        "red":   "#f85149",
        "green": "#3fb950",
        "blue":  "#58a6ff",
        "amber": "#d29922",
    }
    return mapping.get(color_key, "#58a6ff")


def generate_html(data: dict, output_path: str) -> None:
    spot      = data["spot"]
    ref_date  = data["ref_date"]
    ref_tag   = data["ref_tag"]
    atm_iv    = data["atm_iv"] or 0.0
    iv_rank   = data["iv_rank"]   if data["iv_rank"]   is not None else 50.0
    iv_pctile = data["iv_percentile"] if data["iv_percentile"] is not None else 50.0
    iv_tend   = data["iv_tendency_pct"] if data["iv_tendency_pct"] is not None else 0.0
    garch_vol = data["garch_vol"] if data["garch_vol"] is not None else atm_iv
    bands     = data["bands"]
    dists     = data["distances"]
    signal    = data["signal"]
    sig_color = _color_hex(data["signal_color"])
    oport     = data["oportunidade"]
    sugestao  = data["sugestao"]
    prob_rev  = data["prob_reversao"]
    nivel_risco       = data.get("nivel_risco", "MODERADO")
    nivel_risco_color = _color_hex(data.get("nivel_risco_color", "amber"))
    warnings  = data["warnings"]

    # Serialise chart data
    hist_dates  = json.dumps(data["price_history_dates"])
    hist_prices = json.dumps(data["price_history_prices"])

    # Smile data — aggregated preferred, then first available
    smile_map = data["smile_by_venc"]
    if "__aggregated__" in smile_map:
        smile = smile_map["__aggregated__"]
    elif smile_map:
        smile = next(iter(smile_map.values()))
    else:
        smile = {"strikes": [], "call_ivs": [], "put_ivs": []}

    smile_strikes  = json.dumps(smile["strikes"])
    smile_call_ivs = json.dumps(smile["call_ivs"])
    smile_put_ivs  = json.dumps(smile["put_ivs"])

    # Band horizontal line values for chart
    band_upper4  = bands.get("upper_4s", spot)
    band_upper2  = bands.get("upper_2s", spot)
    band_central = bands.get("central",  spot)
    band_lower2  = bands.get("lower_2s", spot)
    band_lower4  = bands.get("lower_4s", spot)

    # Expiry table rows
    expiry_rows_html = ""
    for exp in sorted(data["expiries"], key=lambda x: x["dte"]):
        expiry_rows_html += (
            f'<tr><td>{exp["label"]}</td>'
            f'<td>{exp["dte"]}d</td>'
            f'<td><strong>{exp["atm_iv_pct"]:.2f}%</strong></td>'
            f'<td>{exp["total_oi"]:,}</td></tr>\n'
        )
    if not expiry_rows_html:
        expiry_rows_html = '<tr><td colspan="4" style="color:var(--t3)">Sem dados de vencimento</td></tr>\n'

    # Warnings block
    warnings_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warnings_html = f'<div class="warning-box"><strong>Avisos:</strong><ul>{items}</ul></div>\n'

    # Band table rows
    band_display = [
        ("Superior 4σ",          "upper_4s", "#f85149"),
        ("Superior 2σ",          "upper_2s", "#d29922"),
        ("Linha Central (SMA30)", "central",  "#8b949e"),
        ("Inferior 2σ",          "lower_2s", "#3fb950"),
        ("Inferior 4σ",          "lower_4s", "#58a6ff"),
    ]
    band_rows_html = ""
    for lbl, key, color in band_display:
        val  = bands.get(key, 0.0)
        dist = dists.get(key, 0.0)
        dist_str = f"+{dist:.2f}%" if dist >= 0 else f"{dist:.2f}%"
        band_rows_html += (
            f'<tr>'
            f'<td><span class="band-dot" style="background:{color}"></span>{lbl}</td>'
            f'<td style="color:{color};font-weight:700">R$ {val:.2f}</td>'
            f'<td>{dist_str}</td>'
            f'</tr>\n'
        )

    # Calculator horizon options
    calc_options = ""
    for lbl_h, days_h in [("1 dia", 1), ("1 semana", 5), ("2 semanas", 10), ("1 mês", 21), ("3 meses", 63)]:
        calc_options += f'<option value="{days_h}">{lbl_h} ({days_h}d)</option>\n'

    # IV tendency sign
    iv_tend_str = f"+{iv_tend:.1f}%" if iv_tend >= 0 else f"{iv_tend:.1f}%"
    garch_label = "GARCH(1,1)" if data.get("garch_available") else "EWMA λ=0.94"

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BOVA11 Bandas de Volatilidade — {ref_date}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

    :root {{
      --bg:#FAFAF8; --bg2:#F2F1EE; --bg3:#E8E7E3; --card:#FFFFFF;
      --border:rgba(0,0,0,0.08); --border2:rgba(0,0,0,0.14);
      --t1:#1A1A18; --t2:#6B6960; --t3:#9C9A91;
      --green:#148A63; --amber:#B8720A; --red:#B33530; --blue:#2E6BBF;
      --font:'Instrument Sans',system-ui,sans-serif;
      --mono:'JetBrains Mono',monospace;
    }}
    [data-theme="dark"] {{
      --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --card:#21262d;
      --border:rgba(255,255,255,0.1); --border2:rgba(255,255,255,0.16);
      --t1:#c9d1d9; --t2:#8b949e; --t3:#636c76;
      --green:#3fb950; --amber:#d29922; --red:#f85149; --blue:#58a6ff;
    }}

    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ font-family:var(--font); background:var(--bg); color:var(--t1); min-height:100vh; }}

    #theme-toggle {{
      position:fixed; top:16px; right:16px; z-index:999;
      background:var(--card); border:1px solid var(--border2);
      border-radius:8px; padding:6px 10px; cursor:pointer; font-size:16px; line-height:1;
    }}

    .page-wrap {{ max-width:1200px; margin:0 auto; padding:32px 24px 64px; }}

    /* Header */
    .page-header {{ margin-bottom:28px; }}
    .page-header h1 {{ font-size:1.75rem; font-weight:700; color:var(--t1); margin-bottom:6px; }}
    .page-header p  {{ color:var(--t2); font-size:0.95rem; }}
    .badge-tag {{
      display:inline-block; background:var(--bg3); color:var(--t2);
      font-size:0.75rem; padding:3px 8px; border-radius:4px;
      font-family:var(--mono); margin-top:8px;
    }}

    /* Warnings */
    .warning-box {{
      background:rgba(184,114,10,0.12); border:1px solid var(--amber);
      border-radius:8px; padding:12px 16px; margin-bottom:20px;
      color:var(--amber); font-size:0.85rem;
    }}
    .warning-box ul {{ margin-left:16px; margin-top:4px; }}

    /* Stat Cards */
    .stats-grid {{
      display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
      gap:16px; margin-bottom:24px;
    }}
    .stat-card {{
      background:var(--card); border:1px solid var(--border);
      border-radius:12px; padding:16px; display:flex; flex-direction:column; gap:6px;
    }}
    .stat-card .sc-label {{ font-size:0.72rem; color:var(--t2); font-weight:600;
                            text-transform:uppercase; letter-spacing:.05em; }}
    .stat-card .sc-value {{ font-size:1.45rem; font-weight:700; color:var(--t1); font-family:var(--mono); }}
    .stat-card .sc-sub   {{ font-size:0.72rem; color:var(--t3); }}

    /* Signal Box */
    .signal-box {{
      background:var(--card); border:1px solid var(--border);
      border-radius:12px; padding:24px; margin-bottom:24px;
    }}
    .signal-badge {{
      display:inline-block; font-size:1.05rem; font-weight:700;
      padding:8px 18px; border-radius:8px; margin-bottom:18px; letter-spacing:.03em;
    }}
    .signal-grid {{
      display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px;
    }}
    .signal-item {{ display:flex; flex-direction:column; gap:4px; }}
    .signal-item .si-label {{ font-size:0.72rem; color:var(--t2); text-transform:uppercase; letter-spacing:.05em; }}
    .signal-item .si-value {{ font-size:1rem; font-weight:600; color:var(--t1); }}
    .signal-item .si-sub   {{ font-size:0.78rem; color:var(--t3); }}
    .oport-bar-bg   {{ background:var(--bg3); border-radius:4px; height:8px; width:100%; margin-top:6px; }}
    .oport-bar-fill {{ height:8px; border-radius:4px; transition:width .5s ease; }}

    /* Two-col layout */
    .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:24px; }}
    @media (max-width:768px) {{ .two-col {{ grid-template-columns:1fr; }} }}

    /* Section cards */
    .section-card {{
      background:var(--card); border:1px solid var(--border);
      border-radius:12px; padding:20px; margin-bottom:24px;
    }}
    .section-card h2 {{
      font-size:1rem; font-weight:600; color:var(--t1); margin-bottom:16px;
      padding-bottom:10px; border-bottom:1px solid var(--border);
    }}

    /* Tables */
    table {{ width:100%; border-collapse:collapse; font-size:0.875rem; }}
    th {{
      text-align:left; font-weight:600; color:var(--t2); font-size:0.72rem;
      text-transform:uppercase; letter-spacing:.05em; padding:8px 12px;
      border-bottom:1px solid var(--border);
    }}
    td {{ padding:10px 12px; border-bottom:1px solid var(--border); color:var(--t1); }}
    tr:last-child td {{ border-bottom:none; }}
    tr:hover td {{ background:var(--bg2); }}
    .band-dot {{
      display:inline-block; width:10px; height:10px; border-radius:50%;
      margin-right:8px; vertical-align:middle;
    }}

    /* Charts */
    .chart-wrap    {{ position:relative; height:320px; }}
    .chart-wrap-sm {{ position:relative; height:240px; }}
    .chart-wrap-cone {{ position:relative; height:200px; }}

    /* Calculator */
    .calc-row {{
      display:flex; gap:12px; align-items:flex-end;
      margin-bottom:20px; flex-wrap:wrap;
    }}
    .calc-row label {{
      font-size:0.78rem; color:var(--t2); font-weight:600;
      text-transform:uppercase; letter-spacing:.04em;
      display:block; margin-bottom:5px;
    }}
    .calc-row select,
    .calc-row input {{
      background:var(--bg2); border:1px solid var(--border2);
      color:var(--t1); border-radius:6px; padding:8px 10px;
      font-size:0.875rem; font-family:var(--font);
    }}
    .calc-results {{
      display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
      gap:12px; margin-bottom:16px;
    }}
    .calc-card {{
      background:var(--bg2); border-radius:8px; padding:14px;
      border:1px solid var(--border); text-align:center;
    }}
    .calc-card .cc-label {{
      font-size:0.68rem; color:var(--t2); text-transform:uppercase;
      letter-spacing:.04em; margin-bottom:6px;
    }}
    .calc-card .cc-value {{ font-size:1.1rem; font-weight:700; font-family:var(--mono); }}
    .calc-card .cc-pct   {{ font-size:0.75rem; color:var(--t3); margin-top:3px; }}
  </style>
</head>
<body>

<button id="theme-toggle" onclick="toggleTheme()">◐</button>

<div class="page-wrap">

  <div class="page-header">
    <h1>BOVA11 Bandas de Volatilidade</h1>
    <p>GARCH + Volatilidade Implícita — Análise de Regime e Movimento Esperado</p>
    <span class="badge-tag">ref: {ref_tag} | {ref_date} | spot: R$ {spot:.2f}</span>
  </div>

  {warnings_html}

  <!-- ── Stat Cards ── -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="sc-label">Spot Atual</div>
      <div class="sc-value">R$ {spot:.2f}</div>
      <div class="sc-sub">BOVA11 — preço de referência</div>
    </div>
    <div class="stat-card">
      <div class="sc-label">IV Atual (ATM)</div>
      <div class="sc-value">{atm_iv * 100:.2f}%</div>
      <div class="sc-sub">Média ponderada por DTE</div>
    </div>
    <div class="stat-card">
      <div class="sc-label">IV Rank</div>
      <div class="sc-value">{iv_rank:.1f}</div>
      <div class="sc-sub">Posição 0–100 vs 1 ano</div>
    </div>
    <div class="stat-card">
      <div class="sc-label">IV Percentile</div>
      <div class="sc-value">{iv_pctile:.1f}%</div>
      <div class="sc-sub">% dias abaixo da IV atual</div>
    </div>
    <div class="stat-card">
      <div class="sc-label">IV Tendência</div>
      <div class="sc-value">{iv_tend_str}</div>
      <div class="sc-sub">Prêmio IV vs HV 10d</div>
    </div>
    <div class="stat-card">
      <div class="sc-label">GARCH Vol</div>
      <div class="sc-value">{garch_vol * 100:.2f}%</div>
      <div class="sc-sub">{garch_label} anualizado</div>
    </div>
  </div>

  <!-- ── Signal Box ── -->
  <div class="signal-box">
    <div class="signal-badge"
         style="background:{sig_color}22; color:{sig_color}; border:1px solid {sig_color}44;">
      {signal}
    </div>
    <div class="signal-grid">
      <div class="signal-item">
        <div class="si-label">Nível de Risco</div>
        <div class="si-value" style="color:{nivel_risco_color}">{nivel_risco}</div>
        <div class="si-sub">IV Rank: {iv_rank:.1f} / 100</div>
      </div>
      <div class="signal-item">
        <div class="si-label">Oportunidade</div>
        <div class="si-value">{oport} / 100</div>
        <div class="oport-bar-bg">
          <div class="oport-bar-fill" style="width:{oport}%; background:{sig_color}"></div>
        </div>
      </div>
      <div class="signal-item">
        <div class="si-label">Probabilidade de Reversão</div>
        <div class="si-value">{prob_rev}%</div>
        <div class="si-sub">Estimativa histórica</div>
      </div>
      <div class="signal-item" style="grid-column:1/-1">
        <div class="si-label">Sugestão de Ação</div>
        <div class="si-value" style="font-size:0.875rem; font-weight:500; color:var(--t2)">{sugestao}</div>
      </div>
    </div>
  </div>

  <!-- ── Bandas + Vencimentos ── -->
  <div class="two-col">
    <div class="section-card" style="margin-bottom:0">
      <h2>Bandas de Volatilidade (SMA30 + IV)</h2>
      <table>
        <thead><tr><th>Banda</th><th>Valor</th><th>Dist. Spot</th></tr></thead>
        <tbody>{band_rows_html}</tbody>
      </table>
    </div>
    <div class="section-card" style="margin-bottom:0">
      <h2>Vencimentos Analisados</h2>
      <table>
        <thead><tr><th>Vencimento</th><th>DTE</th><th>ATM IV</th><th>OI Total</th></tr></thead>
        <tbody>{expiry_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- ── Gráfico histórico + bandas ── -->
  <div class="section-card">
    <h2>Histórico de Preços (60 dias) + Bandas de Volatilidade</h2>
    <div class="chart-wrap">
      <canvas id="chartHistorico"></canvas>
    </div>
  </div>

  <!-- ── Calculadora de Movimento Esperado ── -->
  <div class="section-card">
    <h2>Calculadora de Movimento Esperado</h2>
    <div class="calc-row">
      <div>
        <label for="calc-horizon">Horizonte de tempo</label>
        <select id="calc-horizon" onchange="updateCalc()">
          {calc_options}
        </select>
      </div>
      <div>
        <label for="calc-spot">Spot (editável)</label>
        <input type="number" id="calc-spot" value="{spot:.2f}"
               step="0.50" style="width:120px" oninput="updateCalc()">
      </div>
      <div>
        <label for="calc-iv">IV Anual (%)</label>
        <input type="number" id="calc-iv" value="{atm_iv * 100:.2f}"
               step="0.10" style="width:100px" oninput="updateCalc()">
      </div>
    </div>
    <div class="calc-results" id="calc-results"></div>
    <div class="chart-wrap-cone">
      <canvas id="chartCone"></canvas>
    </div>
  </div>

  <!-- ── IV Smile ── -->
  <div class="section-card">
    <h2>IV Smile por Strike</h2>
    <div class="chart-wrap-sm">
      <canvas id="chartSmile"></canvas>
    </div>
  </div>

</div><!-- /page-wrap -->

<script>
// ── Theme ─────────────────────────────────────────────────────
(function(){{ var t=localStorage.getItem('bova11-theme')||'light'; if(t==='dark'){{document.documentElement.setAttribute('data-theme','dark');document.getElementById('theme-toggle').textContent='◐';}} }})();
function toggleTheme(){{ var btn=document.getElementById('theme-toggle'); if(document.documentElement.getAttribute('data-theme')==='dark'){{document.documentElement.removeAttribute('data-theme');localStorage.setItem('bova11-theme','light');btn.textContent='◐';}}else{{document.documentElement.setAttribute('data-theme','dark');localStorage.setItem('bova11-theme','dark');btn.textContent='◐';}} }}

// ── Embedded data ─────────────────────────────────────────────
const SPOT         = {spot};
const ATM_IV       = {atm_iv};
const BAND_U4      = {band_upper4};
const BAND_U2      = {band_upper2};
const BAND_C       = {band_central};
const BAND_L2      = {band_lower2};
const BAND_L4      = {band_lower4};
const HIST_DATES   = {hist_dates};
const HIST_PRICES  = {hist_prices};
const SMILE_STRIKES   = {smile_strikes};
const SMILE_CALL_IVS  = {smile_call_ivs};
const SMILE_PUT_IVS   = {smile_put_ivs};

// ── Helpers ───────────────────────────────────────────────────
function isDark()     {{ return document.documentElement.getAttribute('data-theme') === 'dark'; }}
function gridColor()  {{ return isDark() ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'; }}
function textColor()  {{ return isDark() ? '#8b949e' : '#6B6960'; }}
function cardBg()     {{ return isDark() ? '#21262d' : '#ffffff'; }}

// ── Chart: Histórico + Bandas ─────────────────────────────────
const ctxH = document.getElementById('chartHistorico').getContext('2d');

function makeBand(label, value, color, dash) {{
  return {{
    label: label,
    data: HIST_DATES.map(() => value),
    borderColor: color, borderWidth: 1.5,
    borderDash: dash || [5,3],
    pointRadius: 0, tension: 0, fill: false, order: 2,
  }};
}}

const chartHistorico = new Chart(ctxH, {{
  type: 'line',
  data: {{
    labels: HIST_DATES,
    datasets: [
      {{
        label: 'BOVA11',
        data: HIST_PRICES,
        borderColor: isDark() ? '#c9d1d9' : '#1A1A18',
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
        tension: 0.3, fill: false, order: 1,
      }},
      makeBand('Superior 4σ', BAND_U4, '#f85149', [4,4]),
      makeBand('Superior 2σ', BAND_U2, '#d29922', [4,4]),
      makeBand('Central (SMA30)', BAND_C, '#8b949e', [2,2]),
      makeBand('Inferior 2σ', BAND_L2, '#3fb950', [4,4]),
      makeBand('Inferior 4σ', BAND_L4, '#58a6ff', [4,4]),
      {{
        label: 'Spot Atual',
        data: HIST_DATES.map((d, i) => i === HIST_DATES.length - 1 ? SPOT : null),
        borderColor: '#f0c040', backgroundColor: '#f0c040',
        pointRadius: HIST_DATES.map((d, i) => i === HIST_DATES.length - 1 ? 7 : 0),
        pointHoverRadius: 9, showLine: false, order: 0,
      }},
    ],
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{
        position: 'top',
        labels: {{ color: textColor(), font: {{ size: 11 }}, boxWidth: 14, padding: 14 }},
      }},
      tooltip: {{
        backgroundColor: cardBg(),
        borderColor: isDark() ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.08)',
        borderWidth: 1, titleColor: textColor(),
        bodyColor: isDark() ? '#c9d1d9' : '#1A1A18',
        callbacks: {{
          label: ctx => ctx.dataset.label + ': R$ ' + (ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) : '-'),
        }},
      }},
    }},
    scales: {{
      x: {{
        ticks: {{ color: textColor(), maxTicksLimit: 8, font: {{ size: 10 }} }},
        grid: {{ color: gridColor() }},
      }},
      y: {{
        ticks: {{ color: textColor(), callback: v => 'R$ ' + v.toFixed(0) }},
        grid: {{ color: gridColor() }},
      }},
    }},
  }},
}});

// ── Calculadora ───────────────────────────────────────────────
let coneChart = null;

function expectedMove(s, iv, days) {{
  const em1 = s * iv * Math.sqrt(days / 252);
  const em2 = em1 * 2;
  return {{
    upper2s: s + em2, upper1s: s + em1,
    lower1s: s - em1, lower2s: s - em2,
    pct1: em1 / s * 100, pct2: em2 / s * 100,
  }};
}}

function updateCalc() {{
  const days    = parseInt(document.getElementById('calc-horizon').value);
  const spotVal = parseFloat(document.getElementById('calc-spot').value);
  const ivPct   = parseFloat(document.getElementById('calc-iv').value);
  if (isNaN(spotVal) || isNaN(ivPct) || spotVal <= 0 || ivPct <= 0) return;

  const iv = ivPct / 100;
  const em = expectedMove(spotVal, iv, days);

  document.getElementById('calc-results').innerHTML = `
    <div class="calc-card">
      <div class="cc-label">Teto +2σ</div>
      <div class="cc-value" style="color:#f85149">R$ ${{em.upper2s.toFixed(2)}}</div>
      <div class="cc-pct">+${{em.pct2.toFixed(1)}}%</div>
    </div>
    <div class="calc-card">
      <div class="cc-label">Teto +1σ</div>
      <div class="cc-value" style="color:#d29922">R$ ${{em.upper1s.toFixed(2)}}</div>
      <div class="cc-pct">+${{em.pct1.toFixed(1)}}%</div>
    </div>
    <div class="calc-card">
      <div class="cc-label">Spot Central</div>
      <div class="cc-value">R$ ${{spotVal.toFixed(2)}}</div>
      <div class="cc-pct">referência</div>
    </div>
    <div class="calc-card">
      <div class="cc-label">Piso -1σ</div>
      <div class="cc-value" style="color:#3fb950">R$ ${{em.lower1s.toFixed(2)}}</div>
      <div class="cc-pct">-${{em.pct1.toFixed(1)}}%</div>
    </div>
    <div class="calc-card">
      <div class="cc-label">Piso -2σ</div>
      <div class="cc-value" style="color:#58a6ff">R$ ${{em.lower2s.toFixed(2)}}</div>
      <div class="cc-pct">-${{em.pct2.toFixed(1)}}%</div>
    </div>
  `;

  if (coneChart) {{ coneChart.destroy(); coneChart = null; }}
  const ctxC = document.getElementById('chartCone').getContext('2d');
  coneChart = new Chart(ctxC, {{
    type: 'line',
    data: {{
      labels: ['Hoje', `+${{days}}d`],
      datasets: [
        {{ label: '+2σ Teto', data: [spotVal, em.upper2s], borderColor: '#f85149', borderWidth: 2, borderDash: [4,3], pointRadius: [0, 5], fill: false, tension: 0 }},
        {{ label: '+1σ Teto', data: [spotVal, em.upper1s], borderColor: '#d29922', borderWidth: 2, borderDash: [4,3], pointRadius: [0, 5], fill: false, tension: 0 }},
        {{ label: 'Central',  data: [spotVal, spotVal],    borderColor: '#8b949e', borderWidth: 1.5, borderDash: [2,2], pointRadius: [4, 4], fill: false, tension: 0 }},
        {{ label: '-1σ Piso', data: [spotVal, em.lower1s], borderColor: '#3fb950', borderWidth: 2, borderDash: [4,3], pointRadius: [0, 5], fill: false, tension: 0 }},
        {{ label: '-2σ Piso', data: [spotVal, em.lower2s], borderColor: '#58a6ff', borderWidth: 2, borderDash: [4,3], pointRadius: [0, 5], fill: false, tension: 0 }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ labels: {{ color: textColor(), font: {{ size: 10 }}, boxWidth: 12 }} }},
        tooltip: {{
          backgroundColor: cardBg(), titleColor: textColor(),
          bodyColor: isDark() ? '#c9d1d9' : '#1A1A18',
          callbacks: {{ label: ctx => ctx.dataset.label + ': R$ ' + ctx.parsed.y.toFixed(2) }},
        }},
      }},
      scales: {{
        x: {{ ticks: {{ color: textColor() }}, grid: {{ color: gridColor() }} }},
        y: {{
          ticks: {{ color: textColor(), callback: v => 'R$ ' + v.toFixed(0) }},
          grid: {{ color: gridColor() }},
        }},
      }},
    }},
  }});
}}

updateCalc();

// ── IV Smile ──────────────────────────────────────────────────
const ctxS = document.getElementById('chartSmile').getContext('2d');
new Chart(ctxS, {{
  type: 'line',
  data: {{
    labels: SMILE_STRIKES,
    datasets: [
      {{
        label: 'Call IV',
        data: SMILE_CALL_IVS,
        borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.08)',
        borderWidth: 2, pointRadius: 3, tension: 0.3, fill: true,
      }},
      {{
        label: 'Put IV',
        data: SMILE_PUT_IVS,
        borderColor: '#f85149', backgroundColor: 'rgba(248,81,73,0.08)',
        borderWidth: 2, pointRadius: 3, tension: 0.3, fill: true,
      }},
    ],
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ labels: {{ color: textColor(), font: {{ size: 11 }}, boxWidth: 14 }} }},
      tooltip: {{
        backgroundColor: cardBg(), titleColor: textColor(),
        bodyColor: isDark() ? '#c9d1d9' : '#1A1A18',
        callbacks: {{
          title: items => 'Strike R$ ' + items[0].label,
          label: ctx  => ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + '%',
        }},
      }},
    }},
    scales: {{
      x: {{
        title: {{ display: true, text: 'Strike (R$)', color: textColor(), font: {{ size: 11 }} }},
        ticks: {{ color: textColor(), maxTicksLimit: 14, font: {{ size: 10 }} }},
        grid: {{ color: gridColor() }},
      }},
      y: {{
        title: {{ display: true, text: 'IV (%)', color: textColor(), font: {{ size: 11 }} }},
        ticks: {{ color: textColor(), callback: v => v.toFixed(1) + '%' }},
        grid: {{ color: gridColor() }},
      }},
    }},
  }},
}});
</script>
</body>
</html>"""

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# 8. ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="BOVA11 Bandas de Volatilidade — Módulo 16"
    )
    parser.add_argument("--data-dir",  required=True,  help="Diretório com CSVs B3")
    parser.add_argument("--output",    required=True,  help="Caminho do HTML de saída")
    parser.add_argument("--ref-date",  required=True,  help="Data ISO (ex: 2026-03-25)")
    parser.add_argument("--ref-tag",   required=True,  help="Tag do CSV (ex: 25posmar)")
    parser.add_argument("--spot",      required=True,  type=float, help="Spot atual")
    args = parser.parse_args()

    try:
        ref_date_obj = date.fromisoformat(args.ref_date)
    except ValueError:
        print(
            f"[ERROR] --ref-date inválido: '{args.ref_date}'. Use formato YYYY-MM-DD.",
            file=sys.stderr,
        )
        return 1

    if args.spot <= 0:
        print(f"[ERROR] --spot deve ser positivo (recebido: {args.spot})", file=sys.stderr)
        return 1

    print(
        f"[16/16] Bandas de Volatilidade  ({args.ref_tag}  spot={args.spot:.2f})",
        flush=True,
    )

    try:
        data = run_analysis(
            data_dir=args.data_dir,
            ref_tag=args.ref_tag,
            spot=args.spot,
            ref_date=ref_date_obj,
        )
    except Exception as e:
        print(f"[ERROR] Falha na análise: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    for w in data["warnings"]:
        print(f"  [WARN] {w}", flush=True)

    try:
        generate_html(data, args.output)
    except Exception as e:
        print(f"[ERROR] Falha ao gerar HTML: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    print(f"  -> Salvo em: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
