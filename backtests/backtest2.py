"""
ScalperFlow Backtest v2 - Busca configuracao com WR >= 60-70%
Filtros extras: RSI, tendencia H1, sessao Londres/NY, EMA alternativas
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
from itertools import product

LOGIN    = 83090423
PASSWORD = 'aEzakmi931018@'
SERVER   = 'Exness-MT5Trial12'
PATH     = 'C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe'

SYMBOLS_TEST = ['XAUUSDz', 'BTCUSDz']

TIMEFRAMES_TEST = {
    'M5' : mt5.TIMEFRAME_M5,
    'M15': mt5.TIMEFRAME_M15,
    'M30': mt5.TIMEFRAME_M30,
}

# Combinacoes de EMA a testar
EMA_COMBOS = [
    (9,  21),
    (10, 30),
    (20, 50),
    (13, 34),
]

SL_VALUES   = [1.5, 2.0, 2.5]
TP1_ATR     = 2.0
TP2_ATR     = 3.5
TP3_ATR     = 5.0
VOL_FILTERS = [1.0, 1.3, 1.5]   # 1.0 = desabilitado
VOL_PERIOD  = 20
BARS        = 5000

# Sessoes (hora UTC)
SESSOES = {
    'todas'   : (0,  24),
    'london'  : (7,  16),
    'ny'      : (13, 21),
    'lon+ny'  : (7,  21),
}

def ema_calc(series, p):
    return series.ewm(span=p, adjust=False).mean()

def rsi_calc(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def carregar(symbol, tf, bars=BARS):
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
    if rates is None or len(rates) < 100:
        return None
    df = pd.DataFrame(rates)
    df['time_dt'] = pd.to_datetime(df['time'], unit='s')
    df['hour']    = df['time_dt'].dt.hour
    df['tr']      = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)),
                   abs(df['low']  - df['close'].shift(1)))
    )
    df['atr']         = df['tr'].rolling(14).mean()
    df['tick_volume'] = df['tick_volume'].astype(float)
    df['vol_avg']     = df['tick_volume'].rolling(VOL_PERIOD).mean().shift(1)
    df['rsi']         = rsi_calc(df['close'])
    return df

def carregar_h1(symbol):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 2000)
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    df['ema20'] = ema_calc(df['close'].astype(float), 20)
    df['ema50'] = ema_calc(df['close'].astype(float), 50)
    df['trend_alta'] = df['ema20'] > df['ema50']
    return df[['time','trend_alta']].set_index('time')

def rodar_bt(df, h1_trend, ema_fast, ema_slow, sl_atr, vol_mult, sessao_h, use_rsi, use_h1):
    df = df.copy()
    df['ef'] = ema_calc(df['close'].astype(float), ema_fast)
    df['es'] = ema_calc(df['close'].astype(float), ema_slow)
    df = df.dropna().reset_index(drop=True)

    trades     = []
    in_trade   = False
    entry      = sl_atual = tp1 = tp2 = tp3 = 0
    direcao    = None
    nivel_tp   = 0

    h_ini, h_fim = sessao_h

    for i in range(1, len(df)):
        cur  = df.iloc[i]
        prev = df.iloc[i-1]

        # Gerenciar posicao aberta
        if in_trade:
            hi = cur['high']
            lo = cur['low']
            if direcao == 'BUY':
                if lo <= sl_atual:
                    trades.append({'result': sl_atual - entry, 'exit': 'SL', 'nivel': nivel_tp})
                    in_trade = False; continue
                if hi >= tp3:
                    trades.append({'result': tp3 - entry, 'exit': 'TP3', 'nivel': nivel_tp})
                    in_trade = False; continue
                if nivel_tp < 2 and hi >= tp2:
                    nivel_tp = 2; sl_atual = tp1
                if nivel_tp < 1 and hi >= tp1:
                    nivel_tp = 1; sl_atual = entry
            else:
                if hi >= sl_atual:
                    trades.append({'result': entry - sl_atual, 'exit': 'SL', 'nivel': nivel_tp})
                    in_trade = False; continue
                if lo <= tp3:
                    trades.append({'result': entry - tp3, 'exit': 'TP3', 'nivel': nivel_tp})
                    in_trade = False; continue
                if nivel_tp < 2 and lo <= tp2:
                    nivel_tp = 2; sl_atual = tp1
                if nivel_tp < 1 and lo <= tp1:
                    nivel_tp = 1; sl_atual = entry
            continue

        # Filtro de sessao
        if not (h_ini <= cur['hour'] < h_fim):
            continue

        # Detectar cruzamento
        cross_buy  = prev['ef'] <= prev['es'] and cur['ef'] > cur['es']
        cross_sell = prev['ef'] >= prev['es'] and cur['ef'] < cur['es']
        if not (cross_buy or cross_sell):
            continue

        direcao_sinal = 'BUY' if cross_buy else 'SELL'

        # Filtro de volume
        if vol_mult > 1.0:
            if pd.isna(cur['vol_avg']) or cur['tick_volume'] < cur['vol_avg'] * vol_mult:
                continue

        # Filtro RSI
        if use_rsi:
            if direcao_sinal == 'BUY'  and cur['rsi'] < 50: continue
            if direcao_sinal == 'SELL' and cur['rsi'] > 50: continue

        # Filtro tendencia H1
        if use_h1 and h1_trend is not None:
            t_key = df['time'].iloc[i]
            keys  = h1_trend.index
            pos   = keys.searchsorted(t_key, side='right') - 1
            if pos < 0: continue
            trend_alta = h1_trend['trend_alta'].iloc[pos]
            if direcao_sinal == 'BUY'  and not trend_alta: continue
            if direcao_sinal == 'SELL' and trend_alta:      continue

        atr = cur['atr']
        if atr == 0 or np.isnan(atr): continue

        entry = cur['close']
        if direcao_sinal == 'BUY':
            sl_atual = round(entry - atr * sl_atr, 5)
            tp1      = round(entry + atr * TP1_ATR, 5)
            tp2      = round(entry + atr * TP2_ATR, 5)
            tp3      = round(entry + atr * TP3_ATR, 5)
        else:
            sl_atual = round(entry + atr * sl_atr, 5)
            tp1      = round(entry - atr * TP1_ATR, 5)
            tp2      = round(entry - atr * TP2_ATR, 5)
            tp3      = round(entry - atr * TP3_ATR, 5)

        direcao  = direcao_sinal
        in_trade = True
        nivel_tp = 0

    return trades

def stats(trades):
    if len(trades) < 8:
        return None
    r  = [t['result'] for t in trades]
    w  = [x for x in r if x > 0]
    l  = [x for x in r if x <= 0]
    pf = sum(w) / abs(sum(l)) if l and sum(l) != 0 else 99.0
    eq = np.cumsum(r)
    pk = np.maximum.accumulate(eq)
    return {
        'n'       : len(r),
        'wr'      : len(w)/len(r)*100,
        'pf'      : pf,
        'exp'     : np.mean(r),
        'maxdd'   : (eq - pk).min(),
    }

# ── Main ───────────────────────────────────────────────────────────
print('Conectando MT5...')
mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)

resultados = []
MIN_WR     = 45.0   # so mostra configs acima desse WR

for symbol in SYMBOLS_TEST:
    print(f'\n{"="*70}')
    print(f'PAR: {symbol}')
    print(f'{"="*70}')

    h1 = carregar_h1(symbol)

    for tf_nome, tf_val in TIMEFRAMES_TEST.items():
        df = carregar(symbol, tf_val)
        if df is None:
            print(f'  Sem dados: {symbol} {tf_nome}')
            continue

        print(f'\n  {tf_nome} | {df["time_dt"].iloc[0]:%Y-%m-%d} a {df["time_dt"].iloc[-1]:%Y-%m-%d} | {len(df)} barras')

        for (ef, es), sl, vol, (sess_nome, sess_h), use_rsi, use_h1 in product(
            EMA_COMBOS, SL_VALUES, VOL_FILTERS,
            SESSOES.items(), [False, True], [False, True]
        ):
            if ef >= es: continue

            trades = rodar_bt(df, h1, ef, es, sl, vol, sess_h, use_rsi, use_h1)
            s = stats(trades)
            if s is None: continue
            if s['wr'] < MIN_WR: continue

            vol_str  = f'{vol:.1f}x' if vol > 1.0 else 'OFF'
            rsi_str  = '+RSI' if use_rsi else ''
            h1_str   = '+H1'  if use_h1  else ''
            filtros  = f'{vol_str}{rsi_str}{h1_str}'

            print(f'    EMA{ef}x{es} SL={sl:.1f} VOL={vol_str} SESS={sess_nome:<7} RSI={use_rsi} H1={use_h1} | '
                  f'N={s["n"]:3d} WR={s["wr"]:5.1f}% PF={s["pf"]:5.2f} Exp={s["exp"]:+.4f} DD={s["maxdd"]:+.2f}')

            resultados.append({
                'symbol': symbol, 'tf': tf_nome,
                'ema_fast': ef, 'ema_slow': es,
                'sl': sl, 'vol': vol,
                'sessao': sess_nome, 'rsi': use_rsi, 'h1': use_h1,
                **s
            })

# ── Ranking ────────────────────────────────────────────────────────
print(f'\n{"="*70}')
print('RANKING FINAL - TOP 15 por WIN RATE (min 8 trades)')
print(f'{"="*70}')
df_r = pd.DataFrame(resultados).sort_values('wr', ascending=False)

print(f'\n{"Par":<10} {"TF":<5} {"EMA":<8} {"SL":<5} {"Filtros":<20} {"N":>4} {"WR%":>6} {"PF":>6} {"Exp":>8}')
print('-'*75)
for _, row in df_r.head(15).iterrows():
    vol_str = f'{row["vol"]:.1f}x' if row['vol'] > 1.0 else 'OFF'
    rsi_str = '+RSI' if row['rsi'] else ''
    h1_str  = '+H1'  if row['h1']  else ''
    filtros = f'{vol_str} {row["sessao"]}{rsi_str}{h1_str}'
    print(f'{row["symbol"]:<10} {row["tf"]:<5} {row["ema_fast"]}x{row["ema_slow"]:<5} '
          f'{row["sl"]:.1f}x  {filtros:<20} {row["n"]:>4} {row["wr"]:>6.1f} '
          f'{row["pf"]:>6.2f} {row["exp"]:>+8.4f}')

print(f'\n{"="*70}')
print('MELHOR POR PAR (equilibrio WR x PF x Trades)')
print(f'{"="*70}')
for sym in SYMBOLS_TEST:
    sub = df_r[df_r['symbol'] == sym]
    if sub.empty:
        print(f'\n{sym}: nenhuma config passou o filtro de WR>={MIN_WR}%')
        continue
    # Pontuacao combinada: WR * PF
    sub = sub.copy()
    sub['score'] = sub['wr'] * sub['pf'] * np.log1p(sub['n'])
    best = sub.sort_values('score', ascending=False).iloc[0]
    vol_str = f'{best["vol"]:.1f}x' if best['vol'] > 1.0 else 'OFF'
    print(f'\n{sym}:')
    print(f'  TF         : {best["tf"]}')
    print(f'  EMA        : {best["ema_fast"]}x{best["ema_slow"]}')
    print(f'  SL         : {best["sl"]:.1f}x ATR')
    print(f'  Vol filter : {vol_str}')
    print(f'  Sessao     : {best["sessao"]}')
    print(f'  RSI filter : {best["rsi"]}')
    print(f'  H1 filter  : {best["h1"]}')
    print(f'  Win Rate   : {best["wr"]:.1f}%')
    print(f'  Profit Fac : {best["pf"]:.2f}')
    print(f'  Trades     : {best["n"]:.0f}')
    print(f'  Expectativa: {best["exp"]:+.4f} ATR/trade')

mt5.shutdown()
print('\nBacktest v2 concluido.')
