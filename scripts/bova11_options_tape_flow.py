#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 — Option Tape Flow por Strike
====================================
Lê workbooks de Times & Trades de opções com abas por ticker e gera um painel
de agressão intraday por strike.

Arquivos esperados:
  CALLS VENC <vencimento>.xlsx
  PUTS VENC <vencimento>.xlsx

Dependências: Python 3 stdlib apenas.
"""

from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bova11_tape_flow import (
    NS_MAIN,
    NS_PKG_REL,
    NS_REL,
    classify_side,
    col_index,
    fmt_brl,
    fmt_int,
    fmt_price,
    fmt_pct,
    fmt_signed_int,
    load_shared_strings,
    norm_key,
    parse_number,
    parse_time_seconds,
    read_cell_value,
    strip_accents,
)
from bova11_shared import load_json, parse_br_number, save_json


TICKER_RE = re.compile(r"^BOVA[A-Z](?P<strike>\d+)(?:W[1-5])?$", re.IGNORECASE)
DEFAULT_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "history", "bova11_options_tape_flow.json")
MONTH_TO_TAG = {
    "01": "jan", "02": "fev", "03": "mar", "04": "abr",
    "05": "mai", "06": "jun", "07": "jul", "08": "ago",
    "09": "set", "10": "out", "11": "nov", "12": "dez",
}
TAG_TO_MONTH = {v: k for k, v in MONTH_TO_TAG.items()}


def _safe_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _normalize_expiry(value: str) -> str:
    text = strip_accents(value or "").lower()
    text = re.sub(r"(\d{1,2})(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)", r"\1 \2", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"\s+", " ", text)


def _display_expiry(value: str) -> str:
    meses = {
        "jan": "Jan", "fev": "Fev", "mar": "Mar", "abr": "Abr",
        "mai": "Mai", "jun": "Jun", "jul": "Jul", "ago": "Ago",
        "set": "Set", "out": "Out", "nov": "Nov", "dez": "Dez",
    }
    norm = _normalize_expiry(value)
    out = []
    for part in norm.split():
        if part in meses:
            out.append(meses[part])
        elif part.startswith("w") and part[1:].isdigit():
            out.append(part.upper())
        else:
            out.append(part.capitalize())
    return " ".join(out) or value


def _expiry_sort_key(label: str) -> Tuple[int, int, str]:
    meses = {
        "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
        "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
    }
    norm = _normalize_expiry(label)
    m = re.search(r"(\d{1,2})\s+([a-z]{3})", norm)
    if not m:
        return (99, 99, norm)
    return (meses.get(m.group(2), 99), int(m.group(1)), norm)


def _iso_to_br_date(value: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(value or "").strip())
    if not m:
        return ""
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"


def _br_to_iso_date(value: str) -> str:
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", str(value or "").strip())
    if not m:
        return ""
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def _date_sort_key(value: str) -> str:
    return _br_to_iso_date(value) or str(value)


def _br_to_tag(value: str) -> str:
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", str(value or "").strip())
    if not m:
        return ""
    day = str(int(m.group(1)))
    month = MONTH_TO_TAG.get(m.group(2), "")
    if not month:
        return ""
    return f"{day}{month}"


def _to_num(raw: Any) -> float:
    value = parse_br_number(raw, none_on_blank=True)
    return float(value) if value is not None else 0.0


def _extract_year_from_br_date(value: str) -> Optional[int]:
    m = re.match(r"^\d{2}/\d{2}/(\d{4})$", str(value or "").strip())
    if not m:
        return None
    return int(m.group(1))


def infer_workbook_date_br(path: str, preferred_year: Optional[int] = None) -> str:
    stem = Path(path).stem.strip()
    m = re.match(r"^(?:calls|puts)\s+(\d{1,2})([a-z]{3})\s+venc\s+.+$", stem, re.IGNORECASE)
    if not m:
        return ""
    day = int(m.group(1))
    mon_tag = strip_accents(m.group(2)).lower()
    month = TAG_TO_MONTH.get(mon_tag)
    if not month:
        return ""
    year = preferred_year
    if year is None:
        try:
            year = datetime.fromtimestamp(os.path.getmtime(path)).year
        except OSError:
            year = datetime.now().year
    return f"{day:02d}/{month}/{year}"


def _mid_price(bid: float, ask: float) -> Optional[float]:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return None


def discover_volume_csv_map(data_dir: str, tag: str) -> Dict[str, str]:
    if not data_dir or not tag:
        return {}
    out: Dict[str, str] = {}
    for path in glob.glob(os.path.join(data_dir, "venc * fechamento (*.csv")):
        stem = Path(path).stem.strip()
        m = re.match(r"^venc\s+(.+?)\s+fechamento\s+\(([^)]+)\)$", stem, re.IGNORECASE)
        if not m:
            continue
        label_raw = m.group(1).strip()
        paren_raw = strip_accents(m.group(2).strip()).lower()
        if "volume" not in paren_raw:
            continue
        paren_norm = re.sub(r"\s+", " ", paren_raw).strip()
        if paren_norm != f"{tag.lower()} volume":
            continue
        out[_normalize_expiry(label_raw)] = path
    return out


def discover_close_csv_map(data_dir: str, tag: str) -> Dict[str, str]:
    if not data_dir or not tag:
        return {}
    out: Dict[str, str] = {}
    for path in glob.glob(os.path.join(data_dir, "venc * fechamento (*.csv")):
        stem = Path(path).stem.strip()
        m = re.match(r"^venc\s+(.+?)\s+fechamento\s+\(([^)]+)\)$", stem, re.IGNORECASE)
        if not m:
            continue
        label_raw = m.group(1).strip()
        paren_raw = strip_accents(m.group(2).strip()).lower()
        if "volume" in paren_raw:
            continue
        paren_norm = re.sub(r"\s+", " ", paren_raw).strip()
        if paren_norm != tag.lower():
            continue
        out[_normalize_expiry(label_raw)] = path
    return out


def parse_close_delta_csv(path: str) -> Dict[float, Dict[str, Optional[float]]]:
    by_strike: Dict[float, Dict[str, Optional[float]]] = {}
    if not path or not os.path.exists(path):
        return by_strike
    with open(path, "r", encoding="latin-1", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        header = next(reader, None)
        if not header:
            return by_strike
        for parts in reader:
            if len(parts) < 12:
                continue
            strike = _to_num(parts[11] if len(parts) > 11 else "")
            if strike <= 0:
                continue
            call_delta = parse_br_number(parts[3], none_on_blank=True) if len(parts) > 3 else None
            put_delta = parse_br_number(parts[19], none_on_blank=True) if len(parts) > 19 else None
            row = by_strike.setdefault(strike, {"call_delta": None, "put_delta": None})
            if call_delta is not None:
                row["call_delta"] = float(call_delta)
            if put_delta is not None:
                row["put_delta"] = float(put_delta)
    return by_strike


def parse_oi_volume_csv(path: str) -> Tuple[Dict[float, Dict[str, float]], Dict[str, float]]:
    rows_by_strike: Dict[float, Dict[str, float]] = {}
    totals = {
        "call_oi": 0.0,
        "put_oi": 0.0,
        "total_oi": 0.0,
        "call_oi_value": 0.0,
        "put_oi_value": 0.0,
        "total_oi_value": 0.0,
    }
    if not path or not os.path.exists(path):
        return rows_by_strike, totals

    with open(path, "r", encoding="latin-1", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        header = next(reader, None)
        if not header:
            return rows_by_strike, totals
        for parts in reader:
            if len(parts) < 10:
                continue
            strike = _to_num(parts[5] if len(parts) > 5 else "")
            if strike <= 0:
                continue
            call_oi = _to_num(parts[2] if len(parts) > 2 else "")
            put_oi = _to_num(parts[8] if len(parts) > 8 else "")
            call_mid = _mid_price(
                _to_num(parts[3] if len(parts) > 3 else ""),
                _to_num(parts[4] if len(parts) > 4 else ""),
            )
            put_mid = _mid_price(
                _to_num(parts[6] if len(parts) > 6 else ""),
                _to_num(parts[7] if len(parts) > 7 else ""),
            )

            row = rows_by_strike.setdefault(strike, {
                "call_oi": 0.0,
                "put_oi": 0.0,
                "total_oi": 0.0,
                "call_oi_value": 0.0,
                "put_oi_value": 0.0,
                "total_oi_value": 0.0,
            })
            row["call_oi"] += call_oi
            row["put_oi"] += put_oi
            row["total_oi"] += call_oi + put_oi
            if call_mid is not None:
                row["call_oi_value"] += call_oi * call_mid
            if put_mid is not None:
                row["put_oi_value"] += put_oi * put_mid
            row["total_oi_value"] = row["call_oi_value"] + row["put_oi_value"]

    for row in rows_by_strike.values():
        totals["call_oi"] += row["call_oi"]
        totals["put_oi"] += row["put_oi"]
        totals["total_oi"] += row["total_oi"]
        totals["call_oi_value"] += row["call_oi_value"]
        totals["put_oi_value"] += row["put_oi_value"]
        totals["total_oi_value"] += row["total_oi_value"]

    return rows_by_strike, totals


def discover_option_files(options_dir: str) -> List[Dict[str, Any]]:
    pairs: Dict[str, Dict[str, Any]] = {}
    for path in glob.glob(os.path.join(options_dir, "*.xlsx")):
        stem = Path(path).stem.strip()
        match = re.match(r"^(calls|puts)\s+(?:\d{1,2}[a-z]{3}\s+)?venc\s+(.+)$", stem, re.IGNORECASE)
        if not match:
            continue
        side = "call" if match.group(1).lower() == "calls" else "put"
        raw_label = match.group(2).strip()
        key = _normalize_expiry(raw_label)
        item = pairs.setdefault(key, {
            "key": key,
            "label": _display_expiry(raw_label),
            "call_file": "",
            "put_file": "",
            "call_files": [],
            "put_files": [],
        })
        item[f"{side}_files"].append(path)

    for item in pairs.values():
        for side in ("call", "put"):
            files = sorted(
                set(item.get(f"{side}_files", [])),
                key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0,
                reverse=True,
            )
            item[f"{side}_files"] = files
            item[f"{side}_file"] = files[0] if files else ""

    return sorted(pairs.values(), key=lambda item: _expiry_sort_key(item["label"]))


def discover_option_workbook_paths(options_dir: str) -> List[str]:
    paths = []
    for pair in discover_option_files(options_dir):
        for key in ("call_files", "put_files"):
            for path in pair.get(key, []):
                if path:
                    paths.append(path)
    return sorted(set(paths))


def workbook_trade_dates(path: str, preferred_year: Optional[int] = None) -> List[str]:
    dates = set()
    if not path or not os.path.exists(path):
        return []
    inferred_date_br = infer_workbook_date_br(path, preferred_year=preferred_year)
    with zipfile.ZipFile(path) as zf:
        shared_strings = load_shared_strings(zf)
        for sheet_name, sheet_path in workbook_sheets(zf):
            if not TICKER_RE.match(sheet_name.strip()):
                continue
            rows = read_sheet_rows(zf, sheet_path, shared_strings)
            dates.update(collect_trade_dates(rows, fallback_date_br=inferred_date_br))
    return sorted(dates, key=_date_sort_key)


def pick_workbook_for_date(
    paths: List[str],
    target_date_br: str,
    date_cache: Optional[Dict[Tuple[str, int], List[str]]] = None,
) -> str:
    if not paths:
        return ""
    if not target_date_br:
        return paths[0]
    target_year = _extract_year_from_br_date(target_date_br) or datetime.now().year
    cache = date_cache if date_cache is not None else {}
    for path in paths:
        cache_key = (path, target_year)
        known_dates = cache.get(cache_key)
        if known_dates is None:
            known_dates = workbook_trade_dates(path, preferred_year=target_year)
            cache[cache_key] = known_dates
        if target_date_br in known_dates:
            return path
    return paths[0]


def workbook_sheets(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rels = {
        rel.attrib.get("Id"): rel.attrib.get("Target")
        for rel in rel_root.findall(f"{{{NS_PKG_REL}}}Relationship")
    }
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    sheets = []
    for sheet in wb_root.findall(f".//{{{NS_MAIN}}}sheet"):
        name = sheet.attrib.get("name", "")
        rid = sheet.attrib.get(f"{{{NS_REL}}}id")
        target = rels.get(rid)
        if not target:
            continue
        if target.startswith("/"):
            sheet_path = target.lstrip("/")
        elif target.startswith("xl/"):
            sheet_path = target
        else:
            sheet_path = "xl/" + target
        sheets.append((name, sheet_path))
    return sheets


def read_sheet_rows(
    zf: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: List[str],
) -> List[List[Any]]:
    rows: List[List[Any]] = []
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


def parse_trade_rows(
    rows: List[List[Any]],
    target_date_br: str = "",
    fallback_date_br: str = "",
    min_qty: float = 0.0,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    headers = [norm_key(cell) for cell in rows[0]]
    required = {"data", "valor", "quantidade", "agressor"}
    if not required.issubset(set(headers)):
        return []

    def get(item: Dict[str, Any], key: str) -> Any:
        return item.get(key)

    trades: List[Dict[str, Any]] = []
    for row_idx, row in enumerate(rows[1:], start=2):
        item = {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
        raw_date = get(item, "data")
        raw_date_text = str(raw_date or "").strip() if raw_date is not None else ""
        row_date_token = raw_date_text.split()[0] if raw_date_text else ""
        row_date = row_date_token
        if not re.match(r"^\d{2}/\d{2}/\d{4}$", row_date) and fallback_date_br and re.match(
            r"^\d{2}:\d{2}:\d{2}(?:[.,]\d+)?$",
            row_date_token,
        ):
            row_date = fallback_date_br
        if target_date_br and row_date != target_date_br:
            continue
        t = parse_time_seconds(raw_date)
        price = parse_number(get(item, "valor"))
        qty = parse_number(get(item, "quantidade"))
        if t is None or price is None or qty is None or price <= 0 or qty <= 0:
            continue
        if min_qty > 0 and float(qty) < min_qty:
            continue
        side, side_label = classify_side(get(item, "agressor"))
        trades.append({
            "row": row_idx,
            "time_s": t,
            "price": float(price),
            "qty": float(qty),
            "side": side,
            "side_label": side_label,
        })
    return trades


def collect_trade_dates(rows: List[List[Any]], fallback_date_br: str = "") -> List[str]:
    if not rows:
        return []
    headers = [norm_key(cell) for cell in rows[0]]
    if "data" not in headers:
        return []
    data_idx = headers.index("data")
    dates = set()
    for row in rows[1:]:
        raw = row[data_idx] if data_idx < len(row) else None
        raw_date_text = str(raw or "").strip() if raw is not None else ""
        date_token = raw_date_text.split()[0] if raw_date_text else ""
        date_br = date_token
        if not re.match(r"^\d{2}/\d{2}/\d{4}$", date_br) and fallback_date_br and re.match(
            r"^\d{2}:\d{2}:\d{2}(?:[.,]\d+)?$",
            date_token,
        ):
            date_br = fallback_date_br
        if re.match(r"^\d{2}/\d{2}/\d{4}$", date_br):
            dates.add(date_br)
    return sorted(dates, key=_date_sort_key)


def summarize_trades(
    trades: List[Dict[str, Any]],
    ticker: str,
    strike: float,
    side_name: str,
    option_delta: Optional[float] = None,
) -> Dict[str, Any]:
    trades = sorted(trades, key=lambda item: (item["time_s"], item["row"]))
    if not trades:
        return {
            "ticker": ticker,
            "strike": strike,
            "side": side_name,
            "trades": 0,
            "volume": 0.0,
            "financial": 0.0,
            "vwap": None,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "buy": 0.0,
            "sell": 0.0,
            "neutral": 0.0,
            "cvd": 0.0,
            "buy_pct": 0.0,
            "sell_pct": 0.0,
            "close_vwap_pct": None,
            "avg_trade_qty": 0.0,
            "option_delta": option_delta,
            "delta_volume": None,
            "delta_volume_buy": None,
            "delta_volume_sell": None,
        }

    volume = sum(t["qty"] for t in trades)
    financial = sum(t["price"] * t["qty"] for t in trades)
    buy = sum(t["qty"] for t in trades if t["side"] == "buy")
    sell = sum(t["qty"] for t in trades if t["side"] == "sell")
    neutral = volume - buy - sell
    vwap = financial / volume if volume > 0 else None
    close = trades[-1]["price"]
    return {
        "ticker": ticker,
        "strike": strike,
        "side": side_name,
        "trades": len(trades),
        "volume": volume,
        "financial": financial,
        "vwap": vwap,
        "open": trades[0]["price"],
        "high": max(t["price"] for t in trades),
        "low": min(t["price"] for t in trades),
        "close": close,
        "buy": buy,
        "sell": sell,
        "neutral": neutral,
        "cvd": buy - sell,
        "buy_pct": 100 * buy / volume if volume else 0.0,
        "sell_pct": 100 * sell / volume if volume else 0.0,
        "close_vwap_pct": 100 * (close / vwap - 1) if vwap else None,
        "avg_trade_qty": volume / len(trades) if trades else 0.0,
        "option_delta": option_delta,
        "delta_volume": (buy - sell) * option_delta * 100.0 if option_delta is not None else None,
        "delta_volume_buy": buy * option_delta * 100.0 if option_delta is not None else None,
        "delta_volume_sell": -sell * option_delta * 100.0 if option_delta is not None else None,
    }


def _flow_confidence(total_trades: float, total_volume: float, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    top_strike_vol = sorted(((r.get("call", {}).get("volume", 0.0) + r.get("put", {}).get("volume", 0.0)) for r in rows), reverse=True)
    top5 = float(sum(top_strike_vol[:5]))
    top5_share = (100.0 * top5 / total_volume) if total_volume > 0 else 0.0
    score = 0
    reasons: List[str] = []
    if total_trades >= 1500:
        score += 2
    elif total_trades >= 600:
        score += 1
    else:
        reasons.append("poucos negócios após filtro")
    if total_volume >= 400000:
        score += 2
    elif total_volume >= 150000:
        score += 1
    else:
        reasons.append("volume filtrado baixo")
    if top5_share <= 28:
        score += 2
    elif top5_share <= 42:
        score += 1
    else:
        reasons.append("fluxo concentrado em poucos strikes")
    if score >= 5:
        level = "ALTA"
    elif score >= 3:
        level = "MÉDIA"
    else:
        level = "BAIXA"
        if not reasons:
            reasons.append("microestrutura ruidosa")
    return {
        "level": level,
        "score": score,
        "top5_share_pct": top5_share,
        "note": " · ".join(reasons) if reasons else "boa dispersão de fluxo e amostra robusta",
    }


def load_option_workbook(
    path: str,
    side_name: str,
    target_date_br: str = "",
    delta_map: Optional[Dict[float, Dict[str, Optional[float]]]] = None,
    min_qty: float = 0.0,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    summaries: List[Dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return summaries, warnings
    preferred_year = _extract_year_from_br_date(target_date_br)
    inferred_date_br = infer_workbook_date_br(path, preferred_year=preferred_year)

    with zipfile.ZipFile(path) as zf:
        shared_strings = load_shared_strings(zf)
        for sheet_name, sheet_path in workbook_sheets(zf):
            match = TICKER_RE.match(sheet_name.strip())
            if not match:
                continue
            strike = float(match.group("strike"))
            rows = read_sheet_rows(zf, sheet_path, shared_strings)
            trades = parse_trade_rows(
                rows,
                target_date_br=target_date_br,
                fallback_date_br=inferred_date_br,
                min_qty=min_qty,
            )
            if not trades:
                warnings.append(f"{Path(path).name}/{sheet_name}: sem negócios válidos")
                continue
            side_key = "call_delta" if side_name == "call" else "put_delta"
            option_delta = None
            if delta_map:
                option_delta = (delta_map.get(strike) or {}).get(side_key)
            summaries.append(summarize_trades(trades, sheet_name.strip(), strike, side_name, option_delta=option_delta))

    return sorted(summaries, key=lambda item: item["strike"]), warnings


def collect_available_dates(options_dir: str) -> List[str]:
    dates = set()
    for path in discover_option_workbook_paths(options_dir):
        dates.update(workbook_trade_dates(path))
    return sorted(dates, key=_date_sort_key)


def _zero_leg(strike: float, side_name: str) -> Dict[str, Any]:
    return summarize_trades([], "", strike, side_name)


def combine_expiry(
    label: str,
    call_rows: List[Dict[str, Any]],
    put_rows: List[Dict[str, Any]],
    call_file: str,
    put_file: str,
    oi_rows: Dict[float, Dict[str, float]],
    oi_totals: Dict[str, float],
) -> Dict[str, Any]:
    call_map = {row["strike"]: row for row in call_rows}
    put_map = {row["strike"]: row for row in put_rows}
    strikes = sorted(set(call_map) | set(put_map))
    rows = []

    total_call_vol = sum(row["volume"] for row in call_rows)
    total_put_vol = sum(row["volume"] for row in put_rows)
    total_call_cvd = sum(row["cvd"] for row in call_rows)
    total_put_cvd = sum(row["cvd"] for row in put_rows)
    total_call_financial = sum(row["financial"] for row in call_rows)
    total_put_financial = sum(row["financial"] for row in put_rows)
    total_financial = total_call_financial + total_put_financial
    total_call_trades = sum(row["trades"] for row in call_rows)
    total_put_trades = sum(row["trades"] for row in put_rows)
    total_trades = total_call_trades + total_put_trades
    total_volume = total_call_vol + total_put_vol
    total_call_delta_volume = sum((row.get("delta_volume") or 0.0) for row in call_rows)
    total_put_delta_volume = sum((row.get("delta_volume") or 0.0) for row in put_rows)
    total_delta_volume = total_call_delta_volume + total_put_delta_volume
    call_delta_coverage = sum(1 for row in call_rows if row.get("option_delta") is not None)
    put_delta_coverage = sum(1 for row in put_rows if row.get("option_delta") is not None)
    financial_per_trade = total_financial / total_trades if total_trades > 0 else 0.0
    call_financial_per_trade = total_call_financial / total_call_trades if total_call_trades > 0 else 0.0
    put_financial_per_trade = total_put_financial / total_put_trades if total_put_trades > 0 else 0.0
    call_avg_price = total_call_financial / total_call_vol if total_call_vol > 0 else 0.0
    put_avg_price = total_put_financial / total_put_vol if total_put_vol > 0 else 0.0
    delta_volume_pc = total_put_vol - total_call_vol
    delta_financial_pc = total_put_financial - total_call_financial

    for strike in strikes:
        call = call_map.get(strike) or _zero_leg(strike, "call")
        put = put_map.get(strike) or _zero_leg(strike, "put")
        oi = oi_rows.get(strike) or {
            "call_oi": 0.0,
            "put_oi": 0.0,
            "total_oi": 0.0,
            "call_oi_value": 0.0,
            "put_oi_value": 0.0,
            "total_oi_value": 0.0,
        }
        call_pressure = max(call["cvd"], 0.0)
        put_pressure = max(put["cvd"], 0.0)
        net_protection = put_pressure - call_pressure
        row_volume = call["volume"] + put["volume"]
        threshold = max(1_000.0, 0.05 * max(row_volume, 1.0))
        if put_pressure > call_pressure and net_protection >= threshold:
            bias = "Pressão em Puts"
        elif call_pressure > put_pressure and -net_protection >= threshold:
            bias = "Pressão em Calls"
        else:
            bias = "Misto/Neutro"
        rows.append({
            "strike": strike,
            "call": call,
            "put": put,
            "call_pressure": call_pressure,
            "put_pressure": put_pressure,
            "net_protection": net_protection,
            "put_call_volume_ratio": (put["volume"] / call["volume"]) if call["volume"] > 0 else None,
            "call_oi": oi["call_oi"],
            "put_oi": oi["put_oi"],
            "total_oi": oi["total_oi"],
            "call_oi_value": oi["call_oi_value"],
            "put_oi_value": oi["put_oi_value"],
            "total_oi_value": oi["total_oi_value"],
            "delta_volume_pc": put["volume"] - call["volume"],
            "delta_financial_pc": put["financial"] - call["financial"],
            "bias": bias,
        })

    net_protection_total = sum(row["net_protection"] for row in rows)
    expiry_threshold = max(10_000.0, 0.02 * max(total_volume, 1.0))
    if net_protection_total >= expiry_threshold:
        flow_label = "Proteção via puts"
        flow_note = "Tape indica demanda compradora mais forte em puts."
    elif net_protection_total <= -expiry_threshold:
        flow_label = "Apetite em calls"
        flow_note = "Tape indica demanda compradora mais forte em calls."
    else:
        flow_label = "Misto/Neutro"
        flow_note = "Fluxo dividido; leitura direcional fraca."
    confidence = _flow_confidence(total_trades, total_volume, rows)

    return {
        "label": label,
        "call_file": call_file,
        "put_file": put_file,
        "rows": rows,
        "summary": {
            "strikes": len(rows),
            "call_trades": total_call_trades,
            "put_trades": total_put_trades,
            "total_trades": total_trades,
            "call_volume": total_call_vol,
            "put_volume": total_put_vol,
            "total_volume": total_volume,
            "call_financial": total_call_financial,
            "put_financial": total_put_financial,
            "total_financial": total_financial,
            "financial_per_trade": financial_per_trade,
            "call_financial_per_trade": call_financial_per_trade,
            "put_financial_per_trade": put_financial_per_trade,
            "call_avg_price": call_avg_price,
            "put_avg_price": put_avg_price,
            "delta_volume_pc": delta_volume_pc,
            "delta_financial_pc": delta_financial_pc,
            "call_delta_volume": total_call_delta_volume,
            "put_delta_volume": total_put_delta_volume,
            "total_delta_volume": total_delta_volume,
            "call_delta_coverage": call_delta_coverage,
            "put_delta_coverage": put_delta_coverage,
            "call_cvd": total_call_cvd,
            "put_cvd": total_put_cvd,
            "net_protection": net_protection_total,
            "call_oi": oi_totals.get("call_oi", 0.0),
            "put_oi": oi_totals.get("put_oi", 0.0),
            "total_oi": oi_totals.get("total_oi", 0.0),
            "call_oi_value": oi_totals.get("call_oi_value", 0.0),
            "put_oi_value": oi_totals.get("put_oi_value", 0.0),
            "total_oi_value": oi_totals.get("total_oi_value", 0.0),
            "flow_label": flow_label,
            "flow_note": flow_note,
            "confidence": confidence,
        },
    }


def analyze(options_dir: str, target_date_br: str = "", data_dir: str = "", min_qty: float = 0.0) -> Tuple[List[Dict[str, Any]], List[str]]:
    expiries = []
    warnings: List[str] = []
    tag = _br_to_tag(target_date_br)
    volume_map = discover_volume_csv_map(data_dir, tag) if data_dir and tag else {}
    close_map = discover_close_csv_map(data_dir, tag) if data_dir and tag else {}
    workbook_date_cache: Dict[Tuple[str, int], List[str]] = {}
    for pair in discover_option_files(options_dir):
        key = _normalize_expiry(pair["label"])
        close_path = close_map.get(key, "")
        delta_map = parse_close_delta_csv(close_path) if close_path else {}
        call_path = pick_workbook_for_date(pair.get("call_files", []), target_date_br, workbook_date_cache)
        put_path = pick_workbook_for_date(pair.get("put_files", []), target_date_br, workbook_date_cache)

        call_rows, call_warnings = load_option_workbook(
            call_path,
            "call",
            target_date_br=target_date_br,
            delta_map=delta_map,
            min_qty=min_qty,
        )
        put_rows, put_warnings = load_option_workbook(
            put_path,
            "put",
            target_date_br=target_date_br,
            delta_map=delta_map,
            min_qty=min_qty,
        )
        warnings.extend(call_warnings)
        warnings.extend(put_warnings)
        if not call_rows and not put_rows:
            warnings.append(f"{pair['label']}: nenhum negócio válido")
            continue
        volume_path = volume_map.get(key, "")
        oi_rows, oi_totals = parse_oi_volume_csv(volume_path) if volume_path else ({}, {
            "call_oi": 0.0,
            "put_oi": 0.0,
            "total_oi": 0.0,
            "call_oi_value": 0.0,
            "put_oi_value": 0.0,
            "total_oi_value": 0.0,
        })
        if not close_path:
            warnings.append(f"{pair['label']}: sem fechamento.csv para delta na tag {tag}")
        elif not delta_map:
            warnings.append(f"{pair['label']}: fechamento.csv sem deltas válidos na tag {tag}")
        if not volume_path:
            warnings.append(f"{pair['label']}: sem Volume.csv para tag {tag}")
        expiries.append(combine_expiry(
            pair["label"],
            call_rows,
            put_rows,
            call_path,
            put_path,
            oi_rows,
            oi_totals,
        ))
    return expiries, warnings


def analyze_date_groups(options_dir: str, preferred_date_br: str = "", data_dir: str = "", min_qty: float = 0.0) -> Tuple[List[Dict[str, Any]], List[str]]:
    dates = collect_available_dates(options_dir)
    if preferred_date_br and preferred_date_br not in dates:
        dates.append(preferred_date_br)
        dates = sorted(set(dates), key=_date_sort_key)

    groups: List[Dict[str, Any]] = []
    all_warnings: List[str] = []
    for date_br in dates:
        expiries, warnings = analyze(options_dir, target_date_br=date_br, data_dir=data_dir, min_qty=min_qty)
        all_warnings.extend([f"{date_br}: {w}" for w in warnings])
        if not expiries:
            continue
        groups.append({
            "date": date_br,
            "iso": _br_to_iso_date(date_br),
            "expiries": expiries,
        })
    return groups, all_warnings


def load_options_history(path: str) -> List[Dict[str, Any]]:
    payload = load_json(path, {})
    if isinstance(payload, list):
        groups = [group for group in payload if isinstance(group, dict) and group.get("date")]
    elif isinstance(payload, dict):
        groups = []
        for key, group in payload.items():
            if not isinstance(group, dict):
                continue
            item = dict(group)
            item.setdefault("date", key)
            item.setdefault("iso", _br_to_iso_date(item["date"]))
            item.setdefault("expiries", [])
            groups.append(item)
    else:
        groups = []
    return sorted(groups, key=lambda item: item.get("iso") or _br_to_iso_date(item.get("date", "")) or "")


def save_options_history(path: str, date_groups: List[Dict[str, Any]]) -> None:
    payload = {}
    for group in sorted(date_groups, key=lambda item: item.get("iso") or _br_to_iso_date(item.get("date", "")) or ""):
        date_key = str(group.get("date") or "")
        if not date_key:
            continue
        payload[date_key] = {
            "date": date_key,
            "iso": group.get("iso") or _br_to_iso_date(date_key),
            "expiries": group.get("expiries", []),
        }
    save_json(path, payload)


def merge_date_groups(
    history_groups: List[Dict[str, Any]],
    current_groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for source in (history_groups, current_groups):
        for group in source:
            if not isinstance(group, dict):
                continue
            date_key = str(group.get("date") or "")
            if not date_key:
                continue
            merged[date_key] = {
                "date": date_key,
                "iso": group.get("iso") or _br_to_iso_date(date_key),
                "expiries": group.get("expiries", []),
            }
    return sorted(merged.values(), key=lambda item: item.get("iso") or _br_to_iso_date(item.get("date", "")) or "")


def _fmt_file(path: str) -> str:
    return html.escape(Path(path).name) if path else "N/A"


def build_missing_html(output: str, ref_date: str, ref_tag: str, msg: str) -> None:
    doc = f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 Option Tape Flow</title>
<style>
:root {{ --bg:#FAFAF8; --card:#fff; --text:#1A1A18; --muted:#6B6960; --border:#E5E1D8; }}
[data-theme="dark"] {{ --bg:#0d1117; --card:#161b22; --text:#c9d1d9; --muted:#8b949e; --border:#30363d; }}
body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }}
.wrap {{ max-width:900px; margin:0 auto; padding:48px 22px; }}
.box {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:28px; }}
.kicker {{ color:var(--muted); font-size:.82rem; text-transform:uppercase; font-weight:700; }}
h1 {{ margin:.35rem 0 1rem; font-size:2rem; }}
p {{ color:var(--muted); line-height:1.6; }}
code {{ color:var(--text); }}
#theme-toggle {{ position:fixed; top:14px; right:14px; z-index:10; border:1px solid var(--border); background:var(--card); color:var(--text); border-radius:8px; padding:9px 11px; cursor:pointer; }}
</style>
</head>
<body>
<button id="theme-toggle">🌙</button>
<main class="wrap">
  <section class="box">
    <div class="kicker">Option Tape Flow · {html.escape(ref_date)} · {html.escape(ref_tag)}</div>
    <h1>Nenhum tape de opções encontrado</h1>
    <p>{html.escape(msg)}</p>
    <p>Coloque arquivos no padrão <code>CALLS VENC &lt;vencimento&gt;.xlsx</code> e <code>PUTS VENC &lt;vencimento&gt;.xlsx</code> em <code>data/</code>.</p>
  </section>
</main>
<script>
(function() {{
  const btn = document.getElementById('theme-toggle');
  const saved = localStorage.getItem('bova11-theme') || 'light';
  if (saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
  btn.textContent = document.documentElement.getAttribute('data-theme') === 'dark' ? '☀️' : '🌙';
  btn.onclick = function() {{
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    document.documentElement.setAttribute('data-theme', dark ? 'light' : 'dark');
    localStorage.setItem('bova11-theme', dark ? 'light' : 'dark');
    btn.textContent = dark ? '🌙' : '☀️';
  }};
}})();
</script>
</body>
</html>"""
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(doc)


