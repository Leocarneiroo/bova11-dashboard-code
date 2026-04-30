# BOVA11 Dashboard — Versão Automatizada

Esta versão automatizada dos scripts elimina a necessidade de editar o código manualmente a cada dia.

## 📁 Novos Arquivos

| Arquivo | Descrição |
|---------|-----------|
| `bova11_auto.py` | Gera Rankings + GEX (versão auto) |
| `bova11_insights_auto.py` | Gera Insights narrativos (versão auto) |

## 🚀 Como Usar

### Rodar os scripts

Cada script é independente e solicitará apenas o spot price:

```bash
# Gerar Rankings + GEX
python3 bova11_auto.py

# Gerar Insights (em outro terminal ou após o primeiro)
python3 bova11_insights_auto.py
```

Cada script irá:
1. Detectar automaticamente as datas D-1 e D nos arquivos CSV
2. Pedir o spot price do BOVA11
3. Gerar seu respectivo HTML

## 📂 Organização dos Arquivos CSV

Coloque os arquivos CSV no mesmo diretório dos scripts. Os nomes devem seguir o padrão:

### Padrão com underscores:
```
venc_20_fev_Mensal_fechamento__13fev_.csv
venc_20_fev_Mensal_fechamento__13fev_Volume_.csv
venc_20_fev_Mensal_fechamento__12fev_.csv
venc_20_fev_Mensal_fechamento__12fev_Volume_.csv
```

### Padrão com espaços:
```
venc 20 fev Mensal fechamento (13fev).csv
venc 20 fev Mensal fechamento (13fev Volume).csv
venc 20 fev Mensal fechamento (12fev).csv
venc 20 fev Mensal fechamento (12fev Volume).csv
```

## 🔍 Como Funciona a Detecção Automática

O script detecta as datas automaticamente baseado na **data de modificação** dos arquivos:

1. Encontra todos os arquivos `*fechamento*` no diretório
2. Extrai as tags de data (ex: `12fev`, `13fev`)
3. Ordena por data de modificação (mais antigo = D-1, mais novo = D)
4. Usa as duas datas mais recentes para comparação

> **Nota:** Se houver apenas uma data, o script roda em modo "snapshot" (apenas D, sem comparação D-1).

## ⚙️ Configuração

### VENC_DATES (para cálculo de DTE)

No arquivo `bova11_insights_auto.py`, atualize o dicionário `VENC_DATES` com as datas de vencimento:

```python
VENC_DATES = {
    "13 fev W2":     "2025-02-13",
    "20 fev Mensal": "2025-02-20",
    "27 fev W4":     "2025-02-27",
    # Adicione novos vencimentos conforme necessário
}
```

### Outros ajustes

Se necessário, você pode ajustar:

```python
ANO = "2025"           # Ano atual
GEX_STRIKE_MIN = 160   # Strike mínimo para análise GEX
GEX_STRIKE_MAX = 200   # Strike máximo para análise GEX
```

## 📊 Saída Gerada

Após executar ambos os scripts, você terá 3 arquivos HTML:

```
bova11_rankings_pro_<TAG_D1>_vs_<TAG_D>.html   # Rankings e tabelas
bova11_gex_pro_<TAG_D1>_vs_<TAG_D>.html        # Análise GEX
bova11_insights_<TAG_D1>_vs_<TAG_D>.html       # Insights narrativos
```

Abra qualquer um deles no navegador para visualizar.

## 🔄 Fluxo de Trabalho Diário

1. **Baixe os arquivos CSV** do dia da B3
2. **Coloque na pasta** do projeto
3. **Execute:** `python3 bova11_auto.py`
4. **Digite o spot** quando solicitado
5. **Execute:** `python3 bova11_insights_auto.py`
6. **Digite o spot** novamente
7. **Pronto!** Os HTMLs serão gerados automaticamente

## ❓ Dúvidas

**Q: E se eu quiser usar uma data específica em vez da auto-detecção?**  
A: Use os scripts originais (`bova11.py` e `bova11_insights.py`) e edite a seção CONFIG manualmente.

**Q: Posso rodar sem ter os arquivos do dia anterior (D-1)?**  
A: Sim! O script detecta automaticamente e roda em modo snapshot se só houver uma data.

**Q: O que acontece se houver mais de 2 datas?**  
A: O script usa as duas mais recentes (mais novas) para D-1 e D.
