"""
Yapay Zekâ (Gemini + Google arama)
----------------------------------
1) Araştırma: hisse için internette arar (Google arama ile temellendirilmiş Gemini): neden hareket ediyor,
   katalizör, seyreltme / hisse ihracı riski, olumlu ve olumsuz noktalar. 45 dk önbellek.
2) Karar: sinyalin rakamlarını, 5 modülün görüşünü, öğrenme modelinin beklentisini ve araştırmayı okuyup
   "hedefe stoptan önce ulaşma olasılığı"nı tahmin eder. Karar bundan hesaplanır:
   olasılık ≥ başa baş olasılığı (1 / (1 + R/Ö)) + 5 puan → AL, değilse PAS.
3) Karne: her kararın sonucu gölgede ölçülür (AL dediklerinin ort. R'si − PAS dediklerinin).
   Yetki "otomatik"te yapay zekâ ancak karnesi kanıtlanınca botun kararını etkiler.
4) Analiz, soru-cevap ve gün sonu dersi (Telegram + uygulama).
Anahtar sadece Render Environment'ta: GEMINI_API_KEY. Kota: dakikada en fazla 8, günde YZ_GUNLUK çağrı.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

TR = ZoneInfo("Europe/Istanbul")
PT = ZoneInfo("America/Los_Angeles")          # Gemini günlük kotası Pasifik gece yarısı sıfırlanır
URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
GUNLUK = int(os.getenv("YZ_GUNLUK", "400"))
ARASTIRMA_SN = 45 * 60
KARNE_MIN = 40            # karar yetkisi için en az sonuç
KARNE_GUC = 0.15          # AL dediklerinin ort. R'si PAS dediklerinden en az bu kadar iyi olmalı


def _json(txt):
    if not txt:
        return None
    t = txt.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(t[a:b + 1])
    except Exception:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", t[a:b + 1]))
        except Exception:
            return None


class YZ:
    def __init__(self, scanner, log=None):
        self.sc = scanner                   # anahtar, model adı ve dakikalık çağrı listesi tarayıcıyla ortak
        self.log = log
        self.ara = {}                       # sym -> {"t", "d": araştırma, "kaynak": [...]}
        self.bekleyen = set()               # değerlendirilen sinyal id'leri
        self.gun = None
        self.sayac = 0
        self.durum = "hazır" if scanner.ai_key else "GEMINI_API_KEY yok"
        self.arama = True                   # Google arama aracı reddedilirse aramasız devam
        self.son_hata = ""
        self.ders = None                    # son gün sonu dersi {"t", "metin"}
        self._kilit = {}

    # ------------------------------------------------------------------ çağrı
    @property
    def hazir(self):
        return bool(self.sc.ai_key)

    def kalan(self):
        g = datetime.now(PT).date()
        if g != self.gun:
            self.gun, self.sayac = g, 0
        return GUNLUK - self.sayac

    async def _bekle_kota(self, en_fazla=25):
        t0 = time.time()
        while True:
            now = time.time()
            self.sc.ai_calls = [t for t in self.sc.ai_calls if now - t < 60]
            if len(self.sc.ai_calls) < 8:
                self.sc.ai_calls.append(now)
                return True
            if now - t0 > en_fazla:
                return False
            await asyncio.sleep(2)

    async def cagir(self, prompt, arama=False, sicaklik=0.2, en_fazla_bekle=25, uzun=4096):
        """Dönüş: (metin, kaynaklar) ya da (None, []) — anahtar yok / kota / hata."""
        if not self.hazir:
            return None, []
        if self.kalan() <= 0:
            self.durum = f"günlük kota doldu ({GUNLUK})"
            return None, []
        if not await self._bekle_kota(en_fazla_bekle):
            self.durum = "dakikalık kota dolu"
            return None, []
        self.sayac += 1
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": sicaklik, "maxOutputTokens": uzun}}   # düşünen modellerde düşünme de bu sınırdan yer
        if arama and self.arama:
            body["tools"] = [{"google_search": {}}]
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(URL.format(m=self.sc.ai_model), json=body,
                                 headers={"x-goog-api-key": self.sc.ai_key, "Content-Type": "application/json"})
                if r.status_code == 404:
                    await self.sc.model_bul(c)
                    self.son_hata = "model bulunamadı, yenisi seçiliyor"
                    return None, []
                if r.status_code == 400 and "tools" in body and ("search" in r.text.lower() or "tool" in r.text.lower()):
                    self.arama = False          # bu anahtar / model arama aracını desteklemiyor
                    self.son_hata = "Google arama aracı kullanılamıyor: aramasız devam"
                    body.pop("tools", None)
                    r = await c.post(URL.format(m=self.sc.ai_model), json=body,
                                     headers={"x-goog-api-key": self.sc.ai_key, "Content-Type": "application/json"})
                if r.status_code == 429:
                    self.durum = "kota (429): biraz sonra"
                    self.son_hata = r.text[:120]
                    return None, []
                if r.status_code != 200:
                    self.son_hata = f"{r.status_code}: {r.text[:120]}"
                    return None, []
                js = r.json()
            cand = (js.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            txt = "".join(p.get("text", "") for p in parts).strip()
            kay = []
            for ch in ((cand.get("groundingMetadata") or {}).get("groundingChunks") or []):
                w = ch.get("web") or {}
                if w.get("uri"):
                    kay.append({"t": (w.get("title") or "")[:80], "u": w["uri"]})
            self.durum = f"çalışıyor · bugün {self.sayac}/{GUNLUK} çağrı" + ("" if self.arama else " · aramasız")
            return txt or None, kay[:6]
        except Exception as e:
            self.son_hata = str(e)[:120]
            return None, []

    # ------------------------------------------------------------------ 1) araştırma (internetten)
    def arastirma(self, sym, taze=ARASTIRMA_SN):
        x = self.ara.get(sym)
        return x if x and time.time() - x["t"] < taze else None

    async def arastir(self, sym, ad="", fiyat=None, degisim=None, zorla=False):
        x = self.arastirma(sym)
        if x and not zorla:
            return x
        async with self._kilit.setdefault(sym, asyncio.Lock()):     # aynı hisse için iki kez arama yapma
            x = self.arastirma(sym)
            if x and not zorla:
                return x
            hz = f"Şu an {fiyat} $, önceki kapanışa göre {degisim:+.1f}%." if fiyat and degisim is not None else ""
            prompt = (
                f"ABD borsasındaki {sym} hissesini ({ad}) internette araştır. {hz}\n"
                "Bugün ve son 3 günde neden hareket ettiğini, haber/katalizörü, hisse ihracı / seyreltme / ters bölünme / "
                "ATM teklif riskini, dolaşımdaki hisse (float) büyüklüğünü ve önemli riskleri bul. Uydurma; bilmiyorsan 'bilinmiyor' yaz.\n"
                "Yanıtı SADECE şu JSON ile ver (Türkçe):\n"
                '{"katalizor":"kısa","olumlu":["..."],"riskler":["..."],"seyreltme":"var|yok|bilinmiyor",'
                '"float":"küçük|orta|büyük|bilinmiyor","skor":-100..100,"ozet":"en fazla 2 cümle"}')
            txt, kay = await self.cagir(prompt, arama=True, sicaklik=0.1, uzun=4096)
            d = _json(txt)
            if not d:
                return None
            d["skor"] = int(max(-100, min(100, int(d.get("skor") or 0))))
            d["seyreltme"] = str(d.get("seyreltme") or "bilinmiyor").lower()
            x = {"t": time.time(), "d": d, "kaynak": kay}
            self.ara[sym] = x
            if len(self.ara) > 300:
                for k in sorted(self.ara, key=lambda z: self.ara[z]["t"])[:100]:
                    self.ara.pop(k, None)
            return x

    @staticmethod
    def ara_kova(x):
        """Öğrenme özelliği: araştırmanın özeti tek kelimede."""
        if not x:
            return "yok"
        d = x["d"]
        if d.get("seyreltme") == "var":
            return "seyreltme"
        s = d.get("skor", 0)
        return "olumlu" if s >= 40 else "olumsuz" if s <= -30 else "nötr"

    # ------------------------------------------------------------------ 2) karar
    async def karar(self, sig, baglam):
        """baglam: düz metin (rakamlar, modüller, model, piyasa). Dönüş: {"karar","p","esik","neden","risk","t"}"""
        e, s, h, d = sig["e"], sig["s"], sig["h"], sig.get("dir", 1)
        rr = abs(h - e) / abs(e - s) if e != s else 1.0
        esik = round(100 / (1 + rr) + 5)
        x = self.arastirma(sig["sym"])
        ara = ""
        if x:
            a = x["d"]
            ara = (f"İnternet araştırması: katalizör: {a.get('katalizor')}; olumlu: {', '.join(a.get('olumlu') or [])[:200]}; "
                   f"riskler: {', '.join(a.get('riskler') or [])[:200]}; seyreltme: {a.get('seyreltme')}; float: {a.get('float')}; "
                   f"skor {a.get('skor')}.")
        prompt = (
            "Sen disiplinli bir gün içi (1 dk) ABD hisse trader'ısın. Aşağıdaki sinyal için fiyatın STOPA değmeden "
            "HEDEFE ulaşma olasılığını tahmin et. Abartma: bu kurallar geçmişte ortalamada %35-45 tutuyor.\n"
            f"Sinyal: {sig['sym']} {'AL' if d > 0 else 'SAT'} · kurgu {sig.get('setup')} · seans {sig.get('ses')}\n"
            f"Giriş {e} · stop {s} · hedef {h} · risk/ödül 1:{rr:.1f} (başa baş olasılık %{round(100 / (1 + rr))})\n"
            f"{baglam}\n{ara}\n"
            "Yanıtı SADECE şu JSON ile ver (Türkçe):\n"
            '{"olasilik":0-100,"neden":"en fazla 2 cümle","risk":"en büyük risk, kısa"}')
        txt, _ = await self.cagir(prompt, arama=False, sicaklik=0.1, en_fazla_bekle=15, uzun=4096)
        j = _json(txt)
        if not j:
            return None
        try:
            p = int(max(0, min(100, float(j.get("olasilik")))))
        except (TypeError, ValueError):
            return None
        return {"karar": "AL" if p >= esik else "PAS", "p": p, "esik": esik, "neden": str(j.get("neden") or "")[:180],
                "risk": str(j.get("risk") or "")[:100], "t": int(time.time()), "ara": self.ara_kova(x)}

    # ------------------------------------------------------------------ 3) karne
    @staticmethod
    def karne(sinyaller):
        al, pas = [], []
        for s in sinyaller:
            y = s.get("yz")
            if not y or y.get("karar") not in ("AL", "PAS") or s.get("r") is None or s.get("st") not in ("hedef", "stop", "süre"):
                continue
            (al if y["karar"] == "AL" else pas).append(s["r"])
        ort = lambda a: round(sum(a) / len(a), 3) if a else None
        wr = lambda a: round(100 * sum(1 for x in a if x > 0) / len(a)) if a else None
        guc = round(ort(al) - ort(pas), 3) if al and pas else None
        kanit = len(al) + len(pas) >= KARNE_MIN and len(al) >= 10 and len(pas) >= 10 and guc is not None and guc >= KARNE_GUC
        return {"al": {"n": len(al), "avg": ort(al), "wr": wr(al)}, "pas": {"n": len(pas), "avg": ort(pas), "wr": wr(pas)},
                "n": len(al) + len(pas), "guc": guc, "kanit": kanit, "min": KARNE_MIN, "esik": KARNE_GUC}

    # ------------------------------------------------------------------ 4) analiz / soru / ders
    async def analiz(self, sym, veri):
        x = await self.arastir(sym, veri.get("ad", ""), veri.get("p"), veri.get("ch"))
        ara = json.dumps(x["d"], ensure_ascii=False) if x else "araştırma yapılamadı"
        prompt = (f"{sym} hissesi için Türkçe, sade ve dürüst bir gün içi analiz yaz (en fazla 9 kısa satır, madde işaretli).\n"
                  f"Botun verisi: {veri.get('metin', '')}\nİnternet araştırması: {ara}\n"
                  "Şunları söyle: neden hareket ediyor, teknik durum (destek/direnç, trend), riskler (seyreltme dahil), "
                  "hangi seviyede ne olursa ilgi çeker, hangi durumda uzak durulmalı. Kesin al/sat tavsiyesi verme; "
                  "son satırda 'Yatırım tavsiyesi değildir.' yaz.")
        txt, kay = await self.cagir(prompt, arama=False, sicaklik=0.3, uzun=4096)
        return txt, (x or {}).get("kaynak") or kay

    async def sor(self, soru, baglam):
        prompt = (f"Sen ABD·BOT'un yardımcısısın (ABD hisseleri için paper trading botu). Türkçe, kısa ve dürüst cevap ver; "
                  f"gerekirse internette ara. Botun güncel durumu:\n{baglam}\n\nSoru: {soru}\n"
                  "Bilmediğin şeyi uydurma. Kesin al/sat tavsiyesi verme.")
        return await self.cagir(prompt, arama=True, sicaklik=0.3, uzun=4096)

    async def gun_dersi(self, ozet):
        prompt = ("Aşağıda bir gün içi botun bugünkü sinyal ve işlem sonuçları var. Türkçe, 4-6 madde halinde ders çıkar: "
                  "ne işe yaradı, ne yaramadı, yarın neye dikkat edilmeli. Rakamlara dayan, genel laf etme. "
                  "Tek günlük sonuçtan kural değiştirme önerme; sadece gözlem ve dikkat noktası yaz.\n" + ozet)
        txt, _ = await self.cagir(prompt, arama=False, sicaklik=0.3, uzun=4096)
        if txt:
            self.ders = {"t": int(time.time()), "metin": txt[:1500]}
        return txt
