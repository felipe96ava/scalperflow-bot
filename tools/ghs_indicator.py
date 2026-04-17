"""
God Hunter Scalping (GHS) - Tradução Pine Script → Python
Original: blackcat1402 | https://br.tradingview.com/v/Hl8mcpte/

Sinais gerados:
  BUY  : B (tendência vira alta), BN (buy now), BB (buy burst), GH (god hunter)
  SELL : S (tendência vira baixa ou cruza 0/90)
"""
import numpy as np
import pandas as pd


# ── Funções auxiliares (espelho das Pine Script) ───────────────────

def xrf(series: pd.Series, length: int) -> pd.Series:
    """Primeiro valor não-NA olhando até `length` barras atrás."""
    out = series.copy()
    for lag in range(1, int(length) + 1):
        mask = out.isna()
        if not mask.any():
            break
        out = out.where(~mask, series.shift(lag))
    return out


def xsa(src: pd.Series, length: int, wei: float) -> pd.Series:
    """
    Smoothed MA customizado do Pine Script.
    Para wei=1 equivale ao Wilder RMA.
    """
    length = int(length)
    n      = len(src)
    sumf   = np.zeros(n)
    out    = np.full(n, np.nan)
    vals   = src.values.astype(float)

    for i in range(n):
        prev_sumf  = sumf[i - 1] if i > 0 else 0.0
        src_lag    = vals[i - length] if i >= length else np.nan
        sumf[i]    = prev_sumf - (0 if np.isnan(src_lag) else src_lag) + (0 if np.isnan(vals[i]) else vals[i])

        if i >= length and not np.isnan(src_lag):
            ma = sumf[i] / length
        else:
            ma = np.nan

        prev_out = out[i - 1] if i > 0 else np.nan
        if np.isnan(prev_out):
            out[i] = ma
        elif not np.isnan(vals[i]) and not np.isnan(prev_out):
            out[i] = (vals[i] * wei + prev_out * (length - wei)) / length

    return pd.Series(out, index=src.index)


def xda(src: pd.Series, coeff: float) -> pd.Series:
    """EMA com coeficiente direto."""
    out  = np.full(len(src), np.nan)
    vals = src.values.astype(float)
    for i in range(len(src)):
        if np.isnan(out[i - 1]) or i == 0:
            out[i] = vals[i]
        else:
            out[i] = coeff * vals[i] + (1 - coeff) * out[i - 1]
    return pd.Series(out, index=src.index)


def xsl(src: pd.Series, length: int) -> pd.Series:
    """Slope da regressão linear."""
    from numpy.polynomial import polynomial as P
    length = int(length)
    out    = np.full(len(src), np.nan)
    vals   = src.values.astype(float)
    for i in range(length - 1, len(src)):
        y = vals[i - length + 1: i + 1]
        if np.any(np.isnan(y)):
            continue
        x   = np.arange(length)
        c   = np.polyfit(x, y, 1)
        out[i] = c[0]   # slope
    return pd.Series(out, index=src.index)


def xcn(cond: pd.Series, length: int) -> pd.Series:
    """Conta quantas vezes `cond` foi True nos últimos `length` candles."""
    return cond.astype(int).rolling(int(length)).sum()


def xfl(cond: pd.Series, lbk: int) -> pd.Series:
    """Flag: True se cond ocorreu pelo menos uma vez nos últimos lbk barras."""
    return cond.rolling(int(lbk) + 1).max().fillna(0).astype(bool)


def xkdj(df: pd.DataFrame, m=9, n1=3, n2=3):
    """KDJ customizado."""
    c   = df['close']
    h   = df['high']
    l   = df['low']

    xrf_c4 = xrf(c, 4)
    xrf_c3 = xrf(c, 3)
    xrf_c2 = xrf(c, 2)
    xrf_c1 = xrf(c, 1)

    ed  = -0.4 * xrf_c4 - 0.4 * xrf_c3 - 1.1 * xrf_c2 + 0.9 * xrf_c1 + 2 * c
    ll  = l.rolling(m).min()
    hh  = h.rolling(m).max()
    rsv = (xsa(ed, 4, 1) - ll) / (hh - ll).replace(0, np.nan) * 100
    k   = xsa(rsv, n1, 1)
    d   = xsa(k, n2, 1)
    j   = 2 * k - 1 * d
    return k, d, j


