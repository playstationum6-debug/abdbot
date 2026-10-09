"""
Kırılım Planı — kanalların yaptığını yapar: hareket başlamadan seviyeyi çizer, "X kırılırsa giriş" der
--------------------------------------------------------------------------------------------------------
Seviyeler (bugünün 1 dk mumlarından):
  taban      : gün içindeki en uzun dar bant (≥ 30 mum, aralık ≤ %8) → tavanı (ör. WFF sabah 2,05–2,17 → 2,17)
  kaybedilen : fiyatın önce üstünde olup sonra altına düştüğü taban / öncesi seans tepesi (geri alma)
  pmh        : piyasa öncesi tepesi (normal seansta)
  pdh        : dünkü tepe
  hod        : gün tepesi (fiyat altındaysa)
  yuvarlak   : 0,05 / 0,25 / 0,50 / 1,00 katları (herkesin baktığı sayılar)
Yakın seviyeler (%1,5) birleştirilir; puan = türlerin ağırlıkları. Puanı ≥ 2 olan, fiyatın %0,5–12 üstündeki en iyi
seviye plan olur. Giriş seviyenin hemen üstündeki "güzel" sayı (2,17 → 2,20), stop tabanın dibi ya da seviyenin
%3–8 altı, hedefler K1 = 1R, K2 = 2R, K3 = bir sonraki seviye.
Kırılım (kanalların "kırarsa giriş"i): mum girişin üstünde, hacim ortalamanın ≥ 1,5 katı, mumun üst %40'ında kapanır →
sinyal (giriş = kapanış, en fazla girişin %4 üstü). Sonraki mum seviyenin altına kapanırsa plan SAHTE KIRILIM sayılır
(karnede ayrı tutulur). Tek mumda %4'ten fazla kaçarsa kovalanmaz: 45 dk içinde seviyeye geri çekilip üstünde
tutarsa "retest" girişi.
"""
import math
import time
from datetime import datetime

AGIRLIK = {"taban": 2.0, "kaybedilen": 2.0, "pmh": 1.5, "pdh": 1.5, "hod": 1.0, "yuvarlak": 0.5, "kanal": 3.0, "liste": 2.5}
TUR_AD = {"taban": "sabahki / gün içi tabanın tavanı", "kaybedilen": "kaybedilen seviye (geri alma)",
          "pmh": "piyasa öncesi tepesi", "pdh": "dünkü tepe", "hod": "gün tepesi", "yuvarlak": "yuvarlak sayı",
          "kanal": "kanalın verdiği seviye", "liste": "sabah listesinin seviyesi"}
MIN_PUAN = 2.0
UZAK_MIN, UZAK_MAX = 0.005, 0.12
TEKRAR_SN = 15 * 60          # bitmiş plandan sonra aynı hissede yeni plan için bekleme
SURE_SN = 10 * 3600          # bekleyen plan en fazla bu kadar açık kalır (pratikte gün boyu)


def guzel(x, yukari=True):
    """Seviyenin hemen üstündeki 'güzel' sayı (kanalların yazdığı gibi): 2,17 → 2,20; 6,13 → 6,15."""
    adim = 0.05 if x < 10 else 0.25 if x < 50 else 0.5
    if x < 1:
        adim = 0.01
    y = math.ceil(x / adim - 1e-9) * adim if yukari else math.floor(x / adim + 1e-9) * adim
    if yukari and (y - x) / x > 0.02:          # çok uzaksa kuruşa yuvarla
        y = math.ceil(x * 100) / 100
    return round(y, 4)


def yuvarlaklar(c):
    adimlar = (0.05, 0.25) if c < 5 else (0.25, 0.5, 1.0) if c < 50 else (1.0, 5.0)
    if c < 1:
        adimlar = (0.05, 0.1)
    out = set()
    for a in adimlar:
        y = math.ceil(c / a + 1e-9) * a
        for i in range(3):
            v = round(y + i * a, 4)
            if c * (1 + UZAK_MIN) <= v <= c * (1 + UZAK_MAX):
                out.add(v)
    return sorted(out)


