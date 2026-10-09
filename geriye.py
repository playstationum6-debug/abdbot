"""
Geçmiş test: botun BUGÜNKÜ kurallarını geçmiş günlerin dakikalık verisinde çalıştırır.
------------------------------------------------------------------------------------
- Veri: Alpaca geçmiş dakikalık mumlar (feed=sip: bütün borsalar, tam hacim; ücretsiz planda 15 dk gecikmeyle serbest).
- Sinyaller canlıdakiyle aynı motorla (sinyal.Engine) üretilir ve aynı gölge takiple (kayma + masraf dahil) sonuçlandırılır.
- Sonuçlar öğrenme modülüne "geçmiş test" olarak eklenir: bot haftalarca beklemek yerine binlerce örnekle başlar.
Sınırlar: koşan küçük hisseler (o günlerin tarayıcı listesi yok) ve haber/SEC filtreleri test edilmez.
Render'ın küçük işlemcisinde yavaştır: piyasa kapalıyken çalıştırılır.
"""
import gc
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
            if len(self._b) > 60000:
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
            "xt": s.get("xt"), "bt": 1, "rp": round(abs(s["e"] - s["s"]) / s["e"], 5) if s.get("e") else None,
            **({"plan_tur": s["plan_tur"]} if s.get("plan_tur") else {}),
            **({"lab_r": s["lab_r"]} if s.get("lab_r") else {})}


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


def run(key, secret, symbols, n_days, et, progress=None, should_stop=None, done=None, onceki=None, on_symbol=None):
    """Ana fonksiyon (ayrı iş parçacığında çalıştırılır). Dönüş: (sinyaller, özet)
    done/onceki: yarıda kalan testin bitmiş hisseleri ve sinyalleri (kaldığı yerden devam).
    on_symbol(sym, sinyaller): her hisse bitince çağrılır (ara kayıt için)."""
    done = set(done or [])
    hdr = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    now = datetime.now(UTC)
    end = now - timedelta(minutes=20)                  # ücretsiz planda son 15 dk SIP verisi yok
    out = list(onceki or [])
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
            if sym in done:
                continue
            try:
                bars = spy if sym == "SPY" else fetch(c, hdr, sym, start, end)
            except Exception:
                bars = {}
            if len(bars) < 200:
                if on_symbol:
                    try:
                        on_symbol(sym, [])
                    except Exception:
                        pass
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
            yeni = [slim(s) for s in eng.signals if s.get("r") is not None]
            out.extend(yeni)
            del eng, store, bars, by_day, tss_all
            gc.collect()
            if on_symbol:
                try:
                    on_symbol(sym, yeni)
                except Exception:
                    pass
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


# ----------------------------------------------------------------- koşan hisse testi (küçük hisseler)
# Ana test koşan hisseleri ölçemiyordu (o günlerin tarayıcı listesi yok). Burada liste yeniden kurulur:
# bütün ABD hisselerinin günlük mumlarından "gün içinde önceki kapanışa göre +%25 üstüne çıkan, hacimli" günler
# bulunur; o günün dakikalık mumlarında hisse +%10'u geçtiği dakikadan itibaren "koşan" sayılır (canlı tarayıcı gibi)
# ve koşan kurguları (kosu, geri, onay, gapgo, sicrama …) aynı motorla çalıştırılır.
# Sınırlar: sadece normal seans günlük mumuyla aday seçilir (yalnız öncesi seansta koşup sönenler kaçar);
# veri bütün borsalardan (SIP) gelir, canlıda öncesi/sonrası seans hacmi sadece IEX: hacim kapıları testte daha gevşek;
# hızlı kırılım (5 sn'lik canlı fiyat) ve haber bilgisi test edilmez.
import re as _re

import plan as _plan

_SYM_OK = _re.compile(r"^[A-Z]{1,5}$")


def _get(c, url, hdr, params):
    for _ in range(6):
        r = c.get(url, headers=hdr, params=params)
        if r.status_code == 429:
            time.sleep(3)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Alpaca istek sınırı")


