#!/usr/bin/env python3
"""
Speed Roulette DC — Bot de señales para Docenas y Columnas
Sistema PF + PH + ML Cruzado + Gestión (6 niveles, 2 oportunidades)
+ Sesiones de 30 min + WebSocket Server para HTML + Gestión del Cero (10%)
+ Monitoreo 3 Ruletas (Telegram solo Speed 2, HTML todas) + Keep-Alive
"""

import asyncio
import json
import logging
import os
import sqlite3
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s [SpeedDC] %(levelname)s %(message)s')
logger = logging.getLogger("SpeedDC")
for _ln in ['werkzeug', 'flask.app', 'flask', 'urllib3', 'telegram', 'aiohttp.access']:
    logging.getLogger(_ln).setLevel(logging.ERROR)

# ─── TELEGRAM ROBUSTO ────────────────────────────────────────────────────────
TOKEN           = "8615799238:AAG2kLg-Ostc4Y4E98HXDIoje_U4F7oqdzU"
CHAT_ID         = -1003821352139
STATS_THREAD_ID = 40034

_session = requests.Session()
_retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"], raise_on_status=False)
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))
_session.mount("http://",  HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20))
bot = telebot.TeleBot(TOKEN, threaded=False)
bot.session = _session

# ─── RULETA CONFIGURACIÓN ────────────────────────────────────────────────────
ROULETTES = [
    {"key": 203, "name": "SPEED ROULETTE 1"},
    {"key": 205, "name": "SPEED ROULETTE 2"},
    {"key": 206, "name": "ROULETTE MACAO"},
]
ROULETTE_LINKS = {
    "SPEED ROULETTE 1": "https://1win.lat/casino/play/v_pragmatic:speedroulette1",
    "SPEED ROULETTE 2": "https://1win.lat/casino/play/v_pragmatic:speedroulette2",
    "ROULETTE MACAO":   "https://1win.lat/casino/play/v_pragmatic:roulettemacao"
}

WS_URL              = "wss://dga.pragmaticplaylive.net/ws"
CASINO_ID           = "ppcjd00000007254"
SESSION_ACTIVE      = 25 * 60
SESSION_PAUSE       = 5  * 60
SESSION_TOTAL       = SESSION_ACTIVE + SESSION_PAUSE
BASE_BET            = 0.50
MAX_NIVEL           = 6
WARMUP_SPINS        = 25
MIN_PROB            = 0.78
TRAIN_INTERVAL      = 100
MAX_SIGNALS         = 3
SIGNAL_WAIT_TIMEOUT = 120

REAL_COLOR_MAP = {0:"VERDE",1:"ROJO",2:"NEGRO",3:"ROJO",4:"NEGRO",5:"ROJO",6:"NEGRO",7:"ROJO",8:"NEGRO",9:"ROJO",10:"NEGRO",11:"NEGRO",12:"ROJO",13:"NEGRO",14:"ROJO",15:"NEGRO",16:"ROJO",17:"NEGRO",18:"ROJO",19:"ROJO",20:"NEGRO",21:"ROJO",22:"NEGRO",23:"ROJO",24:"NEGRO",25:"ROJO",26:"NEGRO",27:"ROJO",28:"NEGRO",29:"NEGRO",30:"ROJO",31:"NEGRO",32:"ROJO",33:"NEGRO",34:"ROJO",35:"NEGRO",36:"ROJO"}

def get_dozen(n: int) -> int: return 0 if n == 0 else (n - 1) // 12 + 1
def get_column(n: int) -> int: return 0 if n == 0 else ((n - 1) % 3) + 1

_ws_clients: Set[asyncio.Queue] = set()
def queue_broadcast(data: dict):
    for q in list(_ws_clients):
        try: q.put_nowait(data)
        except: pass

# ─── TELEGRAM HELPERS ORIGINALES ─────────────────────────────────────────────
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
    msg = _tg_call(bot.send_message, chat_id=CHAT_ID, text=text, parse_mode="HTML", message_thread_id=STATS_THREAD_ID)
    return msg.message_id if msg else None

def tg_delete(chat_id: int, message_id: int):
    try: _tg_call(bot.delete_message, chat_id=chat_id, message_id=message_id)
    except: pass

