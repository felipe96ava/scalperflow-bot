"""
ScalperFlow Bot - Estrategias por par
XAUUSDz : Cruzamento EMA20x50 | M5 | SL=2.0x | H1 trend + ATR>=6
BTCUSDz : Cruzamento EMA20x50 + RSI + H1 trend + Sessao NY | M15 | SL=1.5x
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
import time

# ── Credenciais (carregadas de config.py — nao commitado) ──────────
try:
    from config import LOGIN, PASSWORD, SERVER, PATH
except ImportError:
    raise SystemExit(
        'ERRO: arquivo config.py nao encontrado.\n'
        'Copie config.example.py para config.py e preencha suas credenciais.'
    )

MAGIC = 654321

# ── Configuracao por par ───────────────────────────────────────────
# estrategia: 'scalperflow' = cruzamento simples
#             'btc_filtered' = cruzamento + RSI + H1 + sessao NY
SYMBOLS = {
    'XAUUSDz': {
        'lot'       : 0.10,
        'sl_atr'    : 2.0,
        'estrategia': 'scalperflow',
        'tfs'       : {'M5': mt5.TIMEFRAME_M5},  # M3 removido: gerava sinais duplicados
        'atr_min'   : 3.0,                        # so opera com volatilidade suficiente
    },
    'BTCUSDz': {
        'lot'      : 0.20,
        'sl_atr'   : 1.5,
        'estrategia': 'btc_filtered',
        'tfs'      : {'M15': mt5.TIMEFRAME_M15},
        # Filtros especificos BTC
        'sessao_h0': 13,   # Sessao NY inicio (UTC)
        'sessao_h1': 21,   # Sessao NY fim (UTC)
        'rsi_buy'  : 50,   # RSI minimo para BUY
        'rsi_sell' : 50,   # RSI maximo para SELL
    },
}

# ── Parametros globais ─────────────────────────────────────────────
EMA_FAST = 20
EMA_SLW  = 50
TP1_ATR  = 2.0
TP2_ATR  = 3.5
TP3_ATR  = 5.0

TRAIL_ACTIVATE_ATR = 1.0
TRAIL_DISTANCE_ATR = 1.0

VOL_MULTIPLIER  = 1.5
VOL_PERIOD      = 20
BODY_SHADOW_MAX = 0.3

CHECK_INTERVAL = 5

# ── Estado ────────────────────────────────────────────────────────
tp_tracker  = {}
ultima_barra = {(sym, tf): None for sym, cfg in SYMBOLS.items() for tf in cfg['tfs']}
h1_cache     = {}   # cache do H1 por simbolo

# ── Funcoes base ───────────────────────────────────────────────────
def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)

def conectar():
    mt5.shutdown()
    ok = mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, path=PATH)
    if ok:
        for sym in SYMBOLS:
            mt5.symbol_select(sym, True)
    return ok

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi_calc(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def calcular_tf(symbol, timeframe, bars=300):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    if rates is None or len(rates) < EMA_SLW + 20:
        return None
    df = pd.DataFrame(rates)
    df['time_dt']    = pd.to_datetime(df['time'], unit='s')
    df['hour_utc']   = df['time_dt'].dt.hour
    df['ema_fast']   = ema(df['close'], EMA_FAST)
    df['ema_slow']   = ema(df['close'], EMA_SLW)
    df['tr']         = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)),
                   abs(df['low']  - df['close'].shift(1)))
    )
    df['atr']        = df['tr'].rolling(14).mean()
    df['tick_volume']= df['tick_volume'].astype(float)
    df['rsi']        = rsi_calc(df['close'].astype(float))
    return df

def carregar_h1(symbol):
    """Carrega H1 e calcula tendencia (EMA20 > EMA50)."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 500)
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    c  = df['close'].astype(float)
    df['ema20'] = ema(c, 20)
    df['ema50'] = ema(c, 50)
    df['trend_alta'] = df['ema20'] > df['ema50']
    return df[['time', 'trend_alta']].set_index('time')

def tendencia_h1(symbol, ts):
    """Retorna True=alta, False=baixa para o timestamp ts."""
    h1 = h1_cache.get(symbol)
    if h1 is None:
        return None
    pos = h1.index.searchsorted(ts, side='right') - 1
    if pos < 0:
        return None
    return bool(h1['trend_alta'].iloc[pos])

