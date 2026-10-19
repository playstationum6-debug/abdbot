"""Haberci — hisseyi yükseltebilecek haberleri birçok kaynaktan anında yakalar.

Kaynaklar (hepsi ücretsiz, anahtar gerekmez):
  - Benzinga (Alpaca)            → app.py news_loop zaten çekiyor, buraya iletir (benzinga())
  - GlobeNewswire / PR Newswire / Business Wire basın bültenleri (RSS)
  - SEC EDGAR anlık 8-K bildirimleri (Atom)
  - HABER_RSS ortam değişkeniyle istediğin kadar ek RSS (virgülle ayrılmış adres)

Her haber: hisse kodu çıkarılır → olumlu/olumsuz puanlanır → aday listesine düşer.
app.py bu adayları alır, fiyatı kontrol eder ve Telegram'a bildirim atar.
Bellek dostu: sadece başlıklar tutulur, görülen kayıtlar sınırlı kuyrukta.
"""
import logging
import os
import re
import time
from collections import deque
from html import unescape
import xml.etree.ElementTree as X

import httpx

log = logging.getLogger("haberci")

RSS = {
    "gnw": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "prn": "https://www.prnewswire.com/rss/news-releases-list.rss",
    "bw": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==",
}
for i, u in enumerate(x.strip() for x in os.getenv("HABER_RSS", "").split(",")):
    if u:
        RSS[f"ek{i + 1}"] = u
SEC_8K = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
          "&start=0&count=40&output=atom")
SEC_MAP = "https://www.sec.gov/files/company_tickers.json"

# (NASDAQ: ABCD) · (NYSE American: XYZ) · (OTCQB: ABCD) …
TICKER_RE = re.compile(r"\(\s*(?i:nasdaq|nyse|nyse american|nyse mkt|nyse arca|amex|cboe|otcqb|otcqx|otc|otc markets)"
                       r"(?:\s*(?i:cm|gs|gm|global select|global market|capital market))?\s*:\s*\"?([A-Z]{1,5})\b")
TAG_RE = re.compile(r"<[^>]+>")
TARAYICI = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/129.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9"}

# olumsuz: bunlardan biri varsa haber hiç bildirilmez
KOTU = re.compile(r"offering|priced .{0,30}(shares|units)|registered direct|private placement|at[- ]the[- ]market|"
                  r"reverse (stock )?split|dilut|warrants?\b|delist|bankrupt|chapter 11|going concern|"
                  r"complete response letter|\bcrl\b|did not meet|failed to meet|discontinu|lowers? (its )?(guidance|outlook)|"
                  r"cuts? (its )?(guidance|outlook)|miss(es|ed)? |downgrade|investigation|class action|lawsuit|"
                  r"securities fraud|shareholder alert|halted|wind down|layoffs?", re.I)

# güçlü: fiyat tepkisi beklemeden hemen bildir
GUCLU = [
    ("FDA onayı", r"\bfda\b.{0,50}\b(approv|clear|grant)|(approv\w*|clearance).{0,40}\bfda\b"),
    ("Satın alınma", r"to be acquired|agrees? to be acquired|acquired by|take[- ]private|tender offer|"
                     r"buyout|definitive (merger )?agreement .{0,40}(acquired|per share in cash)"),
    ("Olumlu faz sonucu", r"(positive|met|meets|achiev\w*|statistically significant).{0,50}(topline|primary endpoint)"),
    ("Beklenti yükseltti", r"raises? .{0,25}(guidance|outlook|forecast)|raised .{0,20}(guidance|outlook)"),
    ("Rekor gelir", r"record (revenue|quarter|sales|results)"),
    ("Beklenti üstü bilanço", r"(beats?|tops?|exceeds?|surpass\w*) .{0,30}(estimates|expectations|consensus)"),
    ("Büyük sözleşme", r"(\$\s?\d[\d.,]*\s?(million|billion|m|b|mn|bn)\b.{0,60}(contract|order|award))|"
                       r"((contract|order|award).{0,40}\$\s?\d[\d.,]*\s?(million|billion|m|b|mn|bn)\b)"),
    ("Dev ortaklık", r"(partner\w*|collaborat\w*|agreement|deal) with (nvidia|microsoft|amazon|aws|google|alphabet|apple|"
                     r"meta|tesla|openai|oracle|pfizer|merck|eli lilly|novartis|walmart|spacex|anthropic|palantir)"),
]
GUCLU = [(ad, re.compile(p, re.I)) for ad, p in GUCLU]