def build_html(date_groups: List[Dict[str, Any]], warnings: List[str], ref_date: str, ref_tag: str, spot: float, min_qty: float) -> str:
    ref_date_br = _iso_to_br_date(ref_date)
    default_date_index = 0
    for i, group in enumerate(date_groups):
        if group.get("date") == ref_date_br:
            default_date_index = i
            break
    expiries = date_groups[default_date_index]["expiries"] if date_groups else []
    totals = {
        "strikes": sum(exp["summary"]["strikes"] for exp in expiries),
        "call_trades": sum(exp["summary"]["call_trades"] for exp in expiries),
        "put_trades": sum(exp["summary"]["put_trades"] for exp in expiries),
        "total_trades": sum(exp["summary"]["total_trades"] for exp in expiries),
        "call_volume": sum(exp["summary"]["call_volume"] for exp in expiries),
        "put_volume": sum(exp["summary"]["put_volume"] for exp in expiries),
        "total_volume": sum(exp["summary"]["total_volume"] for exp in expiries),
        "delta_volume_pc": sum(exp["summary"]["delta_volume_pc"] for exp in expiries),
        "total_financial": sum(exp["summary"]["total_financial"] for exp in expiries),
        "call_financial": sum(exp["summary"]["call_financial"] for exp in expiries),
        "put_financial": sum(exp["summary"]["put_financial"] for exp in expiries),
        "delta_financial_pc": sum(exp["summary"]["delta_financial_pc"] for exp in expiries),
        "call_delta_volume": sum(exp["summary"]["call_delta_volume"] for exp in expiries),
        "put_delta_volume": sum(exp["summary"]["put_delta_volume"] for exp in expiries),
        "total_delta_volume": sum(exp["summary"]["total_delta_volume"] for exp in expiries),
        "call_cvd": sum(exp["summary"]["call_cvd"] for exp in expiries),
        "put_cvd": sum(exp["summary"]["put_cvd"] for exp in expiries),
        "net_protection": sum(exp["summary"]["net_protection"] for exp in expiries),
        "call_oi": sum(exp["summary"]["call_oi"] for exp in expiries),
        "put_oi": sum(exp["summary"]["put_oi"] for exp in expiries),
        "total_oi": sum(exp["summary"]["total_oi"] for exp in expiries),
        "total_oi_value": sum(exp["summary"]["total_oi_value"] for exp in expiries),
    }
    if totals["net_protection"] > 0:
        top_label = "Proteção via puts"
    elif totals["net_protection"] < 0:
        top_label = "Apetite em calls"
    else:
        top_label = "Misto/Neutro"
    conf_levels = [exp["summary"].get("confidence", {}).get("level", "N/A") for exp in expiries]
    conf_low = sum(1 for lvl in conf_levels if lvl == "BAIXA")
    conf_mid = sum(1 for lvl in conf_levels if lvl == "MÉDIA")
    conf_high = sum(1 for lvl in conf_levels if lvl == "ALTA")

    tabs = "\n".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-index="{i}">{html.escape(exp["label"])}</button>'
        for i, exp in enumerate(expiries)
    )
    date_tabs = "\n".join(
        f'<button class="date-tab{" active" if i == default_date_index else ""}" data-date-index="{i}">{html.escape(group["date"])}</button>'
        for i, group in enumerate(date_groups)
    )
    warnings_html = ""
    if warnings:
        warning_items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings[:20])
        extra = f"<li>+{len(warnings) - 20} avisos adicionais.</li>" if len(warnings) > 20 else ""
        warnings_html = f'<details class="warn"><summary>Avisos de leitura</summary><ul>{warning_items}{extra}</ul></details>'

    data_json = _safe_json(date_groups)
    return f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOVA11 Option Tape Flow</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {{
  --bg:#FAFAF8; --surface:#FFFFFF; --surface2:#F4F1EA; --text:#1A1A18; --muted:#6B6960;
  --border:#E5E1D8; --green:#148A63; --red:#B33530; --gold:#B8720A; --blue:#2E6BBF;
}}
[data-theme="dark"] {{
  --bg:#0d1117; --surface:#161b22; --surface2:#21262d; --text:#c9d1d9; --muted:#8b949e;
  --border:#30363d; --green:#3fb950; --red:#f85149; --gold:#d29922; --blue:#58a6ff;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
#theme-toggle {{ position:fixed; top:14px; right:14px; z-index:20; border:1px solid var(--border); background:var(--surface); color:var(--text); border-radius:8px; padding:9px 11px; cursor:pointer; }}
.wrap {{ max-width:1420px; margin:0 auto; padding:34px 22px 50px; }}
.hero {{ display:flex; justify-content:space-between; gap:18px; align-items:flex-end; margin-bottom:22px; }}
.kicker {{ color:var(--muted); font-size:.78rem; text-transform:uppercase; letter-spacing:.08em; font-weight:800; }}
h1 {{ margin:.35rem 0 .35rem; font-size:clamp(1.8rem, 3vw, 3rem); line-height:1.05; letter-spacing:0; }}
.sub {{ color:var(--muted); line-height:1.45; max-width:820px; }}
.badge {{ display:inline-flex; align-items:center; min-height:34px; padding:7px 11px; border:1px solid var(--border); border-radius:8px; background:var(--surface); font-weight:800; white-space:nowrap; }}
.top-cards {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin-bottom:16px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:16px; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:15px; min-height:104px; min-width:0; overflow:hidden; }}
.label {{ color:var(--muted); font-size:.78rem; font-weight:800; text-transform:uppercase; }}
.value {{ font-size:clamp(1.05rem,1.85vw,1.55rem); font-weight:850; margin-top:8px; line-height:1.15; overflow-wrap:anywhere; word-break:break-word; }}
.mini {{ color:var(--muted); font-size:.82rem; margin-top:4px; }}
.pos {{ color:var(--green); }} .neg {{ color:var(--red); }} .neu {{ color:var(--muted); }} .gold {{ color:var(--gold); }}
.tabs {{ display:flex; flex-wrap:wrap; gap:8px; margin:18px 0; }}
.tab, .date-tab {{ border:1px solid var(--border); background:var(--surface); color:var(--text); border-radius:8px; padding:9px 13px; font-weight:800; cursor:pointer; }}
.tab.active, .date-tab.active {{ border-color:var(--blue); color:var(--blue); background:var(--surface2); }}
.grid {{ display:grid; grid-template-columns:1.1fr .9fr; gap:14px; align-items:start; }}
.panel {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; }}
.panel h2 {{ margin:0 0 4px; font-size:1.05rem; }}
.panel-sub {{ color:var(--muted); font-size:.86rem; margin-bottom:12px; }}
.rank-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
th,td {{ padding:9px 8px; border-bottom:1px solid var(--border); text-align:right; vertical-align:middle; }}
th:first-child,td:first-child, .left {{ text-align:left; }}
th {{ color:var(--muted); font-size:.72rem; text-transform:uppercase; }}
.bias {{ display:inline-flex; padding:4px 8px; border-radius:8px; background:var(--surface2); font-weight:800; white-space:nowrap; }}
.chart-box {{ height:460px; }}
.source {{ margin-top:14px; }}
.source table {{ font-size:.8rem; }}
.warn {{ margin-top:14px; color:var(--muted); }}
.warn summary {{ cursor:pointer; font-weight:800; color:var(--gold); }}
@media (max-width: 980px) {{
  .hero {{ display:block; }}
  .top-cards {{ grid-template-columns:repeat(3,1fr); }}
  .cards {{ grid-template-columns:repeat(2,1fr); }}
  .grid, .rank-grid {{ grid-template-columns:1fr; }}
  .chart-box {{ height:520px; }}
}}
@media (max-width: 560px) {{
  .wrap {{ padding:28px 12px 42px; }}
  .top-cards {{ grid-template-columns:1fr; }}
  .cards {{ grid-template-columns:1fr; }}
  th,td {{ padding:8px 5px; font-size:.78rem; }}
}}
@media (max-width: 1280px) {{
  .top-cards {{ grid-template-columns:repeat(4,1fr); }}
}}
@media (max-width: 760px) {{
  .top-cards {{ grid-template-columns:repeat(2,1fr); }}
}}
</style>
</head>
<body>
<button id="theme-toggle">🌙</button>
<main class="wrap">
  <section class="hero">
    <div>
      <div class="kicker">Option Tape Flow · {html.escape(ref_date)} · {html.escape(ref_tag)}</div>
      <h1>Fluxo real das opções por strike</h1>
      <div class="sub">Leitura conservadora de Times & Trades: CVD, VWAP e agressão por strike. Delta Volume usa: contratos × 100 × delta, com sinal do agressor (compra/venda). Não substitui Greeks/IV do fechamento B3. Filtro ativo: qtd ≥ {fmt_int(min_qty)}.</div>
    </div>
    <div class="badge" id="top-badge">{html.escape(top_label)}</div>
  </section>

  <section class="top-cards">
    <div class="card"><div class="label">Vencimentos</div><div class="value" id="top-expiries">{fmt_int(len(expiries))}</div><div class="mini">workbooks pareados</div></div>
    <div class="card"><div class="label">Strikes</div><div class="value" id="top-strikes">{fmt_int(totals["strikes"])}</div><div class="mini">abas com negócios</div></div>
    <div class="card"><div class="label">Negócios</div><div class="value" id="top-total-trades">{fmt_int(totals["total_trades"])}</div><div class="mini">calls + puts</div></div>
    <div class="card"><div class="label">Contratos</div><div class="value" id="top-total-volume">{fmt_int(totals["total_volume"])}</div><div class="mini">volume total</div></div>
    <div class="card"><div class="label">Δ Volume (P-C)</div><div class="value {'pos' if totals['delta_volume_pc'] >= 0 else 'neg'}" id="top-delta-vol">{fmt_signed_int(totals["delta_volume_pc"])}</div><div class="mini">puts - calls (contratos)</div></div>
    <div class="card"><div class="label">Financeiro Calls</div><div class="value" id="top-call-fin">{fmt_brl(totals["call_financial"])}</div><div class="mini">volume financeiro call</div></div>
    <div class="card"><div class="label">Financeiro Puts</div><div class="value" id="top-put-fin">{fmt_brl(totals["put_financial"])}</div><div class="mini">volume financeiro put</div></div>
    <div class="card"><div class="label">Δ Financeiro (P-C)</div><div class="value {'pos' if totals['delta_financial_pc'] >= 0 else 'neg'}" id="top-delta-fin">{fmt_brl(totals["delta_financial_pc"])}</div><div class="mini">puts - calls (R$)</div></div>
    <div class="card"><div class="label">Fin/Neg Calls</div><div class="value" id="top-call-fin-trade">{fmt_brl((totals["call_financial"] / totals["call_trades"]) if totals["call_trades"] > 0 else 0.0)}</div><div class="mini">ticket médio call</div></div>
    <div class="card"><div class="label">Fin/Neg Puts</div><div class="value" id="top-put-fin-trade">{fmt_brl((totals["put_financial"] / totals["put_trades"]) if totals["put_trades"] > 0 else 0.0)}</div><div class="mini">ticket médio put</div></div>
    <div class="card"><div class="label">Preço Médio Call</div><div class="value" id="top-call-avg-px">{fmt_price((totals["call_financial"] / totals["call_volume"]) if totals["call_volume"] > 0 else 0.0, 4)}</div><div class="mini">R$ por contrato</div></div>
    <div class="card"><div class="label">Preço Médio Put</div><div class="value" id="top-put-avg-px">{fmt_price((totals["put_financial"] / totals["put_volume"]) if totals["put_volume"] > 0 else 0.0, 4)}</div><div class="mini">R$ por contrato</div></div>
    <div class="card"><div class="label">Delta Vol Calls</div><div class="value {'pos' if totals['call_delta_volume'] >= 0 else 'neg'}" id="top-call-dv">{fmt_signed_int(totals["call_delta_volume"])}</div><div class="mini">eq. ações (calls)</div></div>
    <div class="card"><div class="label">Delta Vol Puts</div><div class="value {'pos' if totals['put_delta_volume'] >= 0 else 'neg'}" id="top-put-dv">{fmt_signed_int(totals["put_delta_volume"])}</div><div class="mini">eq. ações (puts)</div></div>
    <div class="card"><div class="label">Delta Vol Líquido</div><div class="value {'pos' if totals['total_delta_volume'] >= 0 else 'neg'}" id="top-net-dv">{fmt_signed_int(totals["total_delta_volume"])}</div><div class="mini">bullish/bearish</div></div>
    <div class="card"><div class="label">CVD Calls</div><div class="value {'pos' if totals['call_cvd'] >= 0 else 'neg'}" id="top-call-cvd">{fmt_signed_int(totals["call_cvd"])}</div><div class="mini">agressão líquida</div></div>
    <div class="card"><div class="label">CVD Puts</div><div class="value {'pos' if totals['put_cvd'] >= 0 else 'neg'}" id="top-put-cvd">{fmt_signed_int(totals["put_cvd"])}</div><div class="mini">agressão líquida</div></div>
    <div class="card"><div class="label">Proteção Líquida</div><div class="value {'pos' if totals['net_protection'] >= 0 else 'neg'}" id="top-net-protection">{fmt_signed_int(totals["net_protection"])}</div><div class="mini">put pressure - call pressure</div></div>
    <div class="card"><div class="label">OI Total</div><div class="value" id="top-total-oi">{fmt_int(totals["total_oi"])}</div><div class="mini">C. Abertos (calls + puts)</div></div>
    <div class="card"><div class="label">OI Total (R$)</div><div class="value" id="top-total-oi-value">{fmt_brl(totals["total_oi_value"])}</div><div class="mini">estimado por bid/ask médio</div></div>
    <div class="card"><div class="label">Qualidade (venc.)</div><div class="value">{fmt_int(conf_high)} alta · {fmt_int(conf_mid)} média · {fmt_int(conf_low)} baixa</div><div class="mini">dispersão e robustez do tape filtrado</div></div>
  </section>

  <div class="panel" style="margin-bottom:14px;">
    <h2>Filtro de data</h2>
    <div class="panel-sub">Mostra as datas presentes nos workbooks de Times & Trades.</div>
    <nav class="tabs" id="date-tabs">{date_tabs}</nav>
  </div>

  <nav class="tabs" id="expiry-tabs">{tabs}</nav>

  <section class="grid">
    <div class="panel">
      <h2 id="chart-title">CVD por strike</h2>
      <div class="panel-sub" id="chart-sub"></div>
      <div class="chart-box"><canvas id="cvd-chart"></canvas></div>
    </div>
    <div class="panel">
      <h2>Resumo do vencimento</h2>
      <div class="panel-sub" id="summary-note"></div>
      <div id="expiry-cards" class="cards" style="grid-template-columns:repeat(2,1fr); margin-bottom:0;"></div>
    </div>
  </section>

  <section class="panel" style="margin-top:14px;">
    <h2>Rankings</h2>
    <div class="panel-sub">Maiores leituras de agressão compradora em puts, calls e proteção líquida.</div>
    <div class="rank-grid">
      <div><table><thead><tr><th>Put CVD</th><th>CVD</th><th>Vol</th></tr></thead><tbody id="rank-put"></tbody></table></div>
      <div><table><thead><tr><th>Call CVD</th><th>CVD</th><th>Vol</th></tr></thead><tbody id="rank-call"></tbody></table></div>
      <div><table><thead><tr><th>Proteção</th><th>Net</th><th>Bias</th></tr></thead><tbody id="rank-net"></tbody></table></div>
    </div>
  </section>

  <section class="panel" style="margin-top:14px;">
    <h2>Detalhe por strike</h2>
    <div class="panel-sub">VWAP, volume, negócios, CVD, Δ volume (P-C) e OI por strike.</div>
    <div style="overflow:auto;">
      <table>
        <thead>
          <tr>
            <th>Strike</th><th>Bias</th><th>Call VWAP</th><th>Call CVD</th><th>Call Vol</th><th>Call Neg</th>
            <th>Put VWAP</th><th>Put CVD</th><th>Put Vol</th><th>Put Neg</th><th>ΔVol Call</th><th>ΔVol Put</th><th>ΔVol Líq</th><th>OI Total</th><th>OI Total (R$)</th><th>Proteção</th><th>P/C Vol</th>
          </tr>
        </thead>
        <tbody id="detail-body"></tbody>
      </table>
    </div>
  </section>

  <section class="panel source">
    <h2>Fontes</h2>
    <table><thead><tr><th>Vencimento</th><th>Calls</th><th>Puts</th></tr></thead><tbody id="source-body"></tbody></table>
    {warnings_html}
  </section>