def tg_edit(chat_id: int, message_id: int, text: str):
    try: _tg_call(bot.edit_message_text, text=text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
    except: pass

def get_roulette_url(name: str) -> Optional[str]:
    clean = name.upper().strip()
    for key, url in ROULETTE_LINKS.items():
        if key.upper() in clean or clean in key.upper(): return url
    return None

def tg_send_with_button(text: str, roulette_name: str) -> Optional[int]:
    url = get_roulette_url(roulette_name)
    if not url: return tg_send(text)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("🎰 ACCEDER A LA RULETA", url=url))
    msg = _tg_call(bot.send_message, chat_id=CHAT_ID, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    return msg.message_id if msg else None

# ─── EMA ──────────────────────────────────────────────────────────────────────
def calc_ema(data: list, period: int) -> list:
    if len(data) < period: return [None] * len(data)
    mult = 2 / (period + 1); out = [None] * (period - 1); prev = sum(data[:period]) / period; out.append(prev)
    for v in data[period:]: prev = v * mult + prev * (1 - mult); out.append(prev)
    return out

def ema_signal(levels: list, mode: str = "moderado") -> bool:
    if len(levels) < 20: return False
    e4, e8, e20 = calc_ema(levels, 4), calc_ema(levels, 8), calc_ema(levels, 20); li = len(levels) - 1
    if any(v is None for v in [e4[li], e8[li], e20[li]]): return False
    cur = levels[li]; ce4, ce8, ce20 = e4[li], e8[li], e20[li]
    pe4  = e4[li-1]  if li > 0 and e4[li-1]  is not None else ce4
    pe8  = e8[li-1]  if li > 0 and e8[li-1]  is not None else ce8
    pe20 = e20[li-1] if li > 0 and e20[li-1] is not None else ce20
    if mode == "tendencia": return (pe4 <= pe20 and ce4 > ce20) or (cur > ce4 and cur > ce8 and cur > ce20)
    else:
        v_pattern = False
        if len(levels) >= 3: a, b, c = levels[-3], levels[-2], levels[-1]; v_pattern = (b < a) and (b < c) and (c > a)
        return (pe4 <= pe8 and ce4 > ce8) or (pe8 <= pe20 and ce8 > ce20) or (cur > ce4 and cur > ce8) or v_pattern

# ─── MARKOV & ML COMPLETO ────────────────────────────────────────────────────
class SmoothedMarkovPredictor:
    def __init__(self, window=60, order=2): self.window=window;self.order=order;self.tc={}
    def update(self, seq):
        self.tc=defaultdict(lambda:defaultdict(int)); r=seq[-self.window:]
        if len(r)>self.order:
            for i in range(len(r)-self.order): self.tc[tuple(r[i:i+self.order])][r[i+self.order]]+=1
    def predict(self, seq):
        if len(seq)<self.order: return None; s=tuple(seq[-self.order:]); c=dict(self.tc.get(s,{})); t=sum(c.values())
        if t<10: return None; a=2.0; vs=3; p={k:(v+a)/(t+a*vs) for k,v in c.items()}
        for x in [1,2,3]: p.setdefault(x, a/(t+a*vs))
        return p

class OnlineEnsemblePredictor:
    W=5; C=[1,2,3]
    def __init__(self): self.mnb=MultinomialNB(alpha=2.0,class_prior=[0.333,0.333,0.333]);self.sgd=SGDClassifier(loss='log_loss',learning_rate='adaptive',eta0=0.005,penalty='l2',alpha=0.01,epsilon=0.2);self.trained=False
    def _ext(self, hd, hc, pfd, phd, pfc, phc):
        if len(hd)<self.W or len(hc)<self.W: return None; f=[]
        for i in range(1,self.W+1): d=hd[-i];c=hc[-i];v=[0]*9;v[(d-1)*3+(c-1)]=1;f.extend(v)
        for p in (pfd,phd,pfc,phc): v=[0,0,0];[v.__setitem__(x-1,1) for x in p];f.extend(v)
        return f
    def partial_train(self, hd, hc, t, pfd, phd, pfc, phc):
        ft=self._ext(hd[:-1],hc[:-1],pfd,phd,pfc,phc);
        if ft is None: return; X=np.array(ft).reshape(1,-1);y=np.array([t])
        if not self.trained: self.mnb.partial_fit(X,y,classes=self.C);self.sgd.partial_fit(X,y,classes=self.C);self.trained=True
        else: self.mnb.partial_fit(X,y);self.sgd.partial_fit(X,y)
    def predict(self, hd, hc, pfd, phd, pfc, phc):
        if not self.trained: return None; ft=self._ext(hd,hc,pfd,phd,pfc,phc);
        if ft is None: return None; X=np.array(ft).reshape(1,-1)
        try: return {c+1:float(p) for c,p in enumerate(0.5*self.mnb.predict_proba(X)[0]+0.5*self.sgd.predict_proba(X)[0])}
        except: return None

# ─── GESTOR ORIGINAL ─────────────────────────────────────────────────────────
class GestorDocenas:
    def __init__(self): self.nivel=1;self.oportunidad=1;self.max_nivel=MAX_NIVEL;self.b0=0.0;self.debt_stack=[]
    def iniciar_senal(self, bal): self.b0=bal;self.oportunidad=1
    def get_target(self) -> float:
        net_base = BASE_BET * 0.9
        if self.debt_stack: return self.debt_stack[-1] + net_base
        return self.b0 + net_base
    def apostar_por_docena(self, bal): return self.nivel*BASE_BET if self.oportunidad==1 else 3*self.nivel*BASE_BET
    def registrar_perdida_senal(self): self.debt_stack.append(self.b0); self.nivel = self.nivel+1 if self.nivel<self.max_nivel else 1; logger.info(f"[Gestor] 📋 Deuda: B0={self.b0:.2f} | Pila: {len(self.debt_stack)} | Nivel→{self.nivel}")
    def verificar_recuperacion(self, bal):
        while self.debt_stack:
            if bal >= self.get_target(): self.debt_stack.pop(); logger.info(f"[Gestor] ✅ Deuda recuperada | Pila: {len(self.debt_stack)}")
            else: break
        if not self.debt_stack: self.nivel=1; logger.info("[Gestor] 🏆 Sin deudas — Nivel 1")

# ─── STATS GLOBAL ORIGINAL ───────────────────────────────────────────────────
class GlobalStats:
    def __init__(self): self.wins=0;self.zeros=0;self.losses=0;self.consecutive=0;self.last_20=deque(maxlen=20);self.signals_processed=0;self.global_bankroll=0.0;self.last_report_signals=0
    def record(self, rt, att, num, val, ts, rn):
        self.signals_processed+=1
        if rt=='WIN': self.wins+=1;self.consecutive+=1
        elif rt=='LOSS': self.losses+=1;self.consecutive=0
        elif rt=='EMPATE': self.zeros+=1
        self.last_20.append({"result":rt,"attempt":att,"number":num,"val":val,"type":ts,"roulette":rn,"balance":self.global_bankroll})
    def should_send(self) -> bool: return (self.signals_processed-self.last_report_signals)>=20
    def mark_sent(self): self.last_report_signals=self.signals_processed
    def get_stats_text(self) -> str:
        total=self.wins+self.zeros+self.losses; eff=((self.wins+self.zeros)/total*100) if total>0 else 0.0
        text="📊 RESUMEN DIARIO — SPEED ROULETTE 2 📊\n🕛 Reporte 12:00 hs (Argentina)\n\n"
        text+=f"► PLACAR = ✅{self.wins} | 🟠{self.zeros} | 🚫{self.losses}\n► Consecutivas = {self.consecutive}\n► Assertividade = {eff:.2f}%\n► Balance total: 💰 {self.global_bankroll:.2f}\n► Total señales: {total}\n\n📌 Últimas 20 SEÑALES 📌\n"
        for s in reversed(list(self.last_20)):
            a_str=f"🔄 GALE #{s['attempt']}"; b_str=f"💰 {s['balance']:.2f}"; rl=s['roulette'][:14]
            if s['result']=='WIN': text+=f"✅ WIN #{s['number']} {s['type']} {s['val']} | {rl} | {a_str} | {b_str}\n"
            elif s['result']=='EMPATE': text+=f"🟠 EMPATE #0 ZERO | {rl} | {a_str} | {b_str}\n"
            else: text+=f"🚫 LOSS #{s['number']} {s['type']} {s['val']} | {rl} | {a_str} | {b_str}\n"
        return text

GLOBAL_STATS = GlobalStats()

# ─── ENGINE COMPLETO ─────────────────────────────────────────────────────────
class RouletteEngine:
    def __init__(self, ws_key, name):
        self.ws_key=ws_key; self.name=name; self.db_path=f"main_roulette_{ws_key}.db"
        self.spin_history=[]; self.dozen_seq=[]; self.column_seq=[]; self.d_levels={1:[],2:[],3:[]}; self.c_levels={1:[],2:[],3:[]}
        self.markov_d=SmoothedMarkovPredictor(); self.markov_c=SmoothedMarkovPredictor()
        self.ensemble_d=OnlineEnsemblePredictor(); self.ensemble_c=OnlineEnsemblePredictor()
        self.after_number_dozen=defaultdict(lambda:defaultdict(int)); self.after_number_column=defaultdict(lambda:defaultdict(int))
        self.signal_active=False; self.active_type=None; self.active_pair=(); self.active_missing=""
        self._last_signal_prob=0.0; self.gestor=GestorDocenas(); self.total_signal_loss=0.0; self.bankroll=0.0
        self.active_signal_msg_id=None; self.spins_since_train=0; self.ws_count=0; self.warmup_done=False
        self._db=self._get_db(); live=self._load_live_history(); self.ws_count=live; self.warmup_done=live>=WARMUP_SPINS
        logger.info(f"[{name}] Pre-cargados: {live} giros | Warmup: {'✅' if self.warmup_done else '⏳'}")

    def _get_db(self): conn=sqlite3.connect(self.db_path,check_same_thread=False); conn.execute("CREATE TABLE IF NOT EXISTS live_spins (id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL, ts INTEGER NOT NULL)"); conn.commit(); return conn
    def _persist(self, n):
        try:
            self._db.execute("INSERT INTO live_spins(number,ts) VALUES(?,?)", (n, int(time.time())))
            self._db.commit()
        except:
            pass
    def _load_live_history(self):
        try: rows=self._db.execute("SELECT number FROM live_spins ORDER BY id ASC").fetchall()
        except: return 0
        for (n,) in rows: self._update_state(n, persist=False, train_model=False)
        if rows: self._train_models()
        return len(rows)
    def _train_models(self): self.markov_d.update(self.dozen_seq); self.markov_c.update(self.column_seq)

    def _update_state(self, number, persist=True, train_model=True):
        color=REAL_COLOR_MAP.get(number,"VERDE"); d=get_dozen(number); c=get_column(number)
        if number!=0 and self.spin_history:
            prev=self.spin_history[-1]["number"]
            if prev!=0: self.after_number_dozen[prev][d]+=1;self.after_number_column[prev][c]+=1
        self.spin_history.append({"number":number,"color":color})
        if d!=0: self.dozen_seq.append(d); [self.d_levels[dd].append((self.d_levels[dd][-1] if self.d_levels[dd] else 0)+(1 if d==dd else -1)) for dd in (1,2,3)]
        if c!=0: self.column_seq.append(c); [self.c_levels[cc].append((self.c_levels[cc][-1] if self.c_levels[cc] else 0)+(1 if c==cc else -1)) for cc in (1,2,3)]
        if train_model and d!=0 and c!=0 and len(self.dozen_seq)>5:
            pf_d,ph_d=self._get_pf("DOCENA"),self._get_ph("DOCENA");pf_c,ph_c=self._get_pf("COLUMNA"),self._get_ph("COLUMNA")
            if pf_d and ph_d and pf_c and ph_c: self.ensemble_d.partial_train(self.dozen_seq,self.column_seq,d,pf_d["pair"],ph_d["pair"],pf_c["pair"],ph_c["pair"]);self.ensemble_c.partial_train(self.dozen_seq,self.column_seq,c,pf_d["pair"],ph_d["pair"],pf_c["pair"],ph_c["pair"])
            self.spins_since_train+=1
            if self.spins_since_train>=TRAIN_INTERVAL: self._train_models();self.spins_since_train=0; logger.info(f"[{self.name}] 🧠 Modelos re-entrenados")
        if persist: self._persist(number)

    def _get_pf(self, ct):
        if len(self.spin_history)<5: return None; counts={1:0,2:0,3:0}
        for s in self.spin_history[-5:]:
            n=s["number"]
            if n!=0: val=get_dozen(n) if ct=="DOCENA" else get_column(n);counts[val]+=1
        active=[k for k,v in counts.items() if v>0]
        if len(active)!=2: return None; missing=list({1,2,3}-set(active))[0]
        return {"pair":tuple(sorted(active)),"missing":missing,"prob":sum(counts[a] for a in active)/5.0}

    def _get_ph(self, ct):
        if not self.spin_history: return None; ln=self.spin_history[-1]["number"]
        if ln==0: return None; counts=self.after_number_dozen.get(ln,{}) if ct=="DOCENA" else self.after_number_column.get(ln,{})
        total=sum(counts.values())
        if total<10: return None; sc=sorted(counts.items(),key=lambda x:x[1],reverse=True)
        if len(sc)<2: return None; missing=list({1,2,3}-{sc[0][0],sc[1][0]})[0]
        return {"pair":tuple(sorted([sc[0][0],sc[1][0]])),"missing":missing,"prob":(sc[0][1]+sc[1][1])/total}

    def _predict_pair_ml(self, ct, mn):
        mk=self.markov_d if ct=="DOCENA" else self.markov_c; hist=self.dozen_seq if ct=="DOCENA" else self.column_seq
        levels=(self.d_levels if ct=="DOCENA" else self.c_levels).get(mn,[])
        mk_pred=mk.predict(hist); m_p_miss=mk_pred.get(mn,1/3) if mk_pred else 1/3
        pf_d,ph_d=self._get_pf("DOCENA"),self._get_ph("DOCENA");pf_c,ph_c=self._get_pf("COLUMNA"),self._get_ph("COLUMNA")
        ens_p_miss=1/3
        if pf_d and ph_d and pf_c and ph_c:
            ens=self.ensemble_d.predict(hist,self.column_seq,pf_d["pair"],ph_d["pair"],pf_c["pair"],ph_c["pair"]) if ct=="DOCENA" else self.ensemble_c.predict(self.dozen_seq,hist,pf_d["pair"],ph_d["pair"],pf_c["pair"],ph_c["pair"])
            if ens: ens_p_miss=ens.get(mn,1/3)
        ml_miss=0.4*m_p_miss+0.6*ens_p_miss
        if len(levels)>=20:
            if ema_signal(levels,"tendencia"): ml_miss*=0.85
            elif ema_signal(levels,"moderado"): ml_miss*=0.92
        return 1.0-ml_miss

    def detect_signal(self):
        pf_d=self._get_pf("DOCENA");pf_c=self._get_pf("COLUMNA")
        if not pf_d and not pf_c: return None
        ph_d=self._get_ph("DOCENA");ph_c=self._get_ph("COLUMNA"); candidates=[]
        if pf_d and ph_d and set(pf_d["pair"])==set(ph_d["pair"]):
            base=0.65*pf_d["prob"]+0.35*ph_d["prob"];ml=self._predict_pair_ml("DOCENA",pf_d["missing"]);prob=0.5*base+0.5*ml
            if prob>=MIN_PROB: candidates.append({"type":"DOCENA","pair":tuple(f"D{x}" for x in sorted(pf_d["pair"])),"missing":f"D{pf_d['missing']}","prob":prob})
        if pf_c and ph_c and set(pf_c["pair"])==set(ph_c["pair"]):
            base=0.65*pf_c["prob"]+0.35*ph_c["prob"];ml=self._predict_pair_ml("COLUMNA",pf_c["missing"]);prob=0.5*base+0.5*ml
            if prob>=MIN_PROB: candidates.append({"type":"COLUMNA","pair":tuple(f"C{x}" for x in sorted(pf_c["pair"])),"missing":f"C{pf_c['missing']}","prob":prob})
        return max(candidates,key=lambda x:x["prob"]) if candidates else None

    def _build_signal_text(self) -> str:
        nums=sorted([p[1:] for p in self.active_pair]); pair_disp=f"{nums[0]} y {nums[1]}"
        type_str,singular=("docenas","docena") if self.active_type=="DOCENA" else ("columnas","columna")
        gale_num=self.gestor.oportunidad-1; bet=self.gestor.apostar_por_docena(self.bankroll); total_bet=bet*2
        return (f"✅✅ ENTRADA CONFIRMADA ✅✅\n\n🕹️ {self.name}\n🎯 Entrar en las {type_str}: {pair_disp}\n💰 Balance: {GLOBAL_STATS.global_bankroll:.2f}\n💵 Apuesta total: {total_bet:.2f}\n🚨 (por {singular}: {bet:.2f})\n⚔️ Cubrir el CERO 🟢\n🛟 GALE #{gale_num}")

    def send_signal(self, send_tg=True):
        if send_tg and self.name=="SPEED ROULETTE 2":
            msg_id=tg_send_with_button(self._build_signal_text(), self.name)
            if msg_id: self.active_signal_msg_id=msg_id
        gale_num=self.gestor.oportunidad-1; bet=self.gestor.apostar_por_docena(self.bankroll); nums=sorted([p[1:] for p in self.active_pair])
        queue_broadcast({"type":"signal","ws_key":self.ws_key,"roulette":self.name,"signal_type":self.active_type,"pair":list(self.active_pair),"pair_nums":nums,"missing":self.active_missing,"prob":round(self._last_signal_prob,4),"attempt":self.gestor.oportunidad,"gale":gale_num,"bet_per_cat":bet,"total_bet":bet*2,"balance":GLOBAL_STATS.global_bankroll if self.name=="SPEED ROULETTE 2" else self.bankroll,"nivel":self.gestor.nivel,"debt_count":len(self.gestor.debt_stack)})

    def iniciar_senal(self, sig, send_tg=True):
        self.signal_active=True;self.active_type=sig["type"];self.active_pair=sig["pair"];self.active_missing=sig["missing"];self._last_signal_prob=sig.get("prob",0)
        self.gestor.iniciar_senal(self.bankroll);self.total_signal_loss=0.0;self.send_signal(send_tg); logger.info(f"[{self.name}] 🎯 SEÑAL {sig['type']}: {sig['pair']} ({sig['prob']:.0%})")

    def resolve(self, number):
        d,c=get_dozen(number),get_column(number); type_str=self.active_type; val_num=d if type_str=="DOCENA" else c; gale_num=self.gestor.oportunidad-1
        bet=self.gestor.apostar_por_docena(self.bankroll); zero_bet=round(0.1*bet,2); spin_investment=round((2*bet)+zero_bet,2)
        
        if number==0:
            zero_payout=round(zero_bet*36,2); spin_profit=round(zero_payout-spin_investment,2); self.bankroll=round(self.bankroll+spin_profit,2); signal_profit=round(spin_profit-self.total_signal_loss,2)
            self.gestor.verificar_recuperacion(self.bankroll)
            if self.name=="SPEED ROULETTE 2": GLOBAL_STATS.global_bankroll=self.bankroll; GLOBAL_STATS.record('EMPATE',self.gestor.oportunidad,0,0,type_str,self.name); tg_send(f"🟠 EMPATE 0 — ZERO — 🔄 GALE #{gale_num}\n🉑 Para la próxima ganaremos {signal_profit:.2f} 🉑\n💰 Balance actual: {GLOBAL_STATS.global_bankroll:.2f}")
            queue_broadcast({"type":"result","ws_key":self.ws_key,"roulette":self.name,"result":"EMPATE","number":0,"color":"VERDE","dozen":0,"column":0,"detail":"ZERO","attempt":self.gestor.oportunidad,"gale":gale_num,"profit":signal_profit,"balance":GLOBAL_STATS.global_bankroll if self.name=="SPEED ROULETTE 2" else self.bankroll,"total_loss":0.0,"nivel":self.gestor.nivel,"debt_count":len(self.gestor.debt_stack)})
            if self.name=="SPEED ROULETTE 2": self._check_stats()
            self._reset_signal(); return True

        won=(type_str=="DOCENA" and d!=0 and f"D{d}" in self.active_pair) or (type_str=="COLUMNA" and c!=0 and f"C{c}" in self.active_pair)
        if won:
            dozen_payout=round(3*bet,2); spin_profit=round(dozen_payout-spin_investment,2); self.bankroll=round(self.bankroll+spin_profit,2); signal_profit=round(spin_profit-self.total_signal_loss,2)
            self.gestor.verificar_recuperacion(self.bankroll); cat_label=f"{'DOCENA' if type_str=='DOCENA' else 'COLUMNA'} {val_num}"
            if self.name=="SPEED ROULETTE 2": GLOBAL_STATS.global_bankroll=self.bankroll; GLOBAL_STATS.record('WIN',self.gestor.oportunidad,number,val_num,type_str,self.name); tg_send(f"✅ WIN {number} — {cat_label} — 🔄 GALE #{gale_num}\n🎉 Felicidades has ganado {signal_profit:.2f} 🎉\n💰 Balance actual: {GLOBAL_STATS.global_bankroll:.2f}")
            queue_broadcast({"type":"result","ws_key":self.ws_key,"roulette":self.name,"result":"WIN","number":number,"color":REAL_COLOR_MAP.get(number,"VERDE"),"dozen":d,"column":c,"detail":cat_label,"attempt":self.gestor.oportunidad,"gale":gale_num,"profit":signal_profit,"balance":GLOBAL_STATS.global_bankroll if self.name=="SPEED ROULETTE 2" else self.bankroll,"total_loss":0.0,"nivel":self.gestor.nivel,"debt_count":len(self.gestor.debt_stack)})
            if self.name=="SPEED ROULETTE 2": self._check_stats()
            self._reset_signal(); return True
        else:
            self.bankroll=round(self.bankroll-spin_investment,2); self.total_signal_loss=round(self.total_signal_loss+spin_investment,2)
            if self.name=="SPEED ROULETTE 2": GLOBAL_STATS.global_bankroll=self.bankroll
            if gale_num==0:
                if self.name=="SPEED ROULETTE 2" and self.active_signal_msg_id: tg_delete(CHAT_ID,self.active_signal_msg_id); self.active_signal_msg_id=None
                self.gestor.oportunidad=2; self.send_signal(send_tg=(self.name=="SPEED ROULETTE 2")); return False
            else:
                cat_label=f"{'DOCENA' if type_str=='DOCENA' else 'COLUMNA'} {val_num}"
                if self.name=="SPEED ROULETTE 2": GLOBAL_STATS.record('LOSS',2,number,val_num,type_str,self.name); tg_send(f"❌ LOSS {number} — {cat_label} — 🔄 GALE #{gale_num}\n🚨 Señal perdida. Monto total perdido en las 2 entradas: -{self.total_signal_loss:.2f} 🚨\n💰 Balance actual: {GLOBAL_STATS.global_bankroll:.2f}")
                self.gestor.registrar_perdida_senal()
                queue_broadcast({"type":"result","ws_key":self.ws_key,"roulette":self.name,"result":"LOSS","number":number,"color":REAL_COLOR_MAP.get(number,"VERDE"),"dozen":d,"column":c,"detail":cat_label,"attempt":2,"gale":gale_num,"profit":-self.total_signal_loss,"balance":GLOBAL_STATS.global_bankroll if self.name=="SPEED ROULETTE 2" else self.bankroll,"total_loss":self.total_signal_loss,"nivel":self.gestor.nivel,"debt_count":len(self.gestor.debt_stack)})
                if self.name=="SPEED ROULETTE 2": self._check_stats()
                self._reset_signal(); return True

    def _check_stats(self):
        if not GLOBAL_STATS.should_send(): return
        tg_send_stats(GLOBAL_STATS.get_stats_text()); GLOBAL_STATS.mark_sent()

    def _reset_signal(self): self.signal_active=False;self.active_pair=();self.active_type=None;self.total_signal_loss=0.0;self.active_signal_msg_id=None;self._last_signal_prob=0.0

    def feed_number(self, number):
        self._update_state(number)
        if not self.warmup_done:
            self.ws_count+=1
            if self.ws_count>=WARMUP_SPINS: self.warmup_done=True; tg_send(f"🟢 <b>{self.name}</b> — Sistema PF+PH+ML Listo.") if self.name=="SPEED ROULETTE 2" else None
        queue_broadcast({"type":"spin","ws_key":self.ws_key,"roulette":self.name,"number":number,"color":REAL_COLOR_MAP.get(number),"dozen":get_dozen(number),"column":get_column(number),"spin_count":len(self.spin_history),"warmup_done":self.warmup_done,"balance":GLOBAL_STATS.global_bankroll if self.name=="SPEED ROULETTE 2" else self.bankroll,"nivel":self.gestor.nivel,"debt_count":len(self.gestor.debt_stack)})

# ─── SESSION MANAGER ORIGINAL ────────────────────────────────────────────────
class SessionManager:
    ARG_UTC_OFFSET = -3
    def __init__(self):
        self.engines={r["key"]:RouletteEngine(r["key"],r["name"]) for r in ROULETTES}
        self.session_start=0.0; self.session_active=False; self.signals_this_session=0; self.prev_start_msg_id=None; self.prev_end_msg_id=None

    def _now_arg(self): return datetime.datetime.utcnow()+datetime.timedelta(hours=self.ARG_UTC_OFFSET)
    def seconds_to_next_slot(self) -> float:
        now=self._now_arg()
        if now.second<=5 and now.minute in (0,30): return 0.0
        if now.minute<30: target=now.replace(minute=30,second=0,microsecond=0)
        else: target=(now+datetime.timedelta(hours=1)).replace(minute=0,second=0,microsecond=0)
        return max(0.0,(target-now).total_seconds())

    def _start_session(self):
        self.session_start=time.time(); self.session_active=True; self.signals_this_session=0
        engine=self.engines.get(205) # Siempre Speed 2 para sesiones de TG
        now_str=self._now_arg().strftime("%H:%M"); end_str=(self._now_arg()+datetime.timedelta(minutes=25)).strftime("%H:%M")
        logger.info(f"[SessionManager] 🟢 Sesión iniciada: {engine.name} | {now_str}–{end_str} (ARG)")
        if self.prev_start_msg_id: tg_delete(CHAT_ID,self.prev_start_msg_id); self.prev_start_msg_id=None
        self.prev_start_msg_id=tg_send(f"🔔 SESION INICIADA — {engine.name} 🔔")
        queue_broadcast({"type":"session","status":"started","roulette":engine.name,"elapsed":0,"remaining":SESSION_ACTIVE,"signals_count":0,"max_signals":MAX_SIGNALS,"nivel":engine.gestor.nivel,"debt_count":len(engine.gestor.debt_stack)})

    def _end_session(self):
        engine=self.engines.get(205)
        self.session_active=False
        if self.prev_end_msg_id: tg_delete(CHAT_ID,self.prev_end_msg_id); self.prev_end_msg_id=None
        text=f"⏸ SESIÓN CERRADA — {engine.name}\n🎰 PRÓXIMA RULETA — {engine.name} 🎰\n\n💵 ¿COMO OPERAR LAS SEÑALES?\n\n1° Op. = $0.50 USD x Docena/Columna\n2° Op. = $1.50 USD x Docena/Columna\n\n🎯 FUNCIONAMIENTO DE LAS SEÑALES 🎯\n\n  • Se envían las señales → Se resuelven\n  • Sesión se cierra → Ej: 12:25 o 12:55\n  • Nueva Sesión → Ej: 15:00 o 15:30\n  • Nueva Señal → Ciclo de señales\n\n♦️ POR SESION SE ENVÍAN 3 SEÑALES MÁXIMAS ♦️"
        self.prev_end_msg_id=tg_send_with_button(text, engine.name)
        queue_broadcast({"type":"session","status":"ended","roulette":engine.name,"elapsed":SESSION_ACTIVE,"remaining":0,"signals_count":self.signals_this_session,"max_signals":MAX_SIGNALS,"nivel":engine.gestor.nivel,"debt_count":len(engine.gestor.debt_stack)})

    async def session_watchdog(self):
        wait=self.seconds_to_next_slot(); logger.info(f"[SessionManager] ⏳ Esperando {wait/60:.1f} min para el primer slot..."); await asyncio.sleep(wait); self._start_session()
        _waiting_signal_since=None
        while True:
            await asyncio.sleep(1); now=time.time(); elapsed=now-self.session_start; engine=self.engines.get(205)
            if self.session_active:
                if elapsed>=SESSION_ACTIVE:
                    if engine.signal_active:
                        if _waiting_signal_since is None: _waiting_signal_since=now; logger.info(f"[SessionManager] ⏳ Sesión terminada pero señal activa — esperando resolución...")
                        elif now-_waiting_signal_since>=SIGNAL_WAIT_TIMEOUT:
                            logger.warning(f"[SessionManager] ⚠️ Timeout. Cancelando señal.")
                            if engine.active_signal_msg_id: tg_edit(CHAT_ID,engine.active_signal_msg_id,engine._build_signal_text()+"\n\n⚠️ Señal cancelada — tiempo de sesión agotado.")
                            engine._reset_signal(); _waiting_signal_since=None
                        else: continue
                    else: _waiting_signal_since=None
                    end_time=time.time(); self._end_session(); pause_remaining=SESSION_TOTAL-(end_time-self.session_start)
                    if pause_remaining>0: await asyncio.sleep(pause_remaining)
                    self._start_session()
            else:
                if elapsed>=SESSION_TOTAL: self._start_session()

    def on_number(self, ws_key, number):
        engine=self.engines.get(ws_key)
        if not engine: return
        engine.feed_number(number)
        if engine.signal_active: engine.resolve(number); return
        if engine.warmup_done:
            sig=engine.detect_signal()
            if sig:
                if engine.name=="SPEED ROULETTE 2":
                    if self.session_active and self.signals_this_session<MAX_SIGNALS: engine.iniciar_senal(sig, send_tg=True); self.signals_this_session+=1
                else: engine.iniciar_senal(sig, send_tg=False) # Solo HTML para las demás

# ─── WS READER COMPLETO ─────────────────────────────────────────────────────
async def ws_reader(ws_key, session_mgr):
    reconnect_delay=5; initial_loaded=False; seen_ids=set(); seen_ids_queue=deque(maxlen=200)
    def is_new_id(gid):
        if not gid or gid in seen_ids: return False
        if len(seen_ids_queue)==seen_ids_queue.maxlen: seen_ids.discard(seen_ids_queue[0])
        seen_ids.add(gid);seen_ids_queue.append(gid); return True
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=40, close_timeout=10) as ws:
                await ws.send(json.dumps({"type":"subscribe","key":ws_key,"casinoId":CASINO_ID})); logger.info(f"[WS-{ws_key}] ✅ Conectado"); reconnect_delay=5
                async def poll_1s():
                    while True:
                        await asyncio.sleep(1)
                        try: await ws.send(json.dumps({"type":"subscribe","key":ws_key,"casinoId":CASINO_ID}))
                        except: break
                poll_task=asyncio.create_task(poll_1s())
                try:
                    async for raw in ws:
                        try: data=json.loads(raw)
                        except: continue
                        if not isinstance(data, dict): continue
                        results=data.get("last20Results")
                        if results and isinstance(results, list):
                            if not initial_loaded:
                                initial_loaded=True; engine=session_mgr.engines.get(ws_key); loaded_count=0
                                if engine:
                                    for item in reversed(results):
                                        gid_init=str(item.get("gameId",""))
                                        if gid_init:
                                            if len(seen_ids_queue)==seen_ids_queue.maxlen: seen_ids.discard(seen_ids_queue[0])
                                            seen_ids.add(gid_init);seen_ids_queue.append(gid_init)
                                        try: n=int(item.get("result",""))
                                        except: continue
                                        if 0<=n<=36: engine._update_state(n,persist=False,train_model=True);loaded_count+=1
                                    engine._train_models()
                                    if not engine.warmup_done and len(engine.spin_history)>=WARMUP_SPINS: engine.warmup_done=True;engine.ws_count=len(engine.spin_history)
                                continue
                            latest=results[0]; gid=str(latest.get("gameId",""))
                            if not is_new_id(gid): continue
                            try: n=int(latest.get("result",""))
                            except: continue
                            if 0<=n<=36: session_mgr.on_number(ws_key,n)
                            continue
                        fallback_gid=str(data.get("gameId","")).strip()
                        if not fallback_gid:
                            for key in ("result","number","outcome","winningNumber"):
                                if key in data: fallback_gid=f"{ws_key}_{data[key]}_{int(time.time())}"; break
                        if not fallback_gid or not is_new_id(fallback_gid): continue
                        for key in ("result","number","outcome","winningNumber"):
                            if key in data:
                                try:
                                    n=int(data[key])
                                    if 0<=n<=36: session_mgr.on_number(ws_key,n); break
                                except: pass
                finally: poll_task.cancel()
        except: await asyncio.sleep(reconnect_delay); reconnect_delay=min(reconnect_delay*2,60)