# orta: ancak fiyat zaten tepki veriyorsa (≥ ORTA_MIN_PCT) bildir
ORTA = [
    ("Sözleşme / sipariş", r"contract|purchase order|\border\b|award(ed)?|selected by|wins?\b|secures?\b"),
    ("Ortaklık", r"partnership|collaborat|strategic agreement|license agreement|joint venture"),
    ("FDA süreci", r"fast track|breakthrough therapy|orphan drug|rare pediatric|ind clearance|\bind\b.{0,20}clear"),
    ("Hisse geri alımı", r"repurchase|buyback"),
    ("Patent", r"patent"),
    ("Yeni ürün / genişleme", r"launch|expands?|milestone|uplist"),
    ("8-K önemli sözleşme", r"^8-K 1\.01"),
]
ORTA = [(ad, re.compile(p, re.I)) for ad, p in ORTA]
ORTA_MIN_PCT = float(os.getenv("HABER_ORTA_PCT", "5"))


def puanla(baslik, ozet=""):
    """Döner: (seviye, tür) — seviye 2 güçlü, 1 orta, 0 bildirilmez."""
    t = f"{baslik} {ozet}"
    if not baslik or KOTU.search(baslik):
        return 0, ""
    for ad, rx in GUCLU:
        if rx.search(t):
            return 2, ad
    for ad, rx in ORTA:
        if rx.search(baslik):
            return 1, ad
    return 0, ""


def anahtar(sym, baslik):
    return sym + ":" + re.sub(r"[^a-z0-9]", "", (baslik or "").lower())[:50]


