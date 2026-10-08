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

import formasyon

SETUP_AD = {
    "orb": "Açılış aralığı kırılımı",
    "vwap": "VWAP geri alma",
    "hacim": "Hacimli seviye kırılımı",
    "trend": "Trend geri çekilme",
    "gap": "Boşluk devamı",
    "uz_seviye": "Seviye kırılımı (uzatılmış)",
    "uz_kirilim": "Seans aralığı kırılımı (uzatılmış)",
    "uz_vwap": "Seans VWAP geri alma (uzatılmış)",
    "kosu": "Haberli momentum kırılımı",
    "geri": "Momentum ilk geri çekilme",
    "itki": "Hacimli itki (1 dk scalp)",
    "vwap_sek": "VWAP sekmesi (1 dk scalp)",
    "hizli": "Haberli hızlı kırılım (anlık)",
    "formasyon": "Formasyon kırılımı",
    "talep": "Alıcı bölgesinden dönüş",
    "fvg": "FVG dönüşü",
    "kanal": "Kanal alt bandından dönüş",
    "yapi": "Yapı kırılımı (BOS)",
    "fib": "Fibonacci geri çekilme dönüşü",
}
SES_AD = {"pre": "Piyasa öncesi", "regular": "Normal seans", "post": "Piyasa sonrası"}
SES_NAME = {0: "pre", 1: "regular", 2: "post", 3: "closed"}

# --- Maliyet varsayımları (temkinli) ---
SLIP = {"regular": 0.0003, "pre": 0.0010, "post": 0.0010}  # tek yön kayma (%0,03 / %0,10)
FEE = 0.00005                                              # tek yön masraf payı (SEC/FINRA ücretleri vb.)

R_MULT = 2.0             # hedef = 2R
R_RUNNER = 3.0           # küçük/haberli koşan hisselerde hedef = 3R

# Haberle koşan küçük hisseler: kayma çok daha yüksek varsayılır
SLIP_RUNNER = {"regular": 0.005, "pre": 0.008, "post": 0.008}
RUN_MIN_MOVE = 10.0          # önceki kapanışa göre en az %10 yükselmiş olmalı
RUN_MIN_SES_DV = 1_000_000   # bu seansta en az 1 mn $ işlem hacmi
RUN_MIN_BAR_DV = 50_000      # sinyal mumunda en az 50 bin $ işlem
IEX_SHARE = 0.05             # öncesi/sonrası seansta hacim sadece IEX: $ hacim eşikleri bu oranla küçültülür
COOLDOWN = 10 * 60       # aynı hisse + kurgu + yön için bekleme (sık işlem: 10 dk)
R_SETUP = {"itki": 1.5, "vwap_sek": 1.5}   # 1 dk scalp kurgularında hedef = 1,5R
MIN_STOP_PCT = 0.0025    # stop girişten en az fiyatın %0,25'i uzakta (1 dk gürültüsünde patlamasın)
MIN_STOP_ATR = 2.2       # ve en az 1 dk ATR × 2,2 (≈ 5 dk mum oynaklığı)
FILL_BARS = 3            # uzatılmış seansta limit emir en fazla 3 mum bekler
FRESH_SEC = 300          # sadece son 5 dk içinde kapanan mumlar değerlendirilir
MAX_KEEP = 6000          # bellekte tutulan en fazla sinyal

# Uzatılmış seans likidite filtresi (düşük hacimli sahte hareketlere karşı)
# Öncesi/sonrası seansta hacim Alpaca IEX'ten gelir (Yahoo bu saatlerde 0 verir).
# IEX tüm piyasa hacminin yaklaşık %2-5'i olduğu için mutlak eşikler IEX ölçeğindedir.
EXT_MIN_SES_VOL = 3_000     # bu seansta toplam IEX hacmi
EXT_MIN_5BAR_VOL = 600      # son 5 mum toplam IEX hacmi
EXT_MIN_BAR_VOL = 150       # sinyal mumu IEX hacmi
EXT_MIN_ACTIVE = 5          # son 10 mumun en az 5'inde işlem olmalı
EXT_MIN_VOLR = 2.0          # sinyal mumu hacmi seans ortalamasının en az 2 katı


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


def usd(v):
    return f"{v / 1e6:.1f} mn $" if v >= 1e6 else f"{v / 1e3:.0f} bin $"


