"""
Grafik okuma modülü: destek/direnç, formasyonlar, alıcı bölgeleri.
-------------------------------------------------------------------
Hem grafik çizimi (app.py → index.html) hem de bot kararları (sinyal.py) bu modülü kullanır:
grafikte ne görüyorsan bot da onu görür.

Girdi: 5 dakikalık mumlar [(t, o, h, l, c, v), ...] eskiden yeniye (son 2-3 gün).

1) Destek / direnç: fiyatın birden fazla kez döndüğü (dönüş noktası kümeleri) seviyeler.
   Puan = dokunuş sayısı + kırılıp ters yönde test edilme (kırılan direnç → destek) + o seviyedeki hacim.
   Fiyatın üstünde en güçlü 2 direnç, altında en güçlü 2 destek döner.
2) Formasyonlar (dönüş noktaları üzerinden):
   - Çift dip (W) / çift tepe (M)
   - TOBO (ters omuz-baş-omuz) / OBO (omuz-baş-omuz)
   - Yükselen / alçalan / simetrik üçgen
   - Boğa bayrağı (sert yükseliş direği + dar konsolidasyon)
   - Düşen kama (boğa) / yükselen kama (ayı): iki kenar aynı yöne eğik ve daralıyor; hedef kamanın başladığı tepe / dip
   - Boğa / ayı flaması: sert direk + simetrik üçgen; hedef direk boyu kadar (kırılım noktasından)
   - Yutan mum (engulfing): dönüş noktasında ters mumun gövdesini tamamen yutan mum
   Her biri için: boyun çizgisi (kırılım seviyesi), hedef (formasyon yüksekliği kadar), durum (oluşuyor / kırıldı).
3) Alıcı bölgesi: yükselen mumlardaki hacmin yoğunlaştığı fiyat bandı (fiyatın altında).
4) FVG (boşluk / fair value gap): 3 mumluk dizide 1. mumun tepesi ile 3. mumun dibi arasında kalan, fiyatın
   hiç işlem görmediği boşluk. Fiyat çoğu zaman bu boşluğu doldurmaya döner; dolmamış boşluk destek/direnç gibi çalışır.
5) Trend çizgisi ve kanal: yükselen diplerden (ya da alçalan tepelerden) geçen çizgi + karşı tarafta paralel çizgi.
6) Piyasa yapısı: tepe/dip dizisi (HH-HL yükseliş, LH-LL düşüş) ve yapı kırılımı (BOS) / karakter değişimi (CHoCH).
7) Fibonacci geri çekilmesi: son sert hareketin 0,382 / 0,5 / 0,618 / 0,786 seviyeleri (0,5–0,618 "altın bölge").
8) Mum okuma: gövde kararı gösterir, uzun fitil o yönün reddedildiğini söyler.
"""


def atr(b, n=14):
    if len(b) < 2:
        return 0.0
    s = 0.0
    m = 0
    for i in range(max(1, len(b) - n), len(b)):
        h, l, pc = b[i][2], b[i][3], b[i - 1][4]
        s += max(h - l, abs(h - pc), abs(l - pc))
        m += 1
    return s / m if m else 0.0


def pivots(b, k=3, min_move=0.0):
    """Zikzak dönüş noktaları: [(i, fiyat, 'H'|'L')], tepe ve dipler sırayla değişir.
    Bir mum her iki yanında k mumdan yüksekse tepe, alçaksa dip. Ardışık aynı türde olan daha uç olanla birleşir.
    min_move: iki dönüş arası en az fiyat farkı (küçük kıpırdamaları ele)."""
    raw = []
    for i in range(k, len(b) - k):
        h, l = b[i][2], b[i][3]
        if all(h >= b[j][2] for j in range(i - k, i + k + 1) if j != i):
            raw.append((i, h, "H"))
        if all(l <= b[j][3] for j in range(i - k, i + k + 1) if j != i):
            raw.append((i, l, "L"))
    raw.sort(key=lambda x: x[0])
    out = []
    for p in raw:
        if out and out[-1][2] == p[2]:
            if (p[2] == "H" and p[1] >= out[-1][1]) or (p[2] == "L" and p[1] <= out[-1][1]):
                out[-1] = p
            continue
        if out and abs(p[1] - out[-1][1]) < min_move:
            continue
        out.append(p)
    return out