# ─── AIOHTTP SERVER + KEEP-ALIVE ─────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    """Permite que el HTML externo (file:// o cualquier origen) se conecte al servidor."""
    if request.method == "OPTIONS":
        resp = web.Response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

async def ws_html_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    q = asyncio.Queue()
    _ws_clients.add(q)
    try:
        while True:
            data = await q.get()
            try: 
                await ws.send_json(data)
            except Exception: 
                break
    finally: 
        _ws_clients.discard(q)
        return ws

async def health_handler(request):
    return web.json_response({"status": "ok", "roulettes": [r["name"] for r in ROULETTES]})

async def self_ping():
    """Evita que Render suspenda el servicio por inactividad."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        logger.info("[KeepAlive] RENDER_EXTERNAL_URL no definida. Auto-ping desactivado (ejecución local).")
        return
    health_url = f"{render_url}/health"
    logger.info(f"[KeepAlive] Auto-ping configurado cada 10 min hacia {health_url}")
    while True:
        await asyncio.sleep(600) # 10 minutos
        try:
            async with ClientSession() as session:
                async with session.get(health_url, timeout=5) as resp:
                    if resp.status == 200: logger.info("[KeepAlive] ✅ Ping exitoso. Servidor activo.")
                    else: logger.warning(f"[KeepAlive] ⚠️ Ping respondió con status: {resp.status}")
        except Exception as e:
            logger.error(f"[KeepAlive] ❌ Error en auto-ping: {e}")

async def background_tasks(app):
    """Inicia las tareas en segundo plano al arrancar el servidor"""
    session_mgr = SessionManager()
    readers = [ws_reader(r["key"], session_mgr) for r in ROULETTES]
    for reader in readers:
        asyncio.create_task(reader)
    asyncio.create_task(session_mgr.session_watchdog())
    asyncio.create_task(self_ping()) # Iniciar el keep-alive

app = web.Application(middlewares=[cors_middleware])
app.router.add_get('/ws', ws_html_handler)
app.router.add_get('/', health_handler)
app.router.add_get('/health', health_handler)
app.on_startup.append(background_tasks)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"[Server] Iniciando servidor unificado en puerto {port}")
    web.run_app(app, host="0.0.0.0", port=port)
