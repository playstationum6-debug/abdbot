"""
Bot: sinyalleri seçer, Alpaca PAPER (sanal) hesabında işlem yapar, pozisyonları yönetir.
Gerçek para YOK — sadece paper-api.alpaca.markets kullanılır.

Kurallar:
- Sermaye: bot sadece BOT_CAPITAL (varsayılan 250 $) + gerçekleşen kâr/zarar kadar parayla çalışır.
  Para açık pozisyonlarda bağlıysa yeni hisse almak için önce birinin kapanması gerekir.
- Pozisyon büyüklüğü: işlem başı en fazla BOT_RISK (varsayılan 5 $) risk; pozisyon en fazla BOT_NOTIONAL
  (varsayılan 250 $), uzatılmış seansta yarısı.
- Normal seans: piyasa emri. Uzatılmış seans: sadece limit emir (3 dk dolmazsa iptal).
- Stop, hedef, kâr koruma (+1,5R'de stop girişe), iz süren stop (+1,5R'den sonra 1R geriden),
  seans bitmeden kapatma, günlük zarar limiti, en fazla N açık pozisyon.
- Uzatılmış seansta açığa satış yok; koşan küçük hisselerde sadece alış.
- Öğrenme (trader gibi): kurgu atlanmaz; geçmişi zayıf kurgu küçük lotla, iyi kurgu büyük lotla alınır
  (0,5×–1,5× risk). Üst üste 3 kayıpta lot yarıya iner, ilk kazançta normale döner. Her işleme ders yazılır.
- Çıkışları bot kendisi yönetir (Alpaca uzatılmış seansta stop emri kabul etmediği için).
"""
import asyncio
import math
import re
import time

from denetci import BEKLE_SN, ONAY_R
from datetime import datetime, timezone

import httpx

MIN_TRADE_USD = 20.0   # boşta kalan para bundan azsa yeni işlem açılmaz
CIKIS = {   # çıkış kuralları (çıkış laboratuvarı ile aynı): yarım kapat, kâr koruma, iz süren stop, hedef (R; None: sinyalin hedefi, 0: yok)
    "A": {"half": 0, "be": 0, "trail": 0, "hr": None, "ad": "sabit hedef"},
    "B": {"half": 1, "be": 1, "trail": 1, "hr": None, "ad": "K1 yarı + maliyet + iz"},
    "C": {"half": 0, "be": 1, "trail": 1, "hr": 0, "ad": "iz süren stop"},
    "D": {"half": 0, "be": 0, "trail": 0, "hr": 1, "ad": "hızlı K1"},
}
BE_R = 1.5   # kâr koruma: bu kadar R kâra ulaşınca stop girişe çekilir (1R çok erkendi)

PAPER = "https://paper-api.alpaca.markets"
OPEN_ST = ("emir", "açık", "çıkış")


def _half_total(pos):
    """Yarım kapat yapıldıysa sonuç: cebe konan kısım + kalan kısım (R ve % başlangıçtaki toplam adede göre)."""
    h = pos.get("half") or {}
    if h.get("st") != "doldu" or not pos.get("entry"):
        return
    q0 = float(pos.get("qty0") or (float(pos["qty"]) + h["qty"]))
    tot = round((pos["pnl"] or 0) + h["pnl"], 2)
    pos["pnl"] = tot
    pos["pct"] = round(tot / (pos["entry"] * q0) * 100, 3) if q0 else pos.get("pct")
    risk = pos.get("risk") or abs(pos["entry"] - pos.get("init_stop", pos["stop"]))
    if risk and q0:
        pos["r"] = round(tot / (risk * q0), 2)


def _iso_ts(s):
    """'2026-10-08T14:30:00.123456789Z' → epoch saniye (bozuksa 0)."""
    try:
        main, _, frac = s.rstrip("Z").partition(".")
        dt = datetime.fromisoformat(main)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() + (float("0." + frac[:6]) if frac[:6].isdigit() else 0.0)
    except Exception:
        return 0.0


def px_round(p):
    return round(p, 2) if p >= 1 else round(p, 4)


