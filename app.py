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
import re
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import websockets
import yfinance as yf

import analiz
import bot as botmod
import geriye
import formasyon
try:
    import grafik                  # Telegram mesajlarına grafik resmi (Pillow)
except Exception:
    grafik = None
import sektor
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
    "capital": float(os.getenv("BOT_CAPITAL", "250")),       # botun toplam parası ($); bağlıysa yeni işlem yok
    "notional": float(os.getenv("BOT_NOTIONAL", "250")),     # işlem başı pozisyon büyüklüğü ($)
    "max_open": int(os.getenv("BOT_MAX_OPEN", "15")),        # aynı anda en fazla açık pozisyon
    "risk_usd": float(os.getenv("BOT_RISK", "5")),           # işlem başı en fazla risk ($), stop mesafesine göre adet
    "daily_loss": float(os.getenv("BOT_DAILY_LOSS", "50")),  # günlük zarar limiti ($)
    "min_conf": int(os.getenv("BOT_MIN_CONF", "55")),        # botun alacağı en düşük güven puanı (normal seans)
    "min_conf_ext": int(os.getenv("BOT_MIN_CONF_EXT", "50")),# piyasa öncesi/sonrası için eşik (yarım lot + limit emir)
    "yeni_islem": 1,                                          # 0: yeni işlem açma (açık pozisyonlar yönetilmeye devam eder)
    "sektor_max": 2,                                          # aynı sektörde en fazla açık pozisyon (0 = sınırsız)
}
RUN_MIN_PRICE = float(os.getenv("RUN_MIN_PRICE", "0"))       # koşan hisselerde en düşük fiyat (0 = sınır yok)
RUN_MIN_PCT = float(os.getenv("RUN_MIN_PCT", "10"))          # en az yükseliş yüzdesi
RUN_MAX = int(os.getenv("RUN_MAX", "25"))                    # aynı anda izlenecek en fazla koşan hisse
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
logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # Yahoo'nun "Invalid Crumb" gürültüsü hata kaydını doldurmasın