def kosan_adaylari(c, hdr, trade_days, et, min_pct=25.0, min_vol=500_000, max_n=120, ekle=None):
    """[(sym, gün, önceki kapanış, gün içi en yüksek %)] — en yeni günler önce."""
    js = _get(c, "https://paper-api.alpaca.markets/v2/assets", hdr, {"status": "active", "asset_class": "us_equity"})
    syms = [a["symbol"] for a in js if a.get("tradable") and _SYM_OK.match(a.get("symbol") or "")
            and a.get("exchange") in ("NASDAQ", "NYSE", "AMEX", "ARCA", "BATS")]
    start = datetime.combine(trade_days[0], dtime(0), tzinfo=et).astimezone(UTC)
    end = datetime.now(UTC) - timedelta(minutes=20)
    gunler = set(trade_days[1:])
    out = []
    for i in range(0, len(syms), 200):
        params = {"symbols": ",".join(syms[i:i + 200]), "timeframe": "1Day", "feed": "sip", "limit": 10000,
                  "adjustment": "raw", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
        rows = {}
        for _ in range(20):
            js = _get(c, "https://data.alpaca.markets/v2/stocks/bars", hdr, params)
            for s_, bs in (js.get("bars") or {}).items():
                rows.setdefault(s_, []).extend(bs)
            tok = js.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
        for s_, bs in rows.items():
            bs.sort(key=lambda b: b["t"])
            for a, b in zip(bs, bs[1:]):
                pc, h, v = float(a["c"]), float(b["h"]), float(b.get("v") or 0)
                d = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(et).date()
                if d in gunler and pc >= 0.5 and v >= min_vol and h / pc - 1 >= min_pct / 100:
                    out.append((s_, d, pc, round((h / pc - 1) * 100, 1)))
        time.sleep(0.3)
    for s_, d in (ekle or []):                      # canlıda koşan listesine girmiş / kanalda geçmiş olanlar
        if d in gunler and not any(x[0] == s_ and x[1] == d for x in out):
            out.append((s_, d, None, None))
    out.sort(key=lambda x: (x[1], x[3] or 0), reverse=True)
    return out[:max_n], len(syms)


def kosan_test(key, secret, n_days, et, progress=None, should_stop=None, ekle=None, max_n=120):
    """Dönüş: (sinyaller, özet). Ayrı iş parçacığında çalıştırılır."""
    hdr = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    now = datetime.now(UTC)
    t0 = time.time()
    out = []
    with httpx.Client(timeout=40) as c:
        days = calendar(c, hdr, (now - timedelta(days=int(n_days * 1.6) + 10)).date(), now.date())
        trade_days = sorted(d for d in days if d <= datetime.now(et).date())[-(n_days + 1):]
        if len(trade_days) < 2:
            return [], {"hata": "takvim alınamadı"}
        cal = BtCal({d: days[d] for d in trade_days}, et)
        if progress:
            progress(0, 0, 0, 0, "aday hisseler bulunuyor (bütün borsa, günlük mumlar)…")
        adaylar, n_tum = kosan_adaylari(c, hdr, trade_days, et, ekle=ekle, max_n=max_n)
        for n, (sym, d, pc, hp) in enumerate(adaylar):
            if should_stop and should_stop():
                break
            i = trade_days.index(d)
            p = trade_days[i - 1]
            start = datetime.combine(p, dtime(4), tzinfo=et).astimezone(UTC)
            end = min(datetime.combine(d, dtime(20), tzinfo=et).astimezone(UTC), now - timedelta(minutes=20))
            try:
                bars = fetch(c, hdr, sym, start, end)
            except Exception:
                continue
            tss = sorted(bars)
            day_tss = [t for t in tss if cal.bar_info(t)[0] == d]
            prev_c = None
            for t in tss:
                if cal.bar_info(t)[0] == p and cal.bar_info(t)[1] == 1:
                    prev_c = bars[t][3]
            prev_c = prev_c or pc
            if len(day_tss) < 30 or not prev_c:
                continue
            store = _Store()
            store.bars[sym] = bars
            eng = sinyal.Engine(store, cal.bar_info, cal, et)
            eng.learner = None
            eng.bt_mode = True
            eng.runners = {}
            eng.plan = _plan.Planci(bt=True)          # kırılım planı: hisse koşmadan önce de çalışır
            eng.sadece_plan = {sym}
            base = len(tss) - len(day_tss)
            for k in range(max(base, 25), len(tss)):
                T = tss[k]
                if cal.bar_info(T)[1] == 3:
                    continue
                if sym not in eng.runners and bars[T][3] >= prev_c * 1.10:
                    eng.runners[sym] = {"sym": sym, "pct": round((bars[T][3] / prev_c - 1) * 100, 1), "news": None,
                                        "src": "test", "first": T, "price": bars[T][3]}
                    eng.sadece_plan.discard(sym)
                if cal.bar_info(T)[0] != d:
                    continue
                try:
                    eng._evaluate(sym, tss, bars, k, T + 60)
                except Exception:
                    pass
            fin = day_tss[-1] + 4 * 3600
            for sig in eng.signals:
                eng._update(sig, day_tss, bars, fin)
            for s_ in eng.signals:
                if s_.get("r") is not None:
                    x = slim(s_)
                    x["id"] = "kt-" + s_["id"]
                    x["runner"] = 1
                    out.append(x)
            del eng, store, bars
            gc.collect()
            if progress:
                progress(n + 1, len(adaylar), len(out), time.time() - t0, sym)
            time.sleep(0.2)
    summ = summary(out, trade_days[1:], time.time() - t0)
    summ["aday"] = len(adaylar)
    summ["taranan"] = n_tum
    return out, summ
