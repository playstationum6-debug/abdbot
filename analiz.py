"""
Hisse analizi ve işlem planı (Grafik sekmesindeki "Analiz ve plan" kartı).
---------------------------------------------------------------------------
Girdi: mumlar [(t, o, h, l, c, v)] — günlük (orta vade) ve 15 dakikalık (kısa vade).
Çıktı: trend özeti, RSI, ATR, destek/direnç, formasyonlar, alıcı bölgesi ve koşullu senaryolar:
  - "Kırılım": şu direncin üstünde kapanış gelirse al; stop, hedef 1/2, potansiyel %, risk/ödül
  - "Geri çekilme": trend yukarıyken desteğe / alıcı bölgesine çekilirse al
  - "Görünüm bozulur": şu seviyenin altında kapanış gelirse plan iptal
Bunlar kurallara dayalı senaryolardır, yatırım tavsiyesi değildir.
"""
import formasyon


def ema(vals, n):
    k = 2 / (n + 1)
    e = None
    out = []
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0) / n
        l += max(-d, 0) / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def _r(x):
    if x is None:
        return None
    return round(x, 4) if x < 1 else round(x, 2)


def _pct(a, b):
    return round((b / a - 1) * 100, 1) if a else None


def _swing_levels(b, last, a):
    """Seviye yoksa yedek: son 20 mumun tepesi / dibi."""
    seg = b[-20:]
    hi = max(x[2] for x in seg)
    lo = min(x[3] for x in seg)
    res = hi if hi > last + a * 0.2 else None
    sup = lo if lo < last - a * 0.2 else None
    return res, sup


