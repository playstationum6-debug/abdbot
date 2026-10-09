"""
Telegram resim kartları (yeni tema): Süper Fırsat, hedef kutlaması, gün sonu, hisse kartı.
------------------------------------------------------------------------------------------
Sadece Pillow. Her kart ~0,1 sn ve birkaç MB geçici bellek; çizim bitince bellek bırakılır.
Yazı tipi grafik.py'den gelir (DejaVu, açılışta bir kez indirilir; yoksa yerleşik yazı tipi).
Emoji resme çizilemez: elmas, ok gibi işaretler şekil olarak çizilir.
"""
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFilter

import grafik

TR = ZoneInfo("Europe/Istanbul")
import gorsel

# v4 paleti (gorsel.py ile aynı): koyu lacivert zemin, yeşil/kırmızı yön, mavi vurgu, altın süper
BG = gorsel.BG
CARD = gorsel.PANEL
LINE = gorsel.GRID
TXT = gorsel.TXT
MUT = gorsel.MUT
UP = gorsel.UP
UP2 = (134, 239, 172)
DN = gorsel.DN
GOLD = gorsel.GOLD
GOLD2 = (253, 224, 140)
BLUE = gorsel.ACC
T = grafik._t


def F(size, bold=False):
    return gorsel.F(size, "b" if bold else "r")


def fp(x):
    if x is None:
        return "—"
    x = float(x)
    nd = 2 if abs(x) >= 10 else 3 if abs(x) >= 1 else 4
    t = f"{x:,.{nd}f}"
    if "." in t:
        a, b = t.split(".")
        b = b.rstrip("0").ljust(2, "0")
        t = a + "." + b
    return t.replace(",", "X").replace(".", ",").replace("X", ".")


def _png(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _zemin(w, h, renk, cx=0.85, cy=0.1, guc=1.0):
    """Köşeden yayılan yumuşak renkli zemin (küçük çizip büyüterek, hızlı)."""
    sw, sh = 48, max(8, int(48 * h / w))
    k = Image.new("RGB", (sw, sh))
    px = k.load()
    for y in range(sh):
        for x in range(sw):
            d = (((x / sw - cx) ** 2) + ((y / sh - cy) * h / w) ** 2) ** 0.5
            a = max(0.0, 1 - d / 0.75) ** 1.6 * guc
            px[x, y] = tuple(int(BG[i] + (renk[i] - BG[i]) * a * 0.32) for i in range(3))
    return k.resize((w, h), Image.BICUBIC)


def _parlak_cizgi(img, pts, renk, w=4, dolgu=True, alt=None):
    """Altı degrade dolgulu, etrafı parlayan çizgi."""
    W_, H_ = img.size
    if len(pts) < 2:
        return
    if dolgu:
        alt = alt if alt is not None else max(p[1] for p in pts) + 40
        m = Image.new("L", (W_, H_), 0)
        ImageDraw.Draw(m).polygon(pts + [(pts[-1][0], alt), (pts[0][0], alt)], fill=255)
        top = int(min(p[1] for p in pts))
        g = Image.new("L", (1, H_), 0)
        for y in range(H_):
            g.putpixel((0, y), int(max(0, min(1, 1 - (y - top) / max(1, alt - top))) * 95) if y >= top else 0)
        g = g.resize((W_, H_))
        from PIL import ImageChops
        m = ImageChops.multiply(m, g)
        img.paste(Image.new("RGB", (W_, H_), renk), (0, 0), m)
    glow = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(pts, fill=renk + (200,), width=w * 3, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    img.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(img)
    d.line(pts, fill=tuple(min(255, c + 60) for c in renk), width=w, joint="curve")


def _rozet(img, xy, metin, renk1, renk2, yazi=(42, 26, 0), elmas=False, boy=26):
    """Parlayan hap şeklinde etiket. Dönüş: sağ kenar x."""
    d = ImageDraw.Draw(img)
    f = F(boy, True)
    x, y = xy
    tw = d.textlength(T(metin), font=f)
    pad = 18
    ek = 34 if elmas else 0
    w, h = int(tw + pad * 2 + ek), boy + 22
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=renk1 + (150,))
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img.paste(glow, (0, 0), glow)
    # yatay degrade
    band = Image.new("RGB", (w, 1))
    for i in range(w):
        t = i / max(1, w - 1)
        band.putpixel((i, 0), tuple(int(renk2[k] + (renk1[k] - renk2[k]) * t) for k in range(3)))
    band = band.resize((w, h))
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=255)
    img.paste(band, (x, y), m)
    tx = x + pad
    if elmas:
        cx, cy, r = x + pad + 11, y + h / 2, 11
        d.polygon([(cx - r, cy - 3), (cx - r / 2, cy - r + 2), (cx + r / 2, cy - r + 2), (cx + r, cy - 3), (cx, cy + r)],
                  fill=(255, 255, 255), outline=yazi)
        d.line([(cx - r, cy - 3), (cx + r, cy - 3)], fill=yazi, width=2)
        tx += ek
    d.text((tx, y + h / 2), T(metin), font=f, fill=yazi, anchor="lm")
    return x + w


