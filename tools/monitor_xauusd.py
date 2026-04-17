import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
import time

LOGIN = 83090423
PASSWORD = 'aEzakmi931018@'
SERVER = 'Exness-MT5Trial12'
PATH = 'C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe'
SYMBOL = 'XAUUSDz'
LOT = 0.10

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def connect():
    mt5.shutdown()
    return mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)

def analisar():
    rates_m5 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 60)
    rates_h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 30)
    df5 = pd.DataFrame(rates_m5)
    dfh1 = pd.DataFrame(rates_h1)

    df5['ema9'] = ema(df5['close'], 9)
    df5['ema21'] = ema(df5['close'], 21)
    df5['rsi'] = rsi(df5['close'])
    df5['tr'] = np.maximum(df5['high'] - df5['low'],
                 np.maximum(abs(df5['high'] - df5['close'].shift(1)),
                            abs(df5['low'] - df5['close'].shift(1))))
    df5['atr'] = df5['tr'].rolling(14).mean()

    dfh1['ema21'] = ema(dfh1['close'], 21)
    tendencia_h1 = 'ALTA' if dfh1['close'].iloc[-1] > dfh1['ema21'].iloc[-1] else 'BAIXA'

    atual = df5.iloc[-1]
    anterior = df5.iloc[-2]

    cruzamento_alta = (anterior['ema9'] <= anterior['ema21']) and (atual['ema9'] > atual['ema21'])

    return {
        'ema9': atual['ema9'],
        'ema21': atual['ema21'],
        'rsi': atual['rsi'],
        'atr': atual['atr'],
        'tendencia_h1': tendencia_h1,
        'cruzamento_alta': cruzamento_alta,
    }

def executar_buy(tick, atr):
    entry = tick.ask
    sl = round(entry - atr * 1.5, 2)
    tp = round(entry + atr * 2.0, 2)

    request = {
        'action': mt5.TRADE_ACTION_DEAL,
        'symbol': SYMBOL,
        'volume': LOT,
        'type': mt5.ORDER_TYPE_BUY,
        'price': entry,
        'sl': sl,
        'tp': tp,
        'deviation': 20,
        'magic': 123456,
        'comment': 'scalp_ema_crossover',
        'type_time': mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    return result, entry, sl, tp

connect()
mt5.symbol_select(SYMBOL, True)
print(f'[{datetime.now().strftime("%H:%M:%S")}] Monitorando {SYMBOL}... aguardando cruzamento EMA9 > EMA21 no M5')

try:
    while True:
        try:
            dados = analisar()
            tick = mt5.symbol_info_tick(SYMBOL)
            hora = datetime.now().strftime('%H:%M:%S')

            print(f'[{hora}] EMA9={dados["ema9"]:.2f} EMA21={dados["ema21"]:.2f} RSI={dados["rsi"]:.1f} H1={dados["tendencia_h1"]} Cruzamento={dados["cruzamento_alta"]}')

            if dados['cruzamento_alta'] and dados['tendencia_h1'] == 'ALTA' and dados['rsi'] < 70:
                print(f'[{hora}] *** SINAL DE COMPRA DETECTADO! Executando BUY... ***')
                resultado, entry, sl, tp = executar_buy(tick, dados['atr'])
                if resultado.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f'[{hora}] ORDEM EXECUTADA: Entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} Ticket={resultado.order}')
                else:
                    print(f'[{hora}] ERRO na ordem: {resultado.retcode} - {resultado.comment}')
                break

        except Exception as e:
            print(f'Erro: {e}')
            connect()

        time.sleep(30)

finally:
    mt5.shutdown()
    print('Monitor encerrado.')