# ----------------------------------------------------------------- destek / direnç
def key_levels(b, piv, a):
    """Fiyatın döndüğü seviyeler. Dönüş: [{"p", "k": res|sup, "n", "flip", "t", "w"}]"""
    if len(b) < 20 or not piv or a <= 0:
        return []
    last = b[-1][4]
    tol = max(a * 0.6, last * 0.0015)
    pts = sorted(piv, key=lambda x: x[1])
    cl = []
    for i, p, kind in pts:
        if cl and p - cl[-1]["hi"] <= tol:
            g = cl[-1]
            g["ps"].append(p)
            g["idx"].append(i)
            g["hi"] = p
        else:
            cl.append({"ps": [p], "idx": [i], "hi": p})
    avgv = sum(x[5] for x in b) / len(b) or 1
    lv = []
    for g in cl:
        p = sum(g["ps"]) / len(g["ps"])
        n = len(g["ps"])
        # seviyede işlem gören hacim (o seviyeye değen mumlar)
        vol = sum(x[5] for x in b if x[3] - tol <= p <= x[2] + tol)
        touch_bars = sum(1 for x in b if x[3] - tol <= p <= x[2] + tol)
        vscore = (vol / touch_bars / avgv) if touch_bars else 0
        # kırılıp ters yönden test edildi mi? (önce altında kapanıp sonra üstünde kapanan ya da tersi)
        first = min(g["idx"])
        flip = _flipped(b[first:], p, tol)
        score = n + (2 if flip else 0) + min(2.0, max(0.0, vscore - 1))
        if n < 2 and not flip:
            continue
        lv.append({"p": round(p, 4), "n": n, "flip": flip, "score": round(score, 2)})
    res = sorted([x for x in lv if x["p"] > last + tol * 0.2], key=lambda x: (x["p"] - last) / (1 + x["score"]))[:2]
    sup = sorted([x for x in lv if x["p"] < last - tol * 0.2], key=lambda x: (last - x["p"]) / (1 + x["score"]))[:2]
    out = []
    for x in res:
        x["k"] = "res"
        x["t"] = "Direnç" + (" (kırılan destek)" if x["flip"] else f" ×{x['n']}")
        out.append(x)
    for x in sup:
        x["k"] = "sup"
        x["t"] = "Destek" + (" (kırılan direnç)" if x["flip"] else f" ×{x['n']}")
        out.append(x)
    if out:
        top = max(out, key=lambda x: x["score"])
        for x in out:
            x["w"] = 2 if x is top or x["score"] >= 4 else 1
    return out


def _flipped(seg, p, tol):
    """Seviye bir yönde kırılıp sonra öbür taraftan test edildi mi?"""
    state = None
    changes = 0
    for x in seg:
        c = x[4]
        s = 1 if c > p + tol * 0.5 else (-1 if c < p - tol * 0.5 else 0)
        if s == 0:
            continue
        if state is not None and s != state:
            changes += 1
        state = s
    return changes >= 1 and any(x[3] - tol <= p <= x[2] + tol for x in seg[-30:])


# ----------------------------------------------------------------- formasyonlar
def _line_at(p1, p2, i):
    (i1, y1), (i2, y2) = p1, p2
    if i2 == i1:
        return y1
    return y1 + (y2 - y1) * (i - i1) / (i2 - i1)


def _status(b, neck_at, start_i, bull):
    """Boyun çizgisi start_i'den sonra kapanışla kırıldı mı? (kırıldıysa mum indeksi)"""
    for j in range(start_i + 1, len(b)):
        lvl = neck_at(j)
        if (bull and b[j][4] > lvl) or (not bull and b[j][4] < lvl):
            return j
    return None