class Haberci:
    def __init__(self, sec_ua=""):
        self.sec_ua = sec_ua or "abdbot paper-trading bot"
        self.goruldu = set()
        self._sira = deque(maxlen=4000)
        self.adaylar = deque(maxlen=60)      # app.py okur ve boşaltır
        self.durum = {}                      # kaynak -> {ok, t, n, hata}
        self.ilk = set()                     # ilk okumada eski haberleri bildirme
        self.cik = {}                        # SEC CIK -> hisse kodu
        self._cik_t = 0
        self.bekle = {}                      # kaynak -> bu zamana kadar deneme (403/429 sonrası)

    # ---------------------------------------------------------------- yardımcı
    def _gor(self, k):
        if k in self.goruldu:
            return False
        self.goruldu.add(k)
        self._sira.append(k)
        if len(self.goruldu) > 4500:
            self.goruldu = set(self._sira)
        return True

    def _ekle(self, kaynak, sym, baslik, url, ozet="", t=None, sessiz=False):
        sev, tur = puanla(baslik, ozet)
        k = anahtar(sym, baslik)
        if not self._gor(k) or sessiz or sev == 0:
            return
        self.adaylar.append({"sym": sym, "baslik": baslik, "url": url, "seviye": sev, "tur": tur,
                             "kaynak": kaynak, "t": t or time.time(), "k": k})

    def _not(self, kaynak, ok, n=0, hata=""):
        self.durum[kaynak] = {"ok": ok, "t": int(time.time()), "n": n, "hata": hata[:120]}

    # ---------------------------------------------------------------- Benzinga
    def benzinga(self, items, epoch):
        """app.py news_loop'tan gelen Alpaca/Benzinga haberleri."""
        sessiz = "bz" not in self.ilk
        self.ilk.add("bz")
        n = 0
        for it in items or []:
            syms = [s for s in (it.get("symbols") or []) if re.fullmatch(r"[A-Z]{1,5}", s)]
            if not syms or len(syms) > 2:          # liste / piyasa özeti haberleri
                continue
            t = epoch(it.get("created_at", ""))
            eski = time.time() - t > 15 * 60       # 15 dk'dan eski haber "anında" değildir
            for s in syms:
                self._ekle("bz", s, it.get("headline", ""), it.get("url", ""), it.get("summary", "") or "",
                           t, sessiz or eski)
                n += 1
        self._not("bz", True, n)

    # ---------------------------------------------------------------- RSS
    async def rss(self, c, kaynak, url):
        sessiz = kaynak not in self.ilk
        if time.time() < self.bekle.get(kaynak, 0):
            return                                  # engelleyen kaynağı bir süre rahat bırak
        try:
            r = await c.get(url, headers=TARAYICI)
            if r.status_code != 200:
                self._not(kaynak, False, hata=f"HTTP {r.status_code}")
                if r.status_code in (403, 429):
                    self.bekle[kaynak] = time.time() + 30 * 60
                return
            kok = X.fromstring(r.content)
            n = 0
            for it in kok.iter():
                if not it.tag.endswith("item") and not it.tag.endswith("entry"):
                    continue
                baslik = desc = link = ""
                for ch in it:
                    tag = ch.tag.rsplit("}", 1)[-1]
                    if tag == "title":
                        baslik = unescape(TAG_RE.sub("", ch.text or "")).strip()
                    elif tag in ("description", "summary", "content"):
                        desc = unescape(TAG_RE.sub(" ", ch.text or ""))[:1500]
                    elif tag == "link":
                        link = (ch.text or ch.get("href") or "").strip()
                if not baslik:
                    continue
                n += 1
                syms = list(dict.fromkeys(TICKER_RE.findall(f"{baslik} {desc}")))[:2]
                for s in syms:
                    self._ekle(kaynak, s, baslik, link, desc[:400], sessiz=sessiz)
            self.ilk.add(kaynak)
            self._not(kaynak, True, n)
        except Exception as e:
            self._not(kaynak, False, hata=str(e))

    # ---------------------------------------------------------------- SEC 8-K
    async def _cik_yukle(self, c):
        if self.cik and time.time() - self._cik_t < 86400:
            return
        r = await c.get(SEC_MAP, headers={"User-Agent": self.sec_ua})
        if r.status_code == 200:
            self.cik = {int(v["cik_str"]): v["ticker"] for v in r.json().values()
                        if re.fullmatch(r"[A-Z]{1,5}", v.get("ticker", ""))}
            self._cik_t = time.time()

    async def sec(self, c):
        sessiz = "sec" not in self.ilk
        try:
            await self._cik_yukle(c)
            r = await c.get(SEC_8K, headers={"User-Agent": self.sec_ua})
            if r.status_code != 200:
                self._not("sec", False, hata=f"HTTP {r.status_code}")
                return
            ns = {"a": "http://www.w3.org/2005/Atom"}
            n = 0
            for e in X.fromstring(r.content).findall("a:entry", ns):
                title = e.findtext("a:title", "", ns)
                m = re.search(r"\((\d{6,10})\)", title)
                sym = self.cik.get(int(m.group(1))) if m else None
                if not sym:
                    continue
                n += 1
                ozet = TAG_RE.sub(" ", unescape(e.findtext("a:summary", "", ns)))
                if "1.01" not in ozet:                # sadece "önemli sözleşme" maddesi ilgimizi çekiyor
                    continue
                link = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "")
                ad = re.sub(r"^8-K - |\s*\(\d+\).*$", "", title).strip()
                self._ekle("sec", sym, f"8-K 1.01 · {ad}: önemli sözleşme bildirimi", link, sessiz=sessiz)
            self.ilk.add("sec")
            self._not("sec", True, n)
        except Exception as e:
            self._not("sec", False, hata=str(e))

    # ---------------------------------------------------------------- tur
    async def tara(self, c, sec_de=True):
        for k, u in RSS.items():
            await self.rss(c, k, u)
        if sec_de:
            await self.sec(c)

    def al(self):
        """Biriken adayları ver ve listeyi boşalt."""
        out = list(self.adaylar)
        self.adaylar.clear()
        return out
