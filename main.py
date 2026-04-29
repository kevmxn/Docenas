#!/usr/bin/env python3
"""
Auto Roulette — Bot de señales para Docenas y Columnas
Sistema AMX · Markov + Decision Tree ML + EMA 4/8/20
  - Señales para 2 docenas o 2 columnas simultáneas
  - Mínimo 2 resultados consecutivos de la categoría excluida para señal
  - Umbral 80% de probabilidad para las dos docenas/columnas opuestas
  - EMA 4/8/20 sobre nivel acumulado confirma la señal
  - Árbol de decisión (sklearn) entrenado en tiempo real
  - Cadena de Markov para predicción de docena/columna
  - Análisis de patrones (Color, Paridad, Rango) de los últimos 10 resultados
  - 2 intentos máximo (1 gale)
  - Stats: cada 20 señales + 24h (se actualiza a las 12:00 AR)
  - Persistencia SQLite 24/7
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
from sklearn.preprocessing import LabelEncoder
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
WS_KEY    = 225          # ← Auto Roulette key (ajustar si es necesario)
DB_TABLE  = "auto_roulette"
LIVE_DB   = "auto_roulette_live.db"

BASE_BET          = 0.10
MAX_ATTEMPTS      = 2    # 1 señal + 1 gale
WARMUP_SPINS      = 25   # giros antes de emitir señales
MIN_STREAK        = 2    # mínimo consecutivos para señal
MIN_PROB          = 0.80 # umbral de probabilidad (80% para las dos opuestas)

# ─── MAPAS ────────────────────────────────────────────────────────────────────
REAL_COLOR_MAP: dict[int,str] = {
    0:"VERDE",
    1:"ROJO",2:"NEGRO",3:"ROJO",4:"NEGRO",5:"ROJO",6:"NEGRO",
    7:"ROJO",8:"NEGRO",9:"ROJO",10:"NEGRO",11:"NEGRO",12:"ROJO",
    13:"NEGRO",14:"ROJO",15:"NEGRO",16:"ROJO",17:"NEGRO",18:"ROJO",
    19:"ROJO",20:"NEGRO",21:"ROJO",22:"NEGRO",23:"ROJO",24:"NEGRO",
    25:"ROJO",26:"NEGRO",27:"ROJO",28:"NEGRO",29:"NEGRO",30:"ROJO",
    31:"NEGRO",32:"ROJO",33:"NEGRO",34:"ROJO",35:"NEGRO",36:"ROJO",
}
COLOR_EMOJI = {"ROJO":"🔴","NEGRO":"⚫️","VERDE":"🟢"}

# ─── MAPAS DE PARIDAD Y RANGO ────────────────────────────────────────────────
def get_parity(n: int) -> str:
    """Retorna 'PAR' o 'IMPAR' (0='CERO')"""
    if n == 0: return "CERO"
    return "PAR" if n % 2 == 0 else "IMPAR"

def get_range(n: int) -> str:
    """Retorna 'BAJO'(1-18), 'ALTO'(19-36), o 'CERO'"""
    if n == 0: return "CERO"
    if n <= 18: return "BAJO"
    return "ALTO"

PARITY_EMOJI = {"PAR":"⚖️", "IMPAR":"⚡", "CERO":"🟢"}
RANGE_EMOJI  = {"BAJO":"⬇️", "ALTO":"⬆️", "CERO":"🟢"}

# Cuántos números de cada rango hay en cada docena/columna
# BAJO=1-18, ALTO=19-36
DOZEN_RANGE_WEIGHTS = {
    1: {"BAJO": 12, "ALTO": 0},    # D1(1-12): 12 bajos, 0 altos
    2: {"BAJO": 6,  "ALTO": 6},    # D2(13-24): 6 bajos, 6 altos
    3: {"BAJO": 0,  "ALTO": 12},   # D3(25-36): 0 bajos, 12 altos
}
COLUMN_RANGE_WEIGHTS = {
    1: {"BAJO": 6, "ALTO": 6},     # C1: 6 bajos(1,4,7,10,13,16), 6 altos(19,22,25,28,31,34)
    2: {"BAJO": 6, "ALTO": 6},     # C2: 6 bajos(2,5,8,11,14,17), 6 altos(20,23,26,29,32,35)
    3: {"BAJO": 6, "ALTO": 6},     # C3: 6 bajos(3,6,9,12,15,18), 6 altos(21,24,27,30,33,36)
}

# Qué paridad predomina en cada docena/columna
DOZEN_PARITY_WEIGHTS = {
    1: {"PAR": 6, "IMPAR": 6},    # D1(1-12): 6 pares, 6 impares
    2: {"PAR": 6, "IMPAR": 6},    # D2(13-24): 6 pares, 6 impares
    3: {"PAR": 6, "IMPAR": 6},    # D3(25-36): 6 pares, 6 impares
}
COLUMN_PARITY_WEIGHTS = {
    1: {"PAR": 4, "IMPAR": 4},    # C1(1,4,7,10,13,16,19,22): 4 pares, 4 impares
    2: {"PAR": 4, "IMPAR": 4},    # C2(2,5,8,11,14,17,20,23): 4 pares, 4 impares
    3: {"PAR": 4, "IMPAR": 4},    # C3(3,6,9,12,15,18,21,24): 4 pares, 4 impares
}

# Qué color predomina en cada docena/columna (conteo de números de cada color)
DOZEN_COLOR_WEIGHTS = {
    1: {"ROJO": 6, "NEGRO": 6},   # D1: 6R, 6N
    2: {"ROJO": 6, "NEGRO": 6},   # D2: 6R, 6N
    3: {"ROJO": 6, "NEGRO": 6},   # D3: 6R, 6N
}
COLUMN_COLOR_WEIGHTS = {
    1: {"ROJO": 4, "NEGRO": 4},   # C1: 4R, 4N
    2: {"ROJO": 4, "NEGRO": 4},   # C2: 4R, 4N
    3: {"ROJO": 4, "NEGRO": 4},   # C3: 4R, 4N
}

def get_dozen(n: int) -> int:
    """0=VERDE, 1=D1(1-12), 2=D2(13-24), 3=D3(25-36)"""
    if n == 0: return 0
    return (n - 1) // 12 + 1

def get_column(n: int) -> int:
    """0=VERDE, 1=C1, 2=C2, 3=C3"""
    if n == 0: return 0
    return ((n - 1) % 3) + 1

# Docena/Columna → icono y nombre
DOZEN_INFO = {
    1: {"name":"D1","emoji":"🟡","range":"(1-12)"},
    2: {"name":"D2","emoji":"🔵","range":"(13-24)"},
    3: {"name":"D3","emoji":"🔴","range":"(25-36)"},
}
COLUMN_INFO = {
    1: {"name":"C1","emoji":"🟡","range":"(1,4,7...)"},
    2: {"name":"C2","emoji":"🔵","range":"(2,5,8...)"},
    3: {"name":"C3","emoji":"🔴","range":"(3,6,9...)"},
}

# Pares: qué apostar cuando la categoría excluida domina
DOZEN_PAIRS = {
    1: (2, 3),   # D1 domina → apostar D2+D3
    2: (1, 3),   # D2 domina → apostar D1+D3
    3: (1, 2),   # D3 domina → apostar D1+D2
}
COLUMN_PAIRS = {
    1: (2, 3),
    2: (1, 3),
    3: (1, 2),
}

# ─── SQLITE ───────────────────────────────────────────────────────────────────
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(LIVE_DB, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number INTEGER NOT NULL,
            ts     INTEGER NOT NULL
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
    """
    Igual al checkAlerts/checkModerateAlerts del HTML.
    Retorna True si las condiciones EMA se cumplen para señal.
    """
    if len(levels) < 20: return False
    e4  = calc_ema(levels, 4)
    e8  = calc_ema(levels, 8)
    e20 = calc_ema(levels, 20)
    li  = len(levels) - 1
    if any(v is None for v in [e4[li], e8[li], e20[li]]): return False
    cur = levels[li]
    ce4, ce8, ce20 = e4[li], e8[li], e20[li]
    pe4  = e4[li-1]  if e4[li-1]  is not None else ce4
    pe8  = e8[li-1]  if e8[li-1]  is not None else ce8
    pe20 = e20[li-1] if e20[li-1] is not None else ce20

    if mode == "tendencia":
        cross_4_20 = (pe4 <= pe20) and (ce4 > ce20)
        over_all   = cur > ce4 and cur > ce8 and cur > ce20
        return cross_4_20 or over_all
    else:  # moderado
        cross_4_8  = (pe4 <= pe8) and (ce4 > ce8)
        cross_8_20 = (pe8 <= pe20) and (ce8 > ce20)
        over_2     = cur > ce4 and cur > ce8
        v_pattern  = False
        if len(levels) >= 3:
            a, b, c = levels[-3], levels[-2], levels[-1]
            v_pattern = (b < a) and (b < c) and (c > a)
        return cross_4_8 or cross_8_20 or over_2 or v_pattern

# ─── MARKOV ───────────────────────────────────────────────────────────────────
class MarkovPredictor:
    """Cadena de Markov orden 2 para docenas/columnas."""
    ORDER = 2
    def __init__(self):
        self.counts: dict = defaultdict(lambda: defaultdict(int))

    def update(self, history: list):
        h = [x for x in history if x != 0]
        for i in range(len(h)-self.ORDER):
            pat = tuple(h[i:i+self.ORDER])
            self.counts[pat][h[i+self.ORDER]] += 1

    def predict(self, history: list) -> Optional[dict]:
        h = [x for x in history if x != 0]
        if len(h) < self.ORDER: return None
        pat = tuple(h[-self.ORDER:])
        c   = dict(self.counts.get(pat, {}))
        tot = sum(c.values())
        if tot < 3: return None
        return {k: v/tot for k,v in c.items()}

# ─── PATTERN ANALYZER (Color, Paridad, Rango) ────────────────────────────────
class PatternAnalyzer:
    """
    Analiza los últimos 10 resultados clasificándolos por Color, Paridad y Rango.
    Determina qué docenas/columnas son más probables según los patrones observados.

    Rango: BAJO (1-18) y ALTO (19-36)

    Este análisis es COMPLEMENTARIO a la señal original (Markov+DT+EMA).
    No modifica la probabilidad ni el formato de la señal.
    Se almacena internamente y se registra en logs para análisis profundo.
    """
    WINDOW = 10

    def __init__(self):
        self.last_analysis: Optional[dict] = None

    def analyze(self, spin_history: list) -> dict:
        """
        Analiza los últimos 10 resultados y retorna:
        - Patrones predominantes (color, paridad, rango)
        - Para cada docena y columna: score de probabilidad según patrones
        - Qué docenas/columnas son más probables según cada categoría
        """
        if len(spin_history) < self.WINDOW:
            return self._empty_analysis()

        recent = spin_history[-self.WINDOW:]

        # ── Contar patrones ──────────────────────────────────────────────
        color_counts  = {"ROJO": 0, "NEGRO": 0, "VERDE": 0}
        parity_counts = {"PAR": 0, "IMPAR": 0, "CERO": 0}
        range_counts  = {"BAJO": 0, "ALTO": 0, "CERO": 0}

        for entry in recent:
            n = entry["number"]
            color_counts[entry["color"]] += 1
            parity_counts[get_parity(n)] += 1
            range_counts[get_range(n)] += 1

        # Patrones predominantes (el más frecuente, excluyendo VERDE/CERO)
        dominant_color  = max((k for k in color_counts if k != "VERDE"),
                             key=lambda k: color_counts[k], default="ROJO")
        dominant_parity = max((k for k in parity_counts if k != "CERO"),
                             key=lambda k: parity_counts[k], default="IMPAR")
        dominant_range  = max((k for k in range_counts if k != "CERO"),
                             key=lambda k: range_counts[k], default="BAJO")

        # ── Calcular scores por docena ───────────────────────────────────
        # Para cada docena, qué tan compatible es con los 3 patrones dominantes
        # Cada componente va de 0 a 1

        dozen_scores = {}
        for d in (1, 2, 3):
            total_nums = 12  # cada docena tiene 12 números

            # Color: fracción de números de la docena que son del color dominante
            color_match = DOZEN_COLOR_WEIGHTS[d].get(dominant_color, 0) / total_nums

            # Paridad: fracción de números de la docena que son de la paridad dominante
            parity_match = DOZEN_PARITY_WEIGHTS[d].get(dominant_parity, 0) / total_nums

            # Rango: fracción de números de la docena que están en el rango dominante
            range_match = DOZEN_RANGE_WEIGHTS[d].get(dominant_range, 0) / total_nums

            dozen_scores[d] = (color_match + parity_match + range_match) / 3.0

        # ── Calcular scores por columna ──────────────────────────────────
        column_scores = {}
        for c in (1, 2, 3):
            total_nums = 12  # cada columna tiene 12 números

            # Color: fracción de números de la columna que son del color dominante
            color_match = COLUMN_COLOR_WEIGHTS[c].get(dominant_color, 0) / total_nums

            # Paridad: fracción de números de la columna que son de la paridad dominante
            parity_match = COLUMN_PARITY_WEIGHTS[c].get(dominant_parity, 0) / total_nums

            # Rango: fracción de números de la columna que están en el rango dominante
            range_match = COLUMN_RANGE_WEIGHTS[c].get(dominant_range, 0) / total_nums

            column_scores[c] = (color_match + parity_match + range_match) / 3.0

        # ── Normalizar scores ────────────────────────────────────────────
        total_d = sum(dozen_scores.values())
        total_c = sum(column_scores.values())
        if total_d > 0:
            dozen_scores = {k: v/total_d for k, v in dozen_scores.items()}
        else:
            dozen_scores = {k: 1/3 for k in (1,2,3)}
        if total_c > 0:
            column_scores = {k: v/total_c for k, v in column_scores.items()}
        else:
            column_scores = {k: 1/3 for k in (1,2,3)}

        # ── Determinar docenas/columnas más probables por categoría ──────
        # Color → qué docenas/columnas favorece
        best_dozen_by_color = max((1,2,3), key=lambda d: DOZEN_COLOR_WEIGHTS[d].get(dominant_color, 0))
        best_column_by_color = max((1,2,3), key=lambda c: COLUMN_COLOR_WEIGHTS[c].get(dominant_color, 0))

        # Paridad → qué docenas/columnas favorece
        best_dozen_by_parity = max((1,2,3), key=lambda d: DOZEN_PARITY_WEIGHTS[d].get(dominant_parity, 0))
        best_column_by_parity = max((1,2,3), key=lambda c: COLUMN_PARITY_WEIGHTS[c].get(dominant_parity, 0))

        # Rango → qué docenas/columnas favorece
        best_dozen_by_range = max((1,2,3), key=lambda d: DOZEN_RANGE_WEIGHTS[d].get(dominant_range, 0))
        best_column_by_range = max((1,2,3), key=lambda c: COLUMN_RANGE_WEIGHTS[c].get(dominant_range, 0))

        analysis = {
            "color_counts":    color_counts,
            "parity_counts":   parity_counts,
            "range_counts":    range_counts,
            "dominant_color":  dominant_color,
            "dominant_parity": dominant_parity,
            "dominant_range":  dominant_range,
            "dozen_scores":    dozen_scores,
            "column_scores":   column_scores,
            "best_dozen_by_color":  best_dozen_by_color,
            "best_column_by_color": best_column_by_color,
            "best_dozen_by_parity": best_dozen_by_parity,
            "best_column_by_parity":best_column_by_parity,
            "best_dozen_by_range":  best_dozen_by_range,
            "best_column_by_range": best_column_by_range,
            "color_pct":  round(color_counts[dominant_color] / self.WINDOW * 100, 0),
            "parity_pct": round(parity_counts[dominant_parity] / self.WINDOW * 100, 0),
            "range_pct":  round(range_counts[dominant_range] / self.WINDOW * 100, 0),
        }
        self.last_analysis = analysis
        return analysis

    def _empty_analysis(self) -> dict:
        return {
            "color_counts":    {"ROJO": 0, "NEGRO": 0, "VERDE": 0},
            "parity_counts":   {"PAR": 0, "IMPAR": 0, "CERO": 0},
            "range_counts":    {"BAJO": 0, "ALTO": 0, "CERO": 0},
            "dominant_color":  "ROJO",
            "dominant_parity": "IMPAR",
            "dominant_range":  "BAJO",
            "dozen_scores":    {1: 1/3, 2: 1/3, 3: 1/3},
            "column_scores":   {1: 1/3, 2: 1/3, 3: 1/3},
            "best_dozen_by_color":  1,
            "best_column_by_color": 1,
            "best_dozen_by_parity": 1,
            "best_column_by_parity":1,
            "best_dozen_by_range":  1,
            "best_column_by_range": 1,
            "color_pct":  0,
            "parity_pct": 0,
            "range_pct":  0,
        }


# ─── DECISION TREE ML ─────────────────────────────────────────────────────────
class DecisionTreePredictor:
    """
    Árbol de decisión entrenado en tiempo real.
    Features: últimos 5 resultados + racha actual + EMA values
    Target: próxima docena/columna
    """
    WINDOW = 5

    def __init__(self):
        self.clf    = DecisionTreeClassifier(max_depth=8, min_samples_split=5)
        self.X: list = []
        self.y: list = []
        self.trained = False

    def _make_features(self, history: list, ema4_v: float, ema8_v: float,
                       ema20_v: float, streak: int) -> list:
        h = [x for x in history if x != 0]
        # Últimos WINDOW resultados (pad con 0 si no hay)
        window = h[-self.WINDOW:] if len(h) >= self.WINDOW else [0]*(self.WINDOW-len(h)) + h
        return window + [round(ema4_v,3), round(ema8_v,3), round(ema20_v,3), streak]

    def add_sample(self, history: list, levels: list, streak: int):
        h = [x for x in history if x != 0]
        if len(h) < self.WINDOW + 1: return
        # Target: el último valor de h
        target = h[-1]
        # Features construidas con la historia ANTES del target
        prev_h = h[:-1]
        e4  = calc_ema(levels[:-1], 4)  if len(levels)>1 else [None]
        e8  = calc_ema(levels[:-1], 8)  if len(levels)>1 else [None]
        e20 = calc_ema(levels[:-1], 20) if len(levels)>1 else [None]
        v4  = e4[-1]  if e4[-1]  is not None else 0.0
        v8  = e8[-1]  if e8[-1]  is not None else 0.0
        v20 = e20[-1] if e20[-1] is not None else 0.0
        feats = self._make_features(prev_h, v4, v8, v20, streak)
        self.X.append(feats)
        self.y.append(target)
        # Re-entrenar cada 30 muestras nuevas
        if len(self.X) >= 30 and len(self.X) % 10 == 0:
            self._train()

    def _train(self):
        if len(set(self.y)) < 2: return
        try:
            self.clf.fit(self.X, self.y)
            self.trained = True
        except Exception as e:
            logger.debug(f"DT training error: {e}")

    def predict(self, history: list, levels: list, streak: int) -> Optional[dict]:
        if not self.trained: return None
        h   = [x for x in history if x != 0]
        if len(h) < self.WINDOW: return None
        e4  = calc_ema(levels, 4)
        e8  = calc_ema(levels, 8)
        e20 = calc_ema(levels, 20)
        v4  = e4[-1]  if e4 and e4[-1]  is not None else 0.0
        v8  = e8[-1]  if e8 and e8[-1]  is not None else 0.0
        v20 = e20[-1] if e20 and e20[-1] is not None else 0.0
        feats = self._make_features(h, v4, v8, v20, streak)
        try:
            proba = self.clf.predict_proba([feats])[0]
            classes = self.clf.classes_
            return {int(c): float(p) for c,p in zip(classes, proba)}
        except Exception:
            return None

# ─── DETAILED STATS ───────────────────────────────────────────────────────────
class DetailedStats:
    def __init__(self, label: str = ""):
        self.label = label
        self.total = self.wins_a1 = self.wins_a2 = self.losses = 0
        self.last_stats_at   = 0
        self.batch_start_bankroll: Optional[float] = None
        self.batch_w1 = self.batch_w2 = self.batch_l = 0
        # Diario
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
        # Diario
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
    """Motor principal — señales para Docenas y Columnas."""

    def __init__(self):
        # Histórico de giros
        self.spin_history:   list  = []   # dicts {number, color, dozen, column}
        self.dozen_history:  list  = []   # 0,1,2,3
        self.column_history: list  = []   # 0,1,2,3

        # Niveles acumulados por categoría excluida (para EMA)
        self.dozen_levels:  dict[int,list] = {1:[],2:[],3:[]}
        self.column_levels: dict[int,list] = {1:[],2:[],3:[]}

        # Predicción
        self.markov_d = MarkovPredictor()
        self.markov_c = MarkovPredictor()
        self.dt_d     = DecisionTreePredictor()
        self.dt_c     = DecisionTreePredictor()
        self.pattern_analyzer = PatternAnalyzer()

        # Rachas actuales
        self.dozen_streak  = 0   # racha de la última docena
        self.column_streak = 0
        self.last_dozen    = 0
        self.last_column   = 0

        # Estado de señal
        self.signal_active:  bool           = False
        self.signal_type:    Optional[str]  = None   # "dozen" | "column"
        self.signal_excl:    int            = 0      # docena/columna excluida
        self.signal_pair:    tuple          = ()     # par apostado
        self.signal_trigger_number: int     = 0
        self.signal_trigger_color:  str     = ""
        self.attempts_left:  int            = MAX_ATTEMPTS
        self.signal_msg_ids: list           = []
        self.gale_active:    bool           = False
        self.signal_pattern_analysis: Optional[dict] = None  # análisis de patrones

        # Bankroll simple
        self.bankroll: float = 0.0

        # Stats
        self.stats_d = DetailedStats("Docenas")
        self.stats_c = DetailedStats("Columnas")

        # Persistencia
        self._db        = _get_db()
        live_loaded     = self._load_live_history()
        self.ws_count:   int  = live_loaded
        self.warmup_done: bool = live_loaded >= WARMUP_SPINS

        # WS dedup
        self.last_game_id: Optional[str] = None

        logger.info(f"[Auto] Iniciado — {live_loaded} giros cargados. "
                    f"Warmup: {'✅' if self.warmup_done else f'{live_loaded}/{WARMUP_SPINS}'}")

    # ── Persistencia ──────────────────────────────────────────────────────────
    def _load_live_history(self) -> int:
        try:
            cutoff = int(time.time()) - 7*86400
            rows   = self._db.execute(
                "SELECT number FROM live_spins WHERE ts>=? ORDER BY id ASC", (cutoff,)
            ).fetchall()
        except Exception as e:
            logger.warning(f"[Auto] Error cargando historial: {e}")
            return 0
        for (n,) in rows:
            self._update_state(n, persist=False)
        logger.info(f"[Auto] ✅ {len(rows)} giros cargados desde SQLite")
        return len(rows)

    def _persist(self, number: int):
        try:
            self._db.execute(
                "INSERT INTO live_spins(number,ts) VALUES(?,?)", (number, int(time.time()))
            )
            self._db.commit()
        except Exception as e:
            logger.warning(f"[Auto] SQLite error: {e}")
            try:
                self._db = _get_db()
                self._db.execute(
                    "INSERT INTO live_spins(number,ts) VALUES(?,?)", (number, int(time.time()))
                )
                self._db.commit()
            except Exception as e2:
                logger.error(f"[Auto] SQLite irrecuperable: {e2}")

    # ── Actualizar estado interno ──────────────────────────────────────────────
    def _update_state(self, number: int, persist: bool = True):
        color  = REAL_COLOR_MAP.get(number, "VERDE")
        dozen  = get_dozen(number)
        column = get_column(number)

        entry = {"number":number,"color":color,"dozen":dozen,"column":column}
        self.spin_history.append(entry)
        if len(self.spin_history) > 300: self.spin_history.pop(0)

        self.dozen_history.append(dozen)
        if len(self.dozen_history) > 300: self.dozen_history.pop(0)

        self.column_history.append(column)
        if len(self.column_history) > 300: self.column_history.pop(0)

        # Actualizar rachas
        if dozen != 0:
            if dozen == self.last_dozen:
                self.dozen_streak += 1
            else:
                self.last_dozen   = dozen
                self.dozen_streak = 1

        if column != 0:
            if column == self.last_column:
                self.column_streak += 1
            else:
                self.last_column   = column
                self.column_streak = 1

        # Actualizar niveles acumulados para EMA
        for d in (1,2,3):
            if dozen != 0:
                delta = 1 if dozen == d else -1
                prev  = self.dozen_levels[d][-1] if self.dozen_levels[d] else 0
                self.dozen_levels[d].append(prev + delta)
                if len(self.dozen_levels[d]) > 300: self.dozen_levels[d].pop(0)

        for c in (1,2,3):
            if column != 0:
                delta = 1 if column == c else -1
                prev  = self.column_levels[c][-1] if self.column_levels[c] else 0
                self.column_levels[c].append(prev + delta)
                if len(self.column_levels[c]) > 300: self.column_levels[c].pop(0)

        # Entrenar predictores
        self.markov_d.update(self.dozen_history)
        self.markov_c.update(self.column_history)
        if dozen != 0 and len(self.dozen_levels[dozen]) > 5:
            self.dt_d.add_sample(
                self.dozen_history, self.dozen_levels[dozen], self.dozen_streak)
        if column != 0 and len(self.column_levels[column]) > 5:
            self.dt_c.add_sample(
                self.column_history, self.column_levels[column], self.column_streak)

        if persist:
            self._persist(number)

    # ── Evaluación de probabilidad unificada ──────────────────────────────────
    def _unified_prob(self, category: str, target: int, excl: int) -> float:
        """
        Calcula probabilidad combinada Markov + DT para que el próximo resultado
        sea el excluido (= señal de alerta para apostar los otros dos).
        target = excl en este caso.
        """
        if category == "dozen":
            markov_pred = self.markov_d.predict(self.dozen_history)
            dt_pred     = self.dt_d.predict(self.dozen_history,
                                             self.dozen_levels.get(excl,[]), self.dozen_streak)
        else:
            markov_pred = self.markov_c.predict(self.column_history)
            dt_pred     = self.dt_c.predict(self.column_history,
                                             self.column_levels.get(excl,[]), self.column_streak)

        m_p  = markov_pred.get(excl, 1/3) if markov_pred else 1/3
        dt_p = dt_pred.get(excl, 1/3)     if dt_pred     else 1/3

        # Ponderación: 40% Markov, 60% DT
        combined = 0.40 * m_p + 0.60 * dt_p
        return round(combined, 4)

    # ── Detección de señal ────────────────────────────────────────────────────
    def _detect_signal(self) -> Optional[dict]:
        """
        Busca señal en docenas y columnas.
        Condiciones:
          1. Racha ≥ MIN_STREAK (2) de la misma docena/columna
          2. EMA confirma reversión en el nivel acumulado de esa categoría
          3. Probabilidad ajustada ≥ MIN_PROB (80% para las dos opuestas)

        Probabilidad ajustada = 70% ML (Markov+DT) + 30% Patrones (Color,Paridad,Rango)
        Los patrones AUMENTAN la probabilidad de las dos docenas/columnas apostadas.
        """
        best = None

        # ── Análisis de patrones ────────────────────────────────────────────
        pattern_analysis = self.pattern_analyzer.analyze(self.spin_history)

        # ── Docenas ───────────────────────────────────────────────────────────
        dh = [x for x in self.dozen_history if x != 0]
        if (self.dozen_streak >= MIN_STREAK and
                len(dh) >= MIN_STREAK and
                dh[-MIN_STREAK:] == [self.last_dozen]*MIN_STREAK):
            excl   = self.last_dozen
            levels = self.dozen_levels.get(excl, [])
            if ema_signal(levels, "moderado"):
                pair   = DOZEN_PAIRS[excl]
                # Probabilidad ML de que la categoría excluida aparezca
                prob_excl = self._unified_prob("dozen", excl, excl)
                # Probabilidad ML de las dos docenas OPUESTAS (las que se apuestan)
                prob_ml = 1 - prob_excl
                # Alineación de patrones con las dos docenas de la apuesta
                pa = pattern_analysis
                pattern_alignment = pa["dozen_scores"][pair[0]] + pa["dozen_scores"][pair[1]]
                # Probabilidad ajustada: 70% ML + 30% Patrones
                adjusted_prob = 0.7 * prob_ml + 0.3 * pattern_alignment
                if adjusted_prob >= MIN_PROB:
                    best = {"type":"dozen","excl":excl,"pair":pair,"prob":adjusted_prob,
                            "prob_ml":prob_ml,"prob_pattern":pattern_alignment,
                            "streak":self.dozen_streak,
                            "pattern_analysis":pattern_analysis}

        # ── Columnas ──────────────────────────────────────────────────────────
        ch = [x for x in self.column_history if x != 0]
        if (self.column_streak >= MIN_STREAK and
                len(ch) >= MIN_STREAK and
                ch[-MIN_STREAK:] == [self.last_column]*MIN_STREAK):
            excl   = self.last_column
            levels = self.column_levels.get(excl, [])
            if ema_signal(levels, "moderado"):
                pair   = COLUMN_PAIRS[excl]
                prob_excl = self._unified_prob("column", excl, excl)
                prob_ml = 1 - prob_excl
                pa = pattern_analysis
                pattern_alignment = pa["column_scores"][pair[0]] + pa["column_scores"][pair[1]]
                adjusted_prob = 0.7 * prob_ml + 0.3 * pattern_alignment
                if adjusted_prob >= MIN_PROB:
                    cand = {"type":"column","excl":excl,"pair":pair,"prob":adjusted_prob,
                            "prob_ml":prob_ml,"prob_pattern":pattern_alignment,
                            "streak":self.column_streak,
                            "pattern_analysis":pattern_analysis}
                    if best is None or adjusted_prob > best["prob"]:
                        best = cand

        return best

    # ── Helpers de display ────────────────────────────────────────────────────
    def _fmt_cat(self, cat_type: str, idx: int) -> str:
        info = DOZEN_INFO[idx] if cat_type == "dozen" else COLUMN_INFO[idx]
        return f"{info['name']} ({info['emoji']})"

    def _build_signal_text(self, sig: dict, trigger_n: int, trigger_c: str,
                            attempt: int) -> str:
        c_emoji = COLOR_EMOJI.get(trigger_c, "")
        b1      = self._fmt_cat(sig["type"], sig["pair"][0])
        b2      = self._fmt_cat(sig["type"], sig["pair"][1])
        return (
            f"✅✅ <b>SEÑAL CONFIRMADA</b> ✅✅\n\n"
            f"🌟 ENTRAR DESPUÉS: {trigger_n} {trigger_c} {c_emoji}\n"
            f"❄️ APOSTAR: {b1} y {b2}\n\n"
            f"🔄 MAX 1 GALE\n\n"
            f"🔔 SEGURO (🟢)\n"
            f"♻️ Intento {attempt}/{MAX_ATTEMPTS}"
        )

    def _build_win_text(self, number: int, color: str, cat_type: str, cat_idx: int,
                         attempt: int) -> str:
        info    = DOZEN_INFO[cat_idx] if cat_type == "dozen" else COLUMN_INFO[cat_idx]
        c_emoji = COLOR_EMOJI.get(color, "")
        return (
            f"☑️ <b>GREEN {info['name']}</b> -- "
            f"{number} {color} {c_emoji}"
        )

    def _build_loss_text(self, number: int, color: str, cat_type: str, cat_idx: int) -> str:
        info    = DOZEN_INFO[cat_idx] if cat_type == "dozen" else COLUMN_INFO[cat_idx]
        c_emoji = COLOR_EMOJI.get(color, "")
        return (
            f"🔘 <b>LOSS {info['name']}</b> -- "
            f"{number} {color} {c_emoji}"
        )

    # ── Envío de mensajes ─────────────────────────────────────────────────────
    def _send_signal(self, sig: dict, trigger_n: int, trigger_c: str, attempt: int):
        # Borrar señal anterior del mismo ciclo
        for mid in self.signal_msg_ids:
            tg_delete(mid)
        self.signal_msg_ids = []
        text   = self._build_signal_text(sig, trigger_n, trigger_c, attempt)
        msg_id = tg_send(text)
        if msg_id: self.signal_msg_ids.append(msg_id)
        prob_ml = sig.get('prob_ml', 0)
        prob_pat = sig.get('prob_pattern', 0)
        logger.info(f"[Auto] 🎯 Señal {sig['type']} excl={sig['excl']} "
                    f"pair={sig['pair']} prob={sig['prob']:.0%} "
                    f"(ML:{prob_ml:.0%} + Patrón:{prob_pat:.0%}) "
                    f"intento={attempt}")
        # Log detallado de patrones (interno, no va a Telegram)
        pa = sig.get("pattern_analysis")
        if pa:
            dc  = pa["dominant_color"]
            dp  = pa["dominant_parity"]
            dr  = pa["dominant_range"]
            ds  = pa["dozen_scores"]
            cs  = pa["column_scores"]
            logger.info(
                f"[Auto] 📋 Patrones → Color:{dc}({pa['color_pct']:.0f}%) "
                f"Paridad:{dp}({pa['parity_pct']:.0f}%) "
                f"Rango:{dr}({pa['range_pct']:.0f}%) "
                f"| Docenas D1:{ds[1]:.0%} D2:{ds[2]:.0%} D3:{ds[3]:.0%} "
                f"| Columnas C1:{cs[1]:.0%} C2:{cs[2]:.0%} C3:{cs[3]:.0%}"
            )

    def _send_result(self, number: int, color: str, won: bool,
                     cat_type: str, winning_cat: int):
        for mid in self.signal_msg_ids:
            tg_delete(mid)
        self.signal_msg_ids = []
        if won:
            text = self._build_win_text(number, color, cat_type, winning_cat,
                                         MAX_ATTEMPTS - self.attempts_left + 1)
        else:
            text = self._build_loss_text(number, color, cat_type, winning_cat)
        tg_send(text)

    # ── Stats ─────────────────────────────────────────────────────────────────
    def _stats_text(self, label: str, s20: dict, s24: dict) -> str:
        text  = f"📊 <b>ESTADÍSTICAS {label}</b>\n\n"
        if s20:
            n = s20['n']
            text += (
                f"👉🏼 <b>ÚLTIMAS {n} SEÑALES</b>\n"
                f"🈯️ T:{n} 📈 E:{s20['eff']}%\n"
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
        return text

    def _check_stats(self, stats: DetailedStats):
        if not stats.should_send(): return
        s20 = stats.batch_stats(self.bankroll)
        s24 = stats.daily_stats(self.bankroll)
        stats.mark_sent(self.bankroll)
        text = self._stats_text(stats.label, s20, s24)
        if text: tg_send(text)

    def _check_daily_report(self, stats: DetailedStats):
        import datetime
        tz_ar  = datetime.timezone(datetime.timedelta(hours=-3))
        now_ar = datetime.datetime.now(tz=tz_ar)
        if now_ar.hour < 12: return
        today  = now_ar.strftime("%Y-%m-%d")
        if stats.last_daily_date == today: return
        bk = self.bankroll
        sd = stats.daily_stats(bk)
        if sd["n"] == 0:
            stats.reset_daily(today, bk); return
        text = (
            f"📅 <b>REPORTE DIARIO — {now_ar.strftime('%d/%m/%Y')}</b>\n"
            f"🕛 12:00 hs (AR) · {stats.label}\n\n"
            f"🈯️ Total señales: {sd['n']}\n"
            f"📈 Eficiencia: {sd['eff']}%\n\n"
            f"1️⃣ W:{sd['w1']} ({sd['e1']}%)\n"
            f"2️⃣ W:{sd['w2']} ({sd['e2']}%)\n"
            f"🈲 L:{sd['l']} ({sd['el']}%)\n\n"
            f"💰 <b>Balance del día: {sd['bk']:+.2f} usd</b>"
        )
        tg_send(text)
        logger.info(f"[Auto] Reporte diario {stats.label} enviado ({today})")
        stats.reset_daily(today, bk)

    # ── Resolución de resultado ────────────────────────────────────────────────
    def _check_win(self, dozen: int, column: int) -> bool:
        if self.signal_type == "dozen":
            return dozen in self.signal_pair
        else:
            return column in self.signal_pair

    def _resolve(self, number: int, color: str, dozen: int, column: int):
        """Evalúa si el giro cierra la señal activa (ganó o perdió)."""
        # Ignorar VERDE
        if number == 0:
            self.attempts_left -= 1
            if self.attempts_left <= 0:
                self._send_result(number, color, False,
                                  self.signal_type or "dozen",
                                  self.signal_excl)
                self._resolve_stats(False)
                self._reset_signal()
            return

        won = self._check_win(dozen, column)

        if won:
            attempt = MAX_ATTEMPTS - self.attempts_left + 1
            winning_cat = dozen if self.signal_type == "dozen" else column
            self.bankroll = round(self.bankroll + BASE_BET * 2, 2)
            self._send_result(number, color, True, self.signal_type or "dozen", winning_cat)
            self._resolve_stats(True, attempt)
            self._reset_signal()
        else:
            self.bankroll = round(self.bankroll - BASE_BET, 2)
            self.attempts_left -= 1
            if self.attempts_left > 0:
                # Gale: reenviar señal para el 2do intento
                self.gale_active = True
                # Re-analizar patrones actualizados para el gale
                pa = self.pattern_analyzer.analyze(self.spin_history)
                if self.signal_type == "dozen":
                    pat_align = pa["dozen_scores"][self.signal_pair[0]] + pa["dozen_scores"][self.signal_pair[1]]
                else:
                    pat_align = pa["column_scores"][self.signal_pair[0]] + pa["column_scores"][self.signal_pair[1]]
                sig = {
                    "type": self.signal_type,
                    "excl": self.signal_excl,
                    "pair": self.signal_pair,
                    "prob": 0.7 * (1 - self._unified_prob(self.signal_type, self.signal_excl, self.signal_excl)) + 0.3 * pat_align,
                    "prob_ml": 1 - self._unified_prob(self.signal_type, self.signal_excl, self.signal_excl),
                    "prob_pattern": pat_align,
                    "pattern_analysis": pa,
                }
                self._send_signal(sig, number, color, 2)
            else:
                # Pérdida total
                self.bankroll = round(self.bankroll - BASE_BET, 2)
                losing_cat = dozen if self.signal_type == "dozen" else column
                self._send_result(number, color, False, self.signal_type or "dozen", losing_cat)
                self._resolve_stats(False)
                self._reset_signal()

    def _resolve_stats(self, won: bool, attempt: int = 0):
        stats = self.stats_d if self.signal_type == "dozen" else self.stats_c
        stats.record(attempt, won, self.bankroll)
        self._check_daily_report(stats)
        self._check_stats(stats)

    def _reset_signal(self):
        self.signal_active = False
        self.signal_type   = None
        self.signal_excl   = 0
        self.signal_pair   = ()
        self.attempts_left = MAX_ATTEMPTS
        self.gale_active   = False
        self.signal_msg_ids = []
        self.signal_pattern_analysis = None

    # ── Proceso principal ─────────────────────────────────────────────────────
    def process_number(self, number: int):
        try:
            self._process_inner(number)
        except Exception as e:
            logger.error(f"[Auto] ❌ Error process_number({number}): {e}", exc_info=True)
            if self.signal_active: self._reset_signal()

    def _process_inner(self, number: int):
        color  = REAL_COLOR_MAP.get(number, "VERDE")
        dozen  = get_dozen(number)
        column = get_column(number)

        # Actualizar estado
        self._update_state(number)

        logger.info(f"[Auto] 🎰 #{len(self.spin_history)}: {number} {color} "
                    f"D{dozen} C{column} "
                    f"(d-streak={self.dozen_streak} c-streak={self.column_streak})")

        # Calentamiento
        if not self.warmup_done:
            self.ws_count += 1
            if self.ws_count < WARMUP_SPINS:
                return
            self.warmup_done = True
            tg_send("🟢 <b>Auto Roulette</b> — Sistema listo. Emitiendo señales.")
            logger.info("[Auto] ✅ Warmup completado")

        # ── Máquina de estados ──────────────────────────────────────────────
        if self.signal_active:
            self._resolve(number, color, dozen, column)
        else:
            # Buscar nueva señal
            sig = self._detect_signal()
            if sig:
                self.signal_active = True
                self.signal_type   = sig["type"]
                self.signal_excl   = sig["excl"]
                self.signal_pair   = sig["pair"]
                self.attempts_left = MAX_ATTEMPTS
                self.signal_trigger_number = number
                self.signal_trigger_color  = color
                self.signal_pattern_analysis = sig.get("pattern_analysis")
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
                            latest   = results[0]
                            game_id  = str(latest.get("gameId",""))
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
app   = Flask(__name__)
engine: Optional[AutoRouletteEngine] = None

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "bot":    "Auto Roulette AMX",
        "uptime": time.time(),
        "url":    os.environ.get("RENDER_EXTERNAL_URL",""),
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "pong", "ts": time.time()})

@app.route("/health")
def health():
    global engine
    return jsonify({
        "status":       "healthy",
        "warmup_done":  engine.warmup_done if engine else False,
        "spins":        len(engine.spin_history) if engine else 0,
        "signal_active":engine.signal_active if engine else False,
        "bankroll":     engine.bankroll if engine else 0.0,
    })

async def self_ping_loop():
    """
    Hace self-ping cada 4 minutos para evitar que Render Free apague el servidor.
    Render duerme tras 15 min de inactividad; 4 min garantiza que nunca llega al límite.
    """
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        logger.warning("[KeepAlive] RENDER_EXTERNAL_URL no definida — self-ping inactivo")
        return
    ping_url = f"{url}/ping"
    logger.info(f"[KeepAlive] ✅ Self-ping activo → {ping_url} cada 4 min")
    # Espera inicial antes del primer ping
    await asyncio.sleep(30)
    while True:
        try:
            with urllib.request.urlopen(ping_url, timeout=15) as r:
                logger.info(f"[KeepAlive] 🟢 Self-ping OK [{r.status}] → {ping_url}")
        except Exception as e:
            logger.warning(f"[KeepAlive] ❌ Self-ping falló → {ping_url}: {e}")
        await asyncio.sleep(240)   # 4 minutos

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
@bot.message_handler(commands=['start','help'])
def cmd_start(message):
    bot.reply_to(message,
        "<b>🎰 Auto Roulette Bot AMX</b>\n\n"
        "<b>Señales:</b> Docenas + Columnas\n"
        "<b>Sistema:</b> EMA 4/8/20 + Markov + Árbol de Decisión + Patrones\n"
        "<b>Min. racha:</b> 2 resultados consecutivos\n"
        "<b>Umbral prob:</b> 80% para las dos opuestas\n"
        "<b>Patrones:</b> Color + Paridad + Rango (últimos 10)\n"
        "<b>Max gale:</b> 1 intento adicional\n\n"
        "Comandos:\n"
        "/status - Estado actual\n"
        "/stats - Estadísticas\n"
        "/reset - Resetear stats\n"
        "/help - Esta ayuda",
        parse_mode="HTML")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    if engine is None:
        bot.reply_to(message,"⏳ Bot iniciando..."); return
    if engine.signal_active:
        pair_str = "+".join([
            (DOZEN_INFO if engine.signal_type=="dozen" else COLUMN_INFO)[p]["name"]
            for p in engine.signal_pair
        ])
        st = f"🟢 Señal activa: {pair_str} (intento {MAX_ATTEMPTS-engine.attempts_left+1}/{MAX_ATTEMPTS})"
    else:
        st = "⚪ Idle"
    warmup = "✅" if engine.warmup_done else f"⏳ {engine.ws_count}/{WARMUP_SPINS}"
    bot.reply_to(message,
        f"<b>📊 ESTADO — Auto Roulette</b>\n\n"
        f"Estado: {st}\n"
        f"Calentamiento: {warmup}\n"
        f"Giros totales: {len(engine.spin_history)}\n"
        f"D-racha: D{engine.last_dozen}×{engine.dozen_streak}\n"
        f"C-racha: C{engine.last_column}×{engine.column_streak}\n"
        f"Bankroll: {engine.bankroll:.2f} usd\n"
        f"DT entrenado: {'✅' if engine.dt_d.trained else '⏳'} Docenas | "
        f"{'✅' if engine.dt_c.trained else '⏳'} Columnas",
        parse_mode="HTML")

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if engine is None:
        bot.reply_to(message,"⏳ Bot iniciando..."); return
    bk = engine.bankroll
    sd = engine.stats_d.daily_stats(bk)
    sc = engine.stats_c.daily_stats(bk)
    text = (
        f"📊 <b>ESTADÍSTICAS HOY</b>\n\n"
        f"<b>Docenas</b> · T:{sd['n']} E:{sd['eff']}%\n"
        f"1️⃣{sd['w1']}  2️⃣{sd['w2']}  ❌{sd['l']}\n"
        f"💰 {sd['bk']:+.2f} usd\n\n"
        f"<b>Columnas</b> · T:{sc['n']} E:{sc['eff']}%\n"
        f"1️⃣{sc['w1']}  2️⃣{sc['w2']}  ❌{sc['l']}\n"
        f"💰 {sc['bk']:+.2f} usd"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['reset'])
def cmd_reset(message):
    if engine:
        engine.stats_d = DetailedStats("Docenas")
        engine.stats_c = DetailedStats("Columnas")
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
        logger.info("Polling Telegram — Auto Roulette")
        bot.polling(none_stop=True,interval=1,timeout=30)
    threading.Thread(target=_poll,daemon=True).start()
    logger.info("🎰 Auto Roulette Bot AMX iniciado")
    await asyncio.gather(*tasks)

if __name__=="__main__":
    threading.Thread(target=run_flask,daemon=True).start()
    logger.info("Flask started.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot detenido.")