def patterns(b, piv, a):
    if len(b) < 30 or a <= 0:
        return []
    n = len(b)
    last_i = n - 1
    tol = a * 1.0
    out = []

    def pt(i, p):
        return [b[i][0], round(p, 4)]

    used = set()

    def free(ids):
        return len(used & set(ids)) < 2           # aynı dönüş noktalarını iki formasyonda sayma

    def add(name, bull, pts_idx, neck_fn, neck_i0, height, extra=None):
        start = pts_idx[-1][0]
        used.update(i for i, _ in pts_idx)
        bi = _status(b, neck_fn, start, bull)
        if bi is not None and last_i - bi > 12:
            return                                    # çok önce kırılmış, artık güncel değil
        neck_now = neck_fn(last_i if bi is None else bi)
        tgt = neck_now + height if bull else neck_now - height
        d = {"ad": name, "bull": bull, "pts": [pt(i, p) for i, p in pts_idx],
             "neck": [pt(neck_i0, neck_fn(neck_i0)), pt(last_i, neck_fn(last_i))],
             "neck_now": round(neck_fn(last_i), 4), "hedef": round(tgt, 4), "boy": round(height, 4),
             "durum": "kırıldı" if bi is not None else "oluşuyor", "t_kirilim": b[bi][0] if bi is not None else None,
             "t1": b[start][0], "i1": start}
        if extra:
            d.update(extra)
        out.append(d)

    P = piv[-9:]
    # TOBO / OBO: L H L H L  /  H L H L H
    for j in range(len(P) - 5, -1, -1):
        s1, n1, hd, n2, s2 = P[j:j + 5]
        if s1[2] == "L" and hd[2] == "L" and s2[2] == "L" and hd[1] < min(s1[1], s2[1]) - 0.5 * a \
                and abs(s1[1] - s2[1]) <= 2 * tol and not any(x[4] < hd[1] for x in b[s2[0]:]):
            f = (lambda i, p1=(n1[0], n1[1]), p2=(n2[0], n2[1]): _line_at(p1, p2, i))
            add("TOBO", True, [(s1[0], s1[1]), (n1[0], n1[1]), (hd[0], hd[1]), (n2[0], n2[1]), (s2[0], s2[1])],
                f, n1[0], f(hd[0]) - hd[1])
            break
        if s1[2] == "H" and hd[2] == "H" and s2[2] == "H" and hd[1] > max(s1[1], s2[1]) + 0.5 * a \
                and abs(s1[1] - s2[1]) <= 2 * tol and not any(x[4] > hd[1] for x in b[s2[0]:]):
            f = (lambda i, p1=(n1[0], n1[1]), p2=(n2[0], n2[1]): _line_at(p1, p2, i))
            add("OBO", False, [(s1[0], s1[1]), (n1[0], n1[1]), (hd[0], hd[1]), (n2[0], n2[1]), (s2[0], s2[1])],
                f, n1[0], hd[1] - f(hd[0]))
            break
    # Üçgenler: son 2 tepe + son 2 dip
    # Klasik tanım: en az 5 dokunuş (bir kenarda 3, diğerinde 2). 4 dokunuşlu yapı çift dip/tepe ile karışır.
    W = P[-5:]
    hs_all = [p for p in W if p[2] == "H"]
    ls_all = [p for p in W if p[2] == "L"]
    hs = [hs_all[0], hs_all[-1]] if len(hs_all) >= 2 else []
    ls_ = [ls_all[0], ls_all[-1]] if len(ls_all) >= 2 else []
    if len(W) == 5 and len(hs) == 2 and len(ls_) == 2:
        dh = hs[1][1] - hs[0][1]
        dl = ls_[1][1] - ls_[0][1]
        flat_h, flat_l = abs(dh) <= tol * 0.6, abs(dl) <= tol * 0.6
        up = (lambda i, p1=(hs[0][0], hs[0][1]), p2=(hs[1][0], hs[1][1]): _line_at(p1, p2, i))
        lo = (lambda i, p1=(ls_[0][0], ls_[0][1]), p2=(ls_[1][0], ls_[1][1]): _line_at(p1, p2, i))
        i0 = min(hs[0][0], ls_[0][0])
        height = up(i0) - lo(i0)
        conv = up(last_i) - lo(last_i) < height * 0.85 and up(last_i) > lo(last_i)
        pts = [(p[0], p[1]) for p in W]
        if height >= 1.5 * a and conv and free([p[0] for p in W]):
            if flat_h and dl > tol * 0.6:
                add("Yükselen üçgen", True, pts, up, hs[0][0], height, {"alt": [
                    [b[ls_[0][0]][0], round(ls_[0][1], 4)], [b[last_i][0], round(lo(last_i), 4)]]})
            elif flat_l and dh < -tol * 0.6:
                add("Alçalan üçgen", False, pts, lo, ls_[0][0], height, {"alt": [
                    [b[hs[0][0]][0], round(hs[0][1], 4)], [b[last_i][0], round(up(last_i), 4)]]})
            elif dh < -tol * 0.6 and dl > tol * 0.6:
                # simetrik: yön kırılıma göre (önce yukarı kırılırsa boğa)
                bu = _status(b, up, pts[-1][0], True)
                bd = _status(b, lo, pts[-1][0], False)
                bull = bu is not None and (bd is None or bu <= bd) or (bu is None and bd is None and b[-1][4] >= (up(last_i) + lo(last_i)) / 2)
                # flama: üçgenden hemen önce sert direk varsa hedef direk boyu kadar (AB = direk, CD = kırılımdan hedefe)
                j0 = max(0, i0 - 15)
                if bull:
                    dip = min(x[3] for x in b[j0:i0 + 1])
                    direk = hs[0][1] - dip
                else:
                    tepe = max(x[2] for x in b[j0:i0 + 1])
                    direk = tepe - ls_[0][1]
                flama = direk >= 3 * a and direk >= 1.8 * height
                ad = ("Boğa flaması" if bull else "Ayı flaması") if flama else "Simetrik üçgen"
                boy = direk if flama else height
                if bull:
                    add(ad, True, pts, up, hs[0][0], boy, {"alt": [
                        [b[ls_[0][0]][0], round(ls_[0][1], 4)], [b[last_i][0], round(lo(last_i), 4)]]})
                else:
                    add(ad, False, pts, lo, ls_[0][0], boy, {"alt": [
                        [b[hs[0][0]][0], round(hs[0][1], 4)], [b[last_i][0], round(up(last_i), 4)]]})
            elif dh < -tol * 0.6 and dl < -tol * 0.3:            # iki kenar da düşüyor; daralma (conv) yukarıda şart
                # düşen kama: tepeler diplerden hızlı düşüyor → yukarı kırılım, hedef kamanın ilk tepesi
                hedef_boy = max(hs[0][1] - up(last_i), 1.5 * a)
                add("Düşen kama", True, pts, up, hs[0][0], hedef_boy, {"alt": [
                    [b[ls_[0][0]][0], round(ls_[0][1], 4)], [b[last_i][0], round(lo(last_i), 4)]]})
            elif dl > tol * 0.6 and dh > tol * 0.3:              # iki kenar da yükseliyor ve daralıyor
                # yükselen kama: dipler tepelerden hızlı yükseliyor → aşağı kırılım, hedef kamanın ilk dibi
                hedef_boy = max(lo(last_i) - ls_[0][1], 1.5 * a)
                add("Yükselen kama", False, pts, lo, ls_[0][0], hedef_boy, {"alt": [
                    [b[hs[0][0]][0], round(hs[0][1], 4)], [b[last_i][0], round(up(last_i), 4)]]})
    # Çift dip / çift tepe: L H L  /  H L H
    for j in range(len(P) - 3, -1, -1):
        x, y, z = P[j], P[j + 1], P[j + 2]
        if z[0] - x[0] < 6:
            continue
        if not free([x[0], y[0], z[0]]):
            continue
        after = b[z[0] + 1:]
        if x[2] == "L" and y[2] == "H" and z[2] == "L" and abs(x[1] - z[1]) <= tol and y[1] - max(x[1], z[1]) >= 1.5 * a \
                and not any(c[4] < min(x[1], z[1]) - tol * 0.5 for c in after):
            neck = y[1]
            add("Çift dip", True, [(x[0], x[1]), (y[0], y[1]), (z[0], z[1])], lambda i, v=neck: v, y[0],
                neck - min(x[1], z[1]))
            break
        if x[2] == "H" and y[2] == "L" and z[2] == "H" and abs(x[1] - z[1]) <= tol and min(x[1], z[1]) - y[1] >= 1.5 * a \
                and not any(c[4] > max(x[1], z[1]) + tol * 0.5 for c in after):
            neck = y[1]
            add("Çift tepe", False, [(x[0], x[1]), (y[0], y[1]), (z[0], z[1])], lambda i, v=neck: v, y[0],
                max(x[1], z[1]) - neck)
            break
    # Boğa bayrağı: son 25 mum içinde sert direk (≥3 ATR, ≤10 mum) + ardından dar konsolidasyon
    best = None
    for top in range(max(10, n - 25), n - 3):
        lo_i = min(range(max(0, top - 10), top), key=lambda i: b[i][3])
        pole = b[top][2] - b[lo_i][3]
        if pole < 3 * a or b[top][2] < max(x[2] for x in b[max(0, top - 10):top + 1]):
            continue
        flag = b[top + 1:]
        if not (3 <= len(flag) <= 15):
            continue
        fh = max(x[2] for x in flag)
        fl = min(x[3] for x in flag)
        if fh > b[top][2] or (b[top][2] - fl) > pole * 0.5 or (fh - fl) > pole * 0.6:
            continue
        if pole < b[top][2] * 0.015 or pole < 2 * (fh - fl) or not free([lo_i, top]):
            continue
        if best is None or pole > best[1]:
            best = (top, pole, lo_i, fh, fl)
    if best:
        top, pole, lo_i, fh, fl = best
        neck = b[top][2]
        # kırılım: bayrak tepesini (direk tepesi) geçen kapanış
        add("Boğa bayrağı", True, [(lo_i, b[lo_i][3]), (top, b[top][2])], lambda i, v=neck: v, top, pole,
            {"alt": [[b[top][0], round(fl, 4)], [b[last_i][0], round(fl, 4)]]})
    # Güncellik: son dönüş noktası en fazla 40 mum önce olsun; en fazla 3 formasyon
    out = [p for p in out if last_i - p["i1"] <= 40]
    out.sort(key=lambda p: (p["durum"] != "oluşuyor", -p["i1"]))
    return out[:3]