class _HataKaydi(logging.Handler):
    """Uyarı ve hataları uygulamadaki 'Hata kaydı' kartı için bellekte tutar (son 60)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.items = []
        self.ver = 0

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        self.items.append({"t": int(record.created), "lv": record.levelname, "m": msg[:220]})
        del self.items[:-60]
        self.ver += 1


HATALAR = _HataKaydi()
logging.getLogger().addHandler(HATALAR)


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
                           max_runners=RUN_MAX, log=log,
                           ai_key=os.getenv("GEMINI_API_KEY", ""), ai_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
scanner.sec_ua = os.getenv("SEC_UA", "abdbot paper-trading bot")
scanner.keep = lambda sym: any(p["sym"] == sym for p in bot.open_positions())
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
    # Grafik okuma (5 dk mumlar, son ~3 gün): ana destek/direnç, formasyonlar, alıcı bölgeleri
    try:
        rows = [(t, *bars[t][:5]) for t in tss[-1100:] if bar_info(t)[1] != 3]
        fx = formasyon.analyze(formasyon.to5m(rows)[-260:])
    except Exception as e:
        log.debug("Grafik analizi hatası %s: %s", sym, e)
        fx = {"lv": [], "pat": [], "zones": [], "atr": 0}
    out = {"lv": lv,
           "res": min(ab, key=lambda x: x["p"]) if ab else None,
           "sup": max(be, key=lambda x: x["p"]) if be else None,
           "fx": fx}
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
        "bil": EARN_ST[0],
        "bkAge": int(now - backup.last_ok) if backup.last_ok else None,
        "rs7": backup.starts_last_7d() if backup.enabled else None,
        "bot": bot.status,
        "scan": scanner.status,
        "ai": scanner.ai_status,
        "tg": {"ok": tg.ok, "st": tg.status, "grafik": GRAFIK_ST[0]},
        "hava": {k: HAVA.get(k) for k in ("etiket", "puan", "neden", "vix", "genislik")},
        "push": {"ok": push.ok, "n": len(push.subs), "st": push.status, "pref": PUSH_PREF},
        "sec": scanner.sec_status,
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
            runs = [r for r in scanner.runners if r not in SYMBOLS] + viewed_syms()
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


# ----------------------------------------------------------------- ayarlar (uygulamadan değiştirilebilir)
AYAR_DEF = {"capital": (float, 10, 1e6), "notional": (float, 5, 1e6), "risk_usd": (float, 0.1, 1e5),
            "max_open": (int, 1, 50), "daily_loss": (float, 1, 1e6), "min_conf": (int, 0, 100),
            "min_conf_ext": (int, 0, 100), "yeni_islem": (int, 0, 1), "sektor_max": (int, 0, 20)}
PUSH_PREF = {"push_islem": 1, "push_plan": 1, "push_haber": 0, "push_rapor": 1,
             "tg_islem": 1, "tg_plan": 1, "tg_haber": 0, "tg_rapor": 1, "tg_grafik": 1}

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT", "")


class Telegram:
    """Akış mesajlarını kendi Telegram kanalına/grubuna gönderir (Telegram Bot API, ücretsiz).
    Plan, sinyal ve işlem mesajlarına o anki grafiğin resmi eklenir (çizgiler, formasyon, giriş/stop/hedef)."""

    def __init__(self):
        self.queue = []
        self.status = "kapalı (TG_TOKEN / TG_CHAT yok)" if not (TG_TOKEN and TG_CHAT) else "hazır"

    @property
    def ok(self):
        return bool(TG_TOKEN and TG_CHAT)

    def send(self, html, chart=None):
        if self.ok:
            self.queue.append({"html": html[:3900], "chart": chart})
            del self.queue[:-30]

    def _chart_data(self, ch):
        """Ana döngüde (veri değişmeden) grafik için 5 dk mumları ve çizimleri topla."""
        sym = ch["sym"]
        bars = store.bars.get(sym) or {}
        if len(bars) < 60:
            return None
        tss = sorted(bars)[-1100:]
        rows = [(t, *bars[t][:5]) for t in tss if bar_info(t)[1] != 3]
        b5 = formasyon.to5m(rows)[-260:]
        fx = (levels(sym) or {}).get("fx") or {}
        return b5, fx

    async def _photo(self, c, item):
        ch = item["chart"]
        data = self._chart_data(ch)
        if not data:
            return False
        png = await asyncio.to_thread(grafik.ciz, data[0], data[1], ch.get("title", ""), ch.get("sub", ""),
                                      ch.get("lines"), ch.get("pat"), ch.get("zone"))
        if not png:
            return False
        cap = item["html"]
        if len(cap) > 1000:
            cap = cap[:1000] + "…"
        r = await c.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                         data={"chat_id": TG_CHAT, "caption": cap, "parse_mode": "HTML"},
                         files={"photo": ("grafik.png", png, "image/png")})
        return r

    async def loop(self):
        async with httpx.AsyncClient(timeout=30) as c:
            while True:
                await asyncio.sleep(3)
                if not self.queue:
                    continue
                batch, self.queue = self.queue[:], []
                use_chart = bool(grafik) and PUSH_PREF.get("tg_grafik", 1)
                charts = [x for x in batch if x.get("chart") and use_chart][:5]       # sel gelirse en fazla 5 resim
                texts = [x["html"] for x in batch if x not in charts]
                if len(texts) > 6:     # Telegram sınırı ~20 mesaj/dk: düz metinleri birleştir
                    texts = ["\n\n".join(texts[i:i + 6]) for i in range(0, len(texts), 6)]
                jobs = [("p", x) for x in charts] + [("t", m) for m in texts]
                for kind, m in jobs:
                    try:
                        r = None
                        if kind == "p":
                            try:
                                r = await self._photo(c, m)
                            except Exception as e:
                                log.warning("Telegram grafiği çizilemedi: %s", e)
                                r = None
                            if r in (None, False) or r.status_code != 200:
                                if r not in (None, False):
                                    log.warning("Telegram resmi gönderilemedi: %s", r.text[:160])
                                m = m["html"]          # resim olmazsa düz metin gönder
                                r = None
                        if r is None:
                            r = await c.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                             json={"chat_id": TG_CHAT, "text": m, "parse_mode": "HTML",
                                                   "disable_web_page_preview": True})
                        if r.status_code == 200:
                            self.status = "çalışıyor"
                        else:
                            self.status = f"hata {r.status_code}: {r.text[:100]}"
                            log.warning("Telegram gönderilemedi: %s", r.text[:160])
                        if r.status_code == 429:
                            await asyncio.sleep(20)
                    except Exception as e:
                        self.status = f"hata: {str(e)[:80]}"
                    await asyncio.sleep(1.5)

    async def find_chats(self):
        """Bota yazılmış son mesajlardan sohbet kimliklerini bul (kurulum yardımı)."""
        if not TG_TOKEN:
            return []
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")
            out = {}
            for u in (r.json() or {}).get("result", []):
                ch = (u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}).get("chat") or {}
                if ch.get("id"):
                    out[ch["id"]] = ch.get("title") or ch.get("username") or ch.get("first_name") or ""
            return [{"id": k, "ad": v} for k, v in out.items()]


tg = Telegram()
GRAFIK_ST = ["Pillow kurulu değil" if grafik is None else "hazırlanıyor"]


async def font_task():
    if grafik is None:
        return
    try:
        GRAFIK_ST[0] = await asyncio.to_thread(grafik.indir)
    except Exception as e:
        GRAFIK_ST[0] = f"hata: {str(e)[:60]}"


def _h(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def apply_ayar(d):
    for k, v in (d or {}).items():
        try:
            if k in AYAR_DEF:
                typ, lo, hi = AYAR_DEF[k]
                BOT_CFG[k] = typ(min(hi, max(lo, float(v))))
            elif k in PUSH_PREF:
                PUSH_PREF[k] = 1 if v else 0
        except (TypeError, ValueError):
            continue


# ----------------------------------------------------------------- telefon bildirimleri (Web Push)
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
    PUSH_LIB = True
except Exception:
    PUSH_LIB = False

_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _seed(tag):
    return hashlib.sha256(f"{tag}:{ALPACA_SECRET or SITE_PASSWORD or 'abdbot'}".encode()).digest()


class Push:
    """Anahtarlar gizli bilgiden türetilir (her açılışta aynı, hiçbir yere yazılmaz).
    Abonelikler yedeğe ŞİFRELİ yazılır (yedek deposu herkese açık olsa bile okunamaz)."""

    def __init__(self):
        self.subs = []
        self.queue = []
        self.ok = False
        self.status = "kütüphane yok (requirements.txt: pywebpush)" if not PUSH_LIB else "hazır"
        if not PUSH_LIB:
            return
        try:
            n = int.from_bytes(_seed("vapid"), "big") % (_P256_N - 1) + 1
            priv = ec.derive_private_key(n, ec.SECP256R1())
            pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
            self.vapid = Vapid02.from_pem(pem)
            raw = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            self.pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
            self.fernet = Fernet(base64.urlsafe_b64encode(_seed("push-enc")))
            mail = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", os.getenv("SEC_UA", ""))
            self.claims = {"sub": "mailto:" + (mail.group(0) if mail else "bildirim@abdbot.app")}
            self.ok = True
        except Exception as e:
            self.status = f"hata: {str(e)[:80]}"

    def load(self, data):
        if not self.ok or not data.get("push_enc"):
            return
        try:
            self.subs = json.loads(self.fernet.decrypt(data["push_enc"].encode()))
        except Exception:
            self.subs = []

    def save(self):
        if self.ok:
            backup.data["push_enc"] = self.fernet.encrypt(json.dumps(self.subs).encode()).decode()
            backup.dirty = True

    def add(self, sub):
        if not (isinstance(sub, dict) and str(sub.get("endpoint", "")).startswith("https://") and sub.get("keys")):
            return False
        self.subs = [x for x in self.subs if x.get("endpoint") != sub["endpoint"]][-9:] + [
            {"endpoint": sub["endpoint"], "keys": sub["keys"]}]
        self.save()
        return True

    def notify(self, title, body, tag="", url="/"):
        if self.ok and self.subs:
            self.queue.append({"title": title[:80], "body": body[:180], "tag": tag, "url": url})
            del self.queue[:-20]

    def _send_all(self, msg):
        dead = []
        for sub in list(self.subs):
            try:
                webpush(sub, json.dumps(msg), vapid_private_key=self.vapid, vapid_claims=dict(self.claims), ttl=600,
                        timeout=10)
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (404, 410):
                    dead.append(sub["endpoint"])
                self.status = f"gönderim hatası {code}"
            except Exception as e:
                self.status = f"gönderim hatası: {str(e)[:60]}"
        if dead:
            self.subs = [x for x in self.subs if x["endpoint"] not in dead]
            self.save()

    async def loop(self):
        while True:
            await asyncio.sleep(3)
            if not self.queue:
                continue
            batch, self.queue = self.queue[:], []
            if len(batch) > 4:   # çok mesaj birikmişse tek bildirimde özetle
                batch = batch[-3:] + [{"title": f"{len(batch) - 3} yeni mesaj daha", "body": "Akış'ta görmek için dokun",
                                       "tag": "ozet", "url": "/"}]
            for m in batch:
                await asyncio.to_thread(self._send_all, m)


push = Push()


# ----------------------------------------------------------------- canlı akış
# Haberler, izleme planları, sinyaller ve bot olayları tek bir zaman akışında (Telegram kanalı gibi).
FEED = deque(maxlen=400)
FEED_KEYS = set()
feed_pending = []
_feed_seq = [0]


def fp_(x):
    if x is None:
        return "—"
    return f"{x:.4f}" if x < 1 else f"{x:.2f}"


def feed_add(k, sym, txt, sub="", tone="", t=None, url="", key=None, extra=None):
    """k: haber | plan | sinyal | bot | tara. tone: iyi | kötü | nötr | al | sat | kar | zarar | bilgi."""
    if key:
        if key in FEED_KEYS:
            return
        FEED_KEYS.add(key)
        if len(FEED_KEYS) > 5000:
            FEED_KEYS.clear()
    _feed_seq[0] += 1
    it = {"id": _feed_seq[0], "t": int(t or time.time()), "k": k,
          "sym": sym if isinstance(sym, list) else ([sym] if sym else []),
          "txt": txt, "sub": sub, "tone": tone, "url": url}
    if extra:
        it.update(extra)
    FEED.append(it)
    feed_pending.append(it)
    # telefon bildirimi (Ayarlar'daki tercihlere göre)
    s0 = it["sym"][0] if it["sym"] else ""
    if k == "bot" and PUSH_PREF["push_islem"] and tone in ("al", "sat", "kar", "zarar"):
        push.notify(f"#{s0} {txt}", sub or "Bot sekmesinde ayrıntılar", tag=f"bot-{s0}")
    elif k == "plan" and PUSH_PREF["push_plan"]:
        push.notify(f"#{s0} {txt}", sub, tag=f"plan-{s0}")
    elif k == "haber" and PUSH_PREF["push_haber"] and tone in ("iyi", "kötü") and (extra or {}).get("watched"):
        push.notify(f"#{s0} {'olumlu' if tone == 'iyi' else 'olumsuz'} haber", txt, tag=f"haber-{s0}", url=url or "/")
    # Telegram kanalı
    tags = " ".join("#" + _h(x) for x in it["sym"])
    icon = {"al": "🟢", "sat": "🔴", "kar": "✅", "zarar": "❌"}.get(tone, "")
    ex = extra or {}
    chart = None
    if s0 and k in ("bot", "sinyal", "plan") and (k != "bot" or ex.get("e")):
        chart = {"sym": s0, "title": f"#{s0}  {txt}", "sub": sub,
                 "lines": {"giris": ex.get("e") or ex.get("lvl"), "stop": ex.get("s") or ex.get("stop"),
                           "hedef": ex.get("h") or ex.get("tgt")},
                 "pat": ex.get("pat"), "zone": bool(ex.get("zone"))}
    if k in ("bot", "sinyal") and PUSH_PREF["tg_islem"] and (k == "bot" or ex.get("conf", 0) >= 70):
        tg.send(f"{icon} <b>{tags} {_h(txt)}</b>" + (f"\n{_h(sub)}" if sub else ""), chart)
    elif k == "plan" and PUSH_PREF["tg_plan"]:
        tg.send(f"👀 <b>{tags} {_h(txt)}</b>" + (f"\n{_h(sub)}" if sub else ""), chart)
    elif k == "haber" and PUSH_PREF["tg_haber"] and tone in ("iyi", "kötü") and (extra or {}).get("watched"):
        tg.send(f"📰 {tags} <b>{'Olumlu' if tone == 'iyi' else 'Olumsuz'} haber:</b> {_h(txt)}")
    elif k == "rapor" and PUSH_PREF["tg_rapor"]:
        tg.send(f"📋 <b>{_h(txt)}</b>\n{_h(sub)}")


def feed_signal(sig):
    d = "AL" if sig["dir"] > 0 else "SAT"
    ad = sinyal.SETUP_AD.get(sig["setup"], sig["setup"])
    feed_add("sinyal", sig["sym"], f"{d} sinyali: {ad}",
             sub=f"giriş {fp_(sig['e'])}   stop {fp_(sig['s'])}   hedef {fp_(sig['h'])}   güven {sig['conf']}",
             tone="al" if sig["dir"] > 0 else "sat", t=sig.get("created") or sig["t"], key=f"s:{sig['id']}",
             extra={"conf": sig["conf"], "e": sig["e"], "s": sig["s"], "h": sig["h"]})


_RX_FILL = re.compile(r"Giriş doldu: ([\d.]+) adet @ ([\d.]+)")
_RX_CLOSE = re.compile(r"Kapandı \(([^)]+)\): ([+-][\d.]+) \$ · ([+-][\d.]+)R")


def feed_bot_note(typ, msg, sym, t):
    if typ == "dolum":
        m = _RX_FILL.search(msg)
        pos = next((p for p in bot.positions if p["sym"] == sym and p["st"] in botmod.OPEN_ST), None)
        side = "Açığa sattım" if pos and pos["dir"] < 0 else "İçerdeyim"
        txt = f"{side}: {fp_(float(m.group(2)))} $'dan {float(m.group(1)):g} adet" if m else f"{side}"
        sub = f"stop {fp_(pos['stop'])}   hedef {fp_(pos['target'])}" if pos else ""
        feed_add("bot", sym, txt, sub=sub, tone="al" if not pos or pos["dir"] > 0 else "sat", t=t,
                 extra={"e": pos.get("entry") or pos.get("plan"), "s": pos["stop"], "h": pos["target"]} if pos else None)
    elif typ == "kapandı":
        m = _RX_CLOSE.search(msg)
        if m:
            why, usd, r = m.group(1), float(m.group(2)), float(m.group(3))
            head = {"hedef": "Hedef geldi", "stop": "Stop oldu", "kâr koruma": "Başa başta çıktım",
                    "iz süren stop": "Kârı aldım (iz süren stop)", "seans sonu": "Seans bitti, kapattım"}.get(why, "Kapattım")
            feed_add("bot", sym, f"{head}: {r:+.2f}R ({usd:+.2f} $)", tone="kar" if usd > 0 else "zarar", t=t)
        else:
            feed_add("bot", sym, msg, tone="bilgi", t=t)
    elif typ in ("koruma", "iz"):
        feed_add("bot", sym, "Stop girişe çekildi, bu işlem artık zarar yazmaz" if typ == "koruma"
                 else "Kâr büyüyor, stop arkadan takip ediyor", tone="kar", t=t)
    elif typ in ("hata", "uyarı"):
        feed_add("bot", sym, msg, tone="zarar", t=t)


async def news_loop():
    """Tüm piyasanın Benzinga haber akışı (Alpaca, ücretsiz): her yeni başlık akışa düşer."""
    first = True
    async with httpx.AsyncClient(timeout=15, headers=alpaca_headers()) as c:
        while True:
            try:
                if ALPACA_KEY and ALPACA_SECRET:
                    r = await c.get("https://data.alpaca.markets/v1beta1/news",
                                    params={"limit": 50, "sort": "desc"})
                    if r.status_code == 200:
                        items = r.json().get("news") or []
                        for n in reversed(items):
                            syms = [x for x in (n.get("symbols") or []) if tarayici.SYM_RE.match(x)][:4]
                            if not syms:
                                continue
                            kind, word = tarayici.haber_turu(n.get("headline", ""))
                            watched = [x for x in syms if x in scanner.runners or x in SYMBOLS]
                            ai = next(((scanner.runners[x].get("ai") or {}) for x in syms if x in scanner.runners
                                       and scanner.runners[x].get("ai_h") == n.get("headline")), {})
                            feed_add("haber", syms, n.get("headline", ""), sub=n.get("source", ""),
                                     tone=kind, t=_iso_epoch(n.get("created_at", "")), url=n.get("url", ""),
                                     key=f"n:{n.get('id')}",
                                     extra={"word": word, "watched": bool(watched), "ai": ai or None})
            except Exception as e:
                log.debug("Haber akışı hatası: %s", e)
            first = False
            await asyncio.sleep(30 if cal.session(time.time()) != "closed" else 180)


def _iso_epoch(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return int(time.time())


def feed_seed():
    """Yeniden başlamada akışı bugünkü sinyal ve bot kayıtlarından doldur."""
    today = datetime.now(ET).date().isoformat()
    evs = []
    for sig in engine.signals[-300:]:
        if sig.get("day") == today:
            evs.append((sig.get("created") or sig["t"], "s", sig))
    for j in bot.journal[-600:]:
        if j.get("day") == today and j.get("typ") in ("dolum", "kapandı", "koruma", "iz"):
            evs.append((j["t"], "b", j))
    for t, kind, o in sorted(evs, key=lambda e: e[0])[-150:]:
        if kind == "s":
            feed_signal(o)
        else:
            feed_bot_note(o["typ"], o["msg"], o["sym"], o["t"])
    feed_pending.clear()


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
                   "learn": {"ins": learner.insights(), "groups": learner.group_rows(), "cmp": compare_rows(),
                             "saat": saat_tablosu(),
                             "bt": {k: BT[k] for k in ("sum", "t", "running", "bekliyor", "prog", "hata")}}})


async def send_bot(cl):
    cl.botv = bot.ver
    d = bot.summary()
    try:
        k = d.get("karne") or {}
        k["spy"] = spy_karsilastir(k.get("eq") or [], k.get("cap0") or 250)
    except Exception:
        pass
    await cl.send({"type": "bot", "d": d})


async def send_scan(cl):
    cl.scanv = scanner.ver
    cl.patv = _pat_ver[0]
    await cl.send({"type": "scan", "rows": scanner.rows(), "status": scanner.status, "dropped": scanner.dropped_rows(),
                   "pats": PATS})


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
        fresh = feed_pending[:]
        feed_pending.clear()
        for cl in list(clients):
            if fresh:
                try:
                    await cl.send({"type": "feed", "items": fresh})
                except Exception:
                    clients.discard(cl)
                    continue
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
                if cl.sigv != engine.ver or getattr(cl, "btv", -1) != _bt_ver[0]:
                    cl.btv = _bt_ver[0]
                    await send_sig(cl)
                if cl.botv != bot.ver or (tick % 3 == 0 and bot.open_positions()):
                    await send_bot(cl)
                if cl.scanv != scanner.ver or getattr(cl, "patv", -1) != _pat_ver[0]:
                    await send_scan(cl)
                if getattr(cl, "logv", -1) != HATALAR.ver:
                    cl.logv = HATALAR.ver
                    await cl.send({"type": "log", "items": HATALAR.items[-50:][::-1]})
                if getattr(cl, "mktv", -1) != _mkt_ver[0]:
                    cl.mktv = _mkt_ver[0]
                    await cl.send({"type": "mkt", **MARKET})
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
            learner.rebuild(engine.signals + BT["sigs"])
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
                    feed_signal(sig)
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
            before = set(scanner.runners)
            await scanner.scan(ET)
            for r in set(scanner.runners) - before:
                info = scanner.runners[r]
                n = info.get("news") or {}
                feed_add("tara", r, f"Koşan hisseler listesine girdi: bugün {info.get('pct') or 0:+.0f}%",
                         sub=n.get("headline", "Haber bulunamadı")[:140], tone="iyi" if n else "nötr",
                         key=f"r:{r}:{datetime.now(ET).date()}")
            engine.runners = scanner.runners
            for r in scanner.runners:
                ensure_sym(r)
        await asyncio.sleep(30 if ses != "closed" else 600)


async def fast_loop():
    """Koşan hisseler: 5 sn'de bir Alpaca anlık fiyatı → tepe kırılımı ŞİMDİ olduysa hemen sinyal + bot."""
    async with httpx.AsyncClient(timeout=10, headers=alpaca_headers()) as c:
        while True:
            await asyncio.sleep(5)
            now = time.time()
            runs = [r for r in scanner.runners if r not in IEX_SYMBOLS]
            if not runs or not (ALPACA_KEY and ALPACA_SECRET) or cal.session(now) == "closed":
                continue
            try:
                r = await c.get("https://data.alpaca.markets/v2/stocks/snapshots",
                                params={"symbols": ",".join(runs), "feed": "iex"})
                if r.status_code != 200:
                    continue
                snaps = r.json() or {}
                if "snapshots" in snaps and isinstance(snaps["snapshots"], dict):
                    snaps = snaps["snapshots"]
            except Exception as e:
                log.debug("Anlık fiyat hatası: %s", e)
                continue
            for sym, sn in snaps.items():
                tr = (sn or {}).get("latestTrade") or {}
                p = float(tr.get("p") or 0)
                t = parse_ts(tr.get("t", "")) if tr.get("t") else 0
                if p <= 0 or now - t > 120:
                    continue                      # fiyat yok ya da 2 dk'dan eski (işlem durmuş olabilir)
                prev = float(((sn or {}).get("prevDailyBar") or {}).get("c") or 0)
                info = scanner.runners.get(sym)
                if info is not None and prev > 0:
                    info["pct_live"] = round((p / prev - 1) * 100, 2)   # Yahoo gecikirse tarayıcı bunu kullanır
                    info["live_t"] = now
                last = store.live.get(sym)
                if not last or t >= last["t"]:
                    store.live[sym] = {"p": p, "t": t}
                try:
                    plan = engine.watch_plan(sym, p, now)
                    if plan:
                        feed_add("plan", sym, f"{fp_(plan['lvl'])} üstünü kırarsa giriş",
                                 sub=f"stop {fp_(plan['stop'])}   hedef {fp_(plan['tgt'])}   şu an {fp_(p)}   "
                                     f"bugün {plan['move']:+.0f}%", tone="bilgi",
                                 extra={"lvl": plan["lvl"], "stop": plan["stop"], "tgt": plan["tgt"]})
                    sig = engine.fast_check(sym, p, now)
                    if sig:
                        log.info("HIZLI SİNYAL %s AL güven=%s giriş=%s stop=%s hedef=%s",
                                 sym, sig["conf"], sig["e"], sig["s"], sig["h"])
                        feed_signal(sig)
                        bot.consider(sig, now)
                except Exception as e:
                    log.warning("Hızlı kırılım hatası %s: %s", sym, e)


