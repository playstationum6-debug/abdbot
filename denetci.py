"""
Karar Denetçisi — birbirinden bağımsız 5 kontrol, tek karar
------------------------------------------------------------
Beş ayrı yapay zekâ konuşmuyor: her modül sinyale kendi açısından bakan kural tabanlı bir kontroldür.
Her biri şu kararlardan birini verir:  olumlu · nötr · olumsuz · küçült (lot) · veto
Denetçi bunları birleştirir:
    bir VETO                     → PAS GEÇ
    Fırsat Avcısı olumsuz         → PAS GEÇ (model bu sinyali zayıf dilimde görüyor)
    2+ olumsuz                    → PAS GEÇ
    eksik onay (hacim / piyasa)   → BEKLE  (bot fiyat onayını BEKLE_SN boyunca bekler)
    hepsi tamam                   → AL     (küçült diyen modüller lotu azaltır)
Oy çokluğu tek başına AL demek değildir.

Modül karnesi: her modülün kararı sinyalle birlikte kaydedilir; sinyal sonuçlanınca
"ayırt gücü" = olumlu dediklerinin ort. R'si − olumsuz dediklerinin ort. R'si ölçülür.
Geçmiş test sinyalleri de aynı kurallarla değerlendirilir, böylece karne ilk günden doludur.
Modüller aynı fiyat verisine baktığı için tam bağımsız değildir; karne bunu da gösterir.
"""
import time

BEKLE_SN = 600            # BEKLE kararında fiyat onayı için en fazla bekleme
ONAY_R = 0.10             # onay: fiyat girişin 0,1R ötesine geçmeli (stopa değmeden)
SPREAD_KUCULT = 0.6       # % spread: üstü lotu yarıya indirir
SPREAD_VETO = 3.0         # % spread: üstü işlem açtırmaz (ücretsiz IEX kotası geniş görünebilir, temkinli eşik)
MIN_KARNE = 30            # karnede bir modül satırı için en az sonuç
# 💎 Süper Fırsat: denetçi AL + model en iyi %3 + Fırsat/Teknik/Piyasa üçü de olumlu + uzun yönlü.
# Geçmiş testte (son %30, görülmemiş veri) günde en fazla 3 sınırıyla: 9 sinyal, 6 kazanç, ort. +0,85R.
# Örnek küçük: canlı karnede ort. R eksiye düşerse eşik kendiliğinden %1'e sıkılaşır (app.py).
SUPER = {"pct": 0.97}
SUPER_GUN = 3             # günde en fazla süper fırsat
SUPER_MOD = ("firsat", "teknik", "piyasa")

MODULLER = [("firsat", "Fırsat Avcısı"), ("teknik", "Teknik Analist"), ("haber", "Haber Analisti"),
            ("risk", "Risk Yöneticisi"), ("piyasa", "Piyasa Analisti")]
MOD_AD = dict(MODULLER)
KARAR_AD = {"AL": "AL", "BEKLE": "BEKLE", "PAS": "PAS GEÇ"}


def _m(k, neden, lot=1.0, eksik=None):
    d = {"k": k, "n": neden}
    if lot != 1.0:
        d["lot"] = lot
    if eksik:
        d["eksik"] = eksik
    return d


# ------------------------------------------------------------------ modüller
def m_firsat(sig, ctx):
    """Fırsat Avcısı: öğrenme modeline göre bu sinyal ne kadar iyi bir aday?"""
    p, q = ctx.get("model") or (None, None)
    if q is not None:
        sira = round(100 * q)
        if q >= 1 - ctx.get("secici", 0.35):
            return _m("olumlu", f"güçlü aday · model sırası {sira}/100, beklenen {p:+.2f}R")
        if q < 0.35:
            return _m("olumsuz", f"zayıf aday · model sırası {sira}/100, beklenen {p:+.2f}R")
        return _m("nötr", f"orta aday · model sırası {sira}/100")
    c = sig.get("conf0", sig.get("conf", 0))
    if c >= 70:
        return _m("olumlu", f"kural puanı {c} (model henüz öğreniyor)")
    if c < 55:
        return _m("olumsuz", f"kural puanı {c} düşük")
    return _m("nötr", f"kural puanı {c}")


