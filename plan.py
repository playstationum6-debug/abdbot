"""
Kırılım Planı — kanalların yaptığını yapar: hareket başlamadan seviyeyi çizer, "X kırılırsa giriş" der
--------------------------------------------------------------------------------------------------------
Seviyeler (bugünün 1 dk mumlarından):
  taban      : gün içindeki en uzun dar bant (≥ 30 mum, aralık ≤ %8) → tavanı (ör. WFF sabah 2,05–2,17 → 2,17)
  kaybedilen : fiyatın önce üstünde olup sonra altına düştüğü taban / öncesi seans tepesi (geri alma)
  pmh        : piyasa öncesi tepesi (normal seansta)
  pdh        : dünkü tepe
  cgd        : çok günlük direnç — son ~3 ayın günlük grafiğinde en az 2 kez dönülen tepe (app.py hesaplar)
  hod        : gün tepesi (fiyat altındaysa)
  yuvarlak   : 0,05 / 0,25 / 0,50 / 1,00 katları (herkesin baktığı sayılar)
Yakın seviyeler (%1,5) birleştirilir; puan = türlerin ağırlıkları. Puanı ≥ 2 olan, fiyatın %0,5–12 üstündeki en iyi
seviye plan olur. Giriş seviyenin hemen üstündeki "güzel" sayı (2,17 → 2,20), stop tabanın dibi ya da seviyenin
%3–8 altı, hedefler K1 = 1R, K2 = 2R, K3 = bir sonraki seviye.
Kırılım: 5 dk mum direncin üstünde kapanır, hacmi son 20 mumun ≥ 2 katı; açılışın ilk 15 dk'sı, tükenme (en yüksek
hacim tepe mumunda + uzun üst fitil), art arda 3 düşen tepe + azalan hacim, satış emirleri alışın 5 katı ya da spread
> %2 ise sinyal yok. Stop kırılan direncin hemen altı, K1 1,5R · K2 2,5R · K3 sonraki direnç; sonraki dirence kadar
risk/ödül 1:1,5 değilse plan kurulmaz. Kırılım tutarsa eski direnç destek olarak izlenir: geri çekilip üstünde
yeşil, hacimli 5 dk mum kapanırsa "tekrar test" (en güçlü giriş). Sonraki mum seviyenin altına kapanırsa plan SAHTE KIRILIM sayılır
(karnede ayrı tutulur). Tek mumda %4'ten fazla kaçarsa kovalanmaz: 45 dk içinde seviyeye geri çekilip üstünde
tutarsa "retest" girişi.
"""
import math
import os
import time
from datetime import datetime

STOP_PCT = float(os.getenv("STOP_PCT", "10")) / 100      # stop: girişin %10 altı (kırılımdan sonra)
HEDEF_PCT = [float(v) / 100 for v in os.getenv("HEDEF_PCT", "5,10,20").split(",")]   # K1 / K2 / K3


def sabit_stop_hedef(p, ref):
    """Stop ve hedefler girişe göre sabit yüzde: stop %10 altı, K1 +%5, K2 +%10, K3 +%20."""
    p["stop"] = round(ref * (1 - STOP_PCT), 4)
    p["hedefler"] = [round(ref * (1 + h), 4) for h in HEDEF_PCT]
    p["iptal_alt"] = p["stop"]                  # kırılımdan önce: 5 dk kapanış bu seviyenin altına inerse plan biter
    return p


AGIRLIK = {"taban": 2.0, "kaybedilen": 2.0, "pmh": 1.5, "pdh": float(os.getenv("PDH_PUAN", "2.0")),
           "cgd": float(os.getenv("CGD_PUAN", "2.0")), "acilis": 3.0, "hod": 1.0, "yuvarlak": 0.5, "kanal": 3.0, "liste": 2.5,
           "direnç": 1.5, "tam sayı": 1.0, "pgh": 1.5}
