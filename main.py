#!/usr/bin/env python3
"""
Speed Roulette DC — Bot de señales para Docenas y Columnas
Sistema PF + PH + ML Cruzado + Gestión (6 niveles, 2 oportunidades)
+ Sesiones de 30 min + WebSocket Server para HTML + Gestión del Cero (10%)
+ Monitoreo 3 Ruletas (Telegram solo Speed 2, HTML todas) + Keep-Alive
+ Mensajes de señal con montos por país (USD/MXN/PEN/COP/ARS/CLP)
+ Reporte diario 12:00 ARG + Comandos Telegram
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import datetime
from collections import deque, defaultdict
from typing import Optional, Dict, Set

import numpy as np
from sklearn.naive_bayes import MultinomialNB
from sklearn.linear_model import SGDClassifier

import telebot
import websockets
from aiohttp import web, ClientSession
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [SpeedDC] %(levelname)s %(message)s')
logger = logging.getLogger("SpeedDC")
for _ln in ['werkzeug', 'flask.app', 'flask', 'urllib3', 'telegram', 'aiohttp.access']:
    logging.getLogger(_ln).setLevel(logging.ERROR)

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
TOKEN           = "8615799238:AAG2kLg-Ostc4Y4E98HXDIoje_U4F7oqdzU"
CHAT_ID         = -1003821352139
STATS_THREAD_ID = 40034

_session = requests.Session()
_retry = Retry(total=5, backoff_factor=1.5,
               status_forcelist=[429, 500, 502, 503, 504],
               allowed_methods=["GET", "POST"], raise_on_status=False)
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))
_session.mount("http://",  HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))
bot = telebot.TeleBot(TOKEN, threaded=False)
bot.session = _session

# ─── RULETAS ─────────────────────────────────────────────────────────────────
ROULETTES = [
    {"key": 203, "name": "SPEED ROULETTE 1"},
    {"key": 205, "name": "SPEED ROULETTE 2"},
    {"key": 206, "name": "ROULETTE MACAO"},
]
ROULETTE_LINKS = {
    "SPEED ROULETTE 1": "https://1win.lat/casino/play/v_pragmatic:speedroulette1",
    "SPEED ROULETTE 2": "https://1win.lat/casino/play/v_pragmatic:speedroulette2",
    "ROULETTE MACAO":   "https://1win.lat/casino/play/v_pragmatic:roulettemacao",
}

# ─── MONEDAS ─────────────────────────────────────────────────────────────────
# mult = multiplicador sobre apuesta base USD
# Gale 0: bet_base * nivel * mult   → docena/columna
# Gale 1: bet_base * nivel * 3 * mult
# Cero siempre = 10% de la apuesta por docena/columna
CURRENCIES = {
    "USD": {"flag": "🇺🇲", "mult": 1,    "dec": 2, "round": 0.01, "sym": "$"},
    "MXN": {"flag": "🇲🇽", "mult": 10,   "dec": 0, "round": 1,   "sym": "$"},
    "PEN": {"flag": "🇵🇪", "mult": 5,    "dec": 2, "round": 0.1,  "sym": "S/."},
    "COP": {"flag": "🇨🇴", "mult": 2500, "dec": 0, "round": 500,  "sym": "$"},
    "ARS": {"flag": "🇦🇷", "mult": 500,  "dec": 0, "round": 50,   "sym": "$"},
    "CLP": {"flag": "🇨🇱", "mult": 200,  "dec": 0, "round": 10,   "sym": "$"},
}

def fmt_cur(amount_usd: float, cur_key: str) -> str:
    """Formatea un monto USD al valor en la moneda indicada."""
    c = CURRENCIES[cur_key]
    val = amount_usd * c["mult"]
    rounded = round(val / c["round"]) * c["round"]
    if c["dec"] == 0:
        return f"{c['sym']}{int(rounded):,}"
    return f"{c['sym']}{rounded:.{c['dec']}f}"

# ─── CONSTANTES ───────────────────────────────────────────────────────────────
WS_URL              = "wss://dga.pragmaticplaylive.net/ws"
CASINO_ID           = "ppcjd00000007254"
SESSION_ACTIVE      = 25 * 60
SESSION_PAUSE       = 5  * 60
SESSION_TOTAL       = SESSION_ACTIVE + SESSION_PAUSE
BASE_BET            = 1.00          # USD base por docena/columna, nivel 1
MAX_NIVEL           = 6
WARMUP_SPINS        = 25
MIN_PROB            = 0.78
TRAIN_INTERVAL      = 100
MAX_SIGNALS         = 3
SIGNAL_WAIT_TIMEOUT = 120

REAL_COLOR_MAP: dict = {
    0:"VERDE",  1:"ROJO",  2:"NEGRO", 3:"ROJO",  4:"NEGRO", 5:"ROJO",
    6:"NEGRO",  7:"ROJO",  8:"NEGRO", 9:"ROJO",  10:"NEGRO",11:"NEGRO",
    12:"ROJO",  13:"NEGRO",14:"ROJO", 15:"NEGRO",16:"ROJO", 17:"NEGRO",
    18:"ROJO",  19:"ROJO", 20:"NEGRO",21:"ROJO",  22:"NEGRO",23:"ROJO",
    24:"NEGRO", 25:"ROJO", 26:"NEGRO",27:"ROJO",  28:"NEGRO",29:"NEGRO",
    30:"ROJO",  31:"NEGRO",32:"ROJO", 33:"NEGRO", 34:"ROJO", 35:"NEGRO",
    36:"ROJO",
}

def get_dozen(n: int) -> int:
    return 0 if n == 0 else (n - 1) // 12 + 1

def get_column(n: int) -> int:
    return 0 if n == 0 else ((n - 1) % 3) + 1

# ─── BROADCAST ────────────────────────────────────────────────────────────────
_ws_clients: Set[asyncio.Queue] = set()

def queue_broadcast(data: dict):
    for q in list(_ws_clients):
        try: q.put_nowait(data)
        except: pass

# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────
_TG_RETRIES = 12

def _tg_call(fn, *a, **kw):
    delay = 2.0
    for attempt in range(1, _TG_RETRIES + 1):
        try: return fn(*a, **kw)
        except Exception as e:
            err = str(e)
            if "retry after" in err.lower():
                try: wait = int(''.join(filter(str.isdigit, err))) + 1
                except: wait = 30
                time.sleep(wait); continue
            if attempt == _TG_RETRIES: return None
            time.sleep(delay); delay = min(delay * 2, 60)
    return None

def tg_send(text: str) -> Optional[int]:
    msg = _tg_call(bot.send_message, chat_id=CHAT_ID, text=text, parse_mode="HTML")
    return msg.message_id if msg else None

def tg_send_stats(text: str) -> Optional[int]:
    msg = _tg_call(bot.send_message, chat_id=CHAT_ID, text=text,
                   parse_mode="HTML", message_thread_id=STATS_THREAD_ID)
    return msg.message_id if msg else None

def tg_delete(chat_id: int, message_id: int):
    try: _tg_call(bot.delete_message, chat_id=chat_id, message_id=message_id)
    except: pass

def tg_edit(chat_id: int, message_id: int, text: str):
    try:
        _tg_call(bot.edit_message_text, text=text, chat_id=chat_id,
                 message_id=message_id, parse_mode="HTML")
    except: pass

def get_roulette_url(name: str) -> Optional[str]:
    clean = name.upper().strip()
    for key, url in ROULETTE_LINKS.items():
        if key.upper() in clean or clean in key.upper():
            return url
    return None

def tg_send_with_button(text: str, roulette_name: str) -> Optional[int]:
    url = get_roulette_url(roulette_name)
    if not url:
        return tg_send(text)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎰 ACCEDER A LA RULETA", url=url))
    msg = _tg_call(bot.send_message, chat_id=CHAT_ID, text=text,
                   parse_mode="HTML", reply_markup=markup,
                   disable_web_page_preview=True)
    return msg.message_id if msg else None

# ─── EMA ──────────────────────────────────────────────────────────────────────
def calc_ema(data: list, period: int) -> list:
    if len(data) < period: return [None] * len(data)
    mult = 2 / (period + 1)
    out  = [None] * (period - 1)
    prev = sum(data[:period]) / period
    out.append(prev)
    for v in data[period:]:
        prev = v * mult + prev * (1 - mult)
        out.append(prev)
    return out

def ema_signal(levels: list, mode: str = "moderado") -> bool:
    if len(levels) < 20: return False
    e4, e8, e20 = calc_ema(levels, 4), calc_ema(levels, 8), calc_ema(levels, 20)
    li = len(levels) - 1
    if any(v is None for v in [e4[li], e8[li], e20[li]]): return False
    cur = levels[li]; ce4, ce8, ce20 = e4[li], e8[li], e20[li]
    pe4  = e4[li-1]  if li > 0 and e4[li-1]  is not None else ce4
    pe8  = e8[li-1]  if li > 0 and e8[li-1]  is not None else ce8
    pe20 = e20[li-1] if li > 0 and e20[li-1] is not None else ce20
    if mode == "tendencia":
        return (pe4 <= pe20 and ce4 > ce20) or (cur > ce4 and cur > ce8 and cur > ce20)
    else:
        v_pattern = False
        if len(levels) >= 3:
            a, b, c = levels[-3], levels[-2], levels[-1]
            v_pattern = (b < a) and (b < c) and (c > a)
        return (pe4 <= pe8 and ce4 > ce8) or (pe8 <= pe20 and ce8 > ce20) or \
               (cur > ce4 and cur > ce8) or v_pattern

# ─── MARKOV ───────────────────────────────────────────────────────────────────
class SmoothedMarkovPredictor:
    def __init__(self, window: int = 60, order: int = 2):
        self.window = window; self.order = order
        self.transition_counts: dict = {}

    def update(self, sequence: list):
        self.transition_counts = defaultdict(lambda: defaultdict(int))
        recent = sequence[-self.window:]
        if len(recent) < self.order + 1: return
        for i in range(len(recent) - self.order):
            state = tuple(recent[i:i + self.order])
            self.transition_counts[state][recent[i + self.order]] += 1

    def predict(self, sequence: list) -> Optional[dict]:
        if len(sequence) < self.order: return None
        state  = tuple(sequence[-self.order:])
        counts = dict(self.transition_counts.get(state, {}))
        total  = sum(counts.values())
        if total < 10: return None
        alpha = 2.0; vs = 3
        probs = {k: (v + alpha) / (total + alpha * vs) for k, v in counts.items()}
        for c in [1, 2, 3]:
            probs.setdefault(c, alpha / (total + alpha * vs))
        return probs

# ─── ENSEMBLE ML ──────────────────────────────────────────────────────────────
class OnlineEnsemblePredictor:
    WINDOW = 5; CLASSES = [1, 2, 3]

    def __init__(self):
        self.mnb = MultinomialNB(alpha=2.0, class_prior=[0.333, 0.333, 0.333])
        self.sgd = SGDClassifier(loss='log_loss', learning_rate='adaptive',
                                 eta0=0.005, penalty='l2', alpha=0.01, epsilon=0.2)
        self.trained = False; self.sample_count = 0

    def _feats(self, hd, hc, pfd, phd, pfc, phc):
        if len(hd) < self.WINDOW or len(hc) < self.WINDOW: return None
        f = []
        for i in range(1, self.WINDOW + 1):
            v = [0]*9; v[(hd[-i]-1)*3+(hc[-i]-1)] = 1; f.extend(v)
        for pair in (pfd, phd, pfc, phc):
            v = [0,0,0]
            for x in pair: v[x-1] = 1
            f.extend(v)
        return f

    def partial_train(self, hd, hc, target, pfd, phd, pfc, phc):
        ft = self._feats(hd[:-1], hc[:-1], pfd, phd, pfc, phc)
        if ft is None: return
        X = np.array(ft).reshape(1,-1); y = np.array([target])
        if not self.trained:
            self.mnb.partial_fit(X, y, classes=self.CLASSES)
            self.sgd.partial_fit(X, y, classes=self.CLASSES)
            self.trained = True
        else:
            self.mnb.partial_fit(X, y); self.sgd.partial_fit(X, y)
        self.sample_count += 1

    def predict(self, hd, hc, pfd, phd, pfc, phc) -> Optional[dict]:
        if not self.trained: return None
        ft = self._feats(hd, hc, pfd, phd, pfc, phc)
        if ft is None: return None
        X = np.array(ft).reshape(1,-1)
        try:
            p = 0.5*self.mnb.predict_proba(X)[0] + 0.5*self.sgd.predict_proba(X)[0]
            return {c+1: float(v) for c, v in enumerate(p)}
        except: return None

# ─── GESTOR (6 NIVELES, 2 OPORTUNIDADES, PILA DE DEUDAS) ─────────────────────
class GestorDocenas:
    """
    Gale #0 → nivel × BASE_BET  por docena/columna
    Gale #1 → 3 × nivel × BASE_BET
    Cero    → 10% de la apuesta por categoría
    """
    def __init__(self):
        self.nivel = 1; self.oportunidad = 1
        self.max_nivel = MAX_NIVEL; self.b0 = 0.0; self.debt_stack: list = []

    def iniciar_senal(self, balance: float):
        self.b0 = balance; self.oportunidad = 1

    def get_target(self) -> float:
        base = BASE_BET * 0.9
        return (self.debt_stack[-1] if self.debt_stack else self.b0) + base

    def apostar_por_docena(self, _balance: float = 0) -> float:
        return self.nivel * BASE_BET if self.oportunidad == 1 \
               else 3 * self.nivel * BASE_BET

    def registrar_perdida_senal(self):
        self.debt_stack.append(self.b0)
        self.nivel = self.nivel + 1 if self.nivel < self.max_nivel else 1
        logger.info(f"[Gestor] 📋 Deuda B0={self.b0:.2f} | "
                    f"Pila:{len(self.debt_stack)} | Nivel→{self.nivel}")

    def verificar_recuperacion(self, balance: float):
        while self.debt_stack:
            if balance >= self.get_target():
                self.debt_stack.pop()
                logger.info(f"[Gestor] ✅ Deuda recuperada | Pila:{len(self.debt_stack)}")
            else: break
        if not self.debt_stack:
            self.nivel = 1; logger.info("[Gestor] 🏆 Sin deudas — Nivel→1")

# ─── STATS GLOBAL ─────────────────────────────────────────────────────────────
class GlobalStats:
    def __init__(self):
        self.wins = 0; self.zeros = 0; self.losses = 0
        self.consecutive = 0; self.last_20 = deque(maxlen=20)
        self.signals_processed = 0; self.global_balance: float = 0.0
        self.last_report_signals = 0

    def record(self, result_type, attempt, number, val, type_str, roulette_name):
        self.signals_processed += 1
        if result_type == 'WIN':   self.wins   += 1; self.consecutive += 1
        elif result_type == 'LOSS': self.losses += 1; self.consecutive  = 0
        elif result_type == 'EMPATE': self.zeros += 1
        self.last_20.append({"result": result_type, "attempt": attempt,
                              "number": number, "val": val, "type": type_str,
                              "roulette": roulette_name, "balance": self.global_balance})

    def should_send(self) -> bool:
        return (self.signals_processed - self.last_report_signals) >= 20

    def mark_sent(self): self.last_report_signals = self.signals_processed

    def get_stats_text(self) -> str:
        total = self.wins + self.zeros + self.losses
        eff   = ((self.wins + self.zeros) / total * 100) if total > 0 else 0.0
        t  = "📊 RESUMEN DIARIO — SPEED ROULETTE 2 📊\n"
        t += "🕛 Reporte 12:00 hs (Argentina)\n\n"
        t += f"► PLACAR = ✅{self.wins} | 🟠{self.zeros} | 🚫{self.losses}\n"
        t += f"► Consecutivas = {self.consecutive}\n"
        t += f"► Assertividade = {eff:.2f}%\n"
        t += f"► Balance total: 💰 {fmt_cur(self.global_balance,'USD')}\n"
        t += f"► Total señales del día: {total}\n\n"
        t += "📌 Últimas 20 SEÑALES 📌\n"
        for s in reversed(list(self.last_20)):
            a_str = f"🔄 GALE #{s['attempt']}"; b_str = f"💰 {fmt_cur(s['balance'],'USD')}"
            rl = s['roulette'][:14]
            if s['result'] == 'WIN':
                t += f"✅ WIN #{s['number']} {s['type']} {s['val']} | {rl} | {a_str} | {b_str}\n"
            elif s['result'] == 'EMPATE':
                t += f"🟠 EMPATE #0 ZERO | {rl} | {a_str} | {b_str}\n"
            else:
                t += f"🚫 LOSS #{s['number']} {s['type']} {s['val']} | {rl} | {a_str} | {b_str}\n"
        return t

GLOBAL_STATS = GlobalStats()

# ─── ENGINE ───────────────────────────────────────────────────────────────────
class RouletteEngine:

    def __init__(self, ws_key: int, name: str):
        self.ws_key = ws_key; self.name = name
        self.db_path = f"main_roulette_{ws_key}.db"
        self.spin_history: list = []
        self.dozen_seq: list = []; self.column_seq: list = []
        self.d_levels: dict = {1:[], 2:[], 3:[]}
        self.c_levels: dict = {1:[], 2:[], 3:[]}
        self.markov_d = SmoothedMarkovPredictor()
        self.markov_c = SmoothedMarkovPredictor()
        self.ensemble_d = OnlineEnsemblePredictor()
        self.ensemble_c = OnlineEnsemblePredictor()
        self.after_number_dozen:  dict = defaultdict(lambda: defaultdict(int))
        self.after_number_column: dict = defaultdict(lambda: defaultdict(int))
        self.signal_active = False; self.active_type = None
        self.active_pair: tuple = (); self.active_missing = ""
        self._last_signal_prob = 0.0; self.gestor = GestorDocenas()
        self.total_signal_loss = 0.0; self.balance: float = 0.0
        self.active_signal_msg_id = None; self.spins_since_train = 0
        self.ws_count = 0; self.warmup_done = False
        self.stat_wins = 0; self.stat_losses = 0; self.stat_zeros = 0
        self._db = self._get_db()
        live = self._load_live_history()
        self.ws_count = live; self.warmup_done = live >= WARMUP_SPINS
        logger.info(f"[{name}] Pre-cargados:{live} | Warmup:{'✅' if self.warmup_done else '⏳'}")

    # ── DB ────────────────────────────────────────────────────────────────────
    def _get_db(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS live_spins "
                     "(id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL, ts INTEGER NOT NULL)")
        conn.commit(); return conn

    def _persist(self, n: int):
        try: self._db.execute("INSERT INTO live_spins(number,ts) VALUES(?,?)", (n, int(time.time()))); self._db.commit()
        except: pass

    def _load_live_history(self) -> int:
        try: rows = self._db.execute("SELECT number FROM live_spins ORDER BY id ASC").fetchall()
        except: return 0
        for (n,) in rows: self._update_state(n, persist=False, train_model=False)
        if rows: self._train_models()
        return len(rows)

    def _train_models(self):
        self.markov_d.update(self.dozen_seq); self.markov_c.update(self.column_seq)

    # ── STATE ─────────────────────────────────────────────────────────────────
    def _update_state(self, number: int, persist=True, train_model=True):
        color = REAL_COLOR_MAP.get(number, "VERDE")
        d = get_dozen(number); c = get_column(number)
        if number != 0 and self.spin_history:
            prev = self.spin_history[-1]["number"]
            if prev != 0:
                self.after_number_dozen[prev][d]  += 1
                self.after_number_column[prev][c] += 1
        self.spin_history.append({"number": number, "color": color})
        if d != 0:
            self.dozen_seq.append(d)
            for dd in (1,2,3):
                pv = self.d_levels[dd][-1] if self.d_levels[dd] else 0
                self.d_levels[dd].append(pv + (1 if d == dd else -1))
        if c != 0:
            self.column_seq.append(c)
            for cc in (1,2,3):
                pv = self.c_levels[cc][-1] if self.c_levels[cc] else 0
                self.c_levels[cc].append(pv + (1 if c == cc else -1))
        if train_model and d != 0 and c != 0 and len(self.dozen_seq) > 5:
            pfd, phd = self._get_pf("DOCENA"),  self._get_ph("DOCENA")
            pfc, phc = self._get_pf("COLUMNA"), self._get_ph("COLUMNA")
            if pfd and phd and pfc and phc:
                self.ensemble_d.partial_train(self.dozen_seq, self.column_seq, d,
                                              pfd["pair"], phd["pair"], pfc["pair"], phc["pair"])
                self.ensemble_c.partial_train(self.dozen_seq, self.column_seq, c,
                                              pfd["pair"], phd["pair"], pfc["pair"], phc["pair"])
            self.spins_since_train += 1
            if self.spins_since_train >= TRAIN_INTERVAL:
                self._train_models(); self.spins_since_train = 0
                logger.info(f"[{self.name}] 🧠 Re-entrenado c/{TRAIN_INTERVAL} giros")
        if persist: self._persist(number)

    # ── PREDICCIÓN ────────────────────────────────────────────────────────────
    def _get_pf(self, ct: str) -> Optional[Dict]:
        if len(self.spin_history) < 5: return None
        counts = {1:0, 2:0, 3:0}
        for s in self.spin_history[-5:]:
            n = s["number"]
            if n != 0: counts[get_dozen(n) if ct=="DOCENA" else get_column(n)] += 1
        active = [k for k,v in counts.items() if v > 0]
        if len(active) != 2: return None
        missing = list({1,2,3} - set(active))[0]
        return {"pair": tuple(sorted(active)), "missing": missing,
                "prob": sum(counts[a] for a in active) / 5.0}

    def _get_ph(self, ct: str) -> Optional[Dict]:
        if not self.spin_history: return None
        ln = self.spin_history[-1]["number"]
        if ln == 0: return None
        counts = self.after_number_dozen.get(ln,{}) if ct=="DOCENA" else self.after_number_column.get(ln,{})
        total = sum(counts.values())
        if total < 10: return None
        sc = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        if len(sc) < 2: return None
        missing = list({1,2,3} - {sc[0][0], sc[1][0]})[0]
        return {"pair": tuple(sorted([sc[0][0], sc[1][0]])), "missing": missing,
                "prob": (sc[0][1]+sc[1][1]) / total}

    def _predict_pair_ml(self, ct: str, missing_num: int) -> float:
        mk   = self.markov_d if ct=="DOCENA" else self.markov_c
        hist = self.dozen_seq if ct=="DOCENA" else self.column_seq
        levels = (self.d_levels if ct=="DOCENA" else self.c_levels).get(missing_num, [])
        mkp = mk.predict(hist); m_pm = mkp.get(missing_num, 1/3) if mkp else 1/3
        pfd,phd = self._get_pf("DOCENA"),  self._get_ph("DOCENA")
        pfc,phc = self._get_pf("COLUMNA"), self._get_ph("COLUMNA")
        ens_pm = 1/3
        if pfd and phd and pfc and phc:
            ens = self.ensemble_d.predict(hist, self.column_seq, pfd["pair"], phd["pair"], pfc["pair"], phc["pair"]) \
                  if ct=="DOCENA" else \
                  self.ensemble_c.predict(self.dozen_seq, hist, pfd["pair"], phd["pair"], pfc["pair"], phc["pair"])
            if ens: ens_pm = ens.get(missing_num, 1/3)
        ml = 0.4*m_pm + 0.6*ens_pm
        if len(levels) >= 20:
            if ema_signal(levels, "tendencia"): ml *= 0.85
            elif ema_signal(levels, "moderado"): ml *= 0.92
        return 1.0 - ml

    def detect_signal(self) -> Optional[dict]:
        pfd = self._get_pf("DOCENA"); pfc = self._get_pf("COLUMNA")
        if not pfd and not pfc: return None
        phd = self._get_ph("DOCENA"); phc = self._get_ph("COLUMNA")
        candidates = []
        if pfd and phd and set(pfd["pair"]) == set(phd["pair"]):
            base = 0.65*pfd["prob"] + 0.35*phd["prob"]
            ml   = self._predict_pair_ml("DOCENA", pfd["missing"])
            prob = 0.5*base + 0.5*ml
            logger.info(f"[{self.name}] D base:{base:.0%} ml:{ml:.0%} final:{prob:.0%}")
            if prob >= MIN_PROB:
                candidates.append({"type":"DOCENA",
                                   "pair": tuple(f"D{x}" for x in sorted(pfd["pair"])),
                                   "missing": f"D{pfd['missing']}", "prob": prob})
        if pfc and phc and set(pfc["pair"]) == set(phc["pair"]):
            base = 0.65*pfc["prob"] + 0.35*phc["prob"]
            ml   = self._predict_pair_ml("COLUMNA", pfc["missing"])
            prob = 0.5*base + 0.5*ml
            logger.info(f"[{self.name}] C base:{base:.0%} ml:{ml:.0%} final:{prob:.0%}")
            if prob >= MIN_PROB:
                candidates.append({"type":"COLUMNA",
                                   "pair": tuple(f"C{x}" for x in sorted(pfc["pair"])),
                                   "missing": f"C{pfc['missing']}", "prob": prob})
        return max(candidates, key=lambda x: x["prob"]) if candidates else None

    # ── SEÑAL — TEXTO TELEGRAM CON MONTOS POR PAÍS ───────────────────────────
    def _build_signal_text(self) -> str:
        nums      = sorted([p[1:] for p in self.active_pair])
        pair_disp = f"{nums[0]} y {nums[1]}"
        type_lbl  = "Docenas" if self.active_type == "DOCENA" else "Columnas"
        cat_lbl   = "D" if self.active_type == "DOCENA" else "C"
        is_gale   = (self.gestor.oportunidad == 2)
        header    = "✅✅ SEGUNDA OPORTUNIDAD ✅✅" if is_gale else "✅✅ ENTRADA CONFIRMADA ✅✅"
        bet_usd   = self.gestor.apostar_por_docena()

        lines = [
            header, "",
            f"🕹️ {self.name}",
            f"🎯 {type_lbl}: {cat_lbl}{nums[0]} y {cat_lbl}{nums[1]}",
            f"⚔️ Cubrir el CERO 🟢",
            f"🚨 MONTO DE APUESTA POR PAIS:",
        ]
        for cur_key, info in CURRENCIES.items():
            bet_loc  = bet_usd * info["mult"]
            zero_loc = bet_loc * 0.1
            # Redondear al múltiplo indicado
            bet_r  = round(bet_loc  / info["round"]) * info["round"]
            zero_r = round(zero_loc / info["round"]) * info["round"]
            sym = info["sym"]
            if info["dec"] == 0:
                b_str  = f"{sym}{int(bet_r):,}"
                z_str  = f"{sym}{int(zero_r):,}"
            else:
                b_str  = f"{sym}{bet_r:.{info['dec']}f}"
                z_str  = f"{sym}{zero_r:.{info['dec']}f}"
            lines.append(f"{info['flag']} {cur_key}: Docena {b_str} + Cero: {z_str}")

        return "\n".join(lines)

    def _build_signal_ws(self) -> dict:
        gale_num  = self.gestor.oportunidad - 1
        bet       = self.gestor.apostar_por_docena()
        # Construir tabla de montos por moneda para el HTML
        bet_table = {}
        for ck, info in CURRENCIES.items():
            b = round(bet * info["mult"] / info["round"]) * info["round"]
            z = round(b * 0.1 / info["round"]) * info["round"]
            bet_table[ck] = {"bet": b, "zero": z}
        return {
            "type":        "signal",
            "ws_key":      self.ws_key,
            "roulette":    self.name,
            "signal_type": self.active_type,
            "pair":        list(self.active_pair),
            "pair_nums":   [p[1:] for p in self.active_pair],
            "missing":     self.active_missing,
            "prob":        round(self._last_signal_prob, 4),
            "attempt":     self.gestor.oportunidad,
            "gale":        gale_num,
            "bet_per_cat": round(bet, 2),
            "total_bet":   round(bet * 2, 2),
            "bet_table":   bet_table,
            "balance":     round(self.balance, 2),
            "nivel":       self.gestor.nivel,
            "wins":        self.stat_wins,
            "losses":      self.stat_losses,
            "zeros":       self.stat_zeros,
            "debt_count":  len(self.gestor.debt_stack),
        }

    def send_signal(self, send_tg: bool = True):
        if send_tg and self.name == "SPEED ROULETTE 2":
            msg_id = tg_send_with_button(self._build_signal_text(), self.name)
            if msg_id: self.active_signal_msg_id = msg_id
        queue_broadcast(self._build_signal_ws())

    def iniciar_senal(self, sig: dict, send_tg: bool = True):
        self.signal_active     = True
        self.active_type       = sig["type"]
        self.active_pair       = sig["pair"]
        self.active_missing    = sig["missing"]
        self._last_signal_prob = sig.get("prob", 0)
        self.gestor.iniciar_senal(self.balance)
        self.total_signal_loss = 0.0
        self.send_signal(send_tg=send_tg)
        logger.info(f"[{self.name}] 🎯 SEÑAL {sig['type']}: {sig['pair']} ({sig['prob']:.0%})")

    # ── RESULTADO ─────────────────────────────────────────────────────────────
    def _build_result_ws(self, result, number, gale_num, profit, cat_label="") -> dict:
        return {
            "type":       "result",
            "ws_key":     self.ws_key,
            "roulette":   self.name,
            "result":     result,
            "number":     number,
            "color":      REAL_COLOR_MAP.get(number, "VERDE"),
            "dozen":      get_dozen(number),
            "column":     get_column(number),
            "detail":     cat_label,
            "attempt":    gale_num + 1,
            "gale":       gale_num,
            "profit":     round(profit, 2),
            "balance":    round(self.balance, 2),
            "total_loss": round(self.total_signal_loss, 2),
            "nivel":      self.gestor.nivel,
            "wins":       self.stat_wins,
            "losses":     self.stat_losses,
            "zeros":      self.stat_zeros,
            "debt_count": len(self.gestor.debt_stack),
        }

    def resolve(self, number: int) -> bool:
        d, c      = get_dozen(number), get_column(number)
        type_str  = self.active_type
        val_num   = d if type_str == "DOCENA" else c
        gale_num  = self.gestor.oportunidad - 1
        bet       = self.gestor.apostar_por_docena()
        zero_bet  = round(0.1 * bet, 2)
        spin_inv  = round(2 * bet + zero_bet, 2)

        if number == 0:
            zero_payout   = round(zero_bet * 36, 2)
            spin_profit   = round(zero_payout - spin_inv, 2)
            self.balance  = round(self.balance + spin_profit, 2)
            sig_profit    = round(spin_profit - self.total_signal_loss, 2)
            self.gestor.verificar_recuperacion(self.balance)
            self.stat_zeros += 1
            if self.name == "SPEED ROULETTE 2":
                GLOBAL_STATS.global_balance = self.balance
                GLOBAL_STATS.record('EMPATE', self.gestor.oportunidad, 0, 0, type_str, self.name)
                tg_send(f"🟠 EMPATE 0 — ZERO — 🔄 GALE #{gale_num}\n"
                        f"🉑 Para la próxima ganaremos {fmt_cur(sig_profit,'USD')} 🉑\n"
                        f"💰 Balance: {fmt_cur(GLOBAL_STATS.global_balance,'USD')}")
            queue_broadcast(self._build_result_ws("EMPATE", 0, gale_num, sig_profit, "ZERO"))
            if self.name == "SPEED ROULETTE 2": self._check_stats()
            self._reset_signal(); return True

        won = (type_str == "DOCENA"  and d != 0 and f"D{d}" in self.active_pair) or \
              (type_str == "COLUMNA" and c != 0 and f"C{c}" in self.active_pair)

        if won:
            payout        = round(3 * bet, 2)
            spin_profit   = round(payout - spin_inv, 2)
            self.balance  = round(self.balance + spin_profit, 2)
            sig_profit    = round(spin_profit - self.total_signal_loss, 2)
            self.gestor.verificar_recuperacion(self.balance)
            cat_label = f"{'DOCENA' if type_str=='DOCENA' else 'COLUMNA'} {val_num}"
            self.stat_wins += 1
            if self.name == "SPEED ROULETTE 2":
                GLOBAL_STATS.global_balance = self.balance
                GLOBAL_STATS.record('WIN', self.gestor.oportunidad, number, val_num, type_str, self.name)
                tg_send(f"✅ WIN {number} — {cat_label} — 🔄 GALE #{gale_num}\n"
                        f"🎉 Ganaste {fmt_cur(sig_profit,'USD')} 🎉\n"
                        f"💰 Balance: {fmt_cur(GLOBAL_STATS.global_balance,'USD')}")
            queue_broadcast(self._build_result_ws("WIN", number, gale_num, sig_profit, cat_label))
            if self.name == "SPEED ROULETTE 2": self._check_stats()
            self._reset_signal(); return True

        else:
            self.balance          = round(self.balance - spin_inv, 2)
            self.total_signal_loss = round(self.total_signal_loss + spin_inv, 2)
            if self.name == "SPEED ROULETTE 2":
                GLOBAL_STATS.global_balance = self.balance

            if gale_num == 0:
                if self.name == "SPEED ROULETTE 2" and self.active_signal_msg_id:
                    tg_delete(CHAT_ID, self.active_signal_msg_id)
                    self.active_signal_msg_id = None
                self.gestor.oportunidad = 2
                self.send_signal(send_tg=(self.name == "SPEED ROULETTE 2"))
                return False
            else:
                cat_label = f"{'DOCENA' if type_str=='DOCENA' else 'COLUMNA'} {val_num}"
                self.stat_losses += 1
                if self.name == "SPEED ROULETTE 2":
                    GLOBAL_STATS.record('LOSS', 2, number, val_num, type_str, self.name)
                    tg_send(f"❌ LOSS {number} — {cat_label} — 🔄 GALE #{gale_num}\n"
                            f"🚨 Total perdido: -{fmt_cur(self.total_signal_loss,'USD')} 🚨\n"
                            f"💰 Balance: {fmt_cur(GLOBAL_STATS.global_balance,'USD')}")
                self.gestor.registrar_perdida_senal()
                queue_broadcast(self._build_result_ws("LOSS", number, gale_num,
                                                       -self.total_signal_loss, cat_label))
                if self.name == "SPEED ROULETTE 2": self._check_stats()
                self._reset_signal(); return True

    def _check_stats(self):
        if not GLOBAL_STATS.should_send(): return
        tg_send_stats(GLOBAL_STATS.get_stats_text()); GLOBAL_STATS.mark_sent()

    def _reset_signal(self):
        self.signal_active = False; self.active_pair = ()
        self.active_type = None; self.total_signal_loss = 0.0
        self.active_signal_msg_id = None; self._last_signal_prob = 0.0

    # ── FEED NUMBER ───────────────────────────────────────────────────────────
    def feed_number(self, number: int, active: bool = False):
        try: self._feed_inner(number, active)
        except Exception as e:
            logger.error(f"[{self.name}] feed_number error: {e}", exc_info=True)
            self._reset_signal()

    def _feed_inner(self, number: int, active: bool = False):
        color  = REAL_COLOR_MAP.get(number, "VERDE")
        d      = get_dozen(number); c = get_column(number)
        tag    = "🟢 ACTIVA" if active else "⚫ pasiva"
        spin_n = len(self.spin_history) + 1
        self._update_state(number)
        if not self.warmup_done:
            self.ws_count += 1
            if self.ws_count >= WARMUP_SPINS:
                self.warmup_done = True
                if self.name == "SPEED ROULETTE 2":
                    tg_send(f"🟢 <b>{self.name}</b> — Sistema PF+PH+ML Listo.")
        logger.info(f"[{self.name}] #{spin_n:>4} | {number:>2} {color:<5} D{d} C{c} "
                    f"| {tag} | {'✔' if self.warmup_done else f'warmup {self.ws_count}/{WARMUP_SPINS}'} "
                    f"| Nv{self.gestor.nivel} | 💰{self.balance:.2f}")
        last20 = self.spin_history[-20:]
        queue_broadcast({
            "type":          "spin",
            "ws_key":        self.ws_key,
            "roulette":      self.name,
            "number":        number,
            "color":         color,
            "dozen":         d,
            "column":        c,
            "spin_count":    len(self.spin_history),
            "warmup_done":   self.warmup_done,
            "signal_active": self.signal_active,
            "balance":       round(self.balance, 2),
            "nivel":         self.gestor.nivel,
            "wins":          self.stat_wins,
            "losses":        self.stat_losses,
            "zeros":         self.stat_zeros,
            "debt_count":    len(self.gestor.debt_stack),
            "last20": [{"number": s["number"], "color": s["color"],
                        "dozen": get_dozen(s["number"]), "column": get_column(s["number"])}
                       for s in last20],
        })

# ─── SESSION MANAGER ─────────────────────────────────────────────────────────
class SessionManager:
    ARG_UTC_OFFSET = -3

    def __init__(self):
        self.engines: dict = {r["key"]: RouletteEngine(r["key"], r["name"])
                              for r in ROULETTES}
        self.session_start = 0.0; self.session_active = False
        self.signals_this_session = 0
        self.prev_start_msg_id: Optional[int] = None
        self.prev_end_msg_id:   Optional[int] = None
        logger.info("[SessionManager] Iniciado — 3 ruletas | Speed2=TG | 30min | 3 señales | 6 niveles")

    def _now_arg(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=self.ARG_UTC_OFFSET)

    def seconds_to_next_slot(self) -> float:
        now = self._now_arg()
        if now.second <= 5 and now.minute in (0, 30): return 0.0
        target = now.replace(minute=30, second=0, microsecond=0) if now.minute < 30 else \
                 (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return max(0.0, (target - now).total_seconds())

    def _tg_engine(self) -> RouletteEngine:
        return self.engines[205]

    def _start_session(self):
        self.session_start = time.time(); self.session_active = True
        self.signals_this_session = 0
        engine  = self._tg_engine()
        now_str = self._now_arg().strftime("%H:%M")
        end_str = (self._now_arg() + datetime.timedelta(minutes=25)).strftime("%H:%M")
        logger.info(f"[SessionManager] 🟢 Sesión: {engine.name} | {now_str}–{end_str}")
        if self.prev_start_msg_id: tg_delete(CHAT_ID, self.prev_start_msg_id)
        self.prev_start_msg_id = tg_send(f"🔔 SESION INICIADA — {engine.name} 🔔")
        queue_broadcast({"type":"session","status":"started","ws_key":engine.ws_key,
                         "roulette":engine.name,"elapsed":0,"remaining":SESSION_ACTIVE,
                         "signals_count":0,"max_signals":MAX_SIGNALS,
                         "nivel":engine.gestor.nivel,"debt_count":len(engine.gestor.debt_stack)})

    def _end_session(self):
        engine = self._tg_engine(); self.session_active = False
        if self.prev_end_msg_id: tg_delete(CHAT_ID, self.prev_end_msg_id)
        text = (f"⏸ SESIÓN CERRADA — {engine.name}\n"
                f"🎰 PRÓXIMA RULETA — {engine.name} 🎰\n\n"
                f"💵 ¿COMO OPERAR LAS SEÑALES?\n\n"
                f"1° Op. = $0.50 USD x Docena/Columna\n"
                f"2° Op. = $1.50 USD x Docena/Columna\n\n"
                f"🎯 FUNCIONAMIENTO DE LAS SEÑALES 🎯\n\n"
                f"  • Se envían las señales → Se resuelven\n"
                f"  • Sesión se cierra → Ej: 12:25 o 12:55\n"
                f"  • Nueva Sesión → Ej: 15:00 o 15:30\n\n"
                f"♦️ POR SESION SE ENVÍAN 3 SEÑALES MÁXIMAS ♦️")
        self.prev_end_msg_id = tg_send_with_button(text, engine.name)
        queue_broadcast({"type":"session","status":"ended","ws_key":engine.ws_key,
                         "roulette":engine.name,"elapsed":SESSION_ACTIVE,"remaining":0,
                         "signals_count":self.signals_this_session,"max_signals":MAX_SIGNALS,
                         "nivel":engine.gestor.nivel,"debt_count":len(engine.gestor.debt_stack)})

    async def session_watchdog(self):
        wait = self.seconds_to_next_slot()
        logger.info(f"[SessionManager] ⏳ Esperando {wait/60:.1f} min al primer slot...")
        await asyncio.sleep(wait)
        self._start_session()
        _waiting_signal_since: Optional[float] = None
        _last_tick = 0.0

        while True:
            await asyncio.sleep(1)
            now = time.time(); elapsed = now - self.session_start
            engine = self._tg_engine()

            if self.session_active:
                remaining = max(0, SESSION_ACTIVE - elapsed)
                if now - _last_tick >= 5:
                    _last_tick = now
                    mins, secs = divmod(int(remaining), 60)
                    queue_broadcast({"type":"timer","status":"active","ws_key":engine.ws_key,
                                     "roulette":engine.name,"remaining":remaining,
                                     "text":f"{mins:02d}:{secs:02d}"})
                if elapsed >= SESSION_ACTIVE:
                    if engine.signal_active:
                        if _waiting_signal_since is None: _waiting_signal_since = now
                        elif now - _waiting_signal_since >= SIGNAL_WAIT_TIMEOUT:
                            if engine.active_signal_msg_id:
                                tg_edit(CHAT_ID, engine.active_signal_msg_id,
                                        engine._build_signal_text() +
                                        "\n\n⚠️ Señal cancelada — tiempo agotado.")
                            engine._reset_signal(); _waiting_signal_since = None
                        else: continue
                    else: _waiting_signal_since = None
                    end_time = time.time(); self._end_session()
                    pause_rem = SESSION_TOTAL - (end_time - self.session_start)
                    if pause_rem > 0:
                        ps = time.time()
                        while True:
                            await asyncio.sleep(1)
                            pe = time.time() - ps; pr = max(0, pause_rem - pe)
                            if time.time() - _last_tick >= 5:
                                _last_tick = time.time()
                                pm, psc = divmod(int(pr), 60)
                                queue_broadcast({"type":"timer","status":"paused",
                                                 "ws_key":engine.ws_key,"roulette":engine.name,
                                                 "remaining":pr,"text":f"{pm:02d}:{psc:02d}"})
                            if pe >= pause_rem: break
                    self._start_session()
            else:
                if elapsed >= SESSION_TOTAL: self._start_session()

    def on_number(self, ws_key: int, number: int):
        engine = self.engines.get(ws_key)
        if not engine: return
        is_active = (ws_key == 205)
        engine.feed_number(number, active=is_active)
        if engine.signal_active: engine.resolve(number); return
        if engine.warmup_done:
            sig = engine.detect_signal()
            if sig:
                if is_active:
                    if self.session_active and self.signals_this_session < MAX_SIGNALS:
                        logger.info(f"[SessionManager] 🎯 Señal #{self.signals_this_session+1} "
                                    f"{engine.name}: {sig}")
                        engine.iniciar_senal(sig, send_tg=True)
                        self.signals_this_session += 1
                else:
                    logger.info(f"[SessionManager] 📡 Señal HTML {engine.name}: {sig}")
                    engine.iniciar_senal(sig, send_tg=False)

    def _advance_session(self):
        self._end_session()
        self.session_start = time.time(); self.session_active = True
        self.signals_this_session = 0

# ─── WS READER ────────────────────────────────────────────────────────────────
async def ws_reader(ws_key: int, session_mgr: SessionManager):
    reconnect_delay = 5; initial_loaded = False
    seen_ids: set = set(); seen_ids_q: deque = deque(maxlen=200)

    def is_new_id(gid: str) -> bool:
        if not gid or gid in seen_ids: return False
        if len(seen_ids_q) == seen_ids_q.maxlen: seen_ids.discard(seen_ids_q[0])
        seen_ids.add(gid); seen_ids_q.append(gid); return True

    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=40, close_timeout=10) as ws:
                await ws.send(json.dumps({"type":"subscribe","key":ws_key,"casinoId":CASINO_ID}))
                logger.info(f"[WS-{ws_key}] ✅ Conectado"); reconnect_delay = 5

                async def poll_1s():
                    while True:
                        await asyncio.sleep(1)
                        try: await ws.send(json.dumps({"type":"subscribe","key":ws_key,"casinoId":CASINO_ID}))
                        except: break

                poll_task = asyncio.create_task(poll_1s())
                try:
                    async for raw in ws:
                        try: data = json.loads(raw)
                        except: continue
                        if not isinstance(data, dict): continue
                        results = data.get("last20Results")
                        if results and isinstance(results, list):
                            if not initial_loaded:
                                initial_loaded = True
                                engine = session_mgr.engines.get(ws_key); loaded = 0
                                if engine:
                                    for item in reversed(results):
                                        gid = str(item.get("gameId",""))
                                        if gid:
                                            if len(seen_ids_q)==seen_ids_q.maxlen: seen_ids.discard(seen_ids_q[0])
                                            seen_ids.add(gid); seen_ids_q.append(gid)
                                        try: n = int(item.get("result",""))
                                        except: continue
                                        if 0 <= n <= 36: engine._update_state(n, persist=False, train_model=True); loaded += 1
                                    engine._train_models()
                                    if not engine.warmup_done and len(engine.spin_history) >= WARMUP_SPINS:
                                        engine.warmup_done = True; engine.ws_count = len(engine.spin_history)
                                    logger.info(f"[WS-{ws_key}] 📦 {loaded} giros | "
                                                f"Hist:{len(engine.spin_history)} | "
                                                f"Warmup:{'✅' if engine.warmup_done else '⏳'}")
                                    last20 = engine.spin_history[-20:]
                                    queue_broadcast({
                                        "type":"spin","ws_key":ws_key,"roulette":engine.name,
                                        "number":last20[-1]["number"] if last20 else -1,
                                        "color":last20[-1]["color"] if last20 else "",
                                        "dozen":get_dozen(last20[-1]["number"]) if last20 else 0,
                                        "column":get_column(last20[-1]["number"]) if last20 else 0,
                                        "spin_count":len(engine.spin_history),
                                        "warmup_done":engine.warmup_done,"signal_active":engine.signal_active,
                                        "balance":round(engine.balance,2),"nivel":engine.gestor.nivel,
                                        "wins":engine.stat_wins,"losses":engine.stat_losses,"zeros":engine.stat_zeros,
                                        "debt_count":len(engine.gestor.debt_stack),
                                        "last20":[{"number":s["number"],"color":s["color"],
                                                   "dozen":get_dozen(s["number"]),"column":get_column(s["number"])}
                                                  for s in last20],
                                    })
                                continue
                            latest = results[0]; gid = str(latest.get("gameId",""))
                            if not is_new_id(gid): continue
                            try: n = int(latest.get("result",""))
                            except: continue
                            if 0 <= n <= 36: session_mgr.on_number(ws_key, n)
                            continue
                        fb = str(data.get("gameId","")).strip()
                        if not fb:
                            for k in ("result","number","outcome","winningNumber"):
                                if k in data: fb = f"{ws_key}_{data[k]}_{int(time.time())}"; break
                        if not fb or not is_new_id(fb): continue
                        for k in ("result","number","outcome","winningNumber"):
                            if k in data:
                                try:
                                    n = int(data[k])
                                    if 0 <= n <= 36: session_mgr.on_number(ws_key, n)
                                except: pass
                                break
                finally:
                    poll_task.cancel()
                    try: await poll_task
                    except asyncio.CancelledError: pass
        except Exception as e:
            logger.warning(f"[WS-{ws_key}] Desconectado: {e}. Reconectando en {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay); reconnect_delay = min(reconnect_delay*2, 60)

# ─── AIOHTTP SERVER ───────────────────────────────────────────────────────────
session_mgr_global: Optional[SessionManager] = None

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        r = web.Response()
        r.headers["Access-Control-Allow-Origin"]  = "*"
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return r
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

async def ws_html_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    q = asyncio.Queue(maxsize=200); _ws_clients.add(q)
    logger.info(f"[WS-Server] 📡 Cliente: {request.remote} | Total:{len(_ws_clients)}")
    try:
        if session_mgr_global:
            tge   = session_mgr_global._tg_engine()
            last20= tge.spin_history[-20:]
            elapsed = int(time.time() - session_mgr_global.session_start)
            remaining = max(0, SESSION_ACTIVE - elapsed)
            sig_data = None
            if tge.signal_active:
                bet = tge.gestor.apostar_por_docena()
                bt  = {}
                for ck, info in CURRENCIES.items():
                    b = round(bet*info["mult"]/info["round"])*info["round"]
                    z = round(b*0.1/info["round"])*info["round"]
                    bt[ck] = {"bet":b,"zero":z}
                sig_data = {"active":True,"type":tge.active_type,"pair":list(tge.active_pair),
                            "pair_nums":[p[1:] for p in tge.active_pair],"missing":tge.active_missing,
                            "prob":round(tge._last_signal_prob,4),"attempt":tge.gestor.oportunidad,
                            "gale":tge.gestor.oportunidad-1,"bet_per_cat":round(bet,2),
                            "total_bet":round(bet*2,2),"bet_table":bt}
            total = GLOBAL_STATS.wins + GLOBAL_STATS.zeros + GLOBAL_STATS.losses
            eff   = ((GLOBAL_STATS.wins+GLOBAL_STATS.zeros)/total*100) if total > 0 else 0.0
            # Historial de color por ruleta (últimos 200 giros)
            color_history = {}
            for rk, eng in session_mgr_global.engines.items():
                hist = eng.spin_history[-200:]
                color_history[rk] = [{"number":s["number"],"color":s["color"],
                                       "dozen":get_dozen(s["number"]),"column":get_column(s["number"])}
                                     for s in hist]
            await ws.send_json({
                "type":"init","ws_key":tge.ws_key,"roulette":tge.name,
                "spins":[{"number":s["number"],"color":s["color"],
                           "dozen":get_dozen(s["number"]),"column":get_column(s["number"])}
                          for s in last20],
                "signal":sig_data,
                "session":{"active":session_mgr_global.session_active,"elapsed":elapsed,
                           "remaining":remaining,"signals_count":session_mgr_global.signals_this_session,
                           "max_signals":MAX_SIGNALS},
                "balance":round(tge.balance,2),"nivel":tge.gestor.nivel,
                "wins":tge.stat_wins,"losses":tge.stat_losses,"zeros":tge.stat_zeros,
                "debt_count":len(tge.gestor.debt_stack),
                "color_history": color_history,
                "stats":{"wins":GLOBAL_STATS.wins,"losses":GLOBAL_STATS.losses,
                         "empates":GLOBAL_STATS.zeros,"consecutive":GLOBAL_STATS.consecutive,
                         "assertiveness":round(eff,2),"total_signals":total},
            })
        while True:
            data = await q.get()
            try: await ws.send_json(data)
            except: break
    finally:
        _ws_clients.discard(q)
        logger.info(f"[WS-Server] 📡 Desconectado | Total:{len(_ws_clients)}")
    return ws

async def health_handler(request):
    if not session_mgr_global:
        return web.json_response({"status":"initializing"})
    tge = session_mgr_global._tg_engine()
    elapsed = int(time.time() - session_mgr_global.session_start)
    return web.json_response({
        "status":"ok","active_roulette":tge.name,
        "session_elapsed_s":elapsed,"session_remaining_s":max(0,SESSION_ACTIVE-elapsed),
        "signals_this_session":session_mgr_global.signals_this_session,"max_signals":MAX_SIGNALS,
        "signal_active":tge.signal_active,"ws_clients":len(_ws_clients),
        "nivel":tge.gestor.nivel,"debt_count":len(tge.gestor.debt_stack),
        "balance":round(tge.balance,2),
        "engines":[{"name":e.name,"ws_key":e.ws_key,"spins":len(e.spin_history),
                    "warmup":e.warmup_done,"balance":round(e.balance,2),
                    "nivel":e.gestor.nivel,"debt_count":len(e.gestor.debt_stack)}
                   for e in session_mgr_global.engines.values()],
    })

# ─── KEEP-ALIVE ───────────────────────────────────────────────────────────────
async def self_ping():
    url = os.environ.get("RENDER_EXTERNAL_URL","").rstrip("/")
    if not url: logger.info("[KeepAlive] Sin RENDER_EXTERNAL_URL — desactivado."); return
    logger.info(f"[KeepAlive] Ping cada 10min → {url}/health")
    while True:
        await asyncio.sleep(600)
        try:
            async with ClientSession() as s:
                async with s.get(f"{url}/health", timeout=15) as r:
                    logger.info(f"[KeepAlive] Status:{r.status}")
        except Exception as e: logger.error(f"[KeepAlive] ❌ {e}")

# ─── REPORTE DIARIO ───────────────────────────────────────────────────────────
async def daily_stats_loop():
    while True:
        now_arg = datetime.datetime.utcnow() + datetime.timedelta(hours=-3)
        target  = now_arg.replace(hour=12, minute=0, second=0, microsecond=0)
        if now_arg >= target: target += datetime.timedelta(days=1)
        await asyncio.sleep((target - now_arg).total_seconds())
        tg_send_stats(GLOBAL_STATS.get_stats_text())
        logger.info("[Stats] ✅ Reporte diario enviado.")

# ─── COMANDOS TELEGRAM ────────────────────────────────────────────────────────
@bot.message_handler(commands=['start','help'])
def cmd_start(m):
    bot.reply_to(m,
        "<b>🎰 Speed Roulette DC — Docenas y Columnas</b>\n\n"
        "3 ruletas | Speed 2 = señales TG | 6 niveles\n"
        "Máx 3 señales/sesión | 30 min ciclo\n\n"
        "/status    — Estado actual\n"
        "/stats     — Ver estadísticas\n"
        "/reset     — Resetear stats y balance\n"
        "/siguiente — Forzar nueva sesión", parse_mode="HTML")

@bot.message_handler(commands=['status'])
def cmd_status(m):
    if not session_mgr_global: return
    tge = session_mgr_global._tg_engine()
    elapsed = int(time.time() - session_mgr_global.session_start)
    rem_min = max(0, SESSION_ACTIVE - elapsed) // 60
    st = f"🟢 {tge.active_pair} ({tge.active_type})" if tge.signal_active else "⚪ Esperando"
    bot.reply_to(m,
        f"<b>Ruleta:</b> {tge.name}\n<b>Señal:</b> {st}\n"
        f"<b>Sesión:</b> {'Activa' if session_mgr_global.session_active else 'Pausa'} | {rem_min}min\n"
        f"<b>Señales:</b> {session_mgr_global.signals_this_session}/{MAX_SIGNALS}\n"
        f"<b>Nivel:</b> {tge.gestor.nivel}/{MAX_NIVEL} | <b>Deudas:</b> {len(tge.gestor.debt_stack)}\n"
        f"<b>Balance:</b> {fmt_cur(GLOBAL_STATS.global_balance,'USD')}\n"
        f"<b>Clientes HTML:</b> {len(_ws_clients)}", parse_mode="HTML")

@bot.message_handler(commands=['stats'])
def cmd_stats(m): tg_send_stats(GLOBAL_STATS.get_stats_text())

@bot.message_handler(commands=['reset'])
def cmd_reset(m):
    if not session_mgr_global: return
    global GLOBAL_STATS; GLOBAL_STATS = GlobalStats()
    tge = session_mgr_global._tg_engine()
    tge.balance = 0.0; tge.gestor.nivel = 1; tge.gestor.debt_stack = []
    tge.stat_wins = tge.stat_losses = tge.stat_zeros = 0
    bot.reply_to(m, "🔄 <b>Resetado — Balance: $0.00 | Nivel: 1</b>", parse_mode="HTML")

@bot.message_handler(commands=['siguiente'])
def cmd_siguiente(m):
    if not session_mgr_global: return
    session_mgr_global._advance_session()
    bot.reply_to(m, f"🔄 Sesión reiniciada — <b>{session_mgr_global._tg_engine().name}</b>",
                 parse_mode="HTML")

# ─── STARTUP ──────────────────────────────────────────────────────────────────
async def background_tasks(app):
    global session_mgr_global
    session_mgr_global = SessionManager()
    threading.Thread(target=lambda: bot.polling(none_stop=True, interval=1, timeout=30),
                     daemon=True).start()
    logger.info("[Main] ✅ Telegram bot thread iniciado")
    for r in ROULETTES:
        asyncio.create_task(ws_reader(r["key"], session_mgr_global))
    asyncio.create_task(session_mgr_global.session_watchdog())
    asyncio.create_task(self_ping())
    asyncio.create_task(daily_stats_loop())
    logger.info(f"[Main] 🚀 {len(ROULETTES)} ruletas | Speed2=TG | WS en /ws")

app = web.Application(middlewares=[cors_middleware])
app.router.add_get('/ws',     ws_html_handler)
app.router.add_get('/',       health_handler)
app.router.add_get('/health', health_handler)
app.router.add_get('/ping',   health_handler)
app.on_startup.append(background_tasks)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"[Server] 🚀 Puerto {port}")
    web.run_app(app, host="0.0.0.0", port=port)