</main>

<script>
const DATE_GROUPS = {data_json};
let currentDate = {default_date_index};
let DATA = DATE_GROUPS[currentDate]?.expiries || [];
let current = 0;
let cvdChart = null;

function fI(v) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  return Math.round(v).toLocaleString('pt-BR');
}}
function fP(v, d=2) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  return v.toLocaleString('pt-BR', {{ minimumFractionDigits:d, maximumFractionDigits:d }});
}}
function fBRL(v, digits=0, compact=true) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  const a = Math.abs(v);
  const s = v < 0 ? '-' : '';
  if (compact && a >= 1_000_000_000) return s + 'R$ ' + fP(a / 1_000_000_000, 2) + ' bi';
  if (compact && a >= 1_000_000) return s + 'R$ ' + fP(a / 1_000_000, 2) + ' mi';
  return v.toLocaleString('pt-BR', {{ style:'currency', currency:'BRL', minimumFractionDigits:digits, maximumFractionDigits:digits }});
}}
function fSigned(v) {{
  if (v === null || v === undefined || Number.isNaN(v)) return 'N/A';
  const sign = v >= 0 ? '+' : '-';
  return sign + fI(Math.abs(v));
}}
function cls(v) {{ return v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu'; }}
function rowHtml(cells) {{ return '<tr>' + cells.map(x => '<td>' + x + '</td>').join('') + '</tr>'; }}
function biasClass(text) {{
  if (text.includes('Puts')) return 'pos';
  if (text.includes('Calls')) return 'neg';
  return 'neu';
}}
function topRows(rows, getter, desc=true) {{
  return [...rows].sort((a,b) => desc ? getter(b)-getter(a) : getter(a)-getter(b)).slice(0,8);
}}
function renderRank(target, rows, mapper) {{
  document.getElementById(target).innerHTML = rows.map(mapper).join('');
}}
function totalsFor(expiries) {{
  return expiries.reduce((acc, exp) => {{
    const s = exp.summary || {{}};
    acc.strikes += s.strikes || 0;
    acc.call_trades += s.call_trades || 0;
    acc.put_trades += s.put_trades || 0;
    acc.total_trades += s.total_trades || 0;
    acc.call_volume += s.call_volume || 0;
    acc.put_volume += s.put_volume || 0;
    acc.total_volume += s.total_volume || 0;
    acc.delta_volume_pc += s.delta_volume_pc || 0;
    acc.total_financial += s.total_financial || 0;
    acc.call_financial += s.call_financial || 0;
    acc.put_financial += s.put_financial || 0;
    acc.delta_financial_pc += s.delta_financial_pc || 0;
    acc.call_delta_volume += s.call_delta_volume || 0;
    acc.put_delta_volume += s.put_delta_volume || 0;
    acc.total_delta_volume += s.total_delta_volume || 0;
    acc.call_cvd += s.call_cvd || 0;
    acc.put_cvd += s.put_cvd || 0;
    acc.net_protection += s.net_protection || 0;
    acc.call_oi += s.call_oi || 0;
    acc.put_oi += s.put_oi || 0;
    acc.total_oi += s.total_oi || 0;
    acc.total_oi_value += s.total_oi_value || 0;
    return acc;
  }}, {{
    strikes:0,
    call_trades:0,
    put_trades:0,
    total_trades:0,
    call_volume:0,
    put_volume:0,
    total_volume:0,
    delta_volume_pc:0,
    total_financial:0,
    call_financial:0,
    put_financial:0,
    delta_financial_pc:0,
    call_delta_volume:0,
    put_delta_volume:0,
    total_delta_volume:0,
    call_cvd:0,
    put_cvd:0,
    net_protection:0,
    call_oi:0,
    put_oi:0,
    total_oi:0,
    total_oi_value:0,
  }});
}}
function setTone(el, value) {{
  el.classList.remove('pos', 'neg', 'neu');
  el.classList.add(cls(value));
}}
function renderTop() {{
  const totals = totalsFor(DATA);
  const badge = totals.net_protection > 0 ? 'Proteção via puts' : totals.net_protection < 0 ? 'Apetite em calls' : 'Misto/Neutro';
  document.getElementById('top-badge').textContent = badge;
  document.getElementById('top-expiries').textContent = fI(DATA.length);
  document.getElementById('top-strikes').textContent = fI(totals.strikes);
  document.getElementById('top-total-trades').textContent = fI(totals.total_trades);
  document.getElementById('top-total-volume').textContent = fI(totals.total_volume);
  document.getElementById('top-call-fin').textContent = fBRL(totals.call_financial);
  document.getElementById('top-put-fin').textContent = fBRL(totals.put_financial);
  document.getElementById('top-call-fin-trade').textContent = fBRL(totals.call_trades > 0 ? totals.call_financial / totals.call_trades : 0, 2);
  document.getElementById('top-put-fin-trade').textContent = fBRL(totals.put_trades > 0 ? totals.put_financial / totals.put_trades : 0, 2);
  document.getElementById('top-call-avg-px').textContent = fP(totals.call_volume > 0 ? totals.call_financial / totals.call_volume : 0, 4);
  document.getElementById('top-put-avg-px').textContent = fP(totals.put_volume > 0 ? totals.put_financial / totals.put_volume : 0, 4);
  const callDvEl = document.getElementById('top-call-dv');
  const putDvEl = document.getElementById('top-put-dv');
  const netDvEl = document.getElementById('top-net-dv');
  const callEl = document.getElementById('top-call-cvd');
  const putEl = document.getElementById('top-put-cvd');
  const netEl = document.getElementById('top-net-protection');
  const deltaVolEl = document.getElementById('top-delta-vol');
  const deltaFinEl = document.getElementById('top-delta-fin');
  callDvEl.textContent = fSigned(totals.call_delta_volume); setTone(callDvEl, totals.call_delta_volume);
  putDvEl.textContent = fSigned(totals.put_delta_volume); setTone(putDvEl, totals.put_delta_volume);
  netDvEl.textContent = fSigned(totals.total_delta_volume); setTone(netDvEl, totals.total_delta_volume);
  callEl.textContent = fSigned(totals.call_cvd); setTone(callEl, totals.call_cvd);
  putEl.textContent = fSigned(totals.put_cvd); setTone(putEl, totals.put_cvd);
  netEl.textContent = fSigned(totals.net_protection); setTone(netEl, totals.net_protection);
  deltaVolEl.textContent = fSigned(totals.delta_volume_pc); setTone(deltaVolEl, totals.delta_volume_pc);
  deltaFinEl.textContent = fBRL(totals.delta_financial_pc); setTone(deltaFinEl, totals.delta_financial_pc);
  document.getElementById('top-total-oi').textContent = fI(totals.total_oi);
  document.getElementById('top-total-oi-value').textContent = fBRL(totals.total_oi_value);
}}
function renderSources() {{
  document.getElementById('source-body').innerHTML = DATA.map(exp => rowHtml([
    exp.label,
    (exp.call_file || '').split('/').pop() || 'N/A',
    (exp.put_file || '').split('/').pop() || 'N/A',
  ])).join('');
}}
function renderExpiryTabs() {{
  document.getElementById('expiry-tabs').innerHTML = DATA.map((exp, i) =>
    `<button class="tab${{i === current ? ' active' : ''}}" data-index="${{i}}">${{exp.label}}</button>`
  ).join('');
  document.querySelectorAll('.tab').forEach(btn => {{
    btn.addEventListener('click', () => {{
      current = Number(btn.dataset.index);
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      render();
    }});
  }});
}}
function render() {{
  const exp = DATA[current];
  if (!exp) return;
  const rows = exp.rows || [];
  const s = exp.summary;
  document.getElementById('chart-title').textContent = 'CVD por strike · ' + exp.label;
  document.getElementById('chart-sub').textContent = s.flow_label + ' · ' + s.flow_note;
  document.getElementById('summary-note').textContent = s.flow_note + ' · Qualidade: ' + (s.confidence?.level || 'N/A');
  document.getElementById('expiry-cards').innerHTML = [
    ['CVD Calls', fSigned(s.call_cvd), cls(s.call_cvd)],
    ['CVD Puts', fSigned(s.put_cvd), cls(s.put_cvd)],
    ['Delta Vol Calls', fSigned(s.call_delta_volume), cls(s.call_delta_volume)],
    ['Delta Vol Puts', fSigned(s.put_delta_volume), cls(s.put_delta_volume)],
    ['Delta Vol Líquido', fSigned(s.total_delta_volume), cls(s.total_delta_volume)],
    ['Proteção Líquida', fSigned(s.net_protection), cls(s.net_protection)],
    ['Δ Volume (P-C)', fSigned(s.delta_volume_pc), cls(s.delta_volume_pc)],
    ['Δ Financeiro (P-C)', fBRL(s.delta_financial_pc), cls(s.delta_financial_pc)],
    ['Strikes', fI(s.strikes), 'neu'],
    ['Negócios Calls', fI(s.call_trades), 'neu'],
    ['Negócios Puts', fI(s.put_trades), 'neu'],
    ['Contratos Calls', fI(s.call_volume), 'neu'],
    ['Contratos Puts', fI(s.put_volume), 'neu'],
    ['Financeiro Calls', fBRL(s.call_financial), 'neu'],
    ['Financeiro Puts', fBRL(s.put_financial), 'neu'],
    ['Fin/Neg Calls', fBRL(s.call_financial_per_trade || 0, 2), 'neu'],
    ['Fin/Neg Puts', fBRL(s.put_financial_per_trade || 0, 2), 'neu'],
    ['Preço Médio Call', fP(s.call_avg_price || 0, 4), 'neu'],
    ['Preço Médio Put', fP(s.put_avg_price || 0, 4), 'neu'],
    ['OI Total', fI(s.total_oi), 'neu'],
    ['OI Total (R$)', fBRL(s.total_oi_value), 'neu'],
    ['Qualidade do Tape', s.confidence?.level || 'N/A', s.confidence?.level === 'ALTA' ? 'pos' : (s.confidence?.level === 'MÉDIA' ? 'gold' : 'neg')],
    ['Top-5 Concentração', fP(s.confidence?.top5_share_pct || 0, 2) + '%', 'neu'],
  ].map(([label,value,tone]) => `<div class="card"><div class="label">${{label}}</div><div class="value ${{tone}}">${{value}}</div></div>`).join('');

  renderRank('rank-put', topRows(rows, r => r.put.cvd), r =>
    rowHtml([`K${{fP(r.strike,0)}}`, `<span class="${{cls(r.put.cvd)}}">${{fSigned(r.put.cvd)}}</span>`, fI(r.put.volume)])
  );
  renderRank('rank-call', topRows(rows, r => r.call.cvd), r =>
    rowHtml([`K${{fP(r.strike,0)}}`, `<span class="${{cls(r.call.cvd)}}">${{fSigned(r.call.cvd)}}</span>`, fI(r.call.volume)])
  );
  renderRank('rank-net', topRows(rows, r => r.net_protection), r =>
    rowHtml([`K${{fP(r.strike,0)}}`, `<span class="${{cls(r.net_protection)}}">${{fSigned(r.net_protection)}}</span>`, `<span class="bias ${{biasClass(r.bias)}}">${{r.bias}}</span>`])
  );

  document.getElementById('detail-body').innerHTML = rows.map(r => rowHtml([
    `<strong>K${{fP(r.strike,0)}}</strong>`,
    `<span class="bias ${{biasClass(r.bias)}}">${{r.bias}}</span>`,
    fP(r.call.vwap, 4),
    `<span class="${{cls(r.call.cvd)}}">${{fSigned(r.call.cvd)}}</span>`,
    fI(r.call.volume),
    fI(r.call.trades),
    fP(r.put.vwap, 4),
    `<span class="${{cls(r.put.cvd)}}">${{fSigned(r.put.cvd)}}</span>`,
    fI(r.put.volume),
    fI(r.put.trades),
    `<span class="${{cls(r.call.delta_volume || 0)}}">${{fSigned(r.call.delta_volume || 0)}}</span>`,
    `<span class="${{cls(r.put.delta_volume || 0)}}">${{fSigned(r.put.delta_volume || 0)}}</span>`,
    `<span class="${{cls((r.call.delta_volume || 0) + (r.put.delta_volume || 0))}}">${{fSigned((r.call.delta_volume || 0) + (r.put.delta_volume || 0))}}</span>`,
    fI(r.total_oi),
    fBRL(r.total_oi_value),
    `<span class="${{cls(r.net_protection)}}">${{fSigned(r.net_protection)}}</span>`,
    r.put_call_volume_ratio == null ? 'N/A' : fP(r.put_call_volume_ratio, 2) + 'x',
  ])).join('');

  const labels = rows.map(r => 'K' + fP(r.strike,0));
  const callCvd = rows.map(r => r.call.cvd);
  const putCvd = rows.map(r => r.put.cvd);
  const ctx = document.getElementById('cvd-chart');
  if (cvdChart) cvdChart.destroy();
  cvdChart = new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels,
      datasets: [
        {{ label:'Calls CVD', data:callCvd, backgroundColor:'rgba(46,107,191,.78)', borderColor:'#2E6BBF', borderWidth:1 }},
        {{ label:'Puts CVD', data:putCvd, backgroundColor:'rgba(179,53,48,.72)', borderColor:'#B33530', borderWidth:1 }},
      ]
    }},
    options: {{
      responsive:true,
      maintainAspectRatio:false,
      indexAxis:'y',
      interaction: {{ mode:'y', intersect:false }},
      plugins: {{ legend: {{ position:'bottom' }} }},
      scales: {{
        x: {{ ticks: {{ callback: v => fI(v) }}, grid: {{ color:'rgba(130,130,130,.15)' }} }},
        y: {{ grid: {{ display:false }} }}
      }}
    }}
  }});
}}