TUR_AD = {"taban": "sabahki / gün içi tabanın tavanı", "kaybedilen": "kaybedilen seviye (geri alma)",
          "pmh": "piyasa öncesi tepesi", "pdh": "dünkü tepe", "hod": "gün tepesi", "yuvarlak": "yuvarlak sayı",
          "kanal": "kanalın verdiği seviye", "liste": "sabah listesinin seviyesi",
          "direnç": "gün içinde birkaç kez dönülen tepe", "tam sayı": "tam dolar (herkesin baktığı sayı)",
          "pgh": "önceki günlerin tepesi", "cgd": "çok günlük direnç (son 3 ayda birkaç kez dönülen tepe)",
          "acilis": "açılış hamlesi (piyasa öncesi tepesi)"}
RED_AD = {"tukenme": "tükenme: en yüksek hacim tepe mumunda ve üst fitil gövdenin 2 katından uzun",
          "dusen": "art arda 3 düşen tepe + azalan hacim",
          "emir": "satış tarafındaki emirler alışın 5 katından fazla",
          "acilis": "açılışın ilk 15 dakikası (açılış hamlesinde: normal seans başlamadı)",
          "hacim": "kırılım mumunun hacmi son 20 mumun 2 katına ulaşmadı",
          "spread": "alış-satış farkı %2'den büyük (0,50$ altında %1) ya da bilinmiyor"}


