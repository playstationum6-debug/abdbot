"""
ABD Trade Botu — Adım 1: Canlı veri + grafik
---------------------------------------------
- Yahoo (yfinance, prepost=True): 50 sembolün 1 dakikalık mumları (öncesi / normal / sonrası),
  konsolide hacimle. Seans içinde 60 sn'de bir çekilir.
- Alpaca IEX WebSocket (ücretsiz): ilk 30 sembol için anlık işlem fiyatı. Sadece fiyatı
  (tepe/dip/kapanış) canlı günceller; hacim Yahoo'dan gelir çünkü IEX piyasanın küçük bir kısmı.
- Alpaca takvimi: tatiller ve erken kapanışlar.
- SQLite: çalışma verisi. Yeniden başlarsa Yahoo son 5 günü tekrar doldurur.
- GitHub yedeği (isteğe bağlı): küçük durum dosyası (ileride işlemler/sinyaller) özel bir repoya yazılır,
  açılışta geri okunur. Hugging Face gibi diski kalıcı olmayan yerler için.
- Tarayıcıya WebSocket ile saniyede bir güncelleme.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import websockets
import yfinance as yf

import bot as botmod
import ikon
import ogrenme
import sinyal
import tarayici
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

BASE = Path(__file__).resolve().parent


# ----------------------------------------------------------------- ayarlar
def load_env():
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

ALPACA_KEY = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
SITE_USER = os.getenv("SITE_USER", "admin")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")
GH_TOKEN = os.getenv("GH_TOKEN", "")
GH_REPO = os.getenv("GH_REPO", "")            # ör. kullaniciadi/abdbot-veri
GH_PATH = os.getenv("GH_PATH", "durum/state.json")
SIG_DIR = "sinyaller"
BOT_DIR = "bot"

# Bot ayarları (Render → Environment'tan değiştirilebilir)
BOT_CFG = {
    "enabled": os.getenv("BOT_ENABLED", "1") == "1",
    "notional": float(os.getenv("BOT_NOTIONAL", "250")),     # işlem başı pozisyon büyüklüğü ($)
    "max_open": int(os.getenv("BOT_MAX_OPEN", "15")),        # aynı anda en fazla açık pozisyon
    "risk_usd": float(os.getenv("BOT_RISK", "5")),           # işlem başı en fazla risk ($), stop mesafesine göre adet
    "daily_loss": float(os.getenv("BOT_DAILY_LOSS", "50")),  # günlük zarar limiti ($)
    "min_conf": int(os.getenv("BOT_MIN_CONF", "55")),        # botun alacağı en düşük güven puanı (normal seans)
    "min_conf_ext": int(os.getenv("BOT_MIN_CONF_EXT", "50")),# piyasa öncesi/sonrası için eşik (yarım lot + limit emir)
}
RUN_MIN_PRICE = float(os.getenv("RUN_MIN_PRICE", "0"))       # koşan hisselerde en düşük fiyat (0 = sınır yok)
RUN_MIN_PCT = float(os.getenv("RUN_MIN_PCT", "10"))          # en az yükseliş yüzdesi
RUN_MAX = int(os.getenv("RUN_MAX", "12"))                    # aynı anda izlenecek en fazla koşan hisse
BACKUP_SEC = int(os.getenv("BACKUP_SEC", "600"))

# İlk 30'u IEX ile anlık izlenir (ücretsiz planın sembol sınırı 30).
DEFAULT_SYMBOLS = (
    "SPY,QQQ,IWM,DIA,TQQQ,SQQQ,SOXL,NVDA,TSLA,AAPL,"
    "MSFT,AMZN,META,GOOGL,AMD,AVGO,PLTR,NFLX,INTC,MU,"
    "COIN,MSTR,HOOD,SOFI,BAC,JPM,F,UBER,ORCL,SMCI,"
    "XLF,XLE,TLT,GLD,SLV,ARM,MARA,RIVN,BABA,PYPL,"
    "DIS,WMT,COST,XOM,CVX,PFE,T,KO,BA,CRM"
)
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
IEX_MAX = int(os.getenv("IEX_MAX", "30"))
IEX_SYMBOLS = SYMBOLS[:IEX_MAX]

DB_PATH = BASE / os.getenv("DB_PATH", "data/piyasa.db")
POLL_ACTIVE = int(os.getenv("YAHOO_POLL_SEC", "60"))  # seans içindeyken
POLL_IDLE = 900                                       # piyasa kapalıyken
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "20"))         # veritabanında tutulan gün
MEM_SEC = 7 * 86400                                   # bellekte tutulan süre
STALE_SEC = 180                                       # seans açıkken bu kadar veri yoksa "eski"
BAD_TICK = 0.10                                       # son canlı fiyattan %10+ sapan tek işlem = hatalı tick

ET = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")
UTC = timezone.utc
SES_CODE = {"pre": 0, "regular": 1, "post": 2, "closed": 3}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("abdbot")


def alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


class State:
    iex = "başlıyor"
    yahoo_ok = 0.0
    yahoo_err = ""
    yahoo_secs = 0.0
    yahoo_t0 = 0.0          # son başarılı Yahoo çekiminin BAŞLADIĞI an (bu andan önce biten mumlar kapanmıştır)
    xvol_last = 0.0         # öncesi/sonrası hacmi için son başarılı Alpaca IEX mum çekimi
    xvol_err = ""
    started = time.time()


state = State()


# ----------------------------------------------------------------- GitHub yedeği
class Backup:
    """GitHub'a kalıcı kayıt: durum dosyası + günlük sinyal dosyaları (sinyaller/YYYY-MM-DD.json).
    Kod reposundan AYRI bir repo kullanın; yoksa her yedek Render'da yeniden kurulum tetikler."""

    def __init__(self):
        self.data = {"starts": []}
        self.shas = {}
        self.status = "kapalı (GH_TOKEN/GH_REPO yok)" if not (GH_TOKEN and GH_REPO) else "başlıyor"
        self.last_ok = 0.0
        self.dirty = False

    @property
    def enabled(self):
        return bool(GH_TOKEN and GH_REPO)

    def _url(self, path):
        return f"https://api.github.com/repos/{GH_REPO}/contents/{path}"

    def _headers(self, raw=False):
        return {"Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    async def _get(self, c, path):
        r = await c.get(self._url(path), headers=self._headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        self.shas[path] = j.get("sha")
        content = j.get("content") or ""
        if content.strip():
            raw = base64.b64decode(content)
        else:   # 1 MB üstü dosyalarda içerik ayrı istenir
            rr = await c.get(self._url(path), headers=self._headers(raw=True))
            rr.raise_for_status()
            raw = rr.content
        return json.loads(raw.decode("utf-8") or "null")

    async def _put(self, c, path, obj):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = {"message": f"{path} {datetime.now(TR):%Y-%m-%d %H:%M}", "content": base64.b64encode(body).decode()}
        if self.shas.get(path):
            payload["sha"] = self.shas[path]
        r = await c.put(self._url(path), headers=self._headers(), json=payload)
        if r.status_code in (409, 422):   # sha eskimiş ya da eksik: güncelini al, tekrar dene
            g = await c.get(self._url(path), headers=self._headers())
            if g.status_code == 200:
                payload["sha"] = g.json().get("sha")
                r = await c.put(self._url(path), headers=self._headers(), json=payload)
        r.raise_for_status()
        self.shas[path] = r.json()["content"]["sha"]

    async def restore(self):
        if not self.enabled:
            return [], []
        sigs, botdays = [], []
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                d = await self._get(c, GH_PATH)
                if isinstance(d, dict):
                    self.data = d
                self.data.setdefault("starts", [])
                r = await c.get(self._url(SIG_DIR), headers=self._headers())
                if r.status_code == 200:
                    files = sorted(x["path"] for x in r.json() if x.get("type") == "file" and x["path"].endswith(".json"))
                    for p in files[-120:]:
                        part = await self._get(c, p)
                        if isinstance(part, list):
                            sigs.extend(part)
                r = await c.get(self._url(BOT_DIR), headers=self._headers())
                if r.status_code == 200:
                    files = sorted(x["path"] for x in r.json() if x.get("type") == "file" and x["path"].endswith(".json"))
                    for p in files[-120:]:
                        part = await self._get(c, p)
                        if isinstance(part, dict):
                            botdays.append(part)
            self.status = f"geri yüklendi ({len(sigs)} sinyal, {sum(len(b.get('pos', [])) for b in botdays)} bot işlemi)"
            self.last_ok = time.time()
        except Exception as e:
            self.status = f"okuma hatası: {str(e)[:80]}"
            log.warning("GitHub yedeği okunamadı: %s", e)
        return sigs, botdays

    async def save(self, engine=None, bot=None):
        if not self.enabled:
            return
        days = set(engine.dirty_days) if engine else set()
        bdays = set(bot.dirty_days) if bot else set()
        if not self.dirty and not days and not bdays:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                if self.dirty:
                    await self._put(c, GH_PATH, self.data)
                    self.dirty = False
                for day in sorted(days):
                    await self._put(c, f"{SIG_DIR}/{day}.json", engine.day_list(day))
                    engine.dirty_days.discard(day)
                for day in sorted(bdays):
                    await self._put(c, f"{BOT_DIR}/{day}.json", bot.day_data(day))
                    bot.dirty_days.discard(day)
            self.last_ok = time.time()
            self.status = "tamam"
        except Exception as e:
            self.status = f"yazma hatası: {str(e)[:80]}"
            log.warning("GitHub yedeği yazılamadı: %s", e)

    def record_start(self):
        s = self.data.setdefault("starts", [])
        s.append(int(state.started))
        del s[:-200]
        self.dirty = True

    def starts_last_7d(self):
        cut = time.time() - 7 * 86400
        return sum(1 for t in self.data.get("starts", []) if t >= cut)


backup = Backup()


# ----------------------------------------------------------------- takvim
class Calendar:
    def __init__(self):
        self.days = {}
        self.source = "kural"
        self.loaded_on = None
        self.last_try = 0.0

    @staticmethod
    def _t(s, default):
        try:
            s = str(s).replace(":", "")
            return dtime(int(s[:2]), int(s[2:4]))
        except Exception:
            return default

    async def refresh(self, force=False):
        today = datetime.now(ET).date()
        if not force and self.loaded_on == today and self.source == "alpaca":
            return
        if not force and self.source != "alpaca" and time.time() - self.last_try < 3600 and self.days:
            return
        self.last_try = time.time()
        days = {}
        if ALPACA_KEY:
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(
                        "https://paper-api.alpaca.markets/v2/calendar",
                        params={"start": (today - timedelta(days=14)).isoformat(),
                                "end": (today + timedelta(days=10)).isoformat()},
                        headers=alpaca_headers(),
                    )
                    r.raise_for_status()
                    for d in r.json():
                        days[date.fromisoformat(d["date"])] = {
                            "open": self._t(d.get("open", "09:30"), dtime(9, 30)),
                            "close": self._t(d.get("close", "16:00"), dtime(16, 0)),
                            "sopen": self._t(d.get("session_open", "0400"), dtime(4, 0)),
                            "sclose": self._t(d.get("session_close", "2000"), dtime(20, 0)),
                        }
                if days:
                    self.source = "alpaca"
            except Exception as e:
                log.warning("Alpaca takvimi alınamadı, hafta içi kuralı kullanılacak: %s", e)
        if not days:
            for i in range(-14, 11):
                d = today + timedelta(days=i)
                if d.weekday() < 5:
                    days[d] = {"open": dtime(9, 30), "close": dtime(16, 0),
                               "sopen": dtime(4, 0), "sclose": dtime(20, 0)}
            self.source = "kural"
        self.days = days
        self.loaded_on = today
        bar_info.cache_clear()

    def session(self, ts):
        et = datetime.fromtimestamp(ts, ET)
        d = self.days.get(et.date())
        if not d:
            return "closed"
        t = et.time()
        if d["sopen"] <= t < d["open"]:
            return "pre"
        if d["open"] <= t < d["close"]:
            return "regular"
        if d["close"] <= t < d["sclose"]:
            return "post"
        return "closed"

    def plan(self):
        """Bugünün (bittiyse bir sonraki işlem gününün) seans saatleri, Türkiye saatiyle."""
        now_et = datetime.now(ET)
        for i in range(0, 10):
            d = now_et.date() + timedelta(days=i)
            x = self.days.get(d)
            if not x:
                continue
            if i == 0 and now_et.time() >= x["sclose"]:
                continue

            def tr(t):
                return datetime.combine(d, t, tzinfo=ET).astimezone(TR).strftime("%H:%M")

            return {"d": d.isoformat(), "pre": tr(x["sopen"]), "open": tr(x["open"]),
                    "close": tr(x["close"]), "post": tr(x["sclose"]),
                    "early": x["close"] != dtime(16, 0), "today": i == 0}
        return None


    def next_event(self):
        """Sıradaki seans olayı: (ad, epoch)."""
        now = time.time()
        today = datetime.now(ET).date()
        for i in range(0, 10):
            d = today + timedelta(days=i)
            x = self.days.get(d)
            if not x:
                continue
            for key, ad in (("sopen", "Piyasa öncesi başlıyor"), ("open", "Açılış"), ("close", "Kapanış"),
                            ("sclose", "Piyasa sonrası bitiyor")):
                t = datetime.combine(d, x[key], tzinfo=ET).timestamp()
                if t > now:
                    return {"ad": ad, "t": int(t)}
        return None


cal = Calendar()


@lru_cache(maxsize=60000)
def bar_info(ts: int):
    """(New York tarihi, seans kodu) — her dakika için önbellekli."""
    return datetime.fromtimestamp(ts, ET).date(), SES_CODE[cal.session(ts)]


# ----------------------------------------------------------------- veri deposu
class Store:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS bars(symbol TEXT NOT NULL, ts INTEGER NOT NULL,"
            " o REAL, h REAL, l REAL, c REAL, v REAL, src TEXT, PRIMARY KEY(symbol, ts))"
        )
        self.db.commit()
        self.bars = {s: {} for s in SYMBOLS}        # sembol -> {ts: [o,h,l,c,v,src]}
        self.live = {}                              # sembol -> {"p": fiyat, "t": zaman}
        self.ver = {s: 0 for s in SYMBOLS}          # küçük değişiklik sayacı
        self.snap_ver = {s: 0 for s in SYMBOLS}     # tam yeniden çizim gerektiren değişiklik
        self.dirty = set()
        self.bad_ticks = 0
        # Öncesi/sonrası seans hacmi: Yahoo bu saatlerde hacmi 0 verir, Alpaca IEX'ten doldurulur.
        self.xvol = {}                              # sembol -> {ts: hacim}

    def load(self):
        since = int(time.time() - MEM_SEC)
        rows = self.db.execute(
            "SELECT symbol, ts, o, h, l, c, v, src FROM bars WHERE ts >= ?", (since,)
        ).fetchall()
        for sym, ts, o, h, l, c, v, src in rows:
            if sym in self.bars:
                self.bars[sym][ts] = [o, h, l, c, v, src]
        log.info("Veritabanından %d mum yüklendi", len(rows))

    def merge_yahoo(self, sym, new):
        cur = self.bars.setdefault(sym, {})
        self.ver.setdefault(sym, 0)
        self.snap_ver.setdefault(sym, 0)
        now_min = int(time.time() // 60 * 60)
        live = self.live.get(sym)
        changed_min = None
        for ts, (o, h, l, c, v) in new.items():
            old = cur.get(ts)
            if old is not None and ts >= now_min - 60:
                # Oluşmakta olan mum: canlı tick'lerle birleştir
                h = max(h, old[1])
                l = min(l, old[2])
                if live and ts <= live["t"] < ts + 60:
                    c = live["p"]
            xv = self.xvol.get(sym, {}).get(ts)
            if xv is not None:
                v = xv
            row = [o, h, l, c, v, "yahoo"]
            if old is None or old[:5] != row[:5]:
                cur[ts] = row
                self.dirty.add((sym, ts))
                changed_min = ts if changed_min is None else min(changed_min, ts)
        if changed_min is not None:
            if changed_min < now_min - 180:
                self.snap_ver[sym] += 1   # geçmiş mumlar değişti -> tam yeniden çizim
            else:
                self.ver[sym] += 1
            return 1
        return 0

    def set_xvol(self, sym, vols):
        """Öncesi/sonrası seans mumlarına Alpaca IEX hacmini yaz."""
        cur = self.xvol.setdefault(sym, {})
        bars = self.bars.get(sym)
        changed = False
        for ts, v in vols.items():
            if cur.get(ts) == v:
                continue
            cur[ts] = v
            b = bars.get(ts) if bars else None
            if b is not None and b[4] != v:
                b[4] = v
                self.dirty.add((sym, ts))
                changed = True
        if changed:
            self.ver[sym] = self.ver.get(sym, 0) + 1
            if min(vols) < time.time() - 180:
                self.snap_ver[sym] = self.snap_ver.get(sym, 0) + 1   # geçmiş mumların hacmi değişti → grafiği yeniden çiz

    def prune_xvol(self):
        cut = int(time.time() - 2 * 86400)
        for d in self.xvol.values():
            for ts in [t for t in d if t < cut]:
                del d[ts]

    def on_trade(self, sym, p, t):
        if sym not in self.bars or p <= 0:
            return
        last = self.live.get(sym)
        if last and t - last["t"] < 300 and abs(p / last["p"] - 1) > BAD_TICK:
            self.bad_ticks += 1
            return
        ts = int(t // 60 * 60)
        b = self.bars[sym].get(ts)
        newer = (not last) or t >= last["t"]
        if b is None:
            self.bars[sym][ts] = [p, p, p, p, 0.0, "iex"]
        else:
            b[1] = max(b[1], p)
            b[2] = min(b[2], p)
            if newer:
                b[3] = p
        if newer:
            self.live[sym] = {"p": p, "t": t}
        self.ver[sym] += 1
        self.dirty.add((sym, ts))

    def flush(self):
        if not self.dirty:
            return
        items = list(self.dirty)
        self.dirty.clear()
        rows = []
        for sym, ts in items:
            b = self.bars.get(sym, {}).get(ts)
            if b:
                rows.append((sym, ts, *b))
        self.db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def prune(self):
        cut = int(time.time() - MEM_SEC)
        for sym, bars in self.bars.items():
            for ts in [t for t in bars if t < cut]:
                del bars[ts]
        self.db.execute("DELETE FROM bars WHERE ts < ?", (int(time.time() - KEEP_DAYS * 86400),))
        self.db.commit()


store = Store()
engine = sinyal.Engine(store, bar_info, cal, ET)
learner = ogrenme.Learner()
engine.learner = learner
scanner = tarayici.Scanner(ALPACA_KEY, ALPACA_SECRET, SYMBOLS, min_price=RUN_MIN_PRICE, min_pct=RUN_MIN_PCT,
                           max_runners=RUN_MAX, log=log)
bot = botmod.Bot(engine, learner, store, cal, ET, ALPACA_KEY, ALPACA_SECRET, log, BOT_CFG)


def runner_pct(sym):
    """Koşan hissenin BUGÜNKÜ önceki kapanışa göre değişimi (bugünün verisi yoksa None)."""
    sm = summary(sym)
    if not sm or sm.get("ch") is None:
        return None
    if sm["day"] != datetime.now(ET).date().isoformat():
        return None
    return sm["ch"]


def all_syms():
    return SYMBOLS + [s for s in scanner.runners if s not in SYMBOLS]


def ensure_sym(sym):
    store.bars.setdefault(sym, {})
    store.ver.setdefault(sym, 0)
    store.snap_ver.setdefault(sym, 0)


# ----------------------------------------------------------------- özetler
_static_cache = {}


def _mx(a, b):
    return b if a is None else max(a, b)


def _mn(a, b):
    return b if a is None else min(a, b)


def compute_static(sym):
    bars = store.bars.get(sym) or {}
    if not bars:
        return None
    tss = sorted(bars)
    last_ts = tss[-1]
    ref_day = bar_info(last_ts)[0]
    pc = dh = dl = ph = pl = None
    for ts in reversed(tss):
        d, sc = bar_info(ts)
        b = bars[ts]
        if d == ref_day:
            if sc == 1:
                dh, dl = _mx(dh, b[1]), _mn(dl, b[2])
            elif sc == 0:
                ph, pl = _mx(ph, b[1]), _mn(pl, b[2])
        elif d < ref_day and sc == 1:
            pc = b[3]   # önceki işlem gününün son normal seans mumu
            break
    return {"last_ts": last_ts, "ref_day": ref_day, "pc": pc, "dh": dh, "dl": dl,
            "ph": ph, "pl": pl, "last_close": bars[last_ts][3]}


def summary(sym):
    key = (store.ver.get(sym, 0), store.snap_ver.get(sym, 0))
    c = _static_cache.get(sym)
    if not c or c[0] != key:
        c = (key, compute_static(sym))
        _static_cache[sym] = c
    st = c[1]
    if not st:
        return None
    now = time.time()
    price = st["last_close"]
    last_t = min(st["last_ts"] + 60, now)
    live = store.live.get(sym)
    if live and live["t"] >= st["last_ts"]:
        price = live["p"]
        last_t = max(last_t, live["t"])
    src = "iex" if live and now - live["t"] < 120 else "yahoo"
    ses_now = cal.session(now)
    pc = st["pc"]
    r4 = lambda x: None if x is None else round(x, 4)
    return {
        "p": r4(price),
        "ch": round((price / pc - 1) * 100, 2) if pc else None,
        "pc": r4(pc), "dh": r4(st["dh"]), "dl": r4(st["dl"]),
        "ph": r4(st["ph"]), "pl": r4(st["pl"]),
        "day": st["ref_day"].isoformat(),
        "t": int(last_t),
        "src": src,
        # Seans açıkken uzun süredir veri yoksa
        "st": ses_now != "closed" and now - last_t > STALE_SEC,
        # Seans açık ama elimizdeki en son veri bugüne ait değil (eski günü bugün sanma hatası)
        "wd": ses_now != "closed" and st["ref_day"] != datetime.now(ET).date(),
    }


def list_row(sym):
    s = summary(sym)
    if not s:
        return None
    return {k: s[k] for k in ("p", "ch", "t", "src", "st", "wd")}


def bar_out(ts, b):
    return [ts, round(b[0], 4), round(b[1], 4), round(b[2], 4), round(b[3], 4),
            int(b[4] or 0), bar_info(ts)[1]]


def snapshot_bars(sym):
    bars = store.bars.get(sym) or {}
    tss = sorted(bars)
    keep = set(sorted({bar_info(ts)[0] for ts in tss})[-2:])   # son 2 işlem günü
    return [bar_out(ts, bars[ts]) for ts in tss if bar_info(ts)[0] in keep]


_lv_cache = {}


def _fractals(seq, n=2):
    """5 dk mumlarda yerel tepe/dip (her iki yanda n mum)."""
    hi, lo = [], []
    for i in range(n, len(seq) - n):
        h, l = seq[i][1], seq[i][2]
        if all(h >= seq[j][1] for j in range(i - n, i + n + 1)):
            hi.append(h)
        if all(l <= seq[j][2] for j in range(i - n, i + n + 1)):
            lo.append(l)
    return hi, lo


def _clusters(prices, tol):
    out = []
    for p in sorted(prices):
        if out and p - out[-1]["hi"] <= tol:
            g = out[-1]
            g["ps"].append(p)
            g["hi"] = p
        else:
            out.append({"ps": [p], "hi": p})
    return [(sum(g["ps"]) / len(g["ps"]), len(g["ps"])) for g in out]


def levels(sym):
    """Grafik için destek/direnç: kilit seviyeler + son 2 günün tepe/diplerinden bölgeler."""
    key = (store.ver.get(sym, 0) // 5, store.snap_ver.get(sym, 0))
    c_ = _lv_cache.get(sym)
    if c_ and c_[0] == key:
        return c_[1]
    bars = store.bars.get(sym) or {}
    out = {"lv": [], "sup": None, "res": None}
    if len(bars) < 30:
        return out
    tss = sorted(bars)
    days = sorted({bar_info(t)[0] for t in tss})
    ref = days[-1]
    prevd = days[-2] if len(days) > 1 else None
    today = [t for t in tss if bar_info(t)[0] == ref]
    reg = [t for t in today if bar_info(t)[1] == 1]
    pre = [t for t in today if bar_info(t)[1] == 0]
    prev_reg = [t for t in tss if bar_info(t)[0] == prevd and bar_info(t)[1] == 1] if prevd else []
    last = bars[tss[-1]][3]
    lv = []

    def add(p, k, t):
        if p:
            lv.append({"p": round(p, 4), "k": k, "t": t})
    if prev_reg:
        add(max(bars[t][1] for t in prev_reg), "pd", "Önc. gün tepe")
        add(min(bars[t][2] for t in prev_reg), "pd", "Önc. gün dip")
    if pre:
        add(max(bars[t][1] for t in pre), "pm", "Öncesi tepe")
        add(min(bars[t][2] for t in pre), "pm", "Öncesi dip")
    if reg:
        b = engine.bounds(ref)
        orb = [t for t in reg if b and t < b["open"] + 15 * 60]
        if len(orb) >= 10 and len(reg) > len(orb):
            add(max(bars[t][1] for t in orb), "or", "Açılış aralığı tepe")
            add(min(bars[t][2] for t in orb), "or", "Açılış aralığı dip")
        if len(reg) > 30:
            add(max(bars[t][1] for t in reg), "hod", "Gün tepe")
            add(min(bars[t][2] for t in reg), "hod", "Gün dip")
    # 5 dk mumlar (son 2 gün, öncesi + normal seans)
    keep = set(days[-2:])
    agg, cur_b = [], None
    for t in tss:
        d_, sc = bar_info(t)
        if d_ not in keep or sc == 3:
            continue
        o, h, l, c, v = bars[t][:5]
        bt = t // 300 * 300
        if cur_b and cur_b[0] == bt:
            cur_b[1] = max(cur_b[1], h)
            cur_b[2] = min(cur_b[2], l)
            cur_b[3] = c
        else:
            if cur_b:
                agg.append(cur_b)
            cur_b = [bt, h, l, c]
    if cur_b:
        agg.append(cur_b)
    if len(agg) > 10:
        rng = sorted(a[1] - a[2] for a in agg[-60:])
        atr5 = rng[len(rng) // 2] or last * 0.002
        tol = max(last * 0.0015, atr5 * 0.5)
        hi, lo = _fractals(agg)
        zones = _clusters(hi + lo, tol)
        # Kilit seviyeyle çakışan bölge: ayrı çizgi yerine o seviyeyi "güçlü" işaretle
        free = []
        for p, n in zones:
            if n < 2:
                continue
            near = [x for x in lv if abs(p - x["p"]) <= tol * 0.6]
            if near:
                for x in near:
                    x["n"] = max(x.get("n", 1), n)
            else:
                free.append((p, n))
        for x in lv:
            if x.get("n", 1) >= 2:
                x["t"] += f" · güçlü ×{x['n']}"
        zones = free
        above = sorted([z for z in zones if z[0] > last], key=lambda z: z[0])[:3]
        below = sorted([z for z in zones if z[0] < last], key=lambda z: -z[0])[:3]
        for p, n in above:
            lv.append({"p": round(p, 4), "k": "res", "t": f"Direnç ×{n}"})
        for p, n in below:
            lv.append({"p": round(p, 4), "k": "sup", "t": f"Destek ×{n}"})
    ab = [x for x in lv if x["p"] > last * 1.0005]
    be = [x for x in lv if x["p"] < last * 0.9995]
    out = {"lv": lv,
           "res": min(ab, key=lambda x: x["p"]) if ab else None,
           "sup": max(be, key=lambda x: x["p"]) if be else None}
    _lv_cache[sym] = (key, out)
    return out


def status_payload():
    now = time.time()
    return {
        "now": now,
        "ses": cal.session(now),
        "iex": state.iex,
        "iexN": len(IEX_SYMBOLS),
        "yAge": int(now - state.yahoo_ok) if state.yahoo_ok else None,
        "ySec": round(state.yahoo_secs, 1),
        "yErr": state.yahoo_err,
        "cal": cal.source,
        "plan": cal.plan(),
        "nx": cal.next_event(),
        "bad": store.bad_ticks,
        "up": int(now - state.started),
        "bk": backup.status,
        "bkAge": int(now - backup.last_ok) if backup.last_ok else None,
        "rs7": backup.starts_last_7d() if backup.enabled else None,
        "bot": bot.status,
        "scan": scanner.status,
        "nRun": len(scanner.runners),
        "sigN": len(engine.signals),
        "sigOpen": engine.open_count(),
        "mkt": engine.mkt.get("dir"),
    }


# ----------------------------------------------------------------- Yahoo
_daily_cache = {}   # sembol -> (zaman, mumlar)


def daily_fetch(sym):
    """Günlük grafik için 1 yıllık günlük mumlar (Yahoo)."""
    df = yf.download(sym, period="1y", interval="1d", auto_adjust=False, progress=False, threads=False)
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = df.columns.get_level_values(0)
        if sym in lv0:
            df = df[sym]
        else:
            df = df.xs(sym, axis=1, level=1) if sym in df.columns.get_level_values(1) else df.droplevel(1, axis=1)
    out = []
    for idx, r in df.iterrows():
        try:
            o, h, l, c = float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])
            v = float(r["Volume"]) if r["Volume"] == r["Volume"] else 0.0
        except Exception:
            continue
        if c != c or o != o:
            continue
        out.append([idx.strftime("%Y-%m-%d"), round(o, 4), round(h, 4), round(l, 4), round(c, 4), int(v)])
    return out


async def get_daily(sym):
    hit = _daily_cache.get(sym)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        bars = await asyncio.to_thread(daily_fetch, sym)
    except Exception as e:
        log.warning("Günlük veri hatası %s: %s", sym, e)
        bars = hit[1] if hit else []
    if bars:
        _daily_cache[sym] = (time.time(), bars)
        if len(_daily_cache) > 80:
            _daily_cache.pop(min(_daily_cache, key=lambda k: _daily_cache[k][0]), None)
    return bars


def yahoo_fetch(symbols, period):
    df = yf.download(
        tickers=" ".join(symbols), period=period, interval="1m", prepost=True,
        group_by="ticker", auto_adjust=False, threads=True, progress=False,
    )
    if df is None or df.empty:
        raise RuntimeError("Yahoo boş veri döndürdü")
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    tsarr = [int(x) for x in ((idx.tz_convert("UTC") - epoch) // pd.Timedelta("1s"))]
    multi = isinstance(df.columns, pd.MultiIndex)
    out = {}
    for s in symbols:
        try:
            if not multi:
                sub = df
            elif s in df.columns.get_level_values(0):
                sub = df[s]
            elif s in df.columns.get_level_values(1):
                sub = df.xs(s, axis=1, level=1)
            else:
                continue
            o, h, l, c, v = (sub[k].to_numpy() for k in ("Open", "High", "Low", "Close", "Volume"))
        except Exception:
            continue
        d = {}
        for i, ts in enumerate(tsarr):
            ci = c[i]
            if ci != ci:   # NaN
                continue
            oi, hi, li = o[i], h[i], l[i]
            oi = ci if oi != oi else oi
            hi = max(oi, ci) if hi != hi else hi
            li = min(oi, ci) if li != li else li
            vi = v[i]
            d[ts] = (float(oi), float(hi), float(li), float(ci), 0.0 if vi != vi else float(vi))
        if d:
            out[s] = d
    return out


def ses_start(now):
    """Şu anki seansın başladığı dakika (en fazla 8 saat geriye bakar)."""
    ses = cal.session(now)
    t = int(now // 60 * 60)
    while cal.session(t - 60) == ses and now - t < 8 * 3600:
        t -= 60
    return t


async def iex_ext_volume(symbols):
    """Öncesi/sonrası seansta 1 dk hacimleri Alpaca IEX'ten çek ve mumlara yaz.
    Not: IEX tüm piyasa hacminin küçük bir kısmıdır; sinyal eşikleri buna göre ölçeklenir (sinyal.py)."""
    if not (ALPACA_KEY and ALPACA_SECRET) or not symbols:
        return
    now = time.time()
    if cal.session(now) not in ("pre", "post"):
        return
    start = ses_start(now)
    if state.xvol_last:
        start = max(start, int(state.xvol_last) - 600)
    params = {"symbols": ",".join(symbols), "timeframe": "1Min", "feed": "iex", "limit": 10000,
              "start": datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    got = {}
    async with httpx.AsyncClient(timeout=20) as c:
        for _ in range(10):
            r = await c.get("https://data.alpaca.markets/v2/stocks/bars", params=params, headers=alpaca_headers())
            r.raise_for_status()
            js = r.json()
            for sym, rows in (js.get("bars") or {}).items():
                d = got.setdefault(sym, {})
                for b in rows or []:
                    ts = int(parse_ts(b.get("t", "")) // 60 * 60)
                    if cal.session(ts) in ("pre", "post"):
                        d[ts] = float(b.get("v") or 0)
            tok = js.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
    for sym, vols in got.items():
        if vols:
            store.set_xvol(sym, vols)
    store.flush()
    state.xvol_last = now
    state.xvol_err = ""


async def yahoo_loop():
    first = True
    while True:
        t0 = time.time()
        try:
            data = await asyncio.to_thread(yahoo_fetch, SYMBOLS, "5d" if first else "1d")
            n = sum(store.merge_yahoo(sym, bars) for sym, bars in data.items())
            store.flush()
            # Öncesi/sonrası seans: Yahoo hacmi 0 → Alpaca IEX hacmi (sinyaller bu mumlar kapanınca değerlendirilir)
            try:
                await iex_ext_volume(all_syms())
            except Exception as e:
                state.xvol_err = str(e)[:120]
                log.warning("IEX hacim hatası: %s", e)
            state.yahoo_ok = time.time()
            state.yahoo_t0 = t0
            state.yahoo_secs = state.yahoo_ok - t0
            state.yahoo_err = ""
            if first:
                for s in SYMBOLS:
                    store.snap_ver[s] = store.snap_ver.get(s, 0) + 1
                log.info("Yahoo ilk yükleme: %d sembol, %.1f sn", len(data), state.yahoo_secs)
            first = False
            if len(data) < len(SYMBOLS) * 0.8:
                state.yahoo_err = f"{len(SYMBOLS) - len(data)} sembol boş geldi"
            # koşan küçük hisseler (tarayıcıdan)
            runs = [r for r in scanner.runners if r not in SYMBOLS]
            if runs:
                for r in runs:
                    ensure_sym(r)
                new = [r for r in runs if not store.bars.get(r)]
                old = [r for r in runs if store.bars.get(r)]
                for group, per in ((new, "5d"), (old, "1d")):
                    if not group:
                        continue
                    try:
                        rd = await asyncio.to_thread(yahoo_fetch, group, per)
                        for sym, bars in rd.items():
                            if store.merge_yahoo(sym, bars) and per == "5d":
                                store.snap_ver[sym] += 1
                    except Exception as e:
                        log.warning("Yahoo (koşan hisseler) hatası: %s", e)
                store.flush()
                scanner.prune(runner_pct, lambda sym: any(p["sym"] == sym for p in bot.open_positions()))
                engine.runners = scanner.runners
            log.debug("Yahoo: %d sembol değişti", n)
        except Exception as e:
            state.yahoo_err = str(e)[:200]
            log.warning("Yahoo hatası: %s", e)
        # Kısa adımlarla bekle; seans değişince hemen çek
        ses0 = cal.session(time.time())
        wait = POLL_ACTIVE if ses0 != "closed" else POLL_IDLE
        if state.yahoo_err and first:
            wait = 30
        while time.time() - t0 < wait:
            await asyncio.sleep(5)
            if cal.session(time.time()) != ses0:
                break


# ----------------------------------------------------------------- Alpaca IEX
def parse_ts(s):
    try:
        s = s.rstrip("Z")
        main, _, frac = s.partition(".")
        dt = datetime.fromisoformat(main)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp() + (float("0." + frac[:6]) if frac[:6].isdigit() else 0.0)
    except Exception:
        return time.time()


async def alpaca_loop():
    if not (ALPACA_KEY and ALPACA_SECRET):
        state.iex = "anahtar yok (.env)"
        log.warning("Alpaca anahtarı yok; sadece Yahoo verisi kullanılacak")
        return
    url = "wss://stream.data.alpaca.markets/v2/iex"
    backoff = 2
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, max_size=2 ** 22) as ws:
                await ws.recv()
                await ws.send(json.dumps({"action": "auth", "key": ALPACA_KEY, "secret": ALPACA_SECRET}))
                resp = json.loads(await ws.recv())
                err = next((m for m in resp if m.get("T") == "error"), None)
                if err:
                    code = err.get("code")
                    if code == 406:
                        raise RuntimeError("bağlantı sınırı: başka bir yerde de çalışıyor olabilir (406)")
                    if code in (401, 402):
                        raise RuntimeError(f"anahtar reddedildi ({code})")
                    raise RuntimeError(f"Alpaca hata {code}: {err.get('msg')}")
                await ws.send(json.dumps({"action": "subscribe", "trades": IEX_SYMBOLS}))
                state.iex = "bağlı"
                backoff = 2
                log.info("Alpaca IEX bağlandı, %d sembol", len(IEX_SYMBOLS))
                async for raw in ws:
                    for m in json.loads(raw):
                        typ = m.get("T")
                        if typ == "t":
                            store.on_trade(m.get("S"), float(m.get("p", 0)), parse_ts(m.get("t", "")))
                        elif typ == "error":
                            log.warning("Alpaca akış hatası: %s", m)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state.iex = f"koptu: {str(e)[:80]}"
            log.warning("Alpaca bağlantısı koptu: %s (%ss sonra tekrar)", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


# ----------------------------------------------------------------- tarayıcı
class Client:
    def __init__(self, ws):
        self.ws = ws
        self.sym = None
        self.ver = -1
        self.snap = -1
        self.wl = {}
        self.sigv = -1
        self.botv = -1
        self.scanv = -1
        self.lock = asyncio.Lock()

    async def send(self, obj):
        async with self.lock:
            await self.ws.send_text(json.dumps(obj, separators=(",", ":")))


clients = set()


async def send_snap(cl):
    sym = cl.sym
    cl.ver = store.ver.get(sym, 0)
    cl.snap = store.snap_ver.get(sym, 0)
    bars = snapshot_bars(sym)
    since = bars[0][0] if bars else 0
    await cl.send({"type": "snap", "sym": sym, "bars": bars, "i": summary(sym),
                   "sigs": engine.for_symbol(sym, since), "lv": levels(sym)})


async def send_sig(cl):
    cl.sigv = engine.ver
    await cl.send({"type": "sig", "list": engine.recent(60), "stats": engine.stats(),
                   "mine": engine.for_symbol(cl.sym, time.time() - 4 * 86400) if cl.sym else [],
                   "learn": {"ins": learner.insights(), "groups": learner.group_rows(), "cmp": compare_rows()}})


async def send_bot(cl):
    cl.botv = bot.ver
    await cl.send({"type": "bot", "d": bot.summary()})


async def send_scan(cl):
    cl.scanv = scanner.ver
    await cl.send({"type": "scan", "rows": scanner.rows(), "status": scanner.status})


async def send_wl(cl, full=False):
    rows = {}
    for s in all_syms():
        r = list_row(s)
        if r and s in scanner.runners:
            r["run"] = 1
        if r and (full or cl.wl.get(s) != r):
            rows[s] = r
            cl.wl[s] = r
    await cl.send({"type": "wl", "rows": rows, "status": status_payload()})


async def broadcaster():
    tick = 0
    while True:
        await asyncio.sleep(1)
        tick += 1
        for cl in list(clients):
            try:
                sym = cl.sym
                if sym:
                    if cl.snap != store.snap_ver.get(sym, 0):
                        await send_snap(cl)
                    elif cl.ver != store.ver.get(sym, 0):
                        cl.ver = store.ver[sym]
                        bars = store.bars.get(sym) or {}
                        last = sorted(bars)[-5:]
                        await cl.send({"type": "bar", "sym": sym,
                                       "bars": [bar_out(ts, bars[ts]) for ts in last],
                                       "i": summary(sym), "lv": levels(sym)})
                if cl.sigv != engine.ver:
                    await send_sig(cl)
                if cl.botv != bot.ver or (tick % 3 == 0 and bot.open_positions()):
                    await send_bot(cl)
                if cl.scanv != scanner.ver:
                    await send_scan(cl)
                if tick % 3 == 0:
                    await send_wl(cl)
            except Exception:
                clients.discard(cl)


async def signal_loop():
    """Her 5 sn: yeni kapanan mumlarda sinyal ara, açık sinyalleri gölgede takip et."""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        cu = state.yahoo_t0
        engine.runners = scanner.runners
        try:
            learner.rebuild(engine.signals)
        except Exception as e:
            log.warning("Öğrenme hatası: %s", e)
        syms = all_syms()
        order = (["SPY"] if "SPY" in syms else []) + [s for s in syms if s != "SPY"]
        for sym in order:
            key = (store.ver.get(sym, 0), store.snap_ver.get(sym, 0), int(cu))
            if engine.seen.get(sym) == key:
                continue
            engine.seen[sym] = key
            try:
                before = len(engine.signals)
                engine.process(sym, cu, now)
                for sig in engine.signals[before:]:
                    log.info("SİNYAL %s %s %s güven=%s giriş=%s stop=%s hedef=%s", sig["sym"],
                             "AL" if sig["dir"] > 0 else "SAT", sig["setup"], sig["conf"], sig["e"], sig["s"], sig["h"])
                    bot.consider(sig, now)
            except Exception as e:
                log.warning("Sinyal hatası %s: %s", sym, e)
        try:
            engine.tick(now)
        except Exception as e:
            log.warning("Sinyal süre kontrolü hatası: %s", e)


async def scan_loop():
    while True:
        ses = cal.session(time.time())
        if ses != "closed" or not scanner.last_ok:
            await scanner.scan(ET)
            engine.runners = scanner.runners
            for r in scanner.runners:
                ensure_sym(r)
        await asyncio.sleep(60 if ses != "closed" else 600)


async def bot_loop():
    while True:
        await asyncio.sleep(2)
        try:
            await bot.manage()
        except Exception as e:
            log.warning("Bot döngüsü hatası: %s", e)


def compare_rows():
    """Tüm sinyaller vs botun seçtikleri vs botun gerçek işlemleri."""
    def pack(k, rs, extra=None):
        n = len(rs)
        row = {"k": k, "n": n, "wr": round(100 * sum(1 for r in rs if r > 0) / n, 1) if n else None,
               "avg": round(sum(rs) / n, 3) if n else None, "tot": round(sum(rs), 2)}
        if extra:
            row.update(extra)
        return row
    closed = [s for s in engine.signals if s.get("r") is not None and s["st"] in ("hedef", "stop", "süre")]
    chosen = {p["sig"] for p in bot.positions if p["st"] != "iptal"}
    real = [p for p in bot.positions if p["st"] == "kapandı" and p.get("r") is not None]
    return [pack("Tüm sinyaller (gölge)", [s["r"] for s in closed]),
            pack("Botun seçtikleri (gölge)", [s["r"] for s in closed if s["id"] in chosen]),
            pack("Botun gerçek işlemleri (Alpaca)", [p["r"] for p in real],
                 {"usd": round(sum(p["pnl"] for p in real), 2)})]


async def housekeeping():
    n = 0
    while True:
        await asyncio.sleep(15)
        n += 1
        try:
            store.flush()
            if n % 240 == 0:   # saatte bir
                await cal.refresh()
                store.prune()
                store.prune_xvol()
            if backup.enabled and (n * 15) % BACKUP_SEC < 15:
                await backup.save(engine, bot)
            elif backup.enabled and bot.dirty_days and time.time() - (backup.last_ok or 0) > 60:
                # İşlem açılıp kapandıkça dakikada bir yedekle: yeniden başlamada kayıt kaybolmasın
                await backup.save(engine, bot)
        except Exception as e:
            log.warning("Bakım hatası: %s", e)


# ----------------------------------------------------------------- uygulama
@asynccontextmanager
async def lifespan(app):
    if not SITE_PASSWORD:
        log.warning("SITE_PASSWORD boş: site şifresiz açık!")
    store.load()
    await cal.refresh(force=True)
    sigs, botdays = await backup.restore()
    engine.load(sigs)
    bot.load(botdays)
    learner.rebuild(engine.signals)
    backup.record_start()
    await backup.save(engine, bot)
    try:
        await bot.reconcile()
    except Exception as e:
        log.warning("Bot senkron hatası: %s", e)
    tasks = [asyncio.create_task(f()) for f in (yahoo_loop, alpaca_loop, broadcaster, signal_loop, housekeeping,
                                                  scan_loop, bot_loop)]
    yield
    for t in tasks:
        t.cancel()
    store.flush()
    await backup.save(engine, bot)


app = FastAPI(lifespan=lifespan)
TOKEN = hashlib.sha256(f"{SITE_USER}:{SITE_PASSWORD}:abdbot".encode()).hexdigest() if SITE_PASSWORD else ""


def authorized(request: Request) -> bool:
    if not SITE_PASSWORD:
        return True
    if request.cookies.get("tk") == TOKEN:
        return True
    h = request.headers.get("authorization", "")
    if h.startswith("Basic "):
        try:
            u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return secrets.compare_digest(u, SITE_USER) and secrets.compare_digest(p, SITE_PASSWORD)
    return False


def ask_login():
    return Response("Giriş gerekli", status_code=401)


LOGIN_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0a0e17">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="ABD·BOT"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/ikon-192.png">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<title>ABD·BOT · Giriş</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(120% 80% at 50% 0%,#16213a 0%,#0a0e17 60%);color:#e6ebf5;font:500 16px Inter,-apple-system,sans-serif}
form{width:100%;max-width:360px;margin:16px;padding:28px 22px;text-align:center;background:rgba(20,27,42,.75);border:1px solid #222c40;border-radius:24px;backdrop-filter:blur(12px)}
img{width:76px;height:76px;border-radius:20px;margin-bottom:14px;box-shadow:0 10px 30px rgba(56,189,248,.25)}
h1{margin:0 0 4px;font-size:26px;font-weight:800;letter-spacing:-.5px}p{margin:0 0 22px;color:#8b95a9;font-size:14px}
input{width:100%;box-sizing:border-box;font:600 16px Inter,sans-serif;padding:14px 16px;border-radius:14px;border:1px solid #2a3550;background:#0f1522;color:#e6ebf5;margin-bottom:12px;outline:none}
input:focus{border-color:#38bdf8}
button{width:100%;font:700 16px Inter,sans-serif;padding:14px;border:0;border-radius:14px;background:linear-gradient(135deg,#38bdf8,#6366f1);color:#fff;cursor:pointer}
button:active{opacity:.85}
.err{color:#fca5a5;background:rgba(239,68,68,.12);border-radius:12px;padding:9px;font-size:14px;margin:0 0 12px}</style></head><body>
<form method="post" action="/giris"><img src="/ikon-192.png" alt=""><h1>ABD·BOT</h1><p>Canlı sinyal ve sanal işlem botu</p>
__ERR__<input type="password" name="sifre" placeholder="Şifre" autocomplete="current-password" autofocus required>
<button type="submit">Giriş yap</button></form></body></html>"""


def login_page(err=""):
    msg = f'<div class="err">{err}</div>' if err else ""
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", msg), headers={"Cache-Control": "no-store"})


@app.get("/")
async def index(request: Request):
    if not authorized(request):
        return login_page()
    resp = HTMLResponse((BASE / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})
    if SITE_PASSWORD:
        resp.set_cookie("tk", TOKEN, max_age=30 * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return resp


_login_fail = {"n": 0, "t": 0.0}


@app.post("/giris")
async def giris(request: Request):
    from urllib.parse import parse_qs
    if not SITE_PASSWORD:
        return Response(status_code=303, headers={"Location": "/"})
    # Kaba kuvvet denemelerine karşı: üst üste 5 hatalı denemeden sonra 60 sn bekletir
    if _login_fail["n"] >= 5 and time.time() - _login_fail["t"] < 60:
        return login_page("Çok fazla hatalı deneme. 1 dakika sonra tekrar dene.")
    body = (await request.body()).decode("utf-8", "ignore")
    pw = (parse_qs(body).get("sifre") or [""])[0]
    if not secrets.compare_digest(pw, SITE_PASSWORD):
        _login_fail["n"] += 1
        _login_fail["t"] = time.time()
        await asyncio.sleep(1)
        return login_page("Şifre yanlış.")
    _login_fail["n"] = 0
    resp = Response(status_code=303, headers={"Location": "/"})
    resp.set_cookie("tk", TOKEN, max_age=30 * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return resp


MANIFEST = {
    "name": "ABD·BOT", "short_name": "ABD·BOT", "description": "ABD borsası canlı sinyal ve sanal işlem botu",
    "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
    "background_color": "#0a0e17", "theme_color": "#0a0e17", "lang": "tr",
    "icons": [
        {"src": "/ikon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/ikon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}
SW_JS = """// ABD·BOT servis çalışanı: uygulama olarak yüklenebilmesi için. Canlı veri asla önbelleğe alınmaz.
const C='abdbot-v1';
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.addAll(['/ikon-192.png','/ikon-512.png'])))});
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
  if(u.pathname.startsWith('/ikon-'))e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));});
"""


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
async def sw():
    return Response(SW_JS, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/ikon-192.png")
async def ikon192():
    return Response(ikon.ICON192, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/ikon-512.png")
async def ikon512():
    return Response(ikon.ICON512, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/apple-touch-icon.png")
async def apple_icon():
    return Response(ikon.APPLE, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.api_route("/saglik", methods=["GET", "HEAD"])
async def saglik():
    # Şifresiz, veri içermez. Uyanık tutma servisi (UptimeRobot) bunu yoklar.
    return Response("ok", media_type="text/plain")


@app.get("/api/durum")
async def durum(request: Request):
    if not authorized(request):
        return ask_login()
    return JSONResponse({**status_payload(), "semboller": len(SYMBOLS),
                         "mum": sum(len(b) for b in store.bars.values()), "istemci": len(clients)})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if SITE_PASSWORD and ws.cookies.get("tk") != TOKEN:
        await ws.close(code=4401)
        return
    await ws.accept()
    cl = Client(ws)
    clients.add(cl)
    try:
        await cl.send({"type": "hello", "symbols": SYMBOLS, "iex": IEX_SYMBOLS})
        await send_wl(cl, full=True)
        await send_sig(cl)
        await send_bot(cl)
        await send_scan(cl)
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("daily"):
                dsym = str(msg["daily"]).upper()[:8]
                if dsym in store.bars:
                    await cl.send({"type": "daily", "sym": dsym, "bars": await get_daily(dsym)})
                continue
            sym = str(msg.get("sub", "")).upper()
            if sym in store.bars:
                cl.sym = sym
                await send_snap(cl)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("İstemci hatası: %s", e)
    finally:
        clients.discard(cl)
