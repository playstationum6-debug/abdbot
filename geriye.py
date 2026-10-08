"""
Geçmiş test: botun BUGÜNKÜ kurallarını geçmiş günlerin dakikalık verisinde çalıştırır.
------------------------------------------------------------------------------------
- Veri: Alpaca geçmiş dakikalık mumlar (feed=sip: bütün borsalar, tam hacim; ücretsiz planda 15 dk gecikmeyle serbest).
- Sinyaller canlıdakiyle aynı motorla (sinyal.Engine) üretilir ve aynı gölge takiple (kayma + masraf dahil) sonuçlandırılır.
- Sonuçlar öğrenme modülüne "geçmiş test" olarak eklenir: bot haftalarca beklemek yerine binlerce örnekle başlar.
Sınırlar: koşan küçük hisseler (o günlerin tarayıcı listesi yok) ve haber/SEC filtreleri test edilmez.
Render'ın küçük işlemcisinde yavaştır: piyasa kapalıyken çalıştırılır.
"""
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

import httpx

import sinyal

UTC = timezone.utc
SES_CODE = {"pre": 0, "regular": 1, "post": 2, "closed": 3}


class BtCal:
    """Geçmiş günler için seans takvimi (Alpaca takviminden)."""

    def __init__(self, days, et):
        self.days = days
        self.ET = et
        self._b = {}

    def session(self, ts):
        dt = datetime.fromtimestamp(ts, self.ET)
        x = self.days.get(dt.date())
        if not x:
            return "closed"
        t = dt.time()
        if x["sopen"] <= t < x["open"]:
            return "pre"
        if x["open"] <= t < x["close"]:
            return "regular"
        if x["close"] <= t < x["sclose"]:
            return "post"
        return "closed"

    def bar_info(self, ts):
        r = self._b.get(ts)
        if r is None:
            r = (datetime.fromtimestamp(ts, self.ET).date(), SES_CODE[self.session(ts)])
            if len(self._b) > 400000:
                self._b.clear()
            self._b[ts] = r
        return r


def _t(s, d):
    try:
        s = str(s).replace(":", "")
        return dtime(int(s[:2]), int(s[2:4]))
    except Exception:
        return d


def calendar(c, hdr, start, end):
    r = c.get("https://paper-api.alpaca.markets/v2/calendar", headers=hdr,
              params={"start": start.isoformat(), "end": end.isoformat()})
    r.raise_for_status()
    out = {}
    for d in r.json():
        out[date.fromisoformat(d["date"])] = {"open": _t(d.get("open"), dtime(9, 30)), "close": _t(d.get("close"), dtime(16)),
                                              "sopen": _t(d.get("session_open"), dtime(4)),
                                              "sclose": _t(d.get("session_close"), dtime(20))}
    return out