# ----------------------------------------------------------------- alıcı bölgesi
def demand_zones(b, last):
    """Yükselen mumlardaki hacmin yoğunlaştığı fiyat bantları (fiyatın altında). [{"lo","hi","pay","t"}]"""
    seg = b[-150:]
    if len(seg) < 20:
        return []
    lo = min(x[3] for x in seg)
    hi = max(x[2] for x in seg)
    if hi <= lo:
        return []
    N = 30
    st = (hi - lo) / N
    upv = [0.0] * N
    tot = [0.0] * N
    for x in seg:
        v = x[5]
        if not v:
            continue
        i0 = min(N - 1, int((x[3] - lo) / st))
        i1 = min(N - 1, int((x[2] - lo) / st))
        per = v / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            tot[i] += per
            if x[4] >= x[1]:
                upv[i] += per
    allv = sum(tot) or 1
    thr = sorted(tot)[int(N * 0.75)]
    zones = []
    i = 0
    while i < N:
        if tot[i] >= thr and upv[i] / (tot[i] or 1) >= 0.58:
            j = i
            while j + 1 < N and tot[j + 1] >= thr * 0.8 and upv[j + 1] / (tot[j + 1] or 1) >= 0.5:
                j += 1
            zlo, zhi = lo + i * st, lo + (j + 1) * st
            if zhi < last:
                share = sum(tot[i:j + 1]) / allv
                buy = sum(upv[i:j + 1]) / (sum(tot[i:j + 1]) or 1)
                zones.append({"lo": round(zlo, 4), "hi": round(zhi, 4), "pay": round(share * 100, 1),
                              "alici": round(buy * 100), "t": "Alıcı bölgesi"})
            i = j + 1
        else:
            i += 1
    zones.sort(key=lambda z: last - z["hi"])
    return zones[:2]


