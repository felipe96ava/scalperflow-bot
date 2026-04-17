"""
Backtest 5 anos - BTCUSDz
Estrategia: EMA20x50 M15 + RSI>50/<50 + H1 trend + Sessao NY (13-21 UTC)
Capital inicial: $200 | Lote: 0.20 | SL=1.5xATR | TP1=2x | TP2=3.5x | TP3=5x
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime

LOGIN    = 83090423
PASSWORD = 'aEzakmi931018@'
SERVER   = 'Exness-MT5Trial12'
PATH     = 'C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe'

LOT          = 0.20
CAPITAL_INI  = 200.0
TICK_VALUE   = 0.01   # USD por tick
TICK_SIZE    = 0.01   # tamanho do tick em preco
# Valor por $1 de movimento = LOT * (1/TICK_SIZE) * TICK_VALUE = 0.20 * 100 * 0.01 = $0.20/ponto

SL_ATR  = 1.5
TP1_ATR = 2.0
TP2_ATR = 3.5
TP3_ATR = 5.0
SESS_H0 = 13   # UTC
SESS_H1 = 21   # UTC

def ec(s, p): return s.ewm(span=p, adjust=False).mean()
def rsi_c(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def pts_para_usd(pts):
    """Converte pontos de preco para USD com 0.20 lot."""
    return pts * LOT * (1 / TICK_SIZE) * TICK_VALUE

print('Conectando MT5...')
mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)
mt5.symbol_select('BTCUSDz', True)

# Carregar M15
print('Carregando M15 (5+ anos)...')
rates = mt5.copy_rates_from_pos('BTCUSDz', mt5.TIMEFRAME_M15, 0, 999999)
df = pd.DataFrame(rates)
df['time_dt']  = pd.to_datetime(df['time'], unit='s')
df['hour_utc'] = df['time_dt'].dt.hour
c = df['close'].astype(float)
h = df['high'].astype(float)
l = df['low'].astype(float)

df['ema20']   = ec(c, 20)
df['ema50']   = ec(c, 50)
df['tr']      = np.maximum(h-l, np.maximum(abs(h-c.shift(1)), abs(l-c.shift(1))))
df['atr']     = df['tr'].rolling(14).mean()
df['tv']      = df['tick_volume'].astype(float)
df['va']      = df['tv'].rolling(20).mean().shift(1)
df['rsi']     = rsi_c(c)
df = df.dropna().reset_index(drop=True)

# Carregar H1 para tendencia
print('Carregando H1 para filtro de tendencia...')
rates_h1 = mt5.copy_rates_from_pos('BTCUSDz', mt5.TIMEFRAME_H1, 0, 999999)
df_h1 = pd.DataFrame(rates_h1)
ch1 = df_h1['close'].astype(float)
df_h1['ema20'] = ec(ch1, 20)
df_h1['ema50'] = ec(ch1, 50)
df_h1['trend_alta'] = df_h1['ema20'] > df_h1['ema50']
df_h1 = df_h1.dropna().set_index('time')

def get_h1_trend(ts):
    pos = df_h1.index.searchsorted(ts, side='right') - 1
    if pos < 0: return None
    return bool(df_h1['trend_alta'].iloc[pos])

mt5.shutdown()

print(f'\nPeriodo: {df["time_dt"].iloc[0]:%Y-%m-%d} a {df["time_dt"].iloc[-1]:%Y-%m-%d}')
print(f'Total de barras M15: {len(df):,}')

# ── Simulacao ──────────────────────────────────────────────────────
trades     = []
capital    = CAPITAL_INI
equity_hist= [CAPITAL_INI]
it         = False
en = sa = t1 = t2 = t3 = 0
di = None; nv = 0
ultima_barra = None

print('\nSimulando...')

for i in range(2, len(df)):
    cur  = df.iloc[i]
    prev = df.iloc[i-1]

    if it:
        hi = cur['high']; lo = cur['low']
        if di == 'BUY':
            if lo <= sa:
                pnl_pts = sa - en
                pnl_usd = pts_para_usd(pnl_pts)
                capital += pnl_usd
                trades.append({'data': cur['time_dt'], 'dir': di, 'entry': en, 'exit': sa,
                               'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd, 'saida': 'SL', 'nivel': nv, 'capital': capital})
                it = False; continue
            if hi >= t3:
                pnl_pts = t3 - en
                pnl_usd = pts_para_usd(pnl_pts)
                capital += pnl_usd
                trades.append({'data': cur['time_dt'], 'dir': di, 'entry': en, 'exit': t3,
                               'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd, 'saida': 'TP3', 'nivel': nv, 'capital': capital})
                it = False; continue
            if nv < 2 and hi >= t2: nv = 2; sa = t1
            if nv < 1 and hi >= t1: nv = 1; sa = en
        else:
            if hi >= sa:
                pnl_pts = en - sa
                pnl_usd = pts_para_usd(pnl_pts)
                capital += pnl_usd
                trades.append({'data': cur['time_dt'], 'dir': di, 'entry': en, 'exit': sa,
                               'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd, 'saida': 'SL', 'nivel': nv, 'capital': capital})
                it = False; continue
            if lo <= t3:
                pnl_pts = en - t3
                pnl_usd = pts_para_usd(pnl_pts)
                capital += pnl_usd
                trades.append({'data': cur['time_dt'], 'dir': di, 'entry': en, 'exit': t3,
                               'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd, 'saida': 'TP3', 'nivel': nv, 'capital': capital})
                it = False; continue
            if nv < 2 and lo <= t2: nv = 2; sa = t1
            if nv < 1 and lo <= t1: nv = 1; sa = en
        equity_hist.append(capital)
        continue

    # Filtro sessao NY
    if not (SESS_H0 <= cur['hour_utc'] < SESS_H1):
        equity_hist.append(capital)
        continue

    # Cruzamento
    cb = prev['ema20'] <= prev['ema50'] and cur['ema20'] > cur['ema50']
    cs = prev['ema20'] >= prev['ema50'] and cur['ema20'] < cur['ema50']
    if not (cb or cs):
        equity_hist.append(capital)
        continue

    d = 'BUY' if cb else 'SELL'

    # Filtro RSI
    if d == 'BUY'  and cur['rsi'] < 50:
        equity_hist.append(capital); continue
    if d == 'SELL' and cur['rsi'] > 50:
        equity_hist.append(capital); continue

    # Filtro H1
    trend = get_h1_trend(df['time'].iloc[i])
    if trend is not None:
        if d == 'BUY'  and not trend:
            equity_hist.append(capital); continue
        if d == 'SELL' and trend:
            equity_hist.append(capital); continue

    atr = cur['atr']
    if not atr or np.isnan(atr):
        equity_hist.append(capital); continue

    # Mesma barra ja processada
    if df['time'].iloc[i] == ultima_barra:
        equity_hist.append(capital); continue
    ultima_barra = df['time'].iloc[i]

    en = cur['close']
    if d == 'BUY':
        sa = en - atr*SL_ATR; t1 = en + atr*TP1_ATR; t2 = en + atr*TP2_ATR; t3 = en + atr*TP3_ATR
    else:
        sa = en + atr*SL_ATR; t1 = en - atr*TP1_ATR; t2 = en - atr*TP2_ATR; t3 = en - atr*TP3_ATR

    di = d; it = True; nv = 0
    equity_hist.append(capital)

# ── Estatisticas ───────────────────────────────────────────────────
df_t = pd.DataFrame(trades)
if df_t.empty:
    print('Nenhum trade simulado.')
    exit()

wins  = df_t[df_t['pnl_usd'] > 0]
loses = df_t[df_t['pnl_usd'] <= 0]
wr    = len(wins) / len(df_t) * 100
pf    = wins['pnl_usd'].sum() / abs(loses['pnl_usd'].sum()) if len(loses) > 0 else 99

equity = np.array(equity_hist)
peak   = np.maximum.accumulate(equity)
dd     = equity - peak
max_dd = dd.min()

# Resultado por ano
df_t['ano'] = df_t['data'].dt.year
por_ano = df_t.groupby('ano').agg(
    trades=('pnl_usd','count'),
    ganhos=('pnl_usd', lambda x: (x>0).sum()),
    lucro=('pnl_usd','sum'),
    wr=('pnl_usd', lambda x: (x>0).mean()*100)
).round(2)

print(f'\n{"="*60}')
print(f'RESULTADO BACKTEST - BTCUSDz M15 (5 anos)')
print(f'Estrategia: EMA20x50 + RSI + H1 trend + Sessao NY 13-21h UTC')
print(f'Capital inicial: ${CAPITAL_INI:.2f} | Lote: {LOT}')
print(f'{"="*60}')
print(f'\nPeriodo    : {df_t["data"].iloc[0]:%Y-%m-%d} a {df_t["data"].iloc[-1]:%Y-%m-%d}')
print(f'Total trades: {len(df_t)}')
print(f'Vencedores  : {len(wins)} ({wr:.1f}%)')
print(f'Perdedores  : {len(loses)} ({100-wr:.1f}%)')
print(f'Profit Factor: {pf:.2f}')
print(f'\nCapital inicial : ${CAPITAL_INI:.2f}')
print(f'Capital final   : ${capital:.2f}')
print(f'Lucro total     : ${capital - CAPITAL_INI:+.2f}')
print(f'Retorno total   : {(capital/CAPITAL_INI - 1)*100:+.1f}%')
print(f'\nMaior ganho     : ${wins["pnl_usd"].max():.2f}')
print(f'Maior perda     : ${loses["pnl_usd"].min():.2f}')
print(f'Media por trade : ${df_t["pnl_usd"].mean():+.2f}')
print(f'MaxDrawdown     : ${max_dd:.2f} ({max_dd/CAPITAL_INI*100:.1f}%)')
print(f'\nSaidas:')
for s, g in df_t.groupby('saida')['pnl_usd'].agg(['count','sum']).iterrows():
    print(f'  {s}: {g["count"]:.0f} trades | ${g["sum"]:.2f}')

print(f'\n{"="*60}')
print(f'RESULTADO POR ANO')
print(f'{"="*60}')
print(f'{"Ano":<6} {"Trades":>7} {"WR%":>6} {"Lucro USD":>12} {"Capital":>10}')
print('-'*45)
cap_acum = CAPITAL_INI
for ano, row in por_ano.iterrows():
    cap_acum += row['lucro']
    print(f'{ano:<6} {row["trades"]:>7.0f} {row["wr"]:>6.1f} {row["lucro"]:>+12.2f} {cap_acum:>10.2f}')

print(f'\n{"="*60}')
tp_stats = df_t[df_t['saida']=='TP3']
sl_stats = df_t[df_t['saida']=='SL']
print(f'TP3 atingidos: {len(tp_stats)} | Media ganho: ${tp_stats["pnl_usd"].mean():.2f}' if len(tp_stats)>0 else 'TP3: 0')
print(f'SL atingidos : {len(sl_stats)} | Media perda: ${sl_stats["pnl_usd"].mean():.2f}' if len(sl_stats)>0 else 'SL: 0')

print('\nFIM')
