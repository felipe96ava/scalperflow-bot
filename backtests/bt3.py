import MetaTrader5 as mt5
import pandas as pd
import numpy as np

mt5.initialize(login=83090423, password='aEzakmi931018@', server='Exness-MT5Trial12',
               path='C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe')

TP1,TP2,TP3 = 2.0, 3.5, 5.0

def ec(s,p): return s.ewm(span=p,adjust=False).mean()
def rsi_c(s,p=14):
    d=s.diff(); g=d.clip(lower=0).rolling(p).mean(); l=(-d.clip(upper=0)).rolling(p).mean()
    return 100-100/(1+g/l.replace(0,np.nan))

def load(sym,tf):
    r=mt5.copy_rates_from_pos(sym,tf,0,5000)
    if r is None: return None
    df=pd.DataFrame(r)
    df['hour']=pd.to_datetime(df['time'],unit='s').dt.hour
    c=df['close'].astype(float); h=df['high'].astype(float); l=df['low'].astype(float)
    df['atr']=np.maximum(h-l,np.maximum(abs(h-c.shift(1)),abs(l-c.shift(1)))).rolling(14).mean()
    df['tv']=df['tick_volume'].astype(float); df['va']=df['tv'].rolling(20).mean().shift(1)
    df['rsi']=rsi_c(c); df['e9']=ec(c,9); df['e21']=ec(c,21)
    m=c.rolling(20).mean(); st=c.rolling(20).std()
    df['bu']=m+2*st; df['bd']=m-2*st
    df['open']=df['open'].astype(float)
    return df.dropna().reset_index(drop=True)

def load_h1(sym):
    r=mt5.copy_rates_from_pos(sym,mt5.TIMEFRAME_H1,0,3000)
    if r is None: return None
    df=pd.DataFrame(r); c=df['close'].astype(float)
    df['up']=ec(c,20)>ec(c,50)
    return df[['time','up']].set_index('time')

def h1trend(h1,ts):
    p=h1.index.searchsorted(ts,'right')-1
    return bool(h1['up'].iloc[p]) if p>=0 else None

def run(df, h1, mode, sl, ema_k='e21', vol=1.3, h0=7, h1e=21, req_c=True,
        bb_dev=2.0, rsi_buy=30, rsi_sell=70, ci=True):
    trades=[]; it=False; en=sa=t1=t2=t3=0; di=None; nv=0

    # Para BTC: recalcular BB com dev especifico
    if mode == 'BB':
        c2=df['close'].astype(float); m=c2.rolling(20).mean(); st=c2.rolling(20).std()
        df=df.copy(); df['bu2']=m+bb_dev*st; df['bd2']=m-bb_dev*st

    for i in range(2, len(df)):
        cur=df.iloc[i]; prev=df.iloc[i-1]
        if it:
            if di=='BUY':
                if cur['low']<=sa: trades.append(sa-en); it=False; continue
                if cur['high']>=t3: trades.append(t3-en); it=False; continue
                if nv<2 and cur['high']>=t2: nv=2; sa=t1
                if nv<1 and cur['high']>=t1: nv=1; sa=en
            else:
                if cur['high']>=sa: trades.append(en-sa); it=False; continue
                if cur['low']<=t3: trades.append(en-t3); it=False; continue
                if nv<2 and cur['low']<=t2: nv=2; sa=t1
                if nv<1 and cur['low']<=t1: nv=1; sa=en
            continue

        if not(h0<=cur['hour']<h1e): continue
        atr=cur['atr']
        if not atr or np.isnan(atr): continue
        if vol>1 and (pd.isna(cur['va']) or cur['tv']<cur['va']*vol): continue

        d = None

        if mode == 'PULLBACK':
            trend=h1trend(h1, df['time'].iloc[i])
            if trend is None: continue
            ev=cur[ema_k]
            if abs(cur['close']-ev)>atr*0.5: continue
            if not(35<=cur['rsi']<=65): continue
            d='BUY' if trend else 'SELL'
            if d=='BUY' and cur['close']>ev*1.002: continue
            if d=='SELL' and cur['close']<ev*0.998: continue
            if req_c:
                bo=abs(cur['close']-cur['open'])
                sh=(cur['open']-cur['low']) if d=='BUY' else (cur['high']-cur['open'])
                if bo==0 or sh<bo*1.5: continue

        elif mode == 'BB':
            if cur['low']<=cur['bd2'] and cur['rsi']<=rsi_buy:
                if ci and cur['close']<=cur['bd2']: continue
                d='BUY'
            elif cur['high']>=cur['bu2'] and cur['rsi']>=rsi_sell:
                if ci and cur['close']>=cur['bu2']: continue
                d='SELL'

        if d is None: continue
        en=cur['close']
        if d=='BUY': sa=en-atr*sl; t1=en+atr*TP1; t2=en+atr*TP2; t3=en+atr*TP3
        else:        sa=en+atr*sl; t1=en-atr*TP1; t2=en-atr*TP2; t3=en-atr*TP3
        di=d; it=True; nv=0
    return trades