PATS = []          # bütün hisselerdeki güncel formasyonlar (Tarayıcı sekmesi)
_pat_ver = [0]


async def pattern_loop():
    """Her dakika bütün hisselerde formasyon tara; yeni oluşan formasyonu akışa yaz."""
    while True:
        await asyncio.sleep(60)
        if cal.session(time.time()) == "closed" and PATS:
            continue
        found = []
        for sym in all_syms():
            try:
                fx = (levels(sym) or {}).get("fx") or {}
            except Exception:
                continue
            last = (summary(sym) or {}).get("p")
            for p in fx.get("pat", []):
                row = dict(p, sym=sym, last=last, runner=sym in scanner.runners)
                row["uzak"] = round((p["neck_now"] / last - 1) * 100, 2) if last else None
                found.append(row)
                if p["durum"] == "oluşuyor" and last:
                    yon = "üstünü" if p["bull"] else "altını"
                    feed_add("plan", sym, f"{p['ad']} oluşuyor: {fp_(p['neck_now'])} {yon} kırarsa hedef {fp_(p['hedef'])}",
                             sub=f"şu an {fp_(last)}   boyun çizgisine {row['uzak']:+.2f}%   formasyon yüksekliği {fp_(p['boy'])}",
                             tone="bilgi" if p["bull"] else "sat",
                             key=f"p:{sym}:{p['ad']}:{p['t1']}", extra={"pat": p["ad"]})
            for z in fx.get("zones", [])[:1]:
                if last and z["hi"] < last <= z["hi"] * 1.01:
                    feed_add("plan", sym, f"Alıcı bölgesine yaklaştı: {fp_(z['lo'])}–{fp_(z['hi'])}",
                             sub=f"bu bantta hacmin %{z['alici']}'i alıcı mumlarında   şu an {fp_(last)}",
                             tone="bilgi", key=f"z:{sym}:{z['lo']}:{datetime.now(ET).date()}", extra={"zone": 1})
        found.sort(key=lambda r: (r["durum"] != "oluşuyor", abs(r["uzak"] or 99)))
        PATS[:] = found[:80]
        _pat_ver[0] += 1


