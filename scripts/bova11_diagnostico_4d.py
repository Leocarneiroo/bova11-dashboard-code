#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 — Diagnóstico 4D (GEX + DEX + VEX + TEX)
=================================================
Combina os 4 sinais de exposição de gregos em uma matriz de decisão
16 cenários com recomendações de estratégia.

Uso:
  python3 bova11_diagnostico_4d.py \\
      --data-dir /path/to/data \\
      --output /path/to/output/bova11_diagnostico_4d.html \\
      --ref-date 2026-03-25 \\
      --ref-tag 25mar \\
      --spot-d 188.50 \\
      --spot-d1 187.20

Dependências: Python 3 stdlib apenas
"""

import os
import re
import glob
import sys
import json
import math
import argparse
from datetime import datetime, date
from html import escape

from bova11_shared import (
    calc_dex_components,
    calc_gex_components,
    calc_tex_components,
    calc_vex_components,
)

# ═══════════════════════════════════════════════════════════════
# 1. PARSER B3
# ═══════════════════════════════════════════════════════════════

def _p(raw) -> float:
    """Parser robusto para números no formato brasileiro (B3 CSV)."""
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


def load_b3(path: str) -> list:
    """Lê CSV B3 (fechamento com Greeks) e retorna lista de dicts por strike."""
    with open(path, "r", encoding="latin-1") as f:
        lines = f.readlines()
    rows = []
    for line in lines[1:]:
        c = line.strip().replace("\r", "").split(";")
        if len(c) < 23:
            continue
        strike = _p(c[11])
        if strike == 0:
            continue
        rows.append({
            "strike":     strike,
            "call_ticker": c[0].strip(),
            "call_last":  _p(c[1]),
            "call_oi":    _p(c[2]),
            "call_delta": _p(c[3]),
            "call_gamma": _p(c[4]),
            "call_theta": _p(c[5]),
            "call_vega":  _p(c[6]),
            "call_iv":    _p(c[7]),
            "call_trades": _p(c[8]),
            "call_bid":   _p(c[9]),
            "call_ask":   _p(c[10]),
            "put_bid":    _p(c[12]),
            "put_ask":    _p(c[13]),
            "put_trades": _p(c[14]),
            "put_iv":     _p(c[15]),
            "put_gamma":  _p(c[18]),
            "put_delta":  _p(c[19]),   # negative by convention
            "put_theta":  _p(c[17]),
            "put_vega":   _p(c[16]),
            "put_oi":     _p(c[20]),
            "put_last":   _p(c[21]),
            "put_ticker": c[22].strip(),
        })
    return rows


def load_volume_csv(path: str) -> dict:
    """Lê CSV de volume B3 e agrega call_vol / put_vol por strike."""
    if not path or not os.path.exists(path):
        return {}

    with open(path, "r", encoding="latin-1") as f:
        lines = f.readlines()

    volume_by_strike = {}
    for line in lines[1:]:
        c = line.strip().replace("\r", "").split(";")
        if len(c) < 10:
            continue
        strike = _p(c[5])
        if strike == 0:
            continue
        if strike not in volume_by_strike:
            volume_by_strike[strike] = {"call_vol": 0.0, "put_vol": 0.0}
        volume_by_strike[strike]["call_vol"] += _p(c[1])
        volume_by_strike[strike]["put_vol"]  += _p(c[9])
    return volume_by_strike


# ═══════════════════════════════════════════════════════════════
# 2. DESCOBERTA DE ARQUIVOS
# ═══════════════════════════════════════════════════════════════

def normalize_tag(tag: str) -> str:
    """Remove sufixos pos/pre de uma tag (ex: '25posmar' -> '25mar')."""
    return re.sub(r"(pos|pre)([a-z]{3})$", r"\2", tag.lower())


def discover_expirations(data_dir: str, ref_tag: str) -> dict:
    """
    Retorna {nome_vencimento: filepath} para a tag de referência dada.
    Tenta tanto formato underscore quanto formato com espaços.
    """
    found = {}
    tag = ref_tag.strip()
    norm = normalize_tag(tag)

    # Padrão underscore: venc_6_mar_W1_fechamento__25mar_.csv
    for fp in sorted(glob.glob(os.path.join(data_dir, f"venc_*_fechamento__{tag}_.csv"))):
        m = re.match(r"venc_(.+?)_fechamento__", os.path.basename(fp))
        if m:
            venc = m.group(1).replace("_", " ")
            found[venc] = fp

    # Se não encontrou, tenta tag normalizada (sem pos/pre)
    if not found and norm != tag:
        for fp in sorted(glob.glob(os.path.join(data_dir, f"venc_*_fechamento__{norm}_.csv"))):
            m = re.match(r"venc_(.+?)_fechamento__", os.path.basename(fp))
            if m:
                venc = m.group(1).replace("_", " ")
                found[venc] = fp

    # Padrão com espaços: venc 6 mar W1 fechamento (25mar).csv
    for fp in sorted(glob.glob(os.path.join(data_dir, f"venc * fechamento ({tag}).csv"))):
        m = re.match(r"venc (.+) fechamento \(", os.path.basename(fp))
        if m and m.group(1) not in found:
            found[m.group(1)] = fp

    if not found and norm != tag:
        for fp in sorted(glob.glob(os.path.join(data_dir, f"venc * fechamento ({norm}).csv"))):
            m = re.match(r"venc (.+) fechamento \(", os.path.basename(fp))
            if m and m.group(1) not in found:
                found[m.group(1)] = fp

    return found


_MONTHS = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4,
    "mai": 5, "jun": 6, "jul": 7, "ago": 8,
    "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def expiry_sort_key(expiry: str) -> tuple:
    """Ordena vencimentos por mês/dia e mantém Mensal após semanais do mesmo dia."""
    m = re.match(r"(\d{1,2})\s+([a-z]{3})", expiry.lower())
    if not m:
        return (99, 99, 99, expiry.lower())
    day = int(m.group(1))
    month = _MONTHS.get(m.group(2), 99)
    monthly_bias = 1 if "mensal" in expiry.lower() else 0
    return (month, day, monthly_bias, expiry.lower())


def discover_expirations_with_volume(data_dir: str, ref_tag: str) -> dict:
    """
    Descobre arquivos de fechamento e volume por vencimento.
    Retorna: {expiry_name: {'close': path_or_none, 'volume': path_or_none}}
    """
    close_files = {}
    volume_files = {}
    norm = normalize_tag(ref_tag)
    tags_to_try = [ref_tag] if ref_tag == norm else [ref_tag, norm]

    for tag in tags_to_try:
        for fp in sorted(glob.glob(os.path.join(data_dir, f"venc_*_fechamento__{tag}_.csv"))):
            m = re.match(r"venc_(.+?)_fechamento__", os.path.basename(fp))
            if m:
                venc = m.group(1).replace("_", " ")
                if venc not in close_files:
                    close_files[venc] = fp

        for fp in sorted(glob.glob(os.path.join(data_dir, f"venc * fechamento ({tag}).csv"))):
            m = re.match(r"venc (.+) fechamento \(", os.path.basename(fp))
            if m and m.group(1) not in close_files:
                close_files[m.group(1)] = fp

        for fp in sorted(glob.glob(os.path.join(data_dir, f"venc_*_fechamento__{tag}_Volume_.csv"))):
            m = re.match(r"venc_(.+?)_fechamento__", os.path.basename(fp))
            if m:
                venc = m.group(1).replace("_", " ")
                if venc not in volume_files:
                    volume_files[venc] = fp

        for fp in sorted(glob.glob(os.path.join(data_dir, f"venc * fechamento ({tag} Volume).csv"))):
            m = re.match(r"venc (.+) fechamento \(", os.path.basename(fp))
            if m and m.group(1) not in volume_files:
                volume_files[m.group(1)] = fp

    expiries = set(close_files) | set(volume_files)
    return {
        expiry: {"close": close_files.get(expiry), "volume": volume_files.get(expiry)}
        for expiry in sorted(expiries, key=expiry_sort_key)
    }


# ═══════════════════════════════════════════════════════════════
# 3. CÁLCULO DOS GREGOS AGREGADOS
# ═══════════════════════════════════════════════════════════════

def calc_greeks(rows: list, spot: float) -> dict:
    """
    Calcula GEX, DEX, VEX, TEX por strike a partir de uma lista de dicts.
    Retorna dict com listas por strike e totais.
    """
    strikes = []
    gex_net_list = []
    dex_net_list = []
    vex_net_list = []
    tex_net_list = []
    gex_call_list = []
    gex_put_list = []

    for r in rows:
        s = r["strike"]
        c_oi = r["call_oi"]
        p_oi = r["put_oi"]

        gex_call, gex_put, gex_net = calc_gex_components(
            call_gamma=r["call_gamma"],
            put_gamma=r["put_gamma"],
            call_oi=c_oi,
            put_oi=p_oi,
            spot=spot,
        )
        dex_call, dex_put, dex_net = calc_dex_components(
            call_delta=r["call_delta"],
            put_delta=r["put_delta"],
            call_oi=c_oi,
            put_oi=p_oi,
            spot=spot,
        )
        vex_call, vex_put, vex_net = calc_vex_components(
            call_vega=r["call_vega"],
            put_vega=r["put_vega"],
            call_oi=c_oi,
            put_oi=p_oi,
        )
        tex_call, tex_put, tex_net = calc_tex_components(
            call_theta=r["call_theta"],
            put_theta=r["put_theta"],
            call_oi=c_oi,
            put_oi=p_oi,
        )

        strikes.append(s)
        gex_net_list.append(gex_net)
        dex_net_list.append(dex_net)
        vex_net_list.append(vex_net)
        tex_net_list.append(tex_net)
        gex_call_list.append(gex_call)
        gex_put_list.append(gex_put)

    return {
        "strikes":  strikes,
        "gex_net":  gex_net_list,
        "dex_net":  dex_net_list,
        "vex_net":  vex_net_list,
        "tex_net":  tex_net_list,
        "gex_call": gex_call_list,
        "gex_put":  gex_put_list,
    }


def find_gamma_flip(strikes: list, gex_net: list) -> float:
    """Encontra o strike onde o GEX cumulativo muda de sinal."""
    if not strikes:
        return 0.0
    paired = sorted(zip(strikes, gex_net), key=lambda x: x[0])
    cumulative = 0.0
    prev_sign = None
    for s, g in paired:
        cumulative += g
        sign = 1 if cumulative >= 0 else -1
        if prev_sign is not None and sign != prev_sign:
            return s
        prev_sign = sign
    return paired[-1][0] if paired else 0.0


def fmt_m(value: float) -> str:
    """Formata um valor em M ou B com 2 casas decimais."""
    if abs(value) >= 1000:
        return f"{value / 1000:.2f}B"
    return f"{value:.2f}M"


# ═══════════════════════════════════════════════════════════════
# 4. MATRIZ DE CENÁRIOS
# ═══════════════════════════════════════════════════════════════

SCENARIOS = {
    ("SHORT", "POS", "ALTO", "ALTO"):   (1,  "Gamma Squeeze Iminente",  "Compra de Call, Broken Wing Butterfly",   "Venda descoberta, Posições vendidas"),
    ("SHORT", "POS", "ALTO", "BAIXO"):  (2,  "Rally Explosivo",          "Compra de Call, Compra de Straddle",      "Venda de volatilidade"),
    ("SHORT", "POS", "BAIXO", "ALTO"):  (3,  "Squeeze Técnico",          "Compra de Call",                          "Compra de volatilidade"),
    ("SHORT", "POS", "BAIXO", "BAIXO"): (4,  "Alta Técnica",             "Compra de Call",                          "Posições de volatilidade"),
    ("SHORT", "NEG", "ALTO", "ALTO"):   (5,  "Crash Iminente",           "Compra de Put, Broken Wing Butterfly",    "Posições compradas, Venda de Put"),
    ("SHORT", "NEG", "ALTO", "BAIXO"):  (6,  "Queda Explosiva",          "Compra de Put, Compra de Straddle",       "Calls, Posições de alta"),
    ("SHORT", "NEG", "BAIXO", "ALTO"):  (7,  "Correção Rápida",          "Compra de Put",                           "Posições de alta"),
    ("SHORT", "NEG", "BAIXO", "BAIXO"): (8,  "Correção Lenta",           "Compra de Put",                           "Posições agressivas"),
    ("LONG",  "POS", "ALTO", "ALTO"):   (9,  "Alta com IV Elevada",      "Box de 3 Pontas, Broken Wing Butterfly",  "Compra de volatilidade, Long Straddles"),
    ("LONG",  "POS", "ALTO", "BAIXO"):  (10, "Alta Sustentada",          "Box de 3 Pontas, Venda de Put",           "Compra de volatilidade"),
    ("LONG",  "POS", "BAIXO", "ALTO"):  (11, "Alta Acelerada",           "Venda de Put, Collar de Alta",            "Compra de opções caras"),
    ("LONG",  "POS", "BAIXO", "BAIXO"): (12, "Tendência de Alta",        "Venda de Put, Venda Coberta",             "Posições de volatilidade"),
    ("LONG",  "NEG", "ALTO", "ALTO"):   (13, "Correção com IV Elevada",  "Broken Wing Butterfly, Collar de Baixa",  "Posições de alta, Calls descobertas"),
    ("LONG",  "NEG", "ALTO", "BAIXO"):  (14, "Recuo Controlado",         "Collar de Baixa",                         "Calls"),
    ("LONG",  "NEG", "BAIXO", "ALTO"):  (15, "Queda Acelerada",          "Collar de Baixa",                         "Posições de alta"),
    ("LONG",  "NEG", "BAIXO", "BAIXO"): (16, "Tendência de Baixa",       "Collar de Baixa",                         "Posições agressivas"),
}

INTERPRETACOES = {
    1:  "Mercado em short gamma com pressão compradora e alta volatilidade. Dealers amplificam movimentos de alta. Momento de aceleração iminente.",
    2:  "Condições ideais para rally explosivo. Short gamma com delta positivo e vega elevada. Dealers forçados a comprar conforme preço sobe.",
    3:  "Pressão compradora em mercado de short gamma com baixa volatilidade implícita. Squeeze técnico provável sem grandes movimentos de IV.",
    4:  "Alta técnica sustentada. Short gamma com viés comprador, volatilidade controlada. Tendência de alta sem aceleração brusca.",
    5:  "Mercado em short gamma com forte pressão vendedora e volatilidade elevada. Dealers amplificam queda. Risco de crash sistêmico.",
    6:  "Queda explosiva esperada. Short gamma com delta negativo e vega elevada. Dealers vendem ativos agravando o movimento de baixa.",
    7:  "Pressão vendedora em mercado de short gamma com volatilidade controlada. Correção rápida e técnica sem expansão de IV.",
    8:  "Correção lenta em andamento. Short gamma com viés vendedor e baixa volatilidade. Tendência de baixa gradual.",
    9:  "Dealers long gamma com pressão compradora e IV elevada. Mercado estabilizado mas com custo alto de opções. Foco em estruturas de spread.",
    10: "Alta sustentada com dealers long gamma. Volatilidade elevada sugere movimento direcional com amplitude. Estruturas de venda de put favorecem o posicionamento.",
    11: "Alta acelerada provável. Long gamma com delta positivo e baixa IV. Dealers conseguem hedgear sem amplificar, movimento gradual e consistente.",
    12: "Tendência de alta clara com IV baixa. Dealers long gamma no controle. Ambiente favorável para estratégias de renda (venda de put, venda coberta).",
    13: "Correção com volatilidade elevada em mercado long gamma. Dealers absorvem parte do movimento. Estruturas protetoras com spreads são preferíveis.",
    14: "Recuo controlado. Long gamma com delta negativo e IV baixa. Dealers hedgeiam suavemente. Collar de baixa protege sem custo excessivo.",
    15: "Queda acelerada em mercado long gamma com pressão vendedora. Movimento consistente mas sem amplificação extrema por parte dos dealers.",
    16: "Tendência de baixa clara com IV baixa. Long gamma estabiliza o mercado. Collar de baixa é a estrutura mais adequada para proteger posições.",
}


def determine_signals(total_gex: float, total_dex: float, total_vex: float, total_tex: float):
    """Determina os 4 sinais binários a partir dos totais em M."""
    sig_gex = "LONG"  if total_gex > 0 else "SHORT"
    sig_dex = "POS"   if total_dex > 0 else "NEG"
    sig_vex = "ALTO"  if abs(total_vex) > 500 else "BAIXO"
    sig_tex = "ALTO"  if abs(total_tex) > 200 else "BAIXO"
    return sig_gex, sig_dex, sig_vex, sig_tex


def determine_force(sig_gex, sig_dex, sig_vex, sig_tex) -> str:
    """Calcula força do sinal baseado na convergência dos 4 gregos."""
    bullish = sum([
        sig_gex == "SHORT",
        sig_dex == "POS",
        sig_vex == "ALTO",
        sig_tex == "ALTO",
    ])
    bearish = sum([
        sig_gex == "SHORT",
        sig_dex == "NEG",
        sig_vex == "ALTO",
        sig_tex == "ALTO",
    ])
    score = max(bullish, bearish)
    if score >= 3:
        return "FORTE"
    elif score >= 2:
        return "MODERADO"
    else:
        return "LEVE"


def fmt_qty(value: float) -> str:
    """Formata OI / volume em unidades compactas."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def _mid_price(bid: float, ask: float, last: float = 0.0) -> float:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return last if last > 0 else 0.0


