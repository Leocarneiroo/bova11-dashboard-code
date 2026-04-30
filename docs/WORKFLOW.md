# BOVA11 Options Analysis — Documentação do Sistema

## Visão Geral

Sistema de análise de opções do BOVA11 com 5 módulos independentes, orquestrados por um runner central. Todos os dados são lidos diretamente dos arquivos CSV; nenhuma configuração manual de datas é necessária.

---

## Estrutura de Arquivos

```
files/
├── bova11_runner.py            ← 🚀 PONTO DE ENTRADA PRINCIPAL
│
├── scripts/                    ← Módulos de análise
│   ├── bova11_auto.py          ← Módulo 1: Dashboard (OI, GEX, MaxPain)
│   ├── bova11_insights_auto.py ← Módulo 2: Insights narrativos
│   ├── bova11_skew_history.py  ← Módulo 3: Histórico de IV/Skew
│   ├── bova11_convexity.py     ← Módulo 4: Convexidade (Vanna/Gamma/Charm)
│   └── bova11_trade_score_james.py ← Módulo 5: Trade Score & Bias
│
├── data/                       ← 📥 Arquivos CSV da B3 (entrada)
│   └── venc *.csv
│
├── output/                     ← 📤 HTMLs gerados automaticamente (saída)
│   └── *.html
│
├── history/                    ← 🔒 Histórico acumulativo (NÃO DELETAR)
│   └── bova11_skew_history.json
│
└── docs/                       ← Documentação
    ├── WORKFLOW.md             ← Esta documentação
    └── README_AUTO.md
```

---

## Como Executar

### Execução Completa (Recomendado)

```bash
cd /Users/leonardocarneiro/Desktop/files
python3 bova11_runner.py
```

O runner vai:
1. **Auto-detectar** as datas D-1 e D pelos CSVs em `data/`
2. Pedir **Spot de D-1** e **Spot de D** (uma única vez)
3. Pedir o **range de strikes** para o Skew History (Enter = padrão 170–210)
4. Rodar os **5 módulos em sequência**, repassando os inputs automaticamente
5. Mostrar um **resumo final** com status de cada módulo

### Execução Individual

Cada módulo pode ser rodado de forma independente:

```bash
python3 scripts/bova11_auto.py
python3 scripts/bova11_insights_auto.py
python3 scripts/bova11_skew_history.py
python3 scripts/bova11_convexity.py
python3 scripts/bova11_trade_score_james.py
```

---

## Módulos em Detalhe

### Módulo 1 — `bova11_auto.py` — Dashboard Principal
- **Input:** Spot de D (único valor)
- **Output:** `output/bova11_rankings_pro_<D1>_vs_<D>.html` + `output/bova11_gex_pro_<D1>_vs_<D>.html`
- **Conteúdo:** Rankings por vencimento, GEX (Gamma Exposure), MaxPain, PCR, ΔOI, ΔVolume

### Módulo 2 — `bova11_insights_auto.py` — Insights Narrativos
- **Input:** Spot de D (único valor)
- **Output:** `output/bova11_insights_<D1>_vs_<D>.html`
- **Conteúdo:** Análise narrativa automática por vencimento com regime GEX, skew, top strikes e conclusão global

### Módulo 3 — `bova11_skew_history.py` — Histórico de IV/Skew
- **Input:** Range de strikes (padrão 170–210)
- **Output:** `output/bova11_skew_history.html` + atualiza `history/bova11_skew_history.json`
- **Conteúdo:** Evolução histórica da IV média de calls e puts, skew put-call
- ⚠️ **O JSON em `history/` é acumulativo** — não deletar, mantém o histórico completo

### Módulo 4 — `bova11_convexity.py` — Decomposição de Convexidade
- **Input:** Spot de D, depois Spot de D-1
- **Output:** `output/bova11_convexity_<D1>_vs_<D>.html`
- **Conteúdo:** Деcompõe a variação do Delta em Gamma/Spot, Vanna/IV e Charm/Time com ranking por impact score

### Módulo 5 — `bova11_trade_score_james.py` — Trade Score & Bias
- **Input:** Spot de D-1, depois Spot de D
- **Output:** `output/bova11_trade_score_<D1>_vs_<D>.html`
- **Conteúdo:** Score 0–100 por strike/opção, bias (SELL_VOL / BUY_VOL / DIRECTIONAL / NEUTRAL / AVOID) e conviction (STRONG / MODERATE / WEAK)

---

## Formato dos Arquivos CSV (B3)

Coloque os arquivos CSV na pasta `data/` seguindo o padrão:

```
data/
├── venc 6 mar W1 fechamento (4mar).csv
├── venc 6 mar W1 fechamento (4mar Volume).csv
├── venc 6 mar W1 fechamento (5mar).csv
├── venc 6 mar W1 fechamento (5mar Volume).csv
└── ...
```

O sistema detecta automaticamente as duas datas mais recentes como D-1 e D.

---

## Interpretação dos Resultados

### Trade Score (Módulo 5)

| Score | Interpretação |
|-------|---------------|
| 60–100 | Alta relevância — avaliar o bias |
| 40–59  | Relevância moderada |
| 25–39  | Baixa relevância |
| 0–24   | Ignorar |

### Bias

| Bias | Significado |
|------|-------------|
| `SELL_VOL` | IV caindo + abertura de posição + driver Vanna/Mixed |
| `BUY_VOL` | IV subindo + abertura + driver Vanna/Mixed |
| `DIRECTIONAL` | Driver Gamma + abertura clara |
| `NEUTRAL` | Sem sinal claro |
| `AVOID` | Charm dominante (perto do vencimento) ou score baixo |

### Conviction

| Conviction | Critério |
|------------|----------|
| `STRONG` | Inst Score > 25 **E** Flow Score > 15 |
| `MODERATE` | Apenas um dos dois |
| `WEAK` | Nenhum dos dois |

### GEX e Regime

- **GEX Positivo + Spot acima do Flip** → Long Gamma → movimentos amortecidos
- **GEX Negativo + Spot abaixo do Flip** → Short Gamma → movimentos amplificados

---

## Dependências

Apenas Python 3 padrão — nenhuma instalação necessária:

```
os, sys, re, glob, json, math, subprocess, datetime
```

---

*Gerado em 06/Mar/2026 — Sistema BOVA11 Options Analysis v2.0*
