"""Kendini ayarlayan seçici: her seans sonunda son 20 günün sonuçlarına bakıp
   1) sinyal saat penceresini (17:00–20:30 gibi) kaydırır,
   2) koşan hisselerde zarar ettiren kurguları kapatır / toparlananları açar,
   3) büyük hisselerde (ana liste) gerçekten kazandıran bir dilim çıkarsa onu açar.

Aşırı uyumu (şansa göre ayar yapmayı) önlemek için:
   · küçük örnekler sıfıra doğru çekilir (n / (n + K) büzme),
   · her değişiklik için en az örnek sayısı ister,
   · pencere kenarı bir günde en fazla 30 dk kayar (yavaş, kararlı hareket),
   · aynı ayar gün aşırı ileri geri gitmesin diye eşiklerde boşluk (histerezis) bırakır.

Saf fonksiyonlar: app.py veriyi toplar, burada karar verilir, sonuç app.py'de uygulanır.
"""

KOVA = 30            # dk: pencere 30 dakikalık dilimlerle ölçülür
SEANS = 390          # normal seans 09:30–16:00 ET = 390 dk
K_BUZ = 20           # büzme: n / (n + 20)
GUN = 20             # bakılan iş günü

P_MIN_N = 60         # pencere: içinde en az bu kadar sonuç
P_MIN_AVG = 0.05     # pencere: işlem başı en az +0,05R
P_MIN_UZ = 60        # pencere: en az 60 dk
P_FARK = 3.0         # yeni pencere eskisinden en az bu kadar (toplam büzülmüş R) iyi olmalı

K_MIN_N = 30         # kurgu kararı için en az sonuç
K_KES = -0.10        # bunun altı → kapat
K_AC = 0.05          # kapalıyken bunun üstü → aç

A_MIN_N = 150        # ana liste dilimi: en az sonuç
A_MIN_AVG = 0.05
A_GUN_ORAN = 0.6     # günlerin en az %60'ında artı
A_MIN_GUN = 5
A_MAX = 3            # en fazla 3 dilim açık
A_KAPAT_N = 100      # açık dilim bu kadar sonuçta eksiye düşerse kapanır