def _kutu(d, x, y, w, h, etiket, deger, renk=TXT):
    d.rounded_rectangle([x, y, x + w, y + h], radius=18, fill=gorsel.PANEL2, outline=gorsel.GRID, width=2)
    d.text((x + 18, y + 14), T(etiket), font=F(22), fill=MUT)
    d.text((x + 18, y + h - 14), T(deger), font=F(34, True), fill=renk, anchor="ls")


def _logo(d, x, y, sym, boy=84):
    renkler = [((43, 26, 74), (201, 168, 255)), ((18, 58, 44), (30, 227, 160)), ((24, 40, 74), (127, 168, 255)),
               ((74, 40, 18), (255, 181, 71)), ((74, 18, 30), (255, 122, 138))]
    a, b = renkler[sum(map(ord, sym)) % len(renkler)]
    d.rounded_rectangle([x, y, x + boy, y + boy], radius=boy // 4, fill=a)
    d.text((x + boy / 2, y + boy / 2), sym[:1], font=F(int(boy * 0.46), True), fill=b, anchor="mm")


def _imza(d, w, y, sag=""):
    d.text((44, y), "ABD·BOT", font=F(22, True), fill=gorsel.DIM)
    if sag:
        d.text((w - 44, y), T(sag), font=F(22), fill=MUT, anchor="ra")


def _seri(vals, x0, x1, y0, y1):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return []
    lo, hi = min(vals), max(vals)
    r = (hi - lo) or 1
    n = len(vals)
    return [(x0 + i / (n - 1) * (x1 - x0), y1 - (v - lo) / r * (y1 - y0)) for i, v in enumerate(vals)]


# ------------------------------------------------------------------------------------------ Süper Fırsat
def super_kart(d_):
    """d_: sym, alt, p, ch, closes, e, s, h, dir, olumlu (0-5), sira (ör. 3 → en iyi %3), etiket_sag"""
    w, h = 1080, 760
    img = _zemin(w, h, UP, 0.88, 0.05, 1.1)
    gl = _zemin(w, h, GOLD, 0.05, 0.0, 0.7)
    img = Image.blend(img, gl, 0.35)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 7, h - 7], radius=36, outline=(92, 76, 36), width=3)
    _rozet(img, (44, 40), "SÜPER FIRSAT", GOLD, GOLD2, elmas=True)
    d.text((w - 44, 58), "ABD·BOT", font=F(24, True), fill=gorsel.DIM, anchor="ra")
    sym = d_["sym"]
    _logo(d, 44, 122, sym)
    d.text((148, 120), T(sym), font=F(64, True), fill=TXT)
    d.text((150, 196), T(d_.get("alt") or "")[:42], font=F(24), fill=MUT)
    d.text((w - 44, 124), "$" + fp(d_.get("p")), font=F(52, True), fill=TXT, anchor="ra")
    ch = d_.get("ch")
    if ch is not None:
        t = f"{'▲' if ch >= 0 else '▼'} {ch:+.1f}%".replace(".", ",")
        f = F(30, True)
        tw = d.textlength(t, font=f)
        x1 = w - 44
        d.rounded_rectangle([x1 - tw - 28, 186, x1, 232], radius=12, fill=UP if ch >= 0 else DN)
        d.text((x1 - 14, 209), t, font=f, fill=(3, 20, 13) if ch >= 0 else (255, 255, 255), anchor="rm")
    # grafik + giriş/stop/hedef çizgileri
    gx0, gx1, gy0, gy1 = 44, w - 44, 268, 470
    cl = list(d_.get("closes") or [])[-72:]
    e, s_, hd = d_.get("e"), d_.get("s"), d_.get("h")
    vals = cl + [v for v in (e, s_, hd) if v]
    if len(cl) >= 2:
        lo, hi = min(vals), max(vals)
        r = (hi - lo) or 1
        Y = lambda v: gy1 - (v - lo) / r * (gy1 - gy0)
        for v, c in ((hd, UP), (s_, DN), (e, (200, 210, 214))):
            if v:
                yy = Y(v)
                for xx in range(gx0, gx1, 22):
                    d.line([(xx, yy), (min(gx1, xx + 11), yy)], fill=c + (150,), width=2)
        pts = [(gx0 + i / (len(cl) - 1) * (gx1 - gx0), Y(v)) for i, v in enumerate(cl)]
        _parlak_cizgi(img, pts, UP, 4, alt=gy1 + 20)
        d = ImageDraw.Draw(img)
    bw = (w - 88 - 2 * 18) // 3
    for i, (et, v, c) in enumerate((("Giriş", e, TXT), ("Stop", s_, (255, 122, 138)), ("Hedef", hd, UP))):
        _kutu(d, 44 + i * (bw + 18), 500, bw, 108, et, fp(v), c)
    n_ok = int(d_.get("olumlu") or 0)
    sw = (w - 88 - 4 * 14) / 5
    for i in range(5):
        x = 44 + i * (sw + 14)
        col = UP if i < n_ok else gorsel.GRID
        if i < n_ok:
            gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(gl).rounded_rectangle([x, 636, x + sw, 648], radius=6, fill=UP + (170,))
            gl = gl.filter(ImageFilter.GaussianBlur(6))
            img.paste(gl, (0, 0), gl)
            d = ImageDraw.Draw(img)
        d.rounded_rectangle([x, 636, x + sw, 648], radius=6, fill=col)
    alt = f"{n_ok}/5 modül olumlu" + (f" · model en iyi %{d_['sira']}" if d_.get("sira") else "")
    d.text((44, 690), T(alt), font=F(24), fill=MUT)
    d.text((w - 44, 690), T(d_.get("etiket_sag") or "paper"), font=F(24), fill=MUT, anchor="ra")
    return _png(img)