# ----------------------------------------------------------------- FVG (boşluk)
def fvg(b, a, look=120):
    """Dolmamış boşluklar. Boğa FVG: mum[i].dip > mum[i-2].tepe (fiyatın altında kalırsa destek).
    Ayı FVG: mum[i].tepe < mum[i-2].dip. Dönüş: [{"lo","hi","bull","t","test"}] (en yakın 2'şer)."""
    n = len(b)
    if n < 10 or a <= 0:
        return []
    last = b[-1][4]
    out = []
    for i in range(max(2, n - look), n):
        h2, l2 = b[i - 2][2], b[i - 2][3]
        h0, l0 = b[i][2], b[i][3]
        if l0 > h2 and l0 - h2 >= 0.35 * a and b[i - 1][4] > b[i - 1][1]:
            lo, hi, bull = h2, l0, True
        elif h0 < l2 and l2 - h0 >= 0.35 * a and b[i - 1][4] < b[i - 1][1]:
            lo, hi, bull = h0, l2, False
        else:
            continue
        after = b[i + 1:]
        if bull:
            if any(x[3] <= lo for x in after):          # tamamen doldu: artık geçersiz
                continue
            test = any(x[3] <= hi for x in after)
        else:
            if any(x[2] >= hi for x in after):
                continue
            test = any(x[2] >= lo for x in after)
        out.append({"lo": round(lo, 4), "hi": round(hi, 4), "bull": bull, "t": b[i - 1][0], "test": test})
    below = sorted([z for z in out if z["bull"] and z["hi"] <= last * 1.002], key=lambda z: last - z["hi"])[:2]
    above = sorted([z for z in out if not z["bull"] and z["lo"] >= last * 0.998], key=lambda z: z["lo"] - last)[:2]
    return below + above