def posicoes_abertas(symbol):
    pos = mt5.positions_get(symbol=symbol)
    return [p for p in pos if p.magic == MAGIC] if pos else []

# ── Deteccao de sinais ─────────────────────────────────────────────
def detectar_cruzamento(df):
    if df is None or len(df) < 3:
        return None
    cur  = df.iloc[-1]
    prev = df.iloc[-2]
    if prev['ema_fast'] <= prev['ema_slow'] and cur['ema_fast'] > cur['ema_slow']:
        return 'BUY'
    if prev['ema_fast'] >= prev['ema_slow'] and cur['ema_fast'] < cur['ema_slow']:
        return 'SELL'
    return None

def detectar_absorcao(df):
    if df is None or len(df) < VOL_PERIOD + 2:
        return None
    idx = len(df) - 2
    row = df.iloc[idx]
    avg_vol  = df['tick_volume'].iloc[max(0, idx - VOL_PERIOD):idx].mean()
    vol_spike= row['tick_volume'] > avg_vol * VOL_MULTIPLIER
    body     = abs(row['close'] - row['open'])
    upper_sh = row['high']  - max(row['open'], row['close'])
    lower_sh = min(row['open'], row['close']) - row['low']
    total_sh = upper_sh + lower_sh
    rejeicao = (total_sh > 0) and (body < total_sh * BODY_SHADOW_MAX)
    if not (vol_spike and rejeicao):
        return None
    return 'ABSORCAO_COMPRA' if row['close'] > row['open'] else 'ABSORCAO_VENDA'

def avaliar_sinal_xau(df, cfg):
    """
    Estrategia XAU filtrada:
    1. Cruzamento EMA20x50 OU Absorcao
    2. ATR >= atr_min (evita mercado parado)
    3. Tendencia H1 alinhada com a direcao
    """
    atr_val = df.iloc[-1]['atr']

    # Filtro ATR minimo
    atr_min = cfg.get('atr_min', 0)
    if atr_val < atr_min:
        return None, None

    sinal_cross = detectar_cruzamento(df)
    sinal_abs   = detectar_absorcao(df)

    if sinal_abs == 'ABSORCAO_COMPRA':
        sinal_final = 'SELL'
        tipo_sinal  = 'Absorcao Compra -> SELL'
    elif sinal_abs == 'ABSORCAO_VENDA':
        sinal_final = 'BUY'
        tipo_sinal  = 'Absorcao Venda -> BUY'
    elif sinal_cross:
        sinal_final = sinal_cross
        tipo_sinal  = f'Cruzamento EMA {sinal_cross}'
    else:
        return None, None

    # Filtro H1 trend
    trend = tendencia_h1('XAUUSDz', df['time'].iloc[-1])
    if trend is not None:
        if sinal_final == 'BUY' and not trend:
            log(f'[XAUUSDz] {tipo_sinal} bloqueado — H1 em baixa')
            return None, None
        if sinal_final == 'SELL' and trend:
            log(f'[XAUUSDz] {tipo_sinal} bloqueado — H1 em alta')
            return None, None

    return sinal_final, f'[XAUUSDz][M5] {tipo_sinal} | ATR={atr_val:.2f} | H1 ok'


def avaliar_sinal_btc(df, cfg):
    """
    Estrategia BTC filtrada:
    1. Cruzamento EMA20x50
    2. RSI alinhado com direcao
    3. Tendencia H1 alinhada
    4. Dentro da sessao NY
    """
    sinal = detectar_cruzamento(df)
    if sinal is None:
        return None, None

    cur = df.iloc[-1]

    # Filtro sessao NY
    hora = cur['hour_utc']
    if not (cfg['sessao_h0'] <= hora < cfg['sessao_h1']):
        log(f'[BTCUSDz] Cruzamento {sinal} fora da sessao NY (hora UTC={hora}) — ignorado')
        return None, None

    # Filtro RSI
    rsi_val = cur['rsi']
    if sinal == 'BUY'  and rsi_val < cfg['rsi_buy']:
        log(f'[BTCUSDz] Cruzamento BUY bloqueado por RSI={rsi_val:.1f} < {cfg["rsi_buy"]}')
        return None, None
    if sinal == 'SELL' and rsi_val > cfg['rsi_sell']:
        log(f'[BTCUSDz] Cruzamento SELL bloqueado por RSI={rsi_val:.1f} > {cfg["rsi_sell"]}')
        return None, None

    # Filtro H1
    trend = tendencia_h1('BTCUSDz', df['time'].iloc[-1])
    if trend is not None:
        if sinal == 'BUY'  and not trend:
            log(f'[BTCUSDz] Cruzamento BUY bloqueado por tendencia H1 de baixa')
            return None, None
        if sinal == 'SELL' and trend:
            log(f'[BTCUSDz] Cruzamento SELL bloqueado por tendencia H1 de alta')
            return None, None

    return sinal, f'[BTCUSDz][M15] EMA {sinal} + RSI={rsi_val:.0f} + H1 ok'

