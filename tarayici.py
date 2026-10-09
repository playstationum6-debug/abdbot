"""
Tarayıcı: haberle / yüksek hacimle koşan küçük hisseleri bulur.
Kaynaklar (Alpaca ücretsiz Market Data):
  - /v1beta1/screener/stocks/movers       → günün en çok yükselenleri
  - /v1beta1/screener/stocks/most-actives → en yüksek hacimliler
  - /v1beta1/news                         → Benzinga haber başlıkları
  - /v2/assets/{sembol}                   → işlem görebilir mi, hangi borsa (OTC elenir)
Öncesi / sonrası seans (04:00–09:30 ve 16:00–20:00 ET): Alpaca'nın yükselenler listesi bu saatlerde
dünün seansını gösterir ve ücretsiz IEX verisinde küçük hisselerin işlemleri çoğu zaman görünmez.
Bu yüzden bütün ABD piyasasının seans dışı değişimi TradingView tarayıcısından (ücretsiz, anahtarsız) alınır.
Bulunan hisseler gün boyu izlenir (Yahoo dakikalık veri + 5 sn'de bir Alpaca anlık fiyat) ve
sinyal motoruna "koşan hisse" olarak verilir.
Haber yorumu: başlık okunur → "iyi" (FDA onayı, sözleşme, satın alma…), "kötü" (hisse ihracı, ters bölünme,
seyreltme…) ya da "nötr". Kötü haberli hisselere alış yapılmaz (genelde tuzak).
Liste doluysa en zayıf (pozisyonu olmayan) hisse, çok daha güçlü yeni bir koşanla değiştirilir.
Yapay zeka haber puanı (GEMINI_API_KEY varsa, ücretsiz katman): her yeni başlık −100…+100 puanlanır.
ŞİMDİLİK SADECE ÖLÇÜLÜR: al-sat kararını değiştirmez, öğrenme modülü puanın işe yarayıp yaramadığını ölçer.
SEC EDGAR (ücretsiz, resmi): koşan her hissenin son SEC bildirimleri okunur.
  - Son 3 günde hisse ihracı / izahname (424B, S-1, F-1, 8-K madde 3.02) → "seyreltme": ALIŞ YAPILMAZ
  - Son 30 günde raf kaydı (S-3, F-3, EFFECT) → "dikkat": şirket her an hisse satabilir, güven −5
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

DATA = "https://data.alpaca.markets"
PAPER = "https://paper-api.alpaca.markets"
SYM_RE = re.compile(r"^[A-Z]{1,5}$")
OK_EXCH = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "NYSEARCA"}
TV_URL = "https://scanner.tradingview.com/america/scan"
TV_MIN_VOL = 20000          # seans dışı en az bu kadar adet işlem görmüş olsun (hayalet fiyatları ele)
TV_LIST_VOL = 2000          # Keşfet listesinde gösterim sınırı
BAD_NAME = ("warrant", "unit", "right", "wt ", " wts", "units")


# Haber başlığı sınıflandırma (Benzinga başlıkları İngilizce). Önce "kötü" kontrol edilir.
NEWS_BAD = ("offering", "priced", "pricing of", "registered direct", "private placement", "reverse split",
            "reverse stock split", "at-the-market", "at the market", "atm program", "dilut", "warrant",
            "securities purchase agreement", "delist", "deficiency", "non-compliance", "noncompliance",
            "bankruptcy", "chapter 11", "going concern", "shelf registration", "resale", "s-1 ", "f-1 ")
NEWS_GOOD = ("fda approv", "approval", "approved", "clearance", "cleared", "breakthrough", "contract", "awarded",
             "partnership", "partners with", "agreement", "acquisition", "acquire", "merger", "to be acquired",
             "buyout", "record revenue", "record quarter", "beats", "raises guidance", "raised guidance", "upgrade",
             "positive", "topline", "patent", "collaboration", "license", "purchase order", "wins", "secures",
             "fast track", "orphan drug", "granted", "clinical trial application", "ind clearance", "receives",
             "health canada", "launches", "strategic", "record", "expands", "milestone")


def haber_turu(headline):
    """Başlıktan (tür, eşleşen kelime): tür = 'kötü' | 'iyi' | 'nötr'."""
    h = (headline or "").lower()
    for w in NEWS_BAD:
        if w in h:
            return "kötü", w.strip()
    for w in NEWS_GOOD:
        if w in h:
            return "iyi", w.strip()
    return "nötr", ""


SEC_UA = "abdbot paper-trading bot"   # app.py SEC_UA ortam değişkeniyle değiştirir (SEC iletişim bilgisi ister)
SEC_DILUTE = ("424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "S-1", "S-1/A", "F-1", "F-1/A")
SEC_SHELF = ("S-3", "S-3/A", "F-3", "F-3/A", "EFFECT", "S-3ASR")
SEC_TTL = 600        # aynı hisse için bildirimler 10 dk'da bir yeniden okunur

DROP_SEC = 20 * 60   # elenen hisse bu süre sonra (hâlâ yükselenler listesindeyse) tekrar izlenir
NEWS_MIN_PCT = 8.0   # olumlu haber gelen hisse bu kadar yükselmişse hemen izlemeye alınır
NEWS_WATCH = 15 * 60 # haber geldikten sonra fiyat tepkisi bu süre boyunca beklenir

AI_PROMPT = """You are a US small-cap day trading news analyst. A stock is up {pct:.0f}% today.
Latest headline: "{headline}"
Score how likely this news supports a CONTINUED intraday move UP for {sym} over the next 30-60 minutes,
from -100 (very bearish: offering, dilution, reverse split, bad trial data) to +100 (very bullish: FDA approval,
large contract, acquisition at premium). Old, vague or promotional news should score near 0.
Answer ONLY JSON: {{"score": <int>, "reason": "<max 12 words, in Turkish>"}}"""


def _ts(s):
    """Alpaca zaman damgası (nanosaniyeli olabilir) → epoch saniye; okunamazsa 0."""
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0


def is_common(sym, name=""):
    """Varant (…W), birim (…U), hak (…R) gibi gerçek hisse olmayanları ele."""
    if len(sym) == 5 and sym[-1] in "WUR":
        return False
    n = (name or "").lower()
    return not any(b in n for b in BAD_NAME)


class Scanner:
    def __init__(self, key, secret, core, min_price=0.0, min_pct=10.0, max_runners=12, log=None,
                 ai_key="", ai_model="gemini-2.5-flash"):
        self.key, self.secret = key, secret
        self.ai_key, self.ai_model = ai_key, ai_model
        self.ai_cache = {}       # başlık -> {"skor", "neden"}
        self.ai_calls = []       # son çağrı zamanları (ücretsiz kota: dakikada en fazla 8)
        self.ai_status = "kapalı (GEMINI_API_KEY yok)" if not ai_key else "hazır"
        self.sec_ua = SEC_UA
        self.sec_cik = {}        # sembol -> CIK (günde bir yüklenir)
        self.sec_cik_t = 0.0
        self.sec_status = "başlamadı"
        self.core = set(core)
        self.min_price = min_price
        self.min_pct = min_pct
        self.max_runners = max_runners
        self.log = log
        self.runners = {}        # sembol -> bilgi
        self.day = None
        self.status = "başlamadı" if key else "kapalı (Alpaca anahtarı yok)"
        self.last_ok = 0.0
        self.ver = 0
        self._assets = {}
        self.dropped = {}        # sembol -> (zaman, sebep): elenen hisse 20 dk sonra tekrar aday olabilir
        self.keep = lambda sym: False   # açık pozisyonu olan hisse listeden çıkarılmaz (app.py ayarlar)
        self.gainers, self.losers, self.actives = [], [], []
        self.news_pending = {}   # sembol -> {"item", "until"}: olumlu haber geldi, fiyat tepkisi bekleniyor
        self.news_seen = set()
        self.spread = {}             # sembol -> (spread %, zaman): Alpaca IEX en iyi alış/satış
        self.recent_news_syms = {}   # sembol -> zaman (seans dışı taramada aday havuzu)
        self.ext_status = "—"
        self.tv_status = "—"
        self.gain_src = "alpaca"     # Keşfet yükselenler listesinin kaynağı: alpaca | pre | post
        self._tv_fail = 0
        self._tv_next = 0.0

    def _h(self):
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    async def _asset_ok(self, c, sym):
        if sym in self._assets:
            return self._assets[sym]
        ok = False
        try:
            r = await c.get(f"{PAPER}/v2/assets/{sym}", headers=self._h())
            if r.status_code == 200:
                a = r.json()
                ok = bool(a.get("tradable")) and a.get("status") == "active" and a.get("exchange") in OK_EXCH \
                    and is_common(sym, a.get("name", ""))
        except Exception:
            ok = False
        self._assets[sym] = ok
        return ok

    async def snapshots(self, c, syms):
        """Alpaca anlık özet (IEX, ücretsiz): {sembol: snapshot}. Seans dışı işlemleri de içerir."""
        out = {}
        syms = [x for x in dict.fromkeys(syms) if SYM_RE.match(x)]
        for i in range(0, len(syms), 50):
            try:
                r = await c.get(f"{DATA}/v2/stocks/snapshots", headers=self._h(),
                                params={"symbols": ",".join(syms[i:i + 50]), "feed": "iex"})
                if r.status_code == 200:
                    js = r.json()
                    part = js.get("snapshots", js) if isinstance(js, dict) else {}
                    out.update(part)
                    now = time.time()
                    for k, sn in (part or {}).items():
                        q = (sn or {}).get("latestQuote") or {}
                        ap, bp = float(q.get("ap") or 0), float(q.get("bp") or 0)
                        if ap > 0 and bp > 0 and ap >= bp:
                            self.spread[k] = (round((ap - bp) / ((ap + bp) / 2) * 100, 3), now)
            except Exception:
                continue
        return out

    def spread_of(self, sym, max_age=300):
        x = self.spread.get(sym)
        return x[0] if x and time.time() - x[1] < max_age else None

    @staticmethod
    def _move(sn, ses):
        """(önceki kapanışa göre %, bu seanstaki %, son fiyat, son işlem zamanı)"""
        if not isinstance(sn, dict):
            return None
        lt = sn.get("latestTrade") or {}
        px = float(lt.get("p") or 0)
        if px <= 0:
            return None
        prev = float((sn.get("prevDailyBar") or {}).get("c") or 0)
        day = float((sn.get("dailyBar") or {}).get("c") or 0)
        p_prev = (px / prev - 1) * 100 if prev else None
        p_ses = (px / day - 1) * 100 if ses == "post" and day else None   # sonrası seans: bugünkü kapanışa göre
        t = _ts(lt.get("t"))
        return p_prev, p_ses, px, t

    async def _add(self, c, sym, pct, price, src, news=None):
        """Aday hisseyi listeye ekle (liste doluysa en zayıfın yerine). True: eklendi."""
        if sym in self.runners or sym in self.core or not SYM_RE.match(sym) or not is_common(sym):
            if sym in self.runners:
                self.runners[sym].update(pct=pct, price=price, seen=time.time())
            return False
        if price <= 0 or price < self.min_price:
            return False
        dr = self.dropped.get(sym)
        if dr and time.time() - dr[0] < DROP_SEC:
            return False
        if len(self.runners) >= self.max_runners:
            weak = [r for r in self.runners.values() if not self.keep(r["sym"])]
            if not weak:
                return False
            w = min(weak, key=lambda r: r.get("pct_now") if r.get("pct_now") is not None else (r.get("pct") or 0))
            wp = w.get("pct_now") if w.get("pct_now") is not None else (w.get("pct") or 0)
            if pct < wp + (0 if news else 10):        # haberli yeni hisse eşit güçteyse bile öncelikli
                return False
            if not await self._asset_ok(c, sym):
                return False
            del self.runners[w["sym"]]
        elif not await self._asset_ok(c, sym):
            return False
        info = {"sym": sym, "pct": round(pct, 2), "price": price, "vol": None, "first": time.time(),
                "seen": time.time(), "news": None, "src": src}
        if news:
            info.update(news=news, news_kind=news.get("kind", "nötr"), news_word=news.get("word", ""))
        self.runners[sym] = info
        self.ver += 1
        if self.log:
            self.log.info("Tarayıcı: %s eklendi (%s, %%%.0f)", sym, src, pct)
        return True

    async def haber_tara(self, items, ses):
        """Olumlu haber düşen (izlenmeyen) hisseyi fiyat tepki verdiği an listeye al.
        items: Alpaca haber kayıtları. Dönüş: eklenen semboller."""
        if not self.key or ses == "closed":
            return []
        now = time.time()
        for n in items or []:
            nid = n.get("id")
            if nid in self.news_seen:
                continue
            self.news_seen.add(nid)
            nt = _ts(n.get("created_at")) or now
            if now - nt > NEWS_WATCH:
                continue
            kind, word = haber_turu(n.get("headline", ""))
            if kind == "kötü":
                continue
            item = {"headline": n.get("headline", ""), "url": n.get("url", ""), "t": n.get("created_at", ""),
                    "source": n.get("source", ""), "kind": kind, "word": word}
            for sym in (n.get("symbols") or [])[:4]:
                if SYM_RE.match(sym) and sym not in self.core:
                    self.recent_news_syms[sym] = now
                    if sym not in self.runners:
                        self.news_pending[sym] = {"item": item, "until": nt + NEWS_WATCH}
        if len(self.news_seen) > 3000:
            self.news_seen = set(list(self.news_seen)[-1500:])
        self.news_pending = {k: v for k, v in self.news_pending.items() if v["until"] > now and k not in self.runners}
        if not self.news_pending:
            return []
        added = []
        async with httpx.AsyncClient(timeout=15) as c:
            snaps = await self.snapshots(c, list(self.news_pending))
            for sym, pend in list(self.news_pending.items()):
                mv = self._move(snaps.get(sym), ses)
                if not mv:
                    continue
                p_prev, p_ses, px, t = mv
                pct = max(x for x in (p_prev, p_ses, -999) if x is not None)
                if pct >= NEWS_MIN_PCT and (not t or now - t < 600):
                    if await self._add(c, sym, p_prev if p_prev is not None else pct, px, "haber", pend["item"]):
                        added.append(sym)
                        self.news_pending.pop(sym, None)
        return added

    async def tv_movers(self, c, ses, desc=True, n=60):
        """TradingView tarayıcısı: bütün ABD piyasasında öncesi/sonrası seansın en çok yükselen (desc) ya da
        düşen hisseleri. Dönüş Alpaca movers biçiminde: [{symbol, percent_change, price, volume}]"""
        k = "premarket" if ses == "pre" else "postmarket"
        body = {
            "markets": ["america"],
            "symbols": {"query": {"types": []}, "tickers": []},
            "options": {"lang": "en"},
            "columns": ["name", f"{k}_change", f"{k}_close", f"{k}_volume", "exchange", "type", "close"],
            "filter": [
                {"left": "type", "operation": "in_range", "right": ["stock", "dr"]},
                {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},
                {"left": f"{k}_close", "operation": "greater", "right": 0},
                {"left": f"{k}_volume", "operation": "greater", "right": TV_LIST_VOL},
                {"left": f"{k}_change", "operation": "greater" if desc else "less", "right": 0},
            ],
            "sort": {"sortBy": f"{k}_change", "sortOrder": "desc" if desc else "asc"},
            "range": [0, n],
        }
        r = await c.post(TV_URL, json=body, headers={
            "User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/", "Content-Type": "application/json"})
        r.raise_for_status()
        out = []
        for row in (r.json() or {}).get("data") or []:
            d = row.get("d") or []
            if len(d) < 4:
                continue
            sym = str(d[0] or (row.get("s") or ":").split(":")[-1]).upper()
            if not SYM_RE.match(sym) or not is_common(sym):
                continue
            try:
                out.append({"symbol": sym, "percent_change": round(float(d[1] or 0), 2),
                            "price": float(d[2] or 0), "volume": int(d[3] or 0), "src": ses})
            except (TypeError, ValueError):
                continue
        return out

    async def tv_scan(self, c, ses):
        """Seans dışı: Keşfet listelerini TradingView verisiyle doldur, %10+ ve yeterli hacimli olanları izlemeye al."""
        now = time.time()
        if now < self._tv_next:
            return 0
        try:
            up = await self.tv_movers(c, ses, True)
            try:
                dn = await self.tv_movers(c, ses, False, 30)
            except Exception:
                dn = []
            self._tv_fail = 0
        except Exception as e:
            self._tv_fail += 1
            self._tv_next = now + min(600, 60 * self._tv_fail)     # hata: giderek seyrek dene
            self.tv_status = f"hata: {str(e)[:60]}"
            return 0
        self.gainers, self.gain_src = up[:30], ses
        if dn:
            self.losers = dn[:30]
        n_add = 0
        for g in up:
            pct, px, vol = g["percent_change"], g["price"], g["volume"]
            if pct < self.min_pct:
                break
            if vol < TV_MIN_VOL or g["symbol"] in self.core:
                continue
            if g["symbol"] in self.runners:
                self.runners[g["symbol"]].update(pct=pct, price=px, vol=vol, seen=now)
                continue
            if await self._add(c, g["symbol"], pct, px, "öncesi seans" if ses == "pre" else "sonrası seans"):
                self.runners[g["symbol"]]["vol"] = vol
                n_add += 1
        self.tv_status = f"{len(up)} yükselen, {n_add} eklendi ({datetime.now().strftime('%H:%M')})"
        return n_add

    async def ext_scan(self, c, ses):
        """Öncesi/sonrası seans: en yüksek hacimliler + yükselenler + son haberli hisseler canlı fiyatla taranır."""
        now = time.time()
        self.recent_news_syms = {k: v for k, v in self.recent_news_syms.items() if now - v < 4 * 3600}
        pool = [x.get("symbol") for x in self.actives] + [g.get("symbol") for g in self.gainers] + list(self.recent_news_syms)
        pool = [x for x in dict.fromkeys(pool) if x and x not in self.core and x not in self.runners][:150]
        snaps = await self.snapshots(c, pool)
        n_add = 0
        for sym, sn in snaps.items():
            mv = self._move(sn, ses)
            if not mv:
                continue
            p_prev, p_ses, px, t = mv
            if t and now - t > 900:
                continue
            ses_move = p_ses if p_ses is not None else p_prev
            if (p_prev is not None and p_prev >= self.min_pct) or (ses_move is not None and ses_move >= self.min_pct * 0.8):
                if await self._add(c, sym, p_prev if p_prev is not None else ses_move, px, "seans dışı"):
                    n_add += 1
        self.ext_status = f"{len(snaps)} hisse tarandı, {n_add} eklendi ({datetime.now().strftime('%H:%M')})"

    async def scan(self, et, ses=None):
        if not self.key:
            return
        today = datetime.now(et).date().isoformat()
        if today != self.day:            # yeni gün: listeyi sıfırla
            self.day = today
            self.runners = {}
            self.dropped = {}
            self.ver += 1
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                mv = await c.get(f"{DATA}/v1beta1/screener/stocks/movers", params={"top": 50}, headers=self._h())
                mv.raise_for_status()
                mj = mv.json()
                gainers = mj.get("gainers") or []
                # Keşfet sayfası: bütün piyasanın en çok yükselen / düşen / en yüksek hacimli hisseleri
                if ses not in ("pre", "post") or self.gain_src == "alpaca":
                    self.gainers = [g for g in gainers if SYM_RE.match(g.get("symbol", "")) and is_common(g.get("symbol", ""))][:30]
                    self.losers = [g for g in (mj.get("losers") or []) if SYM_RE.match(g.get("symbol", "")) and is_common(g.get("symbol", ""))][:30]
                if ses not in ("pre", "post"):
                    self.gain_src = "alpaca"
                vols = {}
                try:
                    ma = await c.get(f"{DATA}/v1beta1/screener/stocks/most-actives",
                                     params={"by": "volume", "top": 100}, headers=self._h())
                    if ma.status_code == 200:
                        acts = ma.json().get("most_actives") or []
                        vols = {x["symbol"]: x.get("volume") for x in acts}
                        self.actives = [x for x in acts if SYM_RE.match(x.get("symbol", "")) and is_common(x.get("symbol", ""))][:30]
                except Exception:
                    pass
                cands = []
                for g in gainers:
                    sym = g.get("symbol", "")
                    pct = float(g.get("percent_change") or 0)
                    price = float(g.get("price") or 0)
                    dr = self.dropped.get(sym)
                    if dr and time.time() - dr[0] < DROP_SEC:
                        continue
                    if not SYM_RE.match(sym) or not is_common(sym) or sym in self.core \
                            or pct < self.min_pct or price <= 0 or price < self.min_price:
                        continue
                    cands.append((pct, sym, price))
                cands.sort(reverse=True)
                added = 0
                for pct, sym, price in cands:
                    if sym in self.runners:
                        self.runners[sym].update(pct=pct, price=price, vol=vols.get(sym), seen=time.time())
                        continue
                    if len(self.runners) >= self.max_runners:
                        # Liste dolu: en zayıf hisseyi (pozisyonu yoksa) çok daha güçlü yenisiyle değiştir
                        weak = [r for r in self.runners.values() if not self.keep(r["sym"])]
                        if not weak:
                            continue
                        w = min(weak, key=lambda r: r.get("pct_now") if r.get("pct_now") is not None else (r.get("pct") or 0))
                        wp = w.get("pct_now") if w.get("pct_now") is not None else (w.get("pct") or 0)
                        if pct < wp + 10:
                            continue
                        if not await self._asset_ok(c, sym):
                            continue
                        del self.runners[w["sym"]]
                        if self.log:
                            self.log.info("Tarayıcı: %s (%%%.0f) yerine %s (%%%.0f) izleniyor", w["sym"], wp, sym, pct)
                    elif not await self._asset_ok(c, sym):
                        continue
                    self.runners[sym] = {"sym": sym, "pct": pct, "price": price, "vol": vols.get(sym),
                                         "first": time.time(), "seen": time.time(), "news": None}
                    added += 1
                # öncesi/sonrası seans: bütün piyasa (TradingView) + canlı fiyatla ek tarama
                if ses in ("pre", "post"):
                    await self.tv_scan(c, ses)
                    try:
                        await self.ext_scan(c, ses)
                    except Exception as e:
                        self.ext_status = f"hata: {str(e)[:60]}"
                # haberler (son 24 saat)
                if self.runners:
                    start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    nr = await c.get(f"{DATA}/v1beta1/news", headers=self._h(), params={
                        "symbols": ",".join(self.runners), "start": start, "limit": 50, "sort": "desc"})
                    if nr.status_code == 200:
                        for n in nr.json().get("news") or []:
                            kind, word = haber_turu(n.get("headline", ""))
                            for sym in n.get("symbols") or []:
                                info = self.runners.get(sym)
                                if not info:
                                    continue
                                item = {"headline": n.get("headline", ""), "url": n.get("url", ""),
                                        "t": n.get("created_at", ""), "source": n.get("source", ""),
                                        "kind": kind, "word": word}
                                if not info.get("news"):
                                    info["news"] = item           # en yeni başlık
                                # Son 24 saatte tek bir seyreltme/ihraç haberi bile hisseyi "kötü" yapar
                                if kind == "kötü" or (kind == "iyi" and info.get("news_kind") != "kötü"):
                                    if info.get("news_kind") != kind:
                                        info["news_kind"], info["news_word"] = kind, word
                                        if kind == "kötü":
                                            info["news"] = item
                                info.setdefault("news_kind", "nötr")
                # SEC EDGAR: seyreltme / hisse ihracı bildirimi var mı?
                await self._sec_check(c)
                # Yapay zeka haber puanı (sadece ölçüm)
                if self.ai_key:
                    for info in list(self.runners.values()):
                        n = info.get("news")
                        if not n or info.get("ai_h") == n.get("headline"):
                            continue
                        res = await self._ai_score(c, info["sym"], n.get("headline", ""), info.get("pct") or 0)
                        if res is None:
                            break            # kota doldu ya da hata: sonraki taramada devam
                        info["ai"], info["ai_h"] = res, n.get("headline")
            self.status = f"tamam · {len(self.runners)} hisse izleniyor" + (f" (+{added} yeni)" if added else "")
            self.last_ok = time.time()
            self.ver += 1
        except Exception as e:
            self.status = f"hata: {str(e)[:100]}"
            self.ver += 1
            if self.log:
                self.log.warning("Tarayıcı hatası: %s", e)

    async def _sec_check(self, c):
        if not self.runners:
            return
        h = {"User-Agent": self.sec_ua, "Accept-Encoding": "gzip, deflate"}
        now = time.time()
        try:
            if not self.sec_cik or now - self.sec_cik_t > 86400:
                r = await c.get("https://www.sec.gov/files/company_tickers.json", headers=h)
                if r.status_code != 200:
                    self.sec_status = f"hata {r.status_code} (sembol listesi)"
                    return
                self.sec_cik = {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}
                self.sec_cik_t = now
            n = 0
            for info in list(self.runners.values()):
                if now - (info.get("sec_t") or 0) < SEC_TTL:
                    continue
                cik = self.sec_cik.get(info["sym"])
                info["sec_t"] = now
                if not cik:
                    info["sec"] = {"kind": "bilinmiyor", "msg": "SEC kaydı bulunamadı (yabancı/yeni şirket olabilir)"}
                    continue
                r = await c.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers=h)
                n += 1
                if r.status_code != 200:
                    continue
                info["sec"] = self._sec_eval(r.json().get("filings", {}).get("recent", {}))
                if n >= 8:          # SEC sınırı saniyede 10 istek; tarama başına en fazla 8
                    break
            self.sec_status = f"çalışıyor · {len(self.sec_cik)} şirket"
        except Exception as e:
            self.sec_status = f"hata: {str(e)[:80]}"

    @staticmethod
    def _sec_eval(rec):
        """Son bildirimlerden seyreltme riski: {"kind": seyreltme|dikkat|temiz, "msg", "form", "date"}."""
        forms = rec.get("form") or []
        dates = rec.get("filingDate") or []
        items = rec.get("items") or [""] * len(forms)
        today = datetime.now(timezone.utc).date()
        shelf = None
        for i, f in enumerate(forms[:60]):
            try:
                age = (today - datetime.strptime(dates[i], "%Y-%m-%d").date()).days
            except Exception:
                continue
            if age > 30:
                break
            it = items[i] if i < len(items) else ""
            if age <= 3 and (f in SEC_DILUTE or (f in ("8-K", "6-K") and "3.02" in (it or ""))):
                why = "hisse ihracı / izahname" if f in SEC_DILUTE else "kayıtsız hisse satışı (8-K 3.02)"
                return {"kind": "seyreltme", "form": f, "date": dates[i],
                        "msg": f"SEC: {dates[i]} tarihli {f} — {why}. Alış yapılmaz."}
            if f in SEC_SHELF and shelf is None:
                shelf = {"kind": "dikkat", "form": f, "date": dates[i],
                         "msg": f"SEC: {dates[i]} tarihli {f} raf kaydı — şirket her an hisse satabilir"}
        return shelf or {"kind": "temiz", "msg": "SEC: son 30 günde seyreltme bildirimi yok"}

    async def model_bul(self, c=None):
        """Model kaldırılmış / adı değişmişse (404): hesabın kullanabildiği en yeni 'flash' modelini otomatik seç."""
        if time.time() - getattr(self, "_model_t", 0) < 600:
            return
        self._model_t = time.time()
        own = c is None
        if own:
            c = httpx.AsyncClient(timeout=20)
        try:
            r = await c.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": self.ai_key},
                            params={"pageSize": 200})
            ms = [m for m in (r.json().get("models") or [])
                  if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            names = [m["name"].split("/", 1)[-1] for m in ms]
            iyi = [n for n in names if "flash" in n and "lite" not in n and "image" not in n and "tts" not in n
                   and "exp" not in n and "preview" not in n] or [n for n in names if "flash" in n] or names
            if iyi:
                def surum(n):
                    import re as _re
                    m = _re.search(r"(\d+(?:\.\d+)?)", n)
                    return (float(m.group(1)) if m else 0, "latest" in n, -len(n))
                yeni = max(iyi, key=surum)
                if yeni != self.ai_model:
                    if self.log:
                        self.log.info("Yapay zeka modeli değişti: %s → %s", self.ai_model, yeni)
                    self.ai_model = yeni
                    self.ai_status = f"model otomatik seçildi: {yeni}"
            else:
                self.ai_status = "kullanılabilir model bulunamadı (anahtarı kontrol et)"
        except Exception as e:
            self.ai_status = f"model listesi alınamadı: {str(e)[:60]}"
        finally:
            if own:
                await c.aclose()

    async def _ai_score(self, c, sym, headline, pct):
        """Gemini ile başlığı puanla: {"skor": -100..100, "neden": str} ya da None (kota/hata)."""
        if headline in self.ai_cache:
            return self.ai_cache[headline]
        now = time.time()
        self.ai_calls = [t for t in self.ai_calls if now - t < 60]
        if len(self.ai_calls) >= 8:
            return None
        self.ai_calls.append(now)
        try:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{self.ai_model}:generateContent",
                headers={"x-goog-api-key": self.ai_key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": AI_PROMPT.format(sym=sym, headline=headline[:300], pct=pct)}]}],
                      "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
            if r.status_code == 404:
                await self.model_bul(c)
                return None
            if r.status_code != 200:
                self.ai_status = f"hata {r.status_code}: {r.text[:80]}"
                return None
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            js = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            res = {"skor": int(max(-100, min(100, int(js.get("score", 0))))), "neden": str(js.get("reason", ""))[:100]}
            self.ai_cache[headline] = res
            self.ai_status = f"çalışıyor · {len(self.ai_cache)} başlık puanlandı"
            return res
        except Exception as e:
            self.ai_status = f"hata: {str(e)[:80]}"
            return None

    def prune(self, pct_now, keep):
        """Alpaca listesini kendi verimizle doğrula.
        pct_now(sym) -> bugünkü % değişim (bugünün verisi yoksa None); keep(sym) -> açık pozisyon var mı."""
        now = time.time()
        changed = False
        for sym in list(self.runners):
            info = self.runners[sym]
            p = pct_now(sym)
            # Yahoo verisi yoksa / gecikmeliyse Alpaca anlık fiyatından hesaplanan yüzdeyi kullan
            if p is None and info.get("pct_live") is not None and now - info.get("live_t", 0) < 300:
                p = info["pct_live"]
            if p is not None:
                info["pct_now"] = round(p, 2)
                info["peak"] = max(info.get("peak", p), p)
            if keep(sym):
                continue
            # Alpaca'nın yükselenler listesi son 2 dk içinde hâlâ güçlü gösteriyorsa kendi verimize göre eleme
            alp_strong = now - info.get("seen", 0) < 120 and (info.get("pct") or 0) >= self.min_pct
            why = None
            if p is None and now - info["first"] > 900 and not alp_strong:
                why = "fiyat verisi alınamadı"
            elif p is not None and p < self.min_pct / 2 and info.get("peak", p) < self.min_pct and not alp_strong:
                why = f"bugünkü değişim %{p:.1f} (Alpaca listesi eski olabilir)"
            elif p is not None and p < 0:
                why = f"yükselişini kaybetti (%{p:.1f})"
            if why:
                del self.runners[sym]
                self.dropped[sym] = (now, why)
                changed = True
                if self.log:
                    self.log.info("Tarayıcı: %s elendi — %s", sym, why)
        if changed:
            self.ver += 1
            self.status = f"tamam · {len(self.runners)} hisse izleniyor"

    def dropped_rows(self):
        now = time.time()
        return [{"sym": k, "t": int(t), "why": w, "back": int(max(0, DROP_SEC - (now - t)))}
                for k, (t, w) in sorted(self.dropped.items(), key=lambda kv: -kv[1][0])][:15]

    def rows(self):
        return sorted(self.runners.values(), key=lambda r: -(r.get("pct_now") if r.get("pct_now") is not None else (r.get("pct") or 0)))
