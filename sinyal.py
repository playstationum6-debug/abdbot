"""
Sinyal motoru + gölge takip
---------------------------
Kurallar:
- Sadece KAPANMIŞ ve Yahoo hacmiyle TEYİTLİ 1 dk mumlarda değerlendirme yapılır (sonradan değişen sinyal yok).
- Sadece taze mumlar (son 5 dk) ve bugünün verisi değerlendirilir; eski veri asla sinyal üretmez.
- Uygulama açılırken geçmiş mumlar sinyal üretmez; sadece açılıştan sonra kapanan mumlar.
- Her sinyal gölgede takip edilir: gerçekçi giriş, stop, hedef, süre çıkışı, kayma ve masraf dahil.

Kurgular (setup):
  Normal seans : orb (açılış aralığı kırılımı), vwap (VWAP geri alma/kaybetme),
                 hacim (hacimli seviye kırılımı), trend (trend içi geri çekilme)
  Öncesi/sonrası: gap (boşluk devamı), uz_seviye (seviye kırılımı) — likidite filtresi + 2 mum teyidi
"""
import math
import time
from datetime import datetime

SETUP_AD = {
    "orb": "Açılış aralığı kırılımı",
    "vwap": "VWAP geri alma",
    "hacim": "Hacimli seviye kırılımı",
    "trend": "Trend geri çekilme",
    "gap": "Boşluk devamı",
    "uz_seviye": "Seviye kırılımı (uzatılmış)",
}
SES_AD = {"pre": "Piyasa öncesi", "regular": "Normal seans", "post": "Piyasa sonrası"}
SES_NAME = {0: "pre", 1: "regular", 2: "post", 3: "closed"}

# --- Maliyet varsayımları (temkinli) ---
SLIP = {"regular": 0.0003, "pre": 0.0010, "post": 0.0010}  # tek yön kayma (%0,03 / %0,10)
FEE = 0.00005                                              # tek yön masraf payı (SEC/FINRA ücretleri vb.)

R_MULT = 2.0             # hedef = 2R
COOLDOWN = 30 * 60       # aynı hisse + kurgu + yön için bekleme
FILL_BARS = 3            # uzatılmış seansta limit emir en fazla 3 mum bekler
FRESH_SEC = 300          # sadece son 5 dk içinde kapanan mumlar değerlendirilir
MAX_KEEP = 6000          # bellekte tutulan en fazla sinyal

# Uzatılmış seans likidite filtresi (düşük hacimli sahte hareketlere karşı)
EXT_MIN_SES_VOL = 100_000   # bu seansta toplam hacim
EXT_MIN_5BAR_VOL = 20_000   # son 5 mum toplam hacim
EXT_MIN_BAR_VOL = 5_000     # sinyal mumu hacmi
EXT_MIN_ACTIVE = 6          # son 10 mumun en az 6'sında işlem olmalı


def _ema_series(vals, n):
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def f2(x):
    return "—" if x is None else (f"{x:.2f}" if x >= 1 else f"{x:.4f}")


def kb(v):
    return f"{v / 1e6:.1f} mn" if v >= 1e6 else f"{v / 1e3:.0f} bin"