# ------------------------------------------------------------------------------------------ hedef / sonuç
def sonuc_kart(d_):
    """v4: seviye merdivenli sonuç kartı (gorsel.py). Merdiven için stop/hedefler yoksa eski kart."""
    if d_.get("stop") and d_.get("hedefler"):
        return gorsel.sonuc_kart(d_)
    return _sonuc_eski(d_)


def _sonuc_eski(d_):
    """d_: sym, ad, r, usd, giris, cikis, dk, kazanc(bool), etiket"""
    w, h = 1080, 640
    win = d_.get("usd", 0) > 0
    ana = UP if win else DN
    img = _zemin(w, h, ana, 0.5, -0.05, 1.4)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 7, h - 7], radius=36, outline=gorsel.GRID, width=3)
    et = d_.get("etiket") or ("HEDEF" if win else "STOP")
    f = F(28, True)
    tw = d.textlength(T(et), font=f) + 36 + 34
    _rozet(img, (int(w / 2 - tw / 2), 44), et, GOLD if win else DN, GOLD2 if win else (255, 140, 150),
           yazi=(42, 26, 0) if win else (255, 255, 255), boy=28)
    d = ImageDraw.Draw(img)
    d.text((w / 2, 158), T(f"{d_['sym']} · {d_.get('ad') or ''}")[:46], font=F(32), fill=TXT, anchor="mm")
    rt = f"{d_.get('r', 0):+.1f}R".replace(".", ",").replace("-", "−")
    gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(gl).text((w / 2, 290), rt, font=F(150, True), fill=ana + (190,), anchor="mm")
    gl = gl.filter(ImageFilter.GaussianBlur(18))
    img.paste(gl, (0, 0), gl)
    d = ImageDraw.Draw(img)
    d.text((w / 2, 290), rt, font=F(150, True), fill=ana, anchor="mm")
    d.text((w / 2, 420), T(f"{d_.get('usd', 0):+.2f} $".replace(".", ",").replace("-", "−")), font=F(56, True), fill=TXT, anchor="mm")
    alt = f"giriş {fp(d_.get('giris'))} → çıkış {fp(d_.get('cikis'))}" + (f"   ·   {d_['dk']} dk" if d_.get("dk") is not None else "")
    d.text((w / 2, 500), T(alt), font=F(28), fill=MUT, anchor="mm")
    if d_.get("seri"):
        d.text((w / 2, 552), T(f"Seri: {d_['seri']} kazanç üst üste"), font=F(28, True), fill=GOLD, anchor="mm")
    _imza(d, w, 590, "paper")
    return _png(img)


