"""
Öğrenme modülü v2 — "trader gibi" öğrenme
-----------------------------------------
Kaynak: gölgede takip edilen TÜM sinyallerin sonuçları (tuttu / tutmadı / süre doldu; net R, kayma ve masraf dahil).
İşleme dönüşmeyen sinyaller de öğrenmeye girer; bot işlem atlasa bile öğrenme durmaz.

Nasıl öğrenir:
  1) Zamana göre hafıza: eski sonuçlar unutulmaz, ağırlıkları her HALF_DAYS günde yarıya iner.
     (v1'de yarı ömür "150 sonuç"tu: 13.000 sonuçta bot sadece son birkaç yüzünü hatırlıyordu.)
  2) Tahmin modeli: her sinyal için "beklenen R" hesaplar. Kurgu, seans, kurgu×seans ve sonuçla gerçekten
     ilişkili koşulların (saat, SPY yönü, büyük resim, hacim, VWAP, EMA, kısa trend) katkısını AYNI ANDA öğrenir
     (ridge / backfitting). Birbirine benzeyen koşullar iki kez sayılmaz.
  3) Aşırı öğrenmeye karşı: az veride her katkı sıfıra çekilir (LAM). Model kendini sınar: eski verinin %70'iyle
     öğrenip en yeni %30'unda dener, sonucu dürüstçe raporlar (insights()["model"]).
  4) Seçicilik: bot, modelin en iyi SECICI diliminde kalmayan sinyallerde işlem açmaz (veto). Sinyal yine
     gölgede takip edilir, sonucu öğrenmeye girer. İyi dilimde lot büyür, sınırda küçülür.
  5) Kapanan her işlem için dürüst bir ders yazar.
"""
import time

# 08.10.2026 17:15 TR (14:15 UTC): geniş stop + risk bazlı lot kuralları devreye girdi.
# Bundan önceki canlı sinyaller öğrenmeye katılmaz (geçmiş test sonuçları bugünkü kurallarla yapıldığı için katılır).
LEARN_SINCE = 1791468900

HALF_DAYS = 10.0      # hafıza: 10 gün önceki sonucun ağırlığı yarıya iner
LAM = 300.0           # büzülme: bir koşulun katkısı, ~300 sonuçtan sonra kendi verisine güvenir
K = 30.0              # kurgu × seans beklentisinde genel ortalamaya çekme gücü
PASSES = 4            # model kurma turu
MIN_MODEL = 500       # model bu kadar sonuçtan sonra karar verir
MIN_GROUP = 20        # kurgu × seans satırı için en az sonuç
MIN_FEAT = 30         # koşul satırı için en az sonuç
SECICI = 0.35         # bot sadece modelin en iyi %35'lik dilimindeki sinyallerde işlem açar
REFIT_SEC = 180       # en sık 3 dakikada bir modeli baştan kur
SINAV_SEC = 1800      # kendi kendine sınav en sık 30 dakikada bir
BOT_MIN_N = MIN_GROUP  # (eski adıyla uyumluluk)

MODEL_FEATS = ("tod", "spy", "mtf", "volr", "vwap", "ema50", "trend", "yz", "yz_ara")   # yz: yapay zekânın kararı

SETUP_AD = {
    "orb": "Açılış aralığı kırılımı", "vwap": "VWAP geri alma", "hacim": "Hacimli seviye kırılımı",
    "trend": "Trend geri çekilme", "gap": "Boşluk devamı", "uz_seviye": "Seviye kırılımı (uzatılmış)",
    "uz_kirilim": "Seans aralığı kırılımı (uzatılmış)", "uz_vwap": "Seans VWAP geri alma (uzatılmış)",
    "kosu": "Haberli momentum kırılımı", "geri": "Momentum ilk geri çekilme", "sicrama": "Dikey sıçrama sonrası geri çekilme",
    "itki": "Hacimli itki (1 dk scalp)", "vwap_sek": "VWAP sekmesi (1 dk scalp)",
    "hizli": "Haberli hızlı kırılım (anlık)",
    "formasyon": "Formasyon kırılımı", "talep": "Alıcı bölgesinden dönüş",
    "poc": "Hacim profili (POC) dönüşü", "gapgo": "Boşlukla açılış kırılımı",
    "fade": "Koşan hissede dönüş (açığa satış)", "fvg": "FVG dönüşü", "kanal": "Kanal alt bandından dönüş",
    "yapi": "Yapı kırılımı (BOS)", "fib": "Fibonacci geri çekilme dönüşü",
    "onay": "Kırılım onayı (1 dk)",
}