class Planci:
    def __init__(self, bt=False):
        self.bt = bt
        self.planlar = {}            # sym -> aktif plan
        self.gecmis = []             # biten planlar (son 300)
        self.olay = []               # (tür, plan) — app.py bildirir
        self.kanal = {}              # sym -> kanal seviyesi {"lvl","stop","hedefler","kanal","t"}
        self._bant = {}              # sym -> (k, bantlar) önbellek
        self._seq = 0
        self.uygun = None            # app.py: hangi hisselerde kendi planını kursun (kanal seviyesi her zaman)
        self.liste = {}              # sym -> sabah listesi seviyesi {"lvl","stop","hedefler","neden","t"} (gün boyu geçerli)

    # ------------------------------------------------------------------ kanal seviyesi
    def kanal_ekle(self, sym, lvl, stop=None, hedefler=None, kanal="", t=None):
        if not lvl or lvl <= 0:
            return
        self.kanal[sym] = {"lvl": float(lvl), "stop": float(stop) if stop else None, "hedefler": hedefler or [],
                           "kanal": kanal, "t": t or time.time()}

    def liste_ekle(self, sym, lvl, stop=None, hedefler=None, neden="", t=None):
        if not lvl or lvl <= 0:
            return
        self.liste[sym] = {"lvl": float(lvl), "stop": float(stop) if stop else None, "hedefler": hedefler or [],
                           "neden": neden, "t": t or time.time()}

    # ------------------------------------------------------------------ yardımcılar
    def _bantlar(self, tss, bars, ds, k):
        """Bugünün dar bantları: [(hi, lo, başlangıç_i, bitiş_i)] — 5 mumda bir yeniden hesaplanır."""
        n = k - ds + 1
        out = []
        i = ds
        while i < k - 30:
            hi, lo = bars[tss[i]][1], bars[tss[i]][2]
            j = i
            while j + 1 <= k - 2:
                h2, l2 = max(hi, bars[tss[j + 1]][1]), min(lo, bars[tss[j + 1]][2])
                if l2 <= 0 or (h2 - l2) / l2 > 0.08:
                    break
                hi, lo, j = h2, l2, j + 1
            if j - i + 1 >= 30:
                out.append((hi, lo, i, j))
                i = j + 1
            else:
                i += 10
        out.sort(key=lambda b: -(b[3] - b[2]))
        return out[:3]

    def seviyeler(self, sym, tss, bars, ds, k, x):
        c = x["c"]
        lv = []
        kk = (sym, tss[ds])
        key = k // 5
        b0 = self._bant.get(kk)
        if not b0 or b0[0] != key:
            self._bant[kk] = (key, self._bantlar(tss, bars, ds, k))
            if len(self._bant) > 60:
                self._bant.pop(next(iter(self._bant)))
        bantlar = self._bant[kk][1]
        for hi, lo, i0, i1 in bantlar:
            sonra = [bars[tss[i]][3] for i in range(i1 + 1, k + 1)]
            ustunde = sonra and max(sonra) > hi * 1.03
            if c < hi:
                lv.append((hi, "kaybedilen" if ustunde else "taban", lo, (tss[i0], tss[i1], lo, hi)))
        if x.get("ses") == "regular" and x.get("pmh") and c < x["pmh"]:
            pm_sonra = max((bars[tss[i]][3] for i in range(ds, k + 1)), default=0)
            lv.append((x["pmh"], "kaybedilen" if pm_sonra > x["pmh"] * 1.03 else "pmh", None, None))
        if x.get("pdh") and c < x["pdh"]:
            lv.append((x["pdh"], "pdh", None, None))
        if x.get("hod") and c < x["hod"]:
            lv.append((x["hod"], "hod", None, None))
        for v in yuvarlaklar(c):
            lv.append((v, "yuvarlak", None, None))
        lv = [v for v in lv if v[0] and c * (1 + UZAK_MIN) <= v[0] <= c * (1 + UZAK_MAX)]
        lv.sort(key=lambda v: v[0])
        kume = []
        for v in lv:
            if kume and v[0] <= kume[-1]["ust"] * 1.015:
                g = kume[-1]
                g["ust"] = max(g["ust"], v[0])
                g["tur"].add(v[1])
                if v[2]:
                    g["dip"] = min(g.get("dip") or v[2], v[2])
                if v[3] and not g.get("bant"):
                    g["bant"] = v[3]
            else:
                kume.append({"alt": v[0], "ust": v[0], "tur": {v[1]}, "dip": v[2], "bant": v[3]})
        for g in kume:
            g["puan"] = sum(AGIRLIK[t] for t in g["tur"])
        return kume

    def _yeni_id(self, sym):
        self._seq += 1
        return f"{sym}-{int(time.time())}-{self._seq}"

    def _plan_kur(self, sym, x, g, T, kaynak="bot", kanal=None, stop_k=None):
        """x["_dip20"]: son 20 mumun dibi (kontrol() doldurur)."""
        c, atr = x["c"], max(x.get("atr") or 0, x["c"] * 0.002)
        lvl = g["ust"]
        giris = guzel(lvl * 1.002)
        pay = min(max(3 * atr, giris * 0.03), giris * 0.08)
        stop = giris - pay
        if g.get("dip") and 0.02 <= (giris - g["dip"]) / giris <= 0.09:
            stop = g["dip"] * 0.99                     # tabanın dibinin %1 altı
        if c <= stop * 1.01 and x.get("_dip20"):
            stop = min(stop, x["_dip20"] * 0.995)      # geri alma planı: fiyat zaten seviyenin epey altında
        if stop_k and stop_k < giris and (giris - stop_k) / giris <= 0.12:
            stop = stop_k
        if stop >= c * 0.995 and x.get("_dip20"):
            stop = x["_dip20"] * 0.995                 # fiyat seviyenin çok altında: stop son dibin altı
        stop = round(stop, 4)
        r = giris - stop
        if r <= 0:
            return None
        k3 = round(giris + 3 * r, 4)
        p = {"id": self._yeni_id(sym), "sym": sym, "lvl": round(lvl, 4), "giris": giris, "stop": stop,
             "hedefler": [round(giris + r, 4), round(giris + 2 * r, 4), k3], "tur": sorted(g["tur"]),
             "puan": round(g["puan"], 1), "st": "bekliyor", "t": T, "kt": None, "kaynak": kaynak, "kanal": kanal,
             "px": c, "uzak": round((giris / c - 1) * 100, 1), "ses": x.get("ses"), "sig": None, "k1": 0, "k2": 0,
             "gun": str(x["day"]), "bant": g.get("bant")}
        p["iptal_alt"] = round(min(stop, (x.get("_dip20") or stop) * 0.995), 4)
        if r / giris > 0.10:                            # geri alma planı: gösterilen stop girişin %7 altı
            p["stop"] = round(giris * 0.93, 4)
            r = giris - p["stop"]
            p["hedefler"] = [round(giris + r, 4), round(giris + 2 * r, 4), round(giris + 3 * r, 4)]
        return p

    def _bitir(self, p, st, neden="", T=None):
        p["st"] = st
        p["neden"] = neden
        p["bitis"] = T or p.get("son_T") or p["t"]
        self.planlar.pop(p["sym"], None)
        self.gecmis.append(p)
        del self.gecmis[:-300]
        self.olay.append((st, p))

    def _aday(self, eng, p, c, T, tip, dip10=None):
        if (c - p["stop"]) / c > 0.10 and dip10:
            # geri alma planında plan stopu (dip) çok uzak kalır: son 10 mumun dibi, en fazla %7
            p["stop"] = round(max(dip10 * 0.995, c * 0.93), 4)
            r_ = p["giris"] - p["stop"] if p["giris"] > p["stop"] else c - p["stop"]
            p["hedefler"] = [round(c + r_, 4), round(c + 2 * r_, 4), round(c + 3 * r_, 4)]
        risk = c - p["stop"]
        if risk <= 0 or risk / c > 0.12:
            return None
        tur = " + ".join(TUR_AD.get(t, t) for t in p["tur"])
        bas = (f"Kırılım planı: {p['giris']} ({tur}) hacimle kırıldı, mum üstünde kapandı" if tip == "kırılım" else
               f"Kırılım planı: {p['giris']} kırılıp kaçtı, geri çekilmede seviye üstünde tuttu (retest)")
        why = [bas,
               f"Plan {datetime.fromtimestamp(p['t'], eng.ET).strftime('%H:%M')} (ABD) itibarıyla hazırdı: "
               f"fiyat seviyenin %{p['uzak']} altındaydı",
               f"Stop {p['stop']} · K1 {p['hedefler'][0]} · K2 {p['hedefler'][1]} · K3 {p['hedefler'][2]}"]
        if p.get("kanal"):
            why.insert(0, f"Seviye @{p['kanal']} kanalından; onayı bot kendi kuralıyla verdi")
        base = 52 + min(12, int(p["puan"] * 2)) + (2 if tip == "retest" else 0)
        p["st"], p["ot"], p["tip"] = "onaylı", T, tip
        self.olay.append(("onaylı", p))
        return (c, p["stop"], risk, base, why, p)

    # ------------------------------------------------------------------ her kapanan mumda
    def kontrol(self, eng, sym, tss, bars, k, x):
        """Dönüş: [(entry, stop, risk, base_conf, why, plan)] — kırılım (ya da kaçtıktan sonra retest) sinyal adayı.
        Durumlar: bekliyor → onaylı (kırılım mumu) → [sonraki mum seviye altı: sahte] · bekliyor → kaçtı → retest / sahte / süre"""
        T, c = x["T"], x["c"]
        ix = eng._index(tss, bars)
        day = ix["days"][k]
        ds = ix["start"][day]
        if k - ds < 20 or x["ses"] == "closed":
            return []
        p = self.planlar.get(sym)
        out = []
        if p and p["gun"] != str(x["day"]):
            self._bitir(p, "süre", "gün bitti", T)
            p = None
        onceki = [bars[tss[i]] for i in range(max(0, k - 20), k)]
        ort_v = sum(b[4] for b in onceki) / len(onceki) if onceki else 0
        x["_volr2"] = (x["v"] / ort_v) if ort_v > 0 else x.get("volr", 0)    # seans başında da çalışır
        x["_dip20"] = min(min((b[2] for b in onceki), default=x["l"]), x["l"])
        dip10 = min(min((b[2] for b in onceki[-10:]), default=x["l"]), x["l"])
        if p:
            p["son_T"] = T
            rng = (x["h"] - x["l"]) or 1e-9
            guclu = max(x.get("volr", 0), x["_volr2"]) >= 1.5 and (c - x["l"]) / rng >= 0.6
            if p["st"] == "bekliyor":
                if c >= p["giris"] and guclu:
                    p["vr"] = round(max(x.get("volr", 0), x["_volr2"]), 1)
                    p["ust"] = round(100 * (x["h"] - c) / rng)          # kapanış mumun tepesine ne kadar yakın (%)
                    if c <= p["giris"] * 1.04:
                        a = self._aday(eng, p, c, T, "kırılım", dip10)
                        if a:
                            out.append(a)
                        else:
                            self._bitir(p, "iptal", "stop çok uzak", T)
                    else:
                        p["st"], p["kt"] = "kaçtı", T
                        self.olay.append(("kaçtı", p))
                elif c < p.get("iptal_alt", p["stop"]):
                    self._bitir(p, "iptal", "fiyat kırılımdan önce stop seviyesinin altında kapandı", T)
                elif T - p["t"] > SURE_SN:
                    self._bitir(p, "süre", "kırılım gelmedi", T)
            elif p["st"] == "onaylı":
                if T > p["ot"]:
                    if c < p["lvl"]:
                        self._bitir(p, "sahte", f"kırılımdan sonraki mum tekrar {p['lvl']} altına kapandı", T)
                    else:
                        self._bitir(p, "onaylı", "kırılım tuttu", T)
            elif p["st"] == "kaçtı":
                if c < p["lvl"] * 0.99:
                    self._bitir(p, "sahte", f"kaçtıktan sonra {p['lvl']} altına döndü", T)
                elif x["l"] <= p["giris"] * 1.02 and c >= p["lvl"] and c > x["o"]:
                    a = self._aday(eng, p, c, T, "retest", dip10)
                    if a:
                        out.append(a)
                elif T - p["kt"] > 45 * 60:
                    self._bitir(p, "süre", "kaçtı, 45 dk içinde geri çekilme gelmedi", T)
            return out
        # yeni plan
        son = next((q for q in reversed(self.gecmis) if q["sym"] == sym), None)
        if son and T - (son.get("bitis") or son["t"]) < TEKRAR_SN:
            return []
        if c < 0.5:
            return []
        kn = self.kanal.get(sym)
        if kn and time.time() - kn["t"] < 6 * 3600 and kn["lvl"] * 0.85 < c < kn["lvl"] * 1.005:
            g = {"ust": kn["lvl"], "tur": {"kanal"}, "dip": None, "puan": AGIRLIK["kanal"]}
            pl = self._plan_kur(sym, x, g, T, "kanal", kn["kanal"], kn.get("stop"))
            if pl:
                pl["giris"] = round(kn["lvl"], 4)
                self.kanal.pop(sym, None)
                self.planlar[sym] = pl
                self.olay.append(("yeni", pl))
            return []
        ls = self.liste.get(sym)
        if ls and time.time() - ls["t"] < 12 * 3600 and ls["lvl"] * 0.85 < c < ls["lvl"] * 1.005:
            kume = [g for g in self.seviyeler(sym, tss, bars, ds, k, x) if abs(g["ust"] / ls["lvl"] - 1) <= 0.015]
            g = kume[0] if kume else {"ust": ls["lvl"], "tur": set(), "dip": None, "bant": None}
            g = dict(g, ust=ls["lvl"], tur=set(g["tur"]) | {"liste"})
            g["puan"] = sum(AGIRLIK[t] for t in g["tur"])
            pl = self._plan_kur(sym, x, g, T, "liste", None, ls.get("stop"))
            if pl:
                pl["giris"] = round(ls["lvl"], 4)
                r_ = pl["giris"] - pl["stop"]
                if r_ > 0:
                    pl["hedefler"] = [round(pl["giris"] + i * r_, 4) for i in (1, 2, 3)]
                pl["liste"] = 1
                self.liste.pop(sym, None)
                self.planlar[sym] = pl
                self.olay.append(("yeni", pl))
            return []
        if self.uygun and not self.uygun(sym):
            return []
        ref = x.get("pc")
        if not ref:
            return []
        hareket = (c / ref - 1) * 100
        sonen = bool(x.get("hod") and x["hod"] / ref >= 1.25)       # bugün koşup sönen hisse (WFF gibi)
        dv = x.get("ses_dv") or 0
        if (hareket < 2 and not sonen) or dv < (150_000 if x["ses"] != "regular" else 500_000):
            return []
        kume = [g for g in self.seviyeler(sym, tss, bars, ds, k, x) if g["puan"] >= MIN_PUAN]
        if not kume:
            return []
        g = max(kume, key=lambda g: (g["puan"], -g["ust"]))
        if "kaybedilen" in g["tur"]:
            dip30 = min((bars[tss[i]][2] for i in range(max(ds, k - 30), k + 1)), default=c)
            if c < dip30 * 1.04:
                return []                                  # düşüş sürüyor: dönüş başlamadan geri alma planı yok
        pl = self._plan_kur(sym, x, g, T)
        if pl:
            self.planlar[sym] = pl
            self.olay.append(("yeni", pl))
        return []

    # ------------------------------------------------------------------ karne
    def karne(self, sinyaller):
        """Plan türlerine göre: kaç plan, kaçı sahte çıktı, onaylıların ortalama R'si."""
        tur = {}
        for p in self.gecmis:
            for t in p["tur"]:
                d = tur.setdefault(t, {"plan": 0, "onay": 0, "sahte": 0, "r": []})
                d["plan"] += 1
                d["onay"] += p["st"] == "onaylı"
                d["sahte"] += p["st"] == "sahte"
        for s in sinyaller:
            if s.get("setup") != "kirilim" or s.get("r") is None or s.get("st") not in ("hedef", "stop", "süre"):
                continue
            for t in (s.get("plan_tur") or []):
                tur.setdefault(t, {"plan": 0, "onay": 0, "sahte": 0, "r": []})["r"].append(s["r"])
        out = []
        for t, d in tur.items():
            out.append({"k": t, "ad": TUR_AD.get(t, t), "plan": d["plan"], "onay": d["onay"], "sahte": d["sahte"],
                        "n": len(d["r"]), "avg": round(sum(d["r"]) / len(d["r"]), 3) if d["r"] else None,
                        "wr": round(100 * sum(1 for r in d["r"] if r > 0) / len(d["r"])) if d["r"] else None})
        out.sort(key=lambda r: -r["plan"])
        return out
