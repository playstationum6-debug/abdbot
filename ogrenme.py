"""
Öğrenme modülü — "trader gibi" öğrenme
--------------------------------------
Kaynak: gölgede takip edilen TÜM sinyallerin sonuçları (net R, kayma/masraf dahil).
Bir trader gibi:
  1) Sadece GEÇERLİ kurallarla alınan sonuçlardan öğrenir (LEARN_SINCE öncesi dar stoplu veriler sayılmaz).
  2) Yakın geçmişe daha çok güvenir: piyasa rejimi değişir, eski sonuçların ağırlığı yarılanarak azalır.
  3) Kötü giden kurguyu hemen bırakmaz, KÜÇÜK LOTLA denemeye devam eder; iyi gidene lotu büyütür.
     (işlem sayısı düşmez, sadece pozisyon büyüklüğü değişir)
  4) Koşulları (hacim, VWAP, trend, SPY yönü, saat…) puanlar; güveni bunlara göre ayarlar.
  5) Kapanan her işlem için dürüst bir ders yazar (iyi koşul = ortalaması gerçekten artı olan koşul).
Aşırı öğrenmeye karşı: az veride sonuç sıfıra doğru çekilir (büzülme, K=15).
"""
import time

# 08.10.2026 17:15 TR (14:15 UTC): geniş stop + risk bazlı lot kuralları devreye girdi.
# Bundan önceki sinyaller çok dar stoplarla ölçüldü, öğrenmeye katılmaz.
LEARN_SINCE = 1791468900
HALF_LIFE = 150        # her 150 yeni sonuçta eski sonuçların ağırlığı yarıya iner

K = 15                 # büzülme: n işlemlik ortalama, (n+K)'ya bölünür
MIN_GROUP = 10         # kurgu × seans ayarı için en az sonuç
MIN_FEAT = 15          # koşul kovası ayarı için en az sonuç
BOT_MIN_N = 20         # bot, bu kadar sonuçtan sonra negatif beklentili kurguyu atlar

SETUP_AD = {
    "orb": "Açılış aralığı kırılımı", "vwap": "VWAP geri alma", "hacim": "Hacimli seviye kırılımı",
    "trend": "Trend geri çekilme", "gap": "Boşluk devamı", "uz_seviye": "Seviye kırılımı (uzatılmış)",
    "uz_kirilim": "Seans aralığı kırılımı (uzatılmış)", "uz_vwap": "Seans VWAP geri alma (uzatılmış)",
    "kosu": "Haberli momentum kırılımı", "geri": "Momentum ilk geri çekilme",
    "itki": "Hacimli itki (1 dk scalp)", "vwap_sek": "VWAP sekmesi (1 dk scalp)",
    "hizli": "Haberli hızlı kırılım (anlık)",
}
SES_AD = {"pre": "Piyasa öncesi", "regular": "Normal seans", "post": "Piyasa sonrası"}
FEAT_AD = {"volr": "Hacim katı", "vwap": "VWAP", "trend": "Kısa trend", "spy": "SPY yönü", "room": "Hedefe yer",
           "tod": "Zaman", "fiyat": "Fiyat", "tip": "Hisse tipi", "haber": "Haber", "obv": "OBV hacim akışı",
           "ai": "Yapay zeka haber puanı"}


def _shr(n, s):
    return s / (n + K) if n >= 0 else 0.0


def feat_label(k, v):
    return f"{FEAT_AD.get(k, k)}: {v}"


