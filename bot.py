"""
Bot: sinyalleri seçer, Alpaca PAPER (sanal) hesabında işlem yapar, pozisyonları yönetir.
Gerçek para YOK — sadece paper-api.alpaca.markets kullanılır.

Kurallar:
- Pozisyon büyüklüğü: normal seansta BOT_NOTIONAL (varsayılan 250 $), uzatılmış seansta yarısı.
- Normal seans: piyasa emri. Uzatılmış seans: sadece limit emir (3 dk dolmazsa iptal).
- Stop, hedef, kâr koruma (+1R'de stop girişe), iz süren stop (+1,5R'den sonra 1R geriden),
  seans bitmeden kapatma, günlük zarar limiti, en fazla N açık pozisyon.
- Uzatılmış seansta açığa satış yok; koşan küçük hisselerde sadece alış.
- Öğrenme: yeterli veri varsa beklentisi negatif kurgular atlanır; her kapanan işlem için ders yazılır.
- Çıkışları bot kendisi yönetir (Alpaca uzatılmış seansta stop emri kabul etmediği için).
"""
import math
import re
import time
from datetime import datetime, timezone

import httpx

PAPER = "https://paper-api.alpaca.markets"
OPEN_ST = ("emir", "açık", "çıkış")


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
        self.ver = 0
        self.dirty_days = set()
        self.seen = set()
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
    def consider(self, sig, now):
        if sig["id"] in self.seen:
            return
        self.seen.add(sig["id"])
        if not self.active or now - sig.get("created", 0) > 180 or sig["st"] not in ("bekliyor", "açık"):
            return
        sym, d, ses, conf = sig["sym"], sig["dir"], sig["ses"], sig["conf"]
        yon = "AL" if d > 0 else "SAT"
        head = f"{yon} {sig['setup']} güven {conf}"

        def skip(why):
            self.note("atla", f"{head} — atlandı: {why}", sym)

        if self.cal.session(now) == "closed":
            return skip("seans kapalı")
        lim = self.cfg["daily_loss"]
        if self.today_pnl() <= -lim:
            return skip(f"günlük zarar limiti ({lim:.0f} $) doldu, bugün yeni işlem yok")
        if any(p["sym"] == sym for p in self.open_positions()):
            return skip("bu hissede zaten pozisyon var")
        if len(self.open_positions()) >= self.cfg["max_open"]:
            return skip(f"en fazla {self.cfg['max_open']} açık pozisyon")
        if d < 0 and ses != "regular":
            return skip("uzatılmış seansta açığa satış yapılmıyor")
        if d < 0 and sig.get("runner"):
            return skip("koşan küçük hisselerde sadece alış")
        thr = self.cfg["min_conf"] if ses == "regular" else self.cfg.get("min_conf_ext", self.cfg["min_conf"])
        if conf < thr:
            return skip(f"güven {conf} < eşik {thr}")
        n, e = self.learner.group(sig["setup"], ses)
        if n >= 20 and e < 0:
            return skip(f"öğrenilmiş beklenti negatif ({e:+.2f}R, {n} işlem)")
        notional = self.cfg["notional"] * (1 if ses == "regular" else 0.5)
        px = sig["e"]
        whole = int(math.floor(notional / px)) if px > 0 else 0
        qty = whole
        # Normal seansta alışlarda kesirli adet (pahalı hisseler 250 $ ile de alınabilsin).
        # Açığa satış ve uzatılmış seansta sadece tam adet.
        if d > 0 and ses == "regular" and (whole < 1 or (notional - whole * px) / notional > 0.15):
            qty = round(notional / px, 4)
        if qty <= 0 or (qty < 1 and not (d > 0 and ses == "regular")):
            return skip(f"fiyat ({px:.2f} $) pozisyon büyüklüğünden ({notional:.0f} $) yüksek"
                        + (" — açığa satışta kesirli adet yok" if d < 0 else " — uzatılmış seansta kesirli adet yok"))
        why = f"güven {conf}" + (f", geçmiş {n} işlem ort. {e:+.2f}R" if n else ", keşif (henüz geçmiş yok)")
        pos = {
            "id": f"P-{sig['id']}", "sig": sig["id"], "sym": sym, "dir": d, "setup": sig["setup"], "ses": ses,
            "runner": bool(sig.get("runner")), "conf": conf, "qty": qty, "notional": round(qty * px, 2),
            "plan": px, "stop": sig["s"], "init_stop": sig["s"], "target": sig["h"],
            "mult": abs(sig["h"] - sig["e"]) / abs(sig["e"] - sig["s"]) if sig["e"] != sig["s"] else 2.0,
            "end": sig["end"], "st": "emir", "oid": None, "entry": None, "et": None, "hw": None, "be": False,
            "trail": False, "xoid": None, "xr": None, "exit": None, "xt": None, "pnl": None, "r": None,
            "pct": None, "lesson": "", "opened": int(now), "day": self._day(now), "f": sig.get("f") or {},
            "tries": 0, "stale_noted": False, "why": why,
        }
        self.positions.append(pos)
        self.note("al", f"{head} — alındı ({why}); {qty:g} adet ≈ {qty * px:.0f} $", sym)
        self._touch(pos)

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
            self.note("hata", f"Çıkış emri gönderilemedi: {e}", pos["sym"])
        self._touch(pos)

    async def _poll(self, oid):
        return await self._req("GET", f"/v2/orders/{oid}")

    # ------------------------------------------------------------ yönetim
    async def manage(self):
        if not self.active:
            return
        now = time.time()
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
            pos["last"] = px
            pos["hw"] = max(pos["hw"], px) if d > 0 else min(pos["hw"], px)
            best_r = (pos["hw"] - entry) * d / risk if risk > 0 else 0
            if not pos["be"] and best_r >= 1:
                pos["be"] = True
                pos["stop"] = entry + d * entry * 0.0005
                self.note("koruma", "Kâr koruma: +1R'ye ulaştı, stop girişe çekildi", pos["sym"])
            if best_r >= 1.5:
                ns = pos["hw"] - d * risk
                if (ns - pos["stop"]) * d > 0:
                    pos["stop"] = ns
                    if not pos["trail"]:
                        pos["trail"] = True
                        self.note("iz", "İz süren stop devrede (zirvenin 1R gerisinden)", pos["sym"])
                    self.dirty_days.add(pos["day"])
            reason = None
            if (px - pos["stop"]) * d <= 0:
                reason = "iz süren stop" if pos["trail"] else ("kâr koruma" if pos["be"] else "stop")
            elif (px - pos["target"]) * d >= 0:
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
            if pos["st"] == "açık" and pos["sym"] not in alp:
                pos["st"] = "kapandı"
                pos["xr"] = "senkron: Alpaca'da pozisyon yok"
                pos["xt"] = int(time.time())
                self.note("uyarı", "Yeniden başlamada pozisyon Alpaca'da bulunamadı; kapandı sayıldı (sonuç bilinmiyor)", pos["sym"])
                self._touch(pos)
            elif pos["st"] == "açık":
                pos["stale_noted"] = False
                self.note("senkron", "Yeniden başlama sonrası pozisyon yönetimi devam ediyor", pos["sym"])

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
            "today": {"pnl": round(sum(p["pnl"] for p in tc), 2), "n": len(tc), "w": sum(1 for p in tc if p["pnl"] > 0)},
            "total": {"pnl": round(sum(p["pnl"] for p in closed), 2), "n": len(closed),
                      "w": sum(1 for p in closed if p["pnl"] > 0),
                      "avg_r": round(sum(rs) / len(rs), 3) if rs else None},
            "limit_hit": self.today_pnl() <= -lim,
            "open": opens, "recent": recent, "journal": self.journal[-80:][::-1],
        }