class Engine:
    def __init__(self, store, bar_info, cal, et):
        self.store = store
        self.bar_info = bar_info
        self.cal = cal
        self.ET = et
        self.signals = []          # eskiden yeniye
        self.by_id = {}
        self.last_eval = {}        # sembol -> son değerlendirilen mum
        self.last_fire = {}        # (sembol, kurgu, yön) -> zaman
        self.fired_day = set()     # (sembol, kurgu, yön, gün) — günde bir kez olan kurgular
        self.mkt = {}              # SPY durumu (piyasa yönü)
        self.ver = 0
        self.dirty_days = set()
        self.seen = {}             # sembol -> (ver, snap_ver)

    # ------------------------------------------------------------ yardımcılar
    def bounds(self, day):
        x = self.cal.days.get(day)
        if not x:
            return None
        f = lambda t: datetime.combine(day, t, tzinfo=self.ET).timestamp()
        return {"sopen": f(x["sopen"]), "open": f(x["open"]), "close": f(x["close"]), "sclose": f(x["sclose"])}

    def _end_of(self, ses, b):
        return {"pre": b["open"], "regular": b["close"], "post": b["sclose"]}[ses]

    @staticmethod
    def _clamp(entry, stop, d, atr, lo, hi):
        risk = (entry - stop) * d
        risk = min(max(risk, lo * atr, entry * 0.0008), hi * atr)
        return entry - d * risk, risk

    # ------------------------------------------------------------ bağlam
    def _ctx(self, tss, bars, k):
        T = tss[k]
        day, sc = self.bar_info(T)
        b = self.bounds(day)
        if not b or sc == 3:
            return None
        days_before = sorted({self.bar_info(t)[0] for t in tss[:k] if self.bar_info(t)[0] < day})
        pday = days_before[-1] if days_before else None

        today, prev_reg = [], []
        for t in tss[:k + 1]:
            d, s = self.bar_info(t)
            if d == day:
                today.append((t, s))
            elif d == pday and s == 1:
                prev_reg.append(t)

        o, h, l, c, v = bars[T][:5]
        pb = bars[tss[k - 1]] if k > 0 else None
        x = {"T": T, "day": day, "sc": sc, "ses": SES_NAME[sc], "b": b, "o": o, "h": h, "l": l, "c": c, "v": v,
             "pc": None, "pdh": None, "pdl": None}
        if prev_reg:
            x["pc"] = bars[prev_reg[-1]][3]
            x["pdh"] = max(bars[t][1] for t in prev_reg)
            x["pdl"] = min(bars[t][2] for t in prev_reg)

        # piyasa öncesi seviyeleri
        pre_before = [t for t, s in today if s == 0 and t < T]
        x["pmh"] = max((bars[t][1] for t in pre_before), default=None)
        x["pml"] = min((bars[t][2] for t in pre_before), default=None)
        # 2 mum teyidi için: kırılım mumundan ÖNCEKİ seviyeler
        pre_before2 = [t for t in pre_before if t < T - 60]
        x["pmh2"] = max((bars[t][1] for t in pre_before2), default=None)
        x["pml2"] = min((bars[t][2] for t in pre_before2), default=None)

        # normal seans: VWAP, açılış aralığı, gün tepe/dip
        reg = [t for t, s in today if s == 1]
        pv = vv = 0.0
        vwap_prev = None
        for t in reg:
            bo, bh, bl, bc, bv = bars[t][:5]
            if t == T:
                vwap_prev = pv / vv if vv > 0 else None
            pv += (bh + bl + bc) / 3 * bv
            vv += bv
        x["vwap"] = pv / vv if vv > 0 else None
        x["vwap_prev"] = vwap_prev
        orb = [t for t in reg if t < b["open"] + 15 * 60]
        x["or_ok"] = len(orb) >= 10 and T >= b["open"] + 15 * 60
        x["orh"] = max((bars[t][1] for t in orb), default=None)
        x["orl"] = min((bars[t][2] for t in orb), default=None)
        reg_before = [t for t in reg if t < T]
        x["hod"] = max((bars[t][1] for t in reg_before), default=None)
        x["lod"] = min((bars[t][2] for t in reg_before), default=None)
        x["rc"] = bars[reg[-1]][3] if reg and sc == 2 else None   # bugünün normal seans kapanışı (sonrası için)

        # EMA, ATR (son 80 mum, seanslar karışık olabilir)
        win = tss[max(0, k - 79):k + 1]
        closes = [bars[t][3] for t in win]
        e9, e21 = _ema_series(closes, 9), _ema_series(closes, 21)
        x["ema9"], x["ema21"] = e9[-1], e21[-1]
        x["ema9p"] = e9[-2] if len(e9) > 1 else e9[-1]
        x["ema21_10"] = e21[-11] if len(e21) > 11 else e21[0]
        trs = []
        for i in range(max(1, len(win) - 14), len(win)):
            ph = bars[win[i - 1]][3]
            bh, bl = bars[win[i]][1], bars[win[i]][2]
            trs.append(max(bh - bl, abs(bh - ph), abs(bl - ph)))
        x["atr"] = (sum(trs) / len(trs)) if trs else (h - l)
        if x["atr"] <= 0:
            x["atr"] = c * 0.001

        # hacim: aynı seansın önceki 20 mumu
        ses_today = [t for t, s in today if s == sc]
        prev20 = [bars[t][4] for t in ses_today[:-1][-20:]]
        x["avgv"] = (sum(prev20) / len(prev20)) if len(prev20) >= 5 else None
        x["volr"] = (v / x["avgv"]) if x["avgv"] else 0.0
        x["ses_vol"] = sum(bars[t][4] for t in ses_today)
        last10 = ses_today[-10:]
        x["vol5"] = sum(bars[t][4] for t in ses_today[-5:])
        x["active10"] = sum(1 for t in last10 if bars[t][4] > 0)
        x["prev_c"] = pb[3] if pb else c
        x["prev2_c"] = bars[tss[k - 2]][3] if k > 1 else x["prev_c"]
        x["prev_l"] = pb[2] if pb else l
        x["prev_h"] = pb[1] if pb else h
        x["low5"] = min(bars[t][2] for t in tss[max(0, k - 4):k + 1])
        x["high5"] = max(bars[t][1] for t in tss[max(0, k - 4):k + 1])
        x["closes4"] = [bars[t][3] for t in tss[max(0, k - 4):k]]
        return x

    # ------------------------------------------------------------ puan / gerekçe
    def _score(self, x, d, base, why, risk, sym):
        s = base
        c = x["c"]
        if x["volr"] >= 1.5:
            s += min(15, (x["volr"] - 1.5) * 5)
        if x["vwap"]:
            if (c - x["vwap"]) * d > 0:
                s += 5
                why.append(f"Fiyat VWAP'ın {'üzerinde' if d > 0 else 'altında'} ({f2(x['vwap'])})")
            else:
                s -= 8
                why.append("Fiyat VWAP'ın ters tarafında (dikkat)")
        if (x["ema9"] - x["ema21"]) * d > 0:
            s += 7
            why.append(f"Kısa trend uyumlu (EMA9 {'>' if d > 0 else '<'} EMA21)")
        else:
            s -= 7
            why.append("Kısa trend ters (EMA9/EMA21)")
        m = self.mkt.get("dir")
        if sym not in ("SPY", "QQQ") and m is not None and x["ses"] == "regular":
            if m == d:
                s += 5
                why.append("SPY aynı yönde (piyasa destekliyor)")
            else:
                s -= 5
                why.append("SPY ters yönde (piyasa desteklemiyor)")
        # yer var mı: karşıdaki en yakın seviye
        lv = [x.get(n) for n in ("pdh", "pmh", "hod", "orh", "pc")] if d > 0 else \
             [x.get(n) for n in ("pdl", "pml", "lod", "orl", "pc")]
        opp = [p for p in lv if p and (p - c) * d > c * 0.0005]
        if opp and risk > 0:
            near = min(opp, key=lambda p: abs(p - c))
            room = abs(near - c) / risk
            if room < 1:
                s -= 10
                why.append(f"Yakın {'direnç' if d > 0 else 'destek'} {f2(near)} ({room:.1f}R) — hedef zor")
            elif room >= 2:
                s += 5
                why.append(f"Önünde {room:.1f}R boşluk var ({f2(near)})")
        if x["ses"] == "regular":
            mins = (x["T"] - x["b"]["open"]) / 60
            if 120 <= mins <= 240:
                s -= 5
                why.append("Öğle saatleri: hareketler zayıf olabilir")
        return int(max(0, min(100, round(s))))

    # ------------------------------------------------------------ kurgular
    def _ready(self, sym, setup, d, T, once=False, day=None):
        if once and (sym, setup, d, day) in self.fired_day:
            return False
        last = self.last_fire.get((sym, setup, d))
        return not (last and T - last < COOLDOWN)

    def _regular(self, sym, x):
        out = []
        T, c, o, atr = x["T"], x["c"], x["o"], x["atr"]
        mins = (T - x["b"]["open"]) / 60
        rng_ok = (x["h"] - x["l"]) <= 4 * atr

        # A) Açılış aralığı kırılımı (15 dk), 09:45–11:30
        if x["or_ok"] and 15 <= mins <= 120 and x["volr"] >= 1.5 and rng_ok:
            for d, lvl, opp in ((1, x["orh"], x["orl"]), (-1, x["orl"], x["orh"])):
                if lvl and (c - lvl) * d > 0 and (x["prev_c"] - lvl) * d <= 0 and \
                        self._ready(sym, "orb", d, T, once=True, day=x["day"]):
                    stop, risk = self._clamp(c, (lvl + opp) / 2, d, atr, 0.7, 3.0)
                    why = [f"15 dk açılış aralığının {'tepesi' if d > 0 else 'dibi'} ({f2(lvl)}) kapanışla kırıldı",
                           f"Hacim ortalamanın {x['volr']:.1f} katı"]
                    out.append(("orb", d, c, stop, risk, 58, why))

        # B) VWAP geri alma / kaybetme, 10:00–15:30
        if x["vwap"] and x["vwap_prev"] and 30 <= mins <= 360 and x["volr"] >= 1.3 and rng_ok and len(x["closes4"]) >= 3:
            for d in (1, -1):
                was_other = all((pc - x["vwap_prev"]) * d < 0 for pc in x["closes4"][-3:])
                if was_other and (c - x["vwap"]) * d > 0 and (c - o) * d > 0 and (x["ema9"] - x["ema9p"]) * d > 0 \
                        and self._ready(sym, "vwap", d, T):
                    base_stop = (x["low5"] if d > 0 else x["high5"]) - d * 0.1 * atr
                    stop, risk = self._clamp(c, base_stop, d, atr, 0.7, 2.5)
                    why = [f"VWAP {'geri alındı' if d > 0 else 'kaybedildi'}: 3 mum {'altında' if d > 0 else 'üstünde'} kaldıktan sonra kapanış {'üstünde' if d > 0 else 'altında'}",
                           f"Hacim ortalamanın {x['volr']:.1f} katı"]
                    out.append(("vwap", d, c, stop, risk, 50, why))

        # C) Hacimli seviye kırılımı (önceki gün tepe/dip, öncesi tepe/dip, gün tepe/dip, önceki kapanış)
        if mins >= 5 and x["volr"] >= 3 and rng_ok:
            for d in (1, -1):
                names = (("pdh", "önceki gün tepesi"), ("pmh", "piyasa öncesi tepesi"), ("hod", "gün tepesi"), ("pc", "önceki kapanış")) \
                    if d > 0 else (("pdl", "önceki gün dibi"), ("pml", "piyasa öncesi dibi"), ("lod", "gün dibi"), ("pc", "önceki kapanış"))
                for key, ad in names:
                    lvl = x.get(key)
                    if key in ("hod", "lod") and mins < 30:
                        continue
                    if lvl and (c - lvl) * d > 0 and (x["prev_c"] - lvl) * d <= 0 and self._ready(sym, "hacim", d, T):
                        base_stop = min(x["l"], lvl) - 0.2 * atr if d > 0 else max(x["h"], lvl) + 0.2 * atr
                        stop, risk = self._clamp(c, base_stop, d, atr, 0.7, 3.0)
                        why = [f"{ad.capitalize()} ({f2(lvl)}) güçlü hacimle {'yukarı' if d > 0 else 'aşağı'} kırıldı",
                               f"Hacim patlaması: ortalamanın {x['volr']:.1f} katı"]
                        out.append(("hacim", d, c, stop, risk, 57, why))
                        break

        # D) Trend içi geri çekilme, 10:00–15:30
        if x["vwap"] and 30 <= mins <= 360 and x["volr"] >= 1.2 and rng_ok:
            for d in (1, -1):
                trend = (x["ema9"] - x["ema21"]) * d > 0 and (x["ema21"] - x["ema21_10"]) * d > 0 and (c - x["vwap"]) * d > 0
                if not trend:
                    continue
                touch = (x["prev_l"] <= x["ema21"] * 1.0005) if d > 0 else (x["prev_h"] >= x["ema21"] * 0.9995)
                if touch and (c - x["ema9"]) * d > 0 and (c - o) * d > 0 and self._ready(sym, "trend", d, T):
                    base_stop = (min(x["l"], x["prev_l"]) if d > 0 else max(x["h"], x["prev_h"])) - d * 0.1 * atr
                    stop, risk = self._clamp(c, base_stop, d, atr, 0.7, 2.5)
                    why = [f"{'Yükselen' if d > 0 else 'Düşen'} trendde EMA21'e geri çekilme sonrası dönüş mumu",
                           f"Hacim ortalamanın {x['volr']:.1f} katı"]
                    out.append(("trend", d, c, stop, risk, 50, why))
        return out

    def _extended(self, sym, x):
        out = []
        T, c, atr = x["T"], x["c"], x["atr"]
        # Likidite filtresi: düşük hacimli sahte hareketleri ele
        if x["ses_vol"] < EXT_MIN_SES_VOL or x["vol5"] < EXT_MIN_5BAR_VOL or x["v"] < EXT_MIN_BAR_VOL \
                or x["active10"] < EXT_MIN_ACTIVE or x["volr"] < 2.5 or (x["h"] - x["l"]) > 3 * atr:
            return out
        liq = f"Likidite yeterli: seans hacmi {kb(x['ses_vol'])}, son 5 dk {kb(x['vol5'])}, hacim ortalamanın {x['volr']:.1f} katı"
        ref = x["pc"] if x["ses"] == "pre" else x["rc"]
        move = (c / ref - 1) * 100 if ref else 0.0

        def confirmed(lvl, d):
            # 2 mum teyidi: önceki mum seviyeyi kırdı, bu mum da kırık tarafta kapandı
            return (x["prev_c"] - lvl) * d > 0 and (c - lvl) * d > 0 and (x["prev2_c"] - lvl) * d <= 0

        cands = []
        if x["ses"] == "pre":
            for d, lvl, ad in ((1, x["pmh2"], "piyasa öncesi tepesi"), (-1, x["pml2"], "piyasa öncesi dibi")):
                if lvl and abs(move) >= 2 and move * d > 0:
                    cands.append(("gap", d, lvl, ad, 52))
            for d, lvl, ad in ((1, x["pc"], "önceki kapanış"), (-1, x["pc"], "önceki kapanış"),
                               (1, x["pdh"], "önceki gün tepesi"), (-1, x["pdl"], "önceki gün dibi")):
                if lvl:
                    cands.append(("uz_seviye", d, lvl, ad, 45))
        else:  # post
            for d, lvl, ad in ((1, x.get("hod"), "gün tepesi"), (-1, x.get("lod"), "gün dibi"),
                               (1, x["rc"], "seans kapanışı"), (-1, x["rc"], "seans kapanışı")):
                if lvl:
                    cands.append(("uz_seviye", d, lvl, ad, 45 if abs(move) < 2 else 52))

        for setup, d, lvl, ad, base in cands:
            if not confirmed(lvl, d) or not self._ready(sym, setup, d, T):
                continue
            base_stop = lvl - d * 0.3 * atr
            stop, risk = self._clamp(c, base_stop, d, atr, 1.0, 3.0)
            risk = max(risk, c * 0.004)
            stop = c - d * risk
            why = [f"{ad.capitalize()} ({f2(lvl)}) {'yukarı' if d > 0 else 'aşağı'} kırıldı, 2 mum teyitli", liq]
            if ref and abs(move) >= 2:
                why.append(f"{'Önceki kapanışa' if x['ses'] == 'pre' else 'Seans kapanışına'} göre {move:+.1f}% hareket "
                           f"(bilanço/haber etkisi olabilir)")
            why.append("Uzatılmış seans: sadece limit emir, daha küçük lot")
            out.append((setup, d, c, stop, risk, base + min(10, abs(move) * 2), why))
            break
        return out

    # ------------------------------------------------------------ ana döngü
    def process(self, sym, complete_until, now):
        bars = self.store.bars.get(sym) or {}
        if not bars or not complete_until:
            return
        lim = int(complete_until) - 60   # bu zamana kadar başlayan mumlar kapanmıştır
        cut = lim - 6 * 86400
        tss = sorted(t for t in bars if cut <= t <= lim)
        if len(tss) < 30:
            return
        last = self.last_eval.get(sym)
        today = datetime.fromtimestamp(now, self.ET).date()
        if last is None:
            self.last_eval[sym] = tss[-1]   # geçmiş mumlardan sinyal üretme
        else:
            start = len(tss)
            for i in range(len(tss) - 1, -1, -1):
                if tss[i] <= last:
                    break
                start = i
            for k in range(start, len(tss)):
                T = tss[k]
                if bars[T][5] != "yahoo" or now - T > FRESH_SEC + 60:
                    continue
                if self.bar_info(T)[0] != today or k < 25:
                    continue
                try:
                    self._evaluate(sym, tss, bars, k, now)
                except Exception:
                    pass
            self.last_eval[sym] = tss[-1]
        for sig in self.signals[-400:]:
            if sig["sym"] == sym and sig["st"] in ("bekliyor", "açık"):
                self._update(sig, tss, bars, now)

    def _evaluate(self, sym, tss, bars, k, now):
        x = self._ctx(tss, bars, k)
        if not x:
            return
        if sym == "SPY" and x["vwap"] and x["ses"] == "regular":
            self.mkt = {"dir": 1 if x["c"] > x["vwap"] else -1, "t": x["T"]}
        cands = self._regular(sym, x) if x["ses"] == "regular" else self._extended(sym, x)
        best = {}   # aynı mum + aynı yön için tek sinyal: en yüksek güvenli kurgu
        for setup, d, entry, stop, risk, base, why in cands:
            conf = self._score(x, d, base, why, risk, sym)
            if d not in best or conf > best[d][-2]:
                best[d] = (setup, d, entry, stop, risk, conf, why)
        if len(best) == 2:   # aynı anda hem AL hem SAT çıktıysa belirsiz: hiçbirini verme
            return
        for setup, d, entry, stop, risk, conf, why in best.values():
            self._create(sym, x, setup, d, entry, stop, risk, conf, why)

    def _create(self, sym, x, setup, d, entry, stop, risk, conf, why):
        T = x["T"]
        ses = x["ses"]
        sid = f"{sym}-{T}-{setup}-{d}"
        if sid in self.by_id:
            return
        tgt = entry + d * R_MULT * risk
        sig = {
            "id": sid, "sym": sym, "dir": d, "setup": setup, "ses": ses, "t": T, "day": x["day"].isoformat(),
            "e": round(entry, 4), "s": round(stop, 4), "h": round(tgt, 4), "conf": conf, "why": why,
            "typ": "market" if ses == "regular" else "limit",
            "end": int(self._end_of(ses, x["b"])), "st": "bekliyor", "wait": 0,
            "fill": None, "ft": None, "x": None, "xt": None, "r": None, "pct": None, "last": T, "lc": x["c"],
            "created": int(time.time()),
        }
        self.signals.append(sig)
        self.by_id[sid] = sig
        self.last_fire[(sym, setup, d)] = T
        if setup == "orb":
            self.fired_day.add((sym, setup, d, x["day"]))
        if len(self.signals) > MAX_KEEP:
            for old in self.signals[:-MAX_KEEP]:
                self.by_id.pop(old["id"], None)
            self.signals = self.signals[-MAX_KEEP:]
        self.dirty_days.add(sig["day"])
        self.ver += 1

    # ------------------------------------------------------------ gölge takip
    def _close(self, sig, px, t, st):
        d = sig["dir"]
        slip = SLIP.get(sig["ses"], 0.001)
        if st in ("stop", "süre"):
            px = px * (1 - d * slip)       # piyasa emri: aleyhte kayma
        fill = sig["fill"]
        pct = d * (px - fill) / fill - 2 * FEE
        risk_pct = abs(fill - sig["s"]) / fill
        sig.update(st=st, x=round(px, 4), xt=int(t), pct=round(pct * 100, 3),
                   r=round(pct / risk_pct, 2) if risk_pct > 0 else 0.0)

    def _update(self, sig, tss, bars, now):
        d, s, h = sig["dir"], sig["s"], sig["h"]
        slip = SLIP.get(sig["ses"], 0.001)
        changed = False
        for t in tss:
            if t <= sig["last"]:
                continue
            if t >= sig["end"]:
                break
            o, hi, lo, c = bars[t][:4]
            if sig["st"] == "bekliyor":
                if sig["typ"] == "market":
                    sig.update(st="açık", fill=round(o * (1 + d * slip), 4), ft=t)
                elif (d > 0 and lo <= sig["e"]) or (d < 0 and hi >= sig["e"]):
                    sig.update(st="açık", fill=sig["e"], ft=t)
                else:
                    sig["wait"] += 1
                    if sig["wait"] >= FILL_BARS:
                        sig.update(st="dolmadı", xt=t)
                        sig["last"] = t
                        changed = True
                        break
            if sig["st"] == "açık":
                hit_s = lo <= s if d > 0 else hi >= s
                hit_h = hi >= h if d > 0 else lo <= h
                if hit_s:   # aynı mumda ikisi de olduysa temkinli: stop sayılır
                    px = o if (o - s) * d < 0 and t != sig["ft"] else s
                    self._close(sig, px, t, "stop")
                elif hit_h:
                    px = o if (o - h) * d > 0 and t != sig["ft"] else h
                    self._close(sig, px, t, "hedef")
            sig["last"] = t
            sig["lc"] = c
            changed = True
            if sig["st"] not in ("bekliyor", "açık"):
                break
        # Seans bitti: açık pozisyonu son fiyattan kapat, dolmayan emri iptal et
        if sig["st"] in ("bekliyor", "açık") and now > sig["end"] + 120:
            if sig["st"] == "açık":
                self._close(sig, sig["lc"], sig["end"], "süre")
            else:
                sig.update(st="dolmadı", xt=sig["end"])
            changed = True
        if changed:
            self.dirty_days.add(sig["day"])
            self.ver += 1

    def tick(self, now):
        """Veri gelmese bile süresi dolan sinyalleri kapat."""
        for sig in self.signals[-400:]:
            if sig["st"] in ("bekliyor", "açık") and now > sig["end"] + 120:
                self._update(sig, [], {}, now)

    # ------------------------------------------------------------ kayıt / özet
    def load(self, sigs):
        for sig in sorted(sigs, key=lambda z: z.get("t", 0)):
            if not isinstance(sig, dict) or "id" not in sig or sig["id"] in self.by_id:
                continue
            self.signals.append(sig)
            self.by_id[sig["id"]] = sig
            key = (sig["sym"], sig["setup"], sig["dir"])
            self.last_fire[key] = max(self.last_fire.get(key, 0), sig["t"])
            if sig["setup"] == "orb":
                try:
                    self.fired_day.add((sig["sym"], "orb", sig["dir"], datetime.fromisoformat(sig["day"]).date()))
                except Exception:
                    pass
        self.signals = self.signals[-MAX_KEEP:]
        self.ver += 1

    def day_list(self, day):
        return [s for s in self.signals if s["day"] == day]

    def recent(self, n=40):
        return [self.slim(s) for s in self.signals[-n:]][::-1]

    def for_symbol(self, sym, since):
        return [self.slim(s) for s in self.signals if s["sym"] == sym and s["t"] >= since]

    @staticmethod
    def slim(s):
        return {k: s[k] for k in ("id", "sym", "dir", "setup", "ses", "t", "e", "s", "h", "conf", "why", "typ",
                                  "st", "fill", "ft", "x", "xt", "r", "pct")}

    def stats(self):
        groups = {}

        def add(key, s):
            g = groups.setdefault(key, {"k": key, "n": 0, "w": 0, "tot": 0.0, "nf": 0, "open": 0})
            if s["st"] in ("hedef", "stop", "süre"):
                g["n"] += 1
                g["w"] += 1 if (s["r"] or 0) > 0 else 0
                g["tot"] += s["r"] or 0.0
            elif s["st"] == "dolmadı":
                g["nf"] += 1
            else:
                g["open"] += 1

        for s in self.signals:
            add("Tümü", s)
            add(SES_AD.get(s["ses"], s["ses"]), s)
            add(f"{SES_AD.get(s['ses'], s['ses'])} · {SETUP_AD.get(s['setup'], s['setup'])}", s)
            add("Güven ≥ 70" if s["conf"] >= 70 else "Güven < 70", s)
        order = ["Tümü"] + [SES_AD[k] for k in ("pre", "regular", "post")] + ["Güven ≥ 70", "Güven < 70"]
        rows = [groups[k] for k in order if k in groups]
        rows += sorted((g for k, g in groups.items() if k not in order), key=lambda g: g["k"])
        for g in rows:
            g["avg"] = round(g["tot"] / g["n"], 3) if g["n"] else None
            g["wr"] = round(100 * g["w"] / g["n"], 1) if g["n"] else None
            g["tot"] = round(g["tot"], 2)
        return rows

    def open_count(self):
        return sum(1 for s in self.signals[-400:] if s["st"] in ("bekliyor", "açık"))
