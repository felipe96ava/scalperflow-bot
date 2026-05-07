"""
ScalperFlow Bot — Interface Grafica
Dashboard com configuracao de lotes, status ao vivo e historico de trades.
"""
from version import __version__

import threading
import queue
import time
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import customtkinter as ctk
import MetaTrader5 as mt5
import pandas as pd
import numpy as np

# ── Tema ───────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

# ── Config persistente ─────────────────────────────────────────────
# Quando empacotado pelo PyInstaller (--onefile), __file__ aponta para
# uma pasta temporária que é deletada ao fechar. Usamos sys.executable
# para salvar ao lado do EXE.
import sys as _sys
_app_dir = Path(_sys.executable).parent if getattr(_sys, "frozen", False) else Path(__file__).parent
CONFIG_FILE = _app_dir / "gui_config.json"

DEFAULT_CONFIG = {
    "login"    : "",
    "password" : "",
    "server"   : "",
    "path"     : "C:/Program Files/MetaTrader 5 EXNESS/terminal64.exe",
    "lot_xau"  : 0.10,
    "lot_btc"  : 0.20,
    "lot_usoil": 1.00,
    # Nomes dos simbolos no MT5 (varia por broker — Exness usa sufixo 'z', FundedNext nao)
    "sym_xau"  : "XAUUSDz",
    "sym_btc"  : "BTCUSDz",
    "sym_usoil": "USOILz",
    "atr_min"  : 3.0,
    "sl_xau"   : 2.0,
    "sl_btc"   : 2.5,    # walk-forward: SL grande melhor para BTC
    "xau_estrategia": "scalperflow",   # "scalperflow" (EMA cross) ou "orb"
    # Modo Mesa Proprietaria (FundingPips Zero, FTMO, etc)
    "prop_firm_mode": False,           # ativa o modo prop firm
    "consistency_pct": 15,             # % maximo do dia vs ciclo de payout
    "cycle_days": 14,                  # bi-weekly default
    "cycle_start_date": "",            # data inicio ciclo (auto-fill)
    "force_close_friday": True,        # fecha XAU/USOIL na sexta
    "friday_close_hour": 20,           # hora UTC do force close
    "friday_close_minute": 55,         # minuto UTC
    # Regras adicionais FundingPips Zero
    "max_risk_floating_pct": 1.0,      # max % do capital em PnL flutuante (-1% = -$1k em $100k)
    "account_size": 100000,            # tamanho da conta (referencia para % limits)
    "trade_cooldown_min": 10,          # minutos cooldown apos loss mesma direcao
    "news_filter_enabled": True,       # bloqueia trades 10min antes/apos high-impact
    "news_buffer_min": 10,             # minutos buffer antes/depois
    "usoil_ativo": True,    # se False, ignora USOIL
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                c = json.load(f)
            return {**DEFAULT_CONFIG, **c}
        except:
            pass
    # tenta importar config.py legado
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from config import LOGIN, PASSWORD, SERVER, PATH
        cfg = {**DEFAULT_CONFIG, "login": str(LOGIN), "password": PASSWORD,
               "server": SERVER, "path": PATH}
        save_config(cfg)
        return cfg
    except:
        pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ── Parametros do bot ──────────────────────────────────────────────
EMA_FAST = 20; EMA_SLW = 50
TP1_ATR  = 2.0; TP2_ATR = 3.5; TP3_ATR = 5.0
TRAIL_DISTANCE_ATR = 1.0
VOL_MULTIPLIER = 1.5; VOL_PERIOD = 20; BODY_SHADOW_MAX = 0.3
MAGIC = 654321

def ema_fn(s, p): return s.ewm(span=p, adjust=False).mean()

def rsi_fn(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

# ── Engine do Bot ──────────────────────────────────────────────────
class BotEngine:
    def __init__(self, cfg, log_q, status_q):
        self.cfg      = cfg
        self.log_q    = log_q
        self.status_q = status_q
        self.running  = False
        self.tp_tracker   = {}
        self.ultima_barra = {}
        self.h1_cache     = {}
        self._ultima_h1   = 0
        # Estado por simbolo para estrategia ORB
        # {sym: {'day': '2026-05-05', 'or_high': X, 'or_low': Y,
        #        'or_done': bool, 'trades_today': N}}
        self.orb_state    = {}
        # Estado prop firm
        self.prop_state   = {
            "daily_locked": False,        # se True, nao abre novas posicoes hoje
            "last_day": None,             # ultimo dia processado (UTC)
            "cycle_start": None,          # datetime inicio ciclo
            "consistency_triggered": False,  # ja fechou tudo hoje por consistency
            "friday_close_done": False,   # ja fechou sexta
            "max_risk_triggered": False,  # ja fechou por max risk 1%
            # cooldown apos loss: {(symbol, direcao): timestamp_release}
            "trade_cooldown": {},
        }

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_q.put(f"[{ts}] {msg}")

    def symbols_cfg(self):
        c = self.cfg
        # Nomes dos simbolos configuraveis (Exness 'z' vs FundedNext sem sufixo)
        sym_xau   = c.get("sym_xau",   "XAUUSDz")
        sym_btc   = c.get("sym_btc",   "BTCUSDz")
        sym_usoil = c.get("sym_usoil", "USOILz")
        # XAU: alternar entre EMA crossover (legado) e ORB (otimizado walk-forward)
        xau_strat = c.get("xau_estrategia", "scalperflow")
        if xau_strat == "orb":
            # ORB DUAL — Asia + NY rodando em paralelo (walk-forward OOS comprovado)
            # Asia (01-09h): PF 2.38, +$35k OOS  |  NY (13-21h): PF 2.26, +$32k OOS
            xau_cfg = {
                "lot": float(c["lot_xau"]),
                "estrategia": "xau_orb",
                "tfs": {"M5": mt5.TIMEFRAME_M5},
                "use_h1": True,
                "sessions": [
                    {"name": "ASIA", "h0": 1, "h1": 9, "or_min": 15,
                     "max_per_day": 2, "sl_atr": 2.5,
                     "tp1_atr": 3.0, "tp2_atr": 5.0, "tp3_atr": 7.0},
                    {"name": "NY",   "h0": 13, "h1": 21, "or_min": 30,
                     "max_per_day": 2, "sl_atr": 1.0,
                     "tp1_atr": 2.5, "tp2_atr": 4.0, "tp3_atr": 6.0},
                ],
                # Backward-compat (executar_ordem usa fallback)
                "sl_atr": 2.5, "tp1_atr": 2.5, "tp2_atr": 4.0, "tp3_atr": 6.0,
            }
        else:
            xau_cfg = {
                "lot": float(c["lot_xau"]), "sl_atr": float(c["sl_xau"]),
                "estrategia": "scalperflow",
                "tfs": {"M5": mt5.TIMEFRAME_M5},
                "atr_min": float(c["atr_min"]),
                "tp1_atr": 2.0, "tp2_atr": 3.5, "tp3_atr": 5.0,
            }
        # Cada cfg ganha o campo 'symbol' = nome real no MT5 (acessivel pelas funcoes)
        xau_cfg["symbol"] = sym_xau
        btc_cfg = {
            # Config OTIMIZADA via walk-forward (TS_PF 1.35, +$2.707 OOS)
            "symbol": sym_btc,
            "lot": float(c["lot_btc"]), "sl_atr": float(c["sl_btc"]),
            "estrategia": "btc_filtered",
            "tfs": {"M15": mt5.TIMEFRAME_M15},
            "sessao_h0": 17, "sessao_h1": 21,
            "rsi_buy": 50, "rsi_sell": 50,
            "tp1_atr": 2.5, "tp2_atr": 4.0, "tp3_atr": 6.0,
            "dias_ok": [1, 2, 3],
            "use_h1":  False,
            "use_rsi": False,
        }
        cfg = {sym_xau: xau_cfg, sym_btc: btc_cfg}
        if c.get("usoil_ativo", True):
            cfg[sym_usoil] = {
                "symbol": sym_usoil,
                "lot": float(c.get("lot_usoil", 1.00)), "sl_atr": 1.2,
                "estrategia": "usoil_nypm",
                "tfs": {"M5": mt5.TIMEFRAME_M5},
                "tp1_atr": 3.0, "tp2_atr": 5.0, "tp3_atr": 7.0,
                "sessao_h0": 17, "sessao_h1": 21,
                "dias_ok": [2, 4],
            }
        return cfg

    def conectar(self):
        mt5.shutdown()
        ok = mt5.initialize(
            login=int(self.cfg["login"]),
            password=self.cfg["password"],
            server=self.cfg["server"],
            path=self.cfg["path"],
        )
        if ok:
            for sym in self.symbols_cfg():
                mt5.symbol_select(sym, True)
        return ok

    def calcular_tf(self, symbol, timeframe, bars=300):
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
        if rates is None or len(rates) < EMA_SLW + 20:
            return None
        df = pd.DataFrame(rates)
        df["time_dt"]  = pd.to_datetime(df["time"], unit="s")
        df["hour_utc"] = df["time_dt"].dt.hour
        df["ema_fast"] = ema_fn(df["close"], EMA_FAST)
        df["ema_slow"] = ema_fn(df["close"], EMA_SLW)
        h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
        df["tr"]  = np.maximum(h - l, np.maximum(abs(h - c.shift(1)), abs(l - c.shift(1))))
        df["atr"] = df["tr"].rolling(14).mean()
        df["tick_volume"] = df["tick_volume"].astype(float)
        df["rsi"] = rsi_fn(df["close"].astype(float))
        df["va"]  = df["tick_volume"].rolling(VOL_PERIOD).mean().shift(1)
        return df

    def carregar_h1(self, symbol):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 500)
        if rates is None: return None
        df = pd.DataFrame(rates)
        c  = df["close"].astype(float)
        df["ema20"] = ema_fn(c, 20); df["ema50"] = ema_fn(c, 50)
        df["trend_alta"] = df["ema20"] > df["ema50"]
        return df[["time", "trend_alta"]].set_index("time")

    def tendencia_h1(self, symbol, ts):
        h1 = self.h1_cache.get(symbol)
        if h1 is None: return None
        pos = h1.index.searchsorted(ts, side="right") - 1
        return bool(h1["trend_alta"].iloc[pos]) if pos >= 0 else None

    def detectar_cruzamento(self, df):
        if df is None or len(df) < 3: return None
        cur = df.iloc[-1]; prev = df.iloc[-2]
        if prev["ema_fast"] <= prev["ema_slow"] and cur["ema_fast"] > cur["ema_slow"]: return "BUY"
        if prev["ema_fast"] >= prev["ema_slow"] and cur["ema_fast"] < cur["ema_slow"]: return "SELL"
        return None

    def detectar_absorcao(self, df):
        if df is None or len(df) < VOL_PERIOD + 2: return None
        idx = len(df) - 2; row = df.iloc[idx]
        va = row["va"]
        if pd.isna(va) or row["tick_volume"] <= va * VOL_MULTIPLIER: return None
        body = abs(row["close"] - row["open"])
        us   = row["high"] - max(row["open"], row["close"])
        ls   = min(row["open"], row["close"]) - row["low"]
        ts   = us + ls
        if not (ts > 0 and body < ts * BODY_SHADOW_MAX): return None
        return "ABSORCAO_COMPRA" if row["close"] > row["open"] else "ABSORCAO_VENDA"

    def avaliar_sinal_xau(self, df, cfg):
        cur     = df.iloc[-1]
        atr_val = cur["atr"]
        atr_min = cfg.get("atr_min", 0)
        ema_d   = cur["ema_fast"] - cur["ema_slow"]
        trend   = self.tendencia_h1(cfg.get("symbol", "XAUUSDz"), df["time"].iloc[-1])
        h1_str  = "ALTA" if trend else ("BAIXA" if trend is not None else "?")

        # Filtro ATR minimo
        if atr_val < atr_min:
            atr_falta = atr_min - atr_val
            self.log(f"[XAU] Aguardando... ATR={atr_val:.2f} (min {atr_min} | falta {atr_falta:.2f}) | EMAd={ema_d:+.2f} | H1={h1_str}")
            return None, None

        sa = self.detectar_absorcao(df)
        sc = self.detectar_cruzamento(df)

        if sa == "ABSORCAO_COMPRA":  sinal, tipo = "SELL", "Absorcao Compra -> SELL"
        elif sa == "ABSORCAO_VENDA": sinal, tipo = "BUY",  "Absorcao Venda -> BUY"
        elif sc:                     sinal, tipo = sc,     f"Cruzamento EMA {sc}"
        else:
            # Sem sinal — mostra distancia da EMA para o proximo cruzamento
            dist_pct = abs(ema_d) / atr_val * 100
            quase = " *** QUASE CRUZANDO! ***" if abs(ema_d) < atr_val * 0.3 else ""
            self.log(f"[XAU] Monitorando | ATR={atr_val:.2f} OK | EMAd={ema_d:+.2f} ({dist_pct:.0f}% do ATR) | H1={h1_str}{quase}")
            return None, None

        # Sinal detectado — verificar H1
        if trend is not None:
            if sinal == "BUY" and not trend:
                self.log(f"[XAU] {tipo} BLOQUEADO | ATR={atr_val:.2f} OK | H1=BAIXA (precisa ALTA)")
                return None, None
            if sinal == "SELL" and trend:
                self.log(f"[XAU] {tipo} BLOQUEADO | ATR={atr_val:.2f} OK | H1=ALTA (precisa BAIXA)")
                return None, None

        self.log(f"[XAU] >>> CONFLUENCIA! {tipo} | ATR={atr_val:.2f} | H1={h1_str} | EXECUTANDO...")
        return sinal, f"[{cfg.get('symbol','XAUUSDz')}][M5] {tipo} | ATR={atr_val:.2f}"

    def avaliar_sinal_btc(self, df, cfg):
        cur      = df.iloc[-1]
        ts       = pd.to_datetime(cur["time"], unit="s", utc=True)
        hora     = cur["hour_utc"]
        dow      = ts.dayofweek
        dia_nome = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"][dow]
        rsi_val  = cur["rsi"]
        ema_d    = cur["ema_fast"] - cur["ema_slow"]
        trend    = self.tendencia_h1(cfg.get("symbol", "BTCUSDz"), df["time"].iloc[-1])
        h1_str   = "ALTA" if trend else ("BAIXA" if trend is not None else "?")
        ny_ok    = cfg["sessao_h0"] <= hora < cfg["sessao_h1"]
        ny_str   = f"OK ({hora}h UTC)" if ny_ok else f"FORA ({hora}h UTC, janela {cfg['sessao_h0']}-{cfg['sessao_h1']}h)"
        dias_ok  = cfg.get("dias_ok")
        use_h1   = cfg.get("use_h1", True)
        use_rsi  = cfg.get("use_rsi", True)

        # Filtro de dia da semana (config otimizada: Ter/Qua/Qui)
        if dias_ok is not None and dow not in dias_ok:
            self.log(f"[BTC] Aguardando — hoje={dia_nome} (so opera dias {dias_ok})")
            return None, None

        sc = self.detectar_cruzamento(df)

        if sc is None:
            quase = " *** QUASE CRUZANDO! ***" if abs(ema_d) < cur["atr"] * 0.3 else ""
            self.log(f"[BTC] Monitorando | {dia_nome} | EMAd={ema_d:+.0f} | RSI={rsi_val:.0f} | H1={h1_str} | NY={ny_str}{quase}")
            return None, None

        sinal = sc
        bloqueios = []

        # Filtro sessao NY
        if not ny_ok:
            bloqueios.append(f"NY=FORA ({hora}h UTC)")

        # Filtro RSI (so se ativado)
        if use_rsi:
            if sinal == "BUY"  and rsi_val < cfg["rsi_buy"]:
                bloqueios.append(f"RSI={rsi_val:.0f} < 50")
            if sinal == "SELL" and rsi_val > cfg["rsi_sell"]:
                bloqueios.append(f"RSI={rsi_val:.0f} > 50")

        # Filtro H1 (so se ativado)
        if use_h1 and trend is not None:
            if sinal == "BUY"  and not trend: bloqueios.append("H1=BAIXA")
            if sinal == "SELL" and trend:     bloqueios.append("H1=ALTA")

        if bloqueios:
            self.log(f"[BTC] Cruzamento {sinal} BLOQUEADO | {' | '.join(bloqueios)}")
            return None, None

        self.log(f"[BTC] >>> CONFLUENCIA! EMA {sinal} | RSI={rsi_val:.0f} | H1={h1_str} | NY={ny_str} | EXECUTANDO...")
        return sinal, f"[{cfg.get('symbol','BTCUSDz')}][M15] EMA {sinal} | RSI={rsi_val:.0f}"

    def avaliar_sinal_usoil(self, df, cfg):
        """USOIL: cruzamento EMA20x50 + sessao 17-21h UTC + Wed/Fri + H1 trend."""
        cur     = df.iloc[-1]
        ts      = pd.to_datetime(cur["time"], unit="s", utc=True)
        hora    = ts.hour
        dow     = ts.dayofweek                       # 0=seg, 4=sex
        atr_v   = cur["atr"]
        ema_d   = cur["ema_fast"] - cur["ema_slow"]
        trend   = self.tendencia_h1(cfg.get("symbol", "USOILz"), df["time"].iloc[-1])
        h1_str  = "ALTA" if trend else ("BAIXA" if trend is not None else "?")

        # Filtro de dia da semana
        dias_ok = cfg.get("dias_ok", [2, 4])
        dia_nome = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"][dow]
        if dow not in dias_ok:
            self.log(f"[USOIL] Aguardando — hoje={dia_nome} (so opera Qua/Sex)")
            return None, None

        # Filtro de sessao
        s_h0 = cfg.get("sessao_h0", 17); s_h1 = cfg.get("sessao_h1", 21)
        if not (s_h0 <= hora < s_h1):
            self.log(f"[USOIL] Aguardando — hora UTC={hora:02d}h (sessao {s_h0}-{s_h1}h)")
            return None, None

        # Detectar cruzamento (sem absorcao para USOIL)
        sc = self.detectar_cruzamento(df)
        if sc is None:
            quase = " *** QUASE CRUZANDO! ***" if abs(ema_d) < atr_v * 0.3 else ""
            self.log(f"[USOIL] Monitorando | {dia_nome} {hora:02d}h | EMAd={ema_d:+.3f} | ATR={atr_v:.3f} | H1={h1_str}{quase}")
            return None, None

        sinal = sc
        # Filtro H1
        if trend is not None:
            if sinal == "BUY" and not trend:
                self.log(f"[USOIL] {sinal} BLOQUEADO | H1=BAIXA (precisa ALTA)")
                return None, None
            if sinal == "SELL" and trend:
                self.log(f"[USOIL] {sinal} BLOQUEADO | H1=ALTA (precisa BAIXA)")
                return None, None

        self.log(f"[USOIL] >>> CONFLUENCIA! EMA {sinal} | {dia_nome} {hora:02d}h | ATR={atr_v:.3f} | H1={h1_str} | EXECUTANDO...")
        return sinal, f"[{cfg.get('symbol','USOILz')}][M5] EMA {sinal} | {dia_nome} {hora:02d}h UTC"

    def avaliar_sinal_xau_orb(self, df, cfg):
        """XAU ORB DUAL: itera sobre as sessoes configuradas (Asia + NY)."""
        sym = cfg.get("symbol", "XAUUSDz")
        sessions = cfg.get("sessions")
        if not sessions:
            sessions = [{
                "name": "single",
                "h0": cfg.get("sessao_h0", 13), "h1": cfg.get("sessao_h1", 21),
                "or_min": cfg.get("or_minutes", 30),
                "max_per_day": cfg.get("max_per_day", 2),
                "sl_atr":  cfg.get("sl_atr",  2.5),
                "tp1_atr": cfg.get("tp1_atr", 2.5),
                "tp2_atr": cfg.get("tp2_atr", 4.0),
                "tp3_atr": cfg.get("tp3_atr", 6.0),
            }]
        use_h1 = cfg.get("use_h1", True)
        for sess in sessions:
            sinal, tipo = self._eval_orb_session(df, sess, use_h1, sym)
            if sinal is not None:
                self.orb_state.setdefault(sym, {})["_last_session"] = sess
                return sinal, tipo
        return None, None

    def _eval_orb_session(self, df, sess, use_h1, sym="XAUUSDz"):
        """Avalia ORB para UMA sessao especifica. Estado por sessao via key <sym>_<name>."""
        cur     = df.iloc[-1]
        ts      = pd.to_datetime(cur["time"], unit="s", utc=True)
        hora    = ts.hour; minuto = ts.minute
        hoje    = ts.strftime("%Y-%m-%d")
        atr_v   = cur["atr"]

        sess_h0 = sess["h0"]; sess_h1 = sess["h1"]
        or_min  = sess["or_min"]; max_dia = sess["max_per_day"]
        sname   = sess["name"]

        # Estado por sessao (key independente)
        state_key = f"{sym}_{sname}"
        st = self.orb_state.get(state_key)
        if st is None or st.get("day") != hoje:
            self.orb_state[state_key] = {
                "day": hoje, "or_high": None, "or_low": None,
                "or_done": False, "trades_today": 0,
                "broke_up": False, "broke_down": False,
            }
            st = self.orb_state[state_key]

        # Fora da sessao — silencioso (sem spam de log com 2 sessoes)
        if hora < sess_h0 or hora >= sess_h1:
            return None, None

        # Construir / recuperar Opening Range
        if not st["or_done"]:
            if hora == sess_h0 and minuto < or_min:
                self.log(f"[XAU-ORB-{sname}] Construindo OR... ({minuto:02d}/{or_min} min, h={cur['high']:.2f} l={cur['low']:.2f})")
                return None, None
            # Buscar OR no historico de hoje
            ts_all = pd.to_datetime(df["time"], unit="s", utc=True)
            mask = ((ts_all.dt.strftime("%Y-%m-%d") == hoje) &
                    (ts_all.dt.hour == sess_h0) &
                    (ts_all.dt.minute < or_min))
            or_window = df[mask.values]
            if len(or_window) > 0:
                st["or_high"] = float(or_window["high"].max())
                st["or_low"]  = float(or_window["low"].min())
                st["or_done"] = True
                self.log(f"[XAU-ORB-{sname}] OR fechado (retro {len(or_window)} barras): "
                         f"HIGH={st['or_high']:.2f}  LOW={st['or_low']:.2f}  | aguardando breakout...")
            else:
                self.log(f"[XAU-ORB-{sname}] OR sem dados (nenhuma barra em {sess_h0:02d}:00-{sess_h0:02d}:{or_min:02d}) — aguardando proxima sessao")
                return None, None

        if st["trades_today"] >= max_dia:
            return None, None

        if np.isnan(atr_v):
            return None, None

        trend  = self.tendencia_h1(sym, df["time"].iloc[-1])
        h1_str = "ALTA" if trend else ("BAIXA" if trend is not None else "?")

        sinal = None; entry = None
        # Trava por lado rompido (evita re-fire enquanto preco fica acima/abaixo)
        if cur["high"] > st["or_high"] and not st["broke_up"]:
            sinal = "BUY"; entry = st["or_high"]
            st["broke_up"] = True
        elif cur["low"] < st["or_low"] and not st["broke_down"]:
            sinal = "SELL"; entry = st["or_low"]
            st["broke_down"] = True
        else:
            dist_h = st["or_high"] - cur["close"]
            dist_l = cur["close"] - st["or_low"]
            ja = []
            if st["broke_up"]:   ja.append("HIGH")
            if st["broke_down"]: ja.append("LOW")
            extra = f" | ja rompido: {','.join(ja)}" if ja else ""
            self.log(f"[XAU-ORB-{sname}] Monitor | preco={cur['close']:.2f} | OR=[{st['or_low']:.2f}-{st['or_high']:.2f}] | "
                     f"falta {dist_h:.2f}p/alta, {dist_l:.2f}p/baixa | H1={h1_str}{extra} | trades={st['trades_today']}/{max_dia}")
            return None, None

        # Filtro H1
        if use_h1 and trend is not None:
            if sinal == "BUY" and not trend:
                self.log(f"[XAU-ORB-{sname}] Breakout {sinal} BLOQUEADO | H1=BAIXA (lado HIGH marcado como rompido)")
                return None, None
            if sinal == "SELL" and trend:
                self.log(f"[XAU-ORB-{sname}] Breakout {sinal} BLOQUEADO | H1=ALTA (lado LOW marcado como rompido)")
                return None, None

        self.log(f"[XAU-ORB-{sname}] >>> BREAKOUT! {sinal} entry={entry:.2f} | H1={h1_str} | EXECUTANDO...")
        return sinal, f"[{sym}][ORB-{sname}] {sinal} OR=[{st['or_low']:.2f}-{st['or_high']:.2f}]"

    def executar_ordem(self, symbol, lot, sl_atr, direcao, atr, tick, tf_nome, sym_cfg=None):
        # TPs especificos do simbolo (fallback p/ globais)
        sym_cfg = sym_cfg or {}
        tp1m = sym_cfg.get("tp1_atr", TP1_ATR)
        tp2m = sym_cfg.get("tp2_atr", TP2_ATR)
        tp3m = sym_cfg.get("tp3_atr", TP3_ATR)
        # USOIL tem 3 casas decimais; XAU/BTC 2
        digits = 3 if symbol.startswith(("USOIL", "UKOIL")) else 2
        if direcao == "BUY":
            entry = tick.ask
            sl  = round(entry - atr * sl_atr, digits)
            tp1 = round(entry + atr * tp1m,   digits)
            tp2 = round(entry + atr * tp2m,   digits)
            tp3 = round(entry + atr * tp3m,   digits)
            tipo = mt5.ORDER_TYPE_BUY
        else:
            entry = tick.bid
            sl  = round(entry + atr * sl_atr, digits)
            tp1 = round(entry - atr * tp1m,   digits)
            tp2 = round(entry - atr * tp2m,   digits)
            tp3 = round(entry - atr * tp3m,   digits)
            tipo = mt5.ORDER_TYPE_SELL
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": lot, "type": tipo, "price": entry,
            "sl": sl, "tp": tp3, "deviation": 20, "magic": MAGIC,
            "comment": f"sf_{tf_nome.lower()}_{direcao.lower()}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        })
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.tp_tracker[result.order] = {
                "entry": entry, "sl_inicial": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "atr": atr, "tipo": direcao, "nivel": 0, "symbol": symbol,
            }
        return result, entry, sl, tp1, tp2, tp3

    def modificar_sl(self, pos, novo_sl):
        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol,
            "position": pos.ticket, "sl": novo_sl, "tp": pos.tp,
        })
        return r.retcode == mt5.TRADE_RETCODE_DONE

    def fechar_posicao(self, pos, tick):
        tipo  = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        preco = tick.bid if pos.type == 0 else tick.ask
        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
            "volume": pos.volume, "type": tipo, "position": pos.ticket,
            "price": preco, "deviation": 20, "magic": MAGIC,
            "comment": "tp3_close", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        })
        return r.retcode == mt5.TRADE_RETCODE_DONE

    def gerenciar_tp(self, pos, tick, atr, sym_cfg=None):
        ticket = pos.ticket
        sym_cfg = sym_cfg or {}
        tp1m = sym_cfg.get("tp1_atr", TP1_ATR)
        tp2m = sym_cfg.get("tp2_atr", TP2_ATR)
        tp3m = sym_cfg.get("tp3_atr", TP3_ATR)
        digits = 3 if pos.symbol.startswith(("USOIL", "UKOIL")) else 2
        if ticket not in self.tp_tracker:
            e = pos.price_open; is_b = pos.type == 0
            self.tp_tracker[ticket] = {
                "entry": e, "sl_inicial": pos.sl,
                "tp1": round(e + atr*tp1m, digits) if is_b else round(e - atr*tp1m, digits),
                "tp2": round(e + atr*tp2m, digits) if is_b else round(e - atr*tp2m, digits),
                "tp3": round(e + atr*tp3m, digits) if is_b else round(e - atr*tp3m, digits),
                "atr": atr, "nivel": 0, "symbol": pos.symbol,
            }
        t  = self.tp_tracker[ticket]
        nv = t["nivel"]; entry = t["entry"]
        tp1,tp2,tp3 = t["tp1"],t["tp2"],t["tp3"]
        is_buy = pos.type == 0
        preco  = tick.bid if is_buy else tick.ask
        sl_atual = pos.sl
        if nv < 3 and ((is_buy and preco >= tp3) or (not is_buy and preco <= tp3)):
            if self.fechar_posicao(pos, tick):
                self.tp_tracker[ticket]["nivel"] = 3
                self.log(f"TP3! [{pos.symbol}] lucro={pos.profit:.2f}")
            return
        if nv < 2 and ((is_buy and preco >= tp2) or (not is_buy and preco <= tp2)):
            if self.modificar_sl(pos, tp1):
                self.tp_tracker[ticket]["nivel"] = 2
                self.log(f"TP2! [{pos.symbol}] SL->TP1={tp1:.3f}")
            return
        if nv < 1 and ((is_buy and preco >= tp1) or (not is_buy and preco <= tp1)):
            if self.modificar_sl(pos, entry):
                self.tp_tracker[ticket]["nivel"] = 1
                self.log(f"TP1! [{pos.symbol}] SL->breakeven={entry:.3f}")
            return
        if nv >= 2:
            dist = atr * TRAIL_DISTANCE_ATR
            if is_buy:
                nsl = round(preco - dist, digits)
                if nsl > sl_atual: self.modificar_sl(pos, nsl)
            else:
                nsl = round(preco + dist, digits)
                if nsl < sl_atual: self.modificar_sl(pos, nsl)

    # ── Mesa Proprietaria — helpers ────────────────────────────────
    def _prop_calcular_pnl_dia(self):
        """Retorna (realized_today, unrealized_open) — em USD."""
        try:
            hoje = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            deals = mt5.history_deals_get(hoje, datetime.utcnow()) or []
            realized = sum(
                (d.profit + d.commission + d.swap) for d in deals
                if d.comment and d.comment.lower().startswith("sf") and d.entry != 0
            )
            pos = mt5.positions_get() or []
            unrealized = sum(
                (p.profit + getattr(p, "commission", 0) + getattr(p, "swap", 0))
                for p in pos if p.magic == MAGIC
            )
            return float(realized), float(unrealized)
        except Exception as e:
            self.log(f"[PROP] erro calculando pnl dia: {e}")
            return 0.0, 0.0

    def _prop_calcular_ciclo(self):
        """P&L acumulado desde o inicio do ciclo de payout."""
        try:
            ini = self.prop_state.get("cycle_start")
            if ini is None: return 0.0
            deals = mt5.history_deals_get(ini, datetime.utcnow()) or []
            return float(sum(
                (d.profit + d.commission + d.swap) for d in deals
                if d.comment and d.comment.lower().startswith("sf") and d.entry != 0
            ))
        except Exception as e:
            self.log(f"[PROP] erro calculando ciclo: {e}")
            return 0.0

    def _prop_fechar_todas_posicoes(self, motivo):
        """Fecha todas posicoes do bot."""
        n = 0
        for sym in self.symbols_cfg():
            pos_list = [p for p in (mt5.positions_get(symbol=sym) or []) if p.magic == MAGIC]
            for p in pos_list:
                tick = mt5.symbol_info_tick(sym)
                if tick is None: continue
                if self.fechar_posicao(p, tick):
                    n += 1
                    self.log(f"[PROP] FECHADO {sym} ticket={p.ticket} lucro=${p.profit:+.2f} | motivo: {motivo}")
        return n

    def _prop_tick_diario(self):
        """Reset diario do estado prop firm."""
        hoje = datetime.utcnow().date()
        if self.prop_state.get("last_day") != hoje:
            if self.prop_state.get("last_day") is not None:
                self.log(f"[PROP] Novo dia UTC ({hoje}) — reset diario")
            self.prop_state["last_day"] = hoje
            self.prop_state["daily_locked"] = False
            self.prop_state["consistency_triggered"] = False
            self.prop_state["friday_close_done"] = False
            self.prop_state["max_risk_triggered"] = False

    # ── Lista de eventos high-impact recorrentes ──
    # NFP: primeira sexta 12:30 UTC
    # CPI US: tipicamente entre dia 10-15, 12:30 UTC
    # FOMC: 8 reunioes/ano, 18:00 UTC + Powell speech 18:30
    # EIA Crude Inventories: toda quarta 14:30 UTC
    # PPI/Retail Sales: variavel, 12:30 UTC
    NEWS_RECORRENTES = [
        # (dia_semana, hora_utc, minuto, duracao_min, currencies, nome)
        # 4 = Friday
        (2, 14, 30, 5,  ["USD", "USOIL"], "EIA Crude"),
    ]

    def _prop_is_news_window(self, dt_utc):
        """Retorna (esta_em_janela_news, nome_evento, currencies). Aplica buffer."""
        if not self.cfg.get("news_filter_enabled", True):
            return False, None, []
        buffer_min = int(self.cfg.get("news_buffer_min", 10))
        # NFP: primeira sexta do mes 12:30 UTC
        if dt_utc.weekday() == 4 and dt_utc.day <= 7:
            event = dt_utc.replace(hour=12, minute=30, second=0, microsecond=0)
            if abs((dt_utc - event).total_seconds()) <= buffer_min * 60:
                return True, "NFP", ["USD"]
        # CPI: tipicamente dias 10-15, 12:30 UTC (heuristica)
        if 10 <= dt_utc.day <= 15 and dt_utc.weekday() < 5:
            event = dt_utc.replace(hour=12, minute=30, second=0, microsecond=0)
            if abs((dt_utc - event).total_seconds()) <= buffer_min * 60:
                # Aplicar so se for terca/quarta (CPI tipico)
                if dt_utc.weekday() in (1, 2):
                    return True, "CPI (estimado)", ["USD"]
        # EIA Crude: toda quarta 14:30 UTC (afeta USOIL)
        if dt_utc.weekday() == 2:
            event = dt_utc.replace(hour=14, minute=30, second=0, microsecond=0)
            if abs((dt_utc - event).total_seconds()) <= buffer_min * 60:
                return True, "EIA Crude", ["USOIL"]
        # FOMC nao recorrente — usuario adiciona manualmente via cfg
        for ev_str in self.cfg.get("news_extras", []):
            try:
                ev = datetime.strptime(ev_str, "%Y-%m-%d %H:%M")
                if abs((dt_utc - ev).total_seconds()) <= buffer_min * 60:
                    return True, f"Custom {ev_str}", ["USD"]
            except: pass
        return False, None, []

    def _prop_check_max_risk(self):
        """Fecha tudo se floating PnL atingir -1% do capital ($1.000 em $100k)."""
        if not self.cfg.get("prop_firm_mode", False):
            return False
        if self.prop_state.get("max_risk_triggered"):
            return True
        max_risk_pct = float(self.cfg.get("max_risk_floating_pct", 1.0)) / 100.0
        account = float(self.cfg.get("account_size", 100000))
        limite = -account * max_risk_pct * 0.85   # safety 85%
        try:
            pos = mt5.positions_get() or []
            floating = sum(
                (p.profit + getattr(p, "commission", 0) + getattr(p, "swap", 0))
                for p in pos if p.magic == MAGIC
            )
            if floating <= limite and floating < 0:
                self.log(f"[PROP] ⚠ MAX RISK {max_risk_pct*100:.1f}% ATINGIDO! "
                         f"floating=${floating:+.2f} <= limite=${limite:+.2f} — fechando tudo")
                self._prop_fechar_todas_posicoes(f"max risk {max_risk_pct*100:.1f}% — proximo do limite")
                self.prop_state["max_risk_triggered"] = True
                self.prop_state["daily_locked"] = True
                return True
        except Exception as e:
            self.log(f"[PROP] erro check max_risk: {e}")
        return False

    def _prop_check_cooldown(self, symbol, direcao):
        """Retorna True se trade pode abrir (cooldown ok), False se em cooldown."""
        if not self.cfg.get("prop_firm_mode", False):
            return True
        cooldown = self.prop_state.get("trade_cooldown", {})
        key = (symbol, direcao)
        rel = cooldown.get(key)
        if rel and datetime.utcnow() < rel:
            mins_left = (rel - datetime.utcnow()).total_seconds() / 60
            self.log(f"[PROP] Trade {symbol} {direcao} BLOQUEADO por cooldown 10min (faltam {mins_left:.1f}min apos ultimo loss)")
            return False
        return True

    def _prop_registrar_loss(self, symbol, direcao):
        """Registra timestamp pra cooldown 10min (chamado quando trade fecha em loss)."""
        if not self.cfg.get("prop_firm_mode", False):
            return
        cd_min = int(self.cfg.get("trade_cooldown_min", 10))
        key = (symbol, direcao)
        self.prop_state.setdefault("trade_cooldown", {})[key] = datetime.utcnow() + timedelta(minutes=cd_min)

    def _prop_check_consistency(self):
        """Fecha tudo se hoje (realized+unrealized) ja viola consistency.

        Regra: dia_today / cycle_total <= consistency_pct
        Se fechar agora: cycle_total_apos = ciclo + unrealized, dia = realized + unrealized
        Logo, fecha cedo quando: dia >= (cycle_outros_dias) * c/(1-c) * margem_seguranca
        """
        if not self.cfg.get("prop_firm_mode", False):
            return False
        if self.prop_state.get("consistency_triggered"):
            return True
        consist = float(self.cfg.get("consistency_pct", 15)) / 100.0
        if consist >= 1.0 or consist <= 0:
            return False
        ciclo = self._prop_calcular_ciclo()
        realized, unrealized = self._prop_calcular_pnl_dia()
        outros_dias = ciclo - realized   # P&L dos outros dias do ciclo (excluindo hoje)
        if outros_dias <= 0:
            # Sem base de outros dias para regra (primeiro dia do ciclo)
            return False
        # Se fechar agora: cycle_total = outros_dias + (realized + unrealized) = outros_dias + dia_total
        # Para nao violar: dia_total / (outros_dias + dia_total) <= consist
        # Solucao: dia_total <= outros_dias * c / (1 - c)
        max_dia = outros_dias * consist / (1.0 - consist)
        safety = 0.90   # fechar a 90% do limite (margem)
        trigger = max_dia * safety
        dia_total = realized + unrealized
        if dia_total >= trigger and dia_total > 0:
            pct_atual = dia_total / (outros_dias + dia_total) * 100
            self.log(f"[PROP] ⚠ CONSISTENCY {consist*100:.0f}% PROXIMO! "
                     f"dia=${dia_total:+.2f} | outros dias=${outros_dias:+.2f} | "
                     f"ratio atual={pct_atual:.1f}% (max={consist*100:.0f}%) | trigger=${trigger:+.2f}")
            self._prop_fechar_todas_posicoes(f"consistency {consist*100:.0f}% — fechando preventivo")
            self.prop_state["consistency_triggered"] = True
            self.prop_state["daily_locked"] = True
            return True
        return False

    def _prop_check_friday_close(self):
        """Sexta-feira: fechar todas posicoes XAU/USOIL antes do market close."""
        if not self.cfg.get("prop_firm_mode", False):
            return False
        if not self.cfg.get("force_close_friday", True):
            return False
        if self.prop_state.get("friday_close_done"):
            return True
        agora = datetime.utcnow()
        if agora.weekday() != 4:  # 4 = sexta
            return False
        h = int(self.cfg.get("friday_close_hour", 20))
        m = int(self.cfg.get("friday_close_minute", 55))
        if (agora.hour, agora.minute) >= (h, m):
            self.log(f"[PROP] 🏁 SEXTA-FEIRA close ({h:02d}:{m:02d} UTC) — fechando posicoes XAU+USOIL")
            sym_xau   = self.cfg.get("sym_xau",   "XAUUSDz")
            sym_usoil = self.cfg.get("sym_usoil", "USOILz")
            for sym in (sym_xau, sym_usoil):
                for p in (mt5.positions_get(symbol=sym) or []):
                    if p.magic == MAGIC:
                        tick = mt5.symbol_info_tick(sym)
                        if tick is None: continue
                        if self.fechar_posicao(p, tick):
                            self.log(f"[PROP] FECHADO {sym} ticket={p.ticket} lucro=${p.profit:+.2f} | sexta close")
            self.prop_state["friday_close_done"] = True
            self.prop_state["daily_locked"] = True
            return True
        return False

    def _prop_init_cycle(self):
        """Inicializa cycle_start se ainda nao foi (auto-fill primeira execucao)."""
        if self.prop_state.get("cycle_start") is not None:
            return
        cycle_str = self.cfg.get("cycle_start_date", "")
        if cycle_str:
            try:
                self.prop_state["cycle_start"] = datetime.strptime(cycle_str, "%Y-%m-%d")
                self.log(f"[PROP] Ciclo iniciado em: {cycle_str}")
                return
            except: pass
        # Auto-fill com hoje
        self.prop_state["cycle_start"] = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        self.cfg["cycle_start_date"] = self.prop_state["cycle_start"].strftime("%Y-%m-%d")
        save_config(self.cfg)
        self.log(f"[PROP] Ciclo auto-iniciado em: {self.cfg['cycle_start_date']}")

    def run(self):
        self.running = True
        self.status_q.put(("status", "conectando"))
        self.log("ScalperFlow Bot iniciando...")

        if not self.conectar():
            self.log("ERRO: Falha ao conectar no MT5")
            self.status_q.put(("status", "erro"))
            self.running = False
            return

        SYMBOLS = self.symbols_cfg()
        self.ultima_barra = {(s, tf): None for s, cfg in SYMBOLS.items() for tf in cfg["tfs"]}

        for sym in SYMBOLS:
            self.h1_cache[sym] = self.carregar_h1(sym)

        sym_xau   = self.cfg.get("sym_xau",   "XAUUSDz")
        sym_btc   = self.cfg.get("sym_btc",   "BTCUSDz")
        sym_usoil = self.cfg.get("sym_usoil", "USOILz")
        xau_strat = SYMBOLS[sym_xau]['estrategia']
        xau_label = "ORB DUAL Asia+NY" if xau_strat == "xau_orb" else "EMA cross"
        msg = (f"Conectado! {sym_xau} lot={SYMBOLS[sym_xau]['lot']} ({xau_label}) | "
               f"{sym_btc} lot={SYMBOLS[sym_btc]['lot']} (Ter-Qui 17-21h UTC)")
        if sym_usoil in SYMBOLS:
            msg += f" | {sym_usoil} lot={SYMBOLS[sym_usoil]['lot']} (Qua/Sex 17-21h UTC)"
        self.log(msg)
        if xau_strat == "xau_orb":
            for s in SYMBOLS[sym_xau].get('sessions', []):
                self.log(f"XAU-ORB-{s['name']}: {s['h0']:02d}-{s['h1']:02d}h UTC | OR={s['or_min']}min | "
                         f"SL={s['sl_atr']}xATR | TPs={s['tp1_atr']}/{s['tp2_atr']}/{s['tp3_atr']}xATR | max {s['max_per_day']} trades/dia")
        else:
            self.log(f"XAU EMA cross: ATR min={SYMBOLS[sym_xau]['atr_min']} | TPs={TP1_ATR}/{TP2_ATR}/{TP3_ATR}x")

        # Modo Mesa Proprietaria
        if self.cfg.get("prop_firm_mode", False):
            self._prop_init_cycle()
            consist = self.cfg.get("consistency_pct", 15)
            fc_h    = self.cfg.get("friday_close_hour", 20)
            fc_m    = self.cfg.get("friday_close_minute", 55)
            fc_on   = self.cfg.get("force_close_friday", True)
            self.log(f"🏛 MODO MESA PROPRIETARIA ATIVADO")
            self.log(f"   Consistency: maximo {consist}% do ciclo de payout por dia")
            self.log(f"   Ciclo: bi-weekly desde {self.cfg.get('cycle_start_date','')}")
            if fc_on:
                self.log(f"   Force-close sexta-feira: {fc_h:02d}:{fc_m:02d} UTC (XAU+USOIL)")
        self.status_q.put(("status", "rodando"))

        falhas = 0; ultima_h1 = 0

        while self.running:
            try:
                if not mt5.terminal_info():
                    self.log("MT5 desconectado — reconectando...")
                    if not self.conectar():
                        falhas += 1; time.sleep(15); continue
                    falhas = 0

                agora = time.time()
                if agora - ultima_h1 > 900:
                    for sym in SYMBOLS:
                        self.h1_cache[sym] = self.carregar_h1(sym)
                    ultima_h1 = agora

                # Atualizar dashboard
                self._push_dashboard(SYMBOLS)

                # Mesa Proprietaria: tick diario + checks (ordem importa)
                if self.cfg.get("prop_firm_mode", False):
                    self._prop_tick_diario()
                    self._prop_check_max_risk()       # 1% floating - mais critico
                    self._prop_check_friday_close()
                    self._prop_check_consistency()
                    # Check news: se entrar em janela, fecha XAU/USOIL/BTC dependendo da currency
                    em_janela, evento, currs = self._prop_is_news_window(datetime.utcnow())
                    if em_janela and not self.prop_state.get("news_blocked"):
                        self.log(f"[PROP] 🚨 NEWS WINDOW: {evento} (currencies={currs}) — fechando posicoes afetadas")
                        sym_xau   = self.cfg.get("sym_xau",   "XAUUSDz")
                        sym_btc   = self.cfg.get("sym_btc",   "BTCUSDz")
                        sym_usoil = self.cfg.get("sym_usoil", "USOILz")
                        # XAU/USOIL/BTC todos sao USD-denominated — afeta todos quando "USD" na lista
                        for s in (sym_xau, sym_btc, sym_usoil):
                            for p in (mt5.positions_get(symbol=s) or []):
                                if p.magic == MAGIC:
                                    tick = mt5.symbol_info_tick(s)
                                    if tick and self.fechar_posicao(p, tick):
                                        self.log(f"[PROP] FECHADO {s} ticket={p.ticket} | news {evento}")
                        self.prop_state["news_blocked"] = True
                    elif not em_janela:
                        self.prop_state["news_blocked"] = False

                for symbol, cfg in SYMBOLS.items():
                    lot = cfg["lot"]; sl_atr = cfg["sl_atr"]
                    tfs = cfg["tfs"]; estrategia = cfg["estrategia"]
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None: continue

                    pos_list = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == MAGIC]
                    tf_ref   = list(tfs.values())[0]
                    df_ref   = self.calcular_tf(symbol, tf_ref)
                    if df_ref is not None:
                        atr_ref = df_ref.iloc[-1]["atr"]
                        for pos in pos_list:
                            self.gerenciar_tp(pos, tick, atr_ref, sym_cfg=cfg)

                    for tf_nome, tf_val in tfs.items():
                        df = self.calcular_tf(symbol, tf_val)
                        if df is None: continue
                        barra_atual = df.iloc[-1]["time"] if "time" in df.columns else None
                        atr_val     = df.iloc[-1]["atr"]
                        chave = (symbol, tf_nome)
                        if barra_atual == self.ultima_barra[chave]: continue
                        self.ultima_barra[chave] = barra_atual

                        if estrategia == "scalperflow":
                            sinal, sinal_tipo = self.avaliar_sinal_xau(df, cfg)
                        elif estrategia == "xau_orb":
                            sinal, sinal_tipo = self.avaliar_sinal_xau_orb(df, cfg)
                        elif estrategia == "btc_filtered":
                            sinal, sinal_tipo = self.avaliar_sinal_btc(df, cfg)
                        elif estrategia == "usoil_nypm":
                            sinal, sinal_tipo = self.avaliar_sinal_usoil(df, cfg)
                        else:
                            continue
                        if sinal is None: continue

                        # Mesa proprietaria: bloquear novas entradas
                        if self.cfg.get("prop_firm_mode", False):
                            if self.prop_state.get("daily_locked"):
                                self.log(f"Sinal {sinal_tipo} — [PROP] daily_locked, novas entradas bloqueadas hoje")
                                continue
                            # Cooldown 10min apos loss mesma direcao
                            if not self._prop_check_cooldown(symbol, sinal):
                                continue
                            # News window
                            em_jan, ev, currs = self._prop_is_news_window(datetime.utcnow())
                            if em_jan:
                                self.log(f"Sinal {sinal_tipo} — [PROP] BLOQUEADO em news window ({ev})")
                                continue
                            # Risk per trade 2%
                            sl_atr_use_check = sl_atr if estrategia != "xau_orb" else self.orb_state.get(symbol, {}).get("_last_session", {}).get("sl_atr", sl_atr)
                            risco_estimado = lot * (atr_val * sl_atr_use_check) * (
                                100.0 if symbol.startswith(("XAU","UKO","USOIL")) else 1.0)
                            # tratamento generico — para precisao usa val_per_pt do simbolo
                            account = float(self.cfg.get("account_size", 100000))
                            max_risk_trade = account * 0.02 * 0.85   # 2% safety 85%
                            if risco_estimado > max_risk_trade:
                                self.log(f"Sinal {sinal_tipo} — [PROP] BLOQUEADO risco/trade=${risco_estimado:.0f} > limite=${max_risk_trade:.0f}")
                                continue
                        if pos_list:
                            self.log(f"Sinal {sinal_tipo} — posicao aberta, aguardando...")
                        else:
                            self.log(f"*** {sinal_tipo} ***")
                            # Para ORB com multiplas sessoes, usar SL/TPs da sessao que disparou
                            sl_atr_use = sl_atr
                            sym_cfg_use = cfg
                            if estrategia == "xau_orb":
                                last_sess = self.orb_state.get(symbol, {}).get("_last_session")
                                if last_sess:
                                    sl_atr_use = last_sess["sl_atr"]
                                    sym_cfg_use = {**cfg,
                                                    "sl_atr":  last_sess["sl_atr"],
                                                    "tp1_atr": last_sess["tp1_atr"],
                                                    "tp2_atr": last_sess["tp2_atr"],
                                                    "tp3_atr": last_sess["tp3_atr"]}
                            result, entry, sl, tp1, tp2, tp3 = self.executar_ordem(
                                symbol, lot, sl_atr_use, sinal, atr_val, tick, tf_nome, sym_cfg=sym_cfg_use)
                            if result.retcode == mt5.TRADE_RETCODE_DONE:
                                self.log(f"ORDEM EXECUTADA! {symbol} {sinal} Entry={entry:.3f} SL={sl:.3f} TP3={tp3:.3f}")
                                # Contador por sessao ORB (so apos execucao bem-sucedida)
                                if estrategia == "xau_orb":
                                    last_sess = self.orb_state.get(symbol, {}).get("_last_session")
                                    if last_sess:
                                        sname = last_sess["name"]
                                        sk = f"{symbol}_{sname}"
                                        st = self.orb_state.get(sk)
                                        if st is not None:
                                            st["trades_today"] = st.get("trades_today", 0) + 1
                                            self.log(f"[XAU-ORB-{sname}] trade {st['trades_today']}/{last_sess['max_per_day']} hoje")
                            else:
                                self.log(f"ERRO {symbol}: {result.retcode} - {result.comment}")
                            break

            except Exception as e:
                self.log(f"Erro: {e}")
            time.sleep(5)

        mt5.shutdown()
        self.status_q.put(("status", "parado"))
        self.log("Bot encerrado.")

    def _push_dashboard(self, SYMBOLS):
        try:
            dados = []
            pnl_total = 0.0
            for sym in SYMBOLS:
                pos_list = mt5.positions_get(symbol=sym) or []
                for p in pos_list:
                    if p.magic != MAGIC: continue
                    dados.append({
                        "symbol": p.symbol,
                        "tipo"  : "BUY" if p.type == 0 else "SELL",
                        "lote"  : p.volume,
                        "entry" : p.price_open,
                        "preco" : p.price_current,
                        "lucro" : p.profit,
                        "ticket": p.ticket,
                    })
                    pnl_total += p.profit
            # Historico de hoje — agrupa por position_id e soma profit+commission+swap
            hoje  = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            desde = hoje - pd.Timedelta(days=2)
            todos = mt5.history_deals_get(desde, datetime.now()) or []
            sf_pos_ids = {d.position_id for d in todos
                          if d.comment and d.comment.lower().startswith("sf")}
            hist = [d for d in todos if d.position_id in sf_pos_ids]
            from collections import defaultdict
            grp = defaultdict(list)
            for h in hist:
                grp[h.position_id].append(h)
            n_fechadas = 0
            hist_pnl   = 0.0
            for pos_id, ds in grp.items():
                outs = [d for d in ds if d.entry != 0]
                if not outs: continue
                if datetime.fromtimestamp(max(d.time for d in outs)) < hoje:
                    continue
                n_fechadas += 1
                hist_pnl   += sum(d.profit + d.commission + d.swap for d in ds)
            self.status_q.put(("dashboard", {
                "posicoes"  : dados,
                "pnl_open"  : pnl_total,
                "hist_trades": n_fechadas,
                "hist_pnl"  : hist_pnl,
            }))
        except:
            pass

    def stop(self):
        self.running = False