# ── Execucao de ordens ─────────────────────────────────────────────
def executar_ordem(symbol, lot, sl_atr, direcao, atr, tick, tf_nome):
    if direcao == 'BUY':
        entry = tick.ask
        sl    = round(entry - atr * sl_atr,  2)
        tp1   = round(entry + atr * TP1_ATR, 2)
        tp2   = round(entry + atr * TP2_ATR, 2)
        tp3   = round(entry + atr * TP3_ATR, 2)
        tipo  = mt5.ORDER_TYPE_BUY
    else:
        entry = tick.bid
        sl    = round(entry + atr * sl_atr,  2)
        tp1   = round(entry - atr * TP1_ATR, 2)
        tp2   = round(entry - atr * TP2_ATR, 2)
        tp3   = round(entry - atr * TP3_ATR, 2)
        tipo  = mt5.ORDER_TYPE_SELL

    result = mt5.order_send({
        'action'      : mt5.TRADE_ACTION_DEAL,
        'symbol'      : symbol,
        'volume'      : lot,
        'type'        : tipo,
        'price'       : entry,
        'sl'          : sl,
        'tp'          : tp3,
        'deviation'   : 20,
        'magic'       : MAGIC,
        'comment'     : f'sf_{tf_nome.lower()}_{direcao.lower()}',
        'type_time'   : mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    })

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        tp_tracker[result.order] = {
            'entry': entry, 'sl_inicial': sl,
            'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'atr': atr, 'tipo': direcao, 'nivel': 0,
            'symbol': symbol,
        }
    return result, entry, sl, tp1, tp2, tp3

def modificar_sl(posicao, novo_sl):
    r = mt5.order_send({
        'action'  : mt5.TRADE_ACTION_SLTP,
        'symbol'  : posicao.symbol,
        'position': posicao.ticket,
        'sl'      : novo_sl,
        'tp'      : posicao.tp,
    })
    return r.retcode == mt5.TRADE_RETCODE_DONE

def fechar_posicao(posicao, tick):
    tipo  = mt5.ORDER_TYPE_SELL if posicao.type == 0 else mt5.ORDER_TYPE_BUY
    preco = tick.bid if posicao.type == 0 else tick.ask
    r = mt5.order_send({
        'action'      : mt5.TRADE_ACTION_DEAL,
        'symbol'      : posicao.symbol,
        'volume'      : posicao.volume,
        'type'        : tipo,
        'position'    : posicao.ticket,
        'price'       : preco,
        'deviation'   : 20,
        'magic'       : MAGIC,
        'comment'     : 'tp3_close',
        'type_time'   : mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    })
    return r.retcode == mt5.TRADE_RETCODE_DONE