def fetch(c, hdr, sym, start, end):
    """Bir hissenin dakikalık mumları: {ts: [o,h,l,c,v,'yahoo']}"""
    params = {"symbols": sym, "timeframe": "1Min", "feed": "sip", "limit": 10000, "adjustment": "raw",
              "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    bars = {}
    for _ in range(30):
        r = c.get("https://data.alpaca.markets/v2/stocks/bars", headers=hdr, params=params)
        if r.status_code == 429:
            time.sleep(3)
            continue
        r.raise_for_status()
        js = r.json()
        for b in (js.get("bars") or {}).get(sym, []) or []:
            try:
                ts = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()) // 60 * 60
            except Exception:
                continue
            bars[ts] = [float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v") or 0), "yahoo"]
        tok = js.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    return bars


class _Store:
    def __init__(self):
        self.bars = {}
        self.live = {}


def slim(s):
    """Öğrenme ve raporlar için hafif kayıt."""
    return {"id": "bt-" + s["id"], "sym": s["sym"], "dir": s["dir"], "setup": s["setup"], "ses": s["ses"], "t": s["t"],
            "day": s["day"], "conf": s["conf"], "st": s["st"], "r": s.get("r"), "f": s.get("f") or {},
            "xt": s.get("xt"), "bt": 1}


def spy_regime(bars, cal):
    """SPY'ın her dakika VWAP'a göre yönü (piyasa yönü özelliği için): {ts: +1/-1}"""
    out = {}
    pv = vv = 0.0
    day = None
    for t in sorted(bars):
        d, sc = cal.bar_info(t)
        if sc != 1:
            continue
        if d != day:
            day, pv, vv = d, 0.0, 0.0
        o, h, l, c, v = bars[t][:5]
        pv += (h + l + c) / 3 * v
        vv += v
        if vv > 0:
            out[t] = 1 if c > pv / vv else -1
    return out


def run(key, secret, symbols, n_days, et, progress=None, should_stop=None):
    """Ana fonksiyon (ayrı iş parçacığında çalıştırılır). Dönüş: (sinyaller, özet)"""
    hdr = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    now = datetime.now(UTC)
    end = now - timedelta(minutes=20)                  # ücretsiz planda son 15 dk SIP verisi yok
    out = []
    t0 = time.time()
    with httpx.Client(timeout=30) as c:
        days = calendar(c, hdr, (now - timedelta(days=int(n_days * 1.6) + 10)).date(), now.date())
        trade_days = sorted(d for d in days if d < datetime.now(et).date())[-(n_days + 1):]
        if len(trade_days) < 2:
            return [], {"hata": "takvim alınamadı"}
        cal = BtCal({d: days[d] for d in trade_days}, et)
        start = datetime.combine(trade_days[0], dtime(4), tzinfo=et).astimezone(UTC)
        test_days = trade_days[1:]                     # ilk gün sadece "önceki gün" bilgisi için
        spy = fetch(c, hdr, "SPY", start, end)
        regime = spy_regime(spy, cal)
        rkeys = sorted(regime)
        syms = ["SPY"] + [s for s in symbols if s != "SPY"]
        for n, sym in enumerate(syms):
            if should_stop and should_stop():
                break
            try:
                bars = spy if sym == "SPY" else fetch(c, hdr, sym, start, end)
            except Exception:
                continue
            if len(bars) < 200:
                continue
            store = _Store()
            store.bars[sym] = bars
            eng = sinyal.Engine(store, cal.bar_info, cal, et)
            eng.learner = None
            tss_all = sorted(bars)
            by_day = {}
            for t in tss_all:
                by_day.setdefault(cal.bar_info(t)[0], []).append(t)
            ri = 0
            for di, d in enumerate(trade_days):
                if d not in test_days or d not in by_day:
                    continue
                prev = by_day.get(trade_days[di - 1], []) if di > 0 else []
                tss = prev + by_day[d]
                base = len(prev)
                before = len(eng.signals)
                for k in range(max(base, 25), len(tss)):
                    T = tss[k]
                    if cal.bar_info(T)[1] == 3:
                        continue
                    # piyasa yönü (SPY VWAP) — o dakikaya kadar bilinen son değer
                    while ri + 1 < len(rkeys) and rkeys[ri + 1] <= T:
                        ri += 1
                    if rkeys and rkeys[ri] <= T and T - rkeys[ri] < 600:
                        eng.mkt = {"dir": regime[rkeys[ri]], "t": rkeys[ri]}
                    try:
                        eng._evaluate(sym, tss, bars, k, T + 60)
                    except Exception:
                        pass
                day_tss = by_day[d]
                fin = day_tss[-1] + 4 * 3600
                for sig in eng.signals[before:]:
                    eng._update(sig, day_tss, bars, fin)
                if len(eng._fx) > 400:
                    eng._fx.clear()
            out.extend(slim(s) for s in eng.signals if s.get("r") is not None)
            if progress:
                progress(n + 1, len(syms), len(out), time.time() - t0)
    return out, summary(out, test_days, time.time() - t0)


def summary(sigs, days, secs):
    def agg(rows):
        if not rows:
            return {"n": 0}
        rs = [r["r"] for r in rows]
        return {"n": len(rs), "avg": round(sum(rs) / len(rs), 3), "wr": round(100 * sum(1 for x in rs if x > 0) / len(rs)),
                "tot": round(sum(rs), 1)}
    by = lambda key: sorted(({"k": k, **agg([s for s in sigs if key(s) == k])} for k in {key(s) for s in sigs}),
                            key=lambda x: -x["n"])
    return {"gun": len(days), "ilk": str(days[0]) if days else None, "son": str(days[-1]) if days else None,
            "sure": round(secs), "tum": agg(sigs),
            "kurgu": by(lambda s: s["setup"]),
            "hacim": by(lambda s: s["f"].get("volr", "?")),
            "seans": by(lambda s: s["ses"]),
            "yon": by(lambda s: "AL" if s["dir"] > 0 else "SAT")}
