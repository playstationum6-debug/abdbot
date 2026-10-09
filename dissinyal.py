"""
Dış sinyal takibi — Telegram kanallarından İLETİLEN (forward) sinyal mesajları
------------------------------------------------------------------------------
Kullanım: bir kanalda sinyal görünce mesajı kendi botuna (özelden ya da yönetim kanalına) ilet.
Bot mesajı okur:  hisse kodu, yön (AL/SAT), varsa giriş / stop / hedef, mesajın KANALDA paylaşıldığı saat.
Sonra sinyali gölgede takip eder (dakikalık veriyle): paylaşımdan sonraki ilk dakikada piyasadan girilseydi
ne olurdu? Stop mu önce geldi, hedef mi? Kayma dahil net R hesaplanır.

Bot bu sinyallerle İŞLEM AÇMAZ. Sadece ölçer ve öğrenir:
  - kanalın gerçek karnesi (tutma oranı, ortalama R, paylaşım anında fiyat ne kadar koşmuştu)
  - sonuçlar öğrenme modülüne "Telegram kanalı" kurgusu olarak eklenir (modelin kendi sinyallerini bozmaz)
  - hisse, seans dışı tarayıcıya aday olarak eklenir
Sınır: Yahoo dakikalık verisi son ~7 günü kapsar; daha eski mesajlar ölçülemez.
"""
import re
import time

SLIP = 0.004          # küçük/koşan hisse varsayımı: her yönde %0,4 kayma
VARS_STOP = 0.05      # stop verilmemişse %5 varsayılır
SURE = 2 * 86400      # en fazla 2 gün takip; sonra son fiyattan kapanır
MAX_KAYIT = 400

_DUR = {"AL", "SAT", "BUY", "SELL", "LONG", "SHORT", "TP", "SL", "STOP", "HEDEF", "USD", "ABD", "NASDAQ", "NYSE",
        "VIP", "ETF", "CEO", "FDA", "SEC", "AI", "IPO", "ATH", "EPS", "PM", "AH", "TR", "OK", "NOT", "VE", "GİRİŞ",
        "GIRIS", "HİSSE", "HISSE", "KAR", "KÂR", "ZARAR", "AMEX", "OTC", "DİKKAT", "DIKKAT", "YTD", "RSI", "MACD",
        "EMA", "SMA", "VWAP", "THE", "AND", "FOR", "NEW", "NOW", "TP1", "TP2", "TP3", "T1", "T2", "T3", "BU", "GÜN",
        "ALIM", "ALIŞ", "SATIŞ", "PRE", "POST", "MARKET", "DAY", "UP", "ON", "IN", "AT", "TO", "IS", "IT", "A", "I"}
_SAYI = r"\$?\s*(\d+(?:[.,]\d+)?)"
_RE_TAG = re.compile(r"[$#]([A-Za-z]{1,5})\b")
_RE_KELIME = re.compile(r"\b([A-Z]{2,5})\b")
_RE_GIRIS = re.compile(r"(giri[şs]|al[ıi][şs]|al[ıi]m|entry|buy\s*zone|maliyet|fiyat)\D{0,14}?" + _SAYI, re.I)
_RE_STOP = re.compile(r"(stop|\bsl\b|zarar\s*kes)\D{0,14}?" + _SAYI, re.I)
_RE_HEDEF = re.compile(r"(hedef\w*|\btp\d?\b|target\w*|\bt\d\b)\s*[:=]?\s*\D{0,10}?" + _SAYI, re.I)
_RE_SAT = re.compile(r"\b(sat|sell|short|açığa|aciga)\b", re.I)
_RE_CIKIS = re.compile(r"(kâr\s*al|kar\s*al|sattık|sattik|kapat|çıkış|cikis|stop\s*oldu|hedef\s*geldi|tp\s*geldi|"
                       r"vurdu|bitti|realiz)", re.I)


def _f(x):
    try:
        return float(str(x).replace(",", "."))
    except ValueError:
        return None


_RE_SINYAL = re.compile(r"\b(al|alış|alis|alım|alim|buy|long|sat|sell|short|giriş|giris|entry|hedef\w*|target\w*|"
                        r"tp\d?|stop|sl)\b", re.I)


