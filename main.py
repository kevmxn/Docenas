#!/usr/bin/env python3
"""
Auto Roulette — Bot de señales para Columnas exclusivamente
Sistema AMX · Markov + Decision Tree ML + EMA 4/8/20
  - Señales para 2 columnas simultáneas (la tercera domina → apostamos las otras dos)
  - Detección multi-modo: EMA moderado + tendencia + frecuencia + DT directo
  - Árbol de decisión entrenado en tiempo real (sklearn)
  - Cadena de Markov orden 1 y 2 combinados
  - 2 intentos máximo (1 gale)
  - Stats: cada 20 señales + 24h (actualiza a las 12:00 AR)
  - Persistencia SQLite 24/7
  - Self-ping cada 4 min (anti-sleep Render)
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from collections import deque, defaultdict
from typing import Optional

import numpy as np
from sklearn.tree import DecisionTreeClassifier
import telebot
import websockets
from flask import Flask, jsonify
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [AutoRoulette] %(levelname)s %(message)s'
)
logger = logging.getLogger("AutoRouletteAMX")

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
TOKEN   = "8615799238:AAG2kLg-Ostc4Y4E98HXDIoje_U4F7oqdzU"
CHAT_ID = -1003821352139
THREAD_ID: Optional[int] = None

_session = requests.Session()
_retry = Retry(total=5, backoff_factor=1.5,
               status_forcelist=[429, 500, 502, 503, 504],
               allowed_methods=["GET","POST"], raise_on_status=False)
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))
_session.mount("http://",  HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))

bot = telebot.TeleBot(TOKEN, threaded=False)
bot.session = _session

# ─── CONSTANTES ───────────────────────────────────────────────────────────────
WS_URL    = "wss://dga.pragmaticplaylive.net/ws"
CASINO_ID = "ppcjd00000007254"
WS_KEY    = 225
LIVE_DB   = "auto_roulette_live.db"

BASE_BET     = 0.10
MAX_ATTEMPTS = 2     # 1 señal + 1 gale
WARMUP_SPINS = 25
MIN_STREAK   = 2     # racha mínima para señal principal
MIN_PROB     = 0.58  # umbral probabilidad unificada (ajustable con /umbral)

# ─── MAPAS ────────────────────────────────────────────────────────────────────
REAL_COLOR_MAP: dict[int, str] = {
    0:"VERDE",
    1:"ROJO",2:"NEGRO",3:"ROJO",4:"NEGRO",5:"ROJO",6:"NEGRO",
    7:"ROJO",8:"NEGRO",9:"ROJO",10:"NEGRO",11:"NEGRO",12:"ROJO",
    13:"NEGRO",14:"ROJO",15:"NEGRO",16:"ROJO",17:"NEGRO",18:"ROJO",
    19:"ROJO",20:"NEGRO",21:"ROJO",22:"NEGRO",23:"ROJO",24:"NEGRO",
    25:"ROJO",26:"NEGRO",27:"ROJO",28:"NEGRO",29:"NEGRO",30:"ROJO",
    31:"NEGRO",32:"ROJO",33:"NEGRO",34:"ROJO",35:"NEGRO",36:"ROJO",
}
COLOR_EMOJI = {"ROJO":"🔴","NEGRO":"⚫️","VERDE":"🟢"}

def get_column(n: int) -> int:
    """0=VERDE, 1=C1(1,4,7..), 2=C2(2,5,8..), 3=C3(3,6,9..)"""
    if n == 0: return 0
    return ((n - 1) % 3) + 1

COLUMN_INFO = {
    1: {"name":"C1","emoji":"🟡"},
    2: {"name":"C2","emoji":"🔵"},
    3: {"name":"C3","emoji":"🔴"},
}
COLUMN_PAIRS = {1:(2,3), 2:(1,3), 3:(1,2)}

# ─── SQLITE ───────────────────────────────────────────────────────────────────
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(LIVE_DB, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_spins (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            number INTEGER NOT NULL,
            ts     INTEGER NOT NULL
        )
    """)
    # Tabla de patrones Markov persistida (orden 1 y 2)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS markov_counts (
            pattern TEXT NOT NULL,
            result  INTEGER NOT NULL,
            count   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (pattern, result)
        )
    """)
    # Tabla de muestras DT persistida
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dt_samples (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            features TEXT NOT NULL,
            target   INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn

# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────
_TG_RETRIES = 12

def _tg_call(fn, *a, **kw):
    delay = 2.0
    for attempt in range(1, _TG_RETRIES + 1):
        try:
            return fn(*a, **kw)
        except Exception as e:
            err = str(e)
            if "retry after" in err.lower():
                try:    wait = int(''.join(filter(str.isdigit, err))) + 1
                except: wait = 30
                time.sleep(wait)
                continue
            if attempt == _TG_RETRIES:
                logger.error(f"Telegram falló tras {_TG_RETRIES} intentos: {e}")
                return None
            time.sleep(delay); delay = min(delay*2, 60)
    return None

def tg_send(text: str) -> Optional[int]:
    kwargs = dict(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    if THREAD_ID: kwargs["message_thread_id"] = THREAD_ID
    msg = _tg_call(bot.send_message, **kwargs)
    return msg.message_id if msg else None

def tg_delete(msg_id: int):
    _tg_call(bot.delete_message, chat_id=CHAT_ID, message_id=msg_id)

# ─── EMA ──────────────────────────────────────────────────────────────────────
def calc_ema(data: list, period: int) -> list:
    if len(data) < period: return [None]*len(data)
    mult = 2 / (period + 1)
    out  = [None]*(period-1)
    prev = sum(data[:period]) / period
    out.append(prev)
    for v in data[period:]:
        prev = v*mult + prev*(1-mult)
        out.append(prev)
    return out

def ema_signal(levels: list, mode: str = "moderado") -> bool:
    """EMA 4/8/20 — retorna True si hay condición de señal."""
    if len(levels) < 20: return False
    e4  = calc_ema(levels, 4)
    e8  = calc_ema(levels, 8)
    e20 = calc_ema(levels, 20)
    li  = len(levels) - 1
    if any(v is None for v in [e4[li], e8[li], e20[li]]): return False
    cur = levels[li]
    ce4, ce8, ce20 = e4[li], e8[li], e20[li]
    pe4  = e4[li-1]  if li > 0 and e4[li-1]  is not None else ce4
    pe8  = e8[li-1]  if li > 0 and e8[li-1]  is not None else ce8
    pe20 = e20[li-1] if li > 0 and e20[li-1] is not None else ce20

    if mode == "tendencia":
        return ((pe4 <= pe20 and ce4 > ce20) or
                (cur > ce4 and cur > ce8 and cur > ce20))
    else:  # moderado
        v_pattern = False
        if len(levels) >= 3:
            a, b, c = levels[-3], levels[-2], levels[-1]
            v_pattern = (b < a) and (b < c) and (c > a)
        return ((pe4 <= pe8 and ce4 > ce8) or
                (pe8 <= pe20 and ce8 > ce20) or
                (cur > ce4 and cur > ce8) or
                v_pattern)

# ─── MARKOV COMBINADO (orden 1 + orden 2) ────────────────────────────────────
class MarkovPredictor:
    """Combina Markov orden 1 y 2 para mayor cobertura de patrones."""
    def __init__(self):
        self.counts1: dict = defaultdict(lambda: defaultdict(int))  # orden 1
        self.counts2: dict = defaultdict(lambda: defaultdict(int))  # orden 2

    def update(self, history: list):
        h = [x for x in history if x != 0]
        for i in range(len(h) - 1):
            self.counts1[(h[i],)][h[i+1]] += 1
        for i in range(len(h) - 2):
            self.counts2[(h[i], h[i+1])][h[i+2]] += 1

    def predict(self, history: list) -> Optional[dict]:
        h = [x for x in history if x != 0]
        if len(h) < 1: return None

        results: dict = {}

        # Orden 2 (prioridad si hay datos)
        if len(h) >= 2:
            pat2 = (h[-2], h[-1])
            c2   = dict(self.counts2.get(pat2, {}))
            tot2 = sum(c2.values())
            if tot2 >= 3:
                for k,v in c2.items(): results[k] = 0.6 * v / tot2

        # Orden 1 (siempre)
        pat1 = (h[-1],)
        c1   = dict(self.counts1.get(pat1, {}))
        tot1 = sum(c1.values())
        if tot1 >= 2:
            w = 0.4 if results else 1.0
            for k,v in c1.items():
                results[k] = results.get(k, 0) + w * v / tot1

        if not results: return None
        total = sum(results.values())
        return {k: v/total for k,v in results.items()} if total > 0 else None

# ─── DECISION TREE ML ─────────────────────────────────────────────────────────
class DecisionTreePredictor:
    """
    Árbol de decisión entrenado en tiempo real.
    Features: últimos 5 cols + racha + EMA4/8/20 + frecuencias relativas
    """
    WINDOW = 5

    def __init__(self):
        self.clf     = DecisionTreeClassifier(max_depth=10, min_samples_split=4,
                                              min_samples_leaf=2)
        self.X: list = []
        self.y: list = []
        self.trained = False

    def _make_features(self, history: list, ema4: float, ema8: float,
                       ema20: float, streak: int, freq: dict) -> list:
        h  = [x for x in history if x != 0]
        window = (h[-self.WINDOW:] if len(h) >= self.WINDOW
                  else [0]*(self.WINDOW - len(h)) + h)
        return (window +
                [round(ema4, 3), round(ema8, 3), round(ema20, 3), streak] +
                [round(freq.get(1, 1/3), 3),
                 round(freq.get(2, 1/3), 3),
                 round(freq.get(3, 1/3), 3)])

    def _get_freq(self, history: list, window: int = 15) -> dict:
        """Frecuencia relativa de cada columna en los últimos N giros."""
        h = [x for x in history if x != 0][-window:]
        if not h: return {1:1/3, 2:1/3, 3:1/3}
        total = len(h)
        return {c: h.count(c)/total for c in (1,2,3)}

    def add_sample_return(self, history: list, levels: list, streak: int) -> Optional[dict]:
        """Agrega muestra, entrena si aplica, retorna para persistencia."""
        h = [x for x in history if x != 0]
        if len(h) < self.WINDOW + 1:
            return None
        target  = h[-1]
        prev_h  = h[:-1]
        lv_prev = levels[:-1] if len(levels) > 1 else levels
        e4  = calc_ema(lv_prev, 4)
        e8  = calc_ema(lv_prev, 8)
        e20 = calc_ema(lv_prev, 20)
        v4  = e4[-1]  if e4  and e4[-1]  is not None else 0.0
        v8  = e8[-1]  if e8  and e8[-1]  is not None else 0.0
        v20 = e20[-1] if e20 and e20[-1] is not None else 0.0
        freq  = self._get_freq(prev_h)
        feats = self._make_features(prev_h, v4, v8, v20, streak, freq)
        self.X.append(feats)
        self.y.append(target)
        if len(self.X) >= 30 and len(self.X) % 5 == 0:
            self._train()
        return {"features": feats, "target": target}

    def add_sample(self, history: list, levels: list, streak: int):
        """Alias para compatibilidad."""
        self.add_sample_return(history, levels, streak)

    def _train(self):
        if len(set(self.y)) < 2: return
        try:
            self.clf.fit(self.X, self.y)
            self.trained = True
        except Exception as e:
            logger.debug(f"DT training error: {e}")

    def predict(self, history: list, levels: list, streak: int) -> Optional[dict]:
        if not self.trained: return None
        h = [x for x in history if x != 0]
        if len(h) < self.WINDOW: return None
        e4  = calc_ema(levels, 4)
        e8  = calc_ema(levels, 8)
        e20 = calc_ema(levels, 20)
        v4  = e4[-1]  if e4  and e4[-1]  is not None else 0.0
        v8  = e8[-1]  if e8  and e8[-1]  is not None else 0.0
        v20 = e20[-1] if e20 and e20[-1] is not None else 0.0
        freq  = self._get_freq(h)
        feats = self._make_features(h, v4, v8, v20, streak, freq)
        try:
            proba   = self.clf.predict_proba([feats])[0]
            classes = self.clf.classes_
            return {int(c): float(p) for c, p in zip(classes, proba)}
        except Exception:
            return None

# ─── DETAILED STATS ───────────────────────────────────────────────────────────
class DetailedStats:
    def __init__(self):
        self.total = self.wins_a1 = self.wins_a2 = self.losses = 0
        self.last_stats_at   = 0
        self.batch_start_bankroll: Optional[float] = None
        self.batch_w1 = self.batch_w2 = self.batch_l = 0
        self.last_daily_date      = ""
        self.daily_start_bankroll: Optional[float] = None
        self.daily_total = self.daily_w1 = self.daily_w2 = self.daily_l = 0

    def record(self, attempt: int, won: bool, bankroll: float):
        self.total += 1
        if won:
            if attempt == 1: self.wins_a1 += 1
            else:            self.wins_a2 += 1
        else:
            self.losses += 1
        self.daily_total += 1
        if won:
            if attempt == 1: self.daily_w1 += 1
            else:            self.daily_w2 += 1
        else:
            self.daily_l += 1
        if self.daily_start_bankroll is None:
            self.daily_start_bankroll = bankroll

    def should_send(self) -> bool:
        return (self.total - self.last_stats_at) >= 20

    def mark_sent(self, bankroll: float):
        self.last_stats_at        = self.total
        self.batch_start_bankroll = bankroll
        self.batch_w1 = self.wins_a1
        self.batch_w2 = self.wins_a2
        self.batch_l  = self.losses

    def batch_stats(self, bankroll: float) -> dict:
        n  = self.total - self.last_stats_at
        w1 = self.wins_a1 - self.batch_w1
        w2 = self.wins_a2 - self.batch_w2
        l  = self.losses  - self.batch_l
        w  = w1 + w2
        bk = round(bankroll - self.batch_start_bankroll, 2) \
             if self.batch_start_bankroll is not None else 0.0
        return {"n":n,"w1":w1,"w2":w2,"l":l,"w":w,
                "eff":round(w/n*100,1) if n else 0.0,
                "e1":round(w1/n*100,1) if n else 0.0,
                "e2":round(w2/n*100,1) if n else 0.0,
                "el":round(l/n*100,1) if n else 0.0,
                "bk":bk}

    def daily_stats(self, bankroll: float) -> dict:
        n  = self.daily_total
        bk = round(bankroll - self.daily_start_bankroll, 2) \
             if self.daily_start_bankroll is not None else 0.0
        w  = self.daily_w1 + self.daily_w2
        return {"n":n,"w1":self.daily_w1,"w2":self.daily_w2,"l":self.daily_l,"w":w,
                "eff":round(w/n*100,1) if n else 0.0,
                "e1":round(self.daily_w1/n*100,1) if n else 0.0,
                "e2":round(self.daily_w2/n*100,1) if n else 0.0,
                "el":round(self.daily_l/n*100,1) if n else 0.0,
                "bk":bk}

    def reset_daily(self, date_str: str, bankroll: float):
        self.last_daily_date      = date_str
        self.daily_start_bankroll = bankroll
        self.daily_total = self.daily_w1 = self.daily_w2 = self.daily_l = 0

# ─── ROULETTE ENGINE ──────────────────────────────────────────────────────────
class AutoRouletteEngine:
    """Motor exclusivo para Columnas."""

    def __init__(self):
        # Historial
        self.spin_history:    list = []
        self.column_history:  list = []  # 0,1,2,3

        # Niveles acumulados por columna (para EMA)
        self.column_levels: dict[int, list] = {1:[], 2:[], 3:[]}

        # Predictores
        self.markov = MarkovPredictor()
        self.dt     = DecisionTreePredictor()

        # Rachas
        self.column_streak = 0
        self.last_column   = 0

        # Estado señal
        self.signal_active:  bool          = False
        self.signal_excl:    int           = 0
        self.signal_pair:    tuple         = ()
        self.signal_trigger_number: int    = 0
        self.signal_trigger_color:  str    = ""
        self.signal_mode:    str           = ""   # "ema_mod"|"ema_tend"|"dt_high"|"freq"
        self.attempts_left:  int           = MAX_ATTEMPTS
        self.signal_msg_ids: list          = []
        self.gale_active:    bool          = False

        # Bankroll
        self.bankroll: float = 0.0

        # Stats
        self.stats = DetailedStats()

        # Persistencia
        self._db        = _get_db()
        live_loaded     = self._load_live_history()
        self.ws_count:   int  = live_loaded
        self.warmup_done: bool = live_loaded >= WARMUP_SPINS
        self.last_game_id: Optional[str] = None

        logger.info(f"[Auto] Iniciado — {live_loaded} giros. "
                    f"Warmup: {'✅' if self.warmup_done else f'{live_loaded}/{WARMUP_SPINS}'}")

    # ── Persistencia ──────────────────────────────────────────────────────────
    def _load_live_history(self) -> int:
        try:
            # Cargar TODOS los giros guardados — sin límite de fecha
            rows = self._db.execute(
                "SELECT number FROM live_spins ORDER BY id ASC"
            ).fetchall()
        except Exception as e:
            logger.warning(f"[Auto] Error cargando historial: {e}")
            return 0

        # Restaurar patrones Markov desde DB
        try:
            pat_rows = self._db.execute(
                "SELECT pattern, result, count FROM markov_counts"
            ).fetchall()
            for (pattern_str, result, count) in pat_rows:
                pattern = tuple(int(x) for x in pattern_str.split(","))
                self.markov.counts1[pattern][result] += count
                if len(pattern) == 2:
                    self.markov.counts2[pattern][result] += count
        except Exception as e:
            logger.debug(f"[Auto] Patrones Markov no cargados: {e}")

        # Restaurar muestras DT desde DB
        try:
            dt_rows = self._db.execute(
                "SELECT features, target FROM dt_samples"
            ).fetchall()
            for (feats_str, target) in dt_rows:
                feats = [float(x) for x in feats_str.split(",")]
                self.dt.X.append(feats)
                self.dt.y.append(int(target))
            if len(self.dt.X) >= 30:
                self.dt._train()
        except Exception as e:
            logger.debug(f"[Auto] Muestras DT no cargadas: {e}")

        # Aplicar giros al estado (solo para historial en memoria / EMA)
        for (n,) in rows:
            self._update_state(n, persist=False, save_patterns=False)

        logger.info(f"[Auto] ✅ {len(rows)} giros + "
                    f"{len(self.dt.X)} muestras DT + "
                    f"Markov {'OK' if self.markov.counts1 else 'vacío'} cargados")
        return len(rows)

    def _persist(self, number: int):
        try:
            self._db.execute(
                "INSERT INTO live_spins(number,ts) VALUES(?,?)",
                (number, int(time.time()))
            )
            self._db.commit()
        except Exception as e:
            logger.warning(f"[Auto] SQLite error: {e}")
            try:
                self._db = _get_db()
                self._db.execute(
                    "INSERT INTO live_spins(number,ts) VALUES(?,?)",
                    (number, int(time.time()))
                )
                self._db.commit()
            except Exception as e2:
                logger.error(f"[Auto] SQLite irrecuperable: {e2}")

    # ── Actualizar estado ──────────────────────────────────────────────────────
    def _update_state(self, number: int, persist: bool = True,
                      save_patterns: bool = True):
        color  = REAL_COLOR_MAP.get(number, "VERDE")
        column = get_column(number)

        self.spin_history.append({"number":number,"color":color,"column":column})
        if len(self.spin_history) > 500: self.spin_history.pop(0)

        self.column_history.append(column)
        if len(self.column_history) > 500: self.column_history.pop(0)

        # Racha
        if column != 0:
            if column == self.last_column:
                self.column_streak += 1
            else:
                self.last_column   = column
                self.column_streak = 1

        # Niveles acumulados (sin límite para predicciones futuras)
        for c in (1, 2, 3):
            if column != 0:
                delta = 1 if column == c else -1
                prev  = self.column_levels[c][-1] if self.column_levels[c] else 0
                self.column_levels[c].append(prev + delta)
                if len(self.column_levels[c]) > 500: self.column_levels[c].pop(0)

        # Actualizar Markov
        self.markov.update(self.column_history)

        # Agregar muestra al DT
        if column != 0 and len(self.column_levels[column]) > 5:
            sample = self.dt.add_sample_return(
                self.column_history, self.column_levels[column], self.column_streak)
            # Persistir muestra DT en DB
            if save_patterns and sample:
                self._persist_dt_sample(sample["features"], sample["target"])
            # Persistir conteos Markov en DB
            if save_patterns:
                self._persist_markov_update(self.column_history)

        if persist:
            self._persist(number)

    def _persist_dt_sample(self, features: list, target: int):
        """Guarda una muestra del DT en SQLite."""
        try:
            feats_str = ",".join(str(round(f, 4)) for f in features)
            self._db.execute(
                "INSERT INTO dt_samples(features, target) VALUES(?,?)",
                (feats_str, target)
            )
            self._db.commit()
        except Exception as e:
            logger.debug(f"[Auto] Error guardando muestra DT: {e}")

    def _persist_markov_update(self, history: list):
        """Actualiza conteos de Markov en SQLite (incrementa o inserta)."""
        h = [x for x in history if x != 0]
        if len(h) < 2: return
        try:
            # Orden 1
            p1  = str(h[-2])
            res = h[-1]
            self._db.execute(
                "INSERT INTO markov_counts(pattern,result,count) VALUES(?,?,1) "
                "ON CONFLICT(pattern,result) DO UPDATE SET count=count+1",
                (p1, res)
            )
            # Orden 2
            if len(h) >= 3:
                p2 = f"{h[-3]},{h[-2]}"
                self._db.execute(
                    "INSERT INTO markov_counts(pattern,result,count) VALUES(?,?,1) "
                    "ON CONFLICT(pattern,result) DO UPDATE SET count=count+1",
                    (p2, res)
                )
            self._db.commit()
        except Exception as e:
            logger.debug(f"[Auto] Error guardando Markov: {e}")

    # ── Probabilidad unificada ────────────────────────────────────────────────
    def _unified_prob(self, excl: int) -> dict:
        """
        Calcula probabilidad de que la próxima columna sea `excl`.
        Retorna {"excl_prob": p, "pair_prob": 1-p, "source": str}
        """
        markov_pred = self.markov.predict(self.column_history)
        dt_pred     = self.dt.predict(
            self.column_history,
            self.column_levels.get(excl, []),
            self.column_streak
        )

        m_p  = markov_pred.get(excl, 1/3) if markov_pred else 1/3
        dt_p = dt_pred.get(excl, 1/3)     if dt_pred     else 1/3

        if dt_pred and markov_pred:
            combined = 0.40 * m_p + 0.60 * dt_p
            source   = "Markov+DT"
        elif dt_pred:
            combined = dt_p
            source   = "DT"
        else:
            combined = m_p
            source   = "Markov"

        # Probabilidad de las dos columnas apostadas = 1 - excl_prob
        pair_prob = round(1.0 - combined, 4)
        return {"excl_prob": round(combined, 4), "pair_prob": pair_prob, "source": source}

    def _freq_prob(self, excl: int, window: int = 10) -> float:
        """Frecuencia relativa de la columna excluida en los últimos 10 giros."""
        h = [x for x in self.column_history if x != 0][-window:]
        if not h: return 1/3
        return h.count(excl) / len(h)

    # ── Detección de señal — multi-modo ──────────────────────────────────────
    def _detect_signal(self) -> Optional[dict]:
        """
        Cuatro modos de detección para mayor frecuencia de señales:

        1. EMA_MODERADO — racha ≥ 2 + EMA moderado + prob_pair ≥ MIN_PROB
        2. EMA_TENDENCIA — racha ≥ 2 + EMA tendencia + prob_pair ≥ MIN_PROB
        3. DT_HIGH — sin EMA requerido; DT predice excl con >70% confianza
                     Y racha ≥ 1
        4. FREQ — columna domina en los últimos 15 giros (>50%) +
                  racha ≥ 2 + prob_pair ≥ MIN_PROB (sin EMA requerido)

        Se retorna el candidato con mayor prob_pair.
        """
        ch = [x for x in self.column_history if x != 0]
        if len(ch) < 5: return None

        candidates = []

        for excl in (1, 2, 3):
            levels = self.column_levels.get(excl, [])
            probs  = self._unified_prob(excl)
            pp     = probs["pair_prob"]   # prob de las dos apostadas
            freq   = self._freq_prob(excl, window=10)

            # ── Modo 1: EMA moderado ─────────────────────────────────────
            if (self.column_streak >= MIN_STREAK and
                    self.last_column == excl and
                    ema_signal(levels, "moderado") and
                    pp >= MIN_PROB):
                candidates.append({
                    "excl":excl, "pair":COLUMN_PAIRS[excl], "prob":pp,
                    "mode":"EMA-MOD", "streak":self.column_streak
                })

            # ── Modo 2: EMA tendencia ────────────────────────────────────
            if (self.column_streak >= MIN_STREAK and
                    self.last_column == excl and
                    ema_signal(levels, "tendencia") and
                    pp >= MIN_PROB):
                candidates.append({
                    "excl":excl, "pair":COLUMN_PAIRS[excl], "prob":pp,
                    "mode":"EMA-TEND", "streak":self.column_streak
                })

            # ── Modo 3: DT alta confianza (sin EMA) ─────────────────────
            dt_pred = self.dt.predict(
                self.column_history, levels, self.column_streak)
            if (dt_pred and
                    dt_pred.get(excl, 0) >= 0.65 and
                    self.column_streak >= 1 and
                    self.last_column == excl):
                dt_excl = dt_pred.get(excl, 0)
                dt_pp   = round(1 - dt_excl, 4)
                if dt_pp >= MIN_PROB:
                    candidates.append({
                        "excl":excl, "pair":COLUMN_PAIRS[excl], "prob":dt_pp,
                        "mode":"DT-HIGH", "streak":self.column_streak
                    })

            # ── Modo 4: Frecuencia dominante ─────────────────────────────
            if (freq >= 0.45 and
                    self.column_streak >= MIN_STREAK and
                    self.last_column == excl and
                    pp >= MIN_PROB):
                candidates.append({
                    "excl":excl, "pair":COLUMN_PAIRS[excl], "prob":pp,
                    "mode":"FREQ", "streak":self.column_streak
                })

        if not candidates: return None
        # Elegir el de mayor prob_pair
        return max(candidates, key=lambda x: x["prob"])

    # ── Mensajes ──────────────────────────────────────────────────────────────
    def _fmt_col(self, idx: int) -> str:
        info = COLUMN_INFO[idx]
        return f"{info['name']} ({info['emoji']})"

    def _build_signal_text(self, sig: dict, trigger_n: int, trigger_c: str,
                            attempt: int) -> str:
        c_emoji = COLOR_EMOJI.get(trigger_c, "")
        b1      = self._fmt_col(sig["pair"][0])
        b2      = self._fmt_col(sig["pair"][1])
        mode_tag = f"  <i>[{sig.get('mode','')} · {int(sig['prob']*100)}%]</i>"
        return (
            f"✅✅ <b>SEÑAL CONFIRMADA</b> ✅✅\n\n"
            f"🌟 ENTRAR DESPUÉS: {trigger_n} {trigger_c} {c_emoji}\n"
            f"❄️ APOSTAR: {b1} y {b2}\n\n"
            f"🔄 MAX 1 GALE\n\n"
            f"🔔 SEGURO (🟢)\n"
            f"♻️ Intento {attempt}/{MAX_ATTEMPTS}{mode_tag}"
        )

    def _build_win_text(self, number: int, color: str, col: int, attempt: int) -> str:
        info    = COLUMN_INFO.get(col, {"name":f"C{col}"})
        c_emoji = COLOR_EMOJI.get(color, "")
        return f"☑️ <b>GREEN {info['name']}</b> -- {number} {color} {c_emoji}"

    def _build_loss_text(self, number: int, color: str, col: int) -> str:
        info    = COLUMN_INFO.get(col, {"name":f"C{col}"})
        c_emoji = COLOR_EMOJI.get(color, "")
        return f"🔘 <b>LOSS {info['name']}</b> -- {number} {color} {c_emoji}"

    def _send_signal(self, sig: dict, trigger_n: int, trigger_c: str, attempt: int):
        for mid in self.signal_msg_ids:
            tg_delete(mid)
        self.signal_msg_ids = []
        msg_id = tg_send(self._build_signal_text(sig, trigger_n, trigger_c, attempt))
        if msg_id: self.signal_msg_ids.append(msg_id)
        logger.info(f"[Auto] 🎯 SEÑAL excl=C{sig['excl']} pair={sig['pair']} "
                    f"prob={sig['prob']:.0%} modo={sig.get('mode','')} intento={attempt}")

    def _send_result(self, number: int, color: str, won: bool, winning_col: int):
        for mid in self.signal_msg_ids:
            tg_delete(mid)
        self.signal_msg_ids = []
        if won:
            attempt = MAX_ATTEMPTS - self.attempts_left + 1
            tg_send(self._build_win_text(number, color, winning_col, attempt))
        else:
            tg_send(self._build_loss_text(number, color, winning_col))

    # ── Stats ─────────────────────────────────────────────────────────────────
    def _check_stats(self):
        if not self.stats.should_send(): return
        s20 = self.stats.batch_stats(self.bankroll)
        s24 = self.stats.daily_stats(self.bankroll)
        self.stats.mark_sent(self.bankroll)
        text = "📊 <b>ESTADÍSTICAS COLUMNAS</b>\n\n"
        if s20:
            text += (
                f"👉🏼 <b>ÚLTIMAS {s20['n']} SEÑALES</b>\n"
                f"🈯️ T:{s20['n']} 📈 E:{s20['eff']}%\n"
                f"1️⃣ W:{s20['w1']} → E:{s20['e1']}%\n"
                f"2️⃣ W:{s20['w2']} → E:{s20['e2']}%\n"
                f"🈲 L:{s20['l']} → E:{s20['el']}%\n"
                f"💰 Bankroll: {s20['bk']:+.2f} usd\n\n"
            )
        if s24 and s24['n'] > 0:
            text += (
                f"👉🏼 <b>ESTADÍSTICAS 24 HORAS</b>\n"
                f"🈯️ T:{s24['n']} 📈 E:{s24['eff']}%\n"
                f"1️⃣ W:{s24['w1']} → E:{s24['e1']}%\n"
                f"2️⃣ W:{s24['w2']} → E:{s24['e2']}%\n"
                f"🈲 L:{s24['l']} → E:{s24['el']}%\n"
                f"💰 Bankroll 24h: {s24['bk']:+.2f} usd"
            )
        if text: tg_send(text)

    def _check_daily_report(self):
        import datetime
        tz_ar  = datetime.timezone(datetime.timedelta(hours=-3))
        now_ar = datetime.datetime.now(tz=tz_ar)
        if now_ar.hour < 12: return
        today  = now_ar.strftime("%Y-%m-%d")
        if self.stats.last_daily_date == today: return
        bk = self.bankroll
        sd = self.stats.daily_stats(bk)
        if sd["n"] == 0:
            self.stats.reset_daily(today, bk); return
        tg_send(
            f"📅 <b>REPORTE DIARIO — {now_ar.strftime('%d/%m/%Y')}</b>\n"
            f"🕛 12:00 hs (AR)\n\n"
            f"🈯️ Total señales: {sd['n']}\n"
            f"📈 Eficiencia: {sd['eff']}%\n\n"
            f"1️⃣ W:{sd['w1']} ({sd['e1']}%)\n"
            f"2️⃣ W:{sd['w2']} ({sd['e2']}%)\n"
            f"🈲 L:{sd['l']} ({sd['el']}%)\n\n"
            f"💰 <b>Balance del día: {sd['bk']:+.2f} usd</b>"
        )
        logger.info(f"[Auto] Reporte diario enviado ({today})")
        self.stats.reset_daily(today, bk)

    # ── Resolución ────────────────────────────────────────────────────────────
    def _check_win(self, column: int) -> bool:
        return column in self.signal_pair

    def _resolve(self, number: int, color: str, column: int):
        if number == 0:   # VERDE — no cuenta
            return

        won = self._check_win(column)

        if won:
            attempt = MAX_ATTEMPTS - self.attempts_left + 1
            self.bankroll = round(self.bankroll + BASE_BET * 2, 2)
            self._send_result(number, color, True, column)
            self.stats.record(attempt, True, self.bankroll)
            self._check_daily_report()
            self._check_stats()
            self._reset_signal()
        else:
            self.bankroll = round(self.bankroll - BASE_BET, 2)
            self.attempts_left -= 1
            if self.attempts_left > 0:
                self.gale_active = True
                sig = {
                    "excl":  self.signal_excl,
                    "pair":  self.signal_pair,
                    "prob":  self._unified_prob(self.signal_excl)["pair_prob"],
                    "mode":  "GALE",
                    "streak": self.column_streak,
                }
                self._send_signal(sig, number, color, 2)
            else:
                self.bankroll = round(self.bankroll - BASE_BET, 2)
                self._send_result(number, color, False, column)
                self.stats.record(0, False, self.bankroll)
                self._check_daily_report()
                self._check_stats()
                self._reset_signal()

    def _reset_signal(self):
        self.signal_active = False
        self.signal_excl   = 0
        self.signal_pair   = ()
        self.attempts_left = MAX_ATTEMPTS
        self.gale_active   = False
        self.signal_msg_ids = []

    # ── Proceso principal ─────────────────────────────────────────────────────
    def process_number(self, number: int):
        try:
            self._process_inner(number)
        except Exception as e:
            logger.error(f"[Auto] ❌ Error process_number({number}): {e}", exc_info=True)
            if self.signal_active: self._reset_signal()

    def _process_inner(self, number: int):
        color  = REAL_COLOR_MAP.get(number, "VERDE")
        column = get_column(number)

        self._update_state(number)

        logger.info(f"[Auto] 🎰 #{len(self.spin_history)}: "
                    f"{number} {color} C{column} "
                    f"(racha C{self.last_column}×{self.column_streak})")

        # Calentamiento
        if not self.warmup_done:
            self.ws_count += 1
            if self.ws_count < WARMUP_SPINS: return
            self.warmup_done = True
            tg_send("🟢 <b>Auto Roulette</b> — Sistema listo. Emitiendo señales de columnas.")
            logger.info("[Auto] ✅ Warmup completado")

        # ── Máquina de estados ─────────────────────────────────────────────
        if self.signal_active:
            self._resolve(number, color, column)
        else:
            sig = self._detect_signal()
            if sig:
                self.signal_active = True
                self.signal_excl   = sig["excl"]
                self.signal_pair   = sig["pair"]
                self.signal_mode   = sig.get("mode","")
                self.attempts_left = MAX_ATTEMPTS
                self.signal_trigger_number = number
                self.signal_trigger_color  = color
                self._send_signal(sig, number, color, 1)

    # ── WebSocket ─────────────────────────────────────────────────────────────
    async def run_ws(self):
        reconnect_delay = 5
        while True:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=30, ping_timeout=60, close_timeout=10
                ) as ws:
                    await ws.send(json.dumps({
                        "type":"subscribe","key":WS_KEY,"casinoId":CASINO_ID
                    }))
                    logger.info(f"[Auto] ✅ WS conectado key={WS_KEY}")
                    reconnect_delay = 5
                    async for raw in ws:
                        try: data = json.loads(raw)
                        except: continue
                        if not isinstance(data, dict): continue

                        # Formato Pragmatic: last20Results
                        results = data.get("last20Results")
                        if results and isinstance(results, list):
                            latest  = results[0]
                            game_id = str(latest.get("gameId",""))
                            if game_id == self.last_game_id: continue
                            self.last_game_id = game_id
                            try:   number = int(latest.get("result",""))
                            except: continue
                            if 0 <= number <= 36:
                                self.process_number(number)
                            continue

                        # Formato alternativo directo
                        for key in ("result","number","outcome","winningNumber"):
                            if key in data:
                                try:
                                    n = int(data[key])
                                    if 0 <= n <= 36:
                                        self.process_number(n)
                                except: pass
                                break

            except Exception as e:
                logger.warning(f"[Auto] WS desconectado: {e}. Recon en {reconnect_delay}s")
                try: tg_send(f"⚠️ <b>Auto Roulette</b> — Conexión perdida. "
                             f"Reconectando en {reconnect_delay}s...")
                except: pass
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay*2, 60)

# ─── FLASK ────────────────────────────────────────────────────────────────────
app    = Flask(__name__)
engine: Optional[AutoRouletteEngine] = None

@app.route("/")
def home():
    return jsonify({
        "status": "ok", "bot": "Auto Roulette Columnas AMX",
        "uptime": time.time(),
        "url":    os.environ.get("RENDER_EXTERNAL_URL",""),
    })

@app.route("/ping")
def ping():
    return jsonify({"status":"pong","ts":time.time()})

@app.route("/health")
def health():
    return jsonify({
        "status":        "healthy",
        "warmup_done":   engine.warmup_done if engine else False,
        "spins":         len(engine.spin_history) if engine else 0,
        "signal_active": engine.signal_active if engine else False,
        "bankroll":      engine.bankroll if engine else 0.0,
        "dt_trained":    engine.dt.trained if engine else False,
        "col_streak":    f"C{engine.last_column}×{engine.column_streak}" if engine else "",
    })

# ─── SELF-PING ANTI-SLEEP ─────────────────────────────────────────────────────
async def self_ping_loop():
    """
    Hace self-ping cada 4 minutos para evitar que Render Free apague el servidor.
    Render duerme tras 15 min de inactividad; 4 min garantiza que nunca llega al límite.
    """
    url = os.environ.get("RENDER_EXTERNAL_URL","").rstrip("/")
    if not url:
        logger.warning("[KeepAlive] RENDER_EXTERNAL_URL no definida — self-ping inactivo")
        return
    ping_url = f"{url}/ping"
    logger.info(f"[KeepAlive] ✅ Self-ping activo → {ping_url} cada 4 min")
    await asyncio.sleep(30)
    while True:
        try:
            with urllib.request.urlopen(ping_url, timeout=15) as r:
                logger.info(f"[KeepAlive] 🟢 OK [{r.status}] → {ping_url}")
        except Exception as e:
            logger.warning(f"[KeepAlive] ❌ Falló → {ping_url}: {e}")
        await asyncio.sleep(240)   # 4 minutos

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
@bot.message_handler(commands=['start','help'])
def cmd_start(message):
    bot.reply_to(message,
        "<b>🎰 Auto Roulette Bot AMX — Columnas</b>\n\n"
        "<b>Sistema:</b> EMA 4/8/20 + Markov (ord 1+2) + Árbol de Decisión\n"
        "<b>Modos detección:</b>\n"
        "  • EMA-MOD — EMA moderado + racha ≥2\n"
        "  • EMA-TEND — EMA tendencia + racha ≥2\n"
        "  • DT-HIGH — DT >70% confianza, sin EMA\n"
        "  • FREQ — frecuencia dominante >50%\n"
        "<b>Umbral:</b> 62% prob. combinada\n"
        "<b>Max gale:</b> 1 intento adicional\n\n"
        "Comandos:\n"
        "/status - Estado actual\n"
        "/stats - Estadísticas del día\n"
        "/reset - Resetear estadísticas\n"
        "/help - Esta ayuda",
        parse_mode="HTML")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    if engine is None:
        bot.reply_to(message,"⏳ Bot iniciando..."); return
    if engine.signal_active:
        pair_str = "+".join([COLUMN_INFO[p]["name"] for p in engine.signal_pair])
        st = (f"🟢 Señal activa: {pair_str} · modo={engine.signal_mode} "
              f"(intento {MAX_ATTEMPTS-engine.attempts_left+1}/{MAX_ATTEMPTS})")
    else:
        st = "⚪ Idle"
    warmup = "✅" if engine.warmup_done else f"⏳ {engine.ws_count}/{WARMUP_SPINS}"
    bot.reply_to(message,
        f"<b>📊 ESTADO — Auto Roulette Columnas</b>\n\n"
        f"Estado: {st}\n"
        f"Calentamiento: {warmup}\n"
        f"Giros totales: {len(engine.spin_history)}\n"
        f"Racha: C{engine.last_column}×{engine.column_streak}\n"
        f"DT entrenado: {'✅' if engine.dt.trained else '⏳'} ({len(engine.dt.X)} muestras)\n"
        f"Bankroll: {engine.bankroll:.2f} usd",
        parse_mode="HTML")

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if engine is None:
        bot.reply_to(message,"⏳ Bot iniciando..."); return
    sd = engine.stats.daily_stats(engine.bankroll)
    text = (
        f"📊 <b>ESTADÍSTICAS HOY — Columnas</b>\n\n"
        f"🈯️ T:{sd['n']} 📈 E:{sd['eff']}%\n"
        f"1️⃣ W:{sd['w1']} ({sd['e1']}%)\n"
        f"2️⃣ W:{sd['w2']} ({sd['e2']}%)\n"
        f"🈲 L:{sd['l']} ({sd['el']}%)\n"
        f"💰 {sd['bk']:+.2f} usd"
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['umbral'])
def cmd_umbral(message):
    global MIN_PROB
    parts = message.text.strip().split()
    if len(parts) < 2:
        pct = str(int(MIN_PROB * 100))
        bot.reply_to(message, '🎚 Umbral actual: ' + pct + '% | Uso: /umbral 58 (rango 45-80)', parse_mode='HTML')
        return
    try:
        val = int(parts[1])
        if not (45 <= val <= 80): raise ValueError
        MIN_PROB = round(val / 100, 2)
        logger.info(f'[Auto] Umbral cambiado a {MIN_PROB}')
        bot.reply_to(message, '✅ Umbral actualizado: ' + str(val) + '% | Próximas señales requerirán >= ' + str(val) + '%', parse_mode='HTML')
    except ValueError:
        bot.reply_to(message, '⚠️ Valor inválido. Ejemplo: /umbral 58', parse_mode='HTML')

@bot.message_handler(commands=['reset'])
def cmd_reset(message):
    if engine: engine.stats = DetailedStats()
    bot.reply_to(message,"🔄 <b>Estadísticas reseteadas</b>",parse_mode="HTML")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def run_flask():
    port = int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False)

async def main():
    global engine
    engine = AutoRouletteEngine()
    tasks  = [asyncio.create_task(engine.run_ws()),
              asyncio.create_task(self_ping_loop())]
    def _poll():
        logger.info("Polling Telegram — Auto Roulette Columnas")
        bot.polling(none_stop=True,interval=1,timeout=30)
    threading.Thread(target=_poll, daemon=True).start()
    logger.info("🎰 Auto Roulette Columnas Bot AMX iniciado")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask started.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot detenido.")