class Bot:
    def __init__(self, engine, learner, store, cal, et, key, secret, log, cfg):
        self.engine = engine
        self.learner = learner
        self.store = store
        self.cal = cal
        self.ET = et
        self.key = key
        self.secret = secret
        self.log = log
        self.cfg = cfg
        self.positions = []
        self.journal = []
        self.on_note = None      # app.py: canlı akışa mesaj düşürmek için
        self.extra_mult = None   # app.py: piyasa havası / bilanço günü lot çarpanı → (çarpan, not)
        self.sector_of = None    # app.py: sembol → sektör adı (sektör yoğunluğu sınırı için)
        self.engel = None        # app.py: zaman → engel sebebi (ekonomik veri saati) ya da None
        self.ver = 0
        self.dirty_days = set()
        self.seen = set()
        self.bekleyen = {}       # Karar Denetçisi BEKLE dedi: sinyal kimliği -> {sig, until, why}
        self.account = {}
        self.status = "kapalı (Alpaca anahtarı yok)" if not (key and secret) else ("kapalı (BOT_ENABLED=0)" if not cfg["enabled"] else "başlıyor")
        self.client = None
        self.started = time.time()
        self._last_acct = 0.0
        self._last_poll = {}
        self._lt = {}          # sembol -> (fiyat, zaman): Alpaca son işlem (IEX) — açık pozisyonlar için

    # ------------------------------------------------------------ yardımcılar
    @property
    def active(self):
        return bool(self.key and self.secret and self.cfg["enabled"])

    def _day(self, t=None):
        return datetime.fromtimestamp(t or time.time(), self.ET).date().isoformat()

    def note(self, typ, msg, sym=None, t=None):
        t = t or time.time()
        day = self._day(t)
        self.journal.append({"t": int(t), "day": day, "typ": typ, "sym": sym, "msg": msg})
        if len(self.journal) > 4000:
            self.journal = self.journal[-4000:]
        self.dirty_days.add(day)
        self.ver += 1
        try:
            self.log.info("BOT %s %s %s", typ, sym or "", msg)
        except Exception:
            pass
        if self.on_note:
            try:
                self.on_note(typ, msg, sym, t)
            except Exception:
                pass

    def _touch(self, pos):
        self.dirty_days.add(pos["day"])
        self.ver += 1

    def price(self, sym):
        best = (None, 0)
        live = self.store.live.get(sym)
        if live:
            best = (live["p"], live["t"])
        bars = self.store.bars.get(sym) or {}
        if bars:
            t = max(bars)
            if t + 60 > best[1]:
                best = (bars[t][3], min(t + 60, time.time()))
        lt = self._lt.get(sym)
        if lt and lt[1] > best[1]:
            best = lt
        return best

    async def _refresh_latest(self, sym, now):
        """Canlı akışı olmayan hisselerde (koşan küçük hisseler) son işlem fiyatını 5 sn'de bir çek."""
        lt = self._lt.get(sym)
        if lt and now - lt[2] < 5:
            return
        try:
            c = await self._cli()
            r = await c.get(f"https://data.alpaca.markets/v2/stocks/{sym}/trades/latest", params={"feed": "iex"})
            if r.status_code == 200:
                tr = (r.json() or {}).get("trade") or {}
                p = float(tr.get("p") or 0)
                t = _iso_ts(tr.get("t", ""))
                if p > 0:
                    self._lt[sym] = (p, min(t, now) if t else now, now)
                    return
        except Exception:
            pass
        self._lt[sym] = (lt[0], lt[1], now) if lt else (None, 0, now)

    def open_positions(self):
        return [p for p in self.positions if p["st"] in OPEN_ST]

    def today_closed(self):
        d = self._day()
        return [p for p in self.positions if p["st"] == "kapandı" and p.get("pnl") is not None and self._day(p["xt"]) == d]

    def capital(self):
        """(toplam sermaye, açık pozisyonlarda bağlı para, boşta kalan) — $."""
        total = self.cfg.get("capital", 250.0) + sum(p["pnl"] for p in self.positions
                                                     if p["st"] == "kapandı" and p.get("pnl") is not None)
        used = sum(abs(p["qty"]) * (p.get("entry") or p.get("plan") or 0) for p in self.open_positions())
        return total, used, max(0.0, total - used)

    def loss_streak(self):
        """Bugün kapanan işlemlerde sondan geriye üst üste kayıp sayısı."""
        n = 0
        for p in sorted(self.today_closed(), key=lambda p: p.get("xt") or 0, reverse=True):
            if p["pnl"] < 0:
                n += 1
            else:
                break
        return n

    def today_pnl(self):
        return sum(p["pnl"] for p in self.today_closed())

    async def _cli(self):
        if self.client is None:
            self.client = httpx.AsyncClient(base_url=PAPER, timeout=15, headers={
                "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})
        return self.client

    async def _req(self, method, path, **kw):
        c = await self._cli()
        r = await c.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except Exception:
                msg = r.text
            raise RuntimeError(f"{r.status_code}: {str(msg)[:140]}")
        return r.json() if r.content else {}

    # ------------------------------------------------------------ karar
    def consider(self, sig, now, onayli=False):
        if not onayli:
            if sig["id"] in self.seen:
                return
            self.seen.add(sig["id"])
        if not self.active or (not onayli and now - sig.get("created", 0) > 180) or sig["st"] not in ("bekliyor", "açık"):
            return
        sym, d, ses, conf = sig["sym"], sig["dir"], sig["ses"], sig["conf"]
        yon = "AL" if d > 0 else "SAT"
        head = f"{yon} {sig['setup']} güven {conf}"

        def skip(why):
            self.note("atla", f"{head} — atlandı: {why}", sym)

        if self.cal.session(now) == "closed":
            return skip("seans kapalı")
        if self.engel:
            try:
                eng_ = self.engel(now)
            except Exception:
                eng_ = None
            if eng_:
                return skip(eng_)
        if not self.cfg.get("yeni_islem", 1):
            return skip("yeni işlem açma duraklatıldı (Ayarlar)")
        lim = self.cfg["daily_loss"]
        if self.today_pnl() <= -lim:
            return skip(f"günlük zarar limiti ({lim:.0f} $) doldu, bugün yeni işlem yok")
        if any(p["sym"] == sym for p in self.open_positions()):
            return skip("bu hissede zaten pozisyon var")
        if len(self.open_positions()) >= self.cfg["max_open"]:
            return skip(f"en fazla {self.cfg['max_open']} açık pozisyon")
        # Sektör yoğunluğu: aynı sektörde en fazla N açık pozisyon (piyasa tek yöne sert giderse hepsi birden yanmasın)
        sec = self.sector_of(sym) if self.sector_of else None
        lim_s = int(self.cfg.get("sektor_max", 2) or 0)
        if sec and lim_s > 0:
            same = [p for p in self.open_positions() if self.sector_of(p["sym"]) == sec]
            if len(same) >= lim_s:
                return skip(f"{sec} sektöründe zaten {len(same)} açık pozisyon var (sınır {lim_s})")
        if sig["setup"] in (self.cfg.get("kapali") or []):
            return skip("bu kurgu Ayarlar'dan kapatıldı (sinyal öğrenme için takip ediliyor)")
        golge = self.golge(sig["setup"]) if getattr(self, "golge", None) else None
        if golge:
            return skip(golge)
        if d < 0 and ses != "regular":
            return skip("uzatılmış seansta açığa satış yapılmıyor")
        if d < 0 and sig.get("runner") and sig["setup"] != "fade":
            return skip("koşan küçük hisselerde sadece alış (dönüş kurgusu hariç)")
        thr = self.cfg["min_conf"] if ses == "regular" else self.cfg.get("min_conf_ext", self.cfg["min_conf"])
        # Eşik, kuralların ham güvenine bakar (öğrenme işlem sayısını düşürmez, sadece lotu ayarlar)
        conf0 = sig.get("conf0", conf)
        if conf0 < thr:
            return skip(f"güven {conf0} < eşik {thr}")
        # Karar Denetçisi: 5 bağımsız kontrol → AL / BEKLE / PAS GEÇ (sinyal her durumda gölgede takip edilir)
        den = sig.get("den")
        den_lot = 1.0
        if den and sig.get("setup") == "kirilim" and den["karar"] in ("PAS", "BEKLE") \
                and not any(v.get("k") == "veto" for v in (den.get("mod") or {}).values()):
            # kırılım planında onay (hacim + mum kapanışı) zaten alındı; sadece VETO (seyreltme, spread, zarar hakkı) durdurur
            den = dict(den, karar="AL")
        if den and self.cfg.get("denetci", 1) and not onayli:
            if den["karar"] == "PAS":
                return skip("Karar Denetçisi PAS GEÇ — " + den["neden"])
            if den["karar"] == "BEKLE":
                self.bekleyen[sig["id"]] = {"sig": sig, "until": now + BEKLE_SN, "why": den["neden"]}
                self.note("bekle", f"{head} — BEKLE: {den['neden']}. Fiyat girişin ötesine geçerse alacağım "
                                   f"({BEKLE_SN // 60} dk)", sym)
                return
        if den and self.cfg.get("denetci", 1):
            den_lot = den.get("lot", 1.0)
        # Yapay zekâ: karar yetkisi varsa (karnesi kanıtlandı ya da Ayarlar'dan açıldı) kararını bekle / uygula
        if getattr(self, "yz_kapi", None):
            try:
                yk = self.yz_kapi(sig)
            except Exception:
                yk = None
            if yk == "bekle":
                self.note("yz", f"{head} — yapay zekâ değerlendiriyor (internet araştırması + karar, en fazla 45 sn)", sym)
                return
            if yk and yk.startswith("PAS"):
                return skip("Yapay zekâ PAS GEÇ — " + yk[4:])
        # Öğrenme filtresi (denetçi kapalıysa eski davranış): model bu sinyali zayıf dilimde görüyorsa işlem açma.
        _veto = getattr(self.learner, "veto", None)
        if _veto and not (den and self.cfg.get("denetci", 1)) and self.cfg.get("ogren_filtre", 1) and not sig.get("runner"):
            try:
                v, vwhy = _veto(sig["setup"], ses, sig.get("f"))
            except Exception:
                v, vwhy = False, ""
            if v:
                return skip(vwhy)
        n, e = self.learner.group(sig["setup"], ses)
        # Trader gibi: lotu modele ve geçmişe göre ayarla
        try:
            mult, mwhy = self.learner.size_mult(sig["setup"], ses, conf, sig.get("f"))
        except Exception:
            mult, mwhy = 1.0, ""
        if den_lot != 1.0:
            mult *= den_lot
            mwhy = (mwhy + ", " if mwhy else "") + f"denetçi lotu ×{den_lot:g}"
        if onayli and not sig.get("_yz_tekrar"):
            mwhy = (mwhy + ", " if mwhy else "") + "BEKLE sonrası fiyat onayı geldi"
        y_ = sig.get("yz") or {}
        if y_.get("karar") == "AL":
            mwhy = (mwhy + ", " if mwhy else "") + f"yapay zekâ AL (olasılık %{y_.get('p')}, eşik %{y_.get('esik')})"
        if self.extra_mult:
            try:
                em, enote = self.extra_mult(sig)
                if em != 1.0:
                    mult *= em
                    mwhy = (mwhy + ", " if mwhy else "") + enote
            except Exception:
                pass
        streak = self.loss_streak()
        if streak >= 3:
            mult *= 0.5
            mwhy = (mwhy + ", " if mwhy else "") + f"üst üste {streak} kayıp: disiplin, lot yarıya"
        notional = self.cfg["notional"] * (1 if ses == "regular" else 0.5)
        px = sig["e"]
        # Risk bazlı büyüklük: stop mesafesi ne olursa olsun işlem başı kayıp en fazla risk_usd
        risk_px = abs(sig["e"] - sig["s"])
        risk_usd = self.cfg.get("risk_usd", 5.0) * (1 if ses == "regular" else 0.5) * mult
        if px > 0 and risk_px > 0:
            notional = min(notional, risk_usd / risk_px * px)
        # Sermaye sınırı: elde kalan nakitten fazlasıyla pozisyon açılamaz
        cap, used, free = self.capital()
        yer = None
        if free < max(MIN_TRADE_USD, notional * 0.6) and self.cfg.get("yer_ac", 1) \
                and conf >= int(self.cfg.get("yer_min_guven", 85)) and ses == "regular":
            # Güçlü sinyale yer aç: kârda olmayan, güveni daha düşük en zayıf pozisyon kapatılır
            adaylar = []
            for p in self.open_positions():
                if p["st"] != "açık" or not p.get("entry") or not p.get("risk") or (p.get("half") or {}).get("st") == "doldu":
                    continue
                if now - (p.get("et") or p.get("opened") or now) < 20 * 60:
                    continue                      # yeni açılmış pozisyonu hemen kapatma (al-sat-al masrafı)
                px_ = self.price(p["sym"])[0] or p["entry"]
                ur = p["dir"] * (px_ - p["entry"]) / p["risk"]
                if ur <= 0.3 and (p.get("conf") or 0) + 10 <= conf:
                    adaylar.append((ur, p.get("conf") or 0, p, px_))
            if adaylar:
                ur, _, yer, px_ = min(adaylar, key=lambda a: (a[0], a[1]))
                free += abs(yer["qty"]) * px_
        if free < MIN_TRADE_USD:
            return skip(f"sermaye dolu: {cap:.0f} $'ın {used:.0f} $'ı açık pozisyonlarda, önce biri kapanmalı")
        notional = min(notional, free)
        whole = int(math.floor(notional / px)) if px > 0 else 0
        qty = whole
        # Normal seansta alışlarda kesirli adet (pahalı hisseler 250 $ ile de alınabilsin).
        # Açığa satış ve uzatılmış seansta sadece tam adet.
        if d > 0 and ses == "regular" and (whole < 1 or (notional - whole * px) / notional > 0.15):
            qty = round(notional / px, 4)
        if qty <= 0 or (qty < 1 and not (d > 0 and ses == "regular")):
            return skip(f"fiyat ({px:.2f} $) pozisyon büyüklüğünden ({notional:.0f} $) yüksek"
                        + (" — açığa satışta kesirli adet yok" if d < 0 else " — uzatılmış seansta kesirli adet yok"))
        why = (f"güven {conf}" + (f", geçmiş {n} işlem beklenti {e:+.2f}R" if n else ", keşif (henüz geçmiş yok)")
               + f", lot ×{mult:.2f}" + (f" ({mwhy})" if mwhy else ""))
        pos = {
            "id": f"P-{sig['id']}", "sig": sig["id"], "sym": sym, "dir": d, "setup": sig["setup"], "ses": ses,
            "runner": bool(sig.get("runner")), "conf": conf, "qty": qty, "notional": round(qty * px, 2),
            "plan": px, "stop": sig["s"], "init_stop": sig["s"], "target": sig["h"],
            "mult": abs(sig["h"] - sig["e"]) / abs(sig["e"] - sig["s"]) if sig["e"] != sig["s"] else 2.0,
            "end": sig["end"], "st": "emir", "oid": None, "entry": None, "et": None, "hw": None, "be": False,
            "trail": False, "xoid": None, "xr": None, "exit": None, "xt": None, "pnl": None, "r": None,
            "pct": None, "lesson": "", "opened": int(now), "day": self._day(now), "f": sig.get("f") or {},
            "tries": 0, "stale_noted": False, "why": why,
            "cikis": (self.cikis() if callable(getattr(self, "cikis", None)) else "B"),
        }
        self.positions.append(pos)
        self.note("al", f"{head} — alındı ({why}); {qty:g} adet ≈ {qty * px:.0f} $", sym)
        self._touch(pos)
        if yer:
            yer["st"], yer["xoid"], yer["xr"] = "çıkış", None, "yer açma"
            self.note("yer", f"{sym} (güven {conf}) için yer açıldı: kârda olmayan bu pozisyon kapatılıyor", yer["sym"])
            self._touch(yer)

    # ------------------------------------------------------------ emirler
    async def _submit_entry(self, pos):
        side = "buy" if pos["dir"] > 0 else "sell"
        body = {"symbol": pos["sym"], "qty": str(pos["qty"]), "side": side, "time_in_force": "day",
                "client_order_id": f"abd-{pos['sym']}-{pos['opened']}-{pos['dir']}"[:48]}
        if pos["ses"] == "regular":
            body["type"] = "market"
        else:
            body.update(type="limit", limit_price=str(px_round(pos["plan"])), extended_hours=True)
        try:
            o = await self._req("POST", "/v2/orders", json=body)
            pos["oid"] = o["id"]
            pos["sent"] = time.time()
            self.note("emir", f"Giriş emri gönderildi: {side} {pos['qty']:g} {body['type']}"
                              + (f" @ {body.get('limit_price')}" if body.get("limit_price") else ""), pos["sym"])
        except Exception as e:
            pos["st"] = "iptal"
            pos["xr"] = f"emir reddedildi: {e}"
            self.note("hata", f"Giriş emri reddedildi: {e}", pos["sym"])
        self._touch(pos)

    async def _submit_exit(self, pos, px, reason):
        side = "sell" if pos["dir"] > 0 else "buy"
        body = {"symbol": pos["sym"], "qty": str(pos["qty"]), "side": side, "time_in_force": "day"}
        ses_now = self.cal.session(time.time())
        if ses_now == "regular":
            body["type"] = "market"
        else:
            off = 0.005 * (1 + pos["tries"])          # her denemede daha agresif limit
            lp = px * (1 - pos["dir"] * off)
            body.update(type="limit", limit_price=str(px_round(lp)), extended_hours=True)
        try:
            o = await self._req("POST", "/v2/orders", json=body)
            pos["xoid"] = o["id"]
            pos["xsent"] = time.time()
            pos["st"] = "çıkış"
            pos["xr"] = reason
            self.note("çıkış", f"Çıkış emri ({reason}): {side} {pos['qty']:g} {body['type']}"
                               + (f" @ {body.get('limit_price')}" if body.get("limit_price") else ""), pos["sym"])
        except Exception as e:
            now = time.time()
            if "insufficient qty" in str(e) and now - pos.get("xfix_t", 0) > 20:
                # Hisselerin bir kısmı başka açık emirde tutuluyor (ör. yeniden başlamada kalan yarım-kapat emri)
                # ya da Alpaca'daki adet farklı: açık emirleri iptal et, gerçek adedi al, bir kez daha dene.
                pos["xfix_t"] = now
                try:
                    acik = await self._req("GET", "/v2/orders", params={"status": "open", "symbols": pos["sym"]})
                    for o_ in acik or []:
                        try:
                            await self._req("DELETE", f"/v2/orders/{o_['id']}")
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    ap = await self._req("GET", f"/v2/positions/{pos['sym']}")
                    q = abs(float(ap.get("qty_available") or ap.get("qty") or 0))
                    if q > 0:
                        if q != float(pos["qty"]):
                            self.note("senkron", f"Adet Alpaca ile eşitlendi: {pos['qty']:g} → {q:g} "
                                                 f"({len(acik or [])} açık emir iptal edildi)", pos["sym"])
                            pos["qty"] = int(q) if q == int(q) else q
                        body["qty"] = str(pos["qty"])
                        o = await self._req("POST", "/v2/orders", json=body)
                        pos["xoid"] = o["id"]
                        pos["xsent"] = time.time()
                        pos["st"] = "çıkış"
                        pos["xr"] = reason
                        self.note("çıkış", f"Çıkış emri ({reason}): {side} {pos['qty']:g} {body['type']} (yeniden denendi)", pos["sym"])
                        return
                except Exception as e2:
                    e = e2
            if now - pos.get("xerr_t", 0) > 60:       # aynı hatayı her saniye yazma
                pos["xerr_t"] = now
                self.note("hata", f"Çıkış emri gönderilemedi: {e}", pos["sym"])
        self._touch(pos)

    # ------------------------------------------------------------ yarım kapat (kârın bir kısmını cebe koy)
    def _half_qty(self, pos, px):
        q = pos["qty"]
        if isinstance(q, int) or float(q) == int(q):
            q = int(q)
            if q >= 2:
                return q // 2
            if pos["dir"] < 0:
                return None                       # açığa satışta kesirli adet yok
        hq = round(float(q) / 2, 4)
        return hq if pos["dir"] > 0 and hq * px >= 2.0 else None

    async def _submit_half(self, pos, hq, px):
        side = "sell" if pos["dir"] > 0 else "buy"
        body = {"symbol": pos["sym"], "qty": str(hq), "side": side, "time_in_force": "day", "type": "market"}
        try:
            o = await self._req("POST", "/v2/orders", json=body)
            pos["half"] = {"oid": o["id"], "qty": hq, "st": "emir", "t": int(time.time())}
            self.note("emir", f"Yarım kapat emri: {side} {hq:g} adet (kârın bir kısmı cebe)", pos["sym"])
        except Exception as e:
            pos["half"] = {"st": "olmadı"}
            self.note("hata", f"Yarım kapat emri gönderilemedi: {e}", pos["sym"])
        self._touch(pos)

    async def _poll_half(self, pos, now):
        h = pos["half"]
        if now - self._last_poll.get(h["oid"], 0) < 3:
            return
        self._last_poll[h["oid"]] = now
        o = await self._poll(h["oid"])
        st = o.get("status")
        fq = float(o.get("filled_qty") or 0)
        if st == "filled" or (st in ("canceled", "expired") and fq > 0):
            d = pos["dir"]
            px = float(o["filled_avg_price"])
            pos["qty0"] = pos.get("qty0") or pos["qty"]
            rest = round(float(pos["qty"]) - fq, 6)
            pos["qty"] = int(rest) if rest == int(rest) else rest
            pnl = round(d * (px - pos["entry"]) * fq, 2)
            h.update(st="doldu", px=px, qty=fq, pnl=pnl, xt=int(now))
            be = pos["entry"] + d * pos["entry"] * 0.0005
            if (be - pos["stop"]) * d > 0:
                pos["stop"] = be
            pos["be"] = True
            kalan_r = float(self.cfg.get("kalan_r", 3.0) or 0)
            if kalan_r and pos.get("risk"):
                nt = pos["entry"] + d * kalan_r * pos["risk"]
                if (nt - pos["target"]) * d > 0:
                    pos["target"] = nt
            self.note("yarım", f"Yarım kapat: {fq:g} adet @ {px} → {pnl:+.2f} $ cebe. Kalan {pos['qty']:g} adet: "
                               f"stop girişte, hedef {kalan_r:g}R", pos["sym"])
        elif st in ("canceled", "expired", "rejected", "done_for_day"):
            h["st"] = "olmadı"
            self.note("uyarı", f"Yarım kapat emri dolmadı ({st}); pozisyon tam haliyle devam ediyor", pos["sym"])
        self._touch(pos)

    async def _poll(self, oid):
        return await self._req("GET", f"/v2/orders/{oid}")

    # ------------------------------------------------------------ yönetim
    def bekle_kontrol(self, now):
        """BEKLE denen sinyaller: fiyat girişin ONAY_R ötesine geçerse al, stopa değerse ya da süre dolarsa bırak."""
        for sid, b in list(self.bekleyen.items()):
            sig = b["sig"]
            sym, d = sig["sym"], sig["dir"]
            if sig["st"] not in ("bekliyor", "açık"):
                del self.bekleyen[sid]
                continue
            px = self.price(sym)[0]
            risk = abs(sig["e"] - sig["s"]) or 1e-9
            if px and (px - sig["s"]) * d <= 0:
                del self.bekleyen[sid]
                self.note("atla", f"{sym} BEKLE iptal: fiyat stop seviyesine geldi, onay gelmedi", sym)
            elif px and (px - sig["e"]) * d >= ONAY_R * risk:
                if (px - sig["h"]) * d >= -0.5 * risk:
                    del self.bekleyen[sid]
                    self.note("atla", f"{sym} BEKLE iptal: fiyat hedefe çok yaklaştı, geç kalındı", sym)
                    continue
                del self.bekleyen[sid]
                self.note("onay", f"{sym} onay geldi: {px:.4g} girişin ötesinde → AL değerlendiriliyor", sym)
                self.consider(sig, now, onayli=True)
            elif now > b["until"]:
                del self.bekleyen[sid]
                self.note("atla", f"{sym} BEKLE süresi doldu: onay gelmedi ({b['why']})", sym)

    async def manage(self):
        if not self.active:
            return
        now = time.time()
        try:
            self.bekle_kontrol(now)
        except Exception as e:
            self.note("hata", f"Bekleme kontrolü hatası: {str(e)[:100]}")
        if now - self._last_acct > 60:
            self._last_acct = now
            try:
                a = await self._req("GET", "/v2/account")
                self.account = {k: a.get(k) for k in ("equity", "cash", "buying_power", "last_equity", "status")}
                if not self.status.startswith("çalışıyor"):
                    self.status = "çalışıyor"
                    self.ver += 1
            except Exception as e:
                self.status = f"hesap okunamadı: {str(e)[:80]}"
                self.ver += 1
        for pos in self.open_positions():
            try:
                await self._manage_one(pos, now)
            except Exception as e:
                self.note("hata", f"Yönetim hatası: {str(e)[:120]}", pos["sym"])

    async def _manage_one(self, pos, now):
        d = pos["dir"]
        if pos["st"] == "emir":
            if not pos.get("oid"):
                return await self._submit_entry(pos)
            if now - self._last_poll.get(pos["oid"], 0) < 3:
                return
            self._last_poll[pos["oid"]] = now
            o = await self._poll(pos["oid"])
            st = o.get("status")
            fq = float(o.get("filled_qty") or 0)
            if st == "filled" or (st in ("canceled", "expired") and fq > 0):
                self._filled(pos, o, now)
            elif st in ("canceled", "expired", "rejected", "done_for_day"):
                pos["st"] = "iptal"
                pos["xr"] = "emir dolmadı" if st != "rejected" else f"emir reddedildi ({o.get('status')})"
                self.note("iptal", f"Giriş emri {pos['xr']}", pos["sym"])
                self._touch(pos)
            else:
                limit = 60 if pos["ses"] == "regular" else 180
                if now - pos.get("sent", now) > limit and not pos.get("cancel_sent"):
                    pos["cancel_sent"] = True
                    try:
                        await self._req("DELETE", f"/v2/orders/{pos['oid']}")
                    except Exception:
                        pass
            return

        if pos["st"] == "açık":
            h = pos.get("half")
            if h and h.get("st") == "emir":           # yarım kapat emri bekliyor: sonuçlanana kadar çıkış yok
                return await self._poll_half(pos, now)
            px, pt = self.price(pos["sym"])[:2]
            if now - pt > 20:
                await self._refresh_latest(pos["sym"], now)
                px, pt = self.price(pos["sym"])[:2]
            if px is None:
                return
            if now - pt > 300 and not pos["stale_noted"]:
                pos["stale_noted"] = True
                self.note("uyarı", "5 dk'dır fiyat gelmiyor (işlem durdurma olabilir)", pos["sym"])
            entry, risk = pos["entry"], pos["risk"]
            pr = CIKIS.get(pos.get("cikis") or "B", CIKIS["B"])
            pos["last"] = px
            pos["hw"] = max(pos["hw"], px) if d > 0 else min(pos["hw"], px)
            best_r = (pos["hw"] - entry) * d / risk if risk > 0 else 0
            if pr["be"] and not pos["be"] and best_r >= BE_R:
                pos["be"] = True
                pos["stop"] = entry + d * entry * 0.0005
                self.note("koruma", f"Kâr koruma: +{BE_R:g}R'ye ulaştı, stop girişe çekildi", pos["sym"])
            if pr["trail"] and best_r >= 1.5:
                ns = pos["hw"] - d * risk
                if (ns - pos["stop"]) * d > 0:
                    pos["stop"] = ns
                    if not pos["trail"]:
                        pos["trail"] = True
                        self.note("iz", "İz süren stop devrede (zirvenin 1R gerisinden)", pos["sym"])
                    self.dirty_days.add(pos["day"])
            tgt = pos["target"] if pr["hr"] is None else (None if pr["hr"] == 0 else entry + d * pr["hr"] * risk)
            if pr["half"] and self.cfg.get("yarim_kapat", 1) and not pos.get("half") and \
                    best_r >= float(self.cfg.get("yarim_r", 1.0)) and self.cal.session(now) == "regular" \
                    and (px - pos["stop"]) * d > 0 and (tgt is None or (px - tgt) * d < 0):
                hq = self._half_qty(pos, px)
                if hq:
                    return await self._submit_half(pos, hq, px)
            reason = None
            if (px - pos["stop"]) * d <= 0:
                reason = "iz süren stop" if pos["trail"] else ("kâr koruma" if pos["be"] else "stop")
            elif tgt is not None and (px - tgt) * d >= 0:
                reason = "hedef"
            elif now >= pos["end"] - 180:
                reason = "seans sonu"
            if reason:
                await self._submit_exit(pos, px, reason)
            return

        if pos["st"] == "çıkış":
            if not pos.get("xoid"):
                px = self.price(pos["sym"])[0]
                return await self._submit_exit(pos, px or pos["entry"], pos.get("xr") or "çıkış")
            if now - self._last_poll.get(pos["xoid"], 0) < 3:
                return
            self._last_poll[pos["xoid"]] = now
            o = await self._poll(pos["xoid"])
            st = o.get("status")
            if st == "filled":
                self._closed(pos, float(o["filled_avg_price"]), now)
            elif st in ("canceled", "expired", "rejected", "done_for_day"):
                pos["tries"] += 1
                pos["xoid"] = None
                self.note("uyarı", f"Çıkış emri dolmadı ({st}), daha agresif fiyatla tekrar deneniyor", pos["sym"])
                self._touch(pos)
            elif now - pos.get("xsent", now) > 60 and o.get("type") == "limit" and not pos.get("xcancel"):
                pos["xcancel"] = True
                try:
                    await self._req("DELETE", f"/v2/orders/{pos['xoid']}")
                except Exception:
                    pass

    def _filled(self, pos, o, now):
        d = pos["dir"]
        entry = float(o["filled_avg_price"])
        fq = float(o.get("filled_qty") or pos["qty"])
        pos["qty"] = int(fq) if fq == int(fq) else fq
        pos["entry"] = entry
        pos["et"] = int(now)
        pos["hw"] = entry
        risk = (entry - pos["stop"]) * d
        pos["st"] = "açık"
        self.note("dolum", f"Giriş doldu: {pos['qty']:g} adet @ {entry}", pos["sym"])
        if risk <= 0:
            pos["risk"] = abs(entry * 0.005)
            pos["xoid"] = None
            pos["st"] = "çıkış"
            pos["xr"] = "dolum stopun ötesinde"
            self.note("uyarı", "Dolum fiyatı stopun ötesinde kaldı, hemen çıkılıyor", pos["sym"])
        else:
            pos["risk"] = risk
            pos["target"] = entry + d * pos["mult"] * risk   # hedefi gerçek dolum fiyatına göre yeniden kur
        self._touch(pos)

    def _closed(self, pos, exit_px, now):
        d = pos["dir"]
        pos["exit"] = exit_px
        pos["xt"] = int(now)
        pos["st"] = "kapandı"
        pos["pnl"] = round(d * (exit_px - pos["entry"]) * pos["qty"], 2)
        pos["pct"] = round(d * (exit_px / pos["entry"] - 1) * 100, 3)
        pos["r"] = round(d * (exit_px - pos["entry"]) / pos["risk"], 2) if pos.get("risk") else None
        _half_total(pos)
        try:
            pos["lesson"] = self.learner.lesson(pos["setup"], pos["ses"], pos["f"], pos["r"])
        except Exception:
            pos["lesson"] = ""
        self.note("kapandı", f"Kapandı ({pos['xr']}): {pos['pnl']:+.2f} $ · {pos['r']:+.2f}R", pos["sym"])
        if pos["lesson"]:
            self.note("ders", pos["lesson"], pos["sym"])
        self._touch(pos)

    # ------------------------------------------------------------ açılışta senkron
    async def reconcile(self):
        if not self.active:
            return
        open_ = self.open_positions()
        if not open_:
            return
        try:
            alp = {p["symbol"]: p for p in await self._req("GET", "/v2/positions")}
        except Exception as e:
            self.note("hata", f"Senkron yapılamadı: {e}")
            return
        for pos in open_:
            if pos["st"] in ("açık", "çıkış") and pos["sym"] not in alp:
                pos["st"] = "kapandı"
                pos["xr"] = "senkron: Alpaca'da pozisyon yok"
                pos["xt"] = int(time.time())
                fill = await self._find_exit(pos)
                if fill and pos.get("entry"):
                    px, xt = fill
                    d = pos["dir"]
                    pos["exit"], pos["xt"] = px, xt
                    pos["pnl"] = round((px - pos["entry"]) * d * pos["qty"], 2)
                    risk = abs(pos["entry"] - pos.get("init_stop", pos["stop"])) or 1e-9
                    pos["r"] = round((px - pos["entry"]) * d / risk, 2)
                    pos["pct"] = round((px / pos["entry"] - 1) * 100 * d, 2)
                    _half_total(pos)
                    pos["xr"] = "senkron: Alpaca çıkışı bulundu"
                    try:
                        pos["lesson"] = self.learner.lesson(pos["setup"], pos["ses"], pos["f"], pos["r"])
                    except Exception:
                        pos["lesson"] = ""
                    self.note("kapandı", f"Yeniden başlamada Alpaca'daki çıkış bulundu: {pos['pnl']:+.2f} $ · "
                                         f"{pos['r']:+.2f}R", pos["sym"])
                else:
                    self.note("uyarı", "Yeniden başlamada pozisyon Alpaca'da bulunamadı; kapandı sayıldı (sonuç bilinmiyor)", pos["sym"])
                self._touch(pos)
            elif pos["st"] == "açık":
                pos["stale_noted"] = False
                self.note("senkron", "Yeniden başlama sonrası pozisyon yönetimi devam ediyor", pos["sym"])

    async def _find_exit(self, pos):
        """Kapanmış pozisyonun çıkış emrini Alpaca geçmişinden bul: (fiyat, zaman) ya da None."""
        try:
            after = datetime.fromtimestamp(pos.get("et") or pos["opened"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            orders = await self._req("GET", "/v2/orders", params={
                "status": "closed", "symbols": pos["sym"], "after": after, "direction": "asc", "limit": 50})
            side = "sell" if pos["dir"] > 0 else "buy"
            for o in orders or []:
                if o.get("side") == side and o.get("filled_avg_price") and float(o.get("filled_qty") or 0) > 0:
                    xt = _iso_ts(o.get("filled_at") or o.get("updated_at") or "") or int(time.time())
                    return float(o["filled_avg_price"]), int(xt)
        except Exception as e:
            self.note("hata", f"Çıkış geçmişi okunamadı: {e}", pos["sym"])
        return None

    # ------------------------------------------------------------ kayıt / özet
    def load(self, days):
        for part in days:
            for p in part.get("pos", []):
                if isinstance(p, dict) and "id" in p and not any(q["id"] == p["id"] for q in self.positions):
                    self.positions.append(p)
                    self.seen.add(p.get("sig"))
            self.journal.extend(j for j in part.get("log", []) if isinstance(j, dict))
        self.positions.sort(key=lambda p: p.get("opened", 0))
        self.journal.sort(key=lambda j: j.get("t", 0))
        self.journal = self.journal[-4000:]
        self.ver += 1

    def day_data(self, day):
        return {"pos": [p for p in self.positions if p.get("day") == day],
                "log": [j for j in self.journal if j.get("day") == day]}

    def summary(self):
        closed = [p for p in self.positions if p["st"] == "kapandı" and p.get("pnl") is not None]
        tc = self.today_closed()
        rs = [p["r"] for p in closed if p.get("r") is not None]
        opens = []
        for p in self.open_positions():
            q = dict(p)
            if p["st"] in ("açık", "çıkış") and p.get("entry"):
                px = self.price(p["sym"])[0]
                if px:
                    q["last"] = px
                    q["upnl"] = round(p["dir"] * (px - p["entry"]) * p["qty"], 2)
                    q["ur"] = round(p["dir"] * (px - p["entry"]) / p["risk"], 2) if p.get("risk") else None
            opens.append(q)
        recent = [p for p in self.positions if p["st"] in ("kapandı", "iptal")][-40:][::-1]
        lim = self.cfg["daily_loss"]
        # Bugünkü kararlar: kaç sinyal geldi, kaçı alındı, neden atlandı
        today = self._day()
        tj = [j for j in self.journal if j.get("day") == today]
        reasons = {}
        for j in tj:
            if j["typ"] == "atla" and "atlandı:" in j["msg"]:
                r = j["msg"].split("atlandı:", 1)[1].strip()
                key = re.sub(r"[-+]?\d+([.,]\d+)?", "#", r).split(" (")[0]
                reasons.setdefault(key, [0, r])
                reasons[key][0] += 1
                reasons[key][1] = r
        top = sorted(reasons.values(), key=lambda x: -x[0])[:4]
        dec = {"al": sum(1 for j in tj if j["typ"] == "al"), "atla": sum(1 for j in tj if j["typ"] == "atla"),
               "reasons": [{"n": n, "ornek": ex} for n, ex in top]}
        # Kârlı gün serisi
        by_day = {}
        for p in closed:
            d = self._day(p["xt"])
            by_day[d] = by_day.get(d, 0) + p["pnl"]
        streak = 0
        for d in sorted(by_day, reverse=True):
            if by_day[d] > 0:
                streak += 1
            else:
                break
        return {
            "dec": dec, "streak": streak, "days": len(by_day),
            "left": round(max(0.0, lim + min(0.0, self.today_pnl())), 2),
            "active": self.active, "status": self.status, "cfg": self.cfg, "account": self.account,
            "capital": dict(zip(("total", "used", "free"), (round(x, 2) for x in self.capital()))),
            "today": {"pnl": round(sum(p["pnl"] for p in tc), 2), "n": len(tc), "w": sum(1 for p in tc if p["pnl"] > 0)},
            "total": {"pnl": round(sum(p["pnl"] for p in closed), 2), "n": len(closed),
                      "w": sum(1 for p in closed if p["pnl"] > 0),
                      "avg_r": round(sum(rs) / len(rs), 3) if rs else None},
            "limit_hit": self.today_pnl() <= -lim,
            "open": opens, "recent": recent, "journal": self.journal[-80:][::-1],
            "bekleyen": [{"sym": b["sig"]["sym"], "dir": b["sig"]["dir"], "setup": b["sig"]["setup"], "e": b["sig"]["e"],
                          "s": b["sig"]["s"], "h": b["sig"]["h"], "until": int(b["until"]), "why": b["why"],
                          "conf": b["sig"].get("conf")} for b in self.bekleyen.values()],
            "karne": self.karne(closed),
        }

    def karne(self, closed):
        """Karne: sermaye eğrisi, günlük kâr/zarar takvimi, en iyi/kötü işlemler, kurgu bazında sonuçlar."""
        cl = sorted(closed, key=lambda p: p.get("xt") or 0)
        cap0 = self.cfg.get("capital", 250.0)
        eq, cum = [], 0.0
        for p in cl:
            cum += p["pnl"]
            eq.append([int(p.get("xt") or 0), round(cap0 + cum, 2)])
        days = {}
        for p in cl:
            d = self._day(p.get("xt"))
            x = days.setdefault(d, {"d": d, "pnl": 0.0, "n": 0, "w": 0})
            x["pnl"] += p["pnl"]
            x["n"] += 1
            x["w"] += 1 if p["pnl"] > 0 else 0
        for x in days.values():
            x["pnl"] = round(x["pnl"], 2)
        setups = {}
        for p in cl:
            x = setups.setdefault(p["setup"], {"k": p["setup"], "n": 0, "pnl": 0.0, "w": 0, "rs": 0.0})
            x["n"] += 1
            x["pnl"] += p["pnl"]
            x["w"] += 1 if p["pnl"] > 0 else 0
            x["rs"] += p.get("r") or 0
        st = sorted(({"k": x["k"], "n": x["n"], "pnl": round(x["pnl"], 2), "wr": round(100 * x["w"] / x["n"]),
                      "avg_r": round(x["rs"] / x["n"], 2)} for x in setups.values()), key=lambda x: -x["pnl"])
        slim = lambda p: {k: p.get(k) for k in ("id", "sym", "dir", "setup", "pnl", "r", "xt", "xr")}
        peak, mdd = cap0, 0.0
        for _, v in eq:
            peak = max(peak, v)
            mdd = max(mdd, peak - v)
        return {"eq": eq[-400:], "cap0": cap0, "days": sorted(days.values(), key=lambda x: x["d"])[-62:],
                "best": [slim(p) for p in sorted(cl, key=lambda p: -p["pnl"])[:5]],
                "worst": [slim(p) for p in sorted(cl, key=lambda p: p["pnl"])[:5]],
                "setups": st, "mdd": round(mdd, 2)}