# ── Interface Grafica ──────────────────────────────────────────────
class ScalperFlowApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ScalperFlow Bot")
        self.geometry("950x680")
        self.resizable(True, True)

        self.cfg     = load_config()
        self.engine  = None
        self.thread  = None
        self.log_q   = queue.Queue()
        self.stat_q  = queue.Queue()

        self._build_ui()
        self._poll()

    # ── Layout ────────────────────────────────────────────────────
    def _build_ui(self):
        # Header
        hdr = ctk.CTkFrame(self, height=60, fg_color="#1a1a2e")
        hdr.pack(fill="x", padx=0, pady=0)
        ctk.CTkLabel(hdr, text="⚡ ScalperFlow Bot",
                     font=ctk.CTkFont(size=22, weight="bold"),
                     text_color="#00ff88").pack(side="left", padx=20, pady=10)
        self.lbl_status = ctk.CTkLabel(hdr, text="● PARADO",
                                        font=ctk.CTkFont(size=13, weight="bold"),
                                        text_color="#ff4444")
        self.lbl_status.pack(side="left", padx=10)
        self.lbl_versao = ctk.CTkLabel(hdr, text=f"v{__version__}",
                                        font=ctk.CTkFont(size=12),
                                        text_color="#888aa0")
        self.lbl_versao.pack(side="left", padx=10)
        self.btn_toggle = ctk.CTkButton(hdr, text="▶  INICIAR", width=130,
                                         fg_color="#00aa55", hover_color="#00cc66",
                                         command=self._toggle_bot)
        self.btn_toggle.pack(side="right", padx=20, pady=10)

        # Tabs
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=(5,10))
        self.tabs.add("📊 Dashboard")
        self.tabs.add("⚙️  Configuracoes")
        self.tabs.add("📋 Log")

        self._build_dashboard()
        self._build_config()
        self._build_log()

    def _build_dashboard(self):
        tab = self.tabs.tab("📊 Dashboard")

        # Estado do filtro de periodo
        self._filtro_periodo = "Diário"

        # Cards de resumo
        cards = ctk.CTkFrame(tab, fg_color="transparent")
        cards.pack(fill="x", padx=10, pady=10)
        self.card_pnl_open  = self._card(cards, "P&L Aberto",  "$0.00", "#00ff88")
        self.card_pnl_hoje  = self._card(cards, "P&L Diário",  "$0.00", "#00aaff")
        self.card_trades    = self._card(cards, "Trades Diário", "0",   "#ffaa00")
        self.card_posicoes  = self._card(cards, "Pos. Abertas", "0",     "#aa88ff")
        for c in [self.card_pnl_open, self.card_pnl_hoje, self.card_trades, self.card_posicoes]:
            c.pack(side="left", expand=True, fill="both", padx=5)

        # Tabela de posicoes abertas (compacta)
        ctk.CTkLabel(tab, text="Posições Abertas",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=15, pady=(10,2))
        self.tbl_frame = ctk.CTkScrollableFrame(tab, height=90)
        self.tbl_frame.pack(fill="x", padx=10, pady=(0,5))
        self._tbl_header()

        # Cabecalho do historico com filtro de periodo
        hist_header = ctk.CTkFrame(tab, fg_color="transparent")
        hist_header.pack(fill="x", padx=15, pady=(10,2))
        self.lbl_hist = ctk.CTkLabel(hist_header, text="Histórico — Diário",
                                      font=ctk.CTkFont(size=14, weight="bold"))
        self.lbl_hist.pack(side="left")
        self.seg_periodo = ctk.CTkSegmentedButton(
            hist_header,
            values=["Diário", "Semanal", "Mensal", "Anual", "Completo"],
            command=self._on_filtro_periodo,
            selected_color="#00aa55",
            selected_hover_color="#00cc66",
        )
        self.seg_periodo.set("Diário")
        self.seg_periodo.pack(side="right")

        self.hist_frame = ctk.CTkScrollableFrame(tab, height=320)
        self.hist_frame.pack(fill="both", expand=True, padx=10, pady=(0,5))
        self._hist_header()
        self._hist_snapshot = None  # cache p/ evitar redraw desnecessario

    def _on_filtro_periodo(self, valor):
        self._filtro_periodo = valor
        self.lbl_hist.configure(text=f"Histórico — {valor}")
        self.card_pnl_hoje._titulo_label.configure(text=f"P&L {valor}")
        self.card_trades._titulo_label.configure(text=f"Trades {valor}")
        self._atualizar_historico(forcar=True)

    def _card(self, parent, titulo, valor, cor):
        f = ctk.CTkFrame(parent, fg_color="#16213e", corner_radius=10)
        tlbl = ctk.CTkLabel(f, text=titulo, font=ctk.CTkFont(size=11),
                             text_color="#888888")
        tlbl.pack(pady=(10,0))
        lbl = ctk.CTkLabel(f, text=valor,
                            font=ctk.CTkFont(size=20, weight="bold"), text_color=cor)
        lbl.pack(pady=(0,10))
        f._val_label = lbl
        f._titulo_label = tlbl
        return f

    def _tbl_header(self):
        cols = ["Par","Direção","Lote","Entrada","Preço Atual","Lucro","Ticket"]
        row  = ctk.CTkFrame(self.tbl_frame, fg_color="#0f3460")
        row.pack(fill="x", pady=(0,2))
        for i, c in enumerate(cols):
            ctk.CTkLabel(row, text=c, font=ctk.CTkFont(size=11, weight="bold"),
                         width=110 if i > 0 else 80).grid(row=0, column=i, padx=4, pady=4)

    def _hist_header(self):
        cols = ["Hora","Par","Direção","Entrada","Saída","Lucro","Tipo Saída"]
        row  = ctk.CTkFrame(self.hist_frame, fg_color="#0f3460")
        row.pack(fill="x", pady=(0,2))
        for i, c in enumerate(cols):
            ctk.CTkLabel(row, text=c, font=ctk.CTkFont(size=11, weight="bold"),
                         width=110 if i > 0 else 80).grid(row=0, column=i, padx=4, pady=4)

    def _build_config(self):
        tab = self.tabs.tab("⚙️  Configuracoes")
        sf  = ctk.CTkScrollableFrame(tab)
        sf.pack(fill="both", expand=True, padx=10, pady=10)

        def section(t):
            ctk.CTkLabel(sf, text=t, font=ctk.CTkFont(size=14, weight="bold"),
                         text_color="#00ff88").pack(anchor="w", pady=(15,5))

        def field(label, key, row_frame):
            ctk.CTkLabel(row_frame, text=label, width=160, anchor="w").pack(side="left", padx=(0,10))
            var = ctk.StringVar(value=str(self.cfg.get(key, "")))
            entry = ctk.CTkEntry(row_frame, textvariable=var, width=200,
                                  show="*" if key == "password" else "")
            entry.pack(side="left")
            self._cfg_vars[key] = var

        self._cfg_vars = {}

        # Conexao MT5
        section("🔌 Conexão MT5")
        for lbl, key in [("Login","login"),("Senha","password"),
                          ("Servidor","server"),("Caminho MT5","path")]:
            fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
            field(lbl, key, fr)

        # Nomes dos simbolos no MT5 (varia por broker)
        section("🏷 Símbolos no MT5")
        ctk.CTkLabel(sf, text="  Exness usa sufixo 'z' (XAUUSDz). FundedNext/outros usam sem sufixo (XAUUSD).",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(anchor="w", pady=(0,3))
        for lbl, key in [("Símbolo XAU (Ouro)","sym_xau"),
                          ("Símbolo BTC (Bitcoin)","sym_btc"),
                          ("Símbolo USOIL (Petróleo)","sym_usoil")]:
            fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
            field(lbl, key, fr)

        # Lotes
        section("📦 Tamanho de Lote")
        for lbl, key in [("Lote XAU (Ouro)","lot_xau"),
                          ("Lote BTC (Bitcoin)","lot_btc"),
                          ("Lote USOIL (Petroleo)","lot_usoil")]:
            fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
            field(lbl, key, fr)

        # Toggle USOIL
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=(8,3))
        ctk.CTkLabel(fr, text="Operar USOIL", width=160, anchor="w").pack(side="left", padx=(0,10))
        self._usoil_var = ctk.BooleanVar(value=bool(self.cfg.get("usoil_ativo", True)))
        ctk.CTkSwitch(fr, text="(Qua+Sex 17-21h UTC)", variable=self._usoil_var,
                      onvalue=True, offvalue=False).pack(side="left")

        # Estrategia XAU
        section("🥇 Estratégia XAU")
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Estratégia", width=160, anchor="w").pack(side="left", padx=(0,10))
        self._xau_strat_var = ctk.StringVar(value=self.cfg.get("xau_estrategia", "scalperflow"))
        ctk.CTkSegmentedButton(
            fr, values=["scalperflow", "orb"],
            variable=self._xau_strat_var,
            selected_color="#00aa55", selected_hover_color="#00cc66",
        ).pack(side="left")
        ctk.CTkLabel(sf, text="  scalperflow = EMA20×50 (legado, PF≈1.0)   |   orb = Opening Range Breakout NY 13-21h (PF≈2.18, otimizado walk-forward)",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(anchor="w", pady=(0,5))

        # ── Mesa Proprietaria ─────────────────────────────────────
        section("🏛 Mesa Proprietária (FundingPips/FTMO/etc)")

        # Switch master
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Ativar Modo Mesa", width=160, anchor="w").pack(side="left", padx=(0,10))
        self._prop_var = ctk.BooleanVar(value=bool(self.cfg.get("prop_firm_mode", False)))
        ctk.CTkSwitch(fr, text="(aplica regras de prop firm)", variable=self._prop_var,
                      onvalue=True, offvalue=False).pack(side="left")

        # Consistency %
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Consistency (%)", width=160, anchor="w").pack(side="left", padx=(0,10))
        cv = ctk.StringVar(value=str(self.cfg.get("consistency_pct", 15)))
        ctk.CTkEntry(fr, textvariable=cv, width=80).pack(side="left")
        ctk.CTkLabel(fr, text="  máx % do ciclo num dia. Bot fecha posições se atingir.",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(side="left")
        self._cfg_vars["consistency_pct"] = cv

        # Cycle days
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Ciclo (dias)", width=160, anchor="w").pack(side="left", padx=(0,10))
        cyv = ctk.StringVar(value=str(self.cfg.get("cycle_days", 14)))
        ctk.CTkEntry(fr, textvariable=cyv, width=80).pack(side="left")
        ctk.CTkLabel(fr, text="  bi-weekly = 14 (FundingPips Zero)",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(side="left")
        self._cfg_vars["cycle_days"] = cyv

        # Cycle start date
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Início do ciclo", width=160, anchor="w").pack(side="left", padx=(0,10))
        cdv = ctk.StringVar(value=str(self.cfg.get("cycle_start_date", "")))
        ctk.CTkEntry(fr, textvariable=cdv, width=120).pack(side="left")
        ctk.CTkLabel(fr, text="  formato YYYY-MM-DD (vazio = auto-fill na primeira execução)",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(side="left")
        self._cfg_vars["cycle_start_date"] = cdv

        # Force close sexta
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Force close sexta", width=160, anchor="w").pack(side="left", padx=(0,10))
        self._fc_var = ctk.BooleanVar(value=bool(self.cfg.get("force_close_friday", True)))
        ctk.CTkSwitch(fr, text="(fecha XAU+USOIL antes do close de sexta)",
                      variable=self._fc_var, onvalue=True, offvalue=False).pack(side="left")

        # Friday close time
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Hora close sexta UTC", width=160, anchor="w").pack(side="left", padx=(0,10))
        fhv = ctk.StringVar(value=str(self.cfg.get("friday_close_hour", 20)))
        ctk.CTkEntry(fr, textvariable=fhv, width=50).pack(side="left", padx=(0,2))
        ctk.CTkLabel(fr, text=":", text_color="#888888").pack(side="left")
        fmv = ctk.StringVar(value=str(self.cfg.get("friday_close_minute", 55)))
        ctk.CTkEntry(fr, textvariable=fmv, width=50).pack(side="left", padx=(2,0))
        ctk.CTkLabel(fr, text="  default 20:55 UTC (~17:55 BR no inverno)",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(side="left", padx=(8,0))
        self._cfg_vars["friday_close_hour"]   = fhv
        self._cfg_vars["friday_close_minute"] = fmv

        # Tamanho da conta (referencia para % limits)
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Tamanho conta (USD)", width=160, anchor="w").pack(side="left", padx=(0,10))
        asv = ctk.StringVar(value=str(self.cfg.get("account_size", 100000)))
        ctk.CTkEntry(fr, textvariable=asv, width=100).pack(side="left")
        ctk.CTkLabel(fr, text="  $100000 padrão Zero $100k",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(side="left", padx=(8,0))
        self._cfg_vars["account_size"] = asv

        # Max risk floating
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Max risk flutuante %", width=160, anchor="w").pack(side="left", padx=(0,10))
        mrv = ctk.StringVar(value=str(self.cfg.get("max_risk_floating_pct", 1.0)))
        ctk.CTkEntry(fr, textvariable=mrv, width=80).pack(side="left")
        ctk.CTkLabel(fr, text="  fecha tudo se floating PnL <= -X% (Zero=1%)",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(side="left", padx=(8,0))
        self._cfg_vars["max_risk_floating_pct"] = mrv

        # News filter
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Filtro de notícias", width=160, anchor="w").pack(side="left", padx=(0,10))
        self._news_var = ctk.BooleanVar(value=bool(self.cfg.get("news_filter_enabled", True)))
        ctk.CTkSwitch(fr, text="(NFP, CPI, EIA — bloqueia 10min antes/depois)",
                      variable=self._news_var, onvalue=True, offvalue=False).pack(side="left")

        # Cooldown
        fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
        ctk.CTkLabel(fr, text="Cooldown loss (min)", width=160, anchor="w").pack(side="left", padx=(0,10))
        cdmv = ctk.StringVar(value=str(self.cfg.get("trade_cooldown_min", 10)))
        ctk.CTkEntry(fr, textvariable=cdmv, width=60).pack(side="left")
        ctk.CTkLabel(fr, text="  evita reabrir mesma direção em 10min após loss",
                     text_color="#888888", font=ctk.CTkFont(size=10)).pack(side="left", padx=(8,0))
        self._cfg_vars["trade_cooldown_min"] = cdmv

        # Parametros
        section("🎯 Parâmetros de Risco")
        for lbl, key in [("ATR Mínimo XAU","atr_min"),
                          ("SL Multiplier XAU","sl_xau"),
                          ("SL Multiplier BTC","sl_btc")]:
            fr = ctk.CTkFrame(sf, fg_color="transparent"); fr.pack(fill="x", pady=3)
            field(lbl, key, fr)

        ctk.CTkLabel(sf, text="TP1=2.0x | TP2=3.5x | TP3=5.0x  (fixos)",
                     text_color="#666666").pack(anchor="w", pady=5)

        ctk.CTkButton(sf, text="💾  Salvar Configurações", fg_color="#00aa55",
                       command=self._salvar_config).pack(pady=20)

    def _build_log(self):
        tab = self.tabs.tab("📋 Log")
        self.log_box = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Courier", size=12),
                                       fg_color="#0a0a0a", text_color="#00ff88")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkButton(tab, text="🗑  Limpar Log", width=120,
                       command=lambda: self.log_box.delete("1.0","end")).pack(pady=(0,10))

    # ── Acoes ─────────────────────────────────────────────────────
    def _toggle_bot(self):
        if self.engine and self.engine.running:
            self.engine.stop()
            self.btn_toggle.configure(text="▶  INICIAR", fg_color="#00aa55", hover_color="#00cc66")
        else:
            self._salvar_config_silencioso()
            self.engine = BotEngine(self.cfg, self.log_q, self.stat_q)
            self.thread = threading.Thread(target=self.engine.run, daemon=True)
            self.thread.start()
            self.btn_toggle.configure(text="⏹  PARAR", fg_color="#cc2200", hover_color="#ee3300")

    def _salvar_config_silencioso(self):
        """Salva configuracoes sem mostrar popup (usado ao iniciar bot)."""
        for key, var in self._cfg_vars.items():
            val = var.get().strip()
            try:
                if key in ("lot_xau","lot_btc","lot_usoil","atr_min","sl_xau","sl_btc"):
                    val = float(val)
                elif key in ("login", "consistency_pct", "cycle_days",
                              "friday_close_hour", "friday_close_minute",
                              "account_size", "trade_cooldown_min"):
                    val = int(val)
                elif key == "max_risk_floating_pct":
                    val = float(val)
            except:
                pass
            self.cfg[key] = val
        # Switch USOIL
        if hasattr(self, "_usoil_var"):
            self.cfg["usoil_ativo"] = bool(self._usoil_var.get())
        # Estrategia XAU (scalperflow / orb)
        if hasattr(self, "_xau_strat_var"):
            v = self._xau_strat_var.get()
            if v in ("scalperflow", "orb"):
                self.cfg["xau_estrategia"] = v
        # Switches Mesa Proprietaria
        if hasattr(self, "_prop_var"):
            self.cfg["prop_firm_mode"] = bool(self._prop_var.get())
        if hasattr(self, "_fc_var"):
            self.cfg["force_close_friday"] = bool(self._fc_var.get())
        if hasattr(self, "_news_var"):
            self.cfg["news_filter_enabled"] = bool(self._news_var.get())
        save_config(self.cfg)

    def _salvar_config(self):
        self._salvar_config_silencioso()
        self._append_log("[Sistema] Configuracoes salvas.")
        self._mostrar_sucesso_config()

    def _mostrar_sucesso_config(self):
        popup = ctk.CTkToplevel(self)
        popup.title("ScalperFlow")
        popup.geometry("320x140")
        popup.resizable(False, False)
        popup.grab_set()
        popup.focus()
        ctk.CTkLabel(popup, text="✅  Configurações salvas!",
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color="#00ff88").pack(expand=True, pady=(30, 10))
        def _ok():
            popup.destroy()
            self.tabs.set("📊 Dashboard")
        ctk.CTkButton(popup, text="OK", width=100, command=_ok).pack(pady=(0, 20))

    def _append_log(self, msg):
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")

    def _atualizar_dashboard(self, dados):
        # Cards "live" (P&L Aberto e Pos. Abertas) — atualizados pelo engine
        pnl_open = dados.get("pnl_open", 0)
        posicoes = dados.get("posicoes", [])

        cor_open = "#00ff88" if pnl_open >= 0 else "#ff4444"
        self.card_pnl_open._val_label.configure(
            text=f"${pnl_open:+.2f}", text_color=cor_open)
        self.card_posicoes._val_label.configure(text=str(len(posicoes)))
        # P&L do periodo e Trades sao atualizados em _atualizar_historico

        # Limpar e repopular tabela de posicoes
        for w in self.tbl_frame.winfo_children()[1:]:  # skip header
            w.destroy()
        if not posicoes:
            ctk.CTkLabel(self.tbl_frame, text="Nenhuma posição aberta",
                         text_color="#555555").pack(pady=10)
        for p in posicoes:
            cor = "#00ff88" if p["lucro"] >= 0 else "#ff4444"
            row = ctk.CTkFrame(self.tbl_frame, fg_color="#1a1a2e")
            row.pack(fill="x", pady=1)
            vals = [p["symbol"], p["tipo"], f'{p["lote"]:.2f}',
                    f'{p["entry"]:.3f}', f'{p["preco"]:.3f}',
                    f'${p["lucro"]:+.2f}', str(p["ticket"])]
            for i, v in enumerate(vals):
                tc = cor if i == 5 else "#cccccc"
                ctk.CTkLabel(row, text=v, text_color=tc,
                             font=ctk.CTkFont(size=11),
                             width=110 if i > 0 else 80).grid(row=0, column=i, padx=4, pady=3)

        # Historico — recarregar do MT5
        self._atualizar_historico()

    def _inicio_periodo(self, periodo, agora):
        """Retorna datetime de inicio do periodo, ou None para 'Completo'."""
        hoje0 = agora.replace(hour=0, minute=0, second=0, microsecond=0)
        if periodo == "Diário":
            return hoje0
        if periodo == "Semanal":
            return hoje0 - pd.Timedelta(days=hoje0.weekday())     # segunda-feira
        if periodo == "Mensal":
            return hoje0.replace(day=1)
        if periodo == "Anual":
            return hoje0.replace(month=1, day=1)
        return None  # Completo

    def _ensure_mt5(self):
        """Garante conexao MT5 para queries da UI, independente do bot engine."""
        if mt5.terminal_info() is not None:
            return True
        login = self.cfg.get("login", "")
        if not login:
            return False
        try:
            return mt5.initialize(
                login=int(login),
                password=str(self.cfg.get("password", "")),
                server=str(self.cfg.get("server", "")),
                path=str(self.cfg.get("path", "")),
            )
        except:
            return False

    def _atualizar_historico(self, forcar=False):
        if not self._ensure_mt5():
            if self._hist_snapshot != "no_mt5":
                for w in self.hist_frame.winfo_children()[1:]:
                    w.destroy()
                ctk.CTkLabel(self.hist_frame, text="MT5 não conectado — configure as credenciais",
                             text_color="#555555").pack(pady=10)
                self._hist_snapshot = "no_mt5"
            return
        try:
            agora    = datetime.now()
            periodo  = getattr(self, "_filtro_periodo", "Diário")
            ini_data = self._inicio_periodo(periodo, agora)
            # Para a query MT5, busca de uma janela um pouco mais ampla (timezone)
            desde = ini_data - pd.Timedelta(days=2) if ini_data else datetime(2000, 1, 1)
            todos = mt5.history_deals_get(desde, agora) or []

            # 1. Identifica position_ids que tem deal com comentario "sf*"
            #    (o comentario sf_m5_buy etc. fica so no deal de ENTRADA)
            sf_pos_ids = {d.position_id for d in todos
                          if d.comment and d.comment.lower().startswith("sf")}

            # 2. Pega TODOS os deals (IN+OUT) das posicoes do bot
            deals = [d for d in todos if d.position_id in sf_pos_ids]

            # Agrupa por position_id (uma posicao = 1 deal IN + 1+ deals OUT)
            from collections import defaultdict
            grupos = defaultdict(list)
            for d in deals:
                grupos[d.position_id].append(d)

            posicoes_fechadas = []
            for pos_id, ds in grupos.items():
                ins  = [d for d in ds if d.entry == 0]
                outs = [d for d in ds if d.entry != 0]
                if not outs:           # ainda aberta
                    continue
                close_time = max(d.time for d in outs)
                close_dt   = datetime.fromtimestamp(close_time)
                if ini_data and close_dt < ini_data:   # filtro de periodo
                    continue
                in_deal  = ins[0] if ins else None
                out_last = max(outs, key=lambda d: d.time)
                entry_price = in_deal.price if in_deal else out_last.price
                exit_price  = out_last.price
                if in_deal:
                    direcao = "BUY" if in_deal.type == 0 else "SELL"
                else:
                    direcao = "SELL" if out_last.type == 0 else "BUY"
                # Soma profit + commission + swap de TODOS os deals (IN e OUT)
                # — comissao geralmente fica no deal de entrada
                lucro_total = sum(d.profit + d.commission + d.swap for d in ds)
                posicoes_fechadas.append({
                    "hora": close_dt.strftime("%H:%M"),
                    "symbol": out_last.symbol,
                    "direcao": direcao,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "lucro": lucro_total,
                    "close_time": close_time,
                })

            # Atualiza cards "P&L" e "Trades" do periodo
            pnl_total = sum(p["lucro"] for p in posicoes_fechadas)
            cor_hoje = "#00aaff" if pnl_total >= 0 else "#ff4444"
            self.card_pnl_hoje._val_label.configure(
                text=f"${pnl_total:+.2f}", text_color=cor_hoje)
            self.card_trades._val_label.configure(text=str(len(posicoes_fechadas)))

            posicoes_fechadas.sort(key=lambda p: p["close_time"], reverse=True)

            # Snapshot p/ evitar redraw desnecessario (corta o piscar)
            snap = (periodo, tuple((p["close_time"], round(p["lucro"], 2),
                                    round(p["entry_price"], 3),
                                    round(p["exit_price"], 3))
                                   for p in posicoes_fechadas))
            if snap == self._hist_snapshot and not forcar:
                return
            self._hist_snapshot = snap

            for w in self.hist_frame.winfo_children()[1:]:
                w.destroy()

            if not posicoes_fechadas:
                ctk.CTkLabel(self.hist_frame,
                             text=f"Nenhum trade fechado neste período ({periodo})",
                             text_color="#555555").pack(pady=10)
                return

            for p in posicoes_fechadas:
                cor = "#00ff88" if p["lucro"] >= 0 else "#ff4444"
                row = ctk.CTkFrame(self.hist_frame, fg_color="#1a1a2e")
                row.pack(fill="x", pady=1)
                tipo_s = "TP" if p["lucro"] > 0 else "SL"
                vals   = [p["hora"], p["symbol"], p["direcao"],
                          f'{p["entry_price"]:.3f}', f'{p["exit_price"]:.3f}',
                          f'${p["lucro"]:+.2f}', tipo_s]
                for i, v in enumerate(vals):
                    tc = cor if i == 5 else "#cccccc"
                    ctk.CTkLabel(row, text=v, text_color=tc,
                                 font=ctk.CTkFont(size=11),
                                 width=110 if i > 0 else 80).grid(row=0, column=i, padx=4, pady=3)
        except Exception as e:
            for w in self.hist_frame.winfo_children()[1:]:
                w.destroy()
            ctk.CTkLabel(self.hist_frame, text=f"Erro: {e}",
                         text_color="#ff4444").pack(pady=10)
            self._hist_snapshot = ("erro", str(e))

    def _atualizar_status(self, estado):
        cores = {"rodando": ("#00ff88","● RODANDO"),
                 "parado":  ("#ff4444","● PARADO"),
                 "conectando": ("#ffaa00","● CONECTANDO..."),
                 "erro":    ("#ff4444","● ERRO MT5")}
        cor, txt = cores.get(estado, ("#888888","● DESCONHECIDO"))
        self.lbl_status.configure(text=txt, text_color=cor)

    # ── Poll loop ─────────────────────────────────────────────────
    _poll_tick = 0

    def _poll(self):
        # Logs
        while not self.log_q.empty():
            self._append_log(self.log_q.get_nowait())
        # Status / Dashboard do engine
        while not self.stat_q.empty():
            tipo, dado = self.stat_q.get_nowait()
            if tipo == "status":      self._atualizar_status(dado)
            elif tipo == "dashboard": self._atualizar_dashboard(dado)
        # Atualiza historico a cada 10s mesmo sem o bot rodando
        self._poll_tick += 1
        if self._poll_tick >= 20:   # 20 * 500ms = 10s
            self._poll_tick = 0
            if not (self.engine and self.engine.running):
                self._atualizar_historico()
        # Auto-update: thread daemon enfileira info quando detecta nova versao;
        # main thread (aqui) eh quem mostra o dialogo (Tkinter nao eh thread-safe).
        try:
            from updater import consume_pending_update, handle_update_choice
            pending = consume_pending_update()
            if pending:
                handle_update_choice(pending, parent=self)
        except Exception as _e:
            print(f"[updater] erro no _poll: {_e}")
        self.after(500, self._poll)


# ── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        from updater import check_for_update_async
        # 60s = intervalo de TESTE da checagem periodica.
        # Em producao, voltar para o default (1800s = 30min) para nao
        # estourar o rate limit do GitHub (60 req/h sem auth).
        check_for_update_async(__version__, interval_seconds=60)
    except Exception as _e:
        print(f"[updater] indisponivel: {_e}")

    app = ScalperFlowApp()
    app.mainloop()
