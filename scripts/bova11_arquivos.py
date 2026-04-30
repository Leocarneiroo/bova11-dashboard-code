#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOVA11 Arquivos — Módulo 20
============================
Consolida os CSVs do run atual e os HTMLs publicados no dashboard em artefatos
estruturados para IA, sem embutir payloads gigantes na aba Arquivos.
"""

from __future__ import annotations

import argparse
import ast
import csv
import html
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from bova11_shared import parse_br_number, normalize_tag
except Exception:  # pragma: no cover - fallback para execução isolada incomum
    def parse_br_number(raw, none_on_blank: bool = False):
        if raw is None:
            return None if none_on_blank else 0.0
        s = str(raw).strip().replace("%", "")
        if s in ("", "-", "--", "None", "nan"):
            return None if none_on_blank else 0.0
        mult = 1.0
        if s.lower().endswith("k"):
            mult = 1_000.0
            s = s[:-1]
        elif s.lower().endswith("m"):
            mult = 1_000_000.0
            s = s[:-1]
        try:
            return float(s.replace(".", "").replace(",", ".")) * mult
        except Exception:
            return None if none_on_blank else 0.0

    def normalize_tag(tag: str) -> str:
        return re.sub(r"(pos|pre)([a-z]{3})$", r"\2", str(tag or "").lower())


CONCEITOS_MD = """
## CONCEITOS E FÓRMULAS VITAIS (Para entendimento da IA)

### 1. Demanda Institucional e PDI (Módulo 19)
- **ADI (Aggregate Demand Index)**: Mede o fluxo total `0.4*ΔSPS + 0.3*PIC + 0.2*OIF + 0.1*VIC`.
  * SPS usa puts OTM abaixo de `spot*0.99`, calls OTM acima de `spot*1.01`, e `spot_d` / `spot_d1` corretos por dia.
  * PIC/VIC/OIF usam a massa combinada `D + D-1` no denominador para manter os ratios na escala interpretável.
  * > +0.02 = Puts demand, Hedging (Bearish/Proteção).
  * < -0.02 = Calls demand, Risk-on (Bullish).
- **PDI (Put Demand Index)**: `ΔSkew * ΔIV_ATM`. Avalia a aceleração na demanda por proteção.
  * Hedge: Puts em demanda + IV subindo (medo agudo).
  * Defensive: Puts em demanda mas IV caindo.
- **IDS (Institutional Demand Score)**: Z-score robusto integrado de 7 variáveis (Skew, Premium, OI, IV, GEX, Charm, Vanna), com clipping de extremos, cobertura mínima e renormalização dos pesos ativos quando IV estiver ausente. `> +0.5 = Bear, < -0.5 = Bull`.
- **DFP (Delta Flow Proxy)**: Proxy do Delta-adjusted flow em Notional Net.

### 2. Max Pain e Gravitação (Módulos 11 e 12)
- **Max Pain**: Preço de máxima dor para lançadores de opções (onde menor valor é pago em expiração). O preço tende a ser atraído para este ponto perto do vencimento.
- **Mapa Gravitacional (Convergência)**: Distância e alinhamento do Spot com 3 centros de gravidade:
  * μOI = Ponto médio ponderado pelo Open Interest.
  * μGEX = Ponto médio ponderado pelo Gamma Exposure.
  * μDEX = Ponto médio ponderado pelo Delta Exposure.
  * **Score de Convergência (0-100)**: Quanto mais alinhados os três, maior o poder gravitacional. > 75 = risco centralizado e atração forte.

### 3. Convexidade e Gregos (Módulos 4 e 7)
- **GEX (Gamma Exposure)**: Exposição de Gamma líquida dos dealers.
  * Fórmula canônica: `GEX = gamma * OI * spot² / 100`, calls positivas e puts negativas.
  * GEX Positivo (>0): Dealers vendem na alta, compram na baixa. Comprime volatilidade e consolida o mercado.
  * GEX Negativo (<0): Dealers compram na alta, vendem na baixa. Aumenta volatilidade e risco de squeeze.
  * **GEX Flip**: Preço onde o GEX muda de sinal.
- **DEX (Delta Exposure)**: Perfil por spot simulado. Na aba unificada há leitura retail e leitura FM/hedge.
- **VEX (Vanna Exposure)**: Sensibilidade do delta à variação de vol, recalculada por spot.
- **TEX (Theta Exposure)**: Fluxo diário por decaimento de tempo, recalculado por spot na perspectiva do vendedor.
- **Vanna**: Mudança de Delta em função da volatilidade.
- **Charm**: Mudança de Delta em função do tempo.
  * Módulo 7 usa `Merton + forward implícito via put-call parity` por vencimento.
  * Guard rails atuais:
    - forward só entra com `OI total >= 10k` por strike para reduzir ruído de liquidez;
    - se `bid/ask` ou forward falhar, o modelo cai em `q = 0`;
    - vencimentos muito curtos podem distorcer `q` anualizado por ruído de microestrutura.

### 4. Trade Score Institucional (Módulos 5 e 6)
- Avalia cada opção em liquidez, OI edge, vol edge, GEX imbalance, delta/theta e distância do Max Pain.
- Gera o **Quality Score (0-100)**. Scores altos indicam melhor suporte estrutural.
- O viés institucional analisa pressão líquida em calls e puts.