def _trade_price(row: dict, option_type: str, action: str) -> float:
    bid = row.get(f"{option_type}_bid", 0.0)
    ask = row.get(f"{option_type}_ask", 0.0)
    last = row.get(f"{option_type}_last", 0.0)
    if action == "buy" and ask > 0:
        return ask
    if action == "sell" and bid > 0:
        return bid
    return _mid_price(bid, ask, last)


def _spread_ratio(row: dict, option_type: str) -> float:
    bid = row.get(f"{option_type}_bid", 0.0)
    ask = row.get(f"{option_type}_ask", 0.0)
    last = row.get(f"{option_type}_last", 0.0)
    mid = _mid_price(bid, ask, last)
    if mid <= 0:
        return 1.0
    return max(ask - bid, 0.0) / mid


def _option_volume(expiry: dict, strike: float, option_type: str) -> float:
    return expiry.get("volume", {}).get(strike, {}).get(f"{option_type}_vol", 0.0)


def _option_liquidity(expiry: dict, strike: float, option_type: str) -> float:
    row = expiry["rows"].get(strike)
    if not row:
        return -1e9

    price = _trade_price(row, option_type, "buy")
    if price <= 0:
        return -1e9

    oi = row.get(f"{option_type}_oi", 0.0)
    vol = _option_volume(expiry, strike, option_type)
    trades = row.get(f"{option_type}_trades", 0.0)
    spread_penalty = 6.0 * _spread_ratio(row, option_type)

    score = math.log1p(max(oi, 0.0))
    score += 0.70 * math.log1p(max(vol, 0.0))
    score += 0.25 * math.log1p(max(trades, 0.0))
    score -= spread_penalty

    if row.get(f"{option_type}_bid", 0.0) <= 0:
        score -= 0.5
    if row.get(f"{option_type}_ask", 0.0) <= 0:
        score -= 0.5
    return score