class Learner:
    def __init__(self):
        self.groups = {}     # (setup, ses) -> [n, sum_r, wins]
        self.feats = {}      # (feat, val)  -> [n, sum_r, wins]
        self.total = [0.0, 0.0, 0.0, 0, 0.0]
        self.ver = 0
        self._sig = None

    def rebuild(self, signals):
        key = (len(signals), sum(1 for s in signals[-500:] if s.get("r") is not None))
        if key == self._sig:
            return
        self._sig = key
        groups, feats, total = {}, {}, [0.0, 0.0, 0.0, 0, 0.0]
        done = [s for s in signals
                if s.get("r") is not None and s.get("st") in ("hedef", "stop", "süre")
                and (s.get("t") or 0) >= LEARN_SINCE]
        done.sort(key=lambda s: s.get("xt") or s.get("t") or 0)
        m = len(done)
        # Her kova: [n (ağırlıklı), toplam R (ağırlıklı), kazanç (ağırlıklı), ham adet, ham toplam R]
        for i, s in enumerate(done):
            r = s["r"]
            wt = 0.5 ** ((m - 1 - i) / HALF_LIFE)   # yeni sonuç = 1, eski sonuç daha az
            win = 1 if r > 0 else 0
            accs = [groups.setdefault((s["setup"], s["ses"]), [0.0, 0.0, 0.0, 0, 0.0]), total]  # total de 5 alanlı
            accs += [feats.setdefault((k, v), [0.0, 0.0, 0.0, 0, 0.0]) for k, v in (s.get("f") or {}).items()]
            for a in accs:
                a[0] += wt
                a[1] += r * wt
                a[2] += win * wt
                if len(a) > 3:
                    a[3] += 1
                    a[4] += r
        self.groups, self.feats, self.total = groups, feats, total
        self.ver += 1

    # ------------------------------------------------------------------
    def group(self, setup, ses):
        g = self.groups.get((setup, ses))
        if not g:
            return 0, 0.0
        return g[3], _shr(g[0], g[1])

    def size_mult(self, setup, ses, conf, feats):
        """Trader gibi lot ayarı: (çarpan 0,5–1,5, gerekçe). İşlem atlanmaz, sadece büyüklük değişir."""
        n, e = self.group(setup, ses)
        mult = 1.0
        why = []
        if n >= MIN_GROUP:
            ge = max(-0.5, min(0.5, e * 1.5))
            mult += ge
            if ge <= -0.1:
                why.append(f"kurgu geçmişi zayıf ({n} işlem, beklenti {e:+.2f}R): küçük lotla deniyor")
            elif ge >= 0.1:
                why.append(f"kurgu geçmişi iyi ({n} işlem, beklenti {e:+.2f}R): lot büyütüldü")
        if conf >= 75:
            mult += 0.25
            why.append("güven yüksek")
        elif conf < 60:
            mult -= 0.25
            why.append("güven sınırda")
        mult = max(0.5, min(1.5, mult))
        return mult, ", ".join(why)

    def adjust(self, setup, ses, conf, feats):
        """Güven puanını geçmiş sonuçlara göre ayarla. (yeni_güven, notlar)"""
        notes = []
        adj = 0.0
        n, e = self.group(setup, ses)
        if n >= MIN_GROUP:
            ga = max(-12.0, min(12.0, e * 40))
            adj += ga
            raw = self.groups[(setup, ses)]
            notes.append(f"Geçmiş: {SES_AD.get(ses, ses).lower()} {SETUP_AD.get(setup, setup).lower()} "
                         f"{n} işlemde ort. {raw[4] / n:+.2f}R, kazanma %{100 * raw[2] / raw[0]:.0f}")
        base = _shr(self.total[0], self.total[1])
        fa = 0.0
        best = None
        for k, v in (feats or {}).items():
            b = self.feats.get((k, v))
            if not b or b[3] < MIN_FEAT:
                continue
            d = _shr(b[0], b[1]) - base
            fa += max(-4.0, min(4.0, d * 20))
            if best is None or abs(d) > abs(best[0]):
                best = (d, k, v, b)
        adj += max(-10.0, min(10.0, fa))
        if best and abs(best[0]) >= 0.05:
            d, k, v, b = best
            notes.append(f"Öğrenilen: '{feat_label(k, v)}' olan sinyaller ort. {b[4] / b[3]:+.2f}R ({b[3]} işlem)")
        adj = max(-20.0, min(20.0, adj))
        if abs(adj) >= 1:
            notes.append(f"Öğrenme düzeltmesi: {adj:+.0f} puan")
        return int(max(0, min(100, round(conf + adj)))), notes

    def lesson(self, setup, ses, feats, r):
        """Kapanan işlem için dürüst, kısa ders."""
        if r is None:
            return ""
        n, e = self.group(setup, ses)
        cands = []
        for k, v in (feats or {}).items():
            b = self.feats.get((k, v))
            if b and b[3] >= 10:
                cands.append((b[4] / b[3], k, v, b))
        sonuc = "kazandı" if r > 0 else "kaybetti"
        head = f"Ders ({sonuc} {r:+.2f}R): "
        if not cands:
            return head + (f"bu kurguda geçerli kurallarla {n} sonuç var; kalıp çıkarmak için erken, "
                           f"en az 10 sonuçta konuşuruz.")
        worst = min(cands)
        best = max(cands)
        if r <= 0:
            if worst[0] < 0:
                return head + (f"'{feat_label(worst[1], worst[2])}' koşulu zayıf: ort. {worst[0]:+.2f}R "
                               f"({worst[3][3]} işlem). Bu koşulda lot otomatik küçülüyor.")
            return head + "koşullar genelde olumluydu; bu kayıp normal dağılım içinde, plan değişmez."
        if best[0] > 0:
            return head + (f"'{feat_label(best[1], best[2])}' koşulu işe yarıyor: ort. {best[0]:+.2f}R "
                           f"({best[3][3]} işlem). Bu koşulda lot büyüyor.")
        return head + "kazandı ama koşulların hiçbiri henüz artı beklenti göstermiyor; şans payı olabilir."

    def insights(self, top=5):
        rows = []
        base = _shr(self.total[0], self.total[1])
        for (k, v), b in self.feats.items():
            if b[3] < MIN_FEAT:
                continue
            rows.append({"k": feat_label(k, v), "n": b[3], "avg": round(b[4] / b[3], 3),
                         "wr": round(100 * b[2] / b[0], 1), "edge": round(_shr(b[0], b[1]) - base, 3)})
        rows.sort(key=lambda r: r["edge"], reverse=True)
        good = [r for r in rows if r["edge"] > 0 and r["avg"] > 0][:top]
        bad = sorted([r for r in rows if r["avg"] < 0], key=lambda r: r["avg"])[:top]
        return {"good": good, "bad": bad, "n": self.total[3],
                "avg": round(self.total[4] / self.total[3], 3) if self.total[3] else None}

    def group_rows(self):
        out = []
        for (setup, ses), g in sorted(self.groups.items(), key=lambda kv: -kv[1][3]):
            n, e = g[3], _shr(g[0], g[1])
            if n < MIN_GROUP:
                durum = "yetersiz veri · normal lot"
            elif e * 1.5 <= -0.1:
                durum = "zayıf · küçük lot"
            elif e * 1.5 >= 0.1:
                durum = "iyi · büyük lot"
            else:
                durum = "nötr · normal lot"
            out.append({"k": f"{SES_AD.get(ses, ses)} · {SETUP_AD.get(setup, setup)}", "n": n,
                        "avg": round(g[4] / n, 3), "exp": round(e, 3),
                        "wr": round(100 * g[2] / g[0], 1), "durum": durum})
        return out