def gerenciar_tp(posicao, tick, atr):
    ticket = posicao.ticket
    if ticket not in tp_tracker:
        e    = posicao.price_open
        is_b = posicao.type == 0
        tp_tracker[ticket] = {
            'entry': e, 'sl_inicial': posicao.sl,
            'tp1': round(e + atr * TP1_ATR, 2) if is_b else round(e - atr * TP1_ATR, 2),
            'tp2': round(e + atr * TP2_ATR, 2) if is_b else round(e - atr * TP2_ATR, 2),
            'tp3': round(e + atr * TP3_ATR, 2) if is_b else round(e - atr * TP3_ATR, 2),
            'atr': atr, 'nivel': 0, 'symbol': posicao.symbol,
        }

    t             = tp_tracker[ticket]
    nivel         = t['nivel']
    entry         = t['entry']
    tp1, tp2, tp3 = t['tp1'], t['tp2'], t['tp3']
    is_buy        = posicao.type == 0
    preco         = tick.bid if is_buy else tick.ask
    sl_atual      = posicao.sl

    if nivel < 3 and ((is_buy and preco >= tp3) or (not is_buy and preco <= tp3)):
        if fechar_posicao(posicao, tick):
            tp_tracker[ticket]['nivel'] = 3
            log(f'TP3! [{posicao.symbol}] Ticket={ticket} preco={preco:.3f} lucro={posicao.profit:.2f}')
        return

    if nivel < 2 and ((is_buy and preco >= tp2) or (not is_buy and preco <= tp2)):
        if modificar_sl(posicao, tp1):
            tp_tracker[ticket]['nivel'] = 2
            log(f'TP2! [{posicao.symbol}] Ticket={ticket} SL->TP1={tp1:.3f}')
        return

    if nivel < 1 and ((is_buy and preco >= tp1) or (not is_buy and preco <= tp1)):
        if modificar_sl(posicao, entry):
            tp_tracker[ticket]['nivel'] = 1
            log(f'TP1! [{posicao.symbol}] Ticket={ticket} SL->breakeven={entry:.3f}')
        return

    # Trailing ativo apenas apos TP2 (nivel >= 2), quando o SL ja esta no TP1
    # Antes disso o trailing movia o SL para o breakeven prematuramente,
    # causando saidas por pullbacks antes de atingir o TP3.
    if nivel >= 2:
        dist_trail = atr * TRAIL_DISTANCE_ATR
        if is_buy:
            novo_sl = round(preco - dist_trail, 2)
            if novo_sl > sl_atual and modificar_sl(posicao, novo_sl):
                log(f'TRAILING [{posicao.symbol}] Ticket={ticket} SL {sl_atual:.3f}->{novo_sl:.3f}')
        else:
            novo_sl = round(preco + dist_trail, 2)
            if novo_sl < sl_atual and modificar_sl(posicao, novo_sl):
                log(f'TRAILING [{posicao.symbol}] Ticket={ticket} SL {sl_atual:.3f}->{novo_sl:.3f}')

# ── Inicializacao ──────────────────────────────────────────────────
log('ScalperFlow Bot iniciado')
log('  XAUUSDz : lote=0.10 | M5 | SL=2.0x | EMA crossover + H1 trend + ATR>=6')
log('  BTCUSDz : lote=0.20 | M15 | SL=1.5x | EMA + RSI + H1 + Sessao NY')
log(f'  TP1={TP1_ATR}x | TP2={TP2_ATR}x | TP3={TP3_ATR}x | EMA{EMA_FAST}x{EMA_SLW}')

if not conectar():
    log('ERRO: Falha ao conectar no MT5')
    exit(1)

# Pre-carregar H1 de todos os simbolos
for _sym in SYMBOLS:
    h1_cache[_sym] = carregar_h1(_sym)
    log(f'H1 {_sym} carregado para filtro de tendencia')

_falhas_conexao  = 0
_ultima_h1_update = 0   # timestamp do ultimo refresh do H1