# ----------------------------------------------------------------- Keşfet: sektörler, en çok yükselen/düşen/hacim
UNIVERSE = sektor.tum_semboller()
MARKET = {"sek": [], "gain": [], "lose": [], "act": [], "t": 0}
_mkt_ver = [0]


async def market_loop():
    await asyncio.sleep(8)
    async with httpx.AsyncClient(timeout=20, headers=alpaca_headers()) as c:
        while True:
            try:
                if ALPACA_KEY and ALPACA_SECRET:
                    syms = list(dict.fromkeys(UNIVERSE + [g.get("symbol") for g in scanner.actives if g.get("symbol")]))
                    snaps = {}
                    for i in range(0, len(syms), 100):
                        r = await c.get("https://data.alpaca.markets/v2/stocks/snapshots",
                                        params={"symbols": ",".join(syms[i:i + 100]), "feed": "iex"})
                        if r.status_code == 200:
                            js = r.json() or {}
                            snaps.update(js.get("snapshots", js) if isinstance(js.get("snapshots"), dict) else js)
                    today = datetime.now(ET).date().isoformat()
                    q = {}
                    for sy, sn in snaps.items():
                        if not isinstance(sn, dict):
                            continue
                        db, pdb = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
                        is_today = (db.get("t") or "")[:10] == today
                        prev = pdb.get("c") if is_today else db.get("c")
                        px = (sn.get("latestTrade") or {}).get("p") or db.get("c")
                        if px and prev:
                            q[sy] = {"p": round(px, 4), "ch": round((px / prev - 1) * 100, 2)}
                    sek = []
                    for ad, lst in sektor.SEKTORLER.items():
                        items = [{"s": x, **q[x]} for x in lst if x in q]
                        if items:
                            items.sort(key=lambda x: -x["ch"])
                            sek.append({"ad": ad, "ort": round(sum(x["ch"] for x in items) / len(items), 2), "h": items})
                    sek.sort(key=lambda x: -x["ort"])
                    mv = lambda lst: [{"s": g["symbol"], "p": g.get("price"), "ch": round(float(g.get("percent_change") or 0), 2),
                                       "sek": sektor.sektor_of(g["symbol"])} for g in lst]
                    act = [{"s": x["symbol"], "v": x.get("volume"), "p": (q.get(x["symbol"]) or {}).get("p"),
                            "ch": (q.get(x["symbol"]) or {}).get("ch"), "sek": sektor.sektor_of(x["symbol"])}
                           for x in scanner.actives]
                    for extra in ("SPY", "^VIX"):
                        await get_daily(extra)
                    hava_hesapla(q)
                    MARKET.update(sek=sek, gain=mv(scanner.gainers), lose=mv(scanner.losers), act=act, t=int(time.time()),
                                  hava=dict(HAVA), bil={k: v for k, v in ((x, bilanco_durum(x)) for x in (set(EARN) & izlenen())) if v},
                                  bilhafta=bilanco_hafta())
                    _mkt_ver[0] += 1
            except Exception as e:
                log.warning("Piyasa özeti hatası: %s", e)
            await asyncio.sleep(60 if cal.session(time.time()) != "closed" else 900)