def stats(t):
    if len(t)<12: return None
    w=[x for x in t if x>0]; l=[x for x in t if x<=0]
    if not l or sum(l)==0: return None
    eq=np.cumsum(t); pk=np.maximum.accumulate(eq)
    return {'n':len(t),'wr':round(len(w)/len(t)*100,1),'pf':round(sum(w)/abs(sum(l)),2),
            'exp':round(np.mean(t),2),'dd':round((eq-pk).min(),2)}

TFS = [('M5',mt5.TIMEFRAME_M5),('M15',mt5.TIMEFRAME_M15),('M30',mt5.TIMEFRAME_M30)]
res = []

# XAUUSDz - Pullback
print("=== XAUUSDz - Pullback EMA + H1 ===", flush=True)
h1x = load_h1('XAUUSDz')
for tf_n,tf_v in TFS:
    df=load('XAUUSDz',tf_v)
    if df is None: continue
    for sl in [1.5,2.0,2.5]:
        for ema_k in ['e9','e21']:
            for vol in [1.0,1.3,1.5]:
                for h0,h1e in [(0,24),(7,21),(7,16),(13,21)]:
                    for req_c in [False,True]:
                        t=run(df,h1x,'PULLBACK',sl,ema_k=ema_k,vol=vol,h0=h0,h1e=h1e,req_c=req_c)
                        s=stats(t)
                        if s and s['wr']>=55:
                            cfg=f"{tf_n} {ema_k} SL={sl} VOL={vol} {h0}-{h1e}h C={req_c}"
                            print(f"  {cfg} | N={s['n']} WR={s['wr']}% PF={s['pf']} Exp={s['exp']:+.1f}", flush=True)
                            res.append({'par':'XAU','cfg':cfg,**s,'score':s['wr']*s['pf']*np.log1p(s['n'])})

# BTCUSDz - Bollinger
print("\n=== BTCUSDz - Bollinger MeanReversion ===", flush=True)
for tf_n,tf_v in TFS:
    df=load('BTCUSDz',tf_v)
    if df is None: continue
    for sl in [1.5,2.0,2.5]:
        for bb_dev in [1.8,2.0,2.2,2.5]:
            for rb,rs in [(25,75),(30,70),(35,65)]:
                for vol in [1.0,1.3,1.5]:
                    for h0,h1e in [(0,24),(7,21),(13,21)]:
                        for ci in [False,True]:
                            t=run(df,None,'BB',sl,vol=vol,h0=h0,h1e=h1e,bb_dev=bb_dev,
                                  rsi_buy=rb,rsi_sell=rs,ci=ci)
                            s=stats(t)
                            if s and s['wr']>=58:
                                cfg=f"{tf_n} BB={bb_dev} SL={sl} RSI={rb}/{rs} VOL={vol} {h0}-{h1e}h CI={ci}"
                                print(f"  {cfg} | N={s['n']} WR={s['wr']}% PF={s['pf']} Exp={s['exp']:+.1f}", flush=True)
                                res.append({'par':'BTC','cfg':cfg,**s,'score':s['wr']*s['pf']*np.log1p(s['n'])})

# Ranking
print("\n=== RANKING FINAL ===", flush=True)
if not res:
    print("Nenhuma config passou o filtro. Maximo encontrado sera mostrado abaixo.")
else:
    df_r=pd.DataFrame(res)
    for par in ['XAU','BTC']:
        sub=df_r[df_r['par']==par]
        if sub.empty: print(f"\n{par}: sem config"); continue
        bw=sub.sort_values('wr',ascending=False).iloc[0]
        bs=sub.sort_values('score',ascending=False).iloc[0]
        print(f"\n{par}:")
        print(f"  Maior WR    : {bw['wr']}% PF={bw['pf']} N={bw['n']} | {bw['cfg']}")
        print(f"  Melhor score: WR={bs['wr']}% PF={bs['pf']} N={bs['n']} | {bs['cfg']}")

mt5.shutdown()
print("\nFIM", flush=True)
