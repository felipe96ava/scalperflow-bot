"""
ScalperFlow Backtest
Testa combinacoes de: timeframe, SL, filtro de volume, EMA
Relatorio completo por configuracao
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from itertools import product

LOGIN  = 83090423
PASSWORD = 'aEzakmi931018@'
SERVER = 'Exness-MT5Trial12'
PATH   = 'C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe'

# ── Parametros a testar ────────────────────────────────────────────
SYMBOLS_TEST = ['XAUUSDz', 'BTCUSDz']

TIMEFRAMES_TEST = {
    'M1' : mt5.TIMEFRAME_M1,
    'M3' : mt5.TIMEFRAME_M3,
    'M5' : mt5.TIMEFRAME_M5,
    'M15': mt5.TIMEFRAME_M15,
}

SL_VALUES    = [1.5, 1.8, 2.0, 2.5]
TP1_VALUES   = [2.0]
TP2_VALUES   = [3.5]
TP3_VALUES   = [5.0]
VOL_FILTERS  = [1.0, 1.3, 1.5, 2.0]   # 1.0 = sem filtro efetivo
EMA_FAST     = 20
EMA_SLW      = 50
VOL_PERIOD   = 20
BARS         = 5000   # barras de historico

# ── Funcoes ────────────────────────────────────────────────────────
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def carregar_dados(symbol, timeframe, bars=BARS):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    if rates is None or len(rates) < EMA_SLW + 50:
        return None
    df = pd.DataFrame(rates)
    df['time_dt']  = pd.to_datetime(df['time'], unit='s')
    df['ema_fast'] = ema(df['close'], EMA_FAST)
    df['ema_slow'] = ema(df['close'], EMA_SLW)
    df['tr']       = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)),
                   abs(df['low']  - df['close'].shift(1)))
    )
    df['atr']      = df['tr'].rolling(14).mean()
    df['vol_avg']  = df['tick_volume'].rolling(VOL_PERIOD).mean().shift(1)
    return df.dropna().reset_index(drop=True)

def rodar_backtest(df, sl_atr, tp1_atr, tp2_atr, tp3_atr, vol_mult):
    """Simula as operacoes no DataFrame e retorna estatisticas."""
    trades = []
    in_trade   = False
    entry      = sl = tp1 = tp2 = tp3 = 0
    direcao    = None
    nivel_tp   = 0
    sl_atual   = 0

    for i in range(1, len(df)):
        cur  = df.iloc[i]
        prev = df.iloc[i-1]

        # ── Gerenciar posicao aberta ───────────────────────────────
        if in_trade:
            hi = cur['high']
            lo = cur['low']

            if direcao == 'BUY':
                # Stop loss
                if lo <= sl_atual:
                    pnl = sl_atual - entry
                    trades.append({'result': pnl, 'exit': 'SL', 'nivel': nivel_tp})
                    in_trade = False
                    continue
                # TP3
                if hi >= tp3:
                    pnl = tp3 - entry
                    trades.append({'result': pnl, 'exit': 'TP3', 'nivel': nivel_tp})
                    in_trade = False
                    continue
                # TP2 → move SL para TP1
                if nivel_tp < 2 and hi >= tp2:
                    nivel_tp = 2
                    sl_atual = tp1
                # TP1 → breakeven
                if nivel_tp < 1 and hi >= tp1:
                    nivel_tp = 1
                    sl_atual = entry

            else:  # SELL
                # Stop loss
                if hi >= sl_atual:
                    pnl = entry - sl_atual
                    trades.append({'result': pnl, 'exit': 'SL', 'nivel': nivel_tp})
                    in_trade = False
                    continue
                # TP3
                if lo <= tp3:
                    pnl = entry - tp3
                    trades.append({'result': pnl, 'exit': 'TP3', 'nivel': nivel_tp})
                    in_trade = False
                    continue
                # TP2 → move SL para TP1
                if nivel_tp < 2 and lo <= tp2:
                    nivel_tp = 2
                    sl_atual = tp1
                # TP1 → breakeven
                if nivel_tp < 1 and lo <= tp1:
                    nivel_tp = 1
                    sl_atual = entry

        # ── Detectar sinal ─────────────────────────────────────────
        if not in_trade:
            cross_buy  = prev['ema_fast'] <= prev['ema_slow'] and cur['ema_fast'] > cur['ema_slow']
            cross_sell = prev['ema_fast'] >= prev['ema_slow'] and cur['ema_fast'] < cur['ema_slow']

            if cross_buy or cross_sell:
                vol_ok = cur['tick_volume'] >= cur['vol_avg'] * vol_mult if vol_mult > 1.0 else True
                if not vol_ok:
                    continue

                atr = cur['atr']
                if atr == 0 or np.isnan(atr):
                    continue

                direcao = 'BUY' if cross_buy else 'SELL'
                if direcao == 'BUY':
                    entry  = cur['close']
                    sl_atual = round(entry - atr * sl_atr, 5)
                    tp1    = round(entry + atr * tp1_atr, 5)
                    tp2    = round(entry + atr * tp2_atr, 5)
                    tp3    = round(entry + atr * tp3_atr, 5)
                else:
                    entry  = cur['close']
                    sl_atual = round(entry + atr * sl_atr, 5)
                    tp1    = round(entry - atr * tp1_atr, 5)
                    tp2    = round(entry - atr * tp2_atr, 5)
                    tp3    = round(entry - atr * tp3_atr, 5)

                in_trade = True
                nivel_tp = 0

    # Fechar posicao aberta no final
    if in_trade:
        last = df.iloc[-1]['close']
        pnl  = (last - entry) if direcao == 'BUY' else (entry - last)
        trades.append({'result': pnl, 'exit': 'OPEN', 'nivel': nivel_tp})

    return trades

def estatisticas(trades, symbol, atr_medio):
    if not trades:
        return None
    results  = [t['result'] for t in trades]
    wins     = [r for r in results if r > 0]
    loses    = [r for r in results if r <= 0]
    total    = sum(results)
    win_rate = len(wins) / len(results) * 100 if results else 0
    pf       = sum(wins) / abs(sum(loses)) if loses and sum(loses) != 0 else float('inf')

    # Drawdown maximo
    equity   = np.cumsum(results)
    peak     = np.maximum.accumulate(equity)
    dd       = equity - peak
    max_dd   = dd.min()

    # Esperar valor monetario (pips * valor_pip estimado)
    # Para XAU: ~1 pip = $1 por 0.01 lot | Para BTC: ~1 USD move = $1 por 0.01 lot
    return {
        'trades'  : len(trades),
        'wins'    : len(wins),
        'loses'   : len(loses),
        'win_rate': win_rate,
        'pf'      : pf,
        'total_pts': total,
        'avg_win' : np.mean(wins) if wins else 0,
        'avg_loss': np.mean(loses) if loses else 0,
        'max_dd'  : max_dd,
        'expectancy': np.mean(results),
    }

# ── Main ───────────────────────────────────────────────────────────
print('Conectando ao MT5...')
mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)

all_results = []

for symbol in SYMBOLS_TEST:
    print(f'\n{"="*70}')
    print(f'Par: {symbol}')
    print(f'{"="*70}')

    for tf_nome, tf_val in TIMEFRAMES_TEST.items():
        print(f'\n  Carregando {symbol} {tf_nome} ({BARS} barras)...')
        df = carregar_dados(symbol, tf_val)
        if df is None:
            print(f'  ERRO: sem dados para {symbol} {tf_nome}')
            continue

        atr_medio = df['atr'].mean()
        data_ini  = df['time_dt'].iloc[0]
        data_fim  = df['time_dt'].iloc[-1]
        print(f'  Periodo: {data_ini:%Y-%m-%d} a {data_fim:%Y-%m-%d} | ATR medio: {atr_medio:.3f}')

        best = None
        for sl, vol in product(SL_VALUES, VOL_FILTERS):
            trades = rodar_backtest(df, sl, TP1_VALUES[0], TP2_VALUES[0], TP3_VALUES[0], vol)
            if not trades:
                continue
            est = estatisticas(trades, symbol, atr_medio)
            if est is None:
                continue

            est.update({
                'symbol': symbol,
                'tf'    : tf_nome,
                'sl'    : sl,
                'vol'   : vol,
            })
            all_results.append(est)

            # Exibir linha resumida
            vol_str = f'{vol:.1f}x' if vol > 1.0 else 'OFF '
            print(f'    SL={sl:.1f}x VOL={vol_str} | '
                  f'Trades={est["trades"]:3d} WR={est["win_rate"]:5.1f}% '
                  f'PF={est["pf"]:5.2f} '
                  f'Exp={est["expectancy"]:+.4f} '
                  f'MaxDD={est["max_dd"]:+.3f}')

# ── Ranking final ──────────────────────────────────────────────────
print(f'\n{"="*70}')
print('TOP 5 CONFIGURACOES POR EXPECTATIVA (pontos ATR por trade)')
print(f'{"="*70}')

df_res = pd.DataFrame(all_results)
df_res = df_res[df_res['trades'] >= 20]  # minimo 20 trades para ser valido
df_res = df_res.sort_values('expectancy', ascending=False)

print(f'\n{"Par":<10} {"TF":<5} {"SL":<5} {"Vol":<5} {"Trades":>6} {"WR%":>6} {"PF":>6} {"Exp":>8} {"MaxDD":>8}')
print('-'*65)
for _, row in df_res.head(10).iterrows():
    vol_str = f'{row["vol"]:.1f}x' if row['vol'] > 1.0 else 'OFF'
    print(f'{row["symbol"]:<10} {row["tf"]:<5} {row["sl"]:.1f}x  {vol_str:<5} '
          f'{row["trades"]:>6.0f} {row["win_rate"]:>6.1f} {row["pf"]:>6.2f} '
          f'{row["expectancy"]:>+8.4f} {row["max_dd"]:>8.3f}')

print(f'\n{"="*70}')
print('TOP 5 POR PROFIT FACTOR')
print(f'{"="*70}')
df_pf = df_res.sort_values('pf', ascending=False)
for _, row in df_pf.head(5).iterrows():
    vol_str = f'{row["vol"]:.1f}x' if row['vol'] > 1.0 else 'OFF'
    print(f'{row["symbol"]:<10} {row["tf"]:<5} SL={row["sl"]:.1f}x VOL={vol_str:<5} '
          f'Trades={row["trades"]:.0f} WR={row["win_rate"]:.1f}% PF={row["pf"]:.2f} Exp={row["expectancy"]:+.4f}')

print(f'\n{"="*70}')
print('RECOMENDACAO POR PAR')
print(f'{"="*70}')
for sym in SYMBOLS_TEST:
    sub = df_res[df_res['symbol'] == sym]
    if sub.empty:
        continue
    best = sub.iloc[0]
    vol_str = f'{best["vol"]:.1f}x' if best['vol'] > 1.0 else 'OFF'
    print(f'\n{sym}:')
    print(f'  Melhor TF  : {best["tf"]}')
    print(f'  Melhor SL  : {best["sl"]:.1f}x ATR')
    print(f'  Filtro Vol : {vol_str}')
    print(f'  Win Rate   : {best["win_rate"]:.1f}%')
    print(f'  Profit Factor: {best["pf"]:.2f}')
    print(f'  Expectativa: {best["expectancy"]:+.4f} ATR/trade')
    print(f'  Trades     : {best["trades"]:.0f}')

mt5.shutdown()
print('\nBacktest concluido.')