def plan(b, kind):
    """kind: 'gun' (günlük, orta vade) | 'dk15' (15 dk, kısa vade)"""
    if len(b) < 40:
        return {"yok": "Yeterli veri yok"}
    fx = formasyon.analyze(b[-260:])
    a = fx["atr"] or formasyon.atr(b)
    closes = [x[4] for x in b]
    last = closes[-1]
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    r = rsi(closes)
    up = last > e20 > e50 and (e200 is None or last > e200)
    down = last < e20 < e50 and (e200 is None or last < e200)
    vade = "orta vade (günlük)" if kind == "gun" else "kısa vade (15 dk)"
    birim = "günlük" if kind == "gun" else "15 dakikalık"

    # trend cümlesi
    if up:
        trend = f"Yükseliş trendi: fiyat {birim} 20 ve 50{' ve 200' if e200 else ''} ortalamanın üzerinde."
    elif down:
        trend = f"Düşüş trendi: fiyat {birim} 20 ve 50{' ve 200' if e200 else ''} ortalamanın altında."
    else:
        trend = f"Yatay / kararsız: fiyat {birim} ortalamaların arasında gidip geliyor."
    rsi_t = None
    if r is not None:
        rsi_t = ("aşırı alım bölgesinde, geri çekilme riski var" if r >= 70 else
                 "aşırı satım bölgesinde, tepki gelebilir" if r <= 30 else
                 "güçlü tarafta" if r >= 55 else "zayıf tarafta" if r <= 45 else "nötr")

    lv = fx["lv"]
    res_l = sorted([x["p"] for x in lv if x["k"] == "res"])
    sup_l = sorted([x["p"] for x in lv if x["k"] == "sup"], reverse=True)
    sw_res, sw_sup = _swing_levels(b, last, a)
    res = res_l[0] if res_l else sw_res
    res2 = res_l[1] if len(res_l) > 1 else None
    sup = sup_l[0] if sup_l else sw_sup
    zone = fx["zones"][0] if fx["zones"] else None
    bull_p = next((p for p in fx["pat"] if p["bull"] and p["durum"] == "oluşuyor"), None)
    bear_p = next((p for p in fx["pat"] if not p["bull"] and p["durum"] == "oluşuyor"), None)

    sen = []
    # 1) Kırılım senaryosu
    if res:
        trig = res * 1.003
        base = (sup - 0.25 * a) if sup and trig - sup <= 3 * a else trig - 1.5 * a
        stop = min(base, trig - 0.8 * a)
        risk = trig - stop
        h1 = res2 if res2 and res2 - trig >= 1.2 * risk else trig + 2 * risk
        h2 = bull_p["hedef"] if bull_p and bull_p["hedef"] > h1 else trig + 3 * risk
        sen.append({"ad": "Kırılım alımı", "kosul": f"{_r(res)} direncinin üstünde {birim} kapanış gelirse",
                    "giris": _r(trig), "stop": _r(stop), "h1": _r(h1), "h2": _r(h2),
                    "pot1": _pct(trig, h1), "pot2": _pct(trig, h2), "rr": round((h1 - trig) / risk, 1) if risk else None,
                    "not": (f"{bull_p['ad']} formasyonu da bu yönü destekliyor." if bull_p else
                            "Kırılım mumunda hacmin ortalamanın üstünde olması önemli.")})
    # 2) Geri çekilme senaryosu (trend yukarıyken)
    pb = zone["hi"] if zone and zone["hi"] < last else (sup if sup else None)
    if pb and not down and last - pb <= 4 * a:
        lo = zone["lo"] if zone and zone["hi"] == pb else pb
        lo = max(lo, pb - 1.0 * a)               # çok geniş bölgede stop çok uzağa düşmesin
        stop = min(lo - 0.5 * a, pb - 1.0 * a)   # en az 1 ATR: gürültüde patlamasın
        risk = pb - stop
        h1 = res if res and res > pb + 0.3 * risk else pb + 2 * risk
        h2 = res2 if res2 and res2 > h1 else max(h1 + risk, pb + 3 * risk)
        rr = round((h1 - pb) / risk, 1) if risk else None
        sen.append({"ad": "Geri çekilmede alım", "kosul": (f"fiyat {_r(lo)}–{_r(pb)} alıcı bölgesine çekilip yeşil mumla dönerse"
                                                           if zone and zone["hi"] == pb else
                                                           f"fiyat {_r(pb)} desteğine çekilip tutunursa"),
                    "giris": _r(pb), "stop": _r(stop), "h1": _r(h1), "h2": _r(h2),
                    "pot1": _pct(pb, h1), "pot2": _pct(pb, h2), "rr": rr,
                    "not": ("Risk/ödül zayıf: ilk hedef stoptan daha yakın. " if rr is not None and rr < 1 else "") +
                           ("Trend yukarı, geri çekilmeler alım fırsatı olabilir." if up else
                            "Trend net değil; dönüş mumu ve hacim teyidi beklenmeli.")})
    # 3) Görünüm bozulur
    bozul = (sup - 0.25 * a) if sup else (last - 2 * a)
    uyar = None
    if bear_p:
        uyar = f"{bear_p['ad']} oluşuyor: {_r(bear_p['neck_now'])} altında kapanış gelirse hedef {_r(bear_p['hedef'])}."
    if r is not None and r >= 75:
        uyar = (uyar + " " if uyar else "") + "RSI çok yüksek, kovalamak riskli."

    ozet = f"{trend} RSI {r:.0f}, {rsi_t}." if r is not None else trend
    return {"vade": vade, "son": _r(last), "atr": _r(a), "atr_pct": round(a / last * 100, 2) if last else None,
            "trend": "yukarı" if up else "aşağı" if down else "yatay", "ozet": ozet, "rsi": round(r) if r else None,
            "ema20": _r(e20), "ema50": _r(e50), "ema200": _r(e200),
            "dirençler": [_r(x) for x in res_l] or ([_r(sw_res)] if sw_res else []),
            "destekler": [_r(x) for x in sup_l] or ([_r(sw_sup)] if sw_sup else []),
            "formasyon": [{"ad": p["ad"], "bull": p["bull"], "durum": p["durum"], "neck": p["neck_now"], "hedef": p["hedef"]}
                          for p in fx["pat"]],
            "bolge": zone, "senaryo": sen, "bozulur": _r(bozul), "uyari": uyar}


def full(daily, m15):
    """daily, m15: [(t,o,h,l,c,v)] → iki vade için plan."""
    out = {"gun": plan(daily, "gun") if daily else {"yok": "Günlük veri alınamadı"},
           "dk15": plan(m15, "dk15") if m15 else {"yok": "15 dakikalık veri alınamadı"}}
    if daily and len(daily) >= 2:
        hi52 = max(x[2] for x in daily[-252:])
        lo52 = min(x[3] for x in daily[-252:])
        last = daily[-1][4]
        out["yil"] = {"tepe": _r(hi52), "dip": _r(lo52), "tepeye": _pct(last, hi52), "dipten": _pct(lo52, last)}
    return out