def slip_of(sig):
    return (SLIP_RUNNER if sig.get("runner") else SLIP).get(sig["ses"], 0.001)


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
        self._fast_px = {}         # koşan hisse -> son anlık fiyat (kırılımın "şimdi" olduğunu anlamak için)
        self._plan_posted = {}     # koşan hisse -> son yayınlanan plan seviyesi
        self._fx = {}              # (sembol, 5 dk dilimi) -> grafik analizi (formasyon, seviye, alıcı bölgesi)
        self._tgt_over = {}        # formasyon hedefi: (sembol, mum, kurgu, yön) -> hedef fiyat
        self.runners = {}          # tarayıcının bulduğu koşan hisseler: sembol -> bilgi (yüzde, haber)
        self.learner = None        # öğrenme modülü (güven ayarı + not)

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
        x["ema20"], x["ema50"] = _ema_series(closes, 20)[-1], _ema_series(closes, 50)[-1]
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
        x["ses_dv"] = sum(bars[t][4] * bars[t][3] for t in ses_today)
        x["sh"] = max((bars[t][1] for t in ses_today if t < T), default=None)
        x["sh2"] = max((bars[t][1] for t in ses_today if t < T - 60), default=None)
        x["sl2"] = min((bars[t][2] for t in ses_today if t < T - 60), default=None)
        # Uzatılmış seansın kendi VWAP'ı (öncesi / sonrası ayrı)
        spv = svv = 0.0
        x["svwap_prev"] = None
        for t in ses_today:
            bo, bh, bl, bc, bv = bars[t][:5]
            if t == T:
                x["svwap_prev"] = spv / svv if svv > 0 else None
            spv += (bh + bl + bc) / 3 * bv
            svv += bv
        x["svwap"] = spv / svv if svv > 0 else None
        start = {0: b["sopen"], 1: b["open"], 2: b["close"]}.get(sc, b["open"])
        x["ses_mins"] = (T - start) / 60
        x["recent"] = [tuple(bars[t][:5]) for t in ses_today[-12:]]
        x["mins"] = (T - b["open"]) / 60
        # OBV eğimi (son 10 mum): hacim alıcı mı satıcı mı tarafta? (-1 … +1)
        seg = ses_today[-11:]
        num = den = 0.0
        for i in range(1, len(seg)):
            pc_, (bc_, bv_) = bars[seg[i - 1]][3], (bars[seg[i]][3], bars[seg[i]][4])
            num += bv_ if bc_ > pc_ else (-bv_ if bc_ < pc_ else 0)
            den += bv_
        x["obv"] = num / den if den > 0 else 0.0
        prev5 = x["recent"][-6:-1]
        x["ph5"] = max((r[1] for r in prev5), default=None)
        x["pl5"] = min((r[2] for r in prev5), default=None)
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
        ob = x.get("obv", 0.0) * d
        if ob >= 0.25:
            s += 5
            why.append(f"OBV {'alıcı' if d > 0 else 'satıcı'} tarafta (son 10 mum hacim akışı %{abs(x['obv']) * 100:.0f})")
        elif ob <= -0.25:
            s -= 6
            why.append("OBV ters: hacim akışı karşı yönde (dikkat)")
        m = self.mkt.get("dir")
        if sym not in ("SPY", "QQQ") and sym not in self.runners and m is not None and x["ses"] == "regular":
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
            x["_room"] = room
            if room < 1:
                s -= 10
                why.append(f"Yakın {'direnç' if d > 0 else 'destek'} {f2(near)} ({room:.1f}R) — hedef zor")
            elif room >= 2:
                s += 5
                why.append(f"Önünde {room:.1f}R boşluk var ({f2(near)})")
        s += self._grafik_puan(x, d, why, risk)
        if x["ses"] == "regular":
            mins = (x["T"] - x["b"]["open"]) / 60
            if 120 <= mins <= 240:
                s -= 5
                why.append("Öğle saatleri: hareketler zayıf olabilir")
        return int(max(0, min(100, round(s))))

    def _grafik_puan(self, x, d, why, risk):
        """Grafik okuması: piyasa yapısı, kanal, FVG, Fibonacci, mum gövde/fitil, EMA 20/50."""
        s = 0
        fx = x.get("_fx") or {}
        c = x["c"]
        a5 = fx.get("atr") or x["atr"] * 2.2
        y = fx.get("yapi") or {}
        tr = y.get("trend")
        if tr in ("yükseliş", "düşüş"):
            if (tr == "yükseliş") == (d > 0):
                s += 4
                why.append(f"Piyasa yapısı lehte ({'yükselen tepe ve dipler' if d > 0 else 'alçalan tepe ve dipler'})")
            else:
                s -= 5
                why.append(f"Piyasa yapısı ters ({tr}): dikkat")
        kr = y.get("kirilim")
        if kr and kr["yon"] == d and x["T"] - kr["t"] <= 1800:
            s += 3
            why.append(f"Yapı kırılımı ({kr['tip']}) {f2(kr['p'])} {'yukarı' if d > 0 else 'aşağı'}")
        kn = fx.get("kanal")
        if kn and not kn.get("kirildi"):
            w = kn["ust_now"] - kn["alt_now"]
            pos = (c - kn["alt_now"]) / w if w > 0 else 0.5
            if ((d > 0 and pos <= 0.25) or (d < 0 and pos >= 0.75)) and not any("kanal" in w.lower() for w in why):
                s += 4
                why.append(f"{kn['ad']}: fiyat {'alt' if d > 0 else 'üst'} bantta (dönüş bölgesi)")
            elif (d > 0 and pos >= 0.85) or (d < 0 and pos <= 0.15):
                s -= 5
                why.append(f"{kn['ad']}: fiyat {'üst' if d > 0 else 'alt'} bantta, yer dar")
        for z in fx.get("fvg") or []:
            if z["bull"] == (d > 0) and z["lo"] <= (x["l"] if d > 0 else x["h"]) <= z["hi"] and (c - z["hi"] if d > 0 else z["lo"] - c) >= 0:
                if any("FVG" in w for w in why):
                    break
                s += 3
                why.append(f"{'Boğa' if d > 0 else 'Ayı'} FVG boşluğundan ({f2(z['lo'])}–{f2(z['hi'])}) tepki")
                break
            if z["bull"] != (d > 0) and risk > 0 and 0 < (z["lo"] - c if d > 0 else c - z["hi"]) <= risk:
                s -= 4
                why.append(f"Hemen {'üstünde' if d > 0 else 'altında'} dolmamış FVG ({f2(z['lo'])}–{f2(z['hi'])}): engel")
                break
        fb = fx.get("fib")
        if fb and fb["yon"] == d:
            lo_, hi_ = fb["altin"]
            ref = x["l"] if d > 0 else x["h"]
            if lo_ - 0.2 * a5 <= ref <= hi_ + 0.2 * a5 and not any("Fibonacci" in w for w in why):
                s += 3
                why.append(f"Fibonacci 0,5–0,618 altın bölgesinden dönüş ({f2(lo_)}–{f2(hi_)})")
        m = formasyon.mum(x["o"], x["h"], x["l"], c)
        x["_mum"] = m
        good = ("güçlü yeşil", "alt fitil reddi") if d > 0 else ("güçlü kırmızı", "üst fitil reddi")
        bad = "üst fitil reddi" if d > 0 else "alt fitil reddi"
        if m in good:
            s += 3
            why.append(f"Sinyal mumu: {m}")
        elif m == bad:
            s -= 5
            why.append(f"Sinyal mumunda uzun {'üst' if d > 0 else 'alt'} fitil: bu yön reddedildi")
        if x.get("ema50"):
            if (x["ema20"] - x["ema50"]) * d > 0:
                s += 2
            else:
                s -= 3
                why.append("EMA 20, EMA 50'nin ters tarafında")
        return s

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

        # E) 1 dk SCALP — hacimli itki: güçlü gövdeli, hacim patlamalı mum son 5 mumun tepesini/dibini kırar
        rng = x["h"] - x["l"]
        if 3 <= mins <= 385 and x["volr"] >= 2.5 and rng > 0 and rng <= 3 * atr and x["ph5"] is not None:
            body = abs(c - o)
            for d in (1, -1):
                lvl = x["ph5"] if d > 0 else x["pl5"]
                near_end = (c - x["l"]) / rng >= 0.75 if d > 0 else (x["h"] - c) / rng >= 0.75
                if (c - o) * d > 0 and body >= 0.6 * rng and near_end and (c - lvl) * d > 0 \
                        and (x["vwap"] is None or (c - x["vwap"]) * d > 0) and (x["ema9"] - x["ema21"]) * d > 0 \
                        and self._ready(sym, "itki", d, T):
                    base_stop = (x["l"] if d > 0 else x["h"]) - d * 0.1 * atr
                    stop, risk = self._clamp(c, base_stop, d, atr, 0.5, 2.0)
                    why = [f"1 dk hacimli itki: hacim ortalamanın {x['volr']:.1f} katı, güçlü {'yeşil' if d > 0 else 'kırmızı'} gövde",
                           f"Son 5 mumun {'tepesi' if d > 0 else 'dibi'} ({f2(lvl)}) kırıldı",
                           "Scalp: hedef 1,5R, hızlı çıkış"]
                    out.append(("itki", d, c, stop, risk, 52, why))

        # F) 1 dk SCALP — VWAP sekmesi: trend yönünde VWAP'a değip hacimle geri dönüş
        vw = x["vwap"]
        if vw and 20 <= mins <= 380 and x["volr"] >= 1.3 and rng_ok and len(x["closes4"]) >= 3:
            for d in (1, -1):
                trend = (x["ema9"] - x["ema21"]) * d > 0 and all((pc_ - vw) * d > 0 for pc_ in x["closes4"][-3:])
                touch = (x["l"] <= vw * 1.0008) if d > 0 else (x["h"] >= vw * 0.9992)
                if trend and touch and (c - vw) * d > 0 and (c - o) * d > 0 and self._ready(sym, "vwap_sek", d, T):
                    base_stop = (min(x["l"], vw) if d > 0 else max(x["h"], vw)) - d * 0.15 * atr
                    stop, risk = self._clamp(c, base_stop, d, atr, 0.5, 2.0)
                    why = [f"{'Yükselen' if d > 0 else 'Düşen'} trendde VWAP'a ({f2(vw)}) değip hacimle geri döndü",
                           f"Hacim ortalamanın {x['volr']:.1f} katı",
                           "Scalp: hedef 1,5R, hızlı çıkış"]
                    out.append(("vwap_sek", d, c, stop, risk, 50, why))
        return out

    def _extended(self, sym, x):
        out = []
        T, c, atr = x["T"], x["c"], x["atr"]
        # Likidite filtresi: düşük hacimli sahte hareketleri ele
        if x["ses_vol"] < EXT_MIN_SES_VOL or x["vol5"] < EXT_MIN_5BAR_VOL or x["v"] < EXT_MIN_BAR_VOL \
                or x["active10"] < EXT_MIN_ACTIVE or x["volr"] < EXT_MIN_VOLR or (x["h"] - x["l"]) > 3 * atr:
            return out
        liq = f"Likidite yeterli (IEX): seans hacmi {kb(x['ses_vol'])}, son 5 dk {kb(x['vol5'])}, hacim ortalamanın {x['volr']:.1f} katı"
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
        # Seans aralığı kırılımı: seansın ilk 20 dk'sından sonra seans tepesi/dibi
        if x["ses_mins"] >= (20 if x["ses"] == "pre" else 10):
            sname = "piyasa öncesi" if x["ses"] == "pre" else "piyasa sonrası"
            for d, lvl, ad in ((1, x["sh2"], f"{sname} seans tepesi"), (-1, x["sl2"], f"{sname} seans dibi")):
                if lvl and not any(c_[0] == "gap" and c_[1] == d for c_ in cands):
                    cands.append(("uz_kirilim", d, lvl, ad, 50))
        if x["ses"] == "pre":
            for d, lvl, ad in ((1, x["pc"], "önceki kapanış"), (-1, x["pc"], "önceki kapanış"),
                               (1, x["pdh"], "önceki gün tepesi"), (-1, x["pdl"], "önceki gün dibi")):
                if lvl:
                    cands.append(("uz_seviye", d, lvl, ad, 45))
        else:  # post
            for d, lvl, ad in ((1, x.get("hod"), "gün tepesi"), (-1, x.get("lod"), "gün dibi"),
                               (1, x["rc"], "seans kapanışı"), (-1, x["rc"], "seans kapanışı")):
                if lvl:
                    cands.append(("uz_seviye", d, lvl, ad, 45 if abs(move) < 2 else 52))

        # Seans VWAP geri alma: 3 mum karşı tarafta, son 2 mum VWAP'ın bu tarafında kapandı
        sv, svp = x["svwap"], x["svwap_prev"]
        if sv and svp and len(x["closes4"]) >= 4 and x["ses_mins"] >= 15:
            for d in (1, -1):
                before = x["closes4"][:3]
                if all((pc_ - svp) * d < 0 for pc_ in before) and (x["prev_c"] - svp) * d > 0 and (c - sv) * d > 0:
                    cands.append(("uz_vwap", d, sv, "seans VWAP'ı", 48))

        for setup, d, lvl, ad, base in cands:
            if setup == "uz_vwap":
                if not self._ready(sym, setup, d, T):
                    continue
            elif not confirmed(lvl, d) or not self._ready(sym, setup, d, T):
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

    def _runner(self, sym, x):
        """Haberle/yüksek hacimle koşan küçük hisseler — sadece alış yönü."""
        out = []
        info = self.runners.get(sym, {})
        if info.get("news_kind") == "kötü" or (info.get("sec") or {}).get("kind") == "seyreltme":
            return out   # hisse ihracı / ters bölünme / seyreltme (haber ya da SEC bildirimi): genelde tuzak, alış yok
        T, c, atr = x["T"], x["c"], x["atr"]
        ref = x["pc"]
        move = (c / ref - 1) * 100 if ref else float(info.get("pct") or 0)
        if move < RUN_MIN_MOVE:
            return out
        dv_bar = x["v"] * c
        ext = x["ses"] != "regular"
        k_ = IEX_SHARE if ext else 1.0
        if x["ses_dv"] < RUN_MIN_SES_DV * k_ or dv_bar < RUN_MIN_BAR_DV * k_ or x["active10"] < 7 \
                or (x["h"] - x["l"]) > 4 * atr:
            return out
        liq = f"Seans işlem hacmi {usd(x['ses_dv'])}, son mum {usd(dv_bar)}" + (" (IEX)" if ext else "")

        # 1) Tepe kırılımı (gün/seans tepesi), hacimli
        if ext:
            lvl = x["sh2"]
            ok = bool(lvl) and x["prev_c"] > lvl and c > lvl and x["prev2_c"] <= lvl
        else:
            lvls = [p for p in (x["hod"], x["pmh"]) if p]
            lvl = max(lvls) if lvls else None
            ok = bool(lvl) and c > lvl and x["prev_c"] <= lvl and (x["vwap"] is None or c > x["vwap"])
        if ok and x["volr"] >= 2 and self._ready(sym, "kosu", 1, T):
            base_stop = (lvl - 0.5 * atr) if ext else (min(x["l"], lvl) - 0.3 * atr)
            stop, risk = self._clamp(c, base_stop, 1, atr, 1.0, 3.0)
            risk = max(risk, c * 0.01)
            why = [f"Günün yükseleni: önceki kapanışa göre {move:+.0f}%",
                   f"{'Seans' if ext else 'Gün'} tepesi ({f2(lvl)}) hacimle kırıldı{', 2 mum teyitli' if ext else ''}",
                   f"Hacim ortalamanın {x['volr']:.1f} katı · {liq}"]
            out.append(["kosu", 1, c, c - risk, risk, 55, why])

        # 2) İlk geri çekilme: güçlü koşu → düşük hacimli geri çekilme → önceki mumun tepesi kırılıyor
        rec = x["recent"]
        if not out and len(rec) >= 8 and x["volr"] >= 1.5 and self._ready(sym, "geri", 1, T):
            hist = rec[:-1]
            pi = max(range(len(hist)), key=lambda i: hist[i][1])
            pull = hist[pi + 1:]
            if 2 <= len(pull) <= 6:
                peak_h = hist[pi][1]
                peak_v = max(b[4] for b in hist[max(0, pi - 2):pi + 1]) or 1
                pull_low = min(b[2] for b in pull)
                pull_v = sum(b[4] for b in pull) / len(pull)
                start = hist[0][3]
                run = peak_h / start - 1 if start else 0
                depth = (peak_h - pull_low) / peak_h
                support = (x["vwap"] is None or pull_low > x["vwap"]) and pull_low > x["ema21"] * 0.995
                if run >= 0.04 and 0.005 <= depth <= 0.6 * run and pull_v < 0.7 * peak_v and support \
                        and c > pull[-1][1] and c > x["o"]:
                    stop, risk = self._clamp(c, pull_low - 0.1 * atr, 1, atr, 1.0, 3.0)
                    risk = max(risk, c * 0.01)
                    why = [f"Güçlü koşu (+{run * 100:.0f}%) sonrası düşük hacimli geri çekilme (−{depth * 100:.1f}%)",
                           f"Geri çekilme bitti: önceki mumun tepesi ({f2(pull[-1][1])}) kırıldı",
                           f"Hacim ortalamanın {x['volr']:.1f} katı · {liq}"]
                    out.append(["geri", 1, c, c - risk, risk, 55, why])

        news = info.get("news")
        ai = info.get("ai")
        for o in out:
            if ai:
                o[6].append(f"Yapay zeka haber puanı {ai['skor']:+d} ({ai['neden']}) — şimdilik sadece ölçülüyor")
            sec = info.get("sec") or {}
            if sec.get("kind") == "dikkat":
                o[6].append(sec["msg"])
                o[5] -= 5
            elif sec.get("kind") == "temiz":
                o[6].append(sec["msg"])
            if news:
                o[6].insert(0, f"Haber: {news.get('headline', '')[:100]}")
                if info.get("news_kind") == "iyi":
                    o[6].insert(1, f"Haber olumlu ('{info.get('news_word', '')}'): gerçek bir sebep var")
                    o[5] += 15
                else:
                    o[5] += 5
            else:
                o[6].append("Haber bulunamadı — hareketin sebebi belirsiz (risk yüksek)")
                o[5] -= 10
            if c < 1:
                o[6].append("1 $ altı hisse: manipülasyon ve işlem durdurma riski çok yüksek")
                o[5] -= 5
            o[6].append("Küçük/koşan hisse: kayma yüksek varsayıldı" + (", limit emir ve yarım lot" if ext else ""))
        return [tuple(o) for o in out]

    # ------------------------------------------------------------ grafik okuma (formasyon + alıcı bölgesi)
    def fx_at(self, sym, tss, bars, k):
        """k. mum anındaki grafik analizi: kapanmış 5 dk mumlar (son ~3 gün) üzerinden. Önbellekli."""
        T = tss[k]
        key = (sym, T // 300)
        c = self._fx.get(key)
        if c is not None:
            return c
        lim = T // 300 * 300                     # bu 5 dk dilimi henüz kapanmadı → dahil etme
        rows = [(t, *bars[t][:5]) for t in tss[max(0, k - 900):k + 1]
                if t < lim and self.bar_info(t)[1] != 3]
        fx = formasyon.analyze(formasyon.to5m(rows)[-260:])
        if len(self._fx) > 3000:
            self._fx.clear()
        self._fx[key] = fx
        return fx

    def _chart(self, sym, x, tss, bars, k):
        """Grafiğe bakarak: formasyon boyun çizgisi kırılımı ve alıcı bölgesinden hacimli dönüş."""
        out = []
        fx = self.fx_at(sym, tss, bars, k)
        x["_fx"] = fx
        T, c, o, atr = x["T"], x["c"], x["o"], x["atr"]
        a5 = fx.get("atr") or atr * 2.2
        ext = x["ses"] != "regular"
        runner = sym in self.runners
        # 1) Formasyon kırılımı: bu mum boyun çizgisini hacimle kapanışla geçti
        for p in fx.get("pat", []):
            if p["durum"] != "oluşuyor":
                continue
            d = 1 if p["bull"] else -1
            if d < 0 and (ext or runner):
                continue                          # uzatılmış seans / koşan hissede açığa satış yok
            neck = p["neck_now"]
            if not ((x["prev_c"] - neck) * d <= 0 < (c - neck) * d):
                continue
            if x["volr"] < 1.5 or not self._ready(sym, "formasyon", d, T):
                continue
            # stop: boyun çizgisinin 1 (5 dk) ATR ötesi; hedef: formasyon yüksekliği kadar
            stop = neck - d * a5
            risk = abs(c - stop)
            risk = max(risk, c * MIN_STOP_PCT, atr * MIN_STOP_ATR)
            if risk > c * 0.05:
                continue
            tgt = p["hedef"]
            if (tgt - c) * d < 1.2 * risk:
                tgt = c + d * 2 * risk            # formasyon hedefi çok yakınsa en az 2R
            self._tgt_over[(sym, T, "formasyon", d)] = tgt
            why = [f"{p['ad']} formasyonu: boyun çizgisi ({f2(neck)}) hacimle {'yukarı' if d > 0 else 'aşağı'} kırıldı",
                   f"Formasyon hedefi {f2(p['hedef'])} (formasyon yüksekliği {f2(p['boy'])})",
                   f"Hacim ortalamanın {x['volr']:.1f} katı"]
            out.append(("formasyon", d, c, c - d * risk, risk, 56, why))
            break
        # 2) Alıcı bölgesinden dönüş: fiyat bölgeye indi, yeşil mumla ve hacimle bölgenin üstünde kapandı
        for z in fx.get("zones", [])[:1]:
            if not (x["l"] <= z["hi"] and c > z["hi"] and c > o and x["prev_c"] <= z["hi"] * 1.003):
                continue
            if x["volr"] < 1.5 or x["ema9"] < x["ema21"] or not self._ready(sym, "talep", 1, T):
                continue
            stop = z["lo"] - 0.3 * atr
            risk = max(c - stop, c * MIN_STOP_PCT, atr * MIN_STOP_ATR)
            if risk > c * 0.04:
                continue
            why = [f"Alıcı bölgesi ({f2(z['lo'])}–{f2(z['hi'])}) test edildi, yeşil mumla üstünde kapandı",
                   f"Bu bantta işlem hacminin %{z['alici']}'i yükselen mumlarda (alıcılar toplanmış)",
                   f"Hacim ortalamanın {x['volr']:.1f} katı, kısa trend yukarı"]
            out.append(("talep", 1, c, c - risk, risk, 55, why))
        # Uzatılmış seans / koşan hisse dışında kalan grafik kurguları (sadece alış)
        up_ok = x["ema9"] > x["ema21"] or ((fx.get("yapi") or {}).get("trend") == "yükseliş")
        green = c > o
        # 3) FVG dönüşü: fiyat boğa boşluğuna indi, yeşil mumla boşluğun üstünde kapandı
        for z in fx.get("fvg") or []:
            if not z["bull"] or not (x["l"] <= z["hi"] < c and x["prev_c"] >= z["lo"] and green):
                continue
            if x["volr"] < 1.2 or not up_ok or not self._ready(sym, "fvg", 1, T):
                break
            stop = z["lo"] - 0.3 * atr
            risk = max(c - stop, c * MIN_STOP_PCT, atr * MIN_STOP_ATR)
            if risk > c * 0.04:
                break
            why = [f"Boğa FVG boşluğu ({f2(z['lo'])}–{f2(z['hi'])}) test edildi, yeşil mumla üstünde kapandı",
                   "Dolmamış boşluk destek gibi çalıştı (alıcılar boşluğu savundu)",
                   f"Hacim ortalamanın {x['volr']:.1f} katı"]
            out.append(("fvg", 1, c, c - risk, risk, 54, why))
            break
        # 4) Kanal alt bandından dönüş (yükselen kanal / trend çizgisi sekmesi)
        kn = fx.get("kanal")
        if kn and kn["yon"] > 0 and not kn["kirildi"]:
            alt = kn["ana_now"]
            if x["l"] <= alt + 0.15 * a5 and c > alt and green and x["volr"] >= 1.2 and self._ready(sym, "kanal", 1, T):
                stop = alt - 0.5 * a5
                risk = max(c - stop, c * MIN_STOP_PCT, atr * MIN_STOP_ATR)
                if risk <= c * 0.04:
                    tgt = kn["karsi_now"] if kn["karsi_now"] - c >= 1.5 * risk else c + 2 * risk
                    self._tgt_over[(sym, T, "kanal", 1)] = tgt
                    why = [f"Yükselen trend çizgisine ({f2(alt)}) dokunup döndü (kanal alt bandı)",
                           f"Hedef kanalın üst bandı {f2(kn['karsi_now'])}",
                           f"Hacim ortalamanın {x['volr']:.1f} katı"]
                    out.append(("kanal", 1, c, c - risk, risk, 54, why))
        # 5) Yapı kırılımı: son tepe (swing high) hacimle kapanışla geçildi
        y = fx.get("yapi") or {}
        lvl = y.get("son_tepe")
        if lvl and x["prev_c"] <= lvl < c and green and x["volr"] >= 1.5 and not ext and self._ready(sym, "yapi", 1, T):
            sd = y.get("son_dip")
            stop = max(sd - 0.2 * a5, c - 3 * a5) if sd and sd < c else c - 1.5 * a5
            risk = max(c - stop, c * MIN_STOP_PCT, atr * MIN_STOP_ATR)
            if risk <= c * 0.04:
                tip = "karakter değişimi (CHoCH)" if y.get("trend") == "düşüş" else "yapı kırılımı (BOS)"
                why = [f"Son tepe {f2(lvl)} kapanışla geçildi: {tip}",
                       f"Stop son dibin altında ({f2(stop)})" if sd and sd < c else "Stop 1,5 ATR altında",
                       f"Hacim ortalamanın {x['volr']:.1f} katı"]
                out.append(("yapi", 1, c, c - risk, risk, 54, why))
        # 6) Fibonacci geri çekilme dönüşü: yukarı hareketin 0,5–0,618 bölgesinde dönüş mumu
        fb = fx.get("fib")
        if fb and fb["yon"] > 0:
            lo_, hi_ = fb["altin"]
            if lo_ - 0.2 * a5 <= x["l"] <= hi_ + 0.2 * a5 and green and c > x["prev_h"] and up_ok \
                    and self._ready(sym, "fib", 1, T):
                stop = fb["lv"]["0.786"] - 0.3 * a5
                risk = max(c - stop, c * MIN_STOP_PCT, atr * MIN_STOP_ATR)
                if risk <= c * 0.04:
                    tepe = fb["b"][1]
                    tgt = tepe if tepe - c >= 1.5 * risk else c + 2 * risk
                    self._tgt_over[(sym, T, "fib", 1)] = tgt
                    why = [f"Son yükselişin Fibonacci 0,5–0,618 bölgesine ({f2(lo_)}–{f2(hi_)}) geri çekildi",
                           "Dönüş mumu: önceki mumun tepesinin üstünde kapandı",
                           f"Hedef hareketin tepesi {f2(tepe)}, stop 0,786 seviyesinin altında"]
                    out.append(("fib", 1, c, c - risk, risk, 54, why))
        return out

    # ------------------------------------------------------------ anlık kırılım (koşan hisseler)
    def fast_check(self, sym, px, now):
        """Koşan hissede canlı fiyat gün/seans tepesini ŞİMDİ kırdıysa mum kapanmasını beklemeden sinyal üret.
        Dönüş: yeni sinyal ya da None."""
        info = self.runners.get(sym)
        if not info or not px or px <= 0:
            return None
        prev = self._fast_px.get(sym)
        self._fast_px[sym] = px
        if prev is None or info.get("news_kind") == "kötü" or (info.get("sec") or {}).get("kind") == "seyreltme":
            return None
        bars = self.store.bars.get(sym) or {}
        lim = int(now // 60 * 60) - 60           # son KAPANMIŞ mum
        tss = sorted(t for t in bars if lim - 6 * 86400 <= t <= lim)
        if len(tss) < 30:
            return None
        T = tss[-1]
        if now - T > 300 or self.bar_info(T)[0] != datetime.fromtimestamp(now, self.ET).date():
            return None                          # veri eski ya da bugünün verisi yok
        x = self._ctx(tss, bars, len(tss) - 1)
        if not x or x["sc"] == 3:
            return None
        ext = x["ses"] != "regular"
        atr = x["atr"]
        # Kırılacak seviye: seansın/günün o ana kadarki tepesi (öncesi tepesi dahil)
        if ext:
            lvl = max(v for v in (x["sh"], x["h"]) if v)
        else:
            lvl = max(v for v in (x["hod"], x["pmh"], x["h"]) if v)
        trig = lvl * 1.002
        if not (prev < trig <= px) or px > lvl * 1.03:
            return None                          # kırılım şimdi olmadı ya da fiyat çok kaçtı
        ref = x["pc"]
        move = (px / ref - 1) * 100 if ref else float(info.get("pct") or 0)
        if move < RUN_MIN_MOVE:
            return None
        k_ = IEX_SHARE if ext else 1.0
        if x["ses_dv"] < RUN_MIN_SES_DV * k_ or x["active10"] < 6:
            return None                          # likidite yok
        recent_vol = max(x["volr"], (x["vol5"] / 5 / x["avgv"]) if x.get("avgv") else 0)
        if recent_vol < 1.5:
            return None                          # kırılımda hacim yok
        if not self._ready(sym, "hizli", 1, T):
            return None
        low3 = min(r[2] for r in x["recent"][-3:]) if x["recent"] else x["l"]
        stop, risk = self._clamp(px, min(low3, lvl - 0.5 * atr), 1, atr, 1.0, 3.0)
        risk = max(risk, px * 0.01, px * MIN_STOP_PCT, atr * MIN_STOP_ATR)
        stop = px - risk
        news = info.get("news")
        kind = info.get("news_kind") or ("nötr" if news else None)
        why = [f"ANLIK kırılım: {'seans' if ext else 'gün'} tepesi ({f2(lvl)}) şimdi {f2(px)} ile geçildi, mum kapanışı beklenmedi",
               f"Günün yükseleni: önceki kapanışa göre {move:+.0f}%",
               f"Son mumlarda hacim ortalamanın {recent_vol:.1f} katı · seans işlem hacmi {usd(x['ses_dv'])}"
               + (" (IEX)" if ext else "")]
        x2 = dict(x)
        x2["c"] = px
        conf = self._score(x2, 1, 58, why, risk, sym)
        if kind == "iyi":
            why.insert(0, f"Haber olumlu ('{info.get('news_word', '')}'): {news.get('headline', '')[:90]}")
            conf += 15
        elif news:
            why.insert(0, f"Haber: {news.get('headline', '')[:100]}")
        else:
            why.append("Haber bulunamadı — hareketin sebebi belirsiz (risk yüksek)")
            conf -= 10
        if px < 1:
            why.append("1 $ altı hisse: manipülasyon ve işlem durdurma riski çok yüksek")
            conf -= 5
        sec = info.get("sec") or {}
        if sec.get("kind") == "dikkat":
            why.append(sec["msg"])
            conf -= 5
        elif sec.get("kind") == "temiz":
            why.append(sec["msg"])
        if info.get("ai"):
            why.append(f"Yapay zeka haber puanı {info['ai']['skor']:+d} ({info['ai']['neden']}) — şimdilik sadece ölçülüyor")
        why.append("Hızlı giriş: kayma yüksek varsayıldı" + (", limit emir ve yarım lot" if ext else ""))
        conf = int(max(0, min(100, conf)))
        before = len(self.signals)
        self._create(sym, x2, "hizli", 1, px, stop, risk, conf, why)
        return self.signals[-1] if len(self.signals) > before else None

    def watch_plan(self, sym, px, now):
        """Koşan hisse tepesine %3'ten fazla yaklaşmadıysa None; yaklaştıysa (ve bu seviye için daha önce
        söylenmediyse) "X'i kırarsa giriyor" planı: {"lvl", "stop", "tgt", "px", "move"}."""
        info = self.runners.get(sym)
        if not info or not px or info.get("news_kind") == "kötü" or (info.get("sec") or {}).get("kind") == "seyreltme":
            return None
        bars = self.store.bars.get(sym) or {}
        lim = int(now // 60 * 60) - 60
        tss = sorted(t for t in bars if lim - 6 * 86400 <= t <= lim)
        if len(tss) < 30:
            return None
        T = tss[-1]
        if now - T > 300 or self.bar_info(T)[0] != datetime.fromtimestamp(now, self.ET).date():
            return None
        x = self._ctx(tss, bars, len(tss) - 1)
        if not x or x["sc"] == 3:
            return None
        ext = x["ses"] != "regular"
        lvl = max(v for v in ((x["sh"], x["h"]) if ext else (x["hod"], x["pmh"], x["h"])) if v)
        if not (lvl * 0.97 <= px < lvl * 1.002):
            return None
        ref = x["pc"]
        move = (px / ref - 1) * 100 if ref else float(info.get("pct") or 0)
        if move < RUN_MIN_MOVE:
            return None
        last = self._plan_posted.get(sym)
        if last and abs(lvl / last - 1) < 0.01:
            return None
        self._plan_posted[sym] = lvl
        atr = x["atr"]
        trig = lvl * 1.002
        low3 = min(r[2] for r in x["recent"][-3:]) if x["recent"] else x["l"]
        stop, risk = self._clamp(trig, min(low3, lvl - 0.5 * atr), 1, atr, 1.0, 3.0)
        risk = max(risk, trig * 0.01, trig * MIN_STOP_PCT, atr * MIN_STOP_ATR)
        return {"lvl": trig, "stop": trig - risk, "tgt": trig + R_RUNNER * risk, "px": px, "move": move}

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
        if sym in self.runners:
            cands = self._runner(sym, x)
        else:
            cands = self._regular(sym, x) if x["ses"] == "regular" else self._extended(sym, x)
        try:
            cands = list(cands) + self._chart(sym, x, tss, bars, k)
        except Exception:
            pass
        best = {}   # aynı mum + aynı yön için tek sinyal: en yüksek güvenli kurgu
        for setup, d, entry, stop, risk, base, why in cands:
            # Asgari stop mesafesi: çok dar stoplar normal 1 dk kıpırdamasında patlıyordu
            min_risk = max(entry * MIN_STOP_PCT, x["atr"] * MIN_STOP_ATR)
            if risk < min_risk:
                risk = min_risk
                stop = entry - d * risk
            conf = self._score(x, d, base, why, risk, sym)
            if d not in best or conf > best[d][-2]:
                best[d] = (setup, d, entry, stop, risk, conf, why)
        if len(best) == 2:   # aynı anda hem AL hem SAT çıktıysa belirsiz: hiçbirini verme
            return
        for setup, d, entry, stop, risk, conf, why in best.values():
            self._create(sym, x, setup, d, entry, stop, risk, conf, why)

    def _features(self, sym, x, d):
        """Öğrenme için sinyal anındaki koşullar (kovalara ayrılmış)."""
        vr = x["volr"]
        f = {"volr": "<2" if vr < 2 else "2-3" if vr < 3 else "3-5" if vr < 5 else "5+"}
        f["vwap"] = "yok" if not x["vwap"] else ("lehte" if (x["c"] - x["vwap"]) * d > 0 else "aleyhte")
        f["trend"] = "uyumlu" if (x["ema9"] - x["ema21"]) * d > 0 else "ters"
        m = self.mkt.get("dir")
        f["spy"] = "yok" if m is None or x["ses"] != "regular" else ("uyumlu" if m == d else "ters")
        room = x.get("_room")
        f["room"] = "bilinmiyor" if room is None else ("<1R" if room < 1 else "1-2R" if room < 2 else "2R+")
        if x["ses"] == "regular":
            mn = x["mins"]
            f["tod"] = "ilk 30 dk" if mn < 30 else "sabah" if mn < 120 else "öğle" if mn < 240 else "kapanışa doğru"
        else:
            f["tod"] = SES_AD[x["ses"]]
        c = x["c"]
        f["fiyat"] = "<1$" if c < 1 else "1-5$" if c < 5 else "5-20$" if c < 20 else "20$+"
        ob = x.get("obv", 0.0) * d
        f["obv"] = "lehte" if ob >= 0.25 else ("aleyhte" if ob <= -0.25 else "nötr")
        f["tip"] = "koşan küçük hisse" if sym in self.runners else "ana liste"
        fx = x.get("_fx") or {}
        same = [p for p in fx.get("pat", []) if (1 if p["bull"] else -1) == d]
        opp = [p for p in fx.get("pat", []) if (1 if p["bull"] else -1) != d]
        f["formasyon"] = same[0]["ad"] + " (lehte)" if same else (opp[0]["ad"] + " (aleyhte)" if opp else "yok")
        zs = fx.get("zones") or []
        f["talep"] = "alıcı bölgesine yakın" if zs and x["c"] - zs[0]["hi"] <= x["atr"] * 3 else "uzak / yok"
        y = fx.get("yapi") or {}
        tr = y.get("trend")
        f["yapi"] = "belirsiz" if tr in (None, "belirsiz", "karışık") else ("lehte" if (tr == "yükseliş") == (d > 0) else "ters")
        kn = fx.get("kanal")
        if kn and not kn.get("kirildi") and kn["ust_now"] > kn["alt_now"]:
            pos = (x["c"] - kn["alt_now"]) / (kn["ust_now"] - kn["alt_now"])
            pos = pos if d > 0 else 1 - pos
            f["kanal"] = "lehte bantta" if pos <= 0.3 else ("ters bantta" if pos >= 0.8 else "ortada")
        else:
            f["kanal"] = "yok"
        fv = fx.get("fvg") or []
        if any(z["bull"] == (d > 0) and z["lo"] <= (x["l"] if d > 0 else x["h"]) <= z["hi"] for z in fv):
            f["fvg"] = "lehte FVG'de"
        elif any(z["bull"] != (d > 0) for z in fv):
            f["fvg"] = "karşıda FVG var"
        else:
            f["fvg"] = "yok"
        fb = fx.get("fib")
        if fb and fb["yon"] == d:
            lo_, hi_ = fb["altin"]
            ref = x["l"] if d > 0 else x["h"]
            a5 = fx.get("atr") or x["atr"] * 2.2
            f["fib"] = "altın bölgede" if lo_ - 0.2 * a5 <= ref <= hi_ + 0.2 * a5 else "dışında"
        else:
            f["fib"] = "yok"
        f["mum"] = x.get("_mum") or formasyon.mum(x["o"], x["h"], x["l"], x["c"])
        if x.get("ema50"):
            f["ema50"] = "uyumlu" if (x["ema20"] - x["ema50"]) * d > 0 else "ters"
        bil = getattr(self, "bilanco", None)
        if bil:
            f["bilanco"] = bil(sym) or "yok"
        hv = getattr(self, "hava", None)
        if hv:
            f["hava"] = hv() or "bilinmiyor"
        if sym in self.runners:
            ri = self.runners[sym]
            f["haber"] = (ri.get("news_kind") or "nötr") if ri.get("news") else "yok"   # iyi / nötr / kötü / yok
            f["sec"] = (ri.get("sec") or {}).get("kind", "bakılmadı")
            ai = (ri.get("ai") or {}).get("skor")
            f["ai"] = "yok" if ai is None else ("+70 ve üstü" if ai >= 70 else "+30…+69" if ai >= 30
                                               else "−29…+29" if ai > -30 else "−30 ve altı")
        return f

    def _create(self, sym, x, setup, d, entry, stop, risk, conf, why):
        T = x["T"]
        ses = x["ses"]
        sid = f"{sym}-{T}-{setup}-{d}"
        if sid in self.by_id:
            return
        runner = sym in self.runners
        tgt = self._tgt_over.pop((sym, T, setup, d), None) or \
            entry + d * (R_RUNNER if runner else R_SETUP.get(setup, R_MULT)) * risk
        feats = self._features(sym, x, d)
        conf0 = conf
        if self.learner:
            try:
                conf, notes = self.learner.adjust(setup, ses, conf, feats)
                why = why + notes
            except Exception:
                pass
        sig = {
            "id": sid, "sym": sym, "dir": d, "setup": setup, "ses": ses, "t": T, "day": x["day"].isoformat(),
            "e": round(entry, 4), "s": round(stop, 4), "h": round(tgt, 4), "conf": conf, "conf0": conf0, "why": why,
            "runner": runner, "f": feats,
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
        slip = slip_of(sig)
        if st in ("stop", "süre"):
            px = px * (1 - d * slip)       # piyasa emri: aleyhte kayma
        fill = sig["fill"]
        pct = d * (px - fill) / fill - 2 * FEE
        risk_pct = abs(fill - sig["s"]) / fill
        sig.update(st=st, x=round(px, 4), xt=int(t), pct=round(pct * 100, 3),
                   r=round(pct / risk_pct, 2) if risk_pct > 0 else 0.0)

    def _update(self, sig, tss, bars, now):
        d, s, h = sig["dir"], sig["s"], sig["h"]
        slip = slip_of(sig)
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
        out = {k: s.get(k) for k in ("id", "sym", "dir", "setup", "ses", "t", "e", "s", "h", "conf", "conf0", "why", "typ",
                                     "st", "fill", "ft", "x", "xt", "r", "pct", "runner")}
        return out

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
            if s.get("runner"):
                add("Koşan küçük hisseler", s)
        order = ["Tümü"] + [SES_AD[k] for k in ("pre", "regular", "post")] + ["Güven ≥ 70", "Güven < 70", "Koşan küçük hisseler"]
        rows = [groups[k] for k in order if k in groups]
        rows += sorted((g for k, g in groups.items() if k not in order), key=lambda g: g["k"])
        for g in rows:
            g["avg"] = round(g["tot"] / g["n"], 3) if g["n"] else None
            g["wr"] = round(100 * g["w"] / g["n"], 1) if g["n"] else None
            g["tot"] = round(g["tot"], 2)
        return rows

    def open_count(self):
        return sum(1 for s in self.signals[-400:] if s["st"] in ("bekliyor", "açık"))
