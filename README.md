# ScalperFlow Bot — MetaTrader 5

Bot automatizado de scalping para MetaTrader 5 operando **XAUUSDz** e **BTCUSDz**.

---

## Estrategias

### XAUUSDz — ScalperFlow
- Timeframes: **M3 + M5**
- Sinal: Cruzamento EMA20 x EMA50 + detector de Absorcao
- Lote: `0.10`
- SL: `2.0x ATR`

### BTCUSDz — Filtered EMA
- Timeframe: **M15**
- Sinal: Cruzamento EMA20 x EMA50
- Filtros: RSI > 50 (BUY) / RSI < 50 (SELL) + Tendencia H1 + Sessao NY (13h-21h UTC)
- Lote: `0.20`
- SL: `1.5x ATR`

---

## Gestao de Risco (ambos os pares)

| Nivel | Acao |
|-------|------|
| TP1 (2x ATR) | SL move para breakeven |
| TP2 (3.5x ATR) | SL move para TP1 |
| TP3 (5x ATR) | Fecha posicao |
| Trailing | Ativo antes do TP1 (1x ATR) |

---

## Instalacao

```bash
pip install -r requirements.txt
```

### Configuracao

Edite as credenciais no inicio do `scalperflow_bot.py`:

```python
LOGIN    = SEU_LOGIN
PASSWORD = 'SUA_SENHA'
SERVER   = 'SEU_SERVIDOR'
PATH     = 'CAMINHO/terminal64.exe'
```

### Executar

```bash
python scalperflow_bot.py
```

---

## Estrutura do Projeto

```
scalperflow-bot/
├── scalperflow_bot.py          # Bot principal
├── requirements.txt
├── .gitignore
├── README.md
├── backtests/                  # Scripts de backtest
│   ├── backtest.py             # Backtest inicial — EMA crossover variações
│   ├── backtest2.py            # Backtest com RSI / H1 / sessão
│   ├── backtest_4anos.py       # Simulação 5 anos BTCUSDz ($200 / 0.20 lot)
│   ├── backtest_nova_estrategia.py  # Bollinger + Pullback (exploratório)
│   ├── bt3.py                  # Backtest focado (WR > 58%, min 12 trades)
│   └── results/                # Resultados salvos dos backtests
│       ├── backtest_result.txt
│       └── backtest_nova_result.txt
└── tools/                      # Ferramentas auxiliares
    ├── dashboard_xauusd.py     # Dashboard XAUUSD
    ├── monitor_xauusd.py       # Monitor de mercado
    └── ghs_indicator.py        # Indicador GHS
```

---

## Resultados Backtest (BTCUSDz — 5 anos)

Periodo: Jan/2021 a Abr/2026 | Capital: $200 | Lote: 0.20

| Ano  | Trades | WR%   | Lucro USD | Capital |
|------|--------|-------|-----------|---------|
| 2021 | 85     | 24.7% | +$543     | $743    |
| 2022 | 111    | 22.5% | -$524     | $219    |
| 2023 | 102    | 20.6% | -$402     | -$183   |
| 2024 | 111    | 31.5% | +$1.462   | $1.280  |
| 2025 | 101    | 30.7% | +$1.819   | $3.099  |
| 2026 | 25     | 40.0% | +$1.118   | $4.218  |

**Retorno total: +2.009% ($200 -> $4.218)**
Profit Factor: 1.19 | Win Rate: 26.7%

> A estrategia perde pequeno (media -$29/SL) e ganha grande (media +$237/TP3).
> Para suportar os anos negativos (2022-2023), recomenda-se capital minimo de $500.

---

## Requisitos

- Python 3.8+
- MetaTrader 5 instalado e logado
- Conta com acesso aos pares XAUUSDz e BTCUSDz