### 5. Prediction e Dinâmica de Skew
- **IV Skew Signal v2/v3**: Sinal composto por `ΔSkew_norm`, Z-Score, aceleração e ajustes de confiança.
- **Hunter Walls**: Agregações relevantes de volume intradiário por strike.
- **GEX History**: Crescimento repentino de GEX em um strike pode virar imã de preço.
- **OI Stats**: Distribuição estatística do Open Interest.
- **Bandas de Volatilidade**: Referência de volatilidade realizada contra IV e Expected Move.
"""


CLOSE_HEADERS = [
    "call_ticker", "call_last", "call_oi", "call_delta", "call_gamma",
    "call_theta", "call_vega", "call_iv", "call_trades", "call_bid",
    "call_ask", "strike", "put_bid", "put_ask", "put_trades", "put_iv",
    "put_vega", "put_theta", "put_gamma", "put_delta", "put_oi",
    "put_last", "put_ticker",
]

VOLUME_HEADERS = [
    "call_ticker", "call_volume", "call_oi", "call_bid", "call_ask",
    "strike", "put_bid", "put_ask", "put_oi", "put_volume", "put_ticker",
]


def agora_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def looks_numeric(raw: Any) -> bool:
    s = str(raw or "").strip()
    if s in ("", "-", "--"):
        return False
    return bool(re.match(r"^-?[\d.]*\d,\d+%?$|^-?[\d.]+([kKmM]|%)?$", s))


def parse_cell(raw: Any) -> Any:
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "-", "--"):
        return None
    if looks_numeric(s):
        val = parse_br_number(s, none_on_blank=True)
        if val is None:
            return s
        if float(val).is_integer() and not re.search(r",\d|%", s):
            return int(val)
        return val
    return s


def unique_headers(headers: Iterable[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for idx, header in enumerate(headers):
        base = re.sub(r"[^0-9a-zA-Z_]+", "_", str(header or "").strip().lower()).strip("_")
        if not base:
            base = f"col_{idx + 1}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        out.append(base if count == 1 else f"{base}_{count}")
    return out


def canonical_headers(raw_headers: List[str], kind: str) -> List[str]:
    if kind == "volume" and len(raw_headers) == len(VOLUME_HEADERS):
        return VOLUME_HEADERS[:]
    if kind == "fechamento" and len(raw_headers) == len(CLOSE_HEADERS):
        return CLOSE_HEADERS[:]
    return unique_headers(raw_headers)


def extract_csv_metadata(filename: str) -> Optional[Dict[str, Any]]:
    space_match = re.match(r"^venc (.+?) fechamento \(([^)]+)\)\.csv$", filename, re.IGNORECASE)
    under_match = re.match(r"^venc_(.+?)_fechamento__([^_]+)_\.csv$", filename, re.IGNORECASE)

    if space_match:
        expiration = space_match.group(1).strip()
        inner = space_match.group(2).strip()
    elif under_match:
        expiration = under_match.group(1).replace("_", " ").strip()
        inner = under_match.group(2).strip()
    else:
        return None

    is_volume = bool(re.search(r"\bvolume\b", inner, re.IGNORECASE))
    tag = re.sub(r"\s+volume$", "", inner, flags=re.IGNORECASE).strip()
    return {
        "filename": filename,
        "expiration": expiration,
        "tag": tag,
        "tag_normalized": normalize_tag(tag),
        "kind": "volume" if is_volume else "fechamento",
    }


def classify_role(tag: str, ref_tag: str, ref_tag_d1: str) -> str:
    tag_l = str(tag or "").lower()
    if ref_tag and tag_l == ref_tag.lower():
        return "D"
    if ref_tag_d1 and tag_l == ref_tag_d1.lower():
        return "D-1"
    return "fora_do_run"


def should_include_csv(meta: Dict[str, Any], ref_tag: str, ref_tag_d1: str) -> bool:
    wanted = {str(ref_tag or "").lower(), str(ref_tag_d1 or "").lower()}
    wanted.discard("")
    return str(meta.get("tag", "")).lower() in wanted


def sum_numeric(rows: List[Dict[str, Any]], key: str) -> float:
    total = 0.0
    for row in rows:
        value = row.get(key)
        if isinstance(value, (int, float)):
            total += float(value)
    return total


def min_max_numeric(rows: List[Dict[str, Any]], key: str) -> Tuple[Optional[float], Optional[float]]:
    vals = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    if not vals:
        return None, None
    return min(vals), max(vals)


def parse_csv_file(path: str, ref_tag: str, ref_tag_d1: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    filename = os.path.basename(path)
    meta = extract_csv_metadata(filename)
    if not meta:
        return None, [f"CSV ignorado por nome fora do padrão: {filename}"]

    try:
        with open(path, "r", encoding="latin-1", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            all_rows = list(reader)
    except Exception as exc:
        return None, [f"Erro lendo CSV {filename}: {exc}"]

    if not all_rows:
        return None, [f"CSV vazio: {filename}"]

    raw_headers = [h.strip() for h in all_rows[0]]
    headers = canonical_headers(raw_headers, meta["kind"])
    rows: List[Dict[str, Any]] = []

    for raw_row in all_rows[1:]:
        if not any(str(cell).strip() for cell in raw_row):
            continue
        padded = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
        row = {headers[i]: parse_cell(padded[i]) for i in range(len(headers))}
        rows.append(row)

    strike_min, strike_max = min_max_numeric(rows, "strike")
    summary = {
        "rows": len(rows),
        "columns": len(headers),
        "strike_min": strike_min,
        "strike_max": strike_max,
        "call_oi_total": sum_numeric(rows, "call_oi"),
        "put_oi_total": sum_numeric(rows, "put_oi"),
        "call_volume_total": sum_numeric(rows, "call_volume"),
        "put_volume_total": sum_numeric(rows, "put_volume"),
    }

    payload = {
        **meta,
        "date_role": classify_role(meta["tag"], ref_tag, ref_tag_d1),
        "raw_headers": raw_headers,
        "headers": headers,
        "summary": summary,
        "rows": rows,
    }
    return payload, warnings


def load_csvs(data_dir: str, ref_tag: str, ref_tag_d1: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    files: List[Dict[str, Any]] = []

    if not data_dir or not os.path.isdir(data_dir):
        return files, [f"Diretório de CSV inexistente: {data_dir}"]

    for filename in sorted(os.listdir(data_dir)):
        if not filename.lower().endswith(".csv"):
            continue
        meta = extract_csv_metadata(filename)
        if not meta or not should_include_csv(meta, ref_tag, ref_tag_d1):
            continue
        parsed, parse_warnings = parse_csv_file(os.path.join(data_dir, filename), ref_tag, ref_tag_d1)
        warnings.extend(parse_warnings)
        if parsed:
            files.append(parsed)

    return files, warnings


DECL_RE = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=", re.MULTILINE)


def read_balanced_literal(text: str, start: int) -> Optional[Tuple[str, int]]:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "[{":
        return None

    pairs = {"[": "]", "{": "}"}
    open_chars = set(pairs)
    close_chars = set(pairs.values())
    stack = [text[start]]
    quote: Optional[str] = None
    escaped = False

    for idx in range(start + 1, len(text)):
        ch = text[idx]
        if quote:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == quote:
                quote = None
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            continue
        if ch in open_chars:
            stack.append(ch)
            continue
        if ch in close_chars:
            if not stack or pairs[stack[-1]] != ch:
                return None
            stack.pop()
            if not stack:
                return text[start:idx + 1], idx + 1
    return None


def strip_js_comments(raw: str) -> str:
    out: List[str] = []
    quote: Optional[str] = None
    escaped = False
    idx = 0
    while idx < len(raw):
        ch = raw[idx]
        nxt = raw[idx + 1] if idx + 1 < len(raw) else ""
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            idx += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            idx += 1
            continue
        if ch == "/" and nxt == "/":
            while idx < len(raw) and raw[idx] not in "\n\r":
                idx += 1
            continue
        if ch == "/" and nxt == "*":
            idx += 2
            while idx + 1 < len(raw) and not (raw[idx] == "*" and raw[idx + 1] == "/"):
                idx += 1
            idx += 2
            continue
        out.append(ch)
        idx += 1
    return "".join(out)


def quote_js_object_keys(raw: str) -> str:
    return re.sub(r"([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:", r'\1"\2":', raw)


def js_to_python_literal(raw: str) -> str:
    converted = strip_js_comments(quote_js_object_keys(raw))
    converted = re.sub(r",\s*([}\]])", r"\1", converted)
    converted = re.sub(r"\btrue\b", "True", converted)
    converted = re.sub(r"\bfalse\b", "False", converted)
    converted = re.sub(r"\bnull\b", "None", converted)
    return converted


def parse_js_literal(raw: str) -> Tuple[Any, str]:
    try:
        return json.loads(raw), "json"
    except Exception:
        pass

    cleaned = strip_js_comments(quote_js_object_keys(raw))
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(cleaned), "json_cleaned"
    except Exception:
        pass

    try:
        return ast.literal_eval(js_to_python_literal(raw)), "python_literal"
    except Exception as exc:
        return {
            "_parse_error": str(exc),
            "_raw_length": len(raw),
            "_raw_preview": raw[:5000],
        }, "raw_preview"


def data_type(value: Any) -> str:
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def count_items(value: Any) -> Optional[int]:
    if isinstance(value, (list, dict)):
        return len(value)
    return None


def make_preview(value: Any, max_items: int = 5) -> Any:
    if isinstance(value, list):
        return value[:max_items]
    if isinstance(value, dict):
        return {k: value[k] for k in list(value.keys())[:max_items]}
    return value


def extract_html_variables(html_text: str) -> Dict[str, Any]:
    extracted: Dict[str, Any] = {}
    variables: List[Dict[str, Any]] = []
    pos = 0

    while True:
        match = DECL_RE.search(html_text, pos)
        if not match:
            break
        name = match.group(1)
        literal = read_balanced_literal(html_text, match.end())
        if not literal:
            pos = match.end()
            continue
        raw, end_pos = literal
        parsed, status = parse_js_literal(raw)
        extracted[name] = parsed
        variables.append({
            "name": name,
            "type": data_type(parsed),
            "items": count_items(parsed),
            "parse_status": status,
            "raw_chars": len(raw),
            "preview": make_preview(parsed),
        })
        pos = end_pos

    return {"variables": variables, "data": extracted}


def load_htmls(output_dir: str, html_files: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    files: List[Dict[str, Any]] = []

    if not html_files:
        html_files = [
            name for name in sorted(os.listdir(output_dir))
            if name.endswith(".html") and name not in ("index.html", "bova11_arquivos.html")
        ]
        warnings.append("Nenhuma lista explícita de HTMLs recebida; fallback usou todos os HTMLs do output.")

    seen = set()
    for filename in html_files:
        filename = os.path.basename(filename.strip())
        if not filename or filename in seen:
            continue
        seen.add(filename)
        path = os.path.join(output_dir, filename)
        if not os.path.exists(path):
            warnings.append(f"HTML não encontrado no run atual: {filename}")
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                html_text = f.read()
        except Exception as exc:
            warnings.append(f"Erro lendo HTML {filename}: {exc}")
            continue

        extracted = extract_html_variables(html_text)
        files.append({
            "filename": filename,
            "size_bytes": os.path.getsize(path),
            "variable_count": len(extracted["variables"]),
            "variables": extracted["variables"],
            "data": extracted["data"],
        })

    return files, warnings


def build_summary(csv_files: List[Dict[str, Any]], html_files: List[Dict[str, Any]], warnings: List[str]) -> Dict[str, Any]:
    csv_rows = sum(int(item.get("summary", {}).get("rows", 0)) for item in csv_files)
    html_vars = sum(int(item.get("variable_count", 0)) for item in html_files)
    return {
        "csv_files": len(csv_files),
        "csv_rows": csv_rows,
        "html_files": len(html_files),
        "html_variables": html_vars,
        "warnings": len(warnings),
    }


def build_payload(args) -> Dict[str, Any]:
    html_files = [name.strip() for name in str(args.html_files or "").split(",") if name.strip()]
    warnings: List[str] = []

    csv_files, csv_warnings = load_csvs(args.data_dir, args.ref_tag, args.ref_tag_d1)
    html_payloads, html_warnings = load_htmls(args.output_dir, html_files)
    warnings.extend(csv_warnings)
    warnings.extend(html_warnings)

    summary = build_summary(csv_files, html_payloads, warnings)
    return {
        "version": 2,
        "metadata": {
            "generated_at": agora_iso(),
            "scope": "run_atual",
            "ref_date": args.ref_date,
            "ref_tag": args.ref_tag,
            "ref_tag_d1": args.ref_tag_d1,
            "spot_d": args.spot_d,
            "spot_d1": args.spot_d1,
            "data_dir": os.path.abspath(args.data_dir) if args.data_dir else None,
            "output_dir": os.path.abspath(args.output_dir),
        },
        "summary": summary,
        "csv": {
            "files": csv_files,
        },
        "html": {
            "files": html_payloads,
        },
        "concepts_markdown": CONCEITOS_MD.strip(),
        "warnings": warnings,
    }


def construir_markdown(payload: Dict[str, Any]) -> str:
    meta = payload["metadata"]
    summary = payload["summary"]
    md_lines = [
        f"# Arquivo de Conhecimento BOVA11 — {meta['ref_date']}",
        f"**Gerado em:** {meta['generated_at']}",
        f"**Preço D:** {meta['spot_d']} | **Preço D-1:** {meta['spot_d1']}",
        f"**Tags:** D `{meta['ref_tag']}` | D-1 `{meta['ref_tag_d1']}`",
        "",
        "## Resumo do Artefato",
        f"- Escopo: `{meta['scope']}`",
        f"- CSVs parseados: {summary['csv_files']} arquivos / {summary['csv_rows']} linhas",
        f"- HTMLs parseados: {summary['html_files']} arquivos / {summary['html_variables']} variáveis",
        f"- Avisos: {summary['warnings']}",
        "",
        "---",
        CONCEITOS_MD.strip(),
        "---",
        "## CSVs Parseados",
        "",
    ]

    for file_payload in payload["csv"]["files"]:
        md_lines.append(f"### {file_payload['filename']}")
        md_lines.append(
            f"- Vencimento: {file_payload['expiration']} | Tipo: {file_payload['kind']} | "
            f"Data: {file_payload['tag']} ({file_payload['date_role']})"
        )
        md_lines.append("**Resumo:**")
        md_lines.append("```json")
        md_lines.append(json.dumps(file_payload["summary"], ensure_ascii=False, indent=2))
        md_lines.append("```")
        md_lines.append("**Linhas:**")
        md_lines.append("```json")
        md_lines.append(json.dumps(file_payload["rows"], ensure_ascii=False, indent=2))
        md_lines.append("```")
        md_lines.append("")

    md_lines.extend(["---", "## HTMLs Parseados", ""])

    for file_payload in payload["html"]["files"]:
        md_lines.append(f"### {file_payload['filename']}")
        md_lines.append(f"- Tamanho: {file_payload['size_bytes']} bytes | Variáveis: {file_payload['variable_count']}")
        md_lines.append("**Variáveis encontradas:**")
        md_lines.append("```json")
        md_lines.append(json.dumps(file_payload["variables"], ensure_ascii=False, indent=2))
        md_lines.append("```")
        md_lines.append("**Dados extraídos:**")
        md_lines.append("```json")
        md_lines.append(json.dumps(file_payload["data"], ensure_ascii=False, indent=2))
        md_lines.append("```")
        md_lines.append("")

    if payload["warnings"]:
        md_lines.extend(["---", "## Avisos", ""])
        for warning in payload["warnings"]:
            md_lines.append(f"- {warning}")

    return "\n".join(md_lines).rstrip() + "\n"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_text(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def load_manifest(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"version": 1, "updated_at": None, "files": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        if isinstance(manifest, dict) and isinstance(manifest.get("files"), list):
            return manifest
    except Exception:
        pass
    return {"version": 1, "updated_at": None, "files": []}


def update_manifest(manifest_path: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    manifest = load_manifest(manifest_path)
    files = [item for item in manifest.get("files", []) if item.get("id") != entry["id"]]
    files.append(entry)
    files.sort(key=lambda item: item.get("ref_date", ""), reverse=True)
    manifest = {
        "version": 1,
        "updated_at": agora_iso(),
        "files": files,
    }
    write_json(manifest_path, manifest)
    return manifest


def gerar_html_visualizador(out_path: str, generated_at: str) -> None:
    safe_generated = html.escape(generated_at)
    html_doc = """<!DOCTYPE html>