def _execution_row_score(row: dict) -> float:
    """Escolhe a linha mais operável quando um strike aparece duplicado."""
    score = row.get("call_oi", 0.0) + row.get("put_oi", 0.0)
    score += 250.0 * (row.get("call_trades", 0.0) + row.get("put_trades", 0.0))
    score += 50_000.0 if _mid_price(row.get("call_bid", 0.0), row.get("call_ask", 0.0), row.get("call_last", 0.0)) > 0 else 0.0
    score += 50_000.0 if _mid_price(row.get("put_bid", 0.0), row.get("put_ask", 0.0), row.get("put_last", 0.0)) > 0 else 0.0
    return score


def _collapse_rows_for_execution(rows: list) -> dict:
    best_rows = {}
    for row in rows:
        strike = row["strike"]
        score = _execution_row_score(row)
        current = best_rows.get(strike)
        if current is None or score > current["_score"]:
            picked = dict(row)
            picked["_score"] = score
            best_rows[strike] = picked
    return {
        strike: {k: v for k, v in row.items() if k != "_score"}
        for strike, row in best_rows.items()
    }


def _infer_strike_step(strikes: list) -> float:
    ordered = sorted(set(strikes))
    diffs = [b - a for a, b in zip(ordered, ordered[1:]) if (b - a) > 0]
    return min(diffs) if diffs else 1.0


def _parse_expiry_date(expiry_name: str, ref_date: str):
    ref_dt = datetime.strptime(ref_date, "%Y-%m-%d").date()
    m = re.match(r"(\d{1,2})\s+([a-z]{3})", expiry_name.lower())
    if not m:
        return None

    day = int(m.group(1))
    month = _MONTHS.get(m.group(2))
    if not month:
        return None

    year = ref_dt.year
    expiry_dt = date(year, month, day)
    if expiry_dt < ref_dt and (ref_dt.month - month) > 6:
        expiry_dt = date(year + 1, month, day)
    return expiry_dt


def _build_expiry_market(expiry_name: str, close_rows: list, volume_by_strike: dict, ref_date: str) -> dict:
    rows = _collapse_rows_for_execution(close_rows)
    strikes = sorted(rows.keys())
    expiry_dt = _parse_expiry_date(expiry_name, ref_date)
    ref_dt = datetime.strptime(ref_date, "%Y-%m-%d").date()
    dte = (expiry_dt - ref_dt).days if expiry_dt else None
    return {
        "expiry": expiry_name,
        "expiry_date": expiry_dt,
        "dte": dte,
        "is_monthly": "mensal" in expiry_name.lower(),
        "rows": rows,
        "volume": volume_by_strike or {},
        "strikes": strikes,
        "step": _infer_strike_step(strikes),
    }


def _expiry_bonus(expiry: dict, strategy_name: str) -> float:
    dte = expiry.get("dte")
    if dte is None:
        return 0.0

    if strategy_name in ("Broken Wing Butterfly", "Box de 3 Pontas"):
        target, width = 16.0, 10.0
    elif strategy_name in ("Compra de Straddle", "Compra de Call", "Compra de Put"):
        target, width = 12.0, 8.0
    elif strategy_name in ("Collar de Alta", "Collar de Baixa", "Venda Coberta", "Venda de Put"):
        target, width = 18.0, 12.0
    else:
        target, width = 14.0, 10.0

    score = 1.8 - abs(dte - target) / width
    if dte <= 1:
        score -= 2.5
    elif dte < 5:
        score -= 0.8
    elif dte > 45:
        score -= 0.6

    if expiry.get("is_monthly"):
        score += 0.25
    return score


def _make_leg(expiry: dict, strike: float, option_type: str, action: str, qty: int = 1) -> dict:
    row = expiry["rows"][strike]
    return {
        "action": action,
        "qty": qty,
        "option_type": "Call" if option_type == "call" else "Put",
        "strike": strike,
        "ticker": row.get(f"{option_type}_ticker", ""),
        "price": _trade_price(row, option_type, action),
        "oi": row.get(f"{option_type}_oi", 0.0),
        "volume": _option_volume(expiry, strike, option_type),
        "delta": row.get(f"{option_type}_delta", 0.0),
    }


def _net_premium(legs: list) -> float:
    total = 0.0
    for leg in legs:
        total += leg["qty"] * leg["price"] * (1.0 if leg["action"] == "buy" else -1.0)
    return total


def _premium_text(value: float) -> str:
    per_structure = abs(value) * 100.0
    if abs(value) < 0.005:
        return "Estrutura praticamente a zero: R$ 0.00/cota (~R$ 0 por 100 cotas)"
    if value < 0:
        return f"Recebe crédito de R$ {abs(value):.2f}/cota (~R$ {per_structure:.0f} por 100 cotas)"
    return f"Paga débito de R$ {value:.2f}/cota (~R$ {per_structure:.0f} por 100 cotas)"


def _strategy_result(title: str, expiry: dict, legs: list, model: str, why: str, risk: str = "") -> dict:
    net = _net_premium(legs)
    return {
        "title": title,
        "expiry": expiry["expiry"],
        "dte": expiry.get("dte"),
        "legs": legs,
        "net_premium": net,
        "premium_text": _premium_text(net),
        "model": model,
        "why": why,
        "risk": risk,
    }


def suggest_buy_call(expiries: list, spot: float):
    best = None
    for expiry in expiries:
        step = expiry["step"]
        for strike in expiry["strikes"]:
            row = expiry["rows"][strike]
            delta = row.get("call_delta", 0.0)
            price = _trade_price(row, "call", "buy")
            if price <= 0 or delta <= 0:
                continue
            if strike < spot - 2 * step or strike > spot + 8 * step:
                continue

            score = _option_liquidity(expiry, strike, "call") + _expiry_bonus(expiry, "Compra de Call")
            score -= abs(delta - 0.45) * 7.0
            score -= abs(strike - spot) * 0.22

            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "result": _strategy_result(
                        "Compra de Call",
                        expiry,
                        [_make_leg(expiry, strike, "call", "buy")],
                        "Call comprada com delta-alvo perto de 0.45.",
                        "Escolhida pela combinação entre delta, liquidez, spread e proximidade do spot.",
                        "Estratégia de débito simples; sofre com theta se o movimento não vier.",
                    ),
                }
    return best["result"] if best else None


def suggest_buy_put(expiries: list, spot: float):
    best = None
    for expiry in expiries:
        step = expiry["step"]
        for strike in expiry["strikes"]:
            row = expiry["rows"][strike]
            delta = abs(row.get("put_delta", 0.0))
            price = _trade_price(row, "put", "buy")
            if price <= 0 or delta <= 0:
                continue
            if strike < spot - 8 * step or strike > spot + 2 * step:
                continue

            score = _option_liquidity(expiry, strike, "put") + _expiry_bonus(expiry, "Compra de Put")
            score -= abs(delta - 0.45) * 7.0
            score -= abs(strike - spot) * 0.22

            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "result": _strategy_result(
                        "Compra de Put",
                        expiry,
                        [_make_leg(expiry, strike, "put", "buy")],
                        "Put comprada com delta-alvo perto de 0.45.",
                        "Escolhida pela combinação entre proteção direcional, liquidez e preço de execução.",
                        "Estratégia de débito simples; sofre com theta se a queda não acelerar.",
                    ),
                }
    return best["result"] if best else None


def suggest_buy_straddle(expiries: list, spot: float):
    best = None
    for expiry in expiries:
        step = expiry["step"]
        for strike in expiry["strikes"]:
            row = expiry["rows"][strike]
            call_price = _trade_price(row, "call", "buy")
            put_price = _trade_price(row, "put", "buy")
            if call_price <= 0 or put_price <= 0:
                continue
            if abs(strike - spot) > 2.0 * step:
                continue

            score = _option_liquidity(expiry, strike, "call")
            score += _option_liquidity(expiry, strike, "put")
            score += _expiry_bonus(expiry, "Compra de Straddle")
            score -= abs(strike - spot) * 0.45

            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "result": _strategy_result(
                        "Compra de Straddle",
                        expiry,
                        [
                            _make_leg(expiry, strike, "call", "buy"),
                            _make_leg(expiry, strike, "put", "buy"),
                        ],
                        "Straddle comprado no strike mais próximo do spot.",
                        "Escolhido pelo melhor equilíbrio entre ATM, liquidez dos dois lados e DTE intermediário.",
                        "Compra direta de volatilidade; theta e IV são fatores críticos.",
                    ),
                }
    return best["result"] if best else None