def coz(text, siki=False):
    """Mesajı çöz: {'sym','dir','giris','stop','hedef','cikis'} ya da None.
    siki=True (otomatik okuma): mesajda bir sinyal kelimesi yoksa (sadece yorum/haber) sinyal sayılmaz."""
    if not text:
        return None
    if siki and not _RE_SINYAL.search(text):
        return None
    syms = [s.upper() for s in _RE_TAG.findall(text) if s.upper() not in _DUR]
    if not syms:
        syms = [s for s in _RE_KELIME.findall(text) if s not in _DUR]
    if not syms:
        return None
    d = -1 if _RE_SAT.search(text) and not re.search(r"\b(al|buy|long)\b", text, re.I) else 1
    g = _RE_GIRIS.search(text)
    s = _RE_STOP.search(text)
    h = _RE_HEDEF.findall(text)
    giris = _f(g.group(2)) if g else None
    stop = _f(s.group(2)) if s else None
    hedefler = [x for x in (_f(m[1]) for m in h) if x]
    hedef = hedefler[0] if hedefler else None
    if giris and stop and (stop - giris) * d >= 0:      # stop yanlış tarafta: yok say
        stop = None
    if giris and hedef and (hedef - giris) * d <= 0:
        hedef = None
    return {"sym": syms[0], "dir": d, "giris": giris, "stop": stop, "hedef": hedef,
            "hedefler": hedefler[:4], "cikis": bool(_RE_CIKIS.search(text)) and not g}


