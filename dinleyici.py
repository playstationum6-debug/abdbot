"""
Kanal dinleyici — Telegram kanallarını OTOMATİK okur (elle iletmeye gerek yok)
-----------------------------------------------------------------------------
Telegram botları sadece yönetici oldukları kanalları okuyabilir. Başkasının kanalını okumak için bir
Telegram HESABI gerekir: bu modül o hesapla (Telethon, MTProto) herkese açık kanalların son mesajlarını
30 sn'de bir okur. Kanala abone olmak gerekmez; web önizlemesi kapalı kanallarda da çalışır.

Render Environment:
  TG_API_ID, TG_API_HASH  → my.telegram.org → API development tools
  TG_SESSION              → sitedeki /tg-giris sayfasında bir kez giriş yapınca verilen oturum anahtarı
GÜVENLİK: TG_SESSION hesabına tam erişim demektir. Sadece Render Environment'a girilir; GitHub yedeğine,
loga ya da sohbete yazılmaz. Mümkünse ayrı (yedek) bir Telegram hesabı kullan.
Bu modül sadece OKUR: mesaj göndermez, kanala katılmaz, hiçbir şeye tıklamaz.
"""
import asyncio
import logging
import re
import time

log = logging.getLogger("dinleyici")

try:
    from telethon import TelegramClient
    from telethon.errors import (FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError,
                                 PasswordHashInvalidError, SessionPasswordNeededError)
    from telethon.sessions import StringSession
    from telethon.tl.types import ChannelParticipantsAdmins
    TELETHON = True
except Exception:          # requirements'a eklenmemişse uygulama yine çalışsın
    TELETHON = False

KANAL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
ARALIK = 30               # sn: her kanal bu aralıkla okunur
GERI = 6 * 86400          # yeni eklenen kanalda son 6 günün mesajları da işlenir (7 gün veri sınırı)
YONETICI_YENILE = 3600    # gruplarda yönetici listesi saatte bir yenilenir


def kanal_adi(x):
    x = str(x or "").strip()
    x = re.sub(r"^(https?://)?(t\.me|telegram\.me)/(s/)?", "", x).lstrip("@").split("/")[0].split("?")[0]
    return x if KANAL_RE.match(x) else None