# ----------------------------------------------------------------- trend çizgisi / kanal
def kanal(b, piv, a):
    """Son iki yükselen dipten (ya da alçalan tepeden) geçen trend çizgisi + paralel kanal çizgisi."""
    n = len(b)
    if n < 30 or a <= 0:
        return None
    last_i = n - 1
    L = [p for p in piv if p[2] == "L"][-3:]
    H = [p for p in piv if p[2] == "H"][-3:]
    cands = []
    if len(L) >= 2 and L[-1][1] > L[-2][1] and L[-1][0] - L[-2][0] >= 6:
        p1, p2 = (L[-2][0], L[-2][1]), (L[-1][0], L[-1][1])
        f = (lambda i, p1=p1, p2=p2: _line_at(p1, p2, i))
        off = max(b[i][2] - f(i) for i in range(p1[0], n))
        cands.append(("Yükselen kanal", 1, f, off, p1[0]))
    if len(H) >= 2 and H[-1][1] < H[-2][1] and H[-1][0] - H[-2][0] >= 6:
        p1, p2 = (H[-2][0], H[-2][1]), (H[-1][0], H[-1][1])
        f = (lambda i, p1=p1, p2=p2: _line_at(p1, p2, i))
        off = min(b[i][3] - f(i) for i in range(p1[0], n))
        cands.append(("Alçalan kanal", -1, f, off, p1[0]))
    best = None
    for ad, yon, f, off, i0 in cands:
        span = last_i - i0
        if span < 10 or abs(f(last_i) - f(i0)) < 1.0 * a or abs(off) < 1.5 * a:
            continue
        line_now = f(last_i)
        other_now = line_now + off
        c = b[-1][4]
        # kırıldı mı: ana çizginin 0,5 ATR ötesinde kapanış (yükselen kanalda altında)
        kir = (c < line_now - 0.5 * a) if yon > 0 else (c > line_now + 0.5 * a)
        if any((x[4] < f(j) - 1.5 * a) if yon > 0 else (x[4] > f(j) + 1.5 * a) for j, x in enumerate(b[i0:-3], i0)):
            continue                                   # çizgi daha önce sertçe delinmiş: geçerli değil
        d = {"ad": ad, "yon": yon,
             "ana": [[b[i0][0], round(f(i0), 4)], [b[last_i][0], round(line_now, 4)]],
             "karsi": [[b[i0][0], round(f(i0) + off, 4)], [b[last_i][0], round(other_now, 4)]],
             "ana_now": round(line_now, 4), "karsi_now": round(other_now, 4), "kirildi": kir,
             "alt_now": round(min(line_now, other_now), 4), "ust_now": round(max(line_now, other_now), 4)}
        if best is None or span > best[0]:
            best = (span, d)
    return best[1] if best else None