def suggest_sell_put(expiries: list, spot: float):
    best = None
    for expiry in expiries:
        step = expiry["step"]
        for strike in expiry["strikes"]:
            row = expiry["rows"][strike]
            credit = _trade_price(row, "put", "sell")
            delta = abs(row.get("put_delta", 0.0))
            if credit <= 0 or delta <= 0 or strike >= spot:
                continue
            if strike < spot - 10 * step:
                continue

            score = _option_liquidity(expiry, strike, "put") + _expiry_bonus(expiry, "Venda de Put")
            score -= abs(delta - 0.25) * 8.0
            score -= abs(strike - (spot - 4 * step)) * 0.12
            score += min(credit, 2.0) * 0.18

            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "result": _strategy_result(
                        "Venda de Put",
                        expiry,
                        [_make_leg(expiry, strike, "put", "sell")],
                        "Put vendida OTM com delta-alvo perto de 0.25.",
                        "Escolhida por liquidez, crédito recebido e distância razoável do spot.",
                        "Exige margem e tolerância a ser exercido abaixo do strike.",
                    ),
                }
    return best["result"] if best else None


def suggest_box_3_pontas(expiries: list, spot: float, gamma_flip: float):
    """
    Convenção do modelo:
      +1 Call ATM/levemente OTM
      -1 Call OTM
      -1 Put OTM
    """
    best = None
    for expiry in expiries:
        step = expiry["step"]
        target_put = min(gamma_flip - step, spot - 3 * step) if gamma_flip else (spot - 3 * step)
        for put_strike in expiry["strikes"]:
            if put_strike >= spot or put_strike < spot - 10 * step:
                continue
            put_row = expiry["rows"][put_strike]
            put_delta = abs(put_row.get("put_delta", 0.0))
            put_credit = _trade_price(put_row, "put", "sell")
            if put_credit <= 0 or not (0.15 <= put_delta <= 0.40):
                continue

            for call_long in expiry["strikes"]:
                if call_long <= put_strike or call_long < spot - 2 * step or call_long > spot + 3 * step:
                    continue
                row_long = expiry["rows"][call_long]
                call_long_delta = row_long.get("call_delta", 0.0)
                long_price = _trade_price(row_long, "call", "buy")
                if long_price <= 0 or not (0.35 <= call_long_delta <= 0.65):
                    continue

                for call_short in expiry["strikes"]:
                    if call_short <= call_long or call_short < spot + 2 * step or call_short > spot + 10 * step:
                        continue
                    row_short = expiry["rows"][call_short]
                    call_short_delta = row_short.get("call_delta", 0.0)
                    short_credit = _trade_price(row_short, "call", "sell")
                    if short_credit <= 0 or not (0.15 <= call_short_delta <= 0.40):
                        continue

                    legs = [
                        _make_leg(expiry, call_long, "call", "buy"),
                        _make_leg(expiry, call_short, "call", "sell"),
                        _make_leg(expiry, put_strike, "put", "sell"),
                    ]
                    net = _net_premium(legs)

                    score = _option_liquidity(expiry, put_strike, "put")
                    score += _option_liquidity(expiry, call_long, "call")
                    score += _option_liquidity(expiry, call_short, "call")
                    score += _expiry_bonus(expiry, "Box de 3 Pontas")
                    score -= abs(put_delta - 0.28) * 6.0
                    score -= abs(call_long_delta - 0.50) * 6.0
                    score -= abs(call_short_delta - 0.30) * 6.0
                    score -= abs(call_long - spot) * 0.18
                    score -= abs(put_strike - target_put) * 0.08
                    score += 0.40 if net <= 0 else -0.35 * net

                    if best is None or score > best["score"]:
                        best = {
                            "score": score,
                            "result": _strategy_result(
                                "Box de 3 Pontas",
                                expiry,
                                legs,
                                "Convenção do modelo: +1 Call ATM/OTM, -1 Call OTM e -1 Put OTM para baratear a alta.",
                                "Montada com base em delta-alvo, liquidez e strikes em torno do spot e do gamma flip.",
                                "Inclui put vendida descoberta; exige margem e controle de risco.",
                            ),
                        }
    return best["result"] if best else None


def suggest_broken_wing_butterfly(expiries: list, spot: float, bullish: bool = True):
    best = None
    for expiry in expiries:
        step = expiry["step"]
        for k1 in expiry["strikes"]:
            if bullish:
                if k1 < spot - 3 * step or k1 > spot + 6 * step:
                    continue
                row1 = expiry["rows"][k1]
                d1 = row1.get("call_delta", 0.0)
                if not (0.30 <= d1 <= 0.60):
                    continue
            else:
                if k1 > spot + 3 * step or k1 < spot - 6 * step:
                    continue
                row1 = expiry["rows"][k1]
                d1 = abs(row1.get("put_delta", 0.0))
                if not (0.30 <= d1 <= 0.60):
                    continue

            for k2 in expiry["strikes"]:
                if bullish:
                    if k2 <= k1:
                        continue
                else:
                    if k2 >= k1:
                        continue

                w1 = abs(k2 - k1)
                if w1 < step or w1 > 4 * step:
                    continue

                row2 = expiry["rows"][k2]
                d2 = row2.get("call_delta", 0.0) if bullish else abs(row2.get("put_delta", 0.0))
                if not (0.18 <= d2 <= 0.45):
                    continue

                for k3 in expiry["strikes"]:
                    if bullish:
                        if k3 <= k2:
                            continue
                    else:
                        if k3 >= k2:
                            continue

                    w2 = abs(k3 - k2)
                    if w2 <= w1 or w2 > 8 * step:
                        continue

                    row3 = expiry["rows"][k3]
                    d3 = row3.get("call_delta", 0.0) if bullish else abs(row3.get("put_delta", 0.0))
                    if not (0.08 <= d3 <= 0.30):
                        continue

                    option_type = "call" if bullish else "put"
                    legs = [
                        _make_leg(expiry, k1, option_type, "buy"),
                        _make_leg(expiry, k2, option_type, "sell", qty=2),
                        _make_leg(expiry, k3, option_type, "buy"),
                    ]
                    net = _net_premium(legs)

                    score = _option_liquidity(expiry, k1, option_type)
                    score += 1.2 * _option_liquidity(expiry, k2, option_type)
                    score += _option_liquidity(expiry, k3, option_type)
                    score += _expiry_bonus(expiry, "Broken Wing Butterfly")
                    score -= abs(d1 - 0.45) * 6.0
                    score -= abs(d2 - 0.34) * 7.0
                    score -= abs(d3 - 0.20) * 5.0
                    score += 0.35 if net <= 0.20 else -0.30 * max(net, 0.0)

                    if best is None or score > best["score"]:
                        label = "Broken Wing Butterfly"
                        note = "Borboleta assimétrica montada com 1 compra, 2 vendas no miolo e 1 compra na asa longa."
                        if bullish:
                            risk = "Estrutura de alta com risco definido e custo reduzido pela venda dupla no strike central."
                        else:
                            risk = "Estrutura de baixa com risco definido e custo reduzido pela venda dupla no strike central."
                        best = {
                            "score": score,
                            "result": _strategy_result(
                                label,
                                expiry,
                                legs,
                                note,
                                "Escolhida por delta-alvo, assimetria de asa e liquidez do miolo da borboleta.",
                                risk,
                            ),
                        }
    return best["result"] if best else None


def suggest_collar(expiries: list, spot: float, aggressive: bool):
    best = None
    title = "Collar de Alta" if aggressive else "Collar de Baixa"
    put_target = 0.22 if aggressive else 0.35
    call_target = 0.20 if aggressive else 0.28

    for expiry in expiries:
        step = expiry["step"]
        for put_strike in expiry["strikes"]:
            if put_strike >= spot or put_strike < spot - 10 * step:
                continue
            put_row = expiry["rows"][put_strike]
            put_delta = abs(put_row.get("put_delta", 0.0))
            if not (0.10 <= put_delta <= 0.50):
                continue

            for call_strike in expiry["strikes"]:
                if call_strike <= spot or call_strike > spot + 10 * step:
                    continue
                call_row = expiry["rows"][call_strike]
                call_delta = call_row.get("call_delta", 0.0)
                if not (0.10 <= call_delta <= 0.45):
                    continue

                legs = [
                    _make_leg(expiry, put_strike, "put", "buy"),
                    _make_leg(expiry, call_strike, "call", "sell"),
                ]
                net = _net_premium(legs)

                score = _option_liquidity(expiry, put_strike, "put")
                score += _option_liquidity(expiry, call_strike, "call")
                score += _expiry_bonus(expiry, title)
                score -= abs(put_delta - put_target) * 7.0
                score -= abs(call_delta - call_target) * 7.0
                score += 0.20 if net <= 0.20 else -0.20 * max(net, 0.0)

                if best is None or score > best["score"]:
                    best = {
                        "score": score,
                        "result": _strategy_result(
                            title,
                            expiry,
                            legs,
                            "Assume 100 cotas de BOVA11 em carteira para proteção via collar.",
                            "Montado para equilibrar hedge, upside residual e custo total da proteção.",
                            "Estratégia coberta; limita parte da alta em troca de proteção ou redução de custo.",
                        ),
                    }
    return best["result"] if best else None


