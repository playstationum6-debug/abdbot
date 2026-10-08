"""
Tarayıcı: haberle / yüksek hacimle koşan küçük hisseleri bulur.
Kaynaklar (Alpaca ücretsiz Market Data):
  - /v1beta1/screener/stocks/movers       → günün en çok yükselenleri
  - /v1beta1/screener/stocks/most-actives → en yüksek hacimliler
  - /v1beta1/news                         → Benzinga haber başlıkları
  - /v2/assets/{sembol}                   → işlem görebilir mi, hangi borsa (OTC elenir)
Bulunan hisseler gün boyu izlenir (Yahoo dakikalık veri + 5 sn'de bir Alpaca anlık fiyat) ve
sinyal motoruna "koşan hisse" olarak verilir.
Haber yorumu: başlık okunur → "iyi" (FDA onayı, sözleşme, satın alma…), "kötü" (hisse ihracı, ters bölünme,
seyreltme…) ya da "nötr". Kötü haberli hisselere alış yapılmaz (genelde tuzak).
Liste doluysa en zayıf (pozisyonu olmayan) hisse, çok daha güçlü yeni bir koşanla değiştirilir.
Yapay zeka haber puanı (GEMINI_API_KEY varsa, ücretsiz katman): her yeni başlık −100…+100 puanlanır.
ŞİMDİLİK SADECE ÖLÇÜLÜR: al-sat kararını değiştirmez, öğrenme modülü puanın işe yarayıp yaramadığını ölçer.
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
BAD_NAME = ("warrant", "unit", "right", "wt ", " wts", "units")


# Haber başlığı sınıflandırma (Benzinga başlıkları İngilizce). Önce "kötü" kontrol edilir.
NEWS_BAD = ("offering", "priced", "pricing of", "registered direct", "private placement", "reverse split",
            "reverse stock split", "at-the-market", "at the market", "atm program", "dilut", "warrant",
            "securities purchase agreement", "delist", "deficiency", "non-compliance", "noncompliance",
            "bankruptcy", "chapter 11", "going concern", "shelf registration", "resale", "s-1 ", "f-1 ")
NEWS_GOOD = ("fda approv", "approval", "approved", "clearance", "cleared", "breakthrough", "contract", "awarded",
             "partnership", "partners with", "agreement", "acquisition", "acquire", "merger", "to be acquired",
             "buyout", "record revenue", "record quarter", "beats", "raises guidance", "raised guidance", "upgrade",
             "positive", "topline", "patent", "collaboration", "license", "purchase order", "wins", "secures")


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


DROP_SEC = 20 * 60   # elenen hisse bu süre sonra (hâlâ yükselenler listesindeyse) tekrar izlenir

AI_PROMPT = """You are a US small-cap day trading news analyst. A stock is up {pct:.0f}% today.
Latest headline: "{headline}"
Score how likely this news supports a CONTINUED intraday move UP for {sym} over the next 30-60 minutes,
from -100 (very bearish: offering, dilution, reverse split, bad trial data) to +100 (very bullish: FDA approval,
large contract, acquisition at premium). Old, vague or promotional news should score near 0.
Answer ONLY JSON: {{"score": <int>, "reason": "<max 12 words, in Turkish>"}}"""


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

    async def scan(self, et):
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
                gainers = mv.json().get("gainers") or []
                vols = {}
                try:
                    ma = await c.get(f"{DATA}/v1beta1/screener/stocks/most-actives",
                                     params={"by": "volume", "top": 100}, headers=self._h())
                    if ma.status_code == 200:
                        vols = {x["symbol"]: x.get("volume") for x in ma.json().get("most_actives") or []}
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