<html lang="pt-BR" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BOVA11 - Arquivos</title>
  <style>
    :root {
      --bg: #f5f7fa;
      --surface: #ffffff;
      --surface-2: #eef3f8;
      --border: #d8e0e8;
      --text: #111827;
      --muted: #64748b;
      --soft: #94a3b8;
      --accent: #2563eb;
      --accent-2: #0f766e;
      --danger: #b42318;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --font: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    [data-theme="dark"] {
      --bg: #0f141b;
      --surface: #151c25;
      --surface-2: #1b2531;
      --border: #2a3544;
      --text: #e5edf6;
      --muted: #a2adbb;
      --soft: #748196;
      --accent: #6ea8ff;
      --accent-2: #5eead4;
      --danger: #f87171;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      overflow: hidden;
    }
    button { font: inherit; }
    .shell {
      display: grid;
      grid-template-columns: minmax(230px, 280px) 1fr;
      height: 100dvh;
      min-height: 0;
    }
    .sidebar {
      border-right: 1px solid var(--border);
      background: var(--surface);
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .side-head {
      padding: 22px 18px 16px;
      border-bottom: 1px solid var(--border);
    }
    .kicker {
      font-family: var(--mono);
      font-size: 0.68rem;
      letter-spacing: 0.1em;
      color: var(--soft);
      text-transform: uppercase;
      margin-bottom: 8px;
    }
    h1, h2, h3, p { margin: 0; }
    h1 {
      font-size: 1.22rem;
      line-height: 1.12;
      letter-spacing: -0.03em;
    }
    .side-head p {
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.45;
      margin-top: 8px;
    }
    .file-list {
      padding: 12px;
      overflow: auto;
      min-height: 0;
      display: grid;
      gap: 6px;
    }
    .file-btn {
      width: 100%;
      text-align: left;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text);
      border-radius: 8px;
      padding: 10px 11px;
      cursor: pointer;
      display: grid;
      gap: 4px;
    }
    .file-btn:hover { background: var(--surface-2); }
    .file-btn.active {
      border-color: color-mix(in srgb, var(--accent) 45%, transparent);
      background: color-mix(in srgb, var(--accent) 14%, transparent);
    }
    .file-date { font-weight: 700; font-size: 0.92rem; }
    .file-meta { color: var(--muted); font-size: 0.74rem; font-family: var(--mono); }
    .main {
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .top {
      border-bottom: 1px solid var(--border);
      background: var(--surface);
      padding: 18px 22px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
    }
    .title-block {
      min-width: 0;
      display: grid;
      gap: 8px;
    }
    .title-line {
      display: flex;
      align-items: baseline;
      gap: 12px;
      flex-wrap: wrap;
    }
    h2 {
      font-size: 1.28rem;
      line-height: 1.1;
      letter-spacing: -0.03em;
    }
    .date-pill {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 0.75rem;
      white-space: nowrap;
    }
    .scope {
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.45;
    }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .btn {
      border: 1px solid var(--border);
      background: var(--surface-2);
      color: var(--text);
      border-radius: 8px;
      padding: 9px 12px;
      cursor: pointer;
      min-height: 38px;
    }
    .btn:hover { border-color: var(--accent); color: var(--accent); }
    .btn.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    [data-theme="dark"] .btn.primary { color: #07111f; }
    .work {
      overflow: auto;
      min-height: 0;
      padding: 22px;
      display: grid;
      gap: 20px;
      align-content: start;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 1px;
      border: 1px solid var(--border);
      background: var(--border);
      border-radius: 8px;
      overflow: hidden;
    }
    .stat {
      background: var(--surface);
      padding: 15px;
      display: grid;
      gap: 5px;
      min-width: 0;
    }
    .stat b {
      font-size: 1.25rem;
      line-height: 1;
      letter-spacing: -0.03em;
    }
    .stat span {
      font-family: var(--mono);
      color: var(--muted);
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.07em;
    }
    .section {
      display: grid;
      gap: 10px;
    }
    .section h3 {
      font-size: 0.96rem;
      letter-spacing: -0.01em;
    }
    .table-wrap {
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: auto;
      background: var(--surface);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      font-size: 0.83rem;
    }
    th {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      background: var(--surface-2);
    }
    tr:last-child td { border-bottom: none; }
    code, pre {
      font-family: var(--mono);
      font-size: 0.76rem;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      max-height: 220px;
      overflow: auto;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
    }
    details {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    summary {
      cursor: pointer;
      padding: 12px 14px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--text);
      font-weight: 700;
    }
    summary span {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 0.75rem;
      font-weight: 500;
    }
    .detail-body {
      padding: 0 14px 14px;
      display: grid;
      gap: 10px;
    }
    .notice {
      border-left: 3px solid var(--accent);
      background: color-mix(in srgb, var(--accent) 10%, transparent);
      padding: 12px 14px;
      color: var(--text);
      font-size: 0.86rem;
      line-height: 1.45;
    }
    .notice.error {
      border-left-color: var(--danger);
      background: color-mix(in srgb, var(--danger) 12%, transparent);
    }
    .empty {
      color: var(--muted);
      border: 1px dashed var(--border);
      border-radius: 8px;
      padding: 18px;
      background: var(--surface);
    }
    @media (max-width: 900px) {
      body { overflow: auto; }
      .shell {
        grid-template-columns: 1fr;
        height: auto;
        min-height: 100dvh;
      }
      .sidebar {
        min-height: auto;
        border-right: none;
        border-bottom: 1px solid var(--border);
      }
      .file-list {
        grid-auto-flow: column;
        grid-auto-columns: minmax(180px, 1fr);
        overflow-x: auto;
      }
      .main { min-height: 0; }
      .top { flex-direction: column; }
      .actions { justify-content: flex-start; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .work { padding: 16px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="side-head">
        <div class="kicker">BOVA11 Arquivos</div>
        <h1>Contexto parseado para IA</h1>
        <p>Histórico gerado a partir dos CSVs e HTMLs publicados no dashboard.</p>
      </div>
      <div class="file-list" id="file-list"></div>
    </aside>

    <main class="main">
      <header class="top">
        <div class="title-block">
          <div class="title-line">
            <h2 id="current-title">Carregando</h2>
            <span class="date-pill" id="current-date">__GENERATED_AT__</span>
          </div>
          <p class="scope" id="scope-text">Run atual, com preview leve e arquivos completos disponíveis para cópia e download.</p>
        </div>
        <div class="actions">
          <button class="btn" id="theme-btn" type="button">Tema</button>
          <button class="btn primary" id="copy-md" type="button">Copiar MD</button>
          <button class="btn" id="download-md" type="button">Baixar MD</button>
          <button class="btn" id="download-json" type="button">Baixar JSON</button>
        </div>
      </header>

      <section class="work">
        <div id="notice"></div>
        <div class="stats" id="stats"></div>
        <section class="section">
          <h3>CSVs do Run</h3>
          <div id="csv-preview"></div>
        </section>
        <section class="section">
          <h3>HTMLs do Dashboard</h3>
          <div id="html-preview"></div>
        </section>
        <section class="section">
          <h3>Avisos</h3>
          <div id="warnings"></div>
        </section>
      </section>
    </main>
  </div>

  <script>
    const state = {
      manifest: null,
      currentEntry: null,
      currentPayload: null,
      markdownCache: new Map()
    };

    const fmt = new Intl.NumberFormat('pt-BR');

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function setNotice(message, isError = false) {
      document.getElementById('notice').innerHTML = message
        ? `<div class="notice ${isError ? 'error' : ''}">${message}</div>`
        : '';
    }

    async function fetchJson(path) {
      const res = await fetch(`${path}?ts=${Date.now()}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }

    async function fetchText(path) {
      const res = await fetch(`${path}?ts=${Date.now()}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.text();
    }

    function renderFileList() {
      const list = document.getElementById('file-list');
      const files = state.manifest?.files || [];
      if (!files.length) {
        list.innerHTML = '<div class="empty">Nenhum artefato publicado em output/arquivos.</div>';
        return;
      }
      list.innerHTML = files.map((entry, index) => `
        <button class="file-btn ${index === 0 ? 'active' : ''}" type="button" data-id="${escapeHtml(entry.id)}">
          <span class="file-date">${escapeHtml(entry.ref_date)}</span>
          <span class="file-meta">${fmt.format(entry.summary?.csv_files || 0)} CSVs / ${fmt.format(entry.summary?.html_files || 0)} HTMLs</span>
        </button>
      `).join('');
      list.querySelectorAll('.file-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          list.querySelectorAll('.file-btn').forEach(el => el.classList.remove('active'));
          btn.classList.add('active');
          selectEntry(btn.dataset.id);
        });
      });
    }

    function stat(label, value) {
      return `<div class="stat"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`;
    }

    function renderStats(payload) {
      const s = payload.summary || {};
      document.getElementById('stats').innerHTML = [
        stat('CSVs', fmt.format(s.csv_files || 0)),
        stat('Linhas CSV', fmt.format(s.csv_rows || 0)),
        stat('HTMLs', fmt.format(s.html_files || 0)),
        stat('Variáveis JS', fmt.format(s.html_variables || 0)),
        stat('Avisos', fmt.format(s.warnings || 0))
      ].join('');
    }

    function jsonPreview(value, limit = 2200) {
      const text = JSON.stringify(value, null, 2) || '';
      return escapeHtml(text.length > limit ? `${text.slice(0, limit)}\\n...` : text);
    }

    function renderCsvPreview(payload) {
      const files = payload.csv?.files || [];
      if (!files.length) {
        document.getElementById('csv-preview').innerHTML = '<div class="empty">Nenhum CSV encontrado para as tags deste run.</div>';
        return;
      }
      document.getElementById('csv-preview').innerHTML = files.map(file => {
        const rows = (file.rows || []).slice(0, 8);
        return `
          <details>
            <summary>
              ${escapeHtml(file.filename)}
              <span>${escapeHtml(file.kind)} / ${fmt.format(file.summary?.rows || 0)} linhas</span>
            </summary>
            <div class="detail-body">
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Vencimento</th>
                      <th>Data</th>
                      <th>Strikes</th>
                      <th>OI Call</th>
                      <th>OI Put</th>
                      <th>Volume Call</th>
                      <th>Volume Put</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>${escapeHtml(file.expiration)}</td>
                      <td>${escapeHtml(file.tag)} (${escapeHtml(file.date_role)})</td>
                      <td>${escapeHtml(file.summary?.strike_min)} - ${escapeHtml(file.summary?.strike_max)}</td>
                      <td>${fmt.format(Math.round(file.summary?.call_oi_total || 0))}</td>
                      <td>${fmt.format(Math.round(file.summary?.put_oi_total || 0))}</td>
                      <td>${fmt.format(Math.round(file.summary?.call_volume_total || 0))}</td>
                      <td>${fmt.format(Math.round(file.summary?.put_volume_total || 0))}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <pre>${jsonPreview(rows)}</pre>
            </div>
          </details>
        `;
      }).join('');
    }

    function renderHtmlPreview(payload) {
      const files = payload.html?.files || [];
      if (!files.length) {
        document.getElementById('html-preview').innerHTML = '<div class="empty">Nenhum HTML foi parseado para este run.</div>';
        return;
      }
      document.getElementById('html-preview').innerHTML = files.map(file => {
        const variables = file.variables || [];
        const varRows = variables.slice(0, 14).map(v => `
          <tr>
            <td><code>${escapeHtml(v.name)}</code></td>
            <td>${escapeHtml(v.type)}</td>
            <td>${escapeHtml(v.items ?? '-')}</td>
            <td>${escapeHtml(v.parse_status)}</td>
            <td>${fmt.format(v.raw_chars || 0)}</td>
          </tr>
        `).join('');
        return `
          <details>
            <summary>
              ${escapeHtml(file.filename)}
              <span>${fmt.format(file.variable_count || 0)} variáveis</span>
            </summary>
            <div class="detail-body">
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Variável</th>
                      <th>Tipo</th>
                      <th>Itens</th>
                      <th>Status</th>
                      <th>Chars</th>
                    </tr>
                  </thead>
                  <tbody>${varRows || '<tr><td colspan="5">Sem variáveis extraídas.</td></tr>'}</tbody>
                </table>
              </div>
              <pre>${jsonPreview(variables.slice(0, 5))}</pre>
            </div>
          </details>
        `;
      }).join('');
    }

    function renderWarnings(payload) {
      const warnings = payload.warnings || [];
      document.getElementById('warnings').innerHTML = warnings.length
        ? `<pre>${escapeHtml(warnings.join('\\n'))}</pre>`
        : '<div class="empty">Nenhum aviso registrado neste artefato.</div>';
    }

    async function selectEntry(id) {
      const entry = (state.manifest?.files || []).find(item => item.id === id);
      if (!entry) return;
      state.currentEntry = entry;
      setNotice('');
      try {
        const payload = await fetchJson(entry.json_path);
        state.currentPayload = payload;
        document.getElementById('current-title').textContent = `Arquivos ${payload.metadata?.ref_date || entry.ref_date}`;
        document.getElementById('current-date').textContent = payload.metadata?.generated_at || entry.generated_at || '';
        document.getElementById('scope-text').textContent =
          `Escopo: ${payload.metadata?.scope || 'run_atual'} / D ${payload.metadata?.ref_tag || '-'} / D-1 ${payload.metadata?.ref_tag_d1 || '-'}`;
        renderStats(payload);
        renderCsvPreview(payload);
        renderHtmlPreview(payload);
        renderWarnings(payload);
      } catch (err) {
        setNotice(`Não foi possível carregar ${escapeHtml(entry.json_path)}. Sirva a pasta output por HTTP ou use GitHub Pages.`, true);
      }
    }

    async function copyMarkdown() {
      if (!state.currentEntry) return;
      const path = state.currentEntry.markdown_path;
      try {
        let text = state.markdownCache.get(path);
        if (!text) {
          text = await fetchText(path);
          state.markdownCache.set(path, text);
        }
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const area = document.createElement('textarea');
          area.value = text;
          document.body.appendChild(area);
          area.select();
          document.execCommand('copy');
          area.remove();
        }
        setNotice('Markdown completo copiado.');
      } catch (err) {
        setNotice('Falha ao copiar o Markdown completo.', true);
      }
    }

    function download(path) {
      if (!path) return;
      const a = document.createElement('a');
      a.href = path;
      a.download = path.split('/').pop();
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    function applySavedTheme() {
      try {
        const saved = localStorage.getItem('bova11-theme');
        if (saved === 'light') document.documentElement.setAttribute('data-theme', 'light');
        if (saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
      } catch (err) {}
    }

    function toggleTheme() {
      const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('bova11-theme', next); } catch (err) {}
    }

    async function init() {
      applySavedTheme();
      document.getElementById('theme-btn').addEventListener('click', toggleTheme);
      document.getElementById('copy-md').addEventListener('click', copyMarkdown);
      document.getElementById('download-md').addEventListener('click', () => download(state.currentEntry?.markdown_path));
      document.getElementById('download-json').addEventListener('click', () => download(state.currentEntry?.json_path));

      try {
        state.manifest = await fetchJson('arquivos/manifest.json');
        renderFileList();
        const first = state.manifest.files?.[0];
        if (first) await selectEntry(first.id);
      } catch (err) {
        setNotice('Manifest não encontrado em arquivos/manifest.json. Gere novamente o módulo Arquivos ou publique output/arquivos.', true);
        document.getElementById('stats').innerHTML = '';
        document.getElementById('csv-preview').innerHTML = '<div class="empty">Sem dados carregados.</div>';
        document.getElementById('html-preview').innerHTML = '<div class="empty">Sem dados carregados.</div>';
        document.getElementById('warnings').innerHTML = '<div class="empty">Sem dados carregados.</div>';
      }
    }

    init();
  </script>
</body>
</html>
"""
    html_doc = html_doc.replace("__GENERATED_AT__", safe_generated)
    write_text(out_path, html_doc)


def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ref-date", required=True)
    parser.add_argument("--spot-d", required=True, type=float)
    parser.add_argument("--spot-d1", required=True, type=float)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--ref-tag", default="")
    parser.add_argument("--ref-tag-d1", default="")
    parser.add_argument("--html-files", default="")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    args.output_dir = os.path.abspath(args.output_dir)
    args.output = os.path.abspath(args.output)
    if args.data_dir is None:
        args.data_dir = os.path.join(os.path.dirname(args.output_dir), "data")
    else:
        args.data_dir = os.path.abspath(args.data_dir)

    base_dir = os.path.dirname(args.output_dir)
    legacy_arquivos_dir = os.path.join(base_dir, "arquivos")
    public_arquivos_dir = os.path.join(args.output_dir, "arquivos")
    ensure_dir(legacy_arquivos_dir)
    ensure_dir(public_arquivos_dir)

    print(f"\n[BOVA11 Arquivos] Processando run atual (Data: {args.ref_date})...")
    payload = build_payload(args)
    md_text = construir_markdown(payload)

    artifact_id = f"bova11_{args.ref_date}"
    public_json = os.path.join(public_arquivos_dir, f"{artifact_id}.json")
    public_md = os.path.join(public_arquivos_dir, f"{artifact_id}.md")
    legacy_md = os.path.join(legacy_arquivos_dir, f"{artifact_id}.md")
    manifest_path = os.path.join(public_arquivos_dir, "manifest.json")

    write_json(public_json, payload)
    write_text(public_md, md_text)
    write_text(legacy_md, md_text)

    entry = {
        "id": artifact_id,
        "ref_date": args.ref_date,
        "ref_tag": args.ref_tag,
        "ref_tag_d1": args.ref_tag_d1,
        "generated_at": payload["metadata"]["generated_at"],
        "json_path": f"arquivos/{artifact_id}.json",
        "markdown_path": f"arquivos/{artifact_id}.md",
        "json_bytes": os.path.getsize(public_json),
        "markdown_bytes": os.path.getsize(public_md),
        "summary": payload["summary"],
    }
    update_manifest(manifest_path, entry)
    gerar_html_visualizador(args.output, payload["metadata"]["generated_at"])

    print(f"[OK] CSVs parseados: {payload['summary']['csv_files']} arquivos / {payload['summary']['csv_rows']} linhas")
    print(f"[OK] HTMLs parseados: {payload['summary']['html_files']} arquivos / {payload['summary']['html_variables']} variáveis")
    if payload["warnings"]:
        print(f"[WARN] {len(payload['warnings'])} aviso(s) registrados no artefato.")
    print(f"[OK] JSON público: {public_json}")
    print(f"[OK] Markdown público: {public_md}")
    print(f"[OK] Markdown legado: {legacy_md}")
    print(f"[OK] Manifest: {manifest_path}")
    print(f"[OK] Visualizador IA atualizado em: {args.output}")
    print("\n[✓] Módulo 20 - Arquivos finalizado com sucesso!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