def setup_ad(k):
    k = str(k)
    if k.startswith("kanal:"):
        return f"Telegram kanalı {k[6:]}"
    return SETUP_AD.get(k, k)
SES_AD = {"pre": "Piyasa öncesi", "regular": "Normal seans", "post": "Piyasa sonrası"}
FEAT_AD = {"volr": "Hacim katı", "vwap": "VWAP", "trend": "Kısa trend", "spy": "SPY yönü", "room": "Hedefe yer",
           "tod": "Zaman", "fiyat": "Fiyat", "tip": "Hisse tipi", "haber": "Haber", "obv": "OBV hacim akışı",
           "ai": "Yapay zeka haber puanı", "sec": "SEC bildirimi", "formasyon": "Formasyon",
           "talep": "Alıcı bölgesi",
           "bilanco": "Bilanço", "hava": "Piyasa havası", "yapi": "Piyasa yapısı", "kanal": "Kanal",
           "fvg": "FVG boşluğu", "fib": "Fibonacci", "mum": "Sinyal mumu", "ema50": "EMA 20/50",
           "mtf": "Büyük resim (15 dk + günlük)",
           "setup": "Kurgu", "ses": "Seans", "kxs": "Kurgu × seans"}


def feat_label(k, v):
    if k == "setup":
        return f"Kurgu: {setup_ad(v)}"
    if k == "ses":
        return f"Seans: {SES_AD.get(v, v)}"
    if k == "kxs":
        a, _, b = str(v).partition("|")
        return f"{SES_AD.get(b, b)} · {setup_ad(a)}"
    return f"{FEAT_AD.get(k, k)}: {v}"


def _cols(setup, ses, feats):
    f = feats or {}
    c = [("setup", setup), ("ses", ses), ("kxs", f"{setup}|{ses}")]
    c += [(k, f[k]) for k in MODEL_FEATS if k in f]
    return c


def _ts(s):
    return s.get("xt") or s.get("t") or 0


def _fit(rows, weights):
    """Ağırlıklı ridge (backfitting). rows: [(cols, r)]. Döner: (mu, beta)."""
    sw = sum(weights) or 1.0
    mu = sum(w * r for (_, r), w in zip(rows, weights)) / sw
    idx = {}
    for i, (cs, _) in enumerate(rows):
        for kv in cs:
            idx.setdefault(kv, []).append(i)
    pred = [mu] * len(rows)
    beta = {}
    for _ in range(PASSES):
        for kv, ii in idx.items():
            b = beta.get(kv, 0.0)
            num = 0.0
            den = LAM
            for i in ii:
                w = weights[i]
                num += w * (rows[i][1] - pred[i] + b)
                den += w
            nb = num / den
            if nb != b:
                dlt = nb - b
                for i in ii:
                    pred[i] += dlt
                beta[kv] = nb
    return mu, beta


def _pred(mu, beta, cols):
    return mu + sum(beta.get(kv, 0.0) for kv in cols)


def _pct(sorted_vals, x):
    """x, dağılımın yüzde kaçından büyük (0..1)."""
    n = len(sorted_vals)
    if not n:
        return 0.5
    lo, hi = 0, n
    while lo < hi:
        m = (lo + hi) // 2
        if sorted_vals[m] <= x:
            lo = m + 1
        else:
            hi = m
    return lo / n