def m_teknik(sig, ctx):
    """Teknik Analist: hacim, VWAP, kısa trend, EMA, sinyal mumu ve OBV."""
    f = sig.get("f") or {}
    d = sig.get("dir", 1)
    art, eks, nd = 0, 0, []
    vr = f.get("volr")
    if vr in ("3-5", "5+"):
        art += 1
        nd.append(f"hacim {vr}×")
    elif vr == "<2":
        eks += 1
    if f.get("vwap") == "lehte":
        art += 1
        nd.append("VWAP lehte")
    elif f.get("vwap") == "aleyhte":
        eks += 1
        nd.append("VWAP aleyhte")
    if f.get("trend") == "uyumlu":
        art += 1
    else:
        eks += 1
    if f.get("ema50") == "uyumlu":
        art += 1
    elif f.get("ema50") == "ters":
        eks += 1
        nd.append("EMA 20/50 ters")
    mum = f.get("mum") or ""
    if (d > 0 and "güçlü yeşil" in mum) or (d < 0 and "güçlü kırmızı" in mum):
        art += 1
        nd.append("güçlü mum")
    elif "kararsız" in mum:
        eks += 1
    if f.get("obv") == "lehte":
        art += 1
    elif f.get("obv") == "aleyhte":
        eks += 1
    neden = ", ".join(nd[:3]) or "göstergeler karışık"
    hacimsiz = vr == "<2"
    if art >= 4 and eks <= 1:
        return _m("olumlu", neden)
    if eks >= 4:
        return _m("olumsuz", neden)
    if hacimsiz and art >= 2:
        return _m("nötr", neden + " · hacim onayı yok", eksik="hacimli onay")
    return _m("nötr", neden)


def m_haber(sig, ctx):
    """Haber Analisti: haber yönü, yapay zeka puanı, SEC bildirimi (seyreltme = veto)."""
    f = sig.get("f") or {}
    sec, hb, ai = f.get("sec"), f.get("haber"), f.get("ai")
    if sec == "seyreltme":
        return _m("veto", "SEC: hisse ihracı / seyreltme")
    if hb == "kötü" or ai == "−30 ve altı":
        return _m("veto" if sig.get("dir", 1) > 0 else "olumlu", "olumsuz haber")
    if hb == "iyi" or ai == "+70 ve üstü":
        return _m("olumlu", "olumlu haber" + (" · YZ puanı yüksek" if ai == "+70 ve üstü" else ""))
    if f.get("bilanco") not in (None, "yok"):
        return _m("küçült", f"bilanço günü ({f.get('bilanco')})", lot=0.5)
    if hb in (None, "yok"):
        return _m("nötr", "yeni haber yok")
    return _m("nötr", "haber nötr")


def m_risk(sig, ctx):
    """Risk Yöneticisi: spread, hedefe yer, stop mesafesi, günlük zarar hakkı."""
    f = sig.get("f") or {}
    sp = ctx.get("spread")
    kalan, risk_usd = ctx.get("kalan"), ctx.get("risk_usd")
    if kalan is not None and risk_usd and kalan < risk_usd * 0.5:
        return _m("veto", f"günlük zarar hakkı {kalan:.0f} $ kaldı")
    if sp is not None and sp >= SPREAD_VETO:
        return _m("veto", f"spread %{sp:.1f} çok yüksek, likidite yok")
    e, s = sig.get("e"), sig.get("s")
    rp = abs(e - s) / e * 100 if e and s else None
    if sp is not None and sp >= SPREAD_KUCULT:
        return _m("küçült", f"spread %{sp:.1f} yüksek → lot yarıya, limit emir", lot=0.5)
    if rp is not None and rp > 6:
        return _m("küçült", f"stop %{rp:.1f} uzak → lot küçük", lot=0.6)
    if f.get("tip") == "koşan küçük hisse":
        return _m("küçült", "koşan küçük hisse: kayma yüksek", lot=0.7)
    if f.get("room") == "<1R":   # testte ayırt gücü yok (−0,01R): bilgi amaçlı, karar etkilemez
        return _m("nötr", "hedefe yer 1R'den az (direnç yakın)")
    return _m("olumlu", ("spread %{:.2f} · ".format(sp) if sp is not None else "") +
              (f"stop %{rp:.1f}" if rp is not None else "risk normal"))


def m_piyasa(sig, ctx):
    """Piyasa Analisti: piyasa havası, seans, günün saati, SPY yönü."""
    f = sig.get("f") or {}
    hava, tod, spy = f.get("hava") or ctx.get("hava"), f.get("tod"), f.get("spy")
    if hava and "riskli" in str(hava).lower():
        return _m("olumsuz", "piyasa havası riskli", eksik="piyasa desteği")
    if tod in ("Piyasa öncesi", "Piyasa sonrası"):
        return _m("olumsuz", f"{tod.lower()}: likidite zayıf", eksik="normal seans")
    if tod == "ilk 30 dk":
        return _m("olumsuz", "açılışın ilk 30 dakikası oynak", eksik="açılışın oturması")
    if tod in ("sabah", "öğle"):
        return _m("olumlu", f"{tod} saatleri · SPY {spy or '—'}")
    return _m("nötr", f"{tod or 'seans'} · SPY {spy or '—'}")


