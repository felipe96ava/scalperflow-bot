import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
import os
import time

LOGIN = 83090423
PASSWORD = 'aEzakmi931018@'
SERVER = 'Exness-MT5Trial12'
PATH = 'C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe'
SYMBOL = 'XAUUSDz'

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def sinal_rsi(v):
    if v >= 70: return 'SOBRECOMPRADO'
    if v <= 30: return 'SOBREVENDIDO'
    if v >= 55: return 'FORTE ALTA'
    if v <= 45: return 'FRAQUEZA'
    return 'NEUTRO'

def sinal_ema(e9, e21):
    diff = e9 - e21
    if diff > 0: return f'ALTA  (+{diff:.2f})'
    return f'BAIXA ({diff:.2f})'

mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)
mt5.symbol_select(SYMBOL, True)

while True:
    try:
        tick = mt5.symbol_info_tick(SYMBOL)

        rates_m1  = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1,  0, 60)
        rates_m5  = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5,  0, 60)
        rates_m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 60)
        rates_h1  = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1,  0, 30)

        def calc(rates):
            df = pd.DataFrame(rates)
            df['ema9']  = ema(df['close'], 9)
            df['ema21'] = ema(df['close'], 21)
            df['rsi']   = rsi(df['close'])
            df['tr']    = np.maximum(df['high'] - df['low'],
                          np.maximum(abs(df['high'] - df['close'].shift(1)),
                                     abs(df['low']  - df['close'].shift(1))))
            df['atr']   = df['tr'].rolling(14).mean()
            r = df.iloc[-1]
            p = df.iloc[-2]
            cross = '*** CRUZAMENTO ALTA ***' if (p['ema9'] <= p['ema21'] and r['ema9'] > r['ema21']) else \
                    '*** CRUZAMENTO BAIXA ***' if (p['ema9'] >= p['ema21'] and r['ema9'] < r['ema21']) else ''
            return r, cross

        r1,  cx1  = calc(rates_m1)
        r5,  cx5  = calc(rates_m5)
        r15, cx15 = calc(rates_m15)
        rh1, cxh1 = calc(rates_h1)

        tendencia = 'ALTA' if rh1['close'] > rh1['ema21'] else 'BAIXA'

        # Sinal geral
        ema_bull = r5['ema9'] > r5['ema21']
        rsi_ok   = r5['rsi'] < 70
        if ema_bull and tendencia == 'ALTA' and rsi_ok:
            sinal_geral = '>> BUY <<'
        elif not ema_bull and tendencia == 'BAIXA' and r5['rsi'] > 30:
            sinal_geral = '>> SELL <<'
        else:
            sinal_geral = '-- AGUARDAR --'

        os.system('cls')
        print('=' * 52)
        print(f'  DASHBOARD SCALPING — {SYMBOL}')
        print(f'  {datetime.now().strftime("%d/%m/%Y  %H:%M:%S")}')
        print('=' * 52)
        print(f'  BID: {tick.bid:<10.3f}  ASK: {tick.ask:<10.3f}')
        print(f'  SPREAD: {round(tick.ask - tick.bid, 3)} pts')
        print('=' * 52)
        print(f'  {"TF":<6} {"EMA9":>9} {"EMA21":>9} {"SINAL EMA":<20} {"RSI":>6} {"ATR":>7}')
        print('-' * 52)
        for lbl, r, cx in [('M1', r1, cx1), ('M5', r5, cx5), ('M15', r15, cx15), ('H1', rh1, cxh1)]:
            print(f'  {lbl:<6} {r["ema9"]:>9.2f} {r["ema21"]:>9.2f} {sinal_ema(r["ema9"], r["ema21"]):<20} {r["rsi"]:>6.1f} {r["atr"]:>7.2f}')
            if cx:
                print(f'         {cx}')
        print('=' * 52)
        print(f'  TENDENCIA H1   : {tendencia}')
        print(f'  RSI M5         : {r5["rsi"]:.1f}  {sinal_rsi(r5["rsi"])}')
        print(f'  SINAL SCALP    : {sinal_geral}')
        print('=' * 52)
        print('  Atualiza a cada 10s  |  Ctrl+C para sair')

    except Exception as e:
        print(f'Erro: {e}')
        mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)

    time.sleep(10)
