#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 — Tape Flow (Módulo 21)
==============================
Lê Times & Trades do BOVA11 e gera leitura intraday de microfluxo:
VWAP, CVD, agressão compradora/vendedora, volume profile, blocos e corretoras.

Uso:
  python3 scripts/bova11_tape_flow.py \
      --times-file "/path/to/times bova11.xlsx" \
      --output output/bova11_tape_flow.html \
      --ref-date 2026-04-22 \
      --ref-tag 22abr \
      --spot 189.22

Dependências: Python 3 stdlib apenas.
"""

from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import math
import os
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


# ═══════════════════════════════════════════════════════════════
# Normalização e formatos
# ═══════════════════════════════════════════════════════════════

def strip_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm_key(value: str) -> str:
    text = strip_accents(value).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


def parse_number(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and math.isnan(raw):
            return None
        return float(raw)
    s = str(raw).strip()
    if s in ("", "-", "--"):
        return None
    s = s.replace("R$", "").replace("%", "").strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def parse_time_seconds(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Excel guarda hora como fração do dia quando a célula é numérica.
        frac = float(raw) % 1.0
        return round(frac * 86400.0, 3)

    s = str(raw).strip()
    if not s:
        return None
    for fmt in (
        "%H:%M:%S.%f",
        "%H:%M:%S",
        "%H:%M",
        "%d/%m/%Y %H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
        except ValueError:
            pass
    return None


def fmt_time(seconds: float) -> str:
    seconds = float(seconds or 0.0)
    hour = int(seconds // 3600)
    minute = int((seconds % 3600) // 60)
    second = int(seconds % 60)
    milli = int(round((seconds - int(seconds)) * 1000))
    if milli:
        return f"{hour:02d}:{minute:02d}:{second:02d}.{milli:03d}"
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def fmt_minute(bucket: int) -> str:
    hour = bucket // 60
    minute = bucket % 60
    return f"{hour:02d}:{minute:02d}"


def fmt_price(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}".replace(".", ",")


def fmt_int(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{int(round(value)):,}".replace(",", ".")


def fmt_signed_int(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else "-"
    return f"{sign}{fmt_int(abs(value))}"


def fmt_pct(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}%".replace(".", ",")


def fmt_brl(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    abs_v = abs(value)
    sign = "-" if value < 0 else ""
    if abs_v >= 1_000_000_000:
        return f"{sign}R$ {abs_v / 1_000_000_000:.2f} bi".replace(".", ",")
    if abs_v >= 1_000_000:
        return f"{sign}R$ {abs_v / 1_000_000:.2f} mi".replace(".", ",")
    return f"{sign}R$ {abs_v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def css_class_signed(value: float) -> str:
    if value > 0:
        return "pos"
    if value < 0:
        return "neg"
    return "neu"


# ═══════════════════════════════════════════════════════════════
# Leitura XLSX/CSV
# ═══════════════════════════════════════════════════════════════

def col_index(cell_ref: str) -> int:
    m = re.match(r"([A-Z]+)", str(cell_ref or ""))
    if not m:
        return 0
    idx = 0
    for ch in m.group(1):
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out: List[str] = []
    for si in root.findall(f"{{{NS_MAIN}}}si"):
        parts = [t.text or "" for t in si.iter(f"{{{NS_MAIN}}}t")]
        out.append("".join(parts))
    return out


def first_sheet_path(zf: zipfile.ZipFile) -> str:
    rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rels = {
        rel.attrib.get("Id"): rel.attrib.get("Target")
        for rel in rel_root.findall(f"{{{NS_PKG_REL}}}Relationship")
    }
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    sheet = wb_root.find(f".//{{{NS_MAIN}}}sheet")
    if sheet is None:
        raise ValueError("Workbook sem planilhas.")
    rid = sheet.attrib.get(f"{{{NS_REL}}}id")
    target = rels.get(rid)
    if not target:
        raise ValueError("Não foi possível localizar a primeira planilha.")
    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return "xl/" + target


def read_cell_value(cell: ET.Element, shared_strings: List[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{NS_MAIN}}}t"))

    v = cell.find(f"{{{NS_MAIN}}}v")
    if v is None or v.text is None:
        return None
    text = v.text

    if cell_type == "s":
        try:
            return shared_strings[int(text)]
        except Exception:
            return text
    if cell_type == "b":
        return text == "1"
    try:
        value = float(text)
        return int(value) if value.is_integer() else value
    except Exception:
        return text


def read_xlsx_rows(path: str) -> List[List[Any]]:
    rows: List[List[Any]] = []
    with zipfile.ZipFile(path) as zf:
        shared_strings = load_shared_strings(zf)
        sheet_path = first_sheet_path(zf)
        with zf.open(sheet_path) as fh:
            for _, elem in ET.iterparse(fh, events=("end",)):
                if elem.tag != f"{{{NS_MAIN}}}row":
                    continue
                values: Dict[int, Any] = {}
                max_idx = -1
                for cell in elem.findall(f"{{{NS_MAIN}}}c"):
                    idx = col_index(cell.attrib.get("r", ""))
                    values[idx] = read_cell_value(cell, shared_strings)
                    max_idx = max(max_idx, idx)
                if max_idx >= 0:
                    rows.append([values.get(i) for i in range(max_idx + 1)])
                elem.clear()
    return rows


def read_csv_rows(path: str) -> List[List[Any]]:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                return [row for row in csv.reader(fh, dialect)]
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def read_table_rows(path: str) -> List[List[Any]]:
    lower = path.lower()
    if lower.endswith(".xlsx"):
        return read_xlsx_rows(path)
    if lower.endswith(".csv"):
        return read_csv_rows(path)
    raise ValueError("Formato não suportado. Use .xlsx ou .csv.")


def discover_times_file(data_dir: Optional[str]) -> Optional[str]:
    priority_candidates: List[str] = []
    fallback_candidates: List[str] = []
    dirs = []
    if data_dir:
        dirs.append(data_dir)
    downloads = os.path.expanduser("~/Downloads")
    if os.path.isdir(downloads):
        dirs.append(downloads)

    patterns = [
        "*times*bova11*.xlsx", "*bova11*times*.xlsx", "*times*.xlsx", "*bova11*.xlsx",
        "*times*bova11*.csv", "*bova11*times*.csv", "*times*.csv",
    ]
    seen = set()
    for base in dirs:
        for pattern in patterns:
            for path in glob.glob(os.path.join(base, pattern)):
                if path in seen or not os.path.isfile(path):
                    continue
                seen.add(path)
                name = os.path.basename(path).lower()
                if "times" in name:
                    priority_candidates.append(path)
                elif data_dir and os.path.abspath(base) == os.path.abspath(data_dir) and "bova11" in name:
                    fallback_candidates.append(path)

    candidates = priority_candidates or fallback_candidates
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def classify_side(raw: Any) -> Tuple[str, str]:
    label = str(raw or "").strip()
    key = strip_accents(label).lower()
    if key.startswith("compr"):
        return "buy", "Comprador"
    if key.startswith("vend"):
        return "sell", "Vendedor"
    if "leilao" in key:
        return "auction", "Leilão"
    if key.startswith("direto"):
        return "direct", "Direto"
    return "other", label or "N/A"


def load_trades(path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    raw_rows = [row for row in read_table_rows(path) if any(cell not in (None, "") for cell in row)]
    if not raw_rows:
        return [], ["Arquivo sem linhas úteis."]

    headers = [norm_key(cell) for cell in raw_rows[0]]
    required = {"data", "compradora", "valor", "quantidade", "vendedora", "agressor"}
    missing = sorted(required - set(headers))
    if missing:
        return [], [f"Colunas ausentes: {', '.join(missing)}"]

    trades: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for row_idx, row in enumerate(raw_rows[1:], start=2):
        item = {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
        t = parse_time_seconds(item.get("data"))
        price = parse_number(item.get("valor"))
        qty = parse_number(item.get("quantidade"))
        if t is None or price is None or qty is None or price <= 0 or qty <= 0:
            continue

        side, side_label = classify_side(item.get("agressor"))
        buyer = str(item.get("compradora") or "").strip()
        seller = str(item.get("vendedora") or "").strip()
        if side == "buy":
            aggressor_agent = buyer
            passive_agent = seller
        elif side == "sell":
            aggressor_agent = seller
            passive_agent = buyer
        else:
            aggressor_agent = side_label
            passive_agent = ""

        trades.append({
            "row": row_idx,
            "time_s": t,
            "time": fmt_time(t),
            "price": float(price),
            "qty": float(qty),
            "total": float(price) * float(qty),
            "side": side,
            "side_label": side_label,
            "buyer": buyer,
            "seller": seller,
            "aggressor_agent": aggressor_agent,
            "passive_agent": passive_agent,
        })

    if not trades:
        warnings.append("Nenhum negócio válido encontrado.")
    return trades, warnings


# ═══════════════════════════════════════════════════════════════
# Cálculos
# ═══════════════════════════════════════════════════════════════

def add_trade_to_bucket(bucket: Dict[str, Any], trade: Dict[str, Any]) -> None:
    if bucket["open"] is None:
        bucket["open"] = trade["price"]
    bucket["close"] = trade["price"]
    bucket["high"] = max(bucket["high"], trade["price"])
    bucket["low"] = min(bucket["low"], trade["price"])
    bucket["vol"] += trade["qty"]
    bucket["fin"] += trade["total"]
    bucket["trades"] += 1
    if trade["side"] == "buy":
        bucket["buy"] += trade["qty"]
    elif trade["side"] == "sell":
        bucket["sell"] += trade["qty"]
    else:
        bucket["neutral"] += trade["qty"]


def empty_bucket() -> Dict[str, Any]:
    return {
        "open": None, "high": -float("inf"), "low": float("inf"), "close": None,
        "vol": 0.0, "fin": 0.0, "buy": 0.0, "sell": 0.0, "neutral": 0.0, "trades": 0,
    }


def compute_window(trades: List[Dict[str, Any]], start_s: float, end_s: float) -> Dict[str, Any]:
    selected = [t for t in trades if start_s <= t["time_s"] <= end_s]
    if not selected:
        return {"trades": 0, "vol": 0.0, "buy": 0.0, "sell": 0.0, "net": 0.0, "vwap": None}
    vol = sum(t["qty"] for t in selected)
    fin = sum(t["total"] for t in selected)
    buy = sum(t["qty"] for t in selected if t["side"] == "buy")
    sell = sum(t["qty"] for t in selected if t["side"] == "sell")
    return {
        "trades": len(selected),
        "vol": vol,
        "buy": buy,
        "sell": sell,
        "net": buy - sell,
        "vwap": fin / vol if vol > 0 else None,
        "open": selected[0]["price"],
        "close": selected[-1]["price"],
    }


def filter_trades_by_min_qty(trades: List[Dict[str, Any]], min_qty: float) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    raw_trades = float(len(trades))
    raw_volume = float(sum(t["qty"] for t in trades))
    if min_qty <= 0:
        return trades, {
            "min_qty": 0.0,
            "raw_trades": raw_trades,
            "raw_volume": raw_volume,
            "kept_trades": raw_trades,
            "kept_volume": raw_volume,
            "dropped_trades": 0.0,
            "dropped_volume": 0.0,
            "kept_trade_pct": 100.0 if raw_trades > 0 else 0.0,
            "kept_volume_pct": 100.0 if raw_volume > 0 else 0.0,
        }
    kept = [t for t in trades if t["qty"] >= min_qty]
    kept_trades = float(len(kept))
    kept_volume = float(sum(t["qty"] for t in kept))
    return kept, {
        "min_qty": float(min_qty),
        "raw_trades": raw_trades,
        "raw_volume": raw_volume,
        "kept_trades": kept_trades,
        "kept_volume": kept_volume,
        "dropped_trades": raw_trades - kept_trades,
        "dropped_volume": raw_volume - kept_volume,
        "kept_trade_pct": (100.0 * kept_trades / raw_trades) if raw_trades > 0 else 0.0,
        "kept_volume_pct": (100.0 * kept_volume / raw_volume) if raw_volume > 0 else 0.0,
    }


def tape_confidence(summary: Dict[str, Any], top_blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    trades = float(summary.get("trades", 0.0) or 0.0)
    volume = float(summary.get("volume", 0.0) or 0.0)
    top5 = float(sum(t.get("qty", 0.0) for t in top_blocks[:5]))
    top5_share = (100.0 * top5 / volume) if volume > 0 else 0.0

    score = 0
    reasons: List[str] = []
    if trades >= 1000:
        score += 2
    elif trades >= 400:
        score += 1
    else:
        reasons.append("poucos negócios após filtro")
    if volume >= 300000:
        score += 2
    elif volume >= 100000:
        score += 1
    else:
        reasons.append("volume filtrado baixo")
    if top5_share <= 20:
        score += 2
    elif top5_share <= 35:
        score += 1
    else:
        reasons.append("CVD concentrado em poucos blocos")

    if score >= 5:
        level, tone = "ALTA", "pos"
    elif score >= 3:
        level, tone = "MÉDIA", "amber"
    else:
        level, tone = "BAIXA", "neg"
        if not reasons:
            reasons.append("microestrutura ruidosa")
    return {
        "level": level,
        "tone": tone,
        "score": score,
        "top5_share_pct": top5_share,
        "note": " · ".join(reasons) if reasons else "boa dispersão de fluxo e amostra robusta",
    }


def compute_tape(trades: List[Dict[str, Any]], spot: float = 0.0) -> Dict[str, Any]:
    trades = sorted(trades, key=lambda item: (item["time_s"], item["row"]))
    volume = sum(t["qty"] for t in trades)
    financial = sum(t["total"] for t in trades)
    buy_vol = sum(t["qty"] for t in trades if t["side"] == "buy")
    sell_vol = sum(t["qty"] for t in trades if t["side"] == "sell")
    neutral_vol = volume - buy_vol - sell_vol
    vwap = financial / volume if volume > 0 else None

    open_price = trades[0]["price"]
    close_price = trades[-1]["price"]
    high_price = max(t["price"] for t in trades)
    low_price = min(t["price"] for t in trades)
    net_aggr = buy_vol - sell_vol
    cvd = 0.0
    cvd_min = 0.0
    cvd_max = 0.0

    by_min: Dict[int, Dict[str, Any]] = defaultdict(empty_bucket)
    by_price: Dict[float, Dict[str, Any]] = defaultdict(lambda: {
        "vol": 0.0, "fin": 0.0, "buy": 0.0, "sell": 0.0, "neutral": 0.0, "trades": 0,
    })
    aggr_broker: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "buy": 0.0, "sell": 0.0, "vol": 0.0, "fin": 0.0, "trades": 0,
    })
    passive_broker: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "absorb_sell_side": 0.0, "absorb_buy_side": 0.0, "vol": 0.0, "fin": 0.0, "trades": 0,
    })

    for trade in trades:
        minute = int(trade["time_s"] // 60)
        add_trade_to_bucket(by_min[minute], trade)

        price_key = round(trade["price"], 2)
        profile = by_price[price_key]
        profile["vol"] += trade["qty"]
        profile["fin"] += trade["total"]
        profile["trades"] += 1
        if trade["side"] == "buy":
            profile["buy"] += trade["qty"]
            cvd += trade["qty"]
        elif trade["side"] == "sell":
            profile["sell"] += trade["qty"]
            cvd -= trade["qty"]
        else:
            profile["neutral"] += trade["qty"]
        cvd_min = min(cvd_min, cvd)
        cvd_max = max(cvd_max, cvd)

        if trade["side"] in ("buy", "sell") and trade["aggressor_agent"]:
            broker = aggr_broker[trade["aggressor_agent"]]
            broker["vol"] += trade["qty"]
            broker["fin"] += trade["total"]
            broker["trades"] += 1
            broker[trade["side"]] += trade["qty"]

        if trade["side"] in ("buy", "sell") and trade["passive_agent"]:
            broker = passive_broker[trade["passive_agent"]]
            broker["vol"] += trade["qty"]
            broker["fin"] += trade["total"]
            broker["trades"] += 1
            if trade["side"] == "buy":
                broker["absorb_sell_side"] += trade["qty"]
            elif trade["side"] == "sell":
                broker["absorb_buy_side"] += trade["qty"]

    minute_rows = []
    cum_fin = 0.0
    cum_vol = 0.0
    cum_cvd = 0.0
    for minute, bucket in sorted(by_min.items()):
        cum_fin += bucket["fin"]
        cum_vol += bucket["vol"]
        cum_cvd += bucket["buy"] - bucket["sell"]
        minute_rows.append({
            "minute": fmt_minute(minute),
            "close": bucket["close"],
            "vwap": cum_fin / cum_vol if cum_vol > 0 else None,
            "volume": bucket["vol"],
            "buy": bucket["buy"],
            "sell": bucket["sell"],
            "net": bucket["buy"] - bucket["sell"],
            "cvd": cum_cvd,
            "trades": bucket["trades"],
        })

    profile_rows = []
    for price, row in by_price.items():
        profile_rows.append({
            "price": price,
            "volume": row["vol"],
            "buy": row["buy"],
            "sell": row["sell"],
            "net": row["buy"] - row["sell"],
            "trades": row["trades"],
        })
    profile_rows.sort(key=lambda item: item["volume"], reverse=True)
    poc = profile_rows[0] if profile_rows else None

    top_blocks = sorted(trades, key=lambda item: item["qty"], reverse=True)[:20]
    top_aggressors = []
    for name, row in aggr_broker.items():
        top_aggressors.append({
            "name": name,
            "volume": row["vol"],
            "buy": row["buy"],
            "sell": row["sell"],
            "net": row["buy"] - row["sell"],
            "trades": row["trades"],
        })
    top_aggressors.sort(key=lambda item: item["volume"], reverse=True)

    top_passive = []
    for name, row in passive_broker.items():
        top_passive.append({
            "name": name,
            "volume": row["vol"],
            "absorb_sell_side": row["absorb_sell_side"],
            "absorb_buy_side": row["absorb_buy_side"],
            "net_absorption": row["absorb_buy_side"] - row["absorb_sell_side"],
            "trades": row["trades"],
        })
    top_passive.sort(key=lambda item: item["volume"], reverse=True)

    start = trades[0]["time_s"]
    end = trades[-1]["time_s"]
    side_counts = Counter(t["side_label"] for t in trades)

    out = {
        "summary": {
            "trades": len(trades),
            "start_time": trades[0]["time"],
            "end_time": trades[-1]["time"],
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "vwap": vwap,
            "spot": spot if spot > 0 else None,
            "volume": volume,
            "financial": financial,
            "avg_trade_qty": volume / len(trades) if trades else 0.0,
            "buy_vol": buy_vol,
            "sell_vol": sell_vol,
            "neutral_vol": neutral_vol,
            "buy_pct": 100 * buy_vol / volume if volume else 0.0,
            "sell_pct": 100 * sell_vol / volume if volume else 0.0,
            "net_aggr": net_aggr,
            "cvd_final": cvd,
            "cvd_min": cvd_min,
            "cvd_max": cvd_max,
            "ret_oc_pct": 100 * (close_price / open_price - 1) if open_price else 0.0,
            "close_vwap_pct": 100 * (close_price / vwap - 1) if vwap else 0.0,
            "range_pct": 100 * (high_price - low_price) / open_price if open_price else 0.0,
            "poc_price": poc["price"] if poc else None,
            "poc_volume": poc["volume"] if poc else 0.0,
            "side_counts": dict(side_counts),
        },
        "minute_rows": minute_rows,
        "profile_rows": profile_rows,
        "top_blocks": top_blocks,
        "top_aggressors": top_aggressors[:25],
        "top_passive": top_passive[:25],
        "windows": {
            "first15": compute_window(trades, start, start + 15 * 60),
            "last15": compute_window(trades, end - 15 * 60, end),
            "last60": compute_window(trades, end - 60 * 60, end),
        },
    }
    out["summary"]["confidence"] = tape_confidence(out["summary"], top_blocks)
    return out


# ═══════════════════════════════════════════════════════════════
# HTML
# ═══════════════════════════════════════════════════════════════

def table_top_blocks(rows: Iterable[Dict[str, Any]]) -> str:
    out = []
    for trade in rows:
        cls = css_class_signed(1 if trade["side"] == "buy" else -1 if trade["side"] == "sell" else 0)
        out.append(f"""
        <tr>
          <td class="mono">{html.escape(trade["time"])}</td>
          <td class="mono num">{fmt_price(trade["price"])}</td>
          <td class="mono num">{fmt_int(trade["qty"])}</td>
          <td class="mono num">{fmt_brl(trade["total"])}</td>
          <td><span class="pill {cls}">{html.escape(trade["side_label"])}</span></td>
          <td>{html.escape(trade["aggressor_agent"])}</td>
          <td>{html.escape(trade["passive_agent"])}</td>
        </tr>""")
    return "\n".join(out) or '<tr><td colspan="7">Sem blocos.</td></tr>'


def table_aggressors(rows: Iterable[Dict[str, Any]]) -> str:
    out = []
    for row in rows:
        cls = css_class_signed(row["net"])
        out.append(f"""
        <tr>
          <td>{html.escape(row["name"])}</td>
          <td class="mono num">{fmt_int(row["volume"])}</td>
          <td class="mono num pos">{fmt_int(row["buy"])}</td>
          <td class="mono num neg">{fmt_int(row["sell"])}</td>
          <td class="mono num {cls}">{fmt_signed_int(row["net"])}</td>
          <td class="mono num">{fmt_int(row["trades"])}</td>
        </tr>""")
    return "\n".join(out) or '<tr><td colspan="6">Sem corretoras.</td></tr>'


def table_passive(rows: Iterable[Dict[str, Any]]) -> str:
    out = []
    for row in rows:
        cls = css_class_signed(row["net_absorption"])
        out.append(f"""
        <tr>
          <td>{html.escape(row["name"])}</td>
          <td class="mono num">{fmt_int(row["volume"])}</td>
          <td class="mono num neg">{fmt_int(row["absorb_buy_side"])}</td>
          <td class="mono num pos">{fmt_int(row["absorb_sell_side"])}</td>
          <td class="mono num {cls}">{fmt_signed_int(row["net_absorption"])}</td>
          <td class="mono num">{fmt_int(row["trades"])}</td>
        </tr>""")
    return "\n".join(out) or '<tr><td colspan="6">Sem absorção.</td></tr>'


def table_profile(rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(rows)[:15]
    max_vol = max([row["volume"] for row in rows], default=1)
    out = []
    for row in rows:
        width = max(4, int(100 * row["volume"] / max_vol))
        cls = css_class_signed(row["net"])
        out.append(f"""
        <tr>
          <td class="mono">{fmt_price(row["price"])}</td>
          <td class="vol-cell">
            <span class="bar-track"><span class="bar {cls}" style="width:{width}%"></span></span>
            <span class="mono">{fmt_int(row["volume"])}</span>
          </td>
          <td class="mono num {cls}">{fmt_signed_int(row["net"])}</td>
          <td class="mono num">{fmt_int(row["trades"])}</td>
        </tr>""")
    return "\n".join(out) or '<tr><td colspan="4">Sem profile.</td></tr>'


def build_chart_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    minutes = data["minute_rows"]
    profile = sorted(data["profile_rows"][:25], key=lambda item: item["price"])
    summary = data["summary"]
    return {
        "minutes": [row["minute"] for row in minutes],
        "close": [round(row["close"], 4) for row in minutes],
        "vwap": [round(row["vwap"], 4) if row["vwap"] is not None else None for row in minutes],
        "spot": summary.get("spot"),
        "volume": [int(round(row["volume"])) for row in minutes],
        "net": [int(round(row["net"])) for row in minutes],
        "cvd": [int(round(row["cvd"])) for row in minutes],
        "profilePrices": [fmt_price(row["price"]) for row in profile],
        "profileVolume": [int(round(row["volume"])) for row in profile],
        "profileNet": [int(round(row["net"])) for row in profile],
    }


def build_missing_html(output: str, ref_date: str, ref_tag: str, message: str) -> None:
    content = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — Tape Flow</title>
<style>
  :root {{ --bg:#FAFAF8;--card:#FFFFFF;--border:#D8D7D4;--t1:#1A1A18;--t2:#4A4A48;--t3:#8A8A88;--blue:#0969DA; }}
  [data-theme="dark"] {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--t1:#c9d1d9;--t2:#8b949e;--t3:#6e7681;--blue:#58a6ff; }}
  body {{ margin:0; padding:28px; background:var(--bg); color:var(--t1); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .box {{ max-width:760px; margin:8vh auto; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:28px; }}
  h1 {{ margin:0 0 8px; font-size:1.35rem; }}
  p {{ color:var(--t2); line-height:1.55; }}
  code {{ color:var(--blue); }}
  #theme-toggle {{ position:fixed; right:18px; top:14px; border:1px solid var(--border); background:var(--card); color:var(--t1); border-radius:999px; padding:7px 10px; cursor:pointer; }}
</style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()">◐</button>
<div class="box">
  <h1>BOVA11 — Tape Flow</h1>
  <p><strong>Dados indisponíveis para {html.escape(ref_date)} ({html.escape(ref_tag)}).</strong></p>
  <p>{html.escape(message)}</p>
  <p>Coloque o arquivo de times and trades em <code>data/</code> ou passe <code>--times-file</code> no runner/script.</p>
</div>
<script>
(function(){{
  const saved = localStorage.getItem('bova11-theme') || 'light';
  if (saved === 'dark') document.documentElement.setAttribute('data-theme','dark');
}})();
function toggleTheme(){{
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  if (dark) {{
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('bova11-theme','light');
  }} else {{
    document.documentElement.setAttribute('data-theme','dark');
    localStorage.setItem('bova11-theme','dark');
  }}
}}
</script>
</body>
</html>"""
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(content)


