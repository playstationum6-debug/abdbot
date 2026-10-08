"""
Tarayıcı: haberle / yüksek hacimle koşan küçük hisseleri bulur.
Kaynaklar (Alpaca ücretsiz Market Data):
  - /v1beta1/screener/stocks/movers       → günün en çok yükselenleri
  - /v1beta1/screener/stocks/most-actives → en yüksek hacimliler
  - /v1beta1/news                         → Benzinga haber başlıkları
  - /v2/assets/{sembol}                   → işlem görebilir mi, hangi borsa (OTC elenir)
Bulunan hisseler gün boyu izlenir (Yahoo dakikalık veri) ve sinyal motoruna "koşan hisse" olarak verilir.
"""
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

DATA = "https://data.alpaca.markets"
PAPER = "https://paper-api.alpaca.markets"
SYM_RE = re.compile(r"^[A-Z]{1,5}$")
OK_EXCH = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "NYSEARCA"}


class Scanner:
    def __init__(self, key, secret, core, min_price=0.0, min_pct=10.0, max_runners=12, log=None):
        self.key, self.secret = key, secret
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
                ok = bool(a.get("tradable")) and a.get("status") == "active" and a.get("exchange") in OK_EXCH
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
                    if not SYM_RE.match(sym) or sym in self.core or pct < self.min_pct or price <= 0 or price < self.min_price:
                        continue
                    cands.append((pct, sym, price))
                cands.sort(reverse=True)
                added = 0
                for pct, sym, price in cands:
                    if sym in self.runners:
                        self.runners[sym].update(pct=pct, price=price, vol=vols.get(sym), seen=time.time())
                        continue
                    if len(self.runners) >= self.max_runners:
                        break
                    if not await self._asset_ok(c, sym):
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
                            for sym in n.get("symbols") or []:
                                info = self.runners.get(sym)
                                if info and not info.get("news"):
                                    info["news"] = {"headline": n.get("headline", ""), "url": n.get("url", ""),
                                                    "t": n.get("created_at", ""), "source": n.get("source", "")}
            self.status = f"tamam · {len(self.runners)} hisse izleniyor" + (f" (+{added} yeni)" if added else "")
            self.last_ok = time.time()
            self.ver += 1
        except Exception as e:
            self.status = f"hata: {str(e)[:100]}"
            self.ver += 1
            if self.log:
                self.log.warning("Tarayıcı hatası: %s", e)

    def rows(self):
        return sorted(self.runners.values(), key=lambda r: -(r.get("pct") or 0))