document.querySelectorAll('.date-tab').forEach(btn => {{
  btn.addEventListener('click', () => {{
    currentDate = Number(btn.dataset.dateIndex);
    DATA = DATE_GROUPS[currentDate]?.expiries || [];
    current = 0;
    document.querySelectorAll('.date-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderTop();
    renderSources();
    renderExpiryTabs();
    render();
  }});
}});

(function() {{
  const btn = document.getElementById('theme-toggle');
  const saved = localStorage.getItem('bova11-theme') || 'light';
  if (saved === 'dark') document.documentElement.setAttribute('data-theme','dark');
  btn.textContent = document.documentElement.getAttribute('data-theme') === 'dark' ? '☀️' : '🌙';
  btn.onclick = function() {{
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    document.documentElement.setAttribute('data-theme', dark ? 'light' : 'dark');
    localStorage.setItem('bova11-theme', dark ? 'light' : 'dark');
    btn.textContent = dark ? '🌙' : '☀️';
    render();
  }};
  renderTop();
  renderSources();
  renderExpiryTabs();
  render();
}})();
</script>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="BOVA11 Option Tape Flow por Strike")
    parser.add_argument("--data-dir", default="", help="Diretório de dados")
    parser.add_argument("--options-dir", default="", help="Diretório dos XLSX CALLS/PUTS")
    parser.add_argument("--output", required=True, help="HTML de saída")
    parser.add_argument("--ref-date", required=True, help="Data ISO dos dados")
    parser.add_argument("--ref-tag", default="", help="Tag do run")
    parser.add_argument("--spot", type=float, default=0.0, help="Spot manual do BOVA11")
    parser.add_argument("--min-qty", type=float, default=1000.0, help="Filtro mínimo de quantidade por negócio")
    parser.add_argument("--history-file", default=DEFAULT_HISTORY_FILE, help="Histórico persistido de grupos por data")
    args = parser.parse_args()

    options_dir = args.options_dir.strip() or args.data_dir.strip()
    history_groups = load_options_history(args.history_file)
    if not options_dir:
        if not history_groups:
            msg = "Diretório de opções não informado."
            build_missing_html(args.output, args.ref_date, args.ref_tag, msg)
            print(f"⚠️ {msg} HTML placeholder gerado em {args.output}")
            return 0

    target_date_br = _iso_to_br_date(args.ref_date)
    current_groups: List[Dict[str, Any]] = []
    warnings: List[str] = []
    if options_dir:
        current_groups, warnings = analyze_date_groups(
            options_dir,
            preferred_date_br=target_date_br,
            data_dir=args.data_dir.strip(),
            min_qty=args.min_qty,
        )

    date_groups = merge_date_groups(history_groups, current_groups)
    if not date_groups:
        msg = f"Nenhum par CALLS/PUTS de opções encontrado em {options_dir}."
        build_missing_html(args.output, args.ref_date, args.ref_tag, msg)
        print(f"⚠️ {msg} HTML placeholder gerado em {args.output}")
        return 0

    save_options_history(args.history_file, date_groups)
    html_doc = build_html(date_groups, warnings, args.ref_date, args.ref_tag, args.spot, args.min_qty)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html_doc)

    selected = next((g for g in date_groups if g.get("date") == target_date_br), date_groups[-1])
    selected_expiries = selected["expiries"]
    total_strikes = sum(exp["summary"]["strikes"] for exp in selected_expiries)
    total_call_cvd = sum(exp["summary"]["call_cvd"] for exp in selected_expiries)
    total_put_cvd = sum(exp["summary"]["put_cvd"] for exp in selected_expiries)
    total_call_delta_volume = sum(exp["summary"]["call_delta_volume"] for exp in selected_expiries)
    total_put_delta_volume = sum(exp["summary"]["put_delta_volume"] for exp in selected_expiries)
    total_delta_volume = sum(exp["summary"]["total_delta_volume"] for exp in selected_expiries)
    total_trades = sum(exp["summary"]["total_trades"] for exp in selected_expiries)
    total_volume = sum(exp["summary"]["total_volume"] for exp in selected_expiries)
    total_call_volume = sum(exp["summary"]["call_volume"] for exp in selected_expiries)
    total_put_volume = sum(exp["summary"]["put_volume"] for exp in selected_expiries)
    total_call_financial = sum(exp["summary"]["call_financial"] for exp in selected_expiries)
    total_put_financial = sum(exp["summary"]["put_financial"] for exp in selected_expiries)
    total_oi = sum(exp["summary"]["total_oi"] for exp in selected_expiries)
    total_oi_value = sum(exp["summary"]["total_oi_value"] for exp in selected_expiries)
    print(f"✅ Option Tape Flow gerado: {args.output}")
    print(f"   Diretório: {options_dir}")
    print(f"   Filtro: qtd >= {fmt_int(args.min_qty)}")
    print(f"   Datas: {', '.join(g['date'] for g in date_groups)}")
    print(f"   Histórico: {args.history_file}")
    print(f"   Data selecionada: {selected['date']} | Vencimentos: {len(selected_expiries)} | Strikes: {fmt_int(total_strikes)}")
    print(f"   Negócios: {fmt_int(total_trades)} | Contratos: {fmt_int(total_volume)}")
    print(f"   Δ Volume (P-C): {fmt_signed_int(total_put_volume - total_call_volume)}")
    print(f"   Delta Vol Calls: {fmt_signed_int(total_call_delta_volume)} | Delta Vol Puts: {fmt_signed_int(total_put_delta_volume)} | Delta Vol Líq: {fmt_signed_int(total_delta_volume)}")
    print(f"   Fin Calls: {fmt_brl(total_call_financial)} | Fin Puts: {fmt_brl(total_put_financial)}")
    print(f"   CVD Calls: {fmt_signed_int(total_call_cvd)} | CVD Puts: {fmt_signed_int(total_put_cvd)}")
    print(f"   OI Total: {fmt_int(total_oi)} | OI Total (R$): {fmt_brl(total_oi_value)}")
    if warnings:
        print(f"   Avisos: {len(warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
