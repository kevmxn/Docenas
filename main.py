#!/usr/bin/env python3
"""
Russian Roulette — Bot DC v30
===========================================================================
CAMBIOS v30 (v29 + niveles v27 + aprendizaje adaptativo):
  ① 3 Estrategias con 3 intentos:
     E1 PF+PHF+ML  — umbral ≥78% | mismo par en los 3 intentos
     E2 PHTML+EMA  — sin umbral  | cada intento usa PHTML del último número
     E3 RETORNO    — umbral ≥78% + PH confirma | mismo par en los 3 intentos
  ② Gestión de niveles de apuesta (v27 + fichas v29):
     - Intento 1   → nivel × BASE_BET  por docena
     - Intentos 2-3 → 3 × nivel × BASE_BET  por docena
     - Nivel sube (1→6) al perder los 3 intentos; baja a 1 al recuperar
  ③ Fichas v29: USD $0.10 | MXN $2.00 | PEN S/.0.40 | COP $500 | ARS $200 | CLP $50
  ④ Sin CERO en gestión de apuestas
  ⑤ Sistema de aprendizaje adaptativo (SignalLearner):
     - Registra condiciones de cada señal en la DB (persistente)
     - Registra el resultado y por qué falló/acertó
     - Calcula ajustes de probabilidad por estrategia, par, tendencia EMA y nivel
     - Aplica esos ajustes antes del umbral en la próxima señal
     - /aprendizaje muestra el resumen completo del aprendizaje
  ⑥ Nuevos formatos de mensajes Telegram (v30b):
     - Links clickeables en texto (sin botones InlineKeyboard)
     - Solo USD en monto de apuesta
     - Nuevos formatos WIN y LOSS
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
from typing import Optional, Dict, Tuple, List

import numpy as np
from sklearn.naive_bayes import MultinomialNB
from sklearn.linear_model import SGDClassifier

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import aiohttp
from flask import Flask, jsonify
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [RussianDC] %(levelname)s %(message)s'
)
logger = logging.getLogger("RussianDC")
for _ln in ['werkzeug', 'flask.app', 'flask', 'urllib3']:
    logging.getLogger(_ln).setLevel(logging.ERROR)

# ─── CREDENCIALES ─────────────────────────────────────────────────────────────
TOKEN   = "8615799238:AAG2kLg-Ostc4Y4E98HXDIoje_U4F7oqdzU"
CHAT_ID = -1003821352139

# ─── URL RULETA ───────────────────────────────────────────────────────────────
RUSSIAN_URL = "https://stake1039.com/es/casino/games/pragmatic-play-russian-roulette"

def russian_links_html() -> str:
    """Links de casas de apuesta como HTML clickeable dentro del mensaje."""
    return (
        '<a href="https://melbet-ar3.com/es/casino-search?game=88366">🟡 Melbet</a> '
        '<a href="https://betwinner-06870.pro/mx/casino-search?game=88366">🟢 Betwwiner</a>\n'
        '<a href="https://play-888starz.com/es/casino-search?game=88366">🔴 888Starz</a> '
        '<a href="https://1xbetarge.com/es/casino-search?game=93659">⚪ 1xbet</a> '
        '<a href="https://1win.lat/casino/play/v_pragmatic:1winroulette">🔵 1win</a>'
    )

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
_session = requests.Session()
_retry = Retry(
    total=5, backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"], raise_on_status=False
)
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))
_session.mount("http://",  HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))

try:
    bot = telebot.TeleBot(TOKEN, threaded=False)
    bot.session = _session
    logger.info("✅ Telegram bot initialized")
except Exception as e:
    logger.error(f"❌ Failed to initialize Telegram bot: {e}")
    exit(1)

# ─── CONSTANTES ───────────────────────────────────────────────────────────────
STATS_URL       = "https://ruletasbot-rjce.onrender.com"
TARGET_ROULETTE = "RUSSIAN"
POLL_INTERVAL   = 1
LIVE_DB         = "russian_live.db"

BASE_BET        = 0.10      # USD — ficha base (v29)
MAX_NIVEL       = 3
WARMUP_SPINS    = 25
MIN_PROB        = 0.78      # umbral base E1 y E3
MAX_INTENTOS    = 2
TRAIN_INTERVAL  = 100

# Pesos PHF = PHTML(60%) + PH(40%)
PHTML_W         = 0.60
PH_W_COMBINE    = 0.40

# Pesos señal E1
PF_W_NORM  = 0.70; PH_W_NORM  = 0.30
BASE_W_NORM= 0.55; ML_W_NORM  = 0.45

# Estrategias
STRAT_E1 = 1
STRAT_E2 = 2
STRAT_E3 = 3

# ─── FICHAS POR MONEDA (v29) ──────────────────────────────────────────────────
CURRENCY_CHIPS: Dict[str, float] = {
    "USD": 0.10, "MXN": 2.00, "PEN": 0.40,
    "COP": 500.0, "ARS": 200.0, "CLP": 50.0
}
CURRENCY_SYMBOLS   = {"USD":"$","MXN":"$","PEN":"S/.","COP":"$","ARS":"$","CLP":"$"}
CURRENCY_FLAGS     = {"USD":"🇺🇲","MXN":"🇲🇽","PEN":"🇵🇪","COP":"🇨🇴","ARS":"🇦🇷","CLP":"🇨🇱"}
CURRENCY_DECIMALS  = {"USD":2,"MXN":2,"PEN":2,"COP":0,"ARS":0,"CLP":0}
CURRENCY_MULTIPLIERS = {k: v / BASE_BET for k, v in CURRENCY_CHIPS.items()}

REAL_COLOR_MAP: Dict[int, str] = {
    0:"VERDE",1:"ROJO",2:"NEGRO",3:"ROJO",4:"NEGRO",5:"ROJO",6:"NEGRO",
    7:"ROJO",8:"NEGRO",9:"ROJO",10:"NEGRO",11:"NEGRO",12:"ROJO",13:"NEGRO",
    14:"ROJO",15:"NEGRO",16:"ROJO",17:"NEGRO",18:"ROJO",19:"ROJO",20:"NEGRO",
    21:"ROJO",22:"NEGRO",23:"ROJO",24:"NEGRO",25:"ROJO",26:"NEGRO",27:"ROJO",
    28:"NEGRO",29:"NEGRO",30:"ROJO",31:"NEGRO",32:"ROJO",33:"NEGRO",34:"ROJO",
    35:"NEGRO",36:"ROJO",
}

# ─── TABLA PHTML ──────────────────────────────────────────────────────────────
DOZEN_TABLE: Dict[int, Dict[str, int]] = {
    0:  {"d1": 32, "d2": 32, "d3": 32},
    1:  {"d1": 28, "d2": 32, "d3": 36},
    2:  {"d1": 36, "d2": 28, "d3": 32},
    3:  {"d1": 24, "d2": 32, "d3": 36},
    4:  {"d1": 32, "d2": 40, "d3": 24},
    5:  {"d1": 40, "d2": 24, "d3": 36},
    6:  {"d1": 32, "d2": 24, "d3": 40},
    7:  {"d1": 36, "d2": 24, "d3": 40},
    8:  {"d1": 32, "d2": 36, "d3": 28},
    9:  {"d1": 28, "d2": 36, "d3": 32},
    10: {"d1": 40, "d2": 32, "d3": 28},
    11: {"d1": 36, "d2": 24, "d3": 36},
    12: {"d1": 32, "d2": 28, "d3": 36},
    13: {"d1": 32, "d2": 28, "d3": 36},
    14: {"d1": 16, "d2": 48, "d3": 32},
    15: {"d1": 36, "d2": 28, "d3": 32},
    16: {"d1": 28, "d2": 32, "d3": 36},
    17: {"d1": 20, "d2": 44, "d3": 32},
    18: {"d1": 32, "d2": 28, "d3": 36},
    19: {"d1": 36, "d2": 28, "d3": 32},
    20: {"d1": 36, "d2": 36, "d3": 28},
    21: {"d1": 24, "d2": 44, "d3": 28},
    22: {"d1": 36, "d2": 36, "d3": 28},
    23: {"d1": 24, "d2": 32, "d3": 40},
    24: {"d1": 44, "d2": 32, "d3": 24},
    25: {"d1": 36, "d2": 24, "d3": 36},
    26: {"d1": 40, "d2": 28, "d3": 32},
    27: {"d1": 32, "d2": 28, "d3": 36},
    28: {"d1": 36, "d2": 28, "d3": 32},
    29: {"d1": 32, "d2": 24, "d3": 40},
    30: {"d1": 36, "d2": 36, "d3": 28},
    31: {"d1": 32, "d2": 36, "d3": 24},
    32: {"d1": 32, "d2": 36, "d3": 28},
    33: {"d1": 28, "d2": 32, "d3": 36},
    34: {"d1": 36, "d2": 28, "d3": 32},
    35: {"d1": 36, "d2": 32, "d3": 24},
    36: {"d1": 28, "d2": 36, "d3": 32},
}

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def get_dozen(n: int) -> int:
    if n == 0: return 0
    return (n - 1) // 12 + 1

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(LIVE_DB, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS live_spins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number INTEGER NOT NULL,
        ts INTEGER NOT NULL
    )""")
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
                try:
                    wait = int(''.join(filter(str.isdigit, err))) + 1
                except:
                    wait = 30
                logger.warning(f"⏳ Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            if attempt == _TG_RETRIES:
                logger.error(f"❌ TG call failed after {_TG_RETRIES} attempts: {err}")
                return None
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return None

def tg_send(text: str, markup: InlineKeyboardMarkup = None) -> Optional[int]:
    if not text:
        return None
    try:
        msg = _tg_call(
            bot.send_message,
            chat_id=CHAT_ID, text=text,
            parse_mode="HTML", reply_markup=markup,
        )
        if msg:
            logger.info(f"✅ Message sent (ID: {msg.message_id})")
            return msg.message_id
        return None
    except Exception as e:
        logger.error(f"❌ Exception in tg_send: {e}")
        return None

def tg_delete(chat_id: int, message_id: int):
    try:
        _tg_call(bot.delete_message, chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"⚠️ Failed to delete message: {e}")

# ─── CLIENTE STATS ─────────────────────────────────────────────────────────────
class StatsClient:
    def __init__(self):
        self.stats_dozen  = {}
        self.last_20      = []
        self.total_spins  = 0
        self.connected    = False
        self.poll_count   = 0
        self.last_poll_ok = 0.0
        self.last_error   = None

    def update(self, data: dict):
        try:
            self.last_20      = data.get("last_20",     self.last_20)
            self.stats_dozen  = data.get("stats_dozen", self.stats_dozen)
            self.total_spins  = data.get("total_spins", self.total_spins)
            self.connected    = True
            self.poll_count  += 1
            self.last_poll_ok = time.time()
            self.last_error   = None
        except Exception as e:
            self.last_error = str(e)

    def get_ph_probs_raw(self, number: int) -> Optional[Dict]:
        num_key = str(number)
        if num_key not in self.stats_dozen:
            return None
        data = self.stats_dozen[num_key]
        if data.get("total", 0) < 10:
            return None
        return {
            1: data.get("1", 0) / 100.0,
            2: data.get("2", 0) / 100.0,
            3: data.get("3", 0) / 100.0,
        }

    def get_ph_pair(self, number: int) -> Optional[Dict]:
        probs = self.get_ph_probs_raw(number)
        if probs is None:
            return None
        sorted_p = sorted(probs.items(), key=lambda x: x[1], reverse=True)
        if sorted_p[0][1] == 0:
            return None
        pair    = tuple(sorted([sorted_p[0][0], sorted_p[1][0]]))
        missing = list({1, 2, 3} - set(pair))[0]
        prob    = sorted_p[0][1] + sorted_p[1][1]
        return {"pair": pair, "missing": missing, "prob": prob}

# ─── EMA ──────────────────────────────────────────────────────────────────────
def calc_ema(data, period):
    if len(data) < period:
        return [None] * len(data)
    mult = 2 / (period + 1)
    out  = [None] * (period - 1)
    prev = sum(data[:period]) / period
    out.append(prev)
    for v in data[period:]:
        prev = v * mult + prev * (1 - mult)
        out.append(prev)
    return out

def ema_signal(levels, mode="moderado"):
    if len(levels) < 20:
        return False
    e4, e8, e20 = calc_ema(levels, 4), calc_ema(levels, 8), calc_ema(levels, 20)
    li = len(levels) - 1
    if any(v is None for v in [e4[li], e8[li], e20[li]]):
        return False
    cur  = levels[li]
    ce4, ce8, ce20 = e4[li], e8[li], e20[li]
    pe4  = e4[li-1]  if li > 0 and e4[li-1]  is not None else ce4
    pe8  = e8[li-1]  if li > 0 and e8[li-1]  is not None else ce8
    pe20 = e20[li-1] if li > 0 and e20[li-1] is not None else ce20
    if mode == "tendencia":
        return (pe4 <= pe20 and ce4 > ce20) or (cur > ce4 and cur > ce8 and cur > ce20)
    else:
        vp = False
        if len(levels) >= 3:
            a, b, c = levels[-3], levels[-2], levels[-1]
            vp = (b < a) and (b < c) and (c > a)
        return (pe4 <= pe8 and ce4 > ce8) or (pe8 <= pe20 and ce8 > ce20) or \
               (cur > ce4 and cur > ce8) or vp

def ema_trend_str(levels) -> str:
    if len(levels) < 20:
        return "neutral"
    e4  = calc_ema(levels, 4)
    e8  = calc_ema(levels, 8)
    e20 = calc_ema(levels, 20)
    li  = len(levels) - 1
    v4, v8, v20 = e4[li], e8[li], e20[li]
    if any(v is None for v in [v4, v8, v20]):
        return "neutral"
    cur = levels[li]
    if cur > v4 and v4 > v8 and v8 > v20:
        return "bull"
    if cur < v4 and v4 < v8 and v8 < v20:
        return "bear"
    return "neutral"

def ema_trend_pair(trend: str) -> Dict:
    if trend == "bull":
        return {"pair": (1, 2), "missing": 3, "label": "ALCISTA"}
    if trend == "bear":
        return {"pair": (2, 3), "missing": 1, "label": "BAJISTA"}
    return {"pair": (1, 3), "missing": 2, "label": "NEUTRAL"}

# ─── MARKOV ───────────────────────────────────────────────────────────────────
class SmoothedMarkovPredictor:
    def __init__(self, window=60, order=2):
        self.window            = window
        self.order             = order
        self.transition_counts = {}

    def update(self, sequence):
        self.transition_counts = defaultdict(lambda: defaultdict(int))
        recent = sequence[-self.window:]
        if len(recent) < self.order + 1:
            return
        for i in range(len(recent) - self.order):
            self.transition_counts[tuple(recent[i:i+self.order])][recent[i+self.order]] += 1

    def predict(self, sequence):
        if len(sequence) < self.order:
            return None
        counts = dict(self.transition_counts.get(tuple(sequence[-self.order:]), {}))
        total  = sum(counts.values())
        if total < 10:
            return None
        alpha = 2.0; vs = 3
        probs = {k: (v + alpha) / (total + alpha * vs) for k, v in counts.items()}
        for c in [1, 2, 3]:
            if c not in probs:
                probs[c] = alpha / (total + alpha * vs)
        return probs

# ─── ENSEMBLE ML ──────────────────────────────────────────────────────────────
class OnlineEnsemblePredictor:
    WINDOW  = 5
    CLASSES = [1, 2, 3]

    def __init__(self):
        self.mnb     = MultinomialNB(alpha=2.0, class_prior=[0.333, 0.333, 0.333])
        self.sgd     = SGDClassifier(
            loss='log_loss', learning_rate='adaptive', eta0=0.005,
            penalty='l2', alpha=0.01, epsilon=0.2
        )
        self.trained = False

    def _extract_features(self, hist_d, pf_pd, ph_pd):
        if len(hist_d) < self.WINDOW:
            return None
        features = []
        for i in range(1, self.WINDOW + 1):
            d   = hist_d[-i]
            vec = [0, 0, 0]
            vec[d - 1] = 1
            features.extend(vec)
        for pair in (pf_pd, ph_pd):
            vec = [0, 0, 0]
            for x in pair:
                vec[x - 1] = 1
            features.extend(vec)
        return features

    def partial_train(self, hist_d, target, pf_d, ph_d):
        feats = self._extract_features(hist_d[:-1], pf_d, ph_d)
        if feats is None:
            return
        X = np.array(feats).reshape(1, -1)
        y = np.array([target])
        if not self.trained:
            self.mnb.partial_fit(X, y, classes=self.CLASSES)
            self.sgd.partial_fit(X, y, classes=self.CLASSES)
            self.trained = True
        else:
            self.mnb.partial_fit(X, y)
            self.sgd.partial_fit(X, y)

    def predict(self, hist_d, pf_d, ph_d):
        if not self.trained:
            return None
        feats = self._extract_features(hist_d, pf_d, ph_d)
        if feats is None:
            return None
        X = np.array(feats).reshape(1, -1)
        try:
            pm = dict(zip(self.CLASSES, self.mnb.predict_proba(X)[0]))
            ps = dict(zip(self.CLASSES, self.sgd.predict_proba(X)[0]))
            return {c: 0.5 * pm[c] + 0.5 * ps[c] for c in self.CLASSES}
        except Exception:
            return None

# ─── GESTOR DE FICHAS Y NIVELES ───────────────────────────────────────────────
class GestorDocenas:
    """
    Niveles de apuesta (3 niveles, 2 intentos):
      Nivel 1: Int.1 = $0.10 | Int.2 = $0.30
      Nivel 2: Int.1 = $0.90 | Int.2 = $2.70
      Nivel 3: Int.1 = $8.10 | Int.2 = $24.30
    Fórmula: Int.1 = BASE_BET × 9^(nivel-1) | Int.2 = 3 × BASE_BET × 9^(nivel-1)
    nivel sube (1→3) al perder los intentos; baja a 1 al recuperar (debt_stack).
    """
    def __init__(self):
        self.nivel      = 1
        self.b0         = 0.0
        self.debt_stack = []

    def iniciar_senal(self, balance: float):
        self.b0 = balance

    def get_bet(self, intento: int = 1) -> float:
        multiplier = 9 ** (self.nivel - 1)
        if intento == 1:
            return round(BASE_BET * multiplier, 2)
        return round(3 * BASE_BET * multiplier, 2)

    def registrar_perdida_senal(self):
        self.debt_stack.append(self.b0)
        self.nivel = self.nivel + 1 if self.nivel < MAX_NIVEL else 1
        logger.info(
            f"[RussianDC] 📋 Deuda | B0={self.b0:.2f} | "
            f"Pila={len(self.debt_stack)} | Nivel→{self.nivel}"
        )

    def verificar_recuperacion(self, balance: float):
        while self.debt_stack:
            if balance >= self.debt_stack[-1] + BASE_BET * 0.9:
                self.debt_stack.pop()
            else:
                break
        if not self.debt_stack:
            self.nivel = 1

# ─── SIGNAL LEARNER ───────────────────────────────────────────────────────────
class SignalLearner:
    """
    Aprende de cada señal enviada para mejorar las decisiones futuras.

    REGISTRA por cada señal:
      strategy, pair, missing, prob calculada, intento, nivel,
      pf_prob, phf_prob, ema_trend, last_number, últimas 5 docenas

    REGISTRA el resultado:
      result (WIN/LOSS), intento_fin, reason (descripción del fallo/acierto)

    CALCULA ajustes de probabilidad (rango ≈ ±0.22) aplicados ANTES
    del umbral MIN_PROB, considerando:
      · Tasa de acierto reciente de la estrategia       (±0.08)
      · Tasa de acierto del par específico              (±0.06)
      · Tasa de acierto según tendencia EMA             (±0.05)
      · Tasa de acierto según nivel de apuesta actual   (±0.04)

    Todos los datos se guardan en la tabla signal_log de la DB SQLite
    (persistente entre reinicios del bot).
    """

    MAX_HISTORY = 500
    WINDOW      = 50
    MIN_SAMPLES = 5

    _COLS = [
        "id", "ts", "strategy", "pair", "missing", "prob",
        "intento_start", "nivel", "pf_prob", "phf_prob",
        "ema_trend", "last_number", "dozen_seq_5",
        "result", "intento_fin", "reason"
    ]

    def __init__(self, db: sqlite3.Connection):
        self.db         = db
        self.history    = deque(maxlen=self.MAX_HISTORY)
        self.pending_id: Optional[int] = None
        self._init_db()
        self._load_history()

    # ── Persistencia ─────────────────────────────────────────────────────────
    def _init_db(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS signal_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL    NOT NULL,
                strategy     INTEGER,
                pair         TEXT,
                missing      INTEGER,
                prob         REAL,
                intento_start INTEGER,
                nivel        INTEGER,
                pf_prob      REAL,
                phf_prob     REAL,
                ema_trend    TEXT,
                last_number  INTEGER,
                dozen_seq_5  TEXT,
                result       TEXT,
                intento_fin  INTEGER,
                reason       TEXT
            )
        """)
        self.db.commit()

    def _row_to_dict(self, row) -> dict:
        d = dict(zip(self._COLS, row))
        raw = d.get("pair", "")
        try:
            d["pair"] = tuple(int(x) for x in raw.split(",") if x)
        except Exception:
            d["pair"] = ()
        return d

    def _load_history(self):
        try:
            rows = self.db.execute(
                "SELECT * FROM signal_log WHERE result IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (self.MAX_HISTORY,)
            ).fetchall()
            for row in reversed(rows):
                self.history.append(self._row_to_dict(row))
            logger.info(
                f"[Learner] 📚 Historial cargado: {len(self.history)} señales previas"
            )
        except Exception as e:
            logger.warning(f"[Learner] ⚠️ Error cargando historial: {e}")

    # ── Registro de señal ─────────────────────────────────────────────────────
    def register_signal(self, strategy: int, pair: tuple, missing: int,
                        prob: float, nivel: int, pf_prob: float,
                        phf_prob: float, ema_trend: str,
                        last_number: int, dozen_seq_5: list):
        try:
            cur = self.db.execute(
                """INSERT INTO signal_log
                   (ts, strategy, pair, missing, prob, intento_start, nivel,
                    pf_prob, phf_prob, ema_trend, last_number, dozen_seq_5)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), strategy,
                    ",".join(str(x) for x in sorted(pair)),
                    missing, round(prob, 6), 1, nivel,
                    round(pf_prob, 6), round(phf_prob, 6),
                    ema_trend, last_number,
                    ",".join(str(x) for x in dozen_seq_5)
                )
            )
            self.db.commit()
            self.pending_id = cur.lastrowid
            logger.debug(f"[Learner] 📝 Señal registrada ID={self.pending_id} "
                         f"strat={strategy} par={pair} prob={prob:.0%} EMA={ema_trend}")
        except Exception as e:
            logger.warning(f"[Learner] ⚠️ Error registrando señal: {e}")

    def resolve(self, result: str, intento_fin: int, reason: str = ""):
        if self.pending_id is None:
            return
        try:
            self.db.execute(
                "UPDATE signal_log SET result=?, intento_fin=?, reason=? WHERE id=?",
                (result, intento_fin, reason, self.pending_id)
            )
            self.db.commit()
            row = self.db.execute(
                "SELECT * FROM signal_log WHERE id=?", (self.pending_id,)
            ).fetchone()
            if row:
                self.history.append(self._row_to_dict(row))
            logger.info(
                f"[Learner] {'✅' if result=='WIN' else '❌'} "
                f"ID={self.pending_id} → {result} Int.{intento_fin} | {reason}"
            )
        except Exception as e:
            logger.warning(f"[Learner] ⚠️ Error resolviendo señal: {e}")
        finally:
            self.pending_id = None

    # ── Cálculo de ajustes ────────────────────────────────────────────────────
    def _recent(self) -> List[dict]:
        completed = [s for s in self.history if s.get("result") in ("WIN", "LOSS")]
        return list(completed)[-self.WINDOW:]

    def _win_rate(self, subset: list) -> Optional[float]:
        if len(subset) < self.MIN_SAMPLES:
            return None
        return sum(1 for s in subset if s["result"] == "WIN") / len(subset)

    @staticmethod
    def _adj(win_rate: Optional[float], scale: float) -> float:
        if win_rate is None:
            return 0.0
        return round((win_rate - 0.5) * 2.0 * scale, 4)

    def strat_adjustment(self, strategy: int) -> float:
        subset = [s for s in self._recent() if s.get("strategy") == strategy]
        return self._adj(self._win_rate(subset), 0.08)

    def pair_adjustment(self, pair: tuple) -> float:
        key = tuple(sorted(pair))
        subset = [s for s in self._recent() if tuple(sorted(s.get("pair", ()))) == key]
        return self._adj(self._win_rate(subset), 0.06)

    def trend_adjustment(self, ema_trend: str) -> float:
        subset = [s for s in self._recent() if s.get("ema_trend") == ema_trend]
        return self._adj(self._win_rate(subset), 0.05)

    def nivel_adjustment(self, nivel: int) -> float:
        subset = [s for s in self._recent() if s.get("nivel") == nivel]
        return self._adj(self._win_rate(subset), 0.04)

    def get_adjustment(self, strategy: int, pair: tuple,
                       ema_trend: str, nivel: int) -> Tuple[float, str]:
        s_adj = self.strat_adjustment(strategy)
        p_adj = self.pair_adjustment(pair)
        t_adj = self.trend_adjustment(ema_trend)
        n_adj = self.nivel_adjustment(nivel)
        total = round(max(-0.23, min(0.23, s_adj + p_adj + t_adj + n_adj)), 4)

        parts = []
        if abs(s_adj) >= 0.005: parts.append(f"Strat:{s_adj:+.3f}")
        if abs(p_adj) >= 0.005: parts.append(f"Par:{p_adj:+.3f}")
        if abs(t_adj) >= 0.005: parts.append(f"EMA:{t_adj:+.3f}")
        if abs(n_adj) >= 0.005: parts.append(f"Nv:{n_adj:+.3f}")
        detail = " | ".join(parts) if parts else "sin ajuste aún"
        return total, detail

    # ── Resumen de aprendizaje ────────────────────────────────────────────────
    def get_summary(self, n: int = 30) -> str:
        recent = self._recent()[-n:]
        total_db = 0
        try:
            row = self.db.execute(
                "SELECT COUNT(*) FROM signal_log WHERE result IS NOT NULL"
            ).fetchone()
            total_db = row[0] if row else 0
        except Exception:
            pass

        if not recent:
            return (
                "🧠 <b>Aprendizaje adaptativo activo</b>\n\n"
                "Aún no hay señales resueltas.\n"
                "Los ajustes se activarán automáticamente\n"
                f"tras {self.MIN_SAMPLES} señales por categoría."
            )

        total = len(recent)
        wins  = sum(1 for s in recent if s["result"] == "WIN")
        eff   = wins / total * 100 if total > 0 else 0

        si = {STRAT_E1: "🅐E1", STRAT_E2: "🅑E2", STRAT_E3: "🅒E3"}
        lines = [
            f"🧠 <b>APRENDIZAJE ADAPTATIVO</b>",
            f"Base de datos: {total_db} señales totales",
            f"Ventana activa: {total} últimas | Aciertos: {wins}/{total} ({eff:.1f}%)\n",
            "<b>📊 Por estrategia:</b>"
        ]

        for st in [STRAT_E1, STRAT_E2, STRAT_E3]:
            sb  = [s for s in recent if s.get("strategy") == st]
            if not sb:
                lines.append(f"  {si[st]}: sin datos")
                continue
            sw  = sum(1 for s in sb if s["result"] == "WIN")
            wr  = sw / len(sb) * 100
            adj = self.strat_adjustment(st)
            bar = "▓" * int(wr / 10) + "░" * (10 - int(wr / 10))
            lines.append(
                f"  {si[st]}: {sw}/{len(sb)} ({wr:.0f}%) {bar} adj:{adj:+.3f}"
            )

        pair_stats: Dict[str, list] = defaultdict(lambda: [0, 0])
        for s in recent:
            p = s.get("pair", ())
            if len(p) == 2:
                key = f"D{p[0]}+D{p[1]}"
                pair_stats[key][1] += 1
                if s["result"] == "WIN":
                    pair_stats[key][0] += 1
        if pair_stats:
            lines.append("\n<b>🎯 Por par de docenas:</b>")
            for pk in sorted(pair_stats):
                pw, pt = pair_stats[pk]
                wr_p   = pw / pt * 100 if pt else 0
                nums   = [int(x[1]) for x in pk.split("+")]
                adj    = self.pair_adjustment(tuple(nums))
                lines.append(f"  {pk}: {pw}/{pt} ({wr_p:.0f}%) adj:{adj:+.3f}")

        trend_stats: Dict[str, list] = defaultdict(lambda: [0, 0])
        for s in recent:
            t = s.get("ema_trend", "neutral")
            trend_stats[t][1] += 1
            if s["result"] == "WIN":
                trend_stats[t][0] += 1
        if trend_stats:
            lines.append("\n<b>📈 Por tendencia EMA:</b>")
            trend_labels = {"bull": "🟢 Bull", "neutral": "⬜ Neutral", "bear": "🔴 Bear"}
            for t in ["bull", "neutral", "bear"]:
                if t not in trend_stats:
                    continue
                tw, tt = trend_stats[t]
                wr_t   = tw / tt * 100 if tt else 0
                adj    = self.trend_adjustment(t)
                lines.append(
                    f"  {trend_labels[t]}: {tw}/{tt} ({wr_t:.0f}%) adj:{adj:+.3f}"
                )

        nivel_stats: Dict[int, list] = defaultdict(lambda: [0, 0])
        for s in recent:
            nv = s.get("nivel", 1)
            nivel_stats[nv][1] += 1
            if s["result"] == "WIN":
                nivel_stats[nv][0] += 1
        if nivel_stats:
            lines.append("\n<b>🎚 Por nivel de apuesta:</b>")
            for nv in sorted(nivel_stats):
                nw, nt = nivel_stats[nv]
                wr_n   = nw / nt * 100 if nt else 0
                adj    = self.nivel_adjustment(nv)
                lines.append(f"  Nv.{nv}: {nw}/{nt} ({wr_n:.0f}%) adj:{adj:+.3f}")

        lines.append("\n<b>🕐 Últimas señales registradas:</b>")
        for s in list(recent)[-6:]:
            icon  = "✅" if s["result"] == "WIN" else "❌"
            strat = si.get(s.get("strategy"), "?")
            p     = s.get("pair", ())
            pr    = f"D{p[0]}+D{p[1]}" if len(p) == 2 else "?"
            fin   = s.get("intento_fin", "?")
            prob  = s.get("prob", 0)
            rsn   = s.get("reason", "")
            lines.append(f"{icon} {strat} {pr} Int.{fin} ({prob:.0%}) — {rsn}")

        return "\n".join(lines)