# ----------------------------------------------------------------- piyasa havası + bilanço takvimi
HAVA = {"etiket": "bilinmiyor", "puan": 0, "neden": [], "t": 0}
EARN = {}          # sembol -> {"YYYY-MM-DD": "önce" | "sonra" | ""}  (Nasdaq bilanço takvimi, bütün ABD)
EARN_ST = ["henüz çekilmedi"]


def hava_hesapla(q):
    """SPY trendi + VIX + piyasa genişliği → Güçlü / Normal / Riskli."""
    puan, neden = 0, []
    try:
        spy = _daily_cache.get("SPY", (0, []))[1]
        vix = _daily_cache.get("^VIX", (0, []))[1]
        if len(spy) >= 21:
            closes = [x[4] for x in spy]
            e20 = analiz.ema(closes, 20)[-1]
            last = (q.get("SPY") or {}).get("p") or closes[-1]
            if last > e20:
                puan += 1
                neden.append("SPY 20 günlük ortalamanın üstünde")
            else:
                puan -= 1
                neden.append("SPY 20 günlük ortalamanın altında")
        sch = (q.get("SPY") or {}).get("ch")
        if sch is not None:
            if sch >= 0.3:
                puan += 1
            elif sch <= -0.5:
                puan -= 1
            neden.append(f"SPY bugün {sch:+.2f}%")
        if vix:
            v = vix[-1][4]
            neden.append(f"VIX {v:.1f}")
            puan += 1 if v < 15 else (-2 if v > 25 else (-1 if v > 20 else 0))
            HAVA["vix"] = round(v, 1)
        ups = [x["ch"] for x in q.values() if x.get("ch") is not None]
        if len(ups) > 30:
            g = round(100 * sum(1 for x in ups if x > 0) / len(ups))
            HAVA["genislik"] = g
            neden.append(f"hisselerin %{g}'i yükselişte")
            puan += 1 if g >= 60 else (-1 if g <= 40 else 0)
    except Exception as e:
        log.debug("Hava hesabı hatası: %s", e)
    HAVA.update(puan=puan, etiket="Güçlü" if puan >= 2 else ("Riskli" if puan <= -2 else "Normal"), neden=neden,
                t=int(time.time()))


def bilanco_durum(sym):
    ds = EARN.get(sym)
    if not ds:
        return None
    today = datetime.now(ET).date()
    for k, off in (("bugün", 0), ("dün", -1), ("yarın", 1)):
        if (today + timedelta(days=off)).isoformat() in ds:
            return k
    return None


def extra_mult(sig):
    m, notes = 1.0, []
    b = bilanco_durum(sig["sym"])
    if b in ("bugün", "dün"):
        m *= 0.5
        notes.append(f"bilanço {b}: oynaklık yüksek, lot yarıya")
    if HAVA["etiket"] == "Riskli":
        m *= 0.6
        notes.append("piyasa havası riskli, lot küçültüldü")
    return m, ", ".join(notes)


def izlenen():
    """Bilanço listesinde gösterilecek hisseler: izleme listesi, sektör listesi, koşanlar, açılanlar."""
    return set(SYMBOLS) | set(UNIVERSE) | set(scanner.runners) | set(VIEWED)


