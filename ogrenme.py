"""
Öğrenme modülü
--------------
Kaynak: gölgede takip edilen TÜM sinyallerin sonuçları (net R, kayma/masraf dahil).
Ne öğrenir:
  1) Kurgu × seans beklentisi  (ör. "Normal seans · ORB": 34 işlem, ort. +0,21R)
  2) Koşul kovaları            (ör. "Hacim 5+ kat": ort. +0,40R; "VWAP aleyhte": ort. −0,30R)
Nasıl kullanır:
  - Yeni sinyalin güven puanını geçmiş sonuçlara göre yukarı/aşağı ayarlar (gerekçeye not düşer).
  - Bot, beklentisi negatif çıkan kurguları (yeterli veri varsa) atlar.
  - Kapanan her işlem için "ders" üretir.
Aşırı öğrenmeye karşı: az veride sonuç sıfıra doğru çekilir (büzülme, K=15) ve
en az 10–15 sonuç olmadan ayar yapılmaz.
"""

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
}
SES_AD = {"pre": "Piyasa öncesi", "regular": "Normal seans", "post": "Piyasa sonrası"}
FEAT_AD = {"volr": "Hacim katı", "vwap": "VWAP", "trend": "Kısa trend", "spy": "SPY yönü", "room": "Hedefe yer",
           "tod": "Zaman", "fiyat": "Fiyat", "tip": "Hisse tipi", "haber": "Haber", "obv": "OBV hacim akışı"}


def _shr(n, s):
    return s / (n + K) if n >= 0 else 0.0


def feat_label(k, v):
    return f"{FEAT_AD.get(k, k)}: {v}"


class Learner:
    def __init__(self):
        self.groups = {}     # (setup, ses) -> [n, sum_r, wins]
        self.feats = {}      # (feat, val)  -> [n, sum_r, wins]
        self.total = [0, 0.0, 0]
        self.ver = 0
        self._sig = None

    def rebuild(self, signals):
        key = (len(signals), sum(1 for s in signals[-500:] if s.get("r") is not None))
        if key == self._sig:
            return
        self._sig = key
        groups, feats, total = {}, {}, [0, 0.0, 0]
        for s in signals:
            r = s.get("r")
            if r is None or s.get("st") not in ("hedef", "stop", "süre"):
                continue
            w = 1 if r > 0 else 0
            for acc in (groups.setdefault((s["setup"], s["ses"]), [0, 0.0, 0]), total):
                acc[0] += 1
                acc[1] += r
                acc[2] += w
            for k, v in (s.get("f") or {}).items():
                a = feats.setdefault((k, v), [0, 0.0, 0])
                a[0] += 1
                a[1] += r
                a[2] += w
        self.groups, self.feats, self.total = groups, feats, total
        self.ver += 1

    # ------------------------------------------------------------------
    def group(self, setup, ses):
        g = self.groups.get((setup, ses))
        if not g:
            return 0, 0.0
        return g[0], _shr(g[0], g[1])

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
                         f"{n} işlemde ort. {raw[1] / n:+.2f}R, kazanma %{100 * raw[2] / n:.0f}")
        base = _shr(self.total[0], self.total[1])
        fa = 0.0
        best = None
        for k, v in (feats or {}).items():
            b = self.feats.get((k, v))
            if not b or b[0] < MIN_FEAT:
                continue
            d = _shr(b[0], b[1]) - base
            fa += max(-4.0, min(4.0, d * 20))
            if best is None or abs(d) > abs(best[0]):
                best = (d, k, v, b)
        adj += max(-10.0, min(10.0, fa))
        if best and abs(best[0]) >= 0.05:
            d, k, v, b = best
            notes.append(f"Öğrenilen: '{feat_label(k, v)}' olan sinyaller ort. {b[1] / b[0]:+.2f}R ({b[0]} işlem)")
        adj = max(-20.0, min(20.0, adj))
        if abs(adj) >= 1:
            notes.append(f"Öğrenme düzeltmesi: {adj:+.0f} puan")
        return int(max(0, min(100, round(conf + adj)))), notes

    def lesson(self, setup, ses, feats, r):
        """Kapanan işlem için kısa ders."""
        n, e = self.group(setup, ses)
        cands = []
        for k, v in (feats or {}).items():
            b = self.feats.get((k, v))
            if b and b[0] >= 10:
                cands.append((_shr(b[0], b[1]), k, v, b))
        if r is None:
            return ""
        if not cands:
            return (f"Ders: Bu kurguda henüz {n} sonuç var; kalıp çıkarmak için erken. "
                    f"Sonuç {r:+.2f}R kaydedildi.")
        if r <= 0:
            e2, k, v, b = min(cands)
            if e2 < 0:
                return (f"Ders: '{feat_label(k, v)}' koşulundaki sinyaller şimdiye kadar ort. {b[1] / b[0]:+.2f}R "
                        f"({b[0]} işlem). Bu koşulda daha seçici olmalı.")
            return f"Ders: Koşullar genelde olumluydu (en zayıf: '{feat_label(k, v)}'). Bu kayıp normal dağılım içinde olabilir."
        e2, k, v, b = max(cands)
        return (f"Ders: '{feat_label(k, v)}' koşulu işe yarıyor: ort. {b[1] / b[0]:+.2f}R ({b[0]} işlem). "
                f"Bu koşulu taşıyan sinyallere öncelik ver.")

    def insights(self, top=5):
        rows = []
        base = _shr(self.total[0], self.total[1])
        for (k, v), b in self.feats.items():
            if b[0] < MIN_FEAT:
                continue
            rows.append({"k": feat_label(k, v), "n": b[0], "avg": round(b[1] / b[0], 3),
                         "wr": round(100 * b[2] / b[0], 1), "edge": round(_shr(b[0], b[1]) - base, 3)})
        rows.sort(key=lambda r: r["edge"], reverse=True)
        good = [r for r in rows if r["edge"] > 0][:top]
        bad = [r for r in rows if r["edge"] < 0][-top:][::-1]
        return {"good": good, "bad": bad, "n": self.total[0],
                "avg": round(self.total[1] / self.total[0], 3) if self.total[0] else None}

    def group_rows(self):
        out = []
        for (setup, ses), g in sorted(self.groups.items(), key=lambda kv: -kv[1][0]):
            out.append({"k": f"{SES_AD.get(ses, ses)} · {SETUP_AD.get(setup, setup)}", "n": g[0],
                        "avg": round(g[1] / g[0], 3), "exp": round(_shr(g[0], g[1]), 3),
                        "wr": round(100 * g[2] / g[0], 1),
                        "durum": "yetersiz veri" if g[0] < BOT_MIN_N else ("bot alıyor" if _shr(g[0], g[1]) >= 0 else "bot atlıyor")})
        return out
