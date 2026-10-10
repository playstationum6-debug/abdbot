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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

TR = ZoneInfo("Europe/Istanbul")
PT = ZoneInfo("America/Los_Angeles")          # Gemini günlük kotası Pasifik gece yarısı sıfırlanır
URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
GUNLUK = int(os.getenv("YZ_GUNLUK", "400"))
ARASTIRMA_SN = 45 * 60
KARNE_MIN = 40            # karar yetkisi için en az sonuç
KARNE_GUC = 0.15          # AL dediklerinin ort. R'si PAS dediklerinden en az bu kadar iyi olmalı
# Hesapta en yeni model çalışmazsa (404 / ücretsiz kotası 0) sırayla denenecek yedekler
YEDEK_MODELLER = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash-lite", "gemini-2.5-flash",
                  "gemini-2.0-flash-lite", "gemini-2.0-flash"]


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
        self.hafif = os.getenv("YZ_MODEL_HAFIF", "gemini-2.5-flash-lite")   # kısa işler: daha geniş ücretsiz kota
        self.agir = os.getenv("YZ_MODEL", "")                               # boş: tarayıcının seçtiği flash modeli
        self.dur = {}                       # model -> bu zamana kadar kota yüzünden kullanılmaz
        self.yok = set()                    # hesapta bulunmayan ya da ücretsiz kotası 0 olan modeller
        self.adaylar = []                   # hesabın listesindeki flash / flash-lite modeller (yeniden eskiye)
        self._liste_t = 0.0
        self.basari = {}                    # model -> son başarılı çağrı zamanı
        self.hata_say = {}                  # neden -> sayı (karne ekranında "neden cevap yok" için)

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

    async def _listele(self, c):
        """Hesabın kullanabildiği flash modelleri (30 dk'da bir) — en yenisi çalışmazsa sıradaki denenir."""
        if self.adaylar and time.time() - self._liste_t < 1800:
            return
        self._liste_t = time.time()
        try:
            r = await c.get("https://generativelanguage.googleapis.com/v1beta/models",
                            headers={"x-goog-api-key": self.sc.ai_key}, params={"pageSize": 200})
            ms = [m["name"].split("/", 1)[-1] for m in (r.json().get("models") or [])
                  if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            ms = [m for m in ms if "flash" in m and not any(x in m for x in ("image", "tts", "live", "audio", "embed", "exp"))]

            def surum(n):
                m_ = re.search(r"(\d+(?:\.\d+)?)", n)
                return ("preview" not in n, float(m_.group(1)) if m_ else 0, "latest" in n)
            self.adaylar = sorted(ms, key=surum, reverse=True)
        except Exception:
            pass

    def _model_sec(self, hafif):
        """Karar / ders gibi kısa işler hafif (lite) modelle — ücretsiz kotası daha geniş; araştırma ve analiz ana modelle.
        Biri kotaya takıldıysa, hesapta yoksa ya da ücretsiz kotası 0 ise sıradakine geçilir."""
        agir = self.agir or self.sc.ai_model
        lite = [m for m in self.adaylar if "lite" in m]
        tam = [m for m in self.adaylar if "lite" not in m]
        yedek_l = [m for m in YEDEK_MODELLER if "lite" in m]
        yedek_t = [m for m in YEDEK_MODELLER if "lite" not in m]
        sira = ([self.hafif] + lite + yedek_l + [agir] + tam + yedek_t) if hafif else \
            ([agir] + tam + yedek_t + [self.hafif] + lite + yedek_l)
        now = time.time()
        for m in dict.fromkeys(x for x in sira if x):
            if m not in self.yok and self.dur.get(m, 0) <= now:
                return m
        return None

    def _say(self, neden):
        self.hata_say[neden] = self.hata_say.get(neden, 0) + 1

    def _kota_coz(self, model, r):
        """429 cevabından: günlük mü dakikalık mı, ne zaman açılır. 'limit: 0' = bu modelin ücretsiz kotası hiç yok."""
        now = time.time()
        if re.search(r"limit:\s*0\b", r.text or ""):
            self.yok.add(model)
            self.durum = f"{model}: bu anahtarda ücretsiz kotası yok, başka model deneniyor"
            self._say("model kotasız")
            return
        bekle, gunluk = 60, False
        try:
            err = (r.json() or {}).get("error") or {}
            for d in err.get("details") or []:
                for v in d.get("violations") or []:
                    if "PerDay" in str(v.get("quotaId", "")):
                        gunluk = True
                rd = d.get("retryDelay")
                if rd:
                    bekle = max(bekle, float(str(rd).rstrip("s") or 60))
        except Exception:
            pass
        self._say("günlük kota" if gunluk else "dakikalık kota")
        if gunluk:
            yarin = datetime.now(PT).replace(hour=0, minute=0, second=5, microsecond=0) + timedelta(days=1)
            self.dur[model] = yarin.timestamp()
            ac = datetime.fromtimestamp(yarin.timestamp(), TR).strftime("%H:%M")
            self.durum = f"{model}: Google'ın ücretsiz GÜNLÜK kotası doldu, TR {ac}'de açılır"
        else:
            self.dur[model] = now + min(bekle, 300)
            self.durum = f"{model}: dakikalık kota doldu, {int(min(bekle, 300))} sn bekleniyor"

    async def cagir(self, prompt, arama=False, sicaklik=0.2, en_fazla_bekle=25, uzun=4096, hafif=False, json_=False):
        """Dönüş: (metin, kaynaklar) ya da (None, []) — anahtar yok / kota / hata.
        Bir model 404 / ücretsiz kotası 0 / dakikalık kota verirse sıradaki model denenir (en fazla 4 deneme)."""
        if not self.hazir:
            return None, []
        if self.kalan() <= 0:
            self.durum = f"günlük sınır doldu (YZ_GUNLUK={GUNLUK})"
            self._say("günlük sınır")
            return None, []
        hdr = {"x-goog-api-key": self.sc.ai_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                await self._listele(c)
                for _deneme in range(4):
                    model = self._model_sec(hafif)
                    if not model:
                        self.son_hata = "kullanılabilir model kalmadı (hepsi kotada) — biraz sonra tekrar denenecek"
                        self._say("model kalmadı")
                        return None, []
                    if not await self._bekle_kota(en_fazla_bekle):
                        self.durum = "dakikalık sınır dolu (8/dk)"
                        self._say("bot sınırı 8/dk")
                        return None, []
                    self.sayac += 1
                    gc = {"temperature": sicaklik, "maxOutputTokens": uzun}   # düşünen modellerde düşünme de bu sınırdan yer
                    if json_ and not arama:
                        gc["responseMimeType"] = "application/json"
                    if hafif:                                   # kısa işlerde uzun düşünme yok: hızlı ve ucuz
                        gc["thinkingConfig"] = {"thinkingBudget": 0} if "2.5" in model or "2.0" in model else {"thinkingLevel": "low"}
                    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gc}
                    if arama and self.arama:
                        body["tools"] = [{"google_search": {}}]
                    r = await c.post(URL.format(m=model), json=body, headers=hdr)
                    if r.status_code == 400 and "thinking" in r.text.lower():
                        gc.pop("thinkingConfig", None)            # bu model düşünme ayarını kabul etmiyor
                        r = await c.post(URL.format(m=model), json=body, headers=hdr)
                    if r.status_code == 400 and "tools" in body and ("search" in r.text.lower() or "tool" in r.text.lower()):
                        self.arama = False          # bu anahtar / model arama aracını desteklemiyor
                        self.son_hata = "Google arama aracı kullanılamıyor: aramasız devam"
                        body.pop("tools", None)
                        r = await c.post(URL.format(m=model), json=body, headers=hdr)
                    if r.status_code == 404:
                        self.yok.add(model)               # bu hesapta yok: sıradaki modelle devam
                        self.son_hata = f"{model} bulunamadı, sıradaki model deneniyor"
                        self._say("model yok")
                        continue
                    if r.status_code == 429:
                        self._kota_coz(model, r)
                        self.son_hata = self.durum
                        continue                      # diğer modeli dene
                    if r.status_code != 200:
                        self.son_hata = f"{model} {r.status_code}: {r.text[:120]}"
                        self._say(f"hata {r.status_code}")
                        return None, []
                    js = r.json()
                    cand = (js.get("candidates") or [{}])[0]
                    parts = (cand.get("content") or {}).get("parts") or []
                    txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                    kay = []
                    for ch in ((cand.get("groundingMetadata") or {}).get("groundingChunks") or []):
                        w = ch.get("web") or {}
                        if w.get("uri"):
                            kay.append({"t": (w.get("title") or "")[:80], "u": w["uri"]})
                    if not txt:
                        self.son_hata = f"{model}: boş cevap ({cand.get('finishReason') or 'bilinmiyor'})"
                        self._say("boş cevap")
                        continue
                    self.basari[model] = time.time()
                    if not hafif and model != self.sc.ai_model and (self.sc.ai_model in self.yok or
                                                                    self.dur.get(self.sc.ai_model, 0) > time.time() + 3600):
                        self.sc.ai_model = model           # tarayıcının haber puanlaması da çalışan modele geçsin
                    self.durum = f"çalışıyor · {model} · bugün {self.sayac}/{GUNLUK} çağrı" + ("" if self.arama else " · aramasız")
                    return txt, kay[:6]
        except Exception as e:
            self.son_hata = str(e)[:120] or type(e).__name__
            self._say("bağlantı / zaman aşımı")
            return None, []
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
        txt, _ = await self.cagir(prompt, arama=False, sicaklik=0.1, en_fazla_bekle=15, uzun=2048, hafif=True, json_=True)
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
        al, pas, yok = [], [], 0
        for s in sinyaller:
            y = s.get("yz")
            if y and y.get("karar") == "YOK":
                yok += 1
            if not y or y.get("karar") not in ("AL", "PAS") or s.get("r") is None or s.get("st") not in ("hedef", "stop", "süre"):
                continue
            (al if y["karar"] == "AL" else pas).append(s["r"])
        ort = lambda a: round(sum(a) / len(a), 3) if a else None
        wr = lambda a: round(100 * sum(1 for x in a if x > 0) / len(a)) if a else None
        guc = round(ort(al) - ort(pas), 3) if al and pas else None
        kanit = len(al) + len(pas) >= KARNE_MIN and len(al) >= 10 and len(pas) >= 10 and guc is not None and guc >= KARNE_GUC
        return {"al": {"n": len(al), "avg": ort(al), "wr": wr(al)}, "pas": {"n": len(pas), "avg": ort(pas), "wr": wr(pas)},
                "n": len(al) + len(pas), "guc": guc, "kanit": kanit, "min": KARNE_MIN, "esik": KARNE_GUC, "yok": yok}

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
        txt, _ = await self.cagir(prompt, arama=False, sicaklik=0.3, uzun=4096, hafif=True)
        if txt:
            self.ders = {"t": int(time.time()), "metin": txt[:1500]}
        return txt