NASDAQ_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36",
              "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
              "Referer": "https://www.nasdaq.com/"}


async def earn_day(c, d):
    """Nasdaq bilanço takvimi: o gün açıklayan bütün ABD hisseleri."""
    r = await c.get("https://api.nasdaq.com/api/calendar/earnings", params={"date": d.isoformat()}, headers=NASDAQ_HDR)
    r.raise_for_status()
    rows = ((r.json() or {}).get("data") or {}).get("rows") or []
    out = {}
    for x in rows:
        sym = str(x.get("symbol") or "").strip().upper()
        if not sym:
            continue
        t = str(x.get("time") or "")
        out[sym] = "önce" if "pre" in t else ("sonra" if "after" in t else "")
    return out


async def earnings_loop():
    """6 saatte bir: dünden 7 gün sonrasına bilanço takvimi (Nasdaq, ücretsiz, anahtarsız)."""
    await asyncio.sleep(20)
    while True:
        today = datetime.now(ET).date()
        new, ok, err = {}, 0, ""
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            for i in range(-1, 8):
                d = today + timedelta(days=i)
                if d.weekday() >= 5:
                    continue
                try:
                    for sym, z in (await earn_day(c, d)).items():
                        new.setdefault(sym, {})[d.isoformat()] = z
                    ok += 1
                except Exception as e:
                    err = str(e)[:120]
                await asyncio.sleep(1.5)
        if ok:
            EARN.clear()
            EARN.update(new)
            EARN_ST[0] = f"tamam ({len(new)} hisse, {datetime.now(TR).strftime('%H:%M')})"
            _mkt_ver[0] += 1
        else:
            EARN_ST[0] = "alınamadı: " + err
            log.warning("Bilanço takvimi alınamadı: %s", err)
        await asyncio.sleep(6 * 3600 if ok else 1800)


# ----------------------------------------------------------------- geçmiş test
BT = {"sigs": [], "sum": None, "t": 0, "running": False, "bekliyor": None, "prog": "", "hata": ""}
_bt_ver = [0]
BT_PATH = "gecmis/test.json"


async def bt_load():
    if not backup.enabled:
        return
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            d = await backup._get(c, BT_PATH)
        if isinstance(d, dict):
            BT.update(sigs=d.get("sigs") or [], sum=d.get("sum"), t=d.get("t") or 0)
    except Exception as e:
        log.warning("Geçmiş test yüklenemedi: %s", e)


async def bt_run(n_days):
    BT.update(running=True, bekliyor=None, prog="başlıyor…", hata="")
    _bt_ver[0] += 1
    syms = [x for x in SYMBOLS]

    def prog(i, n, k, secs):
        BT["prog"] = f"{i}/{n} hisse · {k} sinyal · {int(secs // 60)} dk"
        _bt_ver[0] += 1
    try:
        sigs, summ = await asyncio.to_thread(geriye.run, ALPACA_KEY, ALPACA_SECRET, syms, n_days, ET, prog,
                                             lambda: cal.session(time.time()) == "regular")
        if sigs:
            BT.update(sigs=sigs, sum=summ, t=int(time.time()))
            learner._sig = None
            learner.rebuild(engine.signals + BT["sigs"])
            feed_add("bot", "", f"Geçmiş test bitti: {summ['gun']} günde {summ['tum']['n']} sinyal, ortalama "
                     f"{summ['tum'].get('avg', 0):+.2f}R", sub="Öğren sekmesinde sonuçlar. Bot artık bu verilerle de öğreniyor.",
                     tone="bilgi")
            if backup.enabled:
                async with httpx.AsyncClient(timeout=60) as c:
                    await backup._put(c, BT_PATH, {"sigs": sigs, "sum": summ, "t": BT["t"]})
        else:
            BT["hata"] = (summ or {}).get("hata") or "sinyal çıkmadı"
    except Exception as e:
        BT["hata"] = str(e)[:160]
        log.warning("Geçmiş test hatası: %s", e)
    BT.update(running=False, prog="")
    _bt_ver[0] += 1


async def bt_loop():
    """İstenen test, piyasa normal seansı kapalıyken başlatılır."""
    while True:
        await asyncio.sleep(30)
        if BT["bekliyor"] and not BT["running"] and cal.session(time.time()) != "regular":
            await bt_run(BT["bekliyor"])


def saat_tablosu():
    """Saatlere göre (TR) sinyal sonuçları: canlı (yeni kurallar) + geçmiş test."""
    rows = {}
    for s_ in list(engine.signals) + BT["sigs"]:
        r = s_.get("r")
        if r is None or s_.get("st") not in ("hedef", "stop", "süre"):
            continue
        if not s_.get("bt") and (s_.get("t") or 0) < ogrenme.LEARN_SINCE:
            continue
        h = datetime.fromtimestamp(s_["t"], TR).hour
        x = rows.setdefault(h, [0, 0.0, 0])
        x[0] += 1
        x[1] += r
        x[2] += 1 if r > 0 else 0
    return [{"h": h, "n": v[0], "avg": round(v[1] / v[0], 3), "wr": round(100 * v[2] / v[0])} for h, v in sorted(rows.items())]


def bilanco_hafta():
    today = datetime.now(ET).date()
    out = []
    w = izlenen()
    for sym, ds in EARN.items():
        if sym not in w:
            continue
        for d, z in (ds or {}).items():
            try:
                dd = date.fromisoformat(d[:10])
            except Exception:
                continue
            if 0 <= (dd - today).days <= 6:
                out.append({"s": sym, "d": dd.isoformat(), "sek": sektor.sektor_of(sym), "bugun": dd == today, "z": z})
    out.sort(key=lambda x: (x["d"], x["s"]))
    return out


def gun_sonu_raporu():
    """Seans kapanınca: botun bugünkü işlemleri + gölge sinyallerin özeti."""
    tc = bot.today_closed()
    pnl = sum(p["pnl"] for p in tc)
    w = sum(1 for p in tc if p["pnl"] > 0)
    today = datetime.now(ET).date().isoformat()
    sg = [x for x in engine.signals if x.get("day") == today and x.get("r") is not None and x["st"] in ("hedef", "stop", "süre")]
    by = {}
    for x in sg:
        by.setdefault(x["setup"], []).append(x["r"])
    rows = sorted(((k, sum(v) / len(v), len(v)) for k, v in by.items() if len(v) >= 3), key=lambda r: -r[1])
    best = f"en iyi kurgu {sinyal.SETUP_AD.get(rows[0][0], rows[0][0])} ({rows[0][1]:+.2f}R)" if rows else ""
    worst = f"en zayıf {sinyal.SETUP_AD.get(rows[-1][0], rows[-1][0])} ({rows[-1][1]:+.2f}R)" if len(rows) > 1 else ""
    avg = sum(x["r"] for x in sg) / len(sg) if sg else 0
    cap = bot.capital()[0]
    txt = f"Gün sonu: {len(tc)} işlem, {pnl:+.2f} $ ({w} kazanan)"
    sub = (f"Botun parası {cap:.2f} $. Gölge sinyaller: {len(sg)} sonuç, ort. {avg:+.2f}R"
           + (f"; {best}" if best else "") + (f"; {worst}" if worst else "") + ".")
    return txt, sub