while True:
    try:
        # Reconexao automatica
        if not mt5.terminal_info():
            log('MT5 desconectado — reconectando...')
            if not conectar():
                _falhas_conexao += 1
                log(f'Falha reconexao #{_falhas_conexao}, aguardando 15s...')
                time.sleep(15)
                continue
            else:
                _falhas_conexao = 0
                log('Reconexao OK')

        agora = time.time()

        # Atualizar H1 de todos os simbolos a cada 15 minutos
        if agora - _ultima_h1_update > 900:
            for _sym in SYMBOLS:
                h1_cache[_sym] = carregar_h1(_sym)
            _ultima_h1_update = agora

        # ── Iterar sobre cada par ─────────────────────────────────
        for symbol, cfg in SYMBOLS.items():
            lot        = cfg['lot']
            sl_atr     = cfg['sl_atr']
            tfs        = cfg['tfs']
            estrategia = cfg['estrategia']
            tick       = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue
            posicoes = posicoes_abertas(symbol)

            # Gerenciar TPs com ATR do menor TF
            tf_ref_val = list(tfs.values())[0]
            df_ref = calcular_tf(symbol, tf_ref_val)
            if df_ref is not None:
                atr_ref = df_ref.iloc[-1]['atr']
                for pos in posicoes:
                    gerenciar_tp(pos, tick, atr_ref)

                # Status a cada 60s
                if int(agora) % 60 < CHECK_INTERVAL:
                    cur  = df_ref.iloc[-1]
                    diff = cur['ema_fast'] - cur['ema_slow']
                    dir_ema = 'ALTA' if diff > 0 else 'BAIXA'
                    pos_info = f'{len(posicoes)} pos' if posicoes else 'sem pos'
                    extra = ''
                    t_h1   = tendencia_h1(symbol, df_ref['time'].iloc[-1])
                    h1_str = 'H1-ALTA' if t_h1 else ('H1-BAIXA' if t_h1 is not None else 'H1-?')
                    if estrategia == 'scalperflow':
                        atr_ok = 'ATR-OK' if atr_ref >= cfg.get('atr_min', 0) else f'ATR-BAIXO(<{cfg.get("atr_min",0)})'
                        extra  = f' {atr_ok} {h1_str}'
                    elif estrategia == 'btc_filtered':
                        h = cur['hour_utc']
                        ny = 'NY-OK' if cfg['sessao_h0'] <= h < cfg['sessao_h1'] else 'fora-NY'
                        extra = f' RSI={cur["rsi"]:.0f} {ny} {h1_str}'
                    log(f'[{symbol}] Bid={tick.bid:.3f} | EMAd={diff:+.2f} | ATR={atr_ref:.2f} | {dir_ema}{extra} | {pos_info}')

            # Verificar sinais por timeframe
            for tf_nome, tf_val in tfs.items():
                df = calcular_tf(symbol, tf_val)
                if df is None:
                    continue

                barra_atual = df.iloc[-1]['time'] if 'time' in df.columns else None
                atr_val     = df.iloc[-1]['atr']

                # Ignora se ja processou esta barra (evita logs/ordens duplicados)
                chave = (symbol, tf_nome)
                if barra_atual == ultima_barra[chave]:
                    continue
                ultima_barra[chave] = barra_atual

                # ── XAUUSDz: EMA crossover + ATR min + H1 trend ──
                if estrategia == 'scalperflow':
                    sinal_final, sinal_tipo = avaliar_sinal_xau(df, cfg)
                    if sinal_final is None:
                        continue

                # ── BTCUSDz: estrategia filtrada ──────────────────
                elif estrategia == 'btc_filtered':
                    sinal_final, sinal_tipo = avaliar_sinal_btc(df, cfg)
                    if sinal_final is None:
                        continue

                else:
                    continue

                if posicoes:
                    log(f'Sinal {sinal_tipo} — posicao aberta, aguardando...')
                else:
                    log(f'*** {sinal_tipo} ***')
                    result, entry, sl, tp1, tp2, tp3 = executar_ordem(
                        symbol, lot, sl_atr, sinal_final, atr_val, tick, tf_nome)
                    if result.retcode == mt5.TRADE_RETCODE_DONE:
                        log(f'ORDEM EXECUTADA! [{symbol}]')
                        log(f'  Dir    : {sinal_final} | TF: {tf_nome}')
                        log(f'  Entry  : {entry:.3f}')
                        log(f'  SL     : {sl:.3f}  (-{abs(entry-sl):.3f})')
                        log(f'  TP1    : {tp1:.3f}  (+{abs(tp1-entry):.3f}) -> breakeven')
                        log(f'  TP2    : {tp2:.3f}  (+{abs(tp2-entry):.3f}) -> SL no TP1')
                        log(f'  TP3    : {tp3:.3f}  (+{abs(tp3-entry):.3f}) -> fecha tudo')
                        log(f'  Ticket : {result.order}')
                    else:
                        log(f'ERRO [{symbol}]: {result.retcode} - {result.comment}')
                    break

    except Exception as e:
        log(f'Erro: {e}')
        conectar()

    time.sleep(CHECK_INTERVAL)