def kova(dk):
    return int(dk // KOVA)


def _ort(rs):
    return sum(rs) / len(rs) if rs else None


def _saat(dk):
    """Açılıştan dk → 'HH:MM' ET (gösterim app.py'de TR'ye çevrilir)."""
    t = 570 + int(dk)
    return f"{t // 60:02d}:{t % 60:02d}"


def kova_tablosu(kayit, kotu, min_conf):
    """kayit: [{dk, setup, conf, r, day}] koşan hisse, normal seans. → kova -> [r...]"""
    by = {}
    for x in kayit:
        if x["setup"] in kotu or (x.get("conf") or 0) < min_conf:
            continue
        if not (0 <= x["dk"] < SEANS):
            continue
        by.setdefault(kova(x["dk"]), []).append(x["r"])
    return by


def _skor(rs):
    n = len(rs)
    return sum(rs) * n / (n + K_BUZ) if n else 0.0


def pencere_ogren(kayit, mevcut, kotu, min_conf):
    """En iyi bitişik pencereyi bulur; mevcut pencereyi ona doğru en fazla bir kova (30 dk) kaydırır.
    Dönüş: {"yeni": (b, s), "hedef": (b, s) | None, "tablo": [...], "neden": str, "degisti": bool}"""
    by = kova_tablosu(kayit, kotu, min_conf)
    nk = SEANS // KOVA                              # 13 kova
    sk = [_skor(by.get(i, [])) for i in range(nk)]
    tablo = [{"k": i, "bas": i * KOVA, "n": len(by.get(i, [])), "avg": (round(_ort(by[i]), 3) if by.get(i) else None),
              "skor": round(sk[i], 2)} for i in range(nk)]
    en, en_skor = None, None
    for i in range(nk):
        for j in range(i + P_MIN_UZ // KOVA, nk + 1):
            rs = [r for q in range(i, j) for r in by.get(q, [])]
            if len(rs) < P_MIN_N or _ort(rs) < P_MIN_AVG:
                continue
            s = sum(sk[i:j])
            if en_skor is None or s > en_skor:
                en, en_skor = (i * KOVA, j * KOVA), s
    b0, s0 = mevcut
    cur = sum(sk[kova(b0):kova(s0)]) if s0 > b0 else 0.0
    if en is None:
        return {"yeni": (b0, s0), "hedef": None, "tablo": tablo, "degisti": False,
                "neden": "yeterli örnekli kârlı bir pencere yok; mevcut saat korunuyor"}
    if en == (b0, s0) or en_skor < cur + P_FARK:
        return {"yeni": (b0, s0), "hedef": en, "tablo": tablo, "degisti": False,
                "neden": "mevcut pencere hâlâ en iyisi (ya da fark küçük)"}

    def adim(a, h):
        return a + KOVA if h > a else a - KOVA if h < a else a
    nb, ns = adim(b0, en[0]), adim(s0, en[1])
    if ns - nb < P_MIN_UZ:
        nb, ns = b0, s0
    return {"yeni": (nb, ns), "hedef": en, "tablo": tablo, "degisti": (nb, ns) != (b0, s0),
            "neden": f"en iyi pencere {_saat(en[0])}–{_saat(en[1])} ET (toplam {en_skor:+.1f}R, mevcut {cur:+.1f}R)"}


def kurgu_ogren(kayit, pencere, kotu, min_conf):
    """Pencere içindeki koşan hisse sonuçlarıyla kurgu aç/kapat. Dönüş: (yeni_kotu, olaylar, tablo)"""
    b, s = pencere
    by = {}
    for x in kayit:
        if b <= x["dk"] < s and (x.get("conf") or 0) >= min_conf:
            by.setdefault(x["setup"], []).append(x["r"])
    yeni, olay, tablo = set(kotu), [], []
    for k, rs in sorted(by.items()):
        n, a = len(rs), _ort(rs)
        tablo.append({"k": k, "n": n, "avg": round(a, 3), "kapali": k in kotu})
        if n < K_MIN_N:
            continue
        if k not in kotu and a < K_KES:
            yeni.add(k)
            olay.append(("kapat", k, n, a))
        elif k in kotu and a > K_AC:
            yeni.discard(k)
            olay.append(("ac", k, n, a))
    return yeni, olay, tablo


def ana_dilim_ogren(kayit, mevcut, min_conf):
    """Büyük hisseler: (kurgu, 30 dk kova) dilimi. Dönüş: (yeni_liste, olaylar, en_iyi_dilimler)"""
    by, gun = {}, {}
    for x in kayit:
        if (x.get("conf") or 0) < min_conf or not (0 <= x["dk"] < SEANS):
            continue
        a = f"{x['setup']}@{kova(x['dk'])}"
        by.setdefault(a, []).append(x["r"])
        gun.setdefault(a, {}).setdefault(x.get("day") or "", []).append(x["r"])
    sira = []
    for a, rs in by.items():
        gl = [sum(v) for v in gun[a].values()]
        sira.append({"a": a, "n": len(rs), "avg": round(_ort(rs), 3), "gun": len(gl),
                     "arti_gun": round(sum(1 for g in gl if g > 0) / len(gl), 2) if gl else 0})
    sira.sort(key=lambda d: -(d["avg"] * d["n"] / (d["n"] + K_BUZ)))
    yeni, olay = list(mevcut), []
    for d in sira:
        if d["a"] in yeni and d["n"] >= A_KAPAT_N and d["avg"] < 0:
            yeni.remove(d["a"])
            olay.append(("kapat", d))
    for d in sira:
        if len(yeni) >= A_MAX:
            break
        if d["a"] not in yeni and d["n"] >= A_MIN_N and d["avg"] >= A_MIN_AVG and d["gun"] >= A_MIN_GUN \
                and d["arti_gun"] >= A_GUN_ORAN:
            yeni.append(d["a"])
            olay.append(("ac", d))
    return yeni, olay, sira[:5]


def ogren(kosan, ana, durum, min_conf):
    """Bir öğrenme turu. durum: {"pencere": [b, s], "kotu": [...], "ana": [...]}
    Dönüş: (yeni_durum, olaylar[(tur, metin_bilgi)], rapor)"""
    pen = tuple(durum.get("pencere") or (30, 240))
    kotu = set(durum.get("kotu") or [])
    olaylar = []
    p = pencere_ogren(kosan, pen, kotu, min_conf)
    if p["degisti"]:
        olaylar.append(("pencere", {"eski": pen, "yeni": p["yeni"], "hedef": p["hedef"], "neden": p["neden"]}))
    # kurgular, pencerenin gideceği en iyi dilimde ölçülür (kayarken arada kalan kötü saatler kurguyu haksız yere kapatmasın)
    yeni_kotu, k_olay, k_tab = kurgu_ogren(kosan, tuple(p["hedef"] or p["yeni"]), kotu, min_conf)
    for tur, k, n, a in k_olay:
        olaylar.append(("kurgu", {"tur": tur, "k": k, "n": n, "avg": round(a, 3)}))
    yeni_ana, a_olay, a_en = ana_dilim_ogren(ana, durum.get("ana") or [], min_conf)
    for tur, d in a_olay:
        olaylar.append(("ana", dict(d, tur=tur)))
    yeni = {"pencere": list(p["yeni"]), "kotu": sorted(yeni_kotu), "ana": yeni_ana}
    rapor = {"kova": p["tablo"], "hedef": list(p["hedef"]) if p["hedef"] else None, "pencere_neden": p["neden"],
             "kurgu": k_tab, "ana_en": a_en, "n_kosan": len(kosan), "n_ana": len(ana)}
    return yeni, olaylar, rapor