def bes_dk(tss, bars, ds, k, sn=300):
    """Bugünün 1 dk mumlarından sn saniyelik mumlar [t0, o, h, l, c, v] (k'nın bloğu dahil). 300 = 5 dk, 900 = 15 dk."""
    out = []
    for i in range(ds, k + 1):
        t = tss[i]
        b = bars[t]
        blk = int(t // sn) * sn
        if out and out[-1][0] == blk:
            o_ = out[-1]
            o_[2], o_[3], o_[4], o_[5] = max(o_[2], b[1]), min(o_[3], b[2]), b[3], o_[5] + (b[4] or 0)
        else:
            out.append([blk, b[0], b[1], b[2], b[3], b[4] or 0])
    return out


def hizli_piyasa(tss, bars, k):
    """Hacim ve oynaklık arttı mı: son 5 dk'nın 1 dk mum aralığı önceki 20 mumun 1,5 katı ve hacmi 2 katı.
    Öyleyse kırılım 1 dk kapanışa göre onaylanır (yoksa 5 dk)."""
    if k < 26:
        return False
    son = [bars[tss[i]] for i in range(k - 4, k + 1)]
    once = [bars[tss[i]] for i in range(k - 25, k - 5)]
    r1 = sum(b[1] - b[2] for b in son) / 5
    r0 = sum(b[1] - b[2] for b in once) / 20
    v1 = sum(b[4] or 0 for b in son) / 5
    v0 = sum(b[4] or 0 for b in once) / 20
    return r0 > 0 and v0 > 0 and r1 >= 1.5 * r0 and v1 >= 2 * v0


def blok_sonu(t):
    """1 dk mum 5 dk bloğun son mumu mu (kapanış anı)."""
    return int(t // 60) % 5 == 4


def tukenme(B):
    """Son 20 mumda en yüksek hacim tepe mumundaysa ve üst fitil gövdenin 2 katından uzunsa (tepe son 6 mum içinde)."""
    S = B[-20:]
    if len(S) < 6:
        return False
    it = max(range(len(S)), key=lambda i: S[i][2])
    iv = max(range(len(S)), key=lambda i: S[i][5])
    if it != iv or it < len(S) - 6:
        return False
    o, h, c = S[it][1], S[it][2], S[it][4]
    govde = abs(c - o) or h * 0.0005
    return (h - max(o, c)) > 2 * govde


def dusen_tepeler(B):
    """Art arda 3 düşen tepe + azalan hacim."""
    if len(B) < 3:
        return False
    a, b, c = B[-3:]
    return a[2] > b[2] > c[2] and a[5] > b[5] > c[5]

MIN_PUAN = 2.0
# ---- kurallar (kanal bildirimleri için; işlem açılmaz)
BIRLESTIR = 0.02             # %2'den yakın seviyeler tek bölge
HACIM_K = 2.0                # kırılım mumu hacmi ≥ son 20 mum ortalamasının 2 katı
RETEST_HACIM_K = 1.5         # tekrar testte yeşil mum hacmi
ACILIS_BEKLE = 15            # 9:30'dan sonraki ilk 15 dk yeni sinyal yok
RR_MIN = 1.5                 # bir sonraki dirence kadar risk/ödül en az 1:1,5
MIN_GUN = float(os.getenv("PLAN_MIN_GUN", "10"))          # gün içinde en az bu kadar % yükselmiş olmalı (koşan hisse)
HARIC = set("TSLL TSLQ TSLZ TSLR SOXL SOXS TQQQ SQQQ SPXL SPXS SPXU UPRO SDOW UDOW TNA TZA LABU LABD FNGU FNGD "
            "NVDL NVDU NVDD NVDQ NVDX CONL CONY MSTU MSTX MSTZ AMDL AAPU GGLL AMZU MSFU PLTU METU UVXY SVXY UVIX VXX "
            "BOIL KOLD UCO SCO TMF TMV YINN YANG NUGT DUST JNUG JDST GUSH DRIP DPST NAIL BITX ETHU SBIT BITI".split()) | \
    set(x.strip().upper() for x in os.getenv("PLAN_HARIC", "").split(",") if x.strip())   # kaldıraçlı fonlar: kırılım botunun işi değil
MIN_FIYAT = float(os.getenv("PLAN_MIN_FIYAT", "0.20"))   # bunun altına plan yok
KURUS = 0.50                 # bunun altında spread en fazla %1 ve bilinmeli (kote yoksa sinyal yok)
EMIR_K = 5.0                 # satış tarafı emir miktarı alışın 5 katından büyükse sinyal yok
LOT_USD = 250                # mesajlardaki örnek işlem büyüklüğü
RETEST_SN = 90 * 60          # kırılım tuttuktan sonra tekrar test bu kadar izlenir
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
    def __init__(self, bt=False, ayar=None):
        self.bt = bt
        self.ayar = ayar or {}       # kural testi: {"hacim_k": 2.0, "tf": "uyarlanir"|"5"|"1", "filtre": 1}
        self.planlar = {}            # sym -> aktif plan
        self.gecmis = []             # biten planlar (son 300)
        self.olay = []               # (tür, plan) — app.py bildirir
        self.kanal = {}              # sym -> kanal seviyesi {"lvl","stop","hedefler","kanal","t"}
        self._bant = {}              # sym -> (k, bantlar) önbellek
        self._seq = 0
        self.uygun = None            # app.py: hangi hisselerde kendi planını kursun (kanal seviyesi her zaman)
        self.liste = {}              # sym -> sabah listesi seviyesi {"lvl","stop","hedefler","neden","t"} (gün boyu geçerli)
        self.kote_fn = None          # app.py: sym -> (alış, satış, ts, alış_adet, satış_adet)
        self.retest = {}             # sym -> kırılımı tutmuş plan (eski direnç destek olarak test edilecek)
        self.cgd = None              # app.py: sym -> [çok günlük direnç seviyeleri] (günlük grafikten)
        self.neden = {}              # sym -> (zaman, metin): plan neden kurulmadı / kırılım neden onaylanmadı

    # ------------------------------------------------------------------ kanal seviyesi
    def kanal_ekle(self, sym, lvl, stop=None, hedefler=None, kanal="", t=None):
        if not lvl or lvl <= 0:
            return
        self.kanal[sym] = {"lvl": float(lvl), "stop": float(stop) if stop else None, "hedefler": hedefler or [],
                           "kanal": kanal, "t": t or time.time()}

    def acilis_ekle(self, sym, lvl, t=None):
        """Açılış hamlesi: haberli, piyasa öncesi güçlü hisse — normal seansın ilk 30 dk'sında öncesi seans tepesi kırılırsa."""
        if lvl and lvl > 0:
            self.liste[sym] = {"lvl": float(lvl), "stop": None, "hedefler": [], "neden": "açılış", "t": t or time.time(),
                               "acilis": 1}

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

    @staticmethod
    def _direncler(tss, bars, ds, k, c):
        """Bugün birkaç kez dönülen tepeler (kanalların '4,00 kırılırsa' dediği yer): 7 mumluk pencerede tepe olan
        noktalar %1 içinde kümelenir; en az 2 dokunuş = direnç. Fiyat bir ara %2 üstüne çıkıp geri düştüyse
        'kaybedilen' (geri alma) sayılır."""
        if k - ds + 1 < 20:
            return []
        # destek/direnç 5 dk ve 15 dk mumlardan: tepe = iki yanındaki mumlardan yüksek (fitil ya da kapanış)
        tepe = []
        C = []
        for sn, w in ((300, 2), (900, 1)):
            B = bes_dk(tss, bars, ds, k, sn)
            if sn == 300:
                C = [b[4] for b in B]
            for j in range(w, len(B) - w):
                if B[j][2] >= max(b[2] for b in B[j - w:j + w + 1]):
                    tepe.append((B[j][2], j * sn // 300))
                if B[j][4] >= max(b[4] for b in B[j - w:j + w + 1]) and B[j][4] < B[j][2] * 0.995:
                    tepe.append((B[j][4], j * sn // 300))
        n = len(C)
        tepe.sort()
        kume, out = [], []
        for v, j in tepe:
            if kume and v <= kume[0][0] * (1 + BIRLESTIR):
                kume.append((v, j))
                continue
            if len(kume) >= 2:
                out.append(kume)
            kume = [(v, j)]
        if len(kume) >= 2:
            out.append(kume)
        lv = []
        for g in out:
            ust = max(v for v, _ in g)
            if not (c * (1 + UZAK_MIN) <= ust <= c * (1 + UZAK_MAX)):
                continue
            ilk = min(j for _, j in g)
            ustunde = any(C[j] > ust * 1.02 for j in range(ilk, n))
            lv.append((ust, "kaybedilen" if ustunde else "direnç", None, None))
        return lv

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
        if self.cgd and not self.bt:
            try:
                for v in self.cgd(sym) or []:
                    if c < v <= c * 1.12:
                        lv.append((v, "cgd", None, None))
            except Exception:
                pass
        for v in yuvarlaklar(c):
            lv.append((v, "tam sayı" if v >= 2 and abs(v - round(v)) < 1e-6 else "yuvarlak", None, None))
        lv += self._direncler(tss, bars, ds, k, c)
        # önceki günlerin tepeleri (bellekteki günler)
        ix = x.get("_ix")
        if ix:
            gunler = sorted(d for d in ix["start"] if ix["start"][d] < ds)[-5:]
            for d in gunler:
                a = ix["start"][d]
                b_ = min([ix["start"][e] for e in ix["start"] if ix["start"][e] > a] + [ds])
                if b_ > a:
                    lv.append((max(bars[tss[i]][1] for i in range(a, b_)), "pgh", None, None))
        lv = [v for v in lv if v[0] and c * (1 + UZAK_MIN) <= v[0] <= c * (1 + UZAK_MAX)]
        lv.sort(key=lambda v: v[0])
        kume = []
        for v in lv:
            if kume and v[0] <= kume[-1]["ust"] * (1 + BIRLESTIR):
                g = kume[-1]
                g["ust"] = max(g["ust"], v[0])
                g["tur"].add(v[1])
                if v[2]:
                    g["dip"] = min(g.get("dip") or v[2], v[2])
                if v[3] and not g.get("bant"):
                    g["bant"] = v[3]
            else:
                kume.append({"alt": v[0], "ust": v[0], "tur": {v[1]}, "dip": v[2], "bant": v[3]})
        B5 = bes_dk(tss, bars, ds, k)
        B15 = bes_dk(tss, bars, ds, k, 900)
        for g in kume:
            g["puan"] = sum(AGIRLIK[t] for t in g["tur"])
            u = g["ust"]
            # en az 2 kez dönülen seviye: fitil ya da kapanış seviyeye %1 yaklaşıp altında kapanan 5 dk mum
            g["dokunus"] = max(sum(1 for b in B5 if max(b[2], b[4]) >= u * 0.99 and b[4] < u),
                               sum(1 for b in B15 if max(b[2], b[4]) >= u * 0.99 and b[4] < u))
            # tek mumun içinden geçip giden seviye: gövde seviyeyi kesmiş (aşağıdan yukarı ya da yukarıdan aşağı)
            g["zayif"] = int(any(min(b[1], b[4]) < u * 0.995 and max(b[1], b[4]) > u * 1.005 for b in B5))
            if g["dokunus"] >= 2:
                g["puan"] += 1.0
            if g["zayif"] and g["dokunus"] < 2:
                g["puan"] -= 1.0
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
        sabit_stop_hedef(p, giris)
        p["dokunus"], p["zayif"] = g.get("dokunus", 0), g.get("zayif", 0)
        p["kurallar"] = ["seviye:" + t for t in sorted(g["tur"])] + (["2+ dönüş"] if g.get("dokunus", 0) >= 2 else []) + \
            (["zayıf seviye"] if g.get("zayif") else [])
        if x.get("ses") != "regular" and (x.get("ses_dv") or 0) < 1_000_000:
            p["yarim"] = 1                              # öncesi seans hacmi düşük: yarım lot
            p["kurallar"].append("öncesi seans yarım lot")
        return p

    def _bitir(self, p, st, neden="", T=None):
        p["st"] = st
        p["neden"] = neden
        p["bitis"] = T or p.get("son_T") or p["t"]
        self.planlar.pop(p["sym"], None)
        self.gecmis.append(p)
        del self.gecmis[:-300]
        self.olay.append((st, p))

    def _aday(self, eng, p, c, T, tip, dip10=None, olay=True):
        sabit_stop_hedef(p, c)                          # kırılım fiyatına göre: stop %10 altı, K1/K2/K3 +%5/10/20
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
            why.insert(0, "Seviye takip edilen bir kanaldan; onayı bot kendi kuralıyla verdi")
        base = 52 + min(12, int(p["puan"] * 2)) + (2 if tip == "retest" else 0)
        p["st"], p["ot"], p["tip"] = "onaylı", T, tip
        p["e"] = round(c, 4)
        if olay:
            self.olay.append(("onaylı", p))
        return (c, p["stop"], risk, base, why, p)

    def _red(self, sym, B5, x):
        """Sinyali engelleyen kural (yoksa None)."""
        try:
            n = datetime.fromtimestamp(x["T"], self._et)
            if (self.planlar.get(sym) or {}).get("acilis"):
                if x.get("ses") != "regular":
                    return "acilis"                          # açılış hamlesi sadece normal seansta onaylanır
            elif x.get("ses") == "regular" and n.hour == 9 and 30 <= n.minute < 30 + ACILIS_BEKLE:
                return "acilis"
        except Exception:
            pass
        if self.ayar.get("filtre", 1) and tukenme(B5):
            return "tukenme"
        if self.ayar.get("filtre", 1) and dusen_tepeler(B5[:-1]):
            return "dusen"
        k_ = self.kote_fn(sym) if self.kote_fn else None
        kurus = (x.get("c") or 1) < KURUS and not self.bt
        if kurus and not (k_ and time.time() - k_[2] < 180):
            return "spread"                                  # 0,50$ altı: alış-satış bilinmeden sinyal yok
        if k_ and time.time() - k_[2] < 180:
            if k_[0] > 0 and k_[1] > k_[0] and (k_[1] - k_[0]) / ((k_[0] + k_[1]) / 2) > (0.01 if kurus else 0.02):
                return "spread"
            if len(k_) >= 5 and k_[3] and k_[4] and k_[4] > EMIR_K * k_[3]:
                return "emir"
        return None

    def _nd(self, sym, T, metin):
        self.neden[sym] = (T, metin)
        if len(self.neden) > 400:
            for k_ in sorted(self.neden, key=lambda z: self.neden[z][0])[:100]:
                self.neden.pop(k_, None)
        return []

    def _red_kaydet(self, p, neden):
        self._nd(p["sym"], p.get("son_T") or time.time(), f"{p['giris']} üstüne çıktı ama onay yok: {RED_AD.get(neden, neden)}")
        if p.get("red") != neden:
            p["red"] = neden
            p.setdefault("redler", []).append(neden)
            if neden in ("tukenme", "dusen", "emir"):
                self.olay.append(("red", p))

    def _retest_kontrol(self, eng, sym, tss, bars, ds, k, x, T):
        """Kırılım → geri çekilme → eski direnç destek olarak tutar → yüksek hacimli yeşil 5 dk mum: en güçlü giriş."""
        r = self.retest.get(sym)
        if not r:
            return []
        p = r["p"]
        if T - r["t"] > RETEST_SN or p.get("gun") != str(x["day"]):
            self.retest.pop(sym, None)
            return []
        if not blok_sonu(T):
            return []
        B5 = bes_dk(tss, bars, ds, k)
        if len(B5) < 4:
            return []
        b = B5[-1]
        lvl = p["lvl"]
        if b[4] < lvl:                                  # kapanış desteğin altında: destek kırıldı
            self.retest.pop(sym, None)
            return []
        if b[3] > lvl * 1.01:                           # henüz geri çekilmedi
            r["uzaklasti"] = 1
            return []
        if not r.get("uzaklasti"):
            return []
        ort = sum(q[5] for q in B5[-21:-1]) / max(1, len(B5[-21:-1]))
        if b[4] > b[1] and ort > 0 and b[5] >= RETEST_HACIM_K * ort:   # fitil desteğe değdi, yeşil, hacimli kapanış
            red = self._red(sym, B5, x)
            if red in ("tukenme", "emir", "spread", "acilis"):   # geri çekilmede düşen tepe doğal: o kural burada yok
                return []
            self.retest.pop(sym, None)
            p2 = dict(p, id=p["id"] + "-rt", st="bekliyor", tip="retest2", vr=round(b[5] / ort, 1),
                      kurallar=(p.get("kurallar") or []) + ["kırılım+tekrar test"])
            sabit_stop_hedef(p2, b[4])
            a = self._aday(eng, p2, b[4], T, "retest2", None, olay=False)
            if a:
                self.olay.append(("retest", p2))
                return [a]
        return []

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
        x["_ix"] = ix
        self._et = eng.ET
        p = self.planlar.get(sym)
        out = []
        if not p and sym in self.retest:
            return self._retest_kontrol(eng, sym, tss, bars, ds, k, x, T)
        if p and p["gun"] != str(x["day"]):
            self._bitir(p, "süre", "gün bitti", T)
            p = None
        if p and p.get("acilis") and p.get("st") == "bekliyor" and datetime.fromtimestamp(T, eng.ET).hour >= 10:
            self._bitir(p, "süre", "açılış penceresi (ilk 30 dk) bitti", T)
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
            tfm = self.ayar.get("tf", "1")                  # kırılım/yaklaşma 1 dk mumla; seviyeler 5 ve 15 dk mumlardan
            hz = hizli_piyasa(tss, bars, k) if tfm == "uyarlanir" else tfm == "1"
            B5 = bes_dk(tss, bars, ds, k) if (blok_sonu(T) or hz) else None
            if p["st"] == "bekliyor":
                if hz:                                               # hacim + oynaklık yüksek: 1 dk kapanış
                    b5 = [T, x["o"], x["h"], x["l"], c, x["v"]]
                    ort5 = sum(q[4] or 0 for q in onceki) / len(onceki) if onceki else 0
                else:                                                # normal: 5 dk kapanış
                    b5 = B5[-1] if (B5 and blok_sonu(T)) else None
                    ort5 = (sum(q[5] for q in B5[-21:-1]) / max(1, len(B5[-21:-1]))) if b5 else 0
                if b5 and b5[4] < p["giris"]:
                    p["tut"] = 0
                if b5 and b5[4] >= p["giris"]:                       # kural: direncin üstünde mum kapanışı
                    vr5 = b5[5] / ort5 if ort5 > 0 else 0
                    red = self._red(sym, B5, x) or (None if vr5 >= self.ayar.get("hacim_k", HACIM_K) else "hacim")
                    if red == "hacim" and x.get("ses") != "regular":
                        # öncesi/sonrası seans hacmi eksik ölçülür: hacim yerine üst üste 2 kapanış seviye üstünde tutunsun
                        p["tut"] = p.get("tut", 0) + 1
                        red = None if p["tut"] >= 2 else "bekle"
                    if red == "bekle":
                        pass
                    elif red:
                        self._red_kaydet(p, red)
                        if red == "hacim":
                            p["hacimsiz"] = p.get("hacimsiz", 0) + 1
                            if p["hacimsiz"] >= 3:               # seviye hacimsiz geçilip duruyor: zayıf, plan kapanır
                                self._bitir(p, "iptal", "seviye hacimsiz geçildi (zayıf seviye)", T)
                    else:
                        c5 = b5[4]
                        p["vr"] = round(vr5, 1)
                        p["ust"] = round(100 * (b5[2] - c5) / ((b5[2] - b5[3]) or 1e-9))
                        p["tf"] = 1 if hz else 5
                        p["kurallar"] = (p.get("kurallar") or []) + ["1 dk kapanış + 2x hacim" if hz else "5 dk kapanış + 2x hacim"]
                        if c5 <= p["giris"] * 1.04:
                            a = self._aday(eng, p, c5, T, "kırılım", dip10)
                            if a:
                                out.append(a)
                            else:
                                self._bitir(p, "iptal", "stop çok uzak", T)
                        else:
                            p["st"], p["kt"] = "kaçtı", T
                            self.olay.append(("kaçtı", p))
                elif B5 and blok_sonu(T) and B5[-1][4] < p.get("iptal_alt", p["stop"]):   # 5 dk kapanış, fitil sayılmaz
                    self._bitir(p, "iptal", "fiyat kırılımdan önce stop seviyesinin altında kapandı (5 dk)", T)
                elif T - p["t"] > SURE_SN:
                    self._bitir(p, "süre", "kırılım gelmedi", T)
            elif p["st"] == "onaylı":
                kap = c if p.get("tf") == 1 else (B5[-1][4] if (B5 and blok_sonu(T)) else None)
                if T > p["ot"] and kap is not None:             # kural: karar kapanışa göre (fitil sayılmaz)
                    if kap < p["lvl"]:
                        self._bitir(p, "sahte", f"kırılımdan sonraki {p.get('tf', 5)} dk mum tekrar {p['lvl']} altına kapandı", T)
                    else:
                        self._bitir(p, "onaylı", "kırılım tuttu", T)
                        self.retest[sym] = {"p": p, "t": T}
            elif p["st"] == "kaçtı":
                if c > p["giris"] * 1.10:
                    self._bitir(p, "süre", "kaçtı ve %10'dan fazla uzaklaştı: yeni seviyeye bakılıyor", T)
                elif c < p["lvl"] * 0.99:
                    self._bitir(p, "sahte", f"kaçtıktan sonra {p['lvl']} altına döndü", T)
                elif B5 and blok_sonu(T) and B5[-1][3] <= p["giris"] * 1.02 and B5[-1][4] >= p["lvl"] and B5[-1][4] > B5[-1][1]:
                    if not self._red(sym, B5, x):
                        a = self._aday(eng, p, B5[-1][4], T, "retest", dip10)
                        if a:
                            out.append(a)
                elif T - p["kt"] > 45 * 60:
                    self._bitir(p, "süre", "kaçtı, 45 dk içinde geri çekilme gelmedi", T)
            return out
        # yeni plan
        son = next((q for q in reversed(self.gecmis) if q["sym"] == sym), None)
        if son and T - (son.get("bitis") or son["t"]) < TEKRAR_SN:
            return self._nd(sym, T, "az önce bir plan bitti, 15 dk bekleniyor")
        if sym in HARIC:
            return self._nd(sym, T, "kaldıraçlı fon / ETF: plan kurulmaz")
        if c < MIN_FIYAT:
            return self._nd(sym, T, f"fiyat {MIN_FIYAT:.2f}$ altında".replace(".", ","))
        kn = self.kanal.get(sym)
        if kn and time.time() - kn["t"] < 6 * 3600 and kn["lvl"] * 0.85 < c < kn["lvl"] * 1.005:
            g = {"ust": kn["lvl"], "tur": {"kanal"}, "dip": None, "puan": AGIRLIK["kanal"]}
            pl = self._plan_kur(sym, x, g, T, "kanal", kn["kanal"], kn.get("stop"))
            if pl:
                pl["giris"] = round(kn["lvl"], 4)
                sabit_stop_hedef(pl, pl["giris"])
                self.kanal.pop(sym, None)
                self.planlar[sym] = pl
                self.olay.append(("yeni", pl))
            return []
        ls = self.liste.get(sym)
        if ls and time.time() - ls["t"] < 12 * 3600 and ls["lvl"] * 0.85 < c < ls["lvl"] * 1.005:
            kume = [g for g in self.seviyeler(sym, tss, bars, ds, k, x) if abs(g["ust"] / ls["lvl"] - 1) <= 0.015]
            g = kume[0] if kume else {"ust": ls["lvl"], "tur": set(), "dip": None, "bant": None}
            g = dict(g, ust=ls["lvl"], tur=set(g["tur"]) | ({"acilis"} if ls.get("acilis") else {"liste"}))
            g["puan"] = sum(AGIRLIK[t] for t in g["tur"])
            pl = self._plan_kur(sym, x, g, T, "liste", None, ls.get("stop"))
            if pl:
                pl["giris"] = round(ls["lvl"], 4)
                sabit_stop_hedef(pl, pl["giris"])
                pl["liste"] = 1
                if ls.get("acilis"):
                    pl["acilis"] = 1
                self.liste.pop(sym, None)
                self.planlar[sym] = pl
                self.olay.append(("yeni", pl))
            return []
        if self.uygun and not self.uygun(sym):
            return self._nd(sym, T, "plan listesinde değil (koşan / izleme listesine girmedi)")
        ref = x.get("pc")
        if not ref:
            return self._nd(sym, T, "önceki kapanış bilinmiyor")
        hareket = (c / ref - 1) * 100
        sonen = bool(x.get("hod") and x["hod"] / ref >= 1.25)       # bugün koşup sönen hisse (WFF gibi)
        dv = x.get("ses_dv") or 0
        if (hareket < 2 and not sonen) or dv < (150_000 if x["ses"] != "regular" else 500_000):
            return self._nd(sym, T, f"hareket {hareket:+.1f}% / seans hacmi {dv / 1e3:.0f}K$ — en az +%2 ve "
                                    f"{'150K' if x['ses'] != 'regular' else '500K'}$ gerekli")
        gmax = (max(c, x.get("hod") or c) / ref - 1) * 100
        if gmax < MIN_GUN:
            return self._nd(sym, T, f"gün içinde en fazla {gmax:+.1f}% — koşan hisse için en az +%{MIN_GUN:g}")
        tum = self.seviyeler(sym, tss, bars, ds, k, x)
        kume = [g for g in tum if g["puan"] >= MIN_PUAN]
        if not kume:
            return self._nd(sym, T, "fiyatın %0,5–12 üstünde güçlü seviye yok: " +
                            (", ".join(f"{g['ust']} (puan {g['puan']:g})" for g in tum[:4]) or "hiç seviye bulunamadı"))
        g = max(kume, key=lambda g: (g["puan"], -g["ust"]))
        if "kaybedilen" in g["tur"]:
            dip30 = min((bars[tss[i]][2] for i in range(max(ds, k - 30), k + 1)), default=c)
            if c < dip30 * 1.04:
                return self._nd(sym, T, f"{g['ust']} geri alma seviyesi ama düşüş sürüyor")   # düşüş sürüyor: dönüş başlamadan geri alma planı yok
        pl = self._plan_kur(sym, x, g, T)
        if pl:
            # kural: bir sonraki dirence en az K1 (+%5) kadar yer olmalı
            ust = [q["ust"] for q in tum if q["ust"] > pl["giris"] * 1.005 and q["puan"] >= 1.5]   # gerçek direnç (yuvarlak sayı tek başına değil)
            if ust:
                nx = min(ust)
                if nx < pl["hedefler"][0]:
                    return self._nd(sym, T, f"{pl['giris']} planı: sonraki direnç {nx} çok yakın (K1 {pl['hedefler'][0]} altında)")
                pl["sonraki"] = round(nx, 4)
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