def suggest_covered_call(expiries: list, spot: float):
    best = None
    for expiry in expiries:
        step = expiry["step"]
        for strike in expiry["strikes"]:
            if strike <= spot or strike > spot + 10 * step:
                continue
            row = expiry["rows"][strike]
            credit = _trade_price(row, "call", "sell")
            delta = row.get("call_delta", 0.0)
            if credit <= 0 or not (0.10 <= delta <= 0.40):
                continue

            score = _option_liquidity(expiry, strike, "call") + _expiry_bonus(expiry, "Venda Coberta")
            score -= abs(delta - 0.22) * 7.0
            score += min(credit, 2.0) * 0.15

            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "result": _strategy_result(
                        "Venda Coberta",
                        expiry,
                        [_make_leg(expiry, strike, "call", "sell")],
                        "Assume 100 cotas de BOVA11 em carteira para cobrir a call vendida.",
                        "Call escolhida por liquidez, delta moderado e crédito interessante sobre o spot atual.",
                        "Estratégia de renda com upside limitado acima do strike vendido.",
                    ),
                }
    return best["result"] if best else None


def generate_strategy_suggestions(estrutura: str, expiries: list, spot: float, gamma_flip: float, sig_dex: str) -> list:
    strategy_names = [item.strip() for item in estrutura.split(",") if item.strip()]
    suggestions = []

    for name in strategy_names:
        suggestion = None
        if name == "Compra de Call":
            suggestion = suggest_buy_call(expiries, spot)
        elif name == "Compra de Put":
            suggestion = suggest_buy_put(expiries, spot)
        elif name == "Compra de Straddle":
            suggestion = suggest_buy_straddle(expiries, spot)
        elif name == "Venda de Put":
            suggestion = suggest_sell_put(expiries, spot)
        elif name == "Box de 3 Pontas":
            suggestion = suggest_box_3_pontas(expiries, spot, gamma_flip)
        elif name == "Broken Wing Butterfly":
            suggestion = suggest_broken_wing_butterfly(expiries, spot, bullish=(sig_dex == "POS"))
        elif name == "Collar de Alta":
            suggestion = suggest_collar(expiries, spot, aggressive=True)
        elif name == "Collar de Baixa":
            suggestion = suggest_collar(expiries, spot, aggressive=False)
        elif name == "Venda Coberta":
            suggestion = suggest_covered_call(expiries, spot)

        if suggestion:
            suggestions.append(suggestion)

    return suggestions


def render_strategy_suggestions_html(suggestions: list) -> str:
    if not suggestions:
        return ""

    cards = []
    for suggestion in suggestions:
        legs_html = []
        for leg in suggestion["legs"]:
            verb = "Compra" if leg["action"] == "buy" else "Venda"
            details = [f"@ R$ {leg['price']:.2f}"]
            if leg.get("ticker"):
                details.append(escape(leg["ticker"]))
            if leg.get("delta"):
                details.append(f"Δ {leg['delta']:+.2f}")
            if leg.get("oi", 0.0) > 0:
                details.append(f"OI {fmt_qty(leg['oi'])}")
            if leg.get("volume", 0.0) > 0:
                details.append(f"Vol {fmt_qty(leg['volume'])}")

            legs_html.append(
                f"<li><strong>{verb} {leg['qty']} {leg['option_type']} {leg['strike']:.0f}</strong>"
                f"<span>{' | '.join(details)}</span></li>"
            )

        dte_label = f"{suggestion['dte']} DTE" if suggestion.get("dte") is not None else "DTE n/d"
        premium_class = "credit" if suggestion["net_premium"] < 0 else "debit"
        risk_html = f'<div class="suggest-risk">{escape(suggestion["risk"])}</div>' if suggestion.get("risk") else ""

        cards.append(
            f"""
            <div class="suggest-card">
              <div class="suggest-top">
                <div>
                  <div class="suggest-name">{escape(suggestion['title'])}</div>
                  <div class="suggest-meta">{escape(suggestion['expiry'])} &middot; {escape(dte_label)}</div>
                </div>
                <span class="badge-sm blue">STRIKES</span>
              </div>
              <div class="suggest-model">{escape(suggestion['model'])}</div>
              <ul class="suggest-legs">
                {''.join(legs_html)}
              </ul>
              <div class="suggest-premium {premium_class}">{escape(suggestion['premium_text'])}</div>
              <div class="suggest-why">{escape(suggestion['why'])}</div>
              {risk_html}
            </div>
            """
        )

    return f"""
    <div class="section-title">Sugestão de Strikes</div>
    <div class="suggestions-grid">
      {''.join(cards)}
    </div>
    """


# ═══════════════════════════════════════════════════════════════
# 5. GERAÇÃO DO HTML
# ═══════════════════════════════════════════════════════════════