class Dinleyici:
    def __init__(self, api_id, api_hash, session):
        self.api_id = int(api_id) if str(api_id or "").isdigit() else None
        self.api_hash = api_hash or ""
        self.session = session or ""
        self.st = {"kanallar": {}}           # app, backup.data["dinle"] ile değiştirir: {ad: {"son": id, "t": zaman}}
        self.client = None
        self.on_msg = None                   # async fn(dict) -> bool (sinyal kaydedildi mi)
        self.status = "kapalı"
        self.hesap = ""
        self._giris = None                   # giriş akışı sırasında geçici istemci
        self._tetik = asyncio.Event()
        self._yon = {}                       # grup adı -> (zaman, {yönetici id}) ; None = alınamadı

    @property
    def hazir(self):
        return TELETHON and bool(self.api_id and self.api_hash)

    @property
    def kanallar(self):
        return self.st.setdefault("kanallar", {})

    # ------------------------------------------------------------------ kanal listesi
    def ekle(self, ad):
        k = kanal_adi(ad)
        if not k:
            return None
        if k.lower() not in {x.lower() for x in self.kanallar}:
            self.kanallar[k] = {"son": 0, "t": 0, "n": 0}
        self._tetik.set()
        return k

    def sil(self, ad):
        k = kanal_adi(ad)
        for x in list(self.kanallar):
            if k and x.lower() == k.lower():
                del self.kanallar[x]
                return x
        return None

    # ------------------------------------------------------------------ okuma döngüsü
    async def loop(self, kaydet=None):
        """Sürekli çalışır. kaydet(): durum değişince yedeği kirli işaretler."""
        await asyncio.sleep(20)
        if not TELETHON:
            self.status = "kapalı (requirements.txt'e telethon eklenmeli)"
            return
        if not (self.api_id and self.api_hash):
            self.status = "kapalı (TG_API_ID / TG_API_HASH yok)"
            return
        if not self.session:
            self.status = "giriş gerekli: sitede /tg-giris"
            return
        while True:
            try:
                if self.client is None or not self.client.is_connected():
                    self.client = TelegramClient(StringSession(self.session), self.api_id, self.api_hash,
                                                 device_model="ABD-BOT", app_version="1.0",
                                                 receive_updates=False)
                    await self.client.connect()
                    if not await self.client.is_user_authorized():
                        self.status = "oturum geçersiz: /tg-giris ile yeniden giriş yap, TG_SESSION'ı güncelle"
                        await self.client.disconnect()
                        self.client = None
                        await asyncio.sleep(1800)
                        continue
                    me = await self.client.get_me()
                    self.hesap = (("@" + me.username) if getattr(me, "username", None) else (me.first_name or "?"))
                if not self.kanallar:
                    self.status = f"bağlı ({self.hesap}) · izlenen kanal yok: /kanalekle @kanal"
                else:
                    yeni = 0
                    for ad in list(self.kanallar):
                        try:
                            yeni += await self._oku(ad)
                        except FloodWaitError as e:
                            self.status = f"Telegram bekletiyor ({e.seconds} sn)"
                            await asyncio.sleep(min(e.seconds + 5, 900))
                        except Exception as e:
                            self.kanallar[ad]["hata"] = str(e)[:80]
                            log.info("Kanal okunamadı %s: %s", ad, e)
                        await asyncio.sleep(1)
                    if yeni and kaydet:
                        kaydet()
                    self.status = (f"bağlı ({self.hesap}) · {len(self.kanallar)} kanal · "
                                   f"son okuma {time.strftime('%H:%M:%S', time.gmtime(time.time() + 3 * 3600))}")
            except Exception as e:
                self.status = f"bağlantı hatası: {str(e)[:70]}"
                log.warning("Dinleyici hatası: %s", e)
                try:
                    if self.client:
                        await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                await asyncio.sleep(60)
            try:
                await asyncio.wait_for(self._tetik.wait(), timeout=ARALIK)
            except asyncio.TimeoutError:
                pass
            self._tetik.clear()

    async def _grup_mu(self, ad, k):
        """Kanal mı grup mu? Grupsa yönetici id'leri (sadece onların mesajları sinyal sayılır)."""
        if "tur" not in k:
            ent = await self.client.get_entity(ad)
            k["tur"] = "grup" if getattr(ent, "megagroup", False) or getattr(ent, "gigagroup", False) else "kanal"
        if k["tur"] != "grup":
            return None
        t, ids = self._yon.get(ad, (0, None))
        if time.time() - t > YONETICI_YENILE:
            try:
                ids = {u.id for u in await self.client.get_participants(ad, filter=ChannelParticipantsAdmins)}
            except Exception as e:
                log.info("Yönetici listesi alınamadı %s: %s", ad, e)
                ids = None
            self._yon[ad] = (time.time(), ids)
        return ids if ids is not None else set()

    async def _oku(self, ad):
        k = self.kanallar[ad]
        yonetici = await self._grup_mu(ad, k)     # None: kanal; küme: grup (boşsa liste alınamadı)
        son = int(k.get("son") or 0)
        ilk = son == 0
        msgs = await self.client.get_messages(ad, limit=(200 if yonetici is not None else 40) if ilk else 60,
                                              min_id=son)
        msgs = sorted([m for m in msgs if m and m.id > son], key=lambda m: m.id)
        n = 0
        for m in msgs:
            k["son"] = max(int(k.get("son") or 0), m.id)
            ts = int(m.date.timestamp()) if m.date else int(time.time())
            if ilk and time.time() - ts > GERI:
                continue
            kim = ad
            if yonetici is not None:
                # Grup: herkes yazabilir. Sadece yöneticilerin (ya da grup adına yazılan) mesajları sinyal sayılır.
                sid = getattr(m, "sender_id", None)
                grup_adina = getattr(m, "post", False) or (sid is not None and sid < 0)
                if not grup_adina and sid not in yonetici:
                    if not yonetici:
                        k["uyari"] = "yönetici listesi alınamadı: grup mesajları okunmuyor"
                    continue
                k.pop("uyari", None)
            text = m.message or ""
            if not text.strip():
                k["resim"] = int(k.get("resim") or 0) + 1        # sadece resim: metin yok, okunamaz
                continue
            if self.on_msg:
                try:
                    if await self.on_msg({"text": text, "date": ts, "chat": {"id": None},
                                          "forward_origin": {"type": "channel", "chat": {"username": kim},
                                                             "message_id": m.id, "date": ts},
                                          "_oto": True, "_eski": ilk}):
                        n += 1
                        k["n"] = int(k.get("n") or 0) + 1
                except Exception as e:
                    log.info("Kanal mesajı işlenemedi %s/%s: %s", ad, m.id, e)
        k["t"] = int(time.time())
        k.pop("hata", None)
        return n or (1 if msgs else 0)

    # ------------------------------------------------------------------ giriş (oturum anahtarı üretme)
    async def giris_kod(self, tel):
        if not self.hazir:
            return {"hata": "Önce Render'a TG_API_ID ve TG_API_HASH girilmeli (my.telegram.org)."
                    if TELETHON else "requirements.txt'e telethon eklenmeli."}
        tel = re.sub(r"[^\d+]", "", tel or "")
        if len(tel) < 10:
            return {"hata": "Telefon numarasını ülke koduyla yaz: +905..."}
        await self._giris_kapat()
        c = TelegramClient(StringSession(), self.api_id, self.api_hash, device_model="ABD-BOT", app_version="1.0",
                           receive_updates=False)
        await c.connect()
        try:
            sent = await c.send_code_request(tel)
        except FloodWaitError as e:
            await c.disconnect()
            return {"hata": f"Telegram çok fazla deneme dedi, {e.seconds} sn sonra tekrar dene."}
        except Exception as e:
            await c.disconnect()
            return {"hata": f"Kod gönderilemedi: {str(e)[:120]}"}
        self._giris = {"c": c, "tel": tel, "hash": sent.phone_code_hash, "t": time.time()}
        return {"ok": 1, "adim": "kod"}

    async def giris_onay(self, kod=None, sifre=None):
        g = self._giris
        if not g or time.time() - g["t"] > 600:
            await self._giris_kapat()
            return {"hata": "Giriş süresi doldu, baştan başla."}
        c = g["c"]
        try:
            if sifre is not None:
                await c.sign_in(password=sifre)
            else:
                await c.sign_in(phone=g["tel"], code=re.sub(r"\D", "", str(kod or "")), phone_code_hash=g["hash"])
        except SessionPasswordNeededError:
            return {"ok": 1, "adim": "sifre"}
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            return {"hata": "Kod hatalı ya da süresi geçmiş."}
        except PasswordHashInvalidError:
            return {"hata": "İki adımlı doğrulama şifresi hatalı."}
        except Exception as e:
            return {"hata": f"Giriş olmadı: {str(e)[:120]}"}
        oturum = c.session.save()
        me = await c.get_me()
        await self._giris_kapat()
        return {"ok": 1, "adim": "tamam", "oturum": oturum,
                "hesap": ("@" + me.username) if getattr(me, "username", None) else (me.first_name or "")}

    async def _giris_kapat(self):
        g, self._giris = self._giris, None
        if g:
            try:
                await g["c"].disconnect()
            except Exception:
                pass