# ── Cálculo principal ──────────────────────────────────────────────

def calcular_ghs(df: pd.DataFrame) -> pd.DataFrame:
    """
    Recebe DataFrame com colunas: open, high, low, close, volume, tick_volume
    Retorna o mesmo df com colunas de sinais GHS adicionadas.
    """
    c  = df['close'].astype(float)
    h  = df['high'].astype(float)
    l  = df['low'].astype(float)
    o  = df['open'].astype(float)
    vol = df['tick_volume'].astype(float)

    # x_1, x_2
    stoch9  = (c - l.rolling(9).min())  / (h.rolling(9).max()  - l.rolling(9).min()).replace(0, np.nan) * 100
    stoch10 = (c - l.rolling(10).min()) / (h.rolling(10).max() - l.rolling(10).min()).replace(0, np.nan) * 100
    x_1 = xsa(stoch9,  3, 1)
    x_2 = xsa(stoch10, 3, 1)

    # trend (x_3)
    x_3   = xsa((x_2 - 50) * 2, 3, 1) + xsa((x_1 - 50) * 2, 3, 1)
    trend = x_3

    # x_4, x_5 → god_hunter
    sma_close_20  = c.rolling(20).mean()
    sma_close_10  = c.rolling(10).mean()
    sma_close_5   = c.rolling(5).mean()
    slope20       = xsl(c, 20)
    x_4           = (l > (slope20 * 5 + c).rolling(10).mean()) & (l < sma_close_20)
    cross_under_5_10 = (sma_close_5.shift(1) >= sma_close_10.shift(1)) & (sma_close_5 < sma_close_10)
    x_5           = xcn(cross_under_5_10, 5) >= 1
    god_hunter    = x_4 & x_5

    # x_6 → buy_now, buy_secretly, buy_trial
    stoch27  = (c - l.rolling(27).min()) / (h.rolling(27).max() - l.rolling(27).min()).replace(0, np.nan) * 100
    raw6     = xsa(stoch27, 5, 1)
    x_6      = 3 * raw6 - 2 * xsa(raw6, 3, 1)
    buy_now      = (x_6.shift(1) <= 5) & (x_6 > 5)          # crossover(x_6, 5)
    buy_secretly = x_6 <= 5
    buy_trial    = x_6 <= 13

    # x_7 → buy_fast
    stoch5 = (c - l.rolling(5).min()) / (h.rolling(5).max() - l.rolling(5).min()).replace(0, np.nan) * 100
    raw7   = xsa(stoch5, 5, 1)
    x_7    = 4 * raw7 - 3 * xsa(raw7, 3.2, 1)
    x_8    = ((x_7.shift(1) <= 8) & (x_7 > 8)) & buy_trial   # crossover(x_7, 8) & buy_trial
    xrf_ll150 = xrf(l.rolling(150).min(), 3)
    x_9    = (l <= xrf_ll150) & buy_trial
    buy_fast = x_8 | x_9

    # KDJ
    k, d, j = xkdj(df)
    x_10  = xda((h + l + c * 2) / 4.15, 0.9)
    x_11  = xrf(x_10.ewm(span=3, adjust=False).mean(), 1)
    x_12  = (j < 50) & (c > xrf(c, 3))

    sma30   = c.rolling(30).mean()
    x_13    = (c - sma30) / sma30.replace(0, np.nan) * 100
    cross_under_sma5_0 = ((c - sma_close_5) / sma_close_5.replace(0, np.nan) * 100)
    x_14    = (cross_under_sma5_0.shift(1) >= 0) & (cross_under_sma5_0 < 0)
    x_15    = x_13 < xrf(x_13, 1)
    x_16    = sma_close_10 > xrf(sma_close_10, 1)
    x_17    = xfl(x_14 & x_15 & x_16, 10)

    x_18  = (2 * c + h + l) / 4
    x_19  = (x_18 - l.rolling(5).min()) / (h.rolling(5).max() - l.rolling(5).min()).replace(0, np.nan) * 100
    x_19  = x_19.ewm(span=5, adjust=False).mean().rolling(1).mean()
    x_20  = xrf(x_17, 2) & ((c.shift(1) <= c.ewm(span=5, adjust=False).mean().shift(1)) & (c > c.ewm(span=5, adjust=False).mean()))
    x_21  = (x_19 > xrf(x_19, 1)) & (xrf(x_19, 1) < xrf(x_19, 2))

    range_hl = (h - l).replace(0, np.nan)
    denom    = (range_hl * 2 - abs(c - o)).replace(0, np.nan)
    x_22     = vol / denom
    x_23     = np.where(c > o, x_22 * (h - l),
               np.where(c < o, x_22 * (h - o + c - l), vol / 2))
    x_24     = np.where(c > o, x_22 * (h - c + o - l),
               np.where(c < o, x_22 * (h - l), vol / 2))
    x_23     = pd.Series(x_23, index=df.index)
    x_24     = pd.Series(x_24, index=df.index)
    x_25     = j < 20
    x_26     = (x_23 > x_24 * 6) & x_25
    x_27     = ((c.shift(1) <= x_11.shift(1)) & (c > x_11)) & x_12
    x_28     = x_20 & x_21
    buy_burst = buy_trial & (x_26 | x_27 | x_28)

    # Direção do trend (long/short)
    x_29  = trend > xrf(trend, 1)
    x_30  = trend <= xrf(trend, 1)
    long  = x_29 & ~x_29.shift(1).fillna(False).astype(bool)
    short = x_30 & ~x_30.shift(1).fillna(False).astype(bool)

    # ── SINAIS FINAIS ───────────────────────────────────────────────
    # SELL: S label
    cross_under_trend_90 = (trend.shift(1) >= 90) & (trend < 90)
    cross_under_trend_0  = (trend.shift(1) >= 0)  & (trend < 0)
    sig_sell = ((short & ((trend > 0) | (trend < -90))) |
                cross_under_trend_90 | cross_under_trend_0)

    # BUY: B label
    cross_over_trend_neg90 = (trend.shift(1) <= -90) & (trend > -90)
    cross_over_trend_0     = (trend.shift(1) <= 0)   & (trend > 0)
    sig_buy = ((long & (trend < -120)) |
               cross_over_trend_neg90 | cross_over_trend_0)

    # BUY sinais específicos (requerem trend < -120)
    sig_buy_now    = buy_now    & (trend < -120)
    sig_buy_secret = buy_secretly & (trend < -120)
    sig_buy_burst  = buy_burst  & (trend < -120)
    sig_buy_fast   = buy_fast   & (trend < -120)
    sig_god_hunter = god_hunter & (trend < -100)

    df = df.copy()
    df['ghs_trend']      = trend
    df['ghs_sell']       = sig_sell
    df['ghs_buy']        = sig_buy
    df['ghs_buy_now']    = sig_buy_now
    df['ghs_buy_secret'] = sig_buy_secret
    df['ghs_buy_burst']  = sig_buy_burst
    df['ghs_buy_fast']   = sig_buy_fast
    df['ghs_god_hunter'] = sig_god_hunter

    return df


def sinal_ghs(df: pd.DataFrame):
    """
    Analisa as últimas 2 barras e retorna:
      ('BUY', 'tipo') ou ('SELL', 'S') ou None
    """
    if df is None or len(df) < 2:
        return None, None

    df = calcular_ghs(df)
    prev = df.iloc[-2]   # barra anterior (fechada)

    # Prioridade: sinais mais fortes primeiro
    if prev['ghs_god_hunter']:
        return 'BUY', 'GH (God Hunter)'
    if prev['ghs_buy_burst']:
        return 'BUY', 'BB (Buy Burst)'
    if prev['ghs_buy_now']:
        return 'BUY', 'BN (Buy Now)'
    if prev['ghs_buy_fast']:
        return 'BUY', 'BF (Buy Fast)'
    if prev['ghs_buy']:
        return 'BUY', 'B (Tendência Alta)'
    if prev['ghs_sell']:
        return 'SELL', 'S (Tendência Baixa)'

    return None, None