class DisSinyal:
    def __init__(self):
        self.st = {"items": []}       # app, backup.data["dis"] ile değiştirir
        self.ver = 0
        self.status = "bekliyor"

    @property
    def items(self):
        return self.st.setdefault("items", [])

    def ekle(self, msg, ses_of=None):
        """İletilen Telegram mesajını kaydet. Döner: (kayıt ya da None, açıklama)."""
        text = msg.get("text") or msg.get("caption") or ""
        o = msg.get("forward_origin") or {}
        ch = o.get("chat") or msg.get("forward_from_chat") or {}
        kanal = ch.get("username") or ch.get("title") or (o.get("sender_user") or {}).get("username") \
            or o.get("sender_user_name") or (msg.get("forward_from") or {}).get("username") or "bilinmeyen"
        t_post = int(o.get("date") or msg.get("forward_date") or msg.get("date") or time.time())
        mid = o.get("message_id") or msg.get("forward_from_message_id")
        c = coz(text, siki=bool(msg.get("_oto")))
        if not c:
            return None, "Mesajda hisse kodu bulamadım. $ABCD ya da #ABCD gibi yazılmış bir sinyal iletmeyi dene."
        key = f"{kanal}-{mid}-{c['sym']}" if mid else f"{kanal}-{t_post}-{c['sym']}"
        if any(x["id"] == key for x in self.items):
            return None, f"Bu mesaj zaten kayıtlı ({c['sym']})."
        if c["cikis"]:
            return None, (f"Bu bir çıkış/sonuç mesajı gibi görünüyor ({c['sym']}); yeni sinyal olarak kaydetmedim. "
                          f"Bot, kanalın verdiği sinyalin sonucunu kendi ölçüyor.")
        it = {"id": key, "kanal": str(kanal)[:40], "sym": c["sym"], "dir": c["dir"], "t": t_post,
              "giris_k": c["giris"], "stop_k": c["stop"], "hedef_k": c["hedef"], "hedefler": c["hedefler"],
              "metin": text[:300], "st": "bekliyor", "ses": ses_of(t_post) if ses_of else None,
              "e": None, "s": None, "h": None, "et": None, "x": None, "xt": None, "r": None,
              "mfe": None, "mae": None, "once": None, "gec": None, "not": [], "eklendi": int(time.time())}
        if time.time() - t_post > 6.5 * 86400:
            it["st"] = "veri yok"
            it["not"].append("mesaj 7 günden eski: dakikalık veri yok, ölçülemez")
        self.items.append(it)
        del self.items[:-MAX_KAYIT]
        self.ver += 1
        return it, ""

    def acik(self):
        return [x for x in self.items if x["st"] in ("bekliyor", "açık")]

    def degerlendir(self, it, bars, now=None):
        """bars: {ts: [o,h,l,c,v,...]} dakikalık. Kaydı günceller, değişti mi döner."""
        now = now or time.time()
        if not bars:
            if now - it["t"] > 3 * 3600 and it["st"] == "bekliyor":
                it["st"] = "veri yok"
                it["not"].append("bu hisse için dakikalık veri gelmedi")
                return True
            return False
        ts = sorted(bars)
        giris_ts = (it["t"] // 60 + 1) * 60          # paylaşımdan sonraki ilk tam dakika
        sonra = [t for t in ts if t >= giris_ts]
        if not sonra:
            return False
        d = it["dir"]
        t0 = sonra[0]
        if it["e"] is None:
            o = bars[t0][0]
            e = o * (1 + d * SLIP)
            stop = it["stop_k"] if it["stop_k"] and (e - it["stop_k"]) * d > 0 else e * (1 - d * VARS_STOP)
            if not it["stop_k"]:
                it["not"].append(f"stop verilmemiş: %{100 * VARS_STOP:.0f} varsayıldı")
            risk = abs(e - stop)
            hedef = it["hedef_k"] if it["hedef_k"] and (it["hedef_k"] - e) * d > 0 else e + d * 2 * risk
            if not it["hedef_k"]:
                it["not"].append("hedef verilmemiş (ya da zaten geçilmişti): 2R varsayıldı")
            it.update(e=round(e, 4), s=round(stop, 4), h=round(hedef, 4), et=t0, st="açık")
            if it["giris_k"]:
                it["gec"] = round(100 * (o - it["giris_k"]) / it["giris_k"] * d, 2)
            once = [t for t in ts if it["t"] - 1800 <= t < it["t"]]
            if once:
                it["once"] = round(100 * (o - bars[once[0]][3]) / bars[once[0]][3] * d, 2)
        e, s, h, risk = it["e"], it["s"], it["h"], abs(it["e"] - it["s"]) or 1e-9
        mfe, mae = 0.0, 0.0
        for t in sonra:
            _, hi, lo, cl = bars[t][:4]
            iyi = (hi - e) * d if d > 0 else (e - lo)
            kot = (lo - e) * d if d > 0 else (e - hi)
            mfe = max(mfe, iyi / e)
            mae = min(mae, kot / e)
            stop_vur = lo <= s if d > 0 else hi >= s
            hedef_vur = hi >= h if d > 0 else lo <= h
            if stop_vur:                               # aynı mumda ikisi de olduysa: temkinli, stop
                return self._kapat(it, s, t, "stop", mfe, mae)
            if hedef_vur:
                return self._kapat(it, h, t, "hedef", mfe, mae)
            if t - t0 >= SURE:
                return self._kapat(it, cl, t, "süre", mfe, mae)
        degisti = (round(100 * mfe, 2), round(100 * mae, 2)) != (it["mfe"], it["mae"])
        it["mfe"], it["mae"] = round(100 * mfe, 2), round(100 * mae, 2)
        if now - t0 > SURE + 3 * 3600:                 # veri bitti ama süre doldu
            last = sonra[-1]
            return self._kapat(it, bars[last][3], last, "süre", mfe, mae)
        return degisti

    def _kapat(self, it, px, t, st, mfe, mae):
        d = it["dir"]
        x = px * (1 - d * SLIP)
        risk = abs(it["e"] - it["s"]) or 1e-9
        it.update(x=round(x, 4), xt=t, st=st, r=round(d * (x - it["e"]) / risk, 3),
                  mfe=round(100 * mfe, 2), mae=round(100 * mae, 2))
        self.ver += 1
        return True

    # ------------------------------------------------------------------ öğrenme ve rapor
    def as_signals(self):
        """Öğrenme modülü için sonuçlanan kayıtlar (kurgu: 'kanal:<ad>')."""
        out = []
        for x in self.items:
            if x["st"] in ("hedef", "stop", "süre") and x["r"] is not None:
                out.append({"id": "dis-" + x["id"], "sym": x["sym"], "dir": x["dir"], "setup": "kanal:" + x["kanal"],
                            "ses": x.get("ses") or "regular", "t": x["t"], "xt": x["xt"], "st": x["st"],
                            "r": x["r"], "f": {}, "dis": 1})
        return out

    def karne(self):
        kan = {}
        for x in self.items:
            k = kan.setdefault(x["kanal"], {"kanal": x["kanal"], "n": 0, "acik": 0, "veriyok": 0, "tuttu": 0,
                                            "rs": [], "once": [], "mfe": []})
            if x["st"] in ("bekliyor", "açık"):
                k["acik"] += 1
            elif x["st"] == "veri yok":
                k["veriyok"] += 1
            elif x["r"] is not None:
                k["n"] += 1
                k["tuttu"] += 1 if x["r"] > 0 else 0
                k["rs"].append(x["r"])
                if x.get("once") is not None:
                    k["once"].append(x["once"])
                if x.get("mfe") is not None:
                    k["mfe"].append(x["mfe"])
        ort = lambda a: round(sum(a) / len(a), 2) if a else None
        rows = []
        for k in kan.values():
            rows.append({"kanal": k["kanal"], "n": k["n"], "acik": k["acik"], "veriyok": k["veriyok"],
                         "wr": round(100 * k["tuttu"] / k["n"]) if k["n"] else None, "avg": ort(k["rs"]),
                         "tot": round(sum(k["rs"]), 2), "once": ort(k["once"]), "mfe": ort(k["mfe"])})
        rows.sort(key=lambda r: -(r["n"] + r["acik"]))
        son = [{"kanal": x["kanal"], "sym": x["sym"], "dir": x["dir"], "t": x["t"], "st": x["st"], "r": x["r"],
                "e": x["e"], "s": x["s"], "h": x["h"], "once": x.get("once"), "mfe": x.get("mfe")}
               for x in sorted(self.items, key=lambda x: -x["t"])[:20]]
        return {"kanallar": rows, "son": son}

    def tg_metin(self):
        k = self.karne()
        if not k["kanallar"]:
            return ("📡 <b>Kanal karnesi</b>\nHenüz iletilmiş sinyal yok. Bir kanalda sinyal görünce mesajı bana "
                    "ilet; paylaşım saatinden itibaren gölgede takip edip sonucunu ölçerim.")
        esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        out = ["📡 <b>Kanal karnesi</b> (bot bu sinyallerle işlem açmaz, sadece ölçer)"]
        for r in k["kanallar"]:
            if r["n"]:
                out.append(f"• <b>{esc(r['kanal'])}</b>: {r['n']} sonuç · tutma %{r['wr']} · ort. {r['avg']:+.2f}R "
                           f"(toplam {r['tot']:+.1f}R)"
                           + (f" · paylaşımdan önceki 30 dk'da zaten %{r['once']:+.1f}" if r["once"] is not None else "")
                           + (f" · açık {r['acik']}" if r["acik"] else ""))
            else:
                out.append(f"• <b>{esc(r['kanal'])}</b>: henüz sonuçlanan yok (açık {r['acik']}"
                           + (f", ölçülemeyen {r['veriyok']}" if r["veriyok"] else "") + ")")
        top = sum(r["n"] for r in k["kanallar"])
        if top < 30:
            out.append(f"ℹ️ {top} sonuç var; bir kanal hakkında hüküm için en az 30 sonuç bekle.")
        return "\n".join(out)

    def kayit_metin(self, it):
        y = "AL" if it["dir"] > 0 else "SAT"
        p = []
        if it["giris_k"]:
            p.append(f"giriş {it['giris_k']:g}")
        if it["stop_k"]:
            p.append(f"stop {it['stop_k']:g}")
        if it["hedefler"]:
            p.append("hedef " + " / ".join(f"{x:g}" for x in it["hedefler"]))
        zaman = time.strftime("%d.%m %H:%M", time.gmtime(it["t"] + 3 * 3600))
        s = (f"📡 Kaydedildi: #{it['sym']} {y} · kanal {it['kanal']} · paylaşım {zaman} TR"
             + (f"\nKanalın verdiği: {', '.join(p)}" if p else "\nKanal fiyat vermemiş: stop %5, hedef 2R varsayılacak."))
        if it["st"] == "veri yok":
            s += "\n⚠️ Mesaj 7 günden eski, ölçülemeyecek."
        else:
            s += "\nPaylaşımdan sonraki ilk dakikadan girilmiş gibi gölgede takip ediyorum. Sonuç: /kanal"
        return s