# ─── STATS ────────────────────────────────────────────────────────────────────
class DetailedStats:
    def __init__(self):
        self.wins              = 0
        self.losses            = 0
        self.consecutive       = 0
        self.last_20           = deque(maxlen=20)
        self.signals_processed = 0
        self.last_report_sigs  = 0

    def record(self, result_type: str, intento: int, number: int,
               val: int, bankroll: float, strat: int):
        self.signals_processed += 1
        if result_type == 'WIN':
            self.wins        += 1
            self.consecutive += 1
        else:
            self.losses      += 1
            self.consecutive  = 0
        self.last_20.append({
            "result": result_type, "intento": intento, "number": number,
            "val": val, "balance": bankroll, "strat": strat
        })

    def should_send(self):
        return (self.signals_processed - self.last_report_sigs) >= 20

    def mark_sent(self):
        self.last_report_sigs = self.signals_processed

    def get_stats_text(self, bankroll: float) -> str:
        total = self.wins + self.losses
        eff   = (self.wins / total * 100) if total > 0 else 0.0
        text  = (
            f"📊 RESUMEN 📊\n"
            f"► ✅{self.wins} | 🚫{self.losses}\n"
            f"► Consecutivas = {self.consecutive}\n"
            f"► Assert = {eff:.2f}%\n"
            f"► Balance: 💰 ${bankroll:.2f} USD\n"
            f"► Total señales: {total}\n\n"
            f"📌 Últimas 20 📌\n"
        )
        _si = {1: "🅐", 2: "🅑", 3: "🅒"}
        for s in reversed(list(self.last_20)):
            si   = _si.get(s['strat'], "?")
            opp  = f"Int.{s['intento']}"
            b    = f"💰${s['balance']:.2f}"
            v    = f"D{s['val']}"
            r    = s['result']
            icon = "✅" if r == 'WIN' else "🚫"
            text += f"{icon} WIN #{s['number']} {v} {si} | {opp} | {b}\n" \
                    if r == 'WIN' else \
                    f"🚫 LOSS #{s['number']} {v} {si} | {opp} | {b}\n"
        return text