def sabah_listesi():
    """Öncesi seans açılınca: boşlukla açılanlar, haberliler, bugünkü bilançolar, piyasa havası."""
    parts = []
    g = [f"{x['s']} {x['ch']:+.0f}%" for x in (MARKET.get("gain") or [])[:6]]
    l_ = [f"{x['s']} {x['ch']:+.0f}%" for x in (MARKET.get("lose") or [])[:4]]
    if g:
        parts.append("Yükselenler: " + ", ".join(g))
    if l_:
        parts.append("Düşenler: " + ", ".join(l_))
    news = [f"{r} ({(i.get('news_kind') or 'nötr')})" for r, i in scanner.runners.items() if i.get("news")][:6]
    if news:
        parts.append("Haberli koşanlar: " + ", ".join(news))
    bil = [x["s"] for x in bilanco_hafta() if x["bugun"]]
    if bil:
        parts.append("Bugün bilanço: " + ", ".join(bil[:12]))
    if HAVA.get("etiket") and HAVA["etiket"] != "bilinmiyor":
        parts.append(f"Piyasa havası: {HAVA['etiket']}")
    return "Günaydın: bugünün hazırlık listesi", ("\n".join(parts) or "Henüz veri yok; tarayıcı birkaç dakika içinde dolar.")


async def rapor_loop():
    """Seans geçişlerinde: öncesi seans açılışı → sabah listesi (15 dk sonra), normal seans kapanışı → gün sonu raporu."""
    last = cal.session(time.time())
    pre_at = None
    while True:
        await asyncio.sleep(30)
        now = time.time()
        ses = cal.session(now)
        if last == "closed" and ses == "pre":
            pre_at = now + 15 * 60          # tarayıcı dolsun
        if pre_at and now >= pre_at:
            pre_at = None
            t_, s_ = sabah_listesi()
            feed_add("rapor", "", t_, sub=s_, tone="bilgi")
            if PUSH_PREF["push_rapor"]:
                push.notify(t_, s_.replace("\n", " · "), tag="sabah")
        if last == "regular" and ses == "post":
            await asyncio.sleep(120)        # son işlemler kapansın
            t_, s_ = gun_sonu_raporu()
            feed_add("rapor", "", t_, sub=s_, tone="bilgi")
            if PUSH_PREF["push_rapor"]:
                push.notify(t_, s_, tag="rapor")
        last = ses


def spy_karsilastir(eq, cap0):
    """Botun sermaye eğrisi ile 'aynı parayla SPY alıp tutsaydın' eğrisi."""
    spy = (_daily_cache.get("SPY") or (0, []))[1]
    if not eq or len(spy) < 2:
        return []
    t0 = eq[0][0]
    d0 = datetime.fromtimestamp(t0, ET).date().isoformat()
    base = next((x[4] for x in spy if x[0] >= d0), None)
    if not base:
        return []
    out = []
    for t, _ in eq:
        d = datetime.fromtimestamp(t, ET).date().isoformat()
        c = next((x[4] for x in reversed(spy) if x[0] <= d), base)
        out.append([t, round(cap0 * c / base, 2)])
    return out


def bot_dusunce(sym):
    """'Bot bu hisse hakkında ne düşünüyor?' kartı."""
    today = datetime.now(ET).date().isoformat()
    sigs = [engine.slim(x) for x in engine.signals if x["sym"] == sym and x.get("day") == today][-12:]
    jr = [j for j in bot.journal if j.get("sym") == sym and j.get("day") == today][-15:]
    pos = [p for p in bot.open_positions() if p["sym"] == sym]
    fx = (levels(sym) or {}).get("fx") or {}
    return {"type": "dusun", "sym": sym, "sigs": sigs, "jr": jr, "pos": pos,
            "pat": fx.get("pat", []), "zones": fx.get("zones", []), "bil": bilanco_durum(sym),
            "sek": sektor.sektor_of(sym), "runner": scanner.runners.get(sym), "hava": HAVA.get("etiket")}


VIEWED = {}        # listede olmayan ama uygulamada açılan hisseler: 30 dk boyunca dakikalık veri çekilir


def viewed_syms():
    now = time.time()
    return [v for v, t in VIEWED.items() if now - t < 1800 and v not in SYMBOLS and v not in scanner.runners]


_m15_cache = {}


def m15_fetch(sym):
    """Kısa vade analizi için 60 günlük 15 dk mumlar (Yahoo, normal seans)."""
    df = yf.download(sym, period="60d", interval="15m", auto_adjust=False, progress=False, threads=False, prepost=False)
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = df.columns.get_level_values(0)
        df = df[sym] if sym in lv0 else (df.xs(sym, axis=1, level=1) if sym in df.columns.get_level_values(1)
                                         else df.droplevel(1, axis=1))
    out = []
    for idx, r in df.iterrows():
        try:
            o, h, l, c = float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])
            v = float(r["Volume"]) if r["Volume"] == r["Volume"] else 0.0
        except Exception:
            continue
        if c != c or o != o:
            continue
        ts = idx.tz_localize("UTC") if idx.tzinfo is None else idx
        out.append((int(ts.timestamp()), o, h, l, c, v))
    return out


async def get_m15(sym):
    hit = _m15_cache.get(sym)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        bars = await asyncio.to_thread(m15_fetch, sym)
    except Exception as e:
        log.warning("15 dk veri hatası %s: %s", sym, e)
        bars = hit[1] if hit else []
    if bars:
        _m15_cache[sym] = (time.time(), bars)
        if len(_m15_cache) > 60:
            _m15_cache.pop(min(_m15_cache, key=lambda k: _m15_cache[k][0]), None)
    return bars


async def analiz_payload(sym):
    d = await get_daily(sym)
    daily = []
    for x in d:
        try:
            daily.append((int(datetime.strptime(x[0], "%Y-%m-%d").replace(tzinfo=UTC).timestamp()),
                          x[1], x[2], x[3], x[4], x[5]))
        except Exception:
            continue
    m = await get_m15(sym)
    res = await asyncio.to_thread(analiz.full, daily, m)
    res.update(sym=sym, sektor=sektor.sektor_of(sym), t=int(time.time()))
    return res


def replay_payload(pid):
    pos = next((p for p in bot.positions if p.get("id") == pid), None)
    if not pos:
        return None
    sym = pos["sym"]
    t0 = (pos.get("et") or pos["opened"]) - 45 * 60
    t1 = (pos.get("xt") or time.time()) + 30 * 60
    bars = store.bars.get(sym) or {}
    rows = [bar_out(t, bars[t]) for t in sorted(bars) if t0 <= t <= t1]
    sig = engine.by_id.get(pos.get("sig")) or {}
    return {"type": "replay", "pos": pos, "bars": rows, "why": sig.get("why", []),
            "ad": sinyal.SETUP_AD.get(pos["setup"], pos["setup"])}


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
    await bt_load()
    learner.rebuild(engine.signals + BT["sigs"])
    apply_ayar(backup.data.get("ayar"))
    push.load(backup.data)
    feed_seed()
    bot.on_note = feed_bot_note
    bot.extra_mult = extra_mult
    bot.sector_of = sektor.sektor_of
    engine.bilanco = bilanco_durum
    engine.hava = lambda: HAVA["etiket"]
    backup.record_start()
    await backup.save(engine, bot)
    try:
        await bot.reconcile()
    except Exception as e:
        log.warning("Bot senkron hatası: %s", e)
    tasks = [asyncio.create_task(f()) for f in (yahoo_loop, alpaca_loop, broadcaster, signal_loop, housekeeping,
                                                  scan_loop, bot_loop, fast_loop, news_loop, pattern_loop,
                                                  market_loop, push.loop, earnings_loop, bt_loop, tg.loop,
                                                  rapor_loop, font_task)]
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
const C='abdbot-v2';
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.addAll(['/ikon-192.png','/ikon-512.png'])))});
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
  if(u.pathname.startsWith('/ikon-'))e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));});