class Learner:
    def __init__(self):
        self.groups = {}     # (setup, ses) -> [n_w, sum_r_w, wins_w, n, sum_r]
        self.feats = {}      # (feat, val)  -> [n_w, sum_r_w, wins_w, n, sum_r]
        self.total = [0.0, 0.0, 0.0, 0, 0.0]
        self.mu = 0.0
        self.beta = {}
        self.dist = []       # eğitim sinyallerinin tahmin dağılımı (sıralı)
        self.cut = None      # bot için seçicilik eşiği (beklenen R)
        self.sinav = None    # kendi kendine sınav sonucu
        self.sel = {}        # (setup, ses) -> [son sinyal sayısı, seçilen]
        self.ver = 0
        self._sig = None
        self._fit_t = 0.0
        self._fit_n = 0
        self._sinav_t = 0.0

    # ------------------------------------------------------------------ kurma
    def rebuild(self, signals):
        key = (len(signals), sum(1 for s in signals[-500:] if s.get("r") is not None))
        if key == self._sig:
            return
        self._sig = key
        done = [s for s in signals
                if s.get("r") is not None and s.get("st") in ("hedef", "stop", "süre") and s.get("setup")
                and ((s.get("t") or 0) >= LEARN_SINCE or s.get("bt") or s.get("dis"))]
        done.sort(key=_ts)
        m = len(done)
        if not m:
            return
        t_end = _ts(done[-1])
        wts = [0.5 ** (max(0.0, t_end - _ts(s)) / 86400.0 / HALF_DAYS) for s in done]

        # 1) tablolar (site ve Telegram için): ağırlıklı + ham
        groups, feats, total = {}, {}, [0.0, 0.0, 0.0, 0, 0.0]
        for s, wt in zip(done, wts):
            r = s["r"]
            win = 1 if r > 0 else 0
            accs = [groups.setdefault((s["setup"], s.get("ses")), [0.0, 0.0, 0.0, 0, 0.0]), total]
            accs += [feats.setdefault((k, v), [0.0, 0.0, 0.0, 0, 0.0]) for k, v in (s.get("f") or {}).items()]
            for a in accs:
                a[0] += wt
                a[1] += r * wt
                a[2] += win * wt
                a[3] += 1
                a[4] += r
        self.groups, self.feats, self.total = groups, feats, total

        # 2) tahmin modeli (pahalı kısım: sık sık değil)
        now = time.time()
        if sum(1 for s in done if not s.get("dis")) >= 50 and (not self.beta or now - self._fit_t >= REFIT_SEC or abs(m - self._fit_n) >= 300):
            own = [(s, w) for s, w in zip(done, wts) if not s.get("dis")]
            done_m = [s for s, _ in own]
            wts_m = [w for _, w in own]
            rows = [(_cols(s["setup"], s.get("ses"), s.get("f")), s["r"]) for s in done_m]
            self.mu, self.beta = _fit(rows, wts_m)
            preds = sorted(_pred(self.mu, self.beta, c) for c, _ in rows[-3000:])
            self.dist = preds
            self.cut = preds[int(len(preds) * (1 - SECICI))] if len(preds) >= 50 else None
            sel = {}
            if self.cut is not None:
                for s, (c, _) in zip(done_m[-3000:], rows[-3000:]):
                    a = sel.setdefault((s["setup"], s.get("ses")), [0, 0])
                    a[0] += 1
                    a[1] += 1 if _pred(self.mu, self.beta, c) >= self.cut else 0
            self.sel = sel
            self._fit_t, self._fit_n = now, m
            if len(rows) >= MIN_MODEL and now - self._sinav_t >= SINAV_SEC:
                self._sinav(rows, wts_m)
                self._sinav_t = now
        self.ver += 1

    def _sinav(self, rows, wts):
        """Kendi kendine sınav: eski %70 ile öğren, hiç görmediği en yeni %30'da dene."""
        try:
            k = int(len(rows) * 0.7)
            mu, beta = _fit(rows[:k], wts[:k])
            te = rows[k:]
            p = [_pred(mu, beta, c) for c, _ in te]
            tr_p = sorted(_pred(mu, beta, c) for c, _ in rows[max(0, k - 3000):k])
            cut = tr_p[int(len(tr_p) * (1 - SECICI))] if tr_p else 0.0
            sec = [r for x, (_, r) in zip(p, te) if x >= cut]
            ret = [r for x, (_, r) in zip(p, te) if x < cut]
            hep = [r for _, r in te]
            avg = lambda a: round(sum(a) / len(a), 3) if a else None
            self.sinav = {"n": len(te), "hepsi": avg(hep), "secilen": avg(sec), "secilen_n": len(sec),
                          "elenen": avg(ret), "elenen_n": len(ret),
                          "kazanc": round((avg(sec) or 0) - (avg(hep) or 0), 3) if sec and hep else None,
                          "t": int(time.time())}
        except Exception:
            self.sinav = None

    # ------------------------------------------------------------------ tahmin
    def ready(self):
        return bool(self.beta) and self.total[3] >= MIN_MODEL

    def predict(self, setup, ses, feats):
        """(beklenen R, dilim 0..1). Model hazır değilse (None, None)."""
        if not self.beta:
            return None, None
        p = _pred(self.mu, self.beta, _cols(setup, ses, feats))
        return p, _pct(self.dist, p)

    def group(self, setup, ses):
        g = self.groups.get((setup, ses))
        if not g:
            return 0, 0.0
        mu = self.total[1] / self.total[0] if self.total[0] else 0.0
        return g[3], (g[1] + K * mu) / (g[0] + K)

    def veto(self, setup, ses, feats):
        """Bot işlem açmalı mı? (True = açma, gerekçe). Sinyal yine takip edilir ve öğrenmeye girer."""
        if not self.ready() or self.cut is None:
            return False, ""
        p, q = self.predict(setup, ses, feats)
        if p is None or p >= self.cut:
            return False, ""
        return True, (f"öğrenme filtresi: beklenen {p:+.2f}R, sıra {100 * q:.0f}/100; bot sadece en iyi "
                      f"{100 * SECICI:.0f}/100 dilimde işlem açıyor (sinyal takipte, sonucu öğrenmeye girecek)")

    def size_mult(self, setup, ses, conf, feats):
        """Trader gibi lot ayarı: (çarpan 0,5–1,5, gerekçe)."""
        n, e = self.group(setup, ses)
        if not self.ready():
            if n >= MIN_GROUP and e < -0.2:
                return 0.6, f"kurgu geçmişi zayıf ({n} işlem, beklenti {e:+.2f}R): küçük lot"
            return 1.0, "model henüz öğreniyor: normal lot"
        p, q = self.predict(setup, ses, feats)
        if q >= 0.9:
            return 1.5, f"modelin en iyi %10'u (beklenen {p:+.2f}R): lot büyüdü"
        if q >= 0.8:
            return 1.25, f"modelin en iyi %20'si (beklenen {p:+.2f}R): lot biraz büyüdü"
        if q >= 1 - SECICI:
            return 1.0, f"seçilen dilimde (beklenen {p:+.2f}R): normal lot"
        return 0.5, f"seçilen dilimin altında (beklenen {p:+.2f}R): küçük lot"

    def adjust(self, setup, ses, conf, feats):
        """Güven puanını modele göre ayarla. (yeni_güven, notlar). Ham puan conf0 olarak ayrıca saklanır."""
        notes = []
        n, e = self.group(setup, ses)
        g = self.groups.get((setup, ses))
        if g and n >= MIN_GROUP:
            notes.append(f"Geçmiş: {SES_AD.get(ses, ses).lower()} {setup_ad(setup).lower()} "
                         f"{n} sinyalde ort. {g[4] / n:+.2f}R, tutma %{100 * g[2] / g[0]:.0f}")
        if not self.ready():
            return conf, notes
        p, q = self.predict(setup, ses, feats)
        # Testte kural puanı sonuçla ilişkisizdi; model ağırlıklı karışım
        yeni = int(max(0, min(100, round(0.3 * conf + 0.7 * 100 * q))))
        notes.append(f"Model: beklenen {p:+.2f}R, sıra {100 * q:.0f}/100 (100 = en iyi)")
        cs = _cols(setup, ses, feats)
        katki = sorted(((self.beta.get(kv, 0.0), kv) for kv in cs), key=lambda x: x[0])
        if katki and katki[-1][0] >= 0.03:
            b, kv = katki[-1]
            notes.append(f"En çok yardım eden: '{feat_label(*kv)}' ({b:+.2f}R)")
        if katki and katki[0][0] <= -0.03:
            b, kv = katki[0]
            notes.append(f"En çok zarar veren: '{feat_label(*kv)}' ({b:+.2f}R)")
        if abs(yeni - conf) >= 1:
            notes.append(f"Öğrenme düzeltmesi: {yeni - conf:+d} puan")
        return yeni, notes

    def lesson(self, setup, ses, feats, r):
        """Kapanan işlem için dürüst, kısa ders."""
        if r is None:
            return ""
        sonuc = "kazandı" if r > 0 else "kaybetti"
        head = f"Ders ({sonuc} {r:+.2f}R): "
        if not self.ready():
            n, _ = self.group(setup, ses)
            return head + f"bu kurguda {n} sonuç var; model {MIN_MODEL} sonuçtan sonra konuşacak."
        p, q = self.predict(setup, ses, feats)
        cs = _cols(setup, ses, feats)
        katki = sorted(((self.beta.get(kv, 0.0), kv) for kv in cs), key=lambda x: x[0])
        if r <= 0:
            if q < 1 - SECICI:
                b, kv = katki[0]
                return head + (f"model zaten zayıf görüyordu (beklenen {p:+.2f}R). En büyük eksi: "
                               f"'{feat_label(*kv)}' ({b:+.2f}R). Benzerlerinde lot küçük kalacak.")
            return head + (f"model iyi görüyordu (beklenen {p:+.2f}R, sıra {100 * q:.0f}/100); tek kayıp kalıbı "
                           f"bozmaz, sonuç öğrenmeye eklendi.")
        if q >= 1 - SECICI:
            b, kv = katki[-1]
            return head + (f"model beklediği gibi (beklenen {p:+.2f}R). En büyük artı: "
                           f"'{feat_label(*kv)}' ({b:+.2f}R).")
        return head + f"model zayıf görüyordu (beklenen {p:+.2f}R); kazanç şans payı olabilir, tek sonuçla kural değişmez."

    # ------------------------------------------------------------------ rapor
    def insights(self, top=5):
        """Site ve Telegram için: işe yarayan / yaramayan koşullar + modelin sınav karnesi."""
        rows = []
        for (k, v), b in self.feats.items():
            if b[3] < MIN_FEAT:
                continue
            eff = self.beta.get((k, v))
            if eff is None:   # modelde olmayan koşul: farkı az veride sıfıra çek
                mu = self.total[1] / self.total[0] if self.total[0] else 0.0
                eff = (b[1] - b[0] * mu) / (b[0] + LAM)
            rows.append({"k": feat_label(k, v), "n": b[3], "avg": round(b[4] / b[3], 3),
                         "wr": round(100 * b[2] / b[0], 1) if b[0] else 0.0, "edge": round(eff, 3)})
        for (setup, ses), g in self.groups.items():
            eff = self.beta.get(("kxs", f"{setup}|{ses}"), 0.0) + self.beta.get(("setup", setup), 0.0)
            if g[3] >= MIN_FEAT:
                rows.append({"k": feat_label("kxs", f"{setup}|{ses}"), "n": g[3], "avg": round(g[4] / g[3], 3),
                             "wr": round(100 * g[2] / g[0], 1) if g[0] else 0.0, "edge": round(eff, 3)})
        good = sorted([r for r in rows if r["edge"] > 0.01], key=lambda r: -r["edge"])[:top]
        bad = sorted([r for r in rows if r["edge"] < -0.01], key=lambda r: r["edge"])[:top]
        return {"good": good, "bad": bad, "n": self.total[3],
                "avg": round(self.total[4] / self.total[3], 3) if self.total[3] else None,
                "wr": round(100 * self.total[2] / self.total[0], 1) if self.total[0] else None,
                "model": self.model_info()}

    def model_info(self):
        return {"hazir": self.ready(), "n": self.total[3], "secici": SECICI,
                "esik": round(self.cut, 3) if self.cut is not None else None,
                "hafiza_gun": HALF_DAYS, "sinav": self.sinav,
                "kurulus": int(self._fit_t) if self._fit_t else None}

    def group_rows(self):
        out = []
        for (setup, ses), g in sorted(self.groups.items(), key=lambda kv: -kv[1][3]):
            n, e = self.group(setup, ses)
            a = self.sel.get((setup, ses))
            secilen = round(100 * a[1] / a[0]) if a and a[0] and self.ready() else None
            if n < MIN_GROUP:
                durum = "yetersiz veri · öğreniyor"
            elif secilen is None:
                durum = "iyi" if e >= 0.05 else "zayıf" if e <= -0.1 else "nötr"
            elif secilen >= 60:
                durum = f"iyi · işleme uygun: %{secilen}"
            elif secilen >= 20:
                durum = f"seçici · işleme uygun: %{secilen}"
            elif secilen > 0:
                durum = f"zayıf · işleme uygun: %{secilen}"
            else:
                durum = "zayıf · işlem açılmıyor"
            out.append({"k": f"{SES_AD.get(ses, ses)} · {setup_ad(setup)}", "n": n,
                        "avg": round(g[4] / n, 3), "exp": round(e, 3),
                        "wr": round(100 * g[2] / g[0], 1) if g[0] else 0.0, "durum": durum,
                        "secilen": secilen})
        return out

    def ozet(self):
        """Telegram / site için kısa, dürüst öğrenme özeti (HTML <b> etiketli metin)."""
        if not self.total[3]:
            return "🎓 Henüz öğrenilecek sonuç yok."
        ins = self.insights(top=3)
        out = [f"🎓 <b>Öğrenme</b> · {ins['n']} sinyal sonucu, ort. {ins['avg']:+.2f}R, tutma %{ins['wr']:.0f}"]
        s = self.sinav
        if s and s.get("secilen") is not None:
            out.append(f"🧪 Sınav (görmediği son {s['n']} sinyal): hepsi {s['hepsi']:+.2f}R → "
                       f"modelin seçtikleri {s['secilen']:+.2f}R ({s['secilen_n']}), "
                       f"elediği {s['elenen']:+.2f}R ({s['elenen_n']})")
            if s["secilen"] <= 0:
                out.append("⚠️ Seçilen dilim de henüz artıda değil: sinyal kuralları avantaj üretmiyor, "
                           "model sadece zararı küçültüyor.")
        esc = lambda x: str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if ins["good"]:
            out.append("✅ Yardım edenler: " + "; ".join(f"{esc(r['k'])} ({r['edge']:+.2f}R)" for r in ins["good"]))
        if ins["bad"]:
            out.append("❌ Zarar verenler: " + "; ".join(f"{esc(r['k'])} ({r['edge']:+.2f}R)" for r in ins["bad"]))
        out.append(f"🎯 Bot sadece modelin sıralamasında en iyi {100 * SECICI:.0f}/100 dilimdeki sinyallerde işlem açıyor; "
                   f"geri kalan sinyaller takipte ve öğrenmeye giriyor.")
        return "\n".join(out)