# ----------------------------------------------------------------- piyasa yapısı
def yapi(b, piv, a):
    """HH/HL (yükseliş) · LH/LL (düşüş) dizisi ve son yapı kırılımı."""
    if len(piv) < 4:
        return {"trend": "belirsiz", "etiket": [], "kirilim": None}
    et = []
    lastH = lastL = None
    for i, p, k in piv:
        if k == "H":
            lab = "HH" if lastH is not None and p > lastH else ("LH" if lastH is not None else "H")
            lastH = p
        else:
            lab = "HL" if lastL is not None and p > lastL else ("LL" if lastL is not None else "L")
            lastL = p
        et.append([b[i][0], round(p, 4), lab, i])
    son = [e[2] for e in et[-4:]]
    up = sum(1 for x in son if x in ("HH", "HL"))
    dn = sum(1 for x in son if x in ("LH", "LL"))
    trend = "yükseliş" if up >= 3 else ("düşüş" if dn >= 3 else "karışık")
    # yapı kırılımı: son tepe/dip pivotundan sonra kapanışla geçildi mi
    kir = None
    hs = [e for e in et if e[2] in ("HH", "LH", "H")]
    ls_ = [e for e in et if e[2] in ("HL", "LL", "L")]
    n = len(b)
    if hs:
        h = hs[-1]
        j = next((j for j in range(h[3] + 1, n) if b[j][4] > h[1]), None)
        if j is not None and n - 1 - j <= 24:
            kir = {"yon": 1, "p": h[1], "t": b[j][0], "t0": h[0],
                   "tip": "CHoCH" if trend == "düşüş" or h[2] == "LH" else "BOS"}
    if ls_:
        l = ls_[-1]
        j = next((j for j in range(l[3] + 1, n) if b[j][4] < l[1]), None)
        if j is not None and n - 1 - j <= 24 and (kir is None or b[j][0] > kir["t"]):
            kir = {"yon": -1, "p": l[1], "t": b[j][0], "t0": l[0],
                   "tip": "CHoCH" if trend == "yükseliş" or l[2] == "HL" else "BOS"}
    return {"trend": trend, "etiket": [e[:3] for e in et[-8:]], "kirilim": kir,
            "son_tepe": hs[-1][1] if hs else None, "son_dip": ls_[-1][1] if ls_ else None}


# ----------------------------------------------------------------- Fibonacci
FIB_ORAN = (0.382, 0.5, 0.618, 0.786)


def fib(b, piv, a):
    """Son sert hareket (en az 4 ATR) için geri çekilme seviyeleri."""
    if len(piv) < 2 or a <= 0:
        return None
    last = b[-1][4]
    for j in range(len(piv) - 1, 0, -1):
        p0, p1 = piv[j - 1], piv[j]
        size = abs(p1[1] - p0[1])
        if size < 4 * a:
            continue
        yon = 1 if p1[2] == "H" else -1           # yukarı hareket: dipten tepeye
        lo, hi = min(p0[1], p1[1]), max(p0[1], p1[1])
        if not (lo - a <= last <= hi + a):
            return None
        lv = {str(r): round(hi - r * size if yon > 0 else lo + r * size, 4) for r in FIB_ORAN}
        return {"yon": yon, "a": [b[p0[0]][0], round(p0[1], 4)], "b": [b[p1[0]][0], round(p1[1], 4)], "lv": lv,
                "altin": [min(lv["0.5"], lv["0.618"]), max(lv["0.5"], lv["0.618"])]}
    return None