# ─── ENGINE ───────────────────────────────────────────────────────────────────
class RussianRouletteEngine:
    def __init__(self, stats_client: StatsClient):
        self.stats_client = stats_client

        # Historial
        self.spin_history       = []
        self.dozen_seq          = []
        self.d_levels           = {1: [], 2: [], 3: []}
        self.doc_levels         = []
        self._last_doc_inc      = 0

        # After-number local
        self.after_number_dozen = defaultdict(lambda: defaultdict(int))

        # ML
        self.markov_d           = SmoothedMarkovPredictor()
        self.ensemble_d         = OnlineEnsemblePredictor()
        self.spins_since_train  = 0

        # Señal activa
        self.signal_active        = False
        self.active_strategy      = None
        self.active_pair          = ()
        self.active_missing       = 0
        self.active_intento       = 1
        self.total_signal_loss    = 0.0
        self.active_signal_msg_id = None

        # Gestión de fichas y niveles
        self.gestor    = GestorDocenas()
        self.bankroll  = 100.0

        # Stats y DB
        self.stats     = DetailedStats()
        self._db       = _get_db()

        # Aprendizaje adaptativo
        self.learner   = SignalLearner(self._db)

        self.processed_game_ids: set = set()
        self.MAX_PROCESSED_IDS    = 300

        # Contador de señales para numeración WIN/LOSS
        self.signal_counter       = 0

        # Warmup
        live_loaded      = self._load_live_history()
        self.ws_count    = live_loaded
        self.warmup_done = live_loaded >= WARMUP_SPINS
        logger.info(
            f"[RussianDC] 📦 Pre-cargados: {live_loaded} | "
            f"Warmup: {'✅' if self.warmup_done else '⏳'} | "
            f"Historial learner: {len(self.learner.history)} señales"
        )

    # ── DB ────────────────────────────────────────────────────────────────────
    def _load_live_history(self) -> int:
        try:
            rows = self._db.execute(
                "SELECT number FROM live_spins ORDER BY id ASC"
            ).fetchall()
        except:
            return 0
        for (n,) in rows:
            self._update_state(n, persist=False, train_model=False)
        if rows:
            self.markov_d.update(self.dozen_seq)
        return len(rows)

    def _persist(self, number: int):
        try:
            self._db.execute(
                "INSERT INTO live_spins(number,ts) VALUES(?,?)",
                (number, int(time.time()))
            )
            self._db.commit()
        except Exception as e:
            logger.debug(f"⚠️ DB persist error: {e}")

    # ── Estado interno ────────────────────────────────────────────────────────
    def _update_state(self, number: int, persist=True, train_model=True):
        d = get_dozen(number)

        if number != 0 and self.spin_history:
            prev = self.spin_history[-1]["number"]
            if prev != 0:
                self.after_number_dozen[prev][d] += 1

        self.spin_history.append({"number": number})

        if d != 0:
            for dd in (1, 2, 3):
                prev = self.d_levels[dd][-1] if self.d_levels[dd] else 0
                self.d_levels[dd].append(prev + (1 if d == dd else -1))
                if len(self.d_levels[dd]) > 300:
                    self.d_levels[dd].pop(0)
            self.dozen_seq.append(d)
            if len(self.dozen_seq) > 200:
                self.dozen_seq.pop(0)
            if train_model and len(self.dozen_seq) > 5:
                pf_d = self._get_pf()
                ph_d = self._get_ph(number)
                if pf_d and ph_d:
                    self.ensemble_d.partial_train(
                        self.dozen_seq, d, pf_d["pair"], ph_d["pair"]
                    )
                self.spins_since_train += 1
                if self.spins_since_train >= TRAIN_INTERVAL:
                    self.markov_d.update(self.dozen_seq)
                    self.spins_since_train = 0

        if number != 0:
            inc = 1 if d == 1 else (-1 if d == 3 else (1 if number <= 18 else -1))
            self._last_doc_inc = inc
        else:
            inc = self._last_doc_inc

        prev_lvl = self.doc_levels[-1] if self.doc_levels else 0
        self.doc_levels.append(prev_lvl + inc)
        if len(self.doc_levels) > 300:
            self.doc_levels.pop(0)

        if persist:
            self._persist(number)

    # ── PF ────────────────────────────────────────────────────────────────────
    def _get_pf(self) -> Optional[Dict]:
        if len(self.spin_history) < 5:
            return None
        counts = {1: 0, 2: 0, 3: 0}
        for s in self.spin_history[-5:]:
            n = s["number"]
            if n != 0:
                counts[get_dozen(n)] += 1
        active = [k for k, v in counts.items() if v > 0]
        if len(active) != 2:
            return None
        pair    = tuple(sorted(active))
        missing = list({1, 2, 3} - set(pair))[0]
        return {"pair": pair, "missing": missing,
                "prob": sum(counts[a] for a in pair) / 5.0}

    # ── PH ────────────────────────────────────────────────────────────────────
    def _get_ph(self, number: Optional[int] = None) -> Optional[Dict]:
        if number is None:
            if not self.spin_history:
                return None
            number = self.spin_history[-1]["number"]
        if number == 0:
            return None
        server_ph = self.stats_client.get_ph_pair(number)
        if server_ph:
            return server_ph
        counts = self.after_number_dozen.get(number, {})
        total  = sum(counts.values())
        if total < 10:
            return None
        sc = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        if len(sc) < 2:
            return None
        pair    = tuple(sorted([sc[0][0], sc[1][0]]))
        missing = list({1, 2, 3} - set(pair))[0]
        return {"pair": pair, "missing": missing,
                "prob": (sc[0][1] + sc[1][1]) / total}

    # ── PHTML ─────────────────────────────────────────────────────────────────
    def _get_phtml_probs(self, number: int) -> Optional[Dict]:
        if number == 0:
            return None
        entry = DOZEN_TABLE.get(number)
        if not entry:
            return None
        d1, d2, d3 = entry["d1"], entry["d2"], entry["d3"]
        total = d1 + d2 + d3
        if total == 0:
            return None
        return {1: d1 / total, 2: d2 / total, 3: d3 / total}

    def _get_phtml_pair(self, number: int) -> Optional[Dict]:
        probs = self._get_phtml_probs(number)
        if probs is None:
            return None
        sorted_d = sorted(probs.items(), key=lambda x: x[1], reverse=True)
        pair     = tuple(sorted([sorted_d[0][0], sorted_d[1][0]]))
        missing  = list({1, 2, 3} - set(pair))[0]
        prob     = probs[sorted_d[0][0]] + probs[sorted_d[1][0]]
        return {"pair": pair, "missing": missing, "prob": prob}

    # ── PHF ───────────────────────────────────────────────────────────────────
    def _get_phf(self, number: int) -> Optional[Dict]:
        if number == 0:
            return None
        phtml = self._get_phtml_probs(number)
        if phtml is None:
            return None
        ph = self.stats_client.get_ph_probs_raw(number)
        if ph is None:
            counts = self.after_number_dozen.get(number, {})
            total  = sum(counts.values())
            if total >= 10:
                ph = {1: counts.get(1,0)/total, 2: counts.get(2,0)/total, 3: counts.get(3,0)/total}

        if ph is not None:
            phf_raw = {d: PHTML_W * phtml[d] + PH_W_COMBINE * ph[d] for d in [1, 2, 3]}
        else:
            phf_raw = dict(phtml)

        total = sum(phf_raw.values())
        if total == 0:
            return None
        phf = {d: v / total for d, v in phf_raw.items()}

        sorted_d = sorted(phf.items(), key=lambda x: x[1], reverse=True)
        pair     = tuple(sorted([sorted_d[0][0], sorted_d[1][0]]))
        missing  = list({1, 2, 3} - set(pair))[0]
        prob     = phf[sorted_d[0][0]] + phf[sorted_d[1][0]]
        return {"pair": pair, "missing": missing, "prob": prob, "probs": phf}

    # ── ML ────────────────────────────────────────────────────────────────────
    def _predict_pair_ml(self, missing_num: int) -> float:
        mk_pred    = self.markov_d.predict(self.dozen_seq)
        m_p_miss   = mk_pred.get(missing_num, 1/3) if mk_pred else 1/3
        pf_d       = self._get_pf()
        ph_d       = self._get_ph()
        ens_p_miss = 1/3
        if pf_d and ph_d:
            ens = self.ensemble_d.predict(self.dozen_seq, pf_d["pair"], ph_d["pair"])
            if ens:
                ens_p_miss = ens.get(missing_num, 1/3)
        ml_miss = 0.4 * m_p_miss + 0.6 * ens_p_miss
        levels  = self.d_levels.get(missing_num, [])
        if len(levels) >= 20:
            if ema_signal(levels, "tendencia"): ml_miss *= 0.85
            elif ema_signal(levels, "moderado"): ml_miss *= 0.92
        return 1.0 - ml_miss

    # ── ESTRATEGIA 1: PF + PHF + ML ──────────────────────────────────────────
    def _detect_e1(self) -> Optional[Dict]:
        if not self.warmup_done or not self.spin_history:
            return None
        last_num = self.spin_history[-1]["number"]
        if last_num == 0:
            return None

        pf_d = self._get_pf()
        if not pf_d:
            return None
        phf_d = self._get_phf(last_num)
        if not phf_d:
            return None
        if set(pf_d["pair"]) != set(phf_d["pair"]):
            return None

        base = PF_W_NORM * pf_d["prob"] + PH_W_NORM * phf_d["prob"]
        ml   = self._predict_pair_ml(pf_d["missing"])
        prob = BASE_W_NORM * base + ML_W_NORM * ml

        trend = ema_trend_str(self.doc_levels)
        adj, adj_detail = self.learner.get_adjustment(
            STRAT_E1, pf_d["pair"], trend, self.gestor.nivel
        )
        prob_adj = round(max(0.0, min(1.0, prob + adj)), 4)

        logger.info(
            f"[E1] PF:{pf_d['prob']:.0%} PHF:{phf_d['prob']:.0%} "
            f"base:{base:.0%} ml:{ml:.0%} → raw:{prob:.0%} "
            f"adj:{adj:+.3f}({adj_detail}) → final:{prob_adj:.0%}"
        )

        if prob_adj < MIN_PROB:
            return None

        return {
            "strategy": STRAT_E1, "pair": pf_d["pair"],
            "missing": pf_d["missing"], "prob": prob_adj,
            "label": "PF+PHF+ML",
            "pf_prob": pf_d["prob"], "phf_prob": phf_d["prob"],
            "ema_trend": trend, "last_number": last_num,
            "adj": adj, "adj_detail": adj_detail
        }

    # ── ESTRATEGIA 2: PHTML + EMA ─────────────────────────────────────────────
    def _detect_e2(self, number: Optional[int] = None) -> Optional[Dict]:
        if not self.warmup_done:
            return None
        if number is None:
            if not self.spin_history:
                return None
            number = self.spin_history[-1]["number"]
        if number == 0:
            return None

        phtml_pair = self._get_phtml_pair(number)
        if phtml_pair is None:
            return None

        trend  = ema_trend_str(self.doc_levels)
        e_pair = ema_trend_pair(trend)

        if set(phtml_pair["pair"]) != set(e_pair["pair"]):
            return None

        prob = phtml_pair["prob"]

        adj, adj_detail = self.learner.get_adjustment(
            STRAT_E2, phtml_pair["pair"], trend, self.gestor.nivel
        )
        prob_adj = round(max(0.0, min(1.0, prob + adj)), 4)

        logger.info(
            f"[E2] N°{number} PHTML{phtml_pair['pair']} + EMA {e_pair['label']} "
            f"→ raw:{prob:.0%} adj:{adj:+.3f}({adj_detail}) → final:{prob_adj:.0%}"
        )
        return {
            "strategy": STRAT_E2, "pair": phtml_pair["pair"],
            "missing": phtml_pair["missing"], "prob": prob_adj,
            "label": f"PHTML+EMA({e_pair['label']})", "ema_trend": trend,
            "pf_prob": prob, "phf_prob": prob,
            "last_number": number,
            "adj": adj, "adj_detail": adj_detail
        }

    # ── ESTRATEGIA 3: RETORNO PF Break ───────────────────────────────────────
    def _detect_e3(self) -> Optional[Dict]:
        if not self.warmup_done:
            return None

        non_zero = [s["number"] for s in self.spin_history if s["number"] != 0]
        if len(non_zero) < 6:
            return None

        prev5   = non_zero[-6:-1]
        last_n  = non_zero[-1]
        cats5   = list(set(get_dozen(n) for n in prev5))
        if len(cats5) != 2:
            return None

        pair       = tuple(sorted(cats5))
        last_dozen = get_dozen(last_n)
        if last_dozen in pair:
            return None

        streak = 0
        for n in reversed(non_zero[:-1]):
            if get_dozen(n) in pair:
                streak += 1
            else:
                break

        phf_break = self._get_phf(last_n)
        if phf_break is None or set(phf_break["pair"]) != set(pair):
            return None

        return_prob = self._calc_return_prob(pair, streak, last_n)

        trend = ema_trend_str(self.doc_levels)
        adj, adj_detail = self.learner.get_adjustment(
            STRAT_E3, pair, trend, self.gestor.nivel
        )
        return_prob_adj = round(max(0.0, min(1.0, return_prob + adj)), 4)

        logger.info(
            f"[E3] Rotura N°{last_n} D{last_dozen} | par D{pair} racha={streak} "
            f"raw:{return_prob:.0%} adj:{adj:+.3f}({adj_detail}) → final:{return_prob_adj:.0%}"
        )

        if return_prob_adj < MIN_PROB:
            return None

        return {
            "strategy": STRAT_E3, "pair": pair,
            "missing": last_dozen, "prob": return_prob_adj,
            "label": f"RETORNO (racha {streak}g)",
            "streak": streak, "break_number": last_n,
            "pf_prob": return_prob, "phf_prob": phf_break["prob"],
            "ema_trend": trend, "last_number": last_n,
            "adj": adj, "adj_detail": adj_detail
        }

    def _calc_return_prob(self, pair: tuple, streak: int, break_num: int) -> float:
        non_zero   = [s["number"] for s in self.spin_history if s["number"] != 0]
        last20     = non_zero[-20:]
        pair_count = sum(1 for n in last20 if get_dozen(n) in pair)
        base_prob  = pair_count / len(last20) if last20 else 0.66
        streak_bst = min(0.40, streak * 0.04)
        brk_d      = get_dozen(break_num)
        brk_adj    = 0.02 if brk_d in pair else -0.04
        missing    = list({1, 2, 3} - set(pair))[0]
        levels     = self.d_levels.get(missing, [])
        ema_adj    = 0.0
        if len(levels) >= 20:
            if ema_signal(levels, "tendencia"): ema_adj = -0.08
            elif ema_signal(levels, "moderado"): ema_adj = -0.04
        return round(max(0.35, min(0.97, base_prob + streak_bst + brk_adj + ema_adj)), 4)

    # ── Selección de mejor estrategia ─────────────────────────────────────────
    def _select_best_signal(self) -> Optional[Dict]:
        e1 = self._detect_e1()
        e2 = self._detect_e2()
        e3 = self._detect_e3()

        candidates = [s for s in [e1, e2, e3] if s]
        if not candidates:
            return None

        if e1 and e2 and set(e1["pair"]) == set(e2["pair"]):
            combined_prob = min(0.99, e1["prob"] * 0.55 + e2["prob"] * 0.45)
            logger.info(f"[DC] 🏆 ACUERDO E1+E2 → par{e1['pair']} prob:{combined_prob:.0%}")
            e1["prob"]  = combined_prob
            e1["label"] = f"E1+E2 ACUERDO ({e1['pair']})"

        _priority = {STRAT_E1: 3, STRAT_E3: 2, STRAT_E2: 1}
        candidates.sort(key=lambda x: (x["prob"], _priority.get(x["strategy"], 0)),
                        reverse=True)
        return candidates[0]

    # ── Formato de señal (NUEVOS FORMATOS) ────────────────────────────────────
    def _format_bets(self, bet_usd: float) -> str:
        return f"🇺🇲 USD: ${bet_usd:.2f} x Docena"

    def _intento_header(self, intento: int) -> str:
        if intento == 1: return "✅✅ ENTRADA CONFIRMADA ✅✅"
        if intento == 2: return "☑️☑️ SEGUNDA OPORTUNIDAD ☑️☑️"
        return "☑️☑️ TERCERA OPORTUNIDAD ☑️☑️"

    def _strat_icon(self) -> str:
        return {STRAT_E1: "🅐", STRAT_E2: "🅑", STRAT_E3: "🅒"}.get(
            self.active_strategy, "?"
        )

    def _build_signal_text(self) -> str:
        bet_usd   = self.gestor.get_bet(self.active_intento)
        p         = self.active_pair
        pair_disp = f"D{p[0]} y D{p[1]}"
        header    = self._intento_header(self.active_intento)
        icon      = self._strat_icon()
        return (
            f"{header}\n\n"
            f"🕹️ RUSSIAN ROULETTE\n"
            f"🌟 Estrategia {icon}\n"
            f"❄️ Docenas: {pair_disp}\n"
            f"🇺🇲 USD: ${bet_usd:.2f} x Docena\n\n"
            f"{russian_links_html()}"
        )

    def _send_signal(self):
        msg_id = tg_send(self._build_signal_text())
        if msg_id:
            self.active_signal_msg_id = msg_id

    # ── NUEVOS FORMATOS WIN / LOSS ────────────────────────────────────────────
    def _format_win_msg(self, winning_dozen: int, profit: float) -> str:
        self.signal_counter += 1
        return (
            f"✅ WIN #{self.signal_counter} — DOCENA D{winning_dozen}\n"
            f"💵 Balance: ${self.bankroll:.2f} USD\n"
            f"🎉 Felicitaciones +{profit:.2f} USD"
        )

    def _format_loss_msg(self, missing_dozen: int, total_loss: float) -> str:
        self.signal_counter += 1
        return (
            f"❌ LOSS #{self.signal_counter} — DOCENA D{missing_dozen}\n"
            f"💵 Balance: ${self.bankroll:.2f} USD\n"
            f"🈴 Perdimos -$ {total_loss:.2f} 🈴"
        )

    # ── Reset señal ───────────────────────────────────────────────────────────
    def _reset_signal(self):
        self.signal_active     = False
        self.active_strategy   = None
        self.active_pair       = ()
        self.active_missing    = 0
        self.active_intento    = 1
        self.total_signal_loss = 0.0
        self.active_signal_msg_id = None

    # ── Verificar resultado de señal activa ───────────────────────────────────
    def _check_active_signal(self, number: int):
        if not self.signal_active:
            return
        d = get_dozen(number)
        if d == 0:
            return

        bet_usd = self.gestor.get_bet(self.active_intento)

        if d in self.active_pair:
            profit = bet_usd
            self.bankroll += profit
            self.gestor.verificar_recuperacion(self.bankroll)

            win_msg = self._format_win_msg(d, profit)
            tg_send(win_msg)

            self.stats.record('WIN', self.active_intento,
                              self.signal_counter, d, self.bankroll,
                              self.active_strategy)
            self.learner.resolve("WIN", self.active_intento,
                                 f"D{d} acertó en par D{self.active_pair[0]}+D{self.active_pair[1]}")
            self._reset_signal()

            logger.info(
                f"[RussianDC] ✅ WIN D{d} | Profit: +${profit:.2f} | "
                f"Balance: ${self.bankroll:.2f}"
            )

        elif d == self.active_missing:
            loss = bet_usd * 2
            self.bankroll -= loss
            self.total_signal_loss += loss

            if self.active_intento < MAX_INTENTOS:
                self.active_intento += 1
                self._send_signal()
                logger.info(
                    f"[RussianDC] ❌ Int.{self.active_intento - 1} LOSS D{d} | "
                    f"Loss: -${loss:.2f} | Próximo intento: {self.active_intento}"
                )
            else:
                self.gestor.registrar_perdida_senal()

                loss_msg = self._format_loss_msg(self.active_missing,
                                                  self.total_signal_loss)
                tg_send(loss_msg)

                self.stats.record('LOSS', self.active_intento,
                                  self.signal_counter, self.active_missing,
                                  self.bankroll, self.active_strategy)
                self.learner.resolve("LOSS", self.active_intento,
                                     f"D{d} missing, par D{self.active_pair[0]}+D{self.active_pair[1]} falló")
                self._reset_signal()

                logger.info(
                    f"[RussianDC] ❌ LOSS FINAL D{d} | Total loss: -${loss:.2f} | "
                    f"Balance: ${self.bankroll:.2f} | Nivel→{self.gestor.nivel}"
                )

    # ── Procesar spin ─────────────────────────────────────────────────────────
    def process_spin(self, game_id: str, number: int):
        if game_id in self.processed_game_ids:
            return

        self.processed_game_ids.add(game_id)
        if len(self.processed_game_ids) > self.MAX_PROCESSED_IDS:
            self.processed_game_ids = set(list(self.processed_game_ids)[-200:])

        self._update_state(number)

        if not self.warmup_done:
            self.ws_count += 1
            if self.ws_count >= WARMUP_SPINS:
                self.warmup_done = True
                logger.info(f"[RussianDC] ✅ Warmup completado ({self.ws_count} spins)")
            return

        if self.signal_active:
            self._check_active_signal(number)
            return

        signal = self._select_best_signal()
        if signal is None:
            return

        self.signal_active     = True
        self.active_strategy   = signal["strategy"]
        self.active_pair       = signal["pair"]
        self.active_missing    = signal["missing"]
        self.active_intento    = 1
        self.total_signal_loss = 0.0

        self.gestor.iniciar_senal(self.bankroll)

        dozen_seq_5 = self.dozen_seq[-5:] if len(self.dozen_seq) >= 5 else list(self.dozen_seq)
        self.learner.register_signal(
            strategy=signal["strategy"],
            pair=signal["pair"],
            missing=signal["missing"],
            prob=signal["prob"],
            nivel=self.gestor.nivel,
            pf_prob=signal.get("pf_prob", 0),
            phf_prob=signal.get("phf_prob", 0),
            ema_trend=signal.get("ema_trend", "neutral"),
            last_number=signal.get("last_number", 0),
            dozen_seq_5=dozen_seq_5
        )

        self._send_signal()

        logger.info(
            f"[RussianDC] 🎯 Señal {signal['label']} | "
            f"Par: D{signal['pair'][0]}+D{signal['pair'][1]} | "
            f"Missing: D{signal['missing']} | "
            f"Prob: {signal['prob']:.0%} | Nivel: {self.gestor.nivel}"
        )

        if self.stats.should_send():
            stats_text = self.stats.get_stats_text(self.bankroll)
            tg_send(stats_text)
            self.stats.mark_sent()

    # ── Poll loop ─────────────────────────────────────────────────────────────
    async def poll_loop(self):
        last_stats_poll = 0
        last_spin_id    = None

        while True:
            try:
                # Actualizar stats del servidor cada 30s
                if time.time() - last_stats_poll >= 30:
                    try:
                        url = f"{STATS_URL}/api/{TARGET_ROULETTE.lower()}/stats"
                        req = urllib.request.Request(url, headers={"User-Agent": "RussianDC/1.0"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            data = json.loads(resp.read().decode())
                            self.stats_client.update(data)
                            last_stats_poll = time.time()
                    except Exception as e:
                        self.stats_client.connected = False
                        self.stats_client.last_error = str(e)
                        logger.debug(f"⚠️ Stats poll error: {e}")

                # Poll spins
                try:
                    url = f"{STATS_URL}/api/{TARGET_ROULETTE.lower()}/spins"
                    req = urllib.request.Request(url, headers={"User-Agent": "RussianDC/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode())
                        spins = data.get("spins", [])
                        for spin in spins:
                            gid    = spin.get("id", "")
                            number = spin.get("number", 0)
                            self.process_spin(gid, number)
                except Exception as e:
                    logger.debug(f"⚠️ Spins poll error: {e}")

            except Exception as e:
                logger.error(f"⚠️ Poll loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

# ─── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)
engine: Optional[RussianRouletteEngine] = None

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "roulette": "russian"})

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"})

@app.route('/stats', methods=['GET'])
def stats_endpoint():
    if engine is None:
        return jsonify({"error": "Engine not initialized"}), 503
    return jsonify({
        "bankroll": engine.bankroll,
        "nivel": engine.gestor.nivel,
        "signal_active": engine.signal_active,
        "signals_processed": engine.stats.signals_processed,
        "wins": engine.stats.wins,
        "losses": engine.stats.losses,
        "warmup": engine.warmup_done,
        "spins": len(engine.spin_history)
    })

# ─── BOT COMMANDS ─────────────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
def cmd_start(m):
    bot.reply_to(m,
        "🕹️ <b>RUSSIAN ROULETTE BOT</b>\n\n"
        "Bot de señales activo.\n"
        "Usa /status para ver el estado actual.\n"
        "Usa /aprendizaje para ver el resumen del aprendizaje.",
        parse_mode="HTML"
    )

@bot.message_handler(commands=['status'])
def cmd_status(m):
    if not engine:
        bot.reply_to(m, "❌ Engine no inicializado", parse_mode="HTML")
        return
    total = engine.stats.wins + engine.stats.losses
    eff   = (engine.stats.wins / total * 100) if total > 0 else 0
    status = (
        f"🕹️ <b>RUSSIAN ROULETTE</b>\n\n"
        f"💰 Balance: ${engine.bankroll:.2f} USD\n"
        f"🎚 Nivel: {engine.gestor.nivel}\n"
        f"📊 Señales: {total} (✅{engine.stats.wins} ❌{engine.stats.losses})\n"
        f"📈 Assert: {eff:.1f}%\n"
        f"🔥 Consecutivas: {engine.stats.consecutive}\n"
        f"📡 Stats server: {'🟢' if engine.stats_client.connected else '🔴'}\n"
        f"⏳ Warmup: {'✅' if engine.warmup_done else 'Pendiente'}\n"
        f"🎰 Spins: {len(engine.spin_history)}\n"
        f"🎯 Señal activa: {'Sí' if engine.signal_active else 'No'}"
    )
    bot.reply_to(m, status, parse_mode="HTML")

@bot.message_handler(commands=['aprendizaje'])
def cmd_aprendizaje(m):
    if not engine:
        bot.reply_to(m, "❌ Engine no inicializado", parse_mode="HTML")
        return
    summary = engine.learner.get_summary()
    bot.reply_to(m, summary, parse_mode="HTML")

@bot.message_handler(commands=['stats'])
def cmd_stats(m):
    if not engine:
        bot.reply_to(m, "❌ Engine no inicializado", parse_mode="HTML")
        return
    bot.reply_to(m, engine.stats.get_stats_text(engine.bankroll), parse_mode="HTML")

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    if engine:
        engine.stats              = DetailedStats()
        engine.bankroll           = 100.0
        engine.gestor.nivel       = 1
        engine.gestor.debt_stack  = []
        engine.processed_game_ids.clear()
        engine._reset_signal()
    bot.reply_to(
        m, f"🔄 <b>Resetado — Balance: ${engine.bankroll:.2f} USD | Nivel: 1</b>\n"
           f"<i>🧠 Aprendizaje conservado ({len(engine.learner.history)} señales)</i>",
        parse_mode="HTML"
    )

@bot.message_handler(commands=['reset_learning'])
def cmd_reset_learning(m):
    """Borra el historial de aprendizaje (usar con precaución)."""
    if not engine:
        bot.reply_to(m, "❌ Engine no inicializado", parse_mode="HTML")
        return
    try:
        engine._db.execute("DELETE FROM signal_log")
        engine._db.commit()
        engine.learner.history.clear()
        engine.learner.pending_id = None
        bot.reply_to(m, "🗑️ <b>Historial de aprendizaje borrado.</b>", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}", parse_mode="HTML")

# ─── SELF PING ────────────────────────────────────────────────────────────────
async def self_ping_loop():
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url or "localhost" in url:
        return
    await asyncio.sleep(30)
    while True:
        try:
            import urllib.request as _ur
            _ur.urlopen(f"{url}/ping", timeout=15)
        except:
            pass
        await asyncio.sleep(240)

def run_flask():
    app.run(host="0.0.0.0", port=10005, debug=False, use_reloader=False)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    global engine
    stats_client = StatsClient()
    engine       = RussianRouletteEngine(stats_client)
    threading.Thread(
        target=lambda: bot.polling(none_stop=True, interval=1, timeout=30),
        daemon=True
    ).start()
    logger.info(
        f"[RussianDC] 🎰 Russian Roulette DC v30b — "
        f"HTTP Polling 1s | E1/E2/E3 | {MAX_INTENTOS} intentos | "
        f"Ficha ${BASE_BET} USD | Niveles 1-{MAX_NIVEL} | "
        f"🧠 Learner activo ({len(engine.learner.history)} señales previas)"
    )
    await asyncio.gather(
        asyncio.create_task(engine.poll_loop()),
        asyncio.create_task(self_ping_loop())
    )

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot detenido.")