def build_html(
    ref_date: str,
    ref_tag: str,
    spot_d: float,
    spot_d1: float,
    total_gex: float,
    total_dex: float,
    total_vex: float,
    total_tex: float,
    gex_descoberto: float,
    gamma_flip: float,
    sig_gex: str,
    sig_dex: str,
    sig_vex: str,
    sig_tex: str,
    scenario_key: tuple,
    per_strike: dict,
    strategy_suggestions: list,
) -> str:
    scenario_num, scenario_name, estrutura, evitar = SCENARIOS[scenario_key]
    interpretacao = INTERPRETACOES.get(scenario_num, "")
    force = determine_force(sig_gex, sig_dex, sig_vex, sig_tex)
    direction = "ALTA" if sig_dex == "POS" else "QUEDA"

    # Format numbers
    spot_str  = f"{spot_d:.2f}"
    gex_str   = fmt_m(total_gex)
    dex_str   = fmt_m(total_dex)
    vex_str   = fmt_m(total_vex)
    tex_str   = fmt_m(total_tex)
    gfstr     = f"{gamma_flip:.0f}" if gamma_flip else "—"

    # Badge colors
    gex_color   = "green" if sig_gex == "LONG" else "red"
    dex_color   = "green" if sig_dex == "POS"  else "red"
    vex_color   = "amber" if sig_vex == "ALTO" else "blue"
    tex_color   = "amber" if sig_tex == "ALTO" else "blue"
    dir_color   = "green" if direction == "ALTA" else "red"
    force_color = {"FORTE": "green", "MODERADO": "amber", "LEVE": "blue"}[force]

    # Stat card color helpers
    gex_val_cls = "green" if total_gex > 0 else "red"
    dex_val_cls = "green" if total_dex > 0 else "red"
    vex_val_cls = "amber" if abs(total_vex) > 500 else "blue"
    tex_val_cls = "amber" if abs(total_tex) > 200 else "blue"
    gex_sub     = "Long Gamma" if total_gex > 0 else "Short Gamma"
    dex_sub     = "Pressão Compradora" if total_dex > 0 else "Pressão Vendedora"
    vex_sub     = "IV Elevada" if abs(total_vex) > 500 else "IV Baixa"
    tex_sub     = "Decay Alto" if abs(total_tex) > 200 else "Decay Baixo"

    # Pct change
    pct_change = ((spot_d - spot_d1) / spot_d1 * 100) if spot_d1 else 0.0
    pct_sign   = "+" if pct_change >= 0 else ""
    pct_color  = "green" if pct_change >= 0 else "red"
    pct_str    = f"{pct_sign}{pct_change:.2f}%"

    # Chart data (JSON)
    chart_strikes  = json.dumps(per_strike["strikes"])
    chart_gex_net  = json.dumps([round(v / 1e6, 4) for v in per_strike["gex_net"]])
    chart_dex_net  = json.dumps([round(v / 1e6, 4) for v in per_strike["dex_net"]])
    chart_vex_net  = json.dumps([round(v / 1e6, 4) for v in per_strike["vex_net"]])
    chart_tex_net  = json.dumps([round(v / 1e6, 4) for v in per_strike["tex_net"]])
    strategy_suggestions_html = render_strategy_suggestions_html(strategy_suggestions)

    # Scenario table rows
    table_rows = ""
    for key, (num, name, est, ev) in sorted(SCENARIOS.items(), key=lambda x: x[1][0]):
        sg, sd, sv, st = key
        is_active  = key == scenario_key
        row_class  = ' class="active-row"' if is_active else ""
        gex_badge  = f'<span class="badge-sm {"green" if sg == "LONG" else "red"}">{sg}</span>'
        dex_badge  = f'<span class="badge-sm {"green" if sd == "POS"  else "red"}">{sd}</span>'
        vex_badge  = f'<span class="badge-sm {"amber" if sv == "ALTO" else "blue"}">{sv}</span>'
        tex_badge  = f'<span class="badge-sm {"amber" if st == "ALTO" else "blue"}">{st}</span>'
        prefix     = "&#9654; " if is_active else ""
        table_rows += (
            f'<tr{row_class}>'
            f'<td class="mono">#{num}</td>'
            f'<td>{gex_badge}</td>'
            f'<td>{dex_badge}</td>'
            f'<td>{vex_badge}</td>'
            f'<td>{tex_badge}</td>'
            f'<td class="scenario-name">{prefix}{name}</td>'
            f'<td class="small-text">{est}</td>'
            f'<td class="small-text red-text">{ev}</td>'
            f'</tr>\n'
        )

    # Regime alert content
    if sig_gex == "SHORT":
        regime_desc   = "Dealers estão em short gamma — amplificam movimentos do mercado em ambas as direções."
        regime_change = "Mudança para LONG gamma ocorre quando dealers acumulam posições compradas em gamma, geralmente após grande queda ou expiração de opções."
    else:
        regime_desc   = "Dealers estão em long gamma — amortecem movimentos e tendem a estabilizar o mercado."
        regime_change = "Mudança para SHORT gamma ocorre quando há aumento de OI em calls fora do dinheiro ou aumento de put selling institucional."

    if sig_dex == "POS":
        pressao = "Pressão <strong>compradora</strong> líquida identificada. DEX positivo indica dealers com viés de compra no spot para hedge."
    else:
        pressao = "Pressão <strong>vendedora</strong> líquida identificada. DEX negativo indica dealers com viés de venda no spot para hedge."

    html = f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 Diagnóstico 4D — {ref_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
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

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: var(--font);
    background: var(--bg);
    color: var(--t1);
    min-height: 100vh;
    padding-bottom: 60px;
  }}

  /* ── Header ── */
  .page-header {{
    background: var(--bg2);
    border-bottom: 1px solid var(--border2);
    padding: 18px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    position: sticky;
    top: 0;
    z-index: 100;
  }}
  .header-left h1 {{
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--t1);
    letter-spacing: -0.02em;
  }}
  .header-left .subtitle {{
    font-size: 0.8rem;
    color: var(--t3);
    font-family: var(--mono);
    margin-top: 2px;
  }}
  .theme-btn {{
    background: var(--bg3);
    border: 1px solid var(--border2);
    border-radius: 8px;
    padding: 7px 14px;
    font-size: 0.82rem;
    color: var(--t2);
    cursor: pointer;
    font-family: var(--font);
    transition: all 0.2s;
  }}
  .theme-btn:hover {{ color: var(--t1); border-color: var(--blue); }}

  /* ── Container ── */
  .container {{
    max-width: 1400px;
    margin: 0 auto;
    padding: 24px 32px;
  }}

  /* ── Stat strip ── */
  .stats-strip {{
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 12px;
    margin-bottom: 24px;
  }}
  @media (max-width: 1100px) {{ .stats-strip {{ grid-template-columns: repeat(3, 1fr); }} }}
  @media (max-width: 600px)  {{ .stats-strip {{ grid-template-columns: repeat(2, 1fr); }} }}

  .stat-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
  }}
  .stat-card .label {{
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--t3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }}
  .stat-card .value {{
    font-size: 1.3rem;
    font-weight: 700;
    font-family: var(--mono);
    color: var(--t1);
    line-height: 1.1;
  }}
  .stat-card .value.green {{ color: var(--green); }}
  .stat-card .value.red   {{ color: var(--red); }}
  .stat-card .value.amber {{ color: var(--amber); }}
  .stat-card .value.blue  {{ color: var(--blue); }}
  .stat-card .sub {{
    font-size: 0.72rem;
    color: var(--t3);
    margin-top: 3px;
    font-family: var(--mono);
  }}

  /* ── Main row ── */
  .main-row {{
    display: grid;
    grid-template-columns: 60fr 40fr;
    gap: 16px;
    margin-bottom: 20px;
  }}
  @media (max-width: 900px) {{ .main-row {{ grid-template-columns: 1fr; }} }}

  /* ── Card ── */
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 22px 24px;
  }}
  .card-title {{
    font-size: 0.72rem;
    font-weight: 700;
    color: var(--t3);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 16px;
  }}

  /* ── Scenario heading ── */
  .scenario-name-large {{
    font-size: 1.7rem;
    font-weight: 800;
    color: var(--t1);
    letter-spacing: -0.03em;
    line-height: 1.1;
    margin-bottom: 4px;
  }}
  .scenario-num {{
    font-size: 0.8rem;
    font-family: var(--mono);
    color: var(--t3);
    margin-bottom: 20px;
  }}

  /* ── Signal 2x2 grid ── */
  .signals-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 20px;
  }}
  .signal-block {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .signal-label {{
    font-size: 0.72rem;
    font-weight: 700;
    color: var(--t3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}

  /* ── Badges ── */
  .badge {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    font-family: var(--mono);
  }}
  .badge.green {{ background: rgba(63,185,80,0.15);  color: var(--green); border: 1px solid rgba(63,185,80,0.3); }}
  .badge.red   {{ background: rgba(248,81,73,0.15);   color: var(--red);   border: 1px solid rgba(248,81,73,0.3); }}
  .badge.amber {{ background: rgba(210,153,34,0.15);  color: var(--amber); border: 1px solid rgba(210,153,34,0.3); }}
  .badge.blue  {{ background: rgba(88,166,255,0.15);  color: var(--blue);  border: 1px solid rgba(88,166,255,0.3); }}

  .badge-sm {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.7rem;
    font-weight: 700;
    font-family: var(--mono);
  }}
  .badge-sm.green {{ background: rgba(63,185,80,0.15);  color: var(--green); }}
  .badge-sm.red   {{ background: rgba(248,81,73,0.15);   color: var(--red); }}
  .badge-sm.amber {{ background: rgba(210,153,34,0.15);  color: var(--amber); }}
  .badge-sm.blue  {{ background: rgba(88,166,255,0.15);  color: var(--blue); }}

  /* ── Interpretation ── */
  .interpretacao {{
    font-size: 0.9rem;
    color: var(--t2);
    line-height: 1.6;
    margin-bottom: 18px;
    padding: 12px 14px;
    background: var(--bg2);
    border-left: 3px solid var(--blue);
    border-radius: 0 8px 8px 0;
  }}

  .force-row {{
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .label-small {{
    font-size: 0.74rem;
    color: var(--t3);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}

  /* ── Strategies ── */
  .strategy-section {{ margin-bottom: 16px; }}
  .strategy-section:last-child {{ margin-bottom: 0; }}
  .strategy-heading {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }}
  .strategy-text {{
    font-size: 0.93rem;
    color: var(--t1);
    font-weight: 500;
    line-height: 1.5;
  }}
  .red-text {{ color: var(--red) !important; }}

  /* ── Strike Suggestions ── */
  .suggestions-grid {{
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    margin-bottom: 20px;
  }}
  @media (max-width: 900px) {{ .suggestions-grid {{ grid-template-columns: 1fr; }} }}
  .suggest-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 20px;
  }}
  .suggest-top {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 10px;
  }}
  .suggest-name {{
    font-size: 1rem;
    font-weight: 700;
    color: var(--t1);
  }}
  .suggest-meta {{
    font-size: 0.75rem;
    color: var(--t3);
    font-family: var(--mono);
    margin-top: 3px;
  }}
  .suggest-model,
  .suggest-why,
  .suggest-risk {{
    font-size: 0.84rem;
    color: var(--t2);
    line-height: 1.55;
  }}
  .suggest-model {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 12px;
  }}
  .suggest-legs {{
    list-style: none;
    display: grid;
    gap: 8px;
    margin-bottom: 14px;
  }}
  .suggest-legs li {{
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 10px 12px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
  }}
  .suggest-legs li strong {{
    color: var(--t1);
    font-size: 0.88rem;
  }}
  .suggest-legs li span {{
    color: var(--t3);
    font-size: 0.76rem;
    font-family: var(--mono);
    line-height: 1.5;
    word-break: break-word;
  }}
  .suggest-premium {{
    font-size: 0.86rem;
    font-weight: 700;
    padding: 10px 12px;
    border-radius: 10px;
    margin-bottom: 10px;
    border: 1px solid transparent;
  }}
  .suggest-premium.credit {{
    color: var(--green);
    background: rgba(63,185,80,0.10);
    border-color: rgba(63,185,80,0.18);
  }}
  .suggest-premium.debit {{
    color: var(--amber);
    background: rgba(210,153,34,0.10);
    border-color: rgba(210,153,34,0.18);
  }}
  .suggest-risk {{
    margin-top: 8px;
    color: var(--red);
  }}

  /* ── Alert card ── */
  .alert-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 24px;
    margin-bottom: 20px;
  }}
  .alert-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 16px;
    margin-top: 14px;
  }}
  @media (max-width: 800px) {{ .alert-grid {{ grid-template-columns: 1fr; }} }}
  .al-title {{
    font-size: 0.72rem;
    font-weight: 700;
    color: var(--t3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }}
  .al-text {{
    font-size: 0.87rem;
    color: var(--t2);
    line-height: 1.55;
  }}

  /* ── Charts 2x2 ── */
  .charts-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }}
  @media (max-width: 800px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
  .chart-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 20px;
  }}
  .chart-card .chart-title {{
    font-size: 0.75rem;
    font-weight: 700;
    color: var(--t3);
    text-transform: uppercase;
    letter-spacing: 0.07em;
    margin-bottom: 14px;
  }}
  .chart-wrap {{ position: relative; height: 200px; }}

  /* ── Matrix table ── */
  .matrix-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 24px;
    overflow-x: auto;
  }}
  .matrix-card .card-title {{ margin-bottom: 14px; }}
  table.matrix {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.83rem;
  }}
  table.matrix th {{
    text-align: left;
    padding: 8px 10px;
    font-size: 0.7rem;
    font-weight: 700;
    color: var(--t3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 1px solid var(--border2);
    white-space: nowrap;
  }}
  table.matrix td {{
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
    color: var(--t2);
  }}
  table.matrix tr:last-child td {{ border-bottom: none; }}
  table.matrix tr.active-row td {{
    background: rgba(88,166,255,0.07);
    color: var(--t1);
  }}
  table.matrix tr.active-row .scenario-name {{
    color: var(--blue);
    font-weight: 700;
  }}
  table.matrix .mono {{ font-family: var(--mono); color: var(--t3); font-size: 0.75rem; }}
  table.matrix .scenario-name {{ font-weight: 600; white-space: nowrap; }}
  table.matrix .small-text {{ font-size: 0.78rem; max-width: 200px; }}
  table.matrix tr:hover td {{ background: var(--bg2); }}
  table.matrix tr.active-row:hover td {{ background: rgba(88,166,255,0.12); }}

  /* ── Section title ── */
  .section-title {{
    font-size: 0.72rem;
    font-weight: 700;
    color: var(--t3);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 12px;
    margin-top: 28px;
  }}
  hr.divider {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 18px 0;
  }}