MOD_FN = {"firsat": m_firsat, "teknik": m_teknik, "haber": m_haber, "risk": m_risk, "piyasa": m_piyasa}


def degerlendir(sig, ctx=None):
    """Sinyal için denetçi kararı: {"karar","neden","eksik","lot","mod":{k:{k,n,...}},"t"}"""
    ctx = ctx or {}
    mod = {}
    for k, fn in MOD_FN.items():
        try:
            mod[k] = fn(sig, ctx)
        except Exception as e:  # bir modül hata verirse karar bozulmasın
            mod[k] = _m("nötr", f"hata: {str(e)[:40]}")
    vetolar = [k for k, v in mod.items() if v["k"] == "veto"]
    olumsuz = [k for k, v in mod.items() if v["k"] == "olumsuz"]
    eksik = [v["eksik"] for v in mod.values() if v.get("eksik")]
    lot = 1.0
    for v in mod.values():
        lot *= v.get("lot", 1.0)
    lot = max(0.25, min(1.0, lot))
    if vetolar:
        karar, neden = "PAS", f"{MOD_AD[vetolar[0]]} VETO: {mod[vetolar[0]]['n']}"
    elif mod["firsat"]["k"] == "olumsuz":
        karar, neden = "PAS", f"Fırsat Avcısı: {mod['firsat']['n']}"
    elif len(olumsuz) >= 2:
        karar, neden = "PAS", "olumsuz: " + ", ".join(MOD_AD[k] for k in olumsuz)
    elif eksik or olumsuz:
        karar = "BEKLE"
        neden = "eksik onay: " + (", ".join(dict.fromkeys(eksik)) if eksik else MOD_AD[olumsuz[0]].lower())
    else:
        karar, neden = "AL", "bütün kontroller tamam"
    say = {x: sum(1 for v in mod.values() if v["k"] == x) for x in ("olumlu", "nötr", "olumsuz", "küçült", "veto")}
    q = (ctx.get("model") or (None, None))[1]
    out = {"karar": karar, "neden": neden, "eksik": list(dict.fromkeys(eksik)), "lot": round(lot, 2),
           "mod": mod, "say": say, "t": int(time.time())}
    if q is not None:
        out["q"] = round(q, 3)
    out["sinif"] = super_sinif(out, sig)
    return out


def super_sinif(den, sig):
    """Süper Fırsat sınıfı mı (günlük sınır hariç)?"""
    q = den.get("q")
    return bool(den.get("karar") == "AL" and q is not None and q >= SUPER["pct"] and sig.get("dir", 1) > 0
                and all((den.get("mod") or {}).get(k, {}).get("k") == "olumlu" for k in SUPER_MOD))


def kisa(den):
    """Telegram / akış için tek satırlık özet."""
    if not den:
        return ""
    return f"{KARAR_AD.get(den['karar'], den['karar'])} · {den['neden']}"