# ------------------------------------------------------------------------------------------ gün sonu
def gunsonu_kart(d_):
    """d_: tarih, pnl, deger, eq (değer listesi), n, w, super ("1/1"), seviye, seviye_ad"""
    w, h = 1080, 700
    img = _zemin(w, h, BLUE, 0.1, 0.0, 0.9)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 7, h - 7], radius=36, outline=gorsel.GRID, width=3)
    _rozet(img, (44, 40), "GÜN SONU", BLUE, (143, 211, 255), yazi=(3, 20, 42))
    d = ImageDraw.Draw(img)
    d.text((w - 44, 58), T(d_.get("tarih") or ""), font=F(26), fill=MUT, anchor="ra")
    pnl = d_.get("pnl", 0)
    d.text((44, 134), "Bugün", font=F(26), fill=MUT)
    d.text((44, 168), T(f"{pnl:+.2f} $".replace(".", ",").replace("-", "−")), font=F(76, True), fill=UP if pnl >= 0 else DN)
    d.text((w - 44, 134), "Portföy", font=F(26), fill=MUT, anchor="ra")
    d.text((w - 44, 176), "$" + fp(d_.get("deger")), font=F(58, True), fill=TXT, anchor="ra")
    pts = _seri(d_.get("eq") or [], 44, w - 44, 300, 470)
    if pts:
        _parlak_cizgi(img, pts, UP if (d_["eq"][-1] >= d_["eq"][0]) else DN, 4, alt=490)
        d = ImageDraw.Draw(img)
    else:
        d.text((w / 2, 390), "Bugün kapanan işlem yok", font=F(28), fill=MUT, anchor="mm")
    kol = [("İşlem", str(d_.get("n", 0))), ("Kazanç", str(d_.get("w", 0))), ("Süper", d_.get("super") or "0/0"),
           ("Seviye", str(d_.get("seviye") or "—"))]
    cw = (w - 88) / 4
    for i, (a, b) in enumerate(kol):
        cx = 44 + cw * i + cw / 2
        d.text((cx, 530), T(a), font=F(24), fill=MUT, anchor="mm")
        d.text((cx, 580), T(b), font=F(44, True), fill=GOLD if a == "Süper" else TXT, anchor="mm")
    if d_.get("seviye_ad"):
        d.text((44, 640), T(d_["seviye_ad"]), font=F(24, True), fill=UP)
    d.text((w - 44, 640), "ABD·BOT · paper", font=F(22), fill=MUT, anchor="ra")
    return _png(img)


# ------------------------------------------------------------------------------------------ hisse kartı (/hisse)
def hisse_kart(d_):
    """d_: sym, ad, p, ch, closes, seviyeler [(ad, fiyat, renk)], satirlar [(etiket, değer)]"""
    w, h = 1080, 700
    ch = d_.get("ch") or 0
    img = _zemin(w, h, UP if ch >= 0 else DN, 0.88, 0.05, 1.0)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 7, h - 7], radius=36, outline=LINE, width=3)
    _logo(d, 44, 44, d_["sym"])
    d.text((148, 40), T(d_["sym"]), font=F(62, True), fill=TXT)
    d.text((150, 112), T(d_.get("ad") or "")[:40], font=F(26), fill=MUT)
    d.text((w - 44, 44), "$" + fp(d_.get("p")), font=F(54, True), fill=TXT, anchor="ra")
    t = f"{'▲' if ch >= 0 else '▼'} {ch:+.2f}%".replace(".", ",")
    f = F(30, True)
    tw = d.textlength(t, font=f)
    d.rounded_rectangle([w - 44 - tw - 28, 110, w - 44, 156], radius=12, fill=UP if ch >= 0 else DN)
    d.text((w - 58, 133), t, font=f, fill=(3, 20, 13) if ch >= 0 else (255, 255, 255), anchor="rm")
    cl = list(d_.get("closes") or [])[-96:]
    sv = [x for x in (d_.get("seviyeler") or []) if x[1]]
    if len(cl) >= 2:
        vals = cl + [x[1] for x in sv]
        lo, hi = min(vals), max(vals)
        r = (hi - lo) or 1
        Y = lambda v: 470 - (v - lo) / r * (470 - 200)
        for ad, v, c in sv:
            yy = Y(v)
            for xx in range(44, w - 250, 22):
                d.line([(xx, yy), (xx + 11, yy)], fill=c, width=2)
            d.text((w - 44, yy), T(f"{ad} {fp(v)}"), font=F(22, True), fill=c, anchor="rm")
        pts = [(44 + i / (len(cl) - 1) * (w - 300), Y(v)) for i, v in enumerate(cl)]
        _parlak_cizgi(img, pts, UP if cl[-1] >= cl[0] else DN, 4, alt=490)
        d = ImageDraw.Draw(img)
    rows = (d_.get("satirlar") or [])[:4]
    if rows:
        bw = (w - 88 - (len(rows) - 1) * 16) // len(rows)
        for i, (a, b) in enumerate(rows):
            _kutu(d, 44 + i * (bw + 16), 520, bw, 104, a, b)
    _imza(d, w, 650, datetime.now(TR).strftime("%d.%m %H:%M"))
    return _png(img)
