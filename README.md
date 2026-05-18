# BOVA11 Dashboard Code

Código-fonte público do dashboard de opções BOVA11.

Este repositório é um espelho seguro do projeto principal, pensado para compartilhar a lógica, os scripts e a estrutura do sistema sem expor conteúdo local sensível ou operacional.

## O que este projeto faz

O projeto lê arquivos CSV diários de opções da B3 e gera dashboards HTML com múltiplos módulos de análise, incluindo:

- OI, GEX e Max Pain
- Insights narrativos
- Histórico de skew / IV
- Convexidade (Gamma, Vanna, Charm)
- Trade score e viés institucional
- Perfis estruturais por spot para GEX, DEX, VEX, TEX e CEX
- Demand Flow, Diagnóstico 4D, Bandas de Volatilidade
- Visualizador de arquivos e contexto para IA

## Estrutura

```text
.
├── bova11_runner.py
├── scripts/
├── tests/
├── docs/
└── .gitignore
```

## Como rodar

### Runner principal

```bash
python3 bova11_runner.py
```

O runner detecta os arquivos disponíveis, pede os spots necessários e gera os HTMLs do dashboard.

### Exemplo de módulo individual

```bash
python3 scripts/bova11_tex_dex_vex.py
```

## Dependências

Grande parte dos scripts usa apenas biblioteca padrão do Python.
Alguns módulos exigem bibliotecas extras, como:

```bash
pip install yfinance pandas numpy
```

Alguns fluxos podem usar também `arch`, dependendo do módulo de volatilidade.

## Dados de entrada

Os arquivos de mercado **não** fazem parte deste repositório público.
Para executar o projeto completo, você precisa fornecer seus CSVs locais da B3 no formato esperado pelo projeto.

Também ficaram fora deste espelho:

- `data/`
- `history/`
- `output/`
- `arquivos/`
- estado local de ferramentas/agentes

## Repositórios relacionados

- Dashboard publicado: [https://github.com/Leocarneiroo/bova11-dashboard](https://leocarneiroo.github.io/bova11-dashboard/)
- Código privado/original de trabalho: mantido separadamente

## Objetivo deste espelho público

Este repositório existe para:

- compartilhar a implementação dos módulos
- facilitar revisão de código
- documentar a arquitetura do dashboard
- publicar o código sem expor snapshots históricos, dados de mercado locais ou artefatos gerados

## Observações

Alguns módulos dependem de:

- convenções específicas de nomes de arquivos CSV
- datas de referência extraídas dos nomes dos arquivos
- spots informados manualmente no runner

Sem esses insumos, parte do sistema pode rodar apenas parcialmente.

## Testes

Exemplo de teste unitário já incluído:

```bash
python3 -m unittest tests/test_bova11_tex_dex_vex.py
```

## Licença

Sem licença explícita no momento.