def tg_satirlari(den):
    """Telegram mesajı için modül satırları (HTML güvenli metin, işaretli)."""
    if not den:
        return []
    isaret = {"olumlu": "✅", "nötr": "▫️", "olumsuz": "❌", "küçült": "⚠️", "veto": "⛔"}
    out = []
    for k, ad in MODULLER:
        v = den["mod"].get(k)
        if v:
            n = str(v["n"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            out.append(f"{isaret.get(v['k'], '•')} {ad}: {n}")
    return out


# ------------------------------------------------------------------ karne
def karne(signals, ctx_fn=None):
    """Modül karnesi: her modülün ayırt gücü ve denetçi kararlarının gerçek sonuçları.
    signals: sonuçlanmış sinyaller (canlı + geçmiş test). Kayıtlı 'den' yoksa kurallarla yeniden değerlendirilir."""
    done = [s for s in signals if s.get("r") is not None and s.get("st") in ("hedef", "stop", "süre")
            and not s.get("dis")]
    agg = {k: {"olumlu": [0, 0.0], "olumsuz": [0, 0.0], "nötr": [0, 0.0]} for k, _ in MODULLER}
    kar = {"AL": [0, 0.0, 0], "BEKLE": [0, 0.0, 0], "PAS": [0, 0.0, 0], "SUPER": [0, 0.0, 0]}
    for s in done:
        den = s.get("den")
        if not den:
            ctx = ctx_fn(s) if ctx_fn else {}
            den = degerlendir(s, ctx)
        r = s["r"]
        for k, v in den["mod"].items():
            kk = v["k"]
            kk = "olumsuz" if kk == "veto" else ("olumlu" if kk == "küçült" else kk)
            if kk in agg.get(k, {}):
                a = agg[k][kk]
                a[0] += 1
                a[1] += r
        kr = kar[den["karar"]]
        kr[0] += 1
        kr[1] += r
        kr[2] += 1 if r > 0 else 0
        if den.get("sinif") if "sinif" in den else super_sinif(den, s):
            ks = kar["SUPER"]
            ks[0] += 1
            ks[1] += r
            ks[2] += 1 if r > 0 else 0
    ort = lambda a: round(a[1] / a[0], 3) if a[0] else None
    rows = []
    for k, ad in MODULLER:
        a = agg[k]
        o, x = ort(a["olumlu"]), ort(a["olumsuz"])
        guc = round(o - x, 3) if (o is not None and x is not None and a["olumlu"][0] >= MIN_KARNE
                                  and a["olumsuz"][0] >= MIN_KARNE) else None
        rows.append({"k": k, "ad": ad, "olumlu": o, "olumlu_n": a["olumlu"][0], "olumsuz": x,
                     "olumsuz_n": a["olumsuz"][0], "notr": ort(a["nötr"]), "notr_n": a["nötr"][0], "guc": guc})
    rows.sort(key=lambda r: -(r["guc"] if r["guc"] is not None else -9))
    kararlar = {k: {"n": v[0], "avg": round(v[1] / v[0], 3) if v[0] else None,
                    "wr": round(100 * v[2] / v[0]) if v[0] else None} for k, v in kar.items()}
    return {"moduller": rows, "kararlar": kararlar, "n": len(done), "t": int(time.time())}


# ------------------------------------------------------------------ gerçek performans
def performans(pozisyonlar, slip_of=None):
    """Botun kapanan işlemlerinden: beklenen getiri, kâr faktörü, düşüş, ort. kazanç/kayıp, maliyet, strateji bazında."""
    cl = sorted([p for p in pozisyonlar if p.get("st") == "kapandı" and p.get("pnl") is not None],
                key=lambda p: p.get("xt") or 0)
    if not cl:
        return {"n": 0}
    kaz = [p["pnl"] for p in cl if p["pnl"] > 0]
    kay = [p["pnl"] for p in cl if p["pnl"] <= 0]
    rs = [p["r"] for p in cl if p.get("r") is not None]
    toplam_kaz, toplam_kay = sum(kaz), -sum(kay)
    maliyet = 0.0
    for p in cl:
        tutar = abs(p.get("notional") or 0)
        sl = slip_of(p) if slip_of else (0.0003 if p.get("ses") == "regular" else 0.001)
        maliyet += tutar * sl * 2
    cum = peak = mdd = 0.0
    for p in cl:
        cum += p["pnl"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    st = {}
    for p in cl:
        x = st.setdefault(p.get("setup") or "?", {"k": p.get("setup") or "?", "n": 0, "pnl": 0.0, "w": 0, "rs": 0.0,
                                                   "kaz": 0.0, "kay": 0.0})
        x["n"] += 1
        x["pnl"] += p["pnl"]
        x["w"] += 1 if p["pnl"] > 0 else 0
        x["rs"] += p.get("r") or 0
        if p["pnl"] > 0:
            x["kaz"] += p["pnl"]
        else:
            x["kay"] -= p["pnl"]
    strat = [{"k": x["k"], "n": x["n"], "pnl": round(x["pnl"], 2), "wr": round(100 * x["w"] / x["n"]),
              "avg_r": round(x["rs"] / x["n"], 2), "pf": round(x["kaz"] / x["kay"], 2) if x["kay"] else None}
             for x in st.values()]
    strat.sort(key=lambda x: -x["pnl"])
    return {"n": len(cl), "beklenen_r": round(sum(rs) / len(rs), 3) if rs else None,
            "beklenen_usd": round(sum(p["pnl"] for p in cl) / len(cl), 2),
            "kar_faktoru": round(toplam_kaz / toplam_kay, 2) if toplam_kay else None,
            "mdd": round(mdd, 2), "ort_kazanc": round(toplam_kaz / len(kaz), 2) if kaz else None,
            "ort_kayip": round(-toplam_kay / len(kay), 2) if kay else None,
            "kazanma": round(100 * len(kaz) / len(cl)), "maliyet_islem": round(maliyet / len(cl), 2),
            "maliyet_toplam": round(maliyet, 2), "strateji": strat[:12]}
