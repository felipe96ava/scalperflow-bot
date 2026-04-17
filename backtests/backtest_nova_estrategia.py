"""
Backtest - Nova Estrategia de Alta Win Rate

XAUUSDz: Pullback na EMA + confirmacao de vela
  - Tendencia H1 (EMA20 > EMA50)
  - M15: preco puxa ate a EMA21
  - RSI entre 35-65 (nao em extremo)
  - Candle de reversao (martelo/engolfo) na EMA
  - Volume acima da media

BTCUSDz: Mean Reversion com Bollinger + RSI
  - Preco toca banda externa de Bollinger (2 desvios)
  - RSI em extremo (>70 venda / <30 compra)
  - Volume acima da media
  - Confirmacao de fechamento dentro das bandas
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from itertools import product

LOGIN    = 83090423
PASSWORD = 'aEzakmi931018@'
SERVER   = 'Exness-MT5Trial12'
PATH     = 'C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe'

TP1_ATR = 2.0
TP2_ATR = 3.5
TP3_ATR = 5.0
MIN_TRADES = 15

def ec(s, p): return s.ewm(span=p, adjust=False).mean()

def rsi_calc(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def bollinger(s, p=20, dev=2.0):
    mid = s.rolling(p).mean()
    std = s.rolling(p).std()
    return mid, mid + dev * std, mid - dev * std

def load(sym, tf, bars=5000):
    r = mt5.copy_rates_from_pos(sym, tf, 0, bars)
    if r is None or len(r) < 200: return None
    df = pd.DataFrame(r)
    df['time_dt'] = pd.to_datetime(df['time'], unit='s')
    df['hour'] = df['time_dt'].dt.hour
    c = df['close'].astype(float)
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    o = df['open'].astype(float)
    df['tr'] = np.maximum(h-l, np.maximum(abs(h-c.shift(1)), abs(l-c.shift(1))))
    df['atr'] = df['tr'].rolling(14).mean()
    df['tv'] = df['tick_volume'].astype(float)
    df['va'] = df['tv'].rolling(20).mean().shift(1)
    df['rsi'] = rsi_calc(c)
    df['ema9']  = ec(c, 9)
    df['ema21'] = ec(c, 21)
    df['ema50'] = ec(c, 50)
    df['bb_mid'], df['bb_up'], df['bb_dn'] = bollinger(c, 20, 2.0)
    df['body'] = abs(c - o)
    df['upper_sh'] = h - c.clip(lower=o)
    df['lower_sh'] = o.clip(upper=c) - l
    df['total_sh'] = df['upper_sh'] + df['lower_sh']
    return df.dropna().reset_index(drop=True)

def load_h1(sym):
    r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 3000)
    if r is None: return None
    df = pd.DataFrame(r)
    c = df['close'].astype(float)
    df['ema20'] = ec(c, 20)
    df['ema50'] = ec(c, 50)
    df['up'] = df['ema20'] > df['ema50']
    return df[['time', 'up']].set_index('time')

def get_h1_trend(h1, ts):
    if h1 is None: return None
    pos = h1.index.searchsorted(ts, 'right') - 1
    if pos < 0: return None
    return h1['up'].iloc[pos]

def eh_martelo(row):
    """Candle de reversao: sombra longa inferior, corpo pequeno no topo"""
    if row['body'] == 0: return False, False
    buy  = (row['lower_sh'] > row['body'] * 1.5) and (row['upper_sh'] < row['body'] * 0.5)
    sell = (row['upper_sh'] > row['body'] * 1.5) and (row['lower_sh'] < row['body'] * 0.5)
    return buy, sell

def eh_engolfo(cur, prev):
    """Candle engolfo: fecha fora do corpo anterior"""
    bull = (cur['close'] > cur['open'] and prev['close'] < prev['open'] and
            cur['close'] > prev['open'] and cur['open'] < prev['close'])
    bear = (cur['close'] < cur['open'] and prev['close'] > prev['open'] and
            cur['close'] < prev['open'] and cur['open'] > prev['close'])
    return bull, bear

def simular(trades_list, sl_atr):
    results = []
    for t in trades_list:
        results.append(t)
    if not results: return None
    w = [x for x in results if x > 0]
    l = [x for x in results if x <= 0]
    if not l or sum(l) == 0: return None
    pf = sum(w) / abs(sum(l))
    eq = np.cumsum(results)
    pk = np.maximum.accumulate(eq)
    return {
        'n': len(results), 'wr': len(w)/len(results)*100,
        'pf': pf, 'exp': np.mean(results), 'dd': (eq-pk).min()
    }

# ── Estrategia 1: XAUUSDz - Pullback na EMA ───────────────────────
def backtest_xau_pullback(df, h1, sl_atr, ema_pullback, rsi_min, rsi_max,
                           vol_mult, sess_h, req_candle):
    trades = []
    it = False; en = sa = t1 = t2 = t3 = 0; di = None; nv = 0
    h0, h1e = sess_h

    for i in range(2, len(df)):
        cur  = df.iloc[i]
        prev = df.iloc[i-1]

        if it:
            hi = cur['high']; lo = cur['low']
            if di == 'BUY':
                if lo <= sa: trades.append(sa - en); it = False; continue
                if hi >= t3: trades.append(t3 - en); it = False; continue
                if nv < 2 and hi >= t2: nv = 2; sa = t1
                if nv < 1 and hi >= t1: nv = 1; sa = en
            else:
                if hi >= sa: trades.append(en - sa); it = False; continue
                if lo <= t3: trades.append(en - t3); it = False; continue
                if nv < 2 and lo <= t2: nv = 2; sa = t1
                if nv < 1 and lo <= t1: nv = 1; sa = en
            continue

        if not (h0 <= cur['hour'] < h1e): continue

        trend = get_h1_trend(h1, df['time'].iloc[i])
        if trend is None: continue

        atr = cur['atr']
        if atr == 0 or np.isnan(atr): continue

        # Pullback para EMA
        ema_val = cur[ema_pullback]
        preco_perto = abs(cur['close'] - ema_val) < atr * 0.5

        if not preco_perto: continue

        # RSI no range certo (nao em extremo)
        if not (rsi_min <= cur['rsi'] <= rsi_max): continue

        # Volume
        if vol_mult > 1.0 and (pd.isna(cur['va']) or cur['tv'] < cur['va'] * vol_mult):
            continue

        # Direcao alinhada com tendencia H1
        if trend:  # alta
            d = 'BUY'
            # Preco abaixo ou tocando a EMA (pullback)
            if cur['close'] > ema_val * 1.002: continue
        else:  # baixa
            d = 'SELL'
            if cur['close'] < ema_val * 0.998: continue

        # Confirmacao de candle
        if req_candle:
            mart_buy, mart_sell = eh_martelo(cur)
            eng_buy, eng_sell   = eh_engolfo(cur, prev)
            if d == 'BUY'  and not (mart_buy or eng_buy):   continue
            if d == 'SELL' and not (mart_sell or eng_sell): continue

        en = cur['close']
        if d == 'BUY':
            sa = en - atr*sl_atr; t1 = en + atr*TP1_ATR; t2 = en + atr*TP2_ATR; t3 = en + atr*TP3_ATR
        else:
            sa = en + atr*sl_atr; t1 = en - atr*TP1_ATR; t2 = en - atr*TP2_ATR; t3 = en - atr*TP3_ATR

        di = d; it = True; nv = 0

    return trades

# ── Estrategia 2: BTCUSDz - Mean Reversion Bollinger ──────────────
def backtest_btc_bollinger(df, sl_atr, bb_dev, rsi_buy, rsi_sell,
                            vol_mult, sess_h, req_close_inside):
    trades = []
    it = False; en = sa = t1 = t2 = t3 = 0; di = None; nv = 0
    h0, h1e = sess_h

    # Recalcular BB com dev especificado
    c = df['close'].astype(float)
    mid, up, dn = bollinger(c, 20, bb_dev)
    df = df.copy()
    df['bb_up2'] = up; df['bb_dn2'] = dn; df['bb_mid2'] = mid

    for i in range(2, len(df)):
        cur  = df.iloc[i]
        prev = df.iloc[i-1]

        if it:
            hi = cur['high']; lo = cur['low']
            if di == 'BUY':
                if lo <= sa: trades.append(sa - en); it = False; continue
                if hi >= t3: trades.append(t3 - en); it = False; continue
                if nv < 2 and hi >= t2: nv = 2; sa = t1
                if nv < 1 and hi >= t1: nv = 1; sa = en
            else:
                if hi >= sa: trades.append(en - sa); it = False; continue
                if lo <= t3: trades.append(en - t3); it = False; continue
                if nv < 2 and lo <= t2: nv = 2; sa = t1
                if nv < 1 and lo <= t1: nv = 1; sa = en
            continue

        if not (h0 <= cur['hour'] < h1e): continue

        atr = cur['atr']
        if atr == 0 or np.isnan(atr): continue

        # Volume
        if vol_mult > 1.0 and (pd.isna(cur['va']) or cur['tv'] < cur['va'] * vol_mult):
            continue

        d = None
        # BUY: toca banda inferior + RSI sobrevendido
        if cur['low'] <= cur['bb_dn2'] and cur['rsi'] <= rsi_buy:
            # Confirmacao: fecha acima da banda inferior (rejeicao)
            if req_close_inside and cur['close'] <= cur['bb_dn2']:
                pass
            else:
                d = 'BUY'

        # SELL: toca banda superior + RSI sobrecomprado
        elif cur['high'] >= cur['bb_up2'] and cur['rsi'] >= rsi_sell:
            if req_close_inside and cur['close'] >= cur['bb_up2']:
                pass
            else:
                d = 'SELL'

        if d is None: continue

        en = cur['close']
        if d == 'BUY':
            sa = en - atr*sl_atr; t1 = en + atr*TP1_ATR; t2 = en + atr*TP2_ATR; t3 = en + atr*TP3_ATR
        else:
            sa = en + atr*sl_atr; t1 = en - atr*TP1_ATR; t2 = en - atr*TP2_ATR; t3 = en - atr*TP3_ATR

        di = d; it = True; nv = 0

    return trades

# ── MAIN ───────────────────────────────────────────────────────────
print('Conectando MT5...')
mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)

TFS_XAU = {'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15}
TFS_BTC = {'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15}
SESS = {'todas':(0,24), 'london':(7,16), 'ny':(13,21), 'lon+ny':(7,21)}

# ─── XAUUSDz ──────────────────────────────────────────────────────
print('\n' + '='*70)
print('ESTRATEGIA 1: XAUUSDz - Pullback na EMA + Confirmacao')
print('='*70)

h1_xau = load_h1('XAUUSDz')
res_xau = []

for tf_n, tf_v in TFS_XAU.items():
    df = load('XAUUSDz', tf_v)
    if df is None: continue
    print(f'\n  {tf_n} | {df["time_dt"].iloc[0]:%Y-%m-%d} a {df["time_dt"].iloc[-1]:%Y-%m-%d}')

    for sl, ema_pb, rsi_min, rsi_max, vol, (sn, sh), candle in product(
        [1.5, 2.0, 2.5],
        ['ema9', 'ema21'],
        [30, 35, 40],
        [60, 65, 70],
        [1.0, 1.3, 1.5],
        SESS.items(),
        [False, True]
    ):
        t = backtest_xau_pullback(df, h1_xau, sl, ema_pb, rsi_min, rsi_max, vol, sh, candle)
        if len(t) < MIN_TRADES: continue
        s = simular(t, sl)
        if s is None: continue
        vs = f'{vol:.1f}x' if vol > 1 else 'OFF'
        s.update({'tf': tf_n, 'sl': sl, 'ema': ema_pb, 'rsi_rng': f'{rsi_min}-{rsi_max}',
                  'vol': vs, 'sess': sn, 'candle': candle})
        res_xau.append(s)
        if s['wr'] >= 55:
            print(f'    {tf_n} EMA={ema_pb} SL={sl:.1f} RSI={rsi_min}-{rsi_max} VOL={vs} '
                  f'SESS={sn} CANDLE={candle} | N={s["n"]} WR={s["wr"]:.1f}% PF={s["pf"]:.2f} Exp={s["exp"]:+.2f}')

# ─── BTCUSDz ──────────────────────────────────────────────────────
print('\n' + '='*70)
print('ESTRATEGIA 2: BTCUSDz - Mean Reversion Bollinger + RSI')
print('='*70)

res_btc = []

for tf_n, tf_v in TFS_BTC.items():
    df = load('BTCUSDz', tf_v)
    if df is None: continue
    print(f'\n  {tf_n} | {df["time_dt"].iloc[0]:%Y-%m-%d} a {df["time_dt"].iloc[-1]:%Y-%m-%d}')

    for sl, bb_dev, rsi_buy, rsi_sell, vol, (sn, sh), close_inside in product(
        [1.5, 2.0, 2.5],
        [1.8, 2.0, 2.2, 2.5],
        [25, 30, 35],
        [65, 70, 75],
        [1.0, 1.3, 1.5],
        SESS.items(),
        [False, True]
    ):
        t = backtest_btc_bollinger(df, sl, bb_dev, rsi_buy, rsi_sell, vol, sh, close_inside)
        if len(t) < MIN_TRADES: continue
        s = simular(t, sl)
        if s is None: continue
        vs = f'{vol:.1f}x' if vol > 1 else 'OFF'
        s.update({'tf': tf_n, 'sl': sl, 'bb': bb_dev, 'rsi_b': rsi_buy, 'rsi_s': rsi_sell,
                  'vol': vs, 'sess': sn, 'close_in': close_inside})
        res_btc.append(s)
        if s['wr'] >= 55:
            print(f'    {tf_n} BB={bb_dev} SL={sl:.1f} RSI<{rsi_buy}/>={rsi_sell} VOL={vs} '
                  f'SESS={sn} CI={close_inside} | N={s["n"]} WR={s["wr"]:.1f}% PF={s["pf"]:.2f} Exp={s["exp"]:+.2f}')

# ─── RANKING FINAL ────────────────────────────────────────────────
print('\n' + '='*70)
print('RANKING FINAL')
print('='*70)

for nome, res in [('XAUUSDz - Pullback EMA', res_xau), ('BTCUSDz - Bollinger MeanRev', res_btc)]:
    print(f'\n--- {nome} ---')
    if not res:
        print('  Nenhuma config com trades suficientes.')
        continue
    df_r = pd.DataFrame(res).sort_values('wr', ascending=False)
    df_r['score'] = df_r['wr'] * df_r['pf'] * np.log1p(df_r['n'])
    print(f'  Top WR: {df_r.iloc[0]["wr"]:.1f}% | PF={df_r.iloc[0]["pf"]:.2f} | N={df_r.iloc[0]["n"]}')
    best = df_r.sort_values('score', ascending=False).iloc[0]
    print(f'  Melhor score: WR={best["wr"]:.1f}% | PF={best["pf"]:.2f} | N={best["n"]} | Exp={best["exp"]:+.2f}')
    print(f'  Config: {dict(best)}')

mt5.shutdown()
print('\nBacktest nova estrategia CONCLUIDO.')