GIRIS_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kanal dinleyici girişi</title>
<style>
:root{--bg:#0f1115;--fg:#e8eaed;--mut:#9aa0a6;--ac:#4f8cff;--kart:#181b21;--cizgi:#2a2f38;--uyari:#ffb547}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:520px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:20px;margin:0 0 6px}.mut{color:var(--mut);font-size:13px}
.kart{background:var(--kart);border:1px solid var(--cizgi);border-radius:12px;padding:14px;margin:14px 0}
input{width:100%;box-sizing:border-box;background:#0b0d11;color:var(--fg);border:1px solid var(--cizgi);border-radius:8px;
padding:11px;font-size:16px;margin:8px 0}
button{background:var(--ac);color:#fff;border:0;border-radius:8px;padding:11px 16px;font-size:15px;width:100%}
.uyari{border-color:var(--uyari)}.uyari b{color:var(--uyari)}
textarea{width:100%;box-sizing:border-box;height:120px;background:#0b0d11;color:var(--fg);border:1px solid var(--cizgi);
border-radius:8px;padding:10px;font:12px monospace;word-break:break-all}
ol{padding-left:20px;margin:6px 0}.gizli{display:none}#msg{min-height:20px;color:var(--uyari)}
</style></head><body><main>
<h1>Kanal dinleyici girişi</h1>
<div class="mut">Bot, herkese açık Telegram kanallarını bu hesapla otomatik okur. Sadece okur; mesaj göndermez.</div>
<div class="kart uyari"><b>Önemli:</b> Sonunda verilecek oturum anahtarı, bu Telegram hesabına tam erişim sağlar.
Sadece Render Environment'a gir; sohbete, GitHub'a ya da başka bir yere yazma. Mümkünse ayrı bir yedek hesap kullan.
Hesabın Ayarlar → Cihazlar bölümünde "ABD-BOT" görünecek; oradan istediğin an kapatabilirsin.</div>
<div class="kart" id="a1"><b>1. Telefon numarası</b><div class="mut">Telegram uygulamana bir giriş kodu gelecek.</div>
<input id="tel" type="tel" placeholder="+905xxxxxxxxx" autocomplete="tel"><button onclick="git('kod')">Kod gönder</button></div>
<div class="kart gizli" id="a2"><b>2. Telegram'dan gelen kod</b>
<input id="kod" inputmode="numeric" placeholder="12345" autocomplete="one-time-code"><button onclick="git('onay')">Onayla</button></div>
<div class="kart gizli" id="a3"><b>3. İki adımlı doğrulama şifresi</b>
<input id="sifre" type="password" autocomplete="current-password"><button onclick="git('sifre')">Giriş yap</button></div>
<div class="kart gizli" id="a4"><b>Giriş tamam <span id="hesap"></span></b>
<ol><li>Aşağıdaki anahtarı kopyala.</li><li>Render → abdbot → Environment → <b>TG_SESSION</b> adıyla ekle, kaydet.</li>
<li>Render kendiliğinden yeniden başlar. Sonra Telegram'da <b>/kanalekle @kanaladi</b> yaz.</li></ol>
<textarea id="oturum" readonly></textarea><button onclick="kopyala()">Kopyala</button>
<div class="mut" style="margin-top:8px">Bu sayfayı kapatınca anahtar bir daha gösterilmez; hiçbir yere kaydedilmedi.</div></div>
<div id="msg"></div>
<div class="mut">Durum: <span id="dur">__DURUM__</span></div>
</main><script>
const $=i=>document.getElementById(i);
async function git(adim){$('msg').textContent='Bekle...';
 const b={adim};if(adim==='kod')b.tel=$('tel').value;if(adim==='onay')b.kod=$('kod').value;if(adim==='sifre')b.sifre=$('sifre').value;
 try{const r=await fetch('/api/tg-giris',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
  const j=await r.json();if(j.hata){$('msg').textContent=j.hata;return}$('msg').textContent='';
  if(j.adim==='kod'){$('a2').classList.remove('gizli');$('kod').focus()}
  if(j.adim==='sifre'){$('a3').classList.remove('gizli');$('sifre').focus()}
  if(j.adim==='tamam'){['a1','a2','a3'].forEach(i=>$(i).classList.add('gizli'));$('a4').classList.remove('gizli');
   $('oturum').value=j.oturum;$('hesap').textContent=j.hesap?('('+j.hesap+')'):'';$('sifre').value='';$('kod').value=''}
 }catch(e){$('msg').textContent='Hata: '+e.message}}
async function kopyala(){try{await navigator.clipboard.writeText($('oturum').value);$('msg').textContent='Kopyalandı.'}
 catch(e){$('oturum').select();document.execCommand('copy');$('msg').textContent='Kopyalandı.'}}
</script></body></html>"""