</style>
</head>
<body>

<!-- ── Header ── -->
<header class="page-header">
  <div class="header-left">
    <h1>BOVA11 Diagnóstico 4D</h1>
    <div class="subtitle">GEX &middot; DEX &middot; VEX &middot; TEX &nbsp;|&nbsp; Ref: {ref_date} ({ref_tag})</div>
  </div>
  <button class="theme-btn" id="theme-btn" onclick="toggleTheme()">◐</button>
</header>

<div class="container">

  <!-- ── Stat Cards ── -->
  <div class="stats-strip">
    <div class="stat-card">
      <div class="label">Spot D</div>
      <div class="value mono">R$ {spot_str}</div>
      <div class="sub" style="color:var(--{pct_color});">{pct_str} vs D-1</div>
    </div>
    <div class="stat-card">
      <div class="label">Gamma Flip</div>
      <div class="value mono">{gfstr}</div>
      <div class="sub">Strike de inversão GEX</div>
    </div>
    <div class="stat-card">
      <div class="label">GEX Total</div>
      <div class="value mono {gex_val_cls}">{gex_str}</div>
      <div class="sub">{gex_sub}</div>
    </div>
    <div class="stat-card">
      <div class="label">DEX Total</div>
      <div class="value mono {dex_val_cls}">{dex_str}</div>
      <div class="sub">{dex_sub}</div>
    </div>
    <div class="stat-card">
      <div class="label">VEX Total</div>
      <div class="value mono {vex_val_cls}">{vex_str}</div>
      <div class="sub">{vex_sub}</div>
    </div>
    <div class="stat-card">
      <div class="label">TEX Total</div>
      <div class="value mono {tex_val_cls}">{tex_str}</div>
      <div class="sub">{tex_sub}</div>
    </div>
  </div>

  <!-- ── Main Row: Diagnostic + Strategy ── -->
  <div class="main-row">

    <!-- Diagnostic card -->
    <div class="card">
      <div class="card-title">Cenário Identificado</div>
      <div class="scenario-name-large">{scenario_name}</div>
      <div class="scenario-num">Cenário #{scenario_num} &mdash; Matriz 4D de 16 Cenários</div>

      <div class="signals-grid">
        <div class="signal-block">
          <span class="signal-label">GEX</span>
          <span class="badge {gex_color}">{sig_gex}</span>
        </div>
        <div class="signal-block">
          <span class="signal-label">DEX</span>
          <span class="badge {dex_color}">{sig_dex}</span>
        </div>
        <div class="signal-block">
          <span class="signal-label">VEX</span>
          <span class="badge {vex_color}">{sig_vex}</span>
        </div>
        <div class="signal-block">
          <span class="signal-label">TEX</span>
          <span class="badge {tex_color}">{sig_tex}</span>
        </div>
      </div>

      <div class="interpretacao">{interpretacao}</div>

      <div class="force-row">
        <span class="label-small">Força do Sinal:</span>
        <span class="badge {force_color}">{force}</span>
        <span class="label-small" style="margin-left:8px;">Direção Provável:</span>
        <span class="badge {dir_color}">{direction}</span>
      </div>
    </div>

    <!-- Strategy card -->
    <div class="card">
      <div class="card-title">Estratégias</div>

      <div class="strategy-section">
        <div class="strategy-heading">
          <span class="label-small">Estrutura Ideal</span>
          <span class="badge green">PRINCIPAL</span>
        </div>
        <div class="strategy-text">{estrutura}</div>
      </div>

      <hr class="divider">

      <div class="strategy-section">
        <div class="strategy-heading">
          <span class="label-small">O Que Evitar</span>
          <span class="badge red">RISCO</span>
        </div>
        <div class="strategy-text red-text">{evitar}</div>
      </div>
    </div>
  </div>

  <!-- ── Alertas ── -->
  <div class="alert-card">
    <div class="card-title">Alertas de Mudança de Cenário</div>
    <div class="alert-grid">
      <div>
        <div class="al-title">Regime Atual</div>
        <div class="al-text">{regime_desc}</div>
      </div>
      <div>
        <div class="al-title">Gatilho de Mudança</div>
        <div class="al-text">{regime_change}</div>
      </div>
      <div>
        <div class="al-title">Fluxo de Hedge</div>
        <div class="al-text">{pressao}</div>
      </div>
    </div>
  </div>

  <!-- ── Charts 2x2 ── -->
  <div class="section-title">Gráficos Principais</div>
  <div class="charts-grid">
    <div class="chart-card">
      <div class="chart-title">GEX por Strike (M)</div>
      <div class="chart-wrap"><canvas id="chartGex"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">DEX Momentum (M)</div>
      <div class="chart-wrap"><canvas id="chartDex"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">VEX por Strike (M)</div>
      <div class="chart-wrap"><canvas id="chartVex"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">TEX Cumulativo (M)</div>
      <div class="chart-wrap"><canvas id="chartTex"></canvas></div>
    </div>
  </div>

  <!-- ── Decision Matrix ── -->
  <div class="section-title">Matriz de Decisão 4D &mdash; 16 Cenários</div>
  <div class="matrix-card">
    <table class="matrix">
      <thead>
        <tr>
          <th>#</th>
          <th>GEX</th>
          <th>DEX</th>
          <th>VEX</th>
          <th>TEX</th>
          <th>Cenário</th>
          <th>Estrutura Ideal</th>
          <th>Evitar</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>

  {strategy_suggestions_html}

</div><!-- /container -->