self.addEventListener('push',e=>{let d={};try{d=e.data.json()}catch(x){d={title:'ABD·BOT',body:e.data?e.data.text():''}}
  e.waitUntil(self.registration.showNotification(d.title||'ABD·BOT',{body:d.body||'',tag:d.tag||undefined,renotify:!!d.tag,
    icon:'/ikon-192.png',badge:'/ikon-192.png',data:{url:d.url||'/'}}))});
self.addEventListener('notificationclick',e=>{e.notification.close();const url=(e.notification.data&&e.notification.data.url)||'/';
  e.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(cs=>{for(const c of cs){if('focus' in c){c.focus();return}}
    return self.clients.openWindow(url)}))});
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


@app.post("/api/ayar")
async def ayar_kaydet(request: Request):
    if not authorized(request):
        return JSONResponse({"hata": "giriş gerekli"}, status_code=401)
    try:
        d = await request.json()
    except Exception:
        return JSONResponse({"hata": "geçersiz veri"}, status_code=400)
    apply_ayar(d)
    backup.data["ayar"] = {**{k: BOT_CFG[k] for k in AYAR_DEF}, **PUSH_PREF}
    backup.dirty = True
    bot.ver += 1
    asyncio.create_task(backup.save(engine, bot))
    return JSONResponse({"ok": 1, "cfg": BOT_CFG, "push": PUSH_PREF})


@app.post("/api/gecmis")
async def gecmis_baslat(request: Request):
    if not authorized(request):
        return JSONResponse({"hata": "giriş gerekli"}, status_code=401)
    try:
        d = await request.json()
    except Exception:
        d = {}
    n = int(min(25, max(3, int(d.get("gun") or 15))))
    if BT["running"]:
        return JSONResponse({"ok": 0, "durum": "zaten çalışıyor"})
    BT["bekliyor"] = n
    _bt_ver[0] += 1
    acik = cal.session(time.time()) == "regular"
    return JSONResponse({"ok": 1, "durum": "seans kapanınca başlayacak" if acik else "birkaç saniye içinde başlıyor"})


@app.post("/tg/test")
async def tg_test(request: Request):
    if not authorized(request):
        return JSONResponse({"hata": "giriş gerekli"}, status_code=401)
    if not tg.ok:
        chats = []
        try:
            chats = await tg.find_chats()
        except Exception:
            pass
        return JSONResponse({"ok": 0, "durum": tg.status, "sohbetler": chats})
    tg.send("✅ <b>ABD·BOT bağlandı.</b> Planlar, işlemler ve raporlar bu kanala grafikleriyle düşecek.")
    sym = "SPY" if store.bars.get("SPY") else next((x for x in SYMBOLS if store.bars.get(x)), None)
    if sym:
        lv = levels(sym) or {}
        tg.send(f"📈 <b>#{sym} deneme grafiği</b>\nÇizgiler: destek/direnç, formasyon, alıcı bölgesi.",
                {"sym": sym, "title": f"#{sym}  deneme grafiği", "sub": "destek/direnç, formasyon, alıcı bölgesi",
                 "lines": {}, "pat": next((p["ad"] for p in (lv.get("fx") or {}).get("pat", [])), None), "zone": True})
    return JSONResponse({"ok": 1, "durum": tg.status})


@app.get("/push/key")
async def push_key(request: Request):
    if not authorized(request):
        return JSONResponse({"hata": "giriş gerekli"}, status_code=401)
    return JSONResponse({"key": push.pub if push.ok else None, "durum": push.status})


@app.post("/push/sub")
async def push_sub(request: Request):
    if not authorized(request):
        return JSONResponse({"hata": "giriş gerekli"}, status_code=401)
    try:
        ok = push.add(await request.json())
    except Exception:
        ok = False
    if ok:
        asyncio.create_task(backup.save(engine, bot))
        push.notify("Bildirimler açık", "Bot işlem açınca, kapatınca ve plan yazınca buraya düşecek.", tag="test")
    return JSONResponse({"ok": ok, "n": len(push.subs)})


@app.post("/push/test")
async def push_test(request: Request):
    if not authorized(request):
        return JSONResponse({"hata": "giriş gerekli"}, status_code=401)
    push.notify("Deneme bildirimi", "Bunu görüyorsan bildirimler çalışıyor.", tag="test")
    return JSONResponse({"ok": push.ok, "n": len(push.subs), "durum": push.status})


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
        await cl.send({"type": "feed", "items": list(FEED)[-150:], "full": 1})
        cl.mktv = _mkt_ver[0]
        await cl.send({"type": "mkt", **MARKET})
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("daily"):
                dsym = str(msg["daily"]).upper()[:8]
                if dsym in store.bars:
                    await cl.send({"type": "daily", "sym": dsym, "bars": await get_daily(dsym)})
                continue
            if msg.get("analiz"):
                asym = str(msg["analiz"]).upper()[:8]
                if tarayici.SYM_RE.match(asym):
                    try:
                        await cl.send({"type": "analiz", **(await analiz_payload(asym))})
                    except Exception as e:
                        await cl.send({"type": "analiz", "sym": asym, "hata": str(e)[:120]})
                continue
            if msg.get("dusun"):
                dsym = str(msg["dusun"]).upper()[:8]
                await cl.send(bot_dusunce(dsym))
                continue
            if msg.get("replay"):
                rp = replay_payload(str(msg["replay"]))
                await cl.send(rp or {"type": "replay", "hata": "işlem bulunamadı"})
                continue
            sym = str(msg.get("sub", "")).upper()[:8]
            if sym and sym not in store.bars and tarayici.SYM_RE.match(sym):
                # listede olmayan hisse: dakikalık veriyi hemen çek, 30 dk izle
                ensure_sym(sym)
                VIEWED[sym] = time.time()
                try:
                    rd = await asyncio.to_thread(yahoo_fetch, [sym], "5d")
                    for s_, b_ in rd.items():
                        store.merge_yahoo(s_, b_)
                        store.snap_ver[s_] = store.snap_ver.get(s_, 0) + 1
                except Exception as e:
                    log.debug("Görüntülenen hisse verisi alınamadı %s: %s", sym, e)
            if sym in store.bars:
                if sym not in SYMBOLS:
                    VIEWED[sym] = time.time()
                cl.sym = sym
                await send_snap(cl)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("İstemci hatası: %s", e)
    finally:
        clients.discard(cl)