def build_html(data: Dict[str, Any], source_file: str, ref_date: str, ref_tag: str) -> str:
    summary = data["summary"]
    chart_js = json.dumps(build_chart_payload(data), ensure_ascii=False)
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    source_name = os.path.basename(source_file)

    net_cls = css_class_signed(summary["net_aggr"])
    ret_cls = css_class_signed(summary["ret_oc_pct"])
    vwap_cls = css_class_signed(summary["close_vwap_pct"])
    bias_label = "Compra agressora" if summary["net_aggr"] > 0 else "Venda agressora" if summary["net_aggr"] < 0 else "Neutro"
    vwap_label = "acima do VWAP" if summary["close_vwap_pct"] > 0 else "abaixo do VWAP" if summary["close_vwap_pct"] < 0 else "no VWAP"

    first15 = data["windows"]["first15"]
    last60 = data["windows"]["last60"]
    filter_meta = data.get("filter_meta", {})
    conf = summary.get("confidence", {"level": "N/A", "tone": "neu", "note": "", "top5_share_pct": 0.0})

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 — Tape Flow</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  :root {{
    --bg:#FAFAF8;--bg2:#F0EFEC;--card:#FFFFFF;--border:#D8D7D4;--border2:#C7C5BF;
    --t1:#1A1A18;--t2:#4A4A48;--t3:#8A8A88;--blue:#0969DA;--green:#1A7F37;
    --red:#CF222E;--amber:#9A6700;--purple:#8250DF;
  }}
  [data-theme="dark"] {{
    --bg:#0d1117;--bg2:#161b22;--card:#161b22;--border:#30363d;--border2:#484f58;
    --t1:#c9d1d9;--t2:#8b949e;--t3:#6e7681;--blue:#58a6ff;--green:#3fb950;
    --red:#f85149;--amber:#e3b341;--purple:#d2a8ff;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--t1);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:14px;
  }}
  .mono {{ font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace; }}
  #theme-toggle {{
    position:fixed; right:18px; top:14px; z-index:20;
    border:1px solid var(--border); background:var(--card); color:var(--t1);
    border-radius:999px; padding:7px 10px; cursor:pointer;
  }}
  .page {{ max-width:1440px; margin:0 auto; padding:28px 20px 36px; }}
  .header {{ margin-bottom:18px; padding-right:56px; }}
  .kicker {{
    font-size:.74rem; color:var(--t3); text-transform:uppercase;
    letter-spacing:.08em; font-weight:700; margin-bottom:8px;
  }}
  h1 {{ margin:0; font-size:1.75rem; letter-spacing:-.03em; }}
  .subtitle {{ margin:8px 0 0; color:var(--t2); line-height:1.45; }}
  .meta {{ margin-top:8px; color:var(--t3); font-size:.82rem; }}

  .cards {{
    display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:12px; margin:18px 0;
  }}
  @media(max-width:1120px) {{ .cards {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} }}
  @media(max-width:620px) {{ .cards {{ grid-template-columns:1fr; }} }}
  .card {{
    background:var(--card); border:1px solid var(--border); border-radius:10px;
    padding:14px 15px; min-height:102px;
  }}
  .label {{
    color:var(--t3); text-transform:uppercase; letter-spacing:.06em;
    font-size:.7rem; font-weight:700; margin-bottom:8px;
  }}
  .value {{ font-size:1.38rem; font-weight:800; line-height:1.05; }}
  .sub {{ color:var(--t3); font-size:.78rem; margin-top:7px; line-height:1.35; }}
  .pos {{ color:var(--green); }}
  .neg {{ color:var(--red); }}
  .neu {{ color:var(--t3); }}
  .amber {{ color:var(--amber); }}
  .purple {{ color:var(--purple); }}

  .insight {{
    display:grid; grid-template-columns:1.2fr 1fr 1fr; gap:12px; margin-bottom:18px;
  }}
  @media(max-width:900px) {{ .insight {{ grid-template-columns:1fr; }} }}
  .note {{
    background:var(--card); border:1px solid var(--border); border-radius:10px;
    padding:14px 16px; color:var(--t2); line-height:1.5;
  }}
  .note strong {{ color:var(--t1); }}
  .guide {{ margin:0 0 18px; }}
  .guide-head {{
    display:flex; justify-content:space-between; align-items:flex-end; gap:16px;
    margin:0 0 10px;
  }}
  .guide-head h2 {{ margin:0; font-size:1.08rem; letter-spacing:-.02em; }}
  .guide-note {{ color:var(--t3); font-size:.82rem; line-height:1.4; max-width:420px; }}
  .guide-grid {{
    display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px;
  }}
  @media(max-width:1120px) {{ .guide-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  @media(max-width:720px) {{
    .guide-head {{ display:block; }}
    .guide-note {{ margin-top:8px; max-width:none; }}
    .guide-grid {{ grid-template-columns:1fr; }}
  }}
  .guide-card {{
    background:var(--card); border:1px solid var(--border); border-radius:10px;
    padding:14px 16px; min-width:0;
  }}
  .guide-card h3 {{ margin:0 0 9px; font-size:.9rem; color:var(--t1); }}
  .guide-card ul {{ margin:0; padding-left:18px; color:var(--t2); line-height:1.45; }}
  .guide-card li {{ margin:6px 0; }}
  .guide-card strong {{ color:var(--t1); }}
  .guide-warning {{
    margin-top:10px; padding:11px 13px; border:1px solid var(--border);
    border-radius:10px; color:var(--t2); background:var(--bg2); line-height:1.45;
  }}
  .guide-warning strong {{ color:var(--t1); }}

  .chart-grid {{
    display:grid; grid-template-columns:1.35fr 1fr; gap:16px; margin-bottom:18px;
  }}
  @media(max-width:1020px) {{ .chart-grid {{ grid-template-columns:1fr; }} }}
  .panel {{
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    overflow:hidden;
  }}
  .panel-head {{
    padding:12px 14px; border-bottom:1px solid var(--border);
    background:var(--bg2); font-weight:700; display:flex; justify-content:space-between; gap:10px;
  }}
  .panel-sub {{ color:var(--t3); font-size:.78rem; font-weight:500; }}
  .chart-wrap {{ height:320px; padding:14px; position:relative; }}
  .chart-wrap.short {{ height:260px; }}

  .tables {{
    display:grid; grid-template-columns:1.1fr .9fr; gap:16px; margin-top:18px;
  }}
  @media(max-width:1020px) {{ .tables {{ grid-template-columns:1fr; }} }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{
    text-align:left; color:var(--t3); background:var(--bg2); border-bottom:1px solid var(--border);
    padding:8px 10px; font-size:.72rem; text-transform:uppercase; letter-spacing:.04em;
  }}
  td {{ padding:8px 10px; border-bottom:1px solid var(--border); color:var(--t1); vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  .num {{ text-align:right; white-space:nowrap; }}
  .table-scroll {{ max-height:470px; overflow:auto; }}
  .pill {{
    display:inline-flex; align-items:center; justify-content:center;
    border:1px solid currentColor; border-radius:999px; padding:2px 8px;
    font-size:.74rem; font-weight:700; white-space:nowrap;
  }}
  .bar-track {{
    display:inline-flex; width:92px; height:7px; background:var(--bg2);
    border-radius:999px; overflow:hidden; margin-right:8px; vertical-align:middle;
  }}
  .bar {{ display:block; height:100%; border-radius:999px; background:var(--t3); }}
  .bar.pos {{ background:var(--green); }}
  .bar.neg {{ background:var(--red); }}
  .bar.neu {{ background:var(--t3); }}
  .footer {{
    margin-top:18px; padding-top:14px; border-top:1px solid var(--border);
    color:var(--t3); font-size:.78rem; text-align:center;
  }}
</style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()">◐</button>
<main class="page">
  <header class="header">
    <div class="kicker">Times & Trades · Tape Flow</div>
    <h1>BOVA11 Tape Flow</h1>
    <p class="subtitle">VWAP, CVD, agressão por lado, volume profile, blocos e corretoras do fluxo intraday.</p>
    <div class="meta mono">Data: {html.escape(ref_date)} · Tag: {html.escape(ref_tag)} · Fonte: {html.escape(source_name)} · Janela: {html.escape(summary["start_time"])}–{html.escape(summary["end_time"])} · Filtro qtd ≥ {fmt_int(filter_meta.get("min_qty", 0.0))}</div>
  </header>

  <section class="cards">
    <div class="card">
      <div class="label">Close</div>
      <div class="value mono {ret_cls}">{fmt_price(summary["close"])}</div>
      <div class="sub">O/C {fmt_pct(summary["ret_oc_pct"])} · H/L {fmt_price(summary["high"])}/{fmt_price(summary["low"])}</div>
    </div>
    <div class="card">
      <div class="label">VWAP</div>
      <div class="value mono {vwap_cls}">{fmt_price(summary["vwap"], 4)}</div>
      <div class="sub">Close {vwap_label} ({fmt_pct(summary["close_vwap_pct"])})</div>
    </div>
    <div class="card">
      <div class="label">Volume</div>
      <div class="value mono">{fmt_int(summary["volume"])}</div>
      <div class="sub">{fmt_int(summary["trades"])} trades · médio {fmt_int(summary["avg_trade_qty"])}</div>
    </div>
    <div class="card">
      <div class="label">Financeiro</div>
      <div class="value mono">{fmt_brl(summary["financial"])}</div>
      <div class="sub">Total calculado por preço × quantidade</div>
    </div>
    <div class="card">
      <div class="label">CVD</div>
      <div class="value mono {net_cls}">{fmt_signed_int(summary["cvd_final"])}</div>
      <div class="sub">mín {fmt_signed_int(summary["cvd_min"])} · máx {fmt_signed_int(summary["cvd_max"])}</div>
    </div>
    <div class="card">
      <div class="label">POC</div>
      <div class="value mono purple">{fmt_price(summary["poc_price"])}</div>
      <div class="sub">Volume no preço: {fmt_int(summary["poc_volume"])}</div>
    </div>
  </section>

  <section class="cards" style="margin-top:-2px;">
    <div class="card">
      <div class="label">Qualidade do Tape</div>
      <div class="value mono {conf.get("tone","neu")}">{html.escape(conf.get("level","N/A"))}</div>
      <div class="sub">{html.escape(conf.get("note",""))}</div>
    </div>
    <div class="card">
      <div class="label">Retenção do Filtro</div>
      <div class="value mono">{fmt_pct(filter_meta.get("kept_trade_pct", 0.0))}</div>
      <div class="sub">trades {fmt_int(filter_meta.get("kept_trades", 0.0))}/{fmt_int(filter_meta.get("raw_trades", 0.0))} · volume {fmt_pct(filter_meta.get("kept_volume_pct", 0.0))}</div>
    </div>
    <div class="card">
      <div class="label">Concentração Top-5</div>
      <div class="value mono">{fmt_pct(conf.get("top5_share_pct", 0.0))}</div>
      <div class="sub">participação dos 5 maiores blocos no volume</div>
    </div>
  </section>

  <section class="insight">
    <div class="note">
      <strong>Leitura:</strong> {html.escape(bias_label)} no agregado, com net {fmt_signed_int(summary["net_aggr"])}
      ({fmt_pct(summary["buy_pct"])} compra agressora vs {fmt_pct(summary["sell_pct"])} venda agressora).
      Close terminou {html.escape(vwap_label)}.
    </div>
    <div class="note">
      <strong>Primeiros 15 min:</strong> net {fmt_signed_int(first15["net"])} · volume {fmt_int(first15["vol"])} · VWAP {fmt_price(first15["vwap"], 4)}.
    </div>
    <div class="note">
      <strong>Últimos 60 min:</strong> net {fmt_signed_int(last60["net"])} · volume {fmt_int(last60["vol"])} · VWAP {fmt_price(last60["vwap"], 4)}.
    </div>
  </section>

  <section class="guide" aria-label="Como interpretar Tape Flow">
    <div class="guide-head">
      <div>
        <div class="label">Como ler o Tape Flow</div>
        <h2>Leitura prática dos dados</h2>
      </div>
      <div class="guide-note">Use como leitura de microfluxo intraday: mostra agressão, aceitação de preço e absorção. Não é sinal isolado.</div>
    </div>
    <div class="guide-grid">
      <article class="guide-card">
        <h3>Métricas principais</h3>
        <ul>
          <li><strong>VWAP:</strong> preço médio ponderado por volume. Close acima dele indica sustentação compradora; abaixo indica fragilidade.</li>
          <li><strong>CVD/Net:</strong> compra agressora menos venda agressora. Positivo favorece compra; negativo favorece venda.</li>
          <li><strong>POC:</strong> preço com maior volume. É zona de aceitação e pode virar suporte, resistência ou magneto.</li>
        </ul>
      </article>
      <article class="guide-card">
        <h3>Sinal comprador</h3>
        <ul>
          <li>Preço fecha acima do VWAP.</li>
          <li>CVD positivo e subindo junto com o preço.</li>
          <li>Últimos 60 min com net comprador.</li>
          <li>Venda agressora entra, mas preço não cai: possível comprador passivo absorvendo.</li>
        </ul>
      </article>
      <article class="guide-card">
        <h3>Sinal vendedor</h3>
        <ul>
          <li>Preço fecha abaixo do VWAP.</li>
          <li>CVD negativo e caindo junto com o preço.</li>
          <li>Últimos 60 min com net vendedor.</li>
          <li>Compra agressora entra, mas preço não sobe: possível vendedor passivo absorvendo.</li>
        </ul>
      </article>
      <article class="guide-card">
        <h3>Corretoras e blocos</h3>
        <ul>
          <li><strong>Agressoras:</strong> mostram quem iniciou compra ou venda; net positivo compra, net negativo venda.</li>
          <li><strong>Passivas:</strong> mostram quem ficou na ponta oposta. Net abs. positivo sugere suporte; negativo sugere resistência.</li>
          <li><strong>Blocos:</strong> observe preço, lado, financeiro e se o preço aceitou ou rejeitou aquela região depois.</li>
        </ul>
      </article>
    </div>
    <div class="guide-warning"><strong>Cuidado:</strong> o módulo confia na coluna Agressor do Times & Trades. Ele não reconstrói o book nem revela o player final por trás da corretora. Combine com VWAP, CVD, profile, opções e contexto do índice.</div>
  </section>

  <section class="chart-grid">
    <div class="panel">
      <div class="panel-head">Preço x VWAP <span class="panel-sub">linha de spot manual se fornecida</span></div>
      <div class="chart-wrap"><canvas id="priceChart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-head">CVD <span class="panel-sub">volume comprador - vendedor</span></div>
      <div class="chart-wrap"><canvas id="cvdChart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-head">Volume por minuto <span class="panel-sub">cor por saldo agressor</span></div>
      <div class="chart-wrap short"><canvas id="volumeChart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-head">Volume Profile <span class="panel-sub">top preços por volume</span></div>
      <div class="chart-wrap short"><canvas id="profileChart"></canvas></div>
    </div>
  </section>

  <section class="tables">
    <div class="panel">
      <div class="panel-head">Maiores blocos <span class="panel-sub">por quantidade</span></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Hora</th><th class="num">Preço</th><th class="num">Qtd</th><th class="num">Financeiro</th><th>Lado</th><th>Agressor</th><th>Passivo</th></tr></thead>
          <tbody>{table_top_blocks(data["top_blocks"])}</tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">Volume Profile <span class="panel-sub">top 15</span></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Preço</th><th>Volume</th><th class="num">Net</th><th class="num">Trades</th></tr></thead>
          <tbody>{table_profile(data["profile_rows"])}</tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">Corretoras agressoras <span class="panel-sub">net = compra - venda</span></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Corretora</th><th class="num">Volume</th><th class="num">Compra</th><th class="num">Venda</th><th class="num">Net</th><th class="num">Trades</th></tr></thead>
          <tbody>{table_aggressors(data["top_aggressors"][:15])}</tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">Corretoras passivas <span class="panel-sub">absorção na ponta oposta</span></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Corretora</th><th class="num">Volume</th><th class="num">Abs. venda</th><th class="num">Abs. compra</th><th class="num">Net abs.</th><th class="num">Trades</th></tr></thead>
          <tbody>{table_passive(data["top_passive"][:15])}</tbody>
        </table>
      </div>
    </div>
  </section>

  <div class="footer">BOVA11 Tape Flow · Módulo 21 · Gerado em {generated_at}</div>
</main>

<script>
const TAPE = {chart_js};
let charts = [];

function isDark() {{
  return document.documentElement.getAttribute('data-theme') === 'dark';
}}
function textColor() {{ return isDark() ? '#8b949e' : '#4A4A48'; }}
function gridColor() {{ return isDark() ? 'rgba(139,148,158,.22)' : 'rgba(74,74,72,.18)'; }}
function commonOptions(extra) {{
  return Object.assign({{
    responsive:true,
    maintainAspectRatio:false,
    interaction:{{ mode:'index', intersect:false }},
    plugins:{{ legend:{{ labels:{{ color:textColor(), boxWidth:12 }} }} }},
    scales:{{
      x:{{ ticks:{{ color:textColor(), maxTicksLimit:10 }}, grid:{{ color:gridColor() }} }},
      y:{{ ticks:{{ color:textColor() }}, grid:{{ color:gridColor() }} }}
    }}
  }}, extra || {{}});
}}
function destroyCharts() {{
  charts.forEach(chart => chart.destroy());
  charts = [];
}}
function buildCharts() {{
  destroyCharts();
  const spotLine = TAPE.spot ? [{{
    label:'Spot manual',
    data:TAPE.minutes.map(() => TAPE.spot),
    borderColor:'#8250DF',
    borderWidth:1,
    borderDash:[5,5],
    pointRadius:0,
    tension:0
  }}] : [];

  charts.push(new Chart(document.getElementById('priceChart'), {{
    type:'line',
    data:{{
      labels:TAPE.minutes,
      datasets:[
        {{ label:'Preço', data:TAPE.close, borderColor:'#0969DA', backgroundColor:'rgba(9,105,218,.10)', borderWidth:2, pointRadius:0, tension:.22 }},
        {{ label:'VWAP', data:TAPE.vwap, borderColor:'#9A6700', borderWidth:2, pointRadius:0, tension:.18 }},
        ...spotLine
      ]
    }},
    options:commonOptions({{ scales:{{ x:{{ ticks:{{ color:textColor(), maxTicksLimit:10 }}, grid:{{ color:gridColor() }} }}, y:{{ ticks:{{ color:textColor() }}, grid:{{ color:gridColor() }} }} }} }})
  }}));

  charts.push(new Chart(document.getElementById('cvdChart'), {{
    type:'line',
    data:{{
      labels:TAPE.minutes,
      datasets:[{{ label:'CVD', data:TAPE.cvd, borderColor:'#CF222E', backgroundColor:'rgba(207,34,46,.10)', fill:true, borderWidth:2, pointRadius:0, tension:.2 }}]
    }},
    options:commonOptions()
  }}));

  charts.push(new Chart(document.getElementById('volumeChart'), {{
    type:'bar',
    data:{{
      labels:TAPE.minutes,
      datasets:[{{
        label:'Volume',
        data:TAPE.volume,
        backgroundColor:TAPE.net.map(v => v >= 0 ? 'rgba(26,127,55,.72)' : 'rgba(207,34,46,.72)'),
        borderColor:TAPE.net.map(v => v >= 0 ? '#1A7F37' : '#CF222E'),
        borderWidth:1
      }}]
    }},
    options:commonOptions({{ plugins:{{ legend:{{ display:false }} }} }})
  }}));

  charts.push(new Chart(document.getElementById('profileChart'), {{
    type:'bar',
    data:{{
      labels:TAPE.profilePrices,
      datasets:[{{
        label:'Volume',
        data:TAPE.profileVolume,
        backgroundColor:TAPE.profileNet.map(v => v >= 0 ? 'rgba(26,127,55,.72)' : 'rgba(207,34,46,.72)'),
        borderColor:TAPE.profileNet.map(v => v >= 0 ? '#1A7F37' : '#CF222E'),
        borderWidth:1
      }}]
    }},
    options:commonOptions({{
      indexAxis:'y',
      plugins:{{ legend:{{ display:false }} }},
      scales:{{
        x:{{ ticks:{{ color:textColor() }}, grid:{{ color:gridColor() }} }},
        y:{{ ticks:{{ color:textColor() }}, grid:{{ color:gridColor() }} }}
      }}
    }})
  }}));
}}
function toggleTheme() {{
  const dark = isDark();
  if (dark) {{
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('bova11-theme','light');
  }} else {{
    document.documentElement.setAttribute('data-theme','dark');
    localStorage.setItem('bova11-theme','dark');
  }}
  buildCharts();
}}
(function(){{
  const saved = localStorage.getItem('bova11-theme') || 'light';
  if (saved === 'dark') document.documentElement.setAttribute('data-theme','dark');
  buildCharts();
}})();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="BOVA11 Tape Flow — Times & Trades")
    parser.add_argument("--times-file", default="", help="Arquivo .xlsx/.csv de times and trades")
    parser.add_argument("--data-dir", default="", help="Diretório usado para auto-discovery de times and trades")
    parser.add_argument("--output", required=True, help="HTML de saída")
    parser.add_argument("--ref-date", required=True, help="Data ISO dos dados, ex: 2026-04-22")
    parser.add_argument("--ref-tag", default="", help="Tag do run, ex: 22abr")
    parser.add_argument("--spot", type=float, default=0.0, help="Spot manual do BOVA11")
    parser.add_argument("--min-qty", type=float, default=1000.0, help="Filtro mínimo de quantidade por negócio")
    args = parser.parse_args()

    times_file = args.times_file.strip() or discover_times_file(args.data_dir)
    if not times_file:
        msg = "Nenhum arquivo de times and trades encontrado."
        build_missing_html(args.output, args.ref_date, args.ref_tag, msg)
        print(f"⚠️ {msg} HTML placeholder gerado em {args.output}")
        return 0

    if not os.path.exists(times_file):
        msg = f"Arquivo não encontrado: {times_file}"
        build_missing_html(args.output, args.ref_date, args.ref_tag, msg)
        print(f"⚠️ {msg} HTML placeholder gerado em {args.output}")
        return 0

    trades_raw, warnings = load_trades(times_file)
    if not trades_raw:
        msg = "; ".join(warnings) or "Arquivo sem negócios válidos."
        build_missing_html(args.output, args.ref_date, args.ref_tag, msg)
        print(f"⚠️ {msg} HTML placeholder gerado em {args.output}")
        return 0

    trades, filter_meta = filter_trades_by_min_qty(trades_raw, args.min_qty)
    if not trades:
        msg = f"Nenhum negócio restante após filtro de quantidade mínima ({fmt_int(args.min_qty)})."
        build_missing_html(args.output, args.ref_date, args.ref_tag, msg)
        print(f"⚠️ {msg} HTML placeholder gerado em {args.output}")
        return 0

    data = compute_tape(trades, spot=args.spot)
    data["filter_meta"] = filter_meta
    html_doc = build_html(data, times_file, args.ref_date, args.ref_tag)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html_doc)

    summary = data["summary"]
    print(f"✅ Tape Flow gerado: {args.output}")
    print(f"   Fonte: {times_file}")
    print(f"   Filtro: qtd >= {fmt_int(filter_meta['min_qty'])} | retidos {fmt_int(filter_meta['kept_trades'])}/{fmt_int(filter_meta['raw_trades'])} trades ({fmt_pct(filter_meta['kept_trade_pct'])})")
    print(f"   Trades: {fmt_int(summary['trades'])} | Volume: {fmt_int(summary['volume'])} | VWAP: {fmt_price(summary['vwap'], 4)}")
    print(f"   CVD: {fmt_signed_int(summary['cvd_final'])} | Close: {fmt_price(summary['close'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