# ----------------------------------------------------------------- mum okuma
def mum(o, h, l, c):
    """Gövde ve fitil: 'güçlü yeşil' / 'güçlü kırmızı' / 'alt fitil reddi' / 'üst fitil reddi' / 'kararsız' / 'normal'."""
    rng = h - l
    if rng <= 0:
        return "kararsız"
    body = abs(c - o)
    up = h - max(o, c)
    dn = min(o, c) - l
    if body <= rng * 0.15:
        return "kararsız"
    if dn >= 2 * body and dn >= rng * 0.5:
        return "alt fitil reddi"
    if up >= 2 * body and up >= rng * 0.5:
        return "üst fitil reddi"
    if body >= rng * 0.6:
        return "güçlü yeşil" if c > o else "güçlü kırmızı"
    return "normal"


# ----------------------------------------------------------------- yutan mum (engulfing)
def yutan(b, a, look=12):
    """Son kapanmış iki mumda yutan formasyon: 'boğa' (dipte, kırmızıyı yutan yeşil) / 'ayı' (tepede, yeşili yutan
    kırmızı) / None. Yer şartı: son 'look' mumun dibine (boğa) ya da tepesine (ayı) yakın olmalı; gövde en az 0,3 ATR."""
    if len(b) < look + 2 or a <= 0:
        return None
    p, q = b[-2], b[-1]
    po, pc, qo, qc = p[1], p[4], q[1], q[4]
    if abs(qc - qo) < 0.3 * a or abs(pc - po) < 0.05 * a:
        return None
    pen = b[-look - 2:-2]
    if pc < po and qc > qo and qo <= pc and qc >= po:          # kırmızıyı yutan yeşil
        if min(p[3], q[3]) <= min(x[3] for x in pen) + 0.5 * a:
            return "boğa"
    if pc > po and qc < qo and qo >= pc and qc <= po:          # yeşili yutan kırmızı
        if max(p[2], q[2]) >= max(x[2] for x in pen) - 0.5 * a:
            return "ayı"
    return None


# ----------------------------------------------------------------- hepsi bir arada
def analyze(b):
    """b: 5 dk mumlar [(t,o,h,l,c,v)]. Dönüş: lv, pat, zones, fvg, kanal, yapi, fib, atr"""
    if len(b) < 30:
        return {"lv": [], "pat": [], "zones": [], "fvg": [], "kanal": None, "yapi": None, "fib": None, "atr": 0, "yutan": None}
    a = atr(b)
    last = b[-1][4]
    piv = pivots(b, k=3, min_move=a * 1.0)
    out = {"lv": key_levels(b, piv, a), "pat": patterns(b, piv, a), "zones": demand_zones(b, last),
           "atr": round(a, 4)}
    try:
        out["yutan"] = yutan(b, a)
    except Exception:
        out["yutan"] = None
    for k, fn in (("fvg", lambda: fvg(b, a)), ("kanal", lambda: kanal(b, piv, a)),
                  ("yapi", lambda: yapi(b, piv, a)), ("fib", lambda: fib(b, piv, a))):
        try:
            out[k] = fn()
        except Exception:
            out[k] = None
    return out


def to5m(rows):
    """1 dk mumlar [(t,o,h,l,c,v)] → 5 dk mumlar."""
    out = []
    cur = None
    for t, o, h, l, c, v in rows:
        bt = t // 300 * 300
        if cur and cur[0] == bt:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
            cur[5] += v
        else:
            if cur:
                out.append(tuple(cur))
            cur = [bt, o, h, l, c, v]
    if cur:
        out.append(tuple(cur))
    return out