<script>
  // ── Theme ──
  (function() {{
    try {{
      var t = localStorage.getItem('bova11-theme') || 'dark';
      document.documentElement.setAttribute('data-theme', t);
      document.getElementById('theme-btn').textContent = '◐';
    }} catch(e) {{}}
  }})();

  function toggleTheme() {{
    var html = document.documentElement;
    var next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    document.getElementById('theme-btn').textContent = '◐';
    try {{ localStorage.setItem('bova11-theme', next); }} catch(e) {{}}
    initCharts();
  }}

  // ── Chart data ──
  var labels   = {chart_strikes};
  var gexNet   = {chart_gex_net};
  var dexNet   = {chart_dex_net};
  var vexNet   = {chart_vex_net};
  var texNet   = {chart_tex_net};

  function isDark() {{ return document.documentElement.getAttribute('data-theme') === 'dark'; }}

  function palette() {{
    var d = isDark();
    return {{
      grid:  d ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)',
      tick:  d ? '#636c76' : '#9C9A91',
      green: d ? '#3fb950' : '#148A63',
      red:   d ? '#f85149' : '#B33530',
      blue:  d ? '#58a6ff' : '#2E6BBF',
      amber: d ? '#d29922' : '#B8720A',
      gold:  '#f0c040',
    }};
  }}

  var charts = {{}};

  function makeOpts() {{
    var p = palette();
    return {{
      responsive: true,
      maintainAspectRatio: false,
      animation: {{ duration: 300 }},
      plugins: {{ legend: {{ display: false }}, tooltip: {{ mode: 'index', intersect: false }} }},
      scales: {{
        x: {{ ticks: {{ color: p.tick, font: {{ size: 10 }} }}, grid: {{ color: p.grid }} }},
        y: {{ ticks: {{ color: p.tick, font: {{ size: 10 }} }}, grid: {{ color: p.grid }} }}
      }}
    }};
  }}

  function buildChart(id, cfg) {{
    if (charts[id]) {{ charts[id].destroy(); }}
    charts[id] = new Chart(document.getElementById(id).getContext('2d'), cfg);
  }}

  function initCharts() {{
    var p = palette();

    // GEX
    buildChart('chartGex', {{
      type: 'bar',
      data: {{ labels: labels, datasets: [{{
        label: 'GEX Net',
        data: gexNet,
        backgroundColor: gexNet.map(function(v) {{ return v >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'; }}),
        borderColor:     gexNet.map(function(v) {{ return v >= 0 ? p.green : p.red; }}),
        borderWidth: 1
      }}] }},
      options: makeOpts()
    }});

    // DEX
    buildChart('chartDex', {{
      type: 'bar',
      data: {{ labels: labels, datasets: [{{
        label: 'DEX Net',
        data: dexNet,
        backgroundColor: dexNet.map(function(v) {{ return v >= 0 ? 'rgba(63,185,80,0.6)' : 'rgba(248,81,73,0.6)'; }}),
        borderColor:     dexNet.map(function(v) {{ return v >= 0 ? p.green : p.red; }}),
        borderWidth: 1
      }}] }},
      options: makeOpts()
    }});

    // VEX
    buildChart('chartVex', {{
      type: 'bar',
      data: {{ labels: labels, datasets: [{{
        label: 'VEX Net',
        data: vexNet,
        backgroundColor: vexNet.map(function(v) {{ return v >= 0 ? 'rgba(88,166,255,0.6)' : 'rgba(210,153,34,0.6)'; }}),
        borderColor:     vexNet.map(function(v) {{ return v >= 0 ? p.blue : p.amber; }}),
        borderWidth: 1
      }}] }},
      options: makeOpts()
    }});

    // TEX cumulative line
    var acc = 0;
    var texCum = texNet.map(function(v) {{ acc += v; return Math.round(acc * 100) / 100; }});
    buildChart('chartTex', {{
      type: 'line',
      data: {{ labels: labels, datasets: [{{
        label: 'TEX Acumulado',
        data: texCum,
        borderColor: p.gold,
        backgroundColor: 'rgba(240,192,64,0.1)',
        borderWidth: 2,
        pointRadius: 2,
        fill: true,
        tension: 0.35
      }}] }},
      options: makeOpts()
    }});
  }}

  initCharts();
</script>
</body>
</html>"""

    return html


# ═══════════════════════════════════════════════════════════════
# 6. MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="BOVA11 Diagnostico 4D — GEX+DEX+VEX+TEX -> 16 Cenarios"
    )
    parser.add_argument("--data-dir",  required=True,              help="Diretorio com os CSVs B3")
    parser.add_argument("--output",    required=True,              help="Caminho do HTML de saida")
    parser.add_argument("--ref-date",  required=True,              help="Data de referencia ISO (ex: 2026-03-25)")
    parser.add_argument("--ref-tag",   required=True,              help="Tag original do CSV (ex: 25mar)")
    parser.add_argument("--spot-d",    required=True, type=float,  help="Spot price D")
    parser.add_argument("--spot-d1",   required=True, type=float,  help="Spot price D-1")
    args = parser.parse_args()

    print(f"  [15/15] BOVA11 Diagnostico 4D")
    print(f"  Referencia: {args.ref_date} (tag: {args.ref_tag})")
    print(f"  Spot D: {args.spot_d:.2f}  |  Spot D-1: {args.spot_d1:.2f}")

    # 1. Discover CSV files
    expirations = discover_expirations_with_volume(args.data_dir, args.ref_tag)

    if not expirations:
        print(f"  ERRO: Nenhum CSV encontrado em '{args.data_dir}' para tag '{args.ref_tag}'")
        sys.exit(1)

    print(f"  Vencimentos encontrados: {list(expirations.keys())}")

    # 2. Load all rows from all expirations
    all_rows = []
    market_expiries = []
    for venc, files in expirations.items():
        try:
            close_fp = files.get("close")
            if not close_fp:
                print(f"    AVISO: {venc} sem arquivo de fechamento. Pulando.")
                continue

            rows = load_b3(close_fp)
            vol_rows = load_volume_csv(files.get("volume"))
            all_rows.extend(rows)
            print(f"    {venc}: {len(rows)} strikes carregados")
            market_expiries.append(_build_expiry_market(venc, rows, vol_rows, args.ref_date))
        except Exception as e:
            print(f"    AVISO: erro ao ler {venc}: {e}")

    if not all_rows:
        print("  ERRO: Nenhum dado carregado dos CSVs.")
        sys.exit(1)

    # 3. Aggregate by strike (weighted average for greeks, sum for OI)
    by_strike = {}
    for r in all_rows:
        s = r["strike"]
        if s not in by_strike:
            by_strike[s] = {
                "strike":     s,
                "call_oi":    0.0, "put_oi":    0.0,
                "w_c_delta":  0.0, "w_c_gamma": 0.0,
                "w_c_theta":  0.0, "w_c_vega":  0.0,
                "w_p_delta":  0.0, "w_p_gamma": 0.0,
                "w_p_theta":  0.0, "w_p_vega":  0.0,
                "sum_c_oi":   0.0, "sum_p_oi":  0.0,
            }
        agg = by_strike[s]
        agg["call_oi"] += r["call_oi"]
        agg["put_oi"]  += r["put_oi"]
        if r["call_oi"] > 0:
            agg["w_c_delta"] += r["call_delta"] * r["call_oi"]
            agg["w_c_gamma"] += r["call_gamma"] * r["call_oi"]
            agg["w_c_theta"] += r["call_theta"] * r["call_oi"]
            agg["w_c_vega"]  += r["call_vega"]  * r["call_oi"]
            agg["sum_c_oi"]  += r["call_oi"]
        if r["put_oi"] > 0:
            agg["w_p_delta"] += r["put_delta"] * r["put_oi"]
            agg["w_p_gamma"] += r["put_gamma"] * r["put_oi"]
            agg["w_p_theta"] += r["put_theta"] * r["put_oi"]
            agg["w_p_vega"]  += r["put_vega"]  * r["put_oi"]
            agg["sum_p_oi"]  += r["put_oi"]

    merged_rows = []
    for s in sorted(by_strike.keys()):
        a   = by_strike[s]
        sc  = a["sum_c_oi"]
        sp  = a["sum_p_oi"]
        merged_rows.append({
            "strike":     s,
            "call_oi":    a["call_oi"],
            "put_oi":     a["put_oi"],
            "call_delta": a["w_c_delta"] / sc if sc > 0 else 0.0,
            "call_gamma": a["w_c_gamma"] / sc if sc > 0 else 0.0,
            "call_theta": a["w_c_theta"] / sc if sc > 0 else 0.0,
            "call_vega":  a["w_c_vega"]  / sc if sc > 0 else 0.0,
            "put_delta":  a["w_p_delta"] / sp if sp > 0 else 0.0,
            "put_gamma":  a["w_p_gamma"] / sp if sp > 0 else 0.0,
            "put_theta":  a["w_p_theta"] / sp if sp > 0 else 0.0,
            "put_vega":   a["w_p_vega"]  / sp if sp > 0 else 0.0,
        })

    # 4. Calculate greeks per strike
    per_strike = calc_greeks(merged_rows, args.spot_d)

    # 5. Aggregate totals (in millions)
    total_gex = sum(per_strike["gex_net"])  / 1e6
    total_dex = sum(per_strike["dex_net"])  / 1e6
    total_vex = sum(per_strike["vex_net"])  / 1e6
    total_tex = sum(per_strike["tex_net"])  / 1e6

    if total_gex >= 0:
        gex_descoberto = sum(abs(v) for v in per_strike["gex_net"] if v < 0) / 1e6
    else:
        gex_descoberto = sum(abs(v) for v in per_strike["gex_net"] if v > 0) / 1e6

    gamma_flip = find_gamma_flip(per_strike["strikes"], per_strike["gex_net"])

    print(f"  GEX: {total_gex:.2f}M  DEX: {total_dex:.2f}M  VEX: {total_vex:.2f}M  TEX: {total_tex:.2f}M")
    print(f"  Gamma Flip: {gamma_flip:.0f}  |  GEX Descoberto: {gex_descoberto:.2f}M")

    # 6. Determine signals and scenario
    sig_gex, sig_dex, sig_vex, sig_tex = determine_signals(total_gex, total_dex, total_vex, total_tex)
    scenario_key = (sig_gex, sig_dex, sig_vex, sig_tex)

    print(f"  Sinais: GEX={sig_gex}  DEX={sig_dex}  VEX={sig_vex}  TEX={sig_tex}")
    print(f"  Cenario: #{SCENARIOS[scenario_key][0]} — {SCENARIOS[scenario_key][1]}")

    strategy_suggestions = generate_strategy_suggestions(
        estrutura=SCENARIOS[scenario_key][2],
        expiries=market_expiries,
        spot=args.spot_d,
        gamma_flip=gamma_flip,
        sig_dex=sig_dex,
    )

    if strategy_suggestions:
        print("  Sugestões de strike geradas:")
        for suggestion in strategy_suggestions:
            dte = suggestion.get("dte")
            dte_text = f"{dte} DTE" if dte is not None else "DTE n/d"
            print(f"    - {suggestion['title']}: {suggestion['expiry']} ({dte_text})")
    else:
        print("  AVISO: não foi possível montar sugestões operacionais para as estruturas deste cenário.")

    # 7. Generate HTML
    html = build_html(
        ref_date       = args.ref_date,
        ref_tag        = args.ref_tag,
        spot_d         = args.spot_d,
        spot_d1        = args.spot_d1,
        total_gex      = total_gex,
        total_dex      = total_dex,
        total_vex      = total_vex,
        total_tex      = total_tex,
        gex_descoberto = gex_descoberto,
        gamma_flip     = gamma_flip,
        sig_gex        = sig_gex,
        sig_dex        = sig_dex,
        sig_vex        = sig_vex,
        sig_tex        = sig_tex,
        scenario_key   = scenario_key,
        per_strike     = per_strike,
        strategy_suggestions = strategy_suggestions,
    )

    # 8. Write output
    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  HTML gerado: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
