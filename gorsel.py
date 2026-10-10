"""
ABD·BOT görsel sistemi v4 — Telegram'a giden bütün resimler.
--------------------------------------------------------------
Tek imza: SEVİYE MERDİVENİ. Grafikteki her çizgi (stop · giriş · K1 · K2 · K3) sağ panelde
aynı yükseklikte etiketlenir; şu anki fiyat bir nokta olarak merdivende durur.
Sadece Pillow. Inter yazı tipi repo'daysa onu, yoksa DejaVu'yu kullanır.
"""
import io
import math
import os
from bisect import bisect_left
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import grafik

TR = ZoneInfo("Europe/Istanbul")
_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- renkler
BG = (10, 13, 19)
PANEL = (16, 20, 29)
PANEL2 = (22, 27, 38)
GRID = (30, 36, 49)
TXT = (234, 238, 245)
MUT = (136, 145, 165)
DIM = (82, 90, 110)
UP = (34, 197, 94)
DN = (244, 63, 94)
ACC = (79, 140, 255)
GOLD = (245, 192, 78)
VIO = (167, 139, 250)
VWAP = (251, 146, 60)

DURUM = {   # durum → (etiket, renk)
    "bekliyor": ("BEKLİYOR", ACC), "yaklasiyor": ("YAKLAŞIYOR", GOLD), "onaylı": ("ONAYLANDI", UP),
    "giris": ("BOT İÇERİDE", UP), "k1": ("K1 GELDİ", UP), "k2": ("K2 GELDİ", UP), "k3": ("K3 GELDİ", UP),
    "sahte": ("SAHTE KIRILIM", DN), "kaçtı": ("KAÇTI · RETEST", GOLD), "iptal": ("İPTAL", DIM), "süre": ("SÜRE DOLDU", DIM),
    "stop": ("STOP", DN), "al": ("AL", UP), "sat": ("SAT", DN), "super": ("SÜPER FIRSAT", GOLD),
}

_F = {}
_INTER = {"r": "Inter-Regular", "m": "Inter-Medium", "sb": "Inter-SemiBold", "b": "Inter-Bold", "xb": "Inter-ExtraBold"}


def F(size, w="r"):
    """Inter (repo'da .otf/.ttf varsa) → DejaVu."""
    k = (size, w)
    if k in _F:
        return _F[k]
    f = None
    yedek = {"xb": ("xb", "b", "sb"), "b": ("b", "xb", "sb"), "sb": ("sb", "b", "m"), "m": ("m", "r", "sb"), "r": ("r", "m")}
    for w2 in yedek.get(w, ("r",)):
        n = _INTER[w2]
        for p in (os.path.join(_HERE, n + ".otf"), os.path.join(_HERE, n + ".ttf"), os.path.join(_HERE, "fonts", n + ".otf"),
                  "/usr/share/fonts/opentype/inter/" + n + ".otf"):
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size)
                    break
                except Exception:
                    pass
        if f is not None:
            break
    if f is None:
        f = grafik.font(size, w in ("sb", "b", "xb"))
    _F[k] = f
    return f


def T(s):
    return grafik._t(s)


def fp(x):
    """Türkçe fiyat: 2,21 · 0,4512 · 1.234,50"""
    if x is None:
        return "—"
    s = f"{x:,.2f}" if x >= 1 else f"{x:.4f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fpct(x, d=1):
    if x is None:
        return "—"
    return ("+" if x > 0 else "−" if x < 0 else "") + f"{abs(x):.{d}f}".replace(".", ",") + "%"


def _png(img):
    b = io.BytesIO()
    img.save(b, "PNG", optimize=True)
    return b.getvalue()


def _dash(d, x0, y, x1, col, w, on, off):
    x = x0
    while x < x1:
        d.line([(x, y), (min(x1, x + on), y)], fill=col, width=w)
        x += on + off


def _pill(d, x, y, txt, bg, fg, f, px=14, py=7, r=None, sag=False):
    w = d.textlength(txt, font=f)
    bb = f.getbbox("ÇgH")
    h = bb[3] - bb[1]
    if sag:
        x = x - w - 2 * px
    d.rounded_rectangle([x, y, x + w + 2 * px, y + h + 2 * py], radius=r if r is not None else (h + 2 * py) // 2, fill=bg)
    d.text((x + px, y + py - bb[1]), txt, fill=fg, font=f)
    return x + w + 2 * px


def _mix(c, a, bg=BG):
    return tuple(int(c[i] * a + bg[i] * (1 - a)) for i in range(3))


# ---------------------------------------------------------------- ana grafik: seviye merdiveni
def plan_grafik(bars, p, max_bars=84):
    """
    bars: [(t,o,h,l,c,v[,seans])] — 5 dk (ya da 1 dk) mumlar
    p: {sym, last, ch, durum, baslik, alt, giris, stop, hedefler[], seviye, neden, haber, t_sig, yon, conf, tf}
    """
    if not bars or len(bars) < 8:
        return None
    S = 2
    W, H = 1200, 900
    HDR, FTR, LAD = 138, 112, 300
    B = bars[-max_bars:]
    n = len(B)
    ts = [b[0] for b in B]
    last = p.get("last") or B[-1][4]
    yon = p.get("yon", 1)
    hed = [h for h in (p.get("hedefler") or ([p["hedef"]] if p.get("hedef") else [])) if h]
    lv = []   # (fiyat, etiket, renk, kalın?)
    if p.get("stop"):
        lv.append((p["stop"], "STOP", DN, True))
    if p.get("giris"):
        lv.append((p["giris"], "GİRİŞ", ACC, True))
    for i, h in enumerate(hed[:3]):
        lv.append((h, f"K{i + 1}" if len(hed) > 1 else "HEDEF", UP, i == 0))
    hi = max(b[2] for b in B)
    lo = min(b[3] for b in B)
    for v, *_ in lv:
        if lo * 0.5 < v < hi * 1.8:
            hi, lo = max(hi, v), min(lo, v)
    rng = (hi - lo) or hi * 0.02
    hi += rng * 0.06
    lo -= rng * 0.05

    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img, "RGBA")
    f = lambda sz, w="r": F(sz * S, w)
    x0, x1 = 26 * S, (W - LAD - 8) * S
    py0, py1 = (HDR + 26) * S, (H - FTR - 92) * S
    vy0, vy1 = (H - FTR - 78) * S, (H - FTR - 30) * S
    cw = (x1 - x0) / (n + 4)
    X = lambda i: x0 + (i + 0.5) * cw
    Y = lambda v: py1 - (v - lo) / (hi - lo) * (py1 - py0)

    # seans dışı gölge
    if len(B[0]) > 6:
        i = 0
        while i < n:
            if B[i][6] != 1:
                j = i
                while j + 1 < n and B[j + 1][6] != 1:
                    j += 1
                d.rectangle([X(i) - cw / 2, py0 - 10 * S, X(j) + cw / 2, vy1], fill=(255, 255, 255, 7))
                i = j + 1
            else:
                i += 1
    # ızgara
    step = rng / 5
    mag = 10 ** math.floor(math.log10(step)) if step > 0 else 1
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= step), default=step)
    g = math.ceil(lo / step) * step
    while g < hi:
        _dash(d, x0, Y(g), x1, GRID, S, 2 * S, 6 * S)
        d.text((x0 + 4 * S, Y(g) - 20 * S), fp(g), fill=DIM, font=f(13))
        g += step
    for i in range(0, n, max(1, n // 6)):
        d.text((X(i) - 18 * S, vy1 + 8 * S), datetime.fromtimestamp(ts[i], TR).strftime("%H:%M"), fill=DIM, font=f(13))

    # taban bandı (seviyenin geldiği yer)
    bt = p.get("bant")
    if bt and p.get("bant_uzat"):
        bt = (bt[0], max(bt[1], p.get("t_sig") or ts[-1]), bt[2], bt[3])
    if bt and bt[1] >= ts[0]:
        bx0, bx1 = X(max(0, bisect_left(ts, bt[0]))) - cw / 2, X(min(n - 1, bisect_left(ts, bt[1]))) + cw / 2
        d.rounded_rectangle([bx0, Y(bt[3]), bx1, Y(bt[2])], radius=8 * S, fill=VIO + (26,), outline=VIO + (120,), width=S + 1)
        d.text((bx0 + 10 * S, Y(bt[2]) + 6 * S), T("TABAN"), fill=VIO, font=f(13, "b"))
    # risk / ödül bölgeleri
    e = p.get("giris")
    si = min(max(0, bisect_left(ts, p["t_sig"])), n - 1) if p.get("t_sig") else max(0, n - 18)
    if e and p.get("stop"):
        d.rectangle([X(si) - cw / 2, Y(max(e, p["stop"])), x1, Y(min(e, p["stop"]))], fill=DN + (30,))
    if e and hed:
        top = hed[min(len(hed), 3) - 1]
        d.rectangle([X(si) - cw / 2, Y(max(e, top)), x1, Y(min(e, top))], fill=UP + (20,))
    # seviye çizgileri
    for v, lab, col, kal in lv:
        if not (lo <= v <= hi):
            continue
        if lab == "GİRİŞ":
            d.line([(x0, Y(v)), (x1 + LAD * S, Y(v))], fill=col + (235,), width=3 * S)
        elif kal:
            _dash(d, x0, Y(v), x1 + 8 * S, col + (220,), 2 * S, 12 * S, 7 * S)
        else:
            _dash(d, x0, Y(v), x1 + 8 * S, col + (150,), S + 1, 6 * S, 7 * S)
    # hacim
    vmax = max(b[5] for b in B) or 1
    avg = sum(b[5] for b in B[-21:-1]) / max(1, len(B[-21:-1]))
    for i, b in enumerate(B):
        up = b[4] >= b[1]
        hv = b[5] / vmax * (vy1 - vy0)
        big = b[5] >= 1.5 * avg and avg > 0
        col = (UP if up else DN) + ((210,) if big else (70,))
        d.rounded_rectangle([X(i) - cw * 0.34, vy1 - max(hv, S), X(i) + cw * 0.34, vy1], radius=int(min(cw * 0.2, 3 * S)), fill=col)
    # VWAP
    if len(B[0]) > 6:
        pv = vv = 0.0
        day, pts = None, []
        for i, b in enumerate(B):
            dd = datetime.fromtimestamp(b[0], TR).date()
            if dd != day:
                if len(pts) > 1:
                    d.line(pts, fill=VWAP + (170,), width=2 * S, joint="curve")
                day, pv, vv, pts = dd, 0.0, 0.0, []
            if b[6] == 1:
                pv += (b[2] + b[3] + b[4]) / 3 * b[5]
                vv += b[5]
                if vv:
                    pts.append((X(i), Y(pv / vv)))
        if len(pts) > 1:
            d.line(pts, fill=VWAP + (170,), width=2 * S, joint="curve")
    # mumlar
    for i, b in enumerate(B):
        o, h_, l_, c = b[1], b[2], b[3], b[4]
        col = UP if c >= o else DN
        d.line([(X(i), Y(h_)), (X(i), Y(l_))], fill=col, width=max(2, int(1.6 * S)))
        top, bot = Y(max(o, c)), Y(min(o, c))
        if bot - top < 2 * S:
            bot = top + 2 * S
        r_ = int(min(cw * 0.12, 2 * S))
        d.rounded_rectangle([X(i) - cw * 0.36, top, X(i) + cw * 0.36, bot], radius=r_, fill=col)
    # sinyal oku
    if p.get("t_sig") and ts[0] <= p["t_sig"]:
        ax, ay = X(si), Y(B[si][3]) + 16 * S
        d.polygon([(ax, ay), (ax - 12 * S, ay + 20 * S), (ax + 12 * S, ay + 20 * S)], fill=ACC)

    # ---------------- sağ panel: merdiven
    lx = x1 + 22 * S
    d.rectangle([x1 + 8 * S, HDR * S, W * S, (H - FTR) * S], fill=PANEL)
    d.line([(lx, py0 - 8 * S), (lx, py1 + 8 * S)], fill=GRID, width=3 * S)
    tags = [[Y(v), v, lab, col, kal] for v, lab, col, kal in lv if lo <= v <= hi]
    tags.sort(key=lambda t: t[0])
    MIN = 50 * S
    for i in range(1, len(tags)):
        if tags[i][0] - tags[i - 1][0] < MIN:
            tags[i][0] = tags[i - 1][0] + MIN
    over = tags[-1][0] - (py1 + 30 * S) if tags else 0
    if over > 0:
        for t_ in tags:
            t_[0] -= over
    for y, v, lab, col, kal in tags:
        ry = Y(v)
        d.line([(lx, ry), (lx + 14 * S, y)], fill=col + (160,), width=S + 1)
        d.ellipse([lx - 7 * S, ry - 7 * S, lx + 7 * S, ry + 7 * S], fill=col)
        pct = (v / last - 1) * 100 if last else 0
        cx = lx + 22 * S
        bg = col if lab == "GİRİŞ" else _mix(col, 0.18, PANEL)
        fg = (255, 255, 255) if lab == "GİRİŞ" else col
        nx = _pill(d, cx, y - 19 * S, T(lab), bg, fg, f(14, "b"), px=10 * S, py=6 * S, r=8 * S)
        d.text((nx + 10 * S, y - 22 * S), fp(v), fill=TXT, font=f(22, "b"))
        d.text((nx + 10 * S, y + 3 * S), T(fpct(pct)), fill=MUT, font=f(13, "m"))
    # şu anki fiyat
    cy = Y(last)
    d.ellipse([lx - 12 * S, cy - 12 * S, lx + 12 * S, cy + 12 * S], fill=TXT + (40,))
    d.ellipse([lx - 7 * S, cy - 7 * S, lx + 7 * S, cy + 7 * S], fill=TXT)
    _dash(d, X(n - 1), cy, lx - 12 * S, TXT + (110,), S, 4 * S, 5 * S)
    d.text((x1 - 110 * S, cy - 26 * S), T("şimdi " + fp(last)), fill=TXT, font=f(14, "sb"))

    # ---------------- başlık
    d.rectangle([0, 0, W * S, HDR * S], fill=BG)
    sym = p.get("sym", "")
    d.text((28 * S, 22 * S), sym, fill=TXT, font=f(54, "xb"))
    sw = d.textlength(sym, font=f(54, "xb"))
    d.text((28 * S + sw + 22 * S, 30 * S), fp(last), fill=TXT, font=f(32, "sb"))
    pw = d.textlength(fp(last), font=f(32, "sb"))
    ch = p.get("ch")
    if ch is not None:
        _pill(d, 28 * S + sw + pw + 40 * S, 34 * S, T(fpct(ch)), _mix(UP if ch >= 0 else DN, 0.2), UP if ch >= 0 else DN,
              f(17, "b"), px=12 * S, py=6 * S)
    d.text((30 * S, 92 * S), T(p.get("baslik", "")[:60]), fill=MUT, font=f(19, "m"))
    lab, col = DURUM.get(p.get("durum") or "bekliyor", (str(p.get("durum", "")).upper(), ACC))
    _pill(d, (W - 28) * S, 28 * S, T(lab), col, (255, 255, 255) if col not in (GOLD,) else (40, 26, 0), f(20, "xb"),
          px=20 * S, py=11 * S, sag=True)
    if p.get("alt"):
        a = T(p["alt"][:44])
        d.text(((W - 28) * S - d.textlength(a, font=f(16, "m")), 92 * S), a, fill=MUT, font=f(16, "m"))
    d.line([(0, HDR * S), (W * S, HDR * S)], fill=GRID, width=S)

    # ---------------- alt bilgi
    fy = (H - FTR) * S
    d.rectangle([0, fy, W * S, H * S], fill=PANEL2)
    y = fy + 20 * S
    for k_, v_, c_ in (("NEDEN", p.get("neden"), TXT), ("HABER", p.get("haber"), VIO)):
        if not v_:
            continue
        _pill(d, 28 * S, y - 2 * S, k_, _mix(c_, 0.14, PANEL2), c_ if c_ != TXT else MUT, f(12, "b"), px=8 * S, py=4 * S, r=6 * S)
        d.text((112 * S, y - 3 * S), T(v_[:88]), fill=TXT if c_ == TXT else c_, font=f(17, "m"))
        y += 36 * S
    rr = None
    if e and p.get("stop") and hed:
        rk = abs(e - p["stop"])
        rr = abs(hed[min(1, len(hed) - 1)] - e) / rk if rk else None
    br = T(f"ABD·BOT · {p.get('tf', '5 dk')} · TR saati")
    if rr:
        r_t = T(f"Risk/ödül 1 : {rr:.1f}".replace(".", ","))
        d.text(((W - 28) * S - d.textlength(r_t, font=f(18, "b")), fy + 20 * S), r_t, fill=TXT, font=f(18, "b"))
    d.text(((W - 28) * S - d.textlength(br, font=f(13)), fy + 58 * S), br, fill=DIM, font=f(13))
    # lejant
    d.line([(28 * S, (HDR + 14) * S), (50 * S, (HDR + 14) * S)], fill=VWAP, width=3 * S)
    d.text((58 * S, (HDR + 5) * S), "VWAP", fill=DIM, font=f(13, "m"))
    d.rounded_rectangle([118 * S, (HDR + 8) * S, 132 * S, (HDR + 20) * S], radius=2 * S, fill=UP + (210,))
    d.text((140 * S, (HDR + 5) * S), T("güçlü hacim (≥1,5×)"), fill=DIM, font=f(13, "m"))
    return _png(img.resize((W, H), Image.LANCZOS))


# ---------------------------------------------------------------- küçük yardımcı: plan → resim sözlüğü
def plan_veri(pl, sym, last, ch=None, durum=None, haber=None, tf="5 dk"):
    tur = {"taban": "sabahki tabanın tavanı", "kaybedilen": "kaybedilen seviye", "pmh": "öncesi seans zirvesi",
           "pdh": "dünün zirvesi", "hod": "günün zirvesi", "yuvarlak": "yuvarlak sayı", "kanal": "kanalın verdiği seviye"}
    neden = " + ".join(tur.get(t, t) for t in (pl.get("tur") or []))
    st = durum or pl.get("st") or "bekliyor"
    if st == "bekliyor" and last and pl.get("giris") and 0 < (pl["giris"] / last - 1) * 100 <= 1.5:
        st = "yaklasiyor"
    return {"sym": sym, "last": last, "ch": ch, "durum": st, "giris": pl.get("giris"), "stop": pl.get("stop"),
            "hedefler": pl.get("hedefler"), "baslik": f"Kırılım planı · {fp(pl.get('giris'))} üstü kapanış + hacim",
            "alt": ("kanal seviyesi" if pl.get("kanal") else f"puan {pl.get('puan', 0):g}".replace(".", ",")),
            "neden": neden, "haber": haber, "t_sig": pl.get("t_onay"), "tf": tf, "bant": pl.get("bant"),
            "bant_uzat": "taban" in (pl.get("tur") or [])}


# ---------------------------------------------------------------- sonuç kartı (işlem kapandı / hedef geldi)
def sonuc_kart(d_):
    """d_: sym, ad, r, usd, giris, cikis, dk, etiket, stop, hedefler[], kural, seri"""
    S = 2
    W, H = 1200, 640
    win = (d_.get("usd") or 0) > 0
    ana = UP if win else DN
    img = Image.new("RGB", (W * S, H * S), BG)
    # yumuşak renk ışığı
    gl = Image.new("RGB", (W // 4, H // 4), BG)
    gd = ImageDraw.Draw(gl)
    gd.ellipse([-60, -80, 190, 150], fill=_mix(ana, 0.22))
    gl = gl.filter(ImageFilter.GaussianBlur(40)).resize((W * S, H * S), Image.BICUBIC)
    img = Image.blend(img, gl, 0.9)
    d = ImageDraw.Draw(img, "RGBA")
    f = lambda sz, w="r": F(sz * S, w)
    et = T(d_.get("etiket") or ("HEDEF" if win else "STOP"))
    _pill(d, 56 * S, 52 * S, et, ana, (4, 23, 11) if win else (255, 255, 255), f(20, "xb"), px=16 * S, py=9 * S)
    d.text((56 * S, 120 * S), T(d_.get("sym", "")), fill=TXT, font=f(46, "xb"))
    sw = d.textlength(T(d_.get("sym", "")), font=f(46, "xb"))
    d.text((56 * S + sw + 18 * S, 140 * S), T((d_.get("ad") or "")[:28]), fill=MUT, font=f(22, "m"))
    rt = T(fpct(d_["pct"], 2 if abs(d_["pct"]) < 10 else 1)) if d_.get("pct") is not None else \
        (f"{d_.get('r', 0):+.1f}R").replace(".", ",").replace("-", "−")
    d.text((50 * S, 196 * S), rt, fill=ana, font=f(150, "xb"))
    us = (f"{d_.get('usd', 0):+.2f} $").replace(".", ",").replace("-", "−")
    d.text((58 * S, 392 * S), us, fill=TXT, font=f(44, "b"))
    alt = f"giriş {fp(d_.get('giris'))} → çıkış {fp(d_.get('cikis'))}" + (f" · {d_['dk']} dk" if d_.get("dk") is not None else "")
    d.text((58 * S, 460 * S), T(alt), fill=MUT, font=f(22, "m"))
    if d_.get("kural"):
        d.text((58 * S, 496 * S), T("Çıkış: " + d_["kural"]), fill=MUT, font=f(20, "m"))
    if d_.get("seri"):
        _pill(d, 56 * S, 540 * S, T(f"{d_['seri']} kazanç üst üste"), _mix(GOLD, 0.18), GOLD, f(18, "b"), px=12 * S, py=7 * S)
    # sağda merdiven: hangi basamak geldi
    e, s_, hd, cx_ = d_.get("giris"), d_.get("stop"), [h for h in (d_.get("hedefler") or []) if h], d_.get("cikis")
    if e and s_ and hd and cx_:
        lx, y0, y1 = (W - 300) * S, 70 * S, (H - 70) * S
        d.rounded_rectangle([lx - 40 * S, y0 - 30 * S, (W - 40) * S, y1 + 30 * S], radius=26 * S, fill=PANEL + (235,))
        vals = [s_, e] + hd[:3] + [cx_]
        lo, hi = min(vals), max(vals)
        Y = lambda v: y1 - (v - lo) / ((hi - lo) or 1) * (y1 - y0)
        d.line([(lx, Y(hi)), (lx, Y(lo))], fill=GRID, width=4 * S)
        d.line([(lx, Y(e)), (lx, Y(cx_))], fill=ana, width=6 * S)
        rows = [(s_, "STOP", DN)] + [(e, "GİRİŞ", ACC)] + [(h, f"K{i + 1}", UP) for i, h in enumerate(hd[:3])]
        for v, lab, col in rows:
            hit = (v <= cx_ and lab.startswith("K")) or lab == "GİRİŞ" or (lab == "STOP" and cx_ <= v)
            y = Y(v)
            d.ellipse([lx - 9 * S, y - 9 * S, lx + 9 * S, y + 9 * S], fill=col if hit else PANEL, outline=col, width=3 * S)
            d.text((lx + 24 * S, y - 11 * S), T(lab), fill=col if hit else DIM, font=f(16, "b"))
            d.text((lx + 92 * S, y - 16 * S), fp(v), fill=TXT if hit else DIM, font=f(20, "b"))
        y = Y(cx_)
        d.polygon([(lx - 30 * S, y), (lx - 46 * S, y - 10 * S), (lx - 46 * S, y + 10 * S)], fill=TXT)
    d.text(((W - 40) * S - d.textlength("ABD·BOT · paper", font=f(15)), (H - 30) * S), "ABD·BOT · paper", fill=DIM, font=f(15))
    return _png(img.resize((W, H), Image.LANCZOS))


# ---------------------------------------------------------------- yardımcılar (kartlar)
def _parilti(W, H, S, renk, merkez=(-60, -80, 190, 150), a=0.22):
    gl = Image.new("RGB", (W // 4, H // 4), BG)
    ImageDraw.Draw(gl).ellipse(list(merkez), fill=_mix(renk, a))
    return gl.filter(ImageFilter.GaussianBlur(40)).resize((W * S, H * S), Image.BICUBIC)


def _tik(d, cx, cy, r, renk, S, dolu=True):
    """Yuvarlak içinde onay işareti."""
    if dolu:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=renk)
        c2 = (6, 24, 12)
    else:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=renk, width=3 * S)
        c2 = renk
    d.line([(cx - r * 0.45, cy + r * 0.02), (cx - r * 0.12, cy + r * 0.36), (cx + r * 0.5, cy - r * 0.34)],
           fill=c2, width=max(2, int(r * 0.24)), joint="curve")


def _saat(t):
    return datetime.fromtimestamp(t, TR).strftime("%H:%M") if t else "—"


def fpk(x):
    """Büyük yüzdeler kısa: +%409 · +%33,8"""
    if x is None:
        return "—"
    return fpct(x, 0 if abs(x) >= 100 else 1)


def _mini_mum(d, B, kutu, S, seviyeler=(), isaret=(), t_giris=None):
    """kutu=(x0,y0,x1,y1) içine 5 dk mumlar + yatay seviyeler. seviyeler: (fiyat, renk, kalın, etiket);
    isaret: (ts, fiyat, renk) noktaları."""
    x0, y0, x1, y1 = kutu
    n = len(B)
    if n < 2:
        return
    hi = max(b[2] for b in B)
    lo = min(b[3] for b in B)
    for v, *_ in seviyeler:
        if v and lo * 0.6 < v < hi * 1.6:
            hi, lo = max(hi, v), min(lo, v)
    r = (hi - lo) or hi * 0.02
    hi += r * 0.07
    lo -= r * 0.05
    cw = (x1 - x0) / (n + 1)
    X = lambda i: x0 + (i + 0.5) * cw
    Y = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)
    ts = [b[0] for b in B]
    if t_giris:
        gi = min(n - 1, max(0, bisect_left(ts, t_giris)))
        d.rectangle([X(gi) - cw / 2, y0, x1, y1], fill=(255, 255, 255, 6))
    for v, col, kal, lab in seviyeler:
        if not v or not (lo <= v <= hi):
            continue
        if kal:
            d.line([(x0, Y(v)), (x1, Y(v))], fill=col + (230,), width=2 * S)
        else:
            _dash(d, x0, Y(v), x1, col + (170,), S + 1, 8 * S, 6 * S)
        if lab:
            f_ = F(12 * S, "b")
            tw = d.textlength(lab, font=f_)
            d.rounded_rectangle([x1 - tw - 14 * S, Y(v) - 20 * S, x1, Y(v) - 3 * S], radius=5 * S, fill=_mix(col, 0.25, PANEL))
            d.text((x1 - tw - 7 * S, Y(v) - 19 * S), lab, fill=col, font=f_)
    for i, b in enumerate(B):
        o, h_, l_, c = b[1], b[2], b[3], b[4]
        col = UP if c >= o else DN
        d.line([(X(i), Y(h_)), (X(i), Y(l_))], fill=col, width=max(2, S))
        top, bot = Y(max(o, c)), Y(min(o, c))
        d.rectangle([X(i) - cw * 0.32, top, X(i) + cw * 0.32, max(bot, top + 2 * S)], fill=col)
    for t_, v, col in isaret:
        if not t_ or not v:
            continue
        i = min(n - 1, max(0, bisect_left(ts, t_ // 300 * 300)))
        cx, cy = X(i), Y(v)
        d.ellipse([cx - 13 * S, cy - 13 * S, cx + 13 * S, cy + 13 * S], fill=col + (60,))
        _tik(d, cx, cy, 9 * S, col, S)
    if t_giris:
        gi = min(n - 1, max(0, bisect_left(ts, t_giris // 300 * 300)))
        ax, ay = X(gi), Y(B[gi][3]) + 12 * S
        d.polygon([(ax, ay), (ax - 10 * S, ay + 17 * S), (ax + 10 * S, ay + 17 * S)], fill=ACC)


# ---------------------------------------------------------------- 🏁 tüm kademeler tamam
def kademe_kart(bars, d_):
    """bars: 5 dk [(t,o,h,l,c,v)] · d_: sym, giris, stop, ks[3], gel[3], t_sig, son, zirve, zt, stop_t, ad"""
    S = 2
    W, H = 1200, 760
    stop_once = bool(d_.get("stop_t"))
    img = Image.blend(Image.new("RGB", (W * S, H * S), BG), _parilti(W, H, S, UP, a=0.26), 0.9)
    d = ImageDraw.Draw(img, "RGBA")
    f = lambda sz, w="r": F(sz * S, w)
    e, ks, gel = d_["giris"], d_["ks"], d_["gel"]
    nx = _pill(d, 56 * S, 50 * S, T("TÜM KADEMELER TAMAM"), UP, (4, 23, 11), f(20, "xb"), px=16 * S, py=9 * S)
    if stop_once:
        _pill(d, nx + 12 * S, 50 * S, T("ÖNCE STOP GELDİ"), _mix(GOLD, 0.22), GOLD, f(18, "b"), px=14 * S, py=10 * S)
    sym = T(d_.get("sym", ""))
    d.text((56 * S, 112 * S), sym, fill=TXT, font=f(58, "xb"))
    sw = d.textlength(sym, font=f(58, "xb"))
    d.text((56 * S + sw + 20 * S, 134 * S), T(f"giriş {fp(e)} · {_saat(d_.get('t_sig'))}"), fill=MUT, font=f(22, "m"))
    k3 = (ks[-1] / e - 1) * 100 if e else 0
    d.text((50 * S, 186 * S), T(fpk(k3)), fill=UP, font=f(132, "xb"))
    sure = (gel[-1] - d_["t_sig"]) // 60 if gel[-1] and d_.get("t_sig") else None
    if sure is not None:
        sy = f"{sure // 60} sa {sure % 60} dk" if sure >= 60 else f"{sure} dk"
        d.text((58 * S, 354 * S), T(f"Giriş → K3 · {sy} içinde tamamlandı"), fill=MUT, font=f(21, "m"))
    # kademe satırları
    y = 410 * S
    for i, (k, g) in enumerate(zip(ks, gel)):
        d.rounded_rectangle([50 * S, y, 590 * S, y + 64 * S], radius=16 * S, fill=PANEL + (235,))
        _tik(d, 86 * S, y + 32 * S, 17 * S, UP, S, dolu=bool(g))
        d.text((118 * S, y + 17 * S), f"K{i + 1}", fill=UP, font=f(24, "xb"))
        d.text((178 * S, y + 15 * S), fp(k), fill=TXT, font=f(27, "b"))
        d.text((330 * S, y + 21 * S), _saat(g), fill=MUT, font=f(20, "m"))
        pt = T(fpk((k / e - 1) * 100))
        d.text((574 * S - d.textlength(pt, font=f(24, "b")), y + 18 * S), pt, fill=UP, font=f(24, "b"))
        y += 76 * S
    # alt çipler
    y = 650 * S
    x = 50 * S
    son, zr = d_.get("son"), d_.get("zirve")
    if son:
        c_ = UP if son >= e else DN
        x = _pill(d, x, y, T(f"Şimdi {fp(son)} · {fpk((son / e - 1) * 100)}"), _mix(c_, 0.18), c_, f(18, "b"), px=14 * S, py=9 * S) + 12 * S
    if zr and zr > ks[-1] * 1.01:
        _pill(d, x, y, T(f"Zirve {fp(zr)} · {fpk((zr / e - 1) * 100)}"), _mix(GOLD, 0.18), GOLD, f(18, "b"), px=14 * S, py=9 * S)
    # sağ: mini grafik
    kx0, ky0, kx1, ky1 = 630 * S, 60 * S, (W - 44) * S, (H - 86) * S
    d.rounded_rectangle([kx0 - 16 * S, ky0 - 16 * S, kx1 + 16 * S, ky1 + 16 * S], radius=26 * S, fill=PANEL + (240,))
    if bars and len(bars) >= 3:
        sev = [(d_.get("stop"), DN, False, "STOP"), (e, ACC, True, "GİRİŞ")] + \
              [(k, UP, i == 2, f"K{i + 1}") for i, k in enumerate(ks)]
        isr = [(g, k, UP) for k, g in zip(ks, gel) if g]
        _mini_mum(d, bars[-60:], (kx0, ky0 + 10 * S, kx1, ky1), S, sev, isr, d_.get("t_sig"))
    br = T("ABD·BOT · paper trade · yatırım tavsiyesi değildir")
    d.text(((W - 44) * S - d.textlength(br, font=f(14)), (H - 40) * S), br, fill=DIM, font=f(14))
    return _png(img.resize((W, H), Image.LANCZOS))


# ---------------------------------------------------------------- 💡 bugün bu kırılımlardan girseydin
def girseydin_kart(d_):
    """d_: gun_ad, toplam, iyi_n, stop_n, hisseler: [{sym, en, seri:[(t,c)], girisler:[{t,e,pk,zt,zirve,not}]}]"""
    S = 2
    H_ = d_["hisseler"][:6]
    W = 1200
    RH = 168
    H = 214 + len(H_) * RH + 96
    img = Image.blend(Image.new("RGB", (W * S, H * S), BG), _parilti(W, H, S, GOLD, (-40, -60, 260, 120), 0.2), 0.9)
    d = ImageDraw.Draw(img, "RGBA")
    f = lambda sz, w="r": F(sz * S, w)
    _pill(d, 56 * S, 44 * S, T("GİRSEYDİN"), GOLD, (40, 26, 0), f(18, "xb"), px=14 * S, py=8 * S)
    d.text((56 * S, 96 * S), T(d_.get("baslik") or "Bugün bu kırılımlardan girseydin"), fill=TXT, font=f(44, "xb"))
    alt = f"{d_.get('gun_ad', '')} · {d_['toplam']} kırılım paylaşıldı · {d_['iyi_n']} tanesi en az +%5 · {d_['stop_n']} tanesi stop oldu"
    d.text((58 * S, 158 * S), T(alt), fill=MUT, font=f(19, "m"))
    y = 214 * S
    for h in H_:
        d.rounded_rectangle([40 * S, y, (W - 40) * S, y + (RH - 16) * S], radius=22 * S, fill=PANEL + (240,))
        d.text((64 * S, y + 22 * S), T(h["sym"]), fill=TXT, font=f(36, "xb"))
        d.text((64 * S, y + 72 * S), T(fpk(h["en"])), fill=UP, font=f(40, "xb"))
        d.text((66 * S, y + 122 * S), T("en iyi giriş"), fill=DIM, font=f(13, "m"))
        # çizgi grafik
        sx0, sy0, sx1, sy1 = 270 * S, y + 20 * S, 690 * S, y + (RH - 36) * S
        sr = h.get("seri") or []
        if len(sr) >= 3:
            lo = min(c for _, c in sr)
            hi = max(c for _, c in sr)
            for g in h["girisler"]:
                lo = min(lo, g["e"])
                hi = max(hi, g.get("zirve") or hi)
            r = (hi - lo) or hi * 0.02
            t0, t1 = sr[0][0], sr[-1][0]
            X = lambda t: sx0 + (t - t0) / ((t1 - t0) or 1) * (sx1 - sx0)
            Y = lambda v: sy1 - (v - lo) / r * (sy1 - sy0)
            pts = [(X(t), Y(c)) for t, c in sr]
            d.polygon(pts + [(pts[-1][0], sy1), (pts[0][0], sy1)], fill=UP + (26,))
            d.line(pts, fill=UP + (230,), width=2 * S, joint="curve")
            for j, g in enumerate(h["girisler"][:4]):
                gx, gy = X(max(t0, g["t"])), Y(g["e"])
                d.ellipse([gx - 13 * S, gy - 13 * S, gx + 13 * S, gy + 13 * S], fill=ACC, outline=PANEL, width=3 * S)
                no = str(j + 1)
                d.text((gx - d.textlength(no, font=f(14, "xb")) / 2, gy - 9 * S), no, fill=(255, 255, 255), font=f(14, "xb"))
                if g.get("zt") and g.get("zirve"):
                    zx, zy = X(g["zt"]), Y(g["zirve"])
                    d.ellipse([zx - 5 * S, zy - 5 * S, zx + 5 * S, zy + 5 * S], fill=GOLD)
        # girişler listesi
        ly = y + 20 * S
        for j, g in enumerate(h["girisler"][:4]):
            no = str(j + 1)
            d.ellipse([716 * S, ly + 2 * S, 742 * S, ly + 28 * S], fill=ACC)
            d.text((729 * S - d.textlength(no, font=f(14, "xb")) / 2, ly + 6 * S), no, fill=(255, 255, 255), font=f(14, "xb"))
            d.text((754 * S, ly + 3 * S), T(f"{_saat(g['t'])}  {fp(g['e'])}"), fill=TXT, font=f(19, "sb"))
            pt = T(fpk(g["pk"]))
            col = UP if g["pk"] >= 5 else MUT
            d.text(((W - 64) * S - d.textlength(pt, font=f(21, "xb")), ly + 2 * S), pt, fill=col, font=f(21, "xb"))
            if g.get("not"):
                nt = T(g["not"])
                d.text(((W - 64) * S - d.textlength(pt, font=f(21, "xb")) - 14 * S - d.textlength(nt, font=f(13, "b")),
                        ly + 8 * S), nt, fill=GOLD if "stop" in g["not"] else MUT, font=f(13, "b"))
            ly += 32 * S
        y += RH * S
    nt = T("Zirve = girişten sonra görülen en yüksek fiyat. Geriye dönük hesap; gerçek işlem değil, yatırım tavsiyesi değildir.")
    d.text((56 * S, (H - 70) * S), nt, fill=DIM, font=f(15, "m"))
    br = T("ABD·BOT · paper trade")
    d.text(((W - 44) * S - d.textlength(br, font=f(14)), (H - 40) * S), br, fill=DIM, font=f(14))
    return _png(img.resize((W, H), Image.LANCZOS))


# ---------------------------------------------------------------- 📅 haftalık rapor kartı (kare: Telegram / Instagram / TikTok)
def haftalik_kart(H):
    """H: aralik, paylasilan, n, wr, avg, tot, en_iyi:[{sym,pct,ad}], kademe, girseydin{sym,en}"""
    S = 2
    W = Hh = 1080
    img = Image.blend(Image.new("RGB", (W * S, Hh * S), BG), _parilti(W, Hh, S, ACC, (-80, -100, 300, 160), 0.3), 0.9)
    d = ImageDraw.Draw(img, "RGBA")
    f = lambda sz, w="r": F(sz * S, w)
    x0 = 64 * S
    d.text((x0, 58 * S), "$ABDBOT", fill=TXT, font=f(34, "xb"))
    _pill(d, x0 + d.textlength("$ABDBOT", font=f(34, "xb")) + 18 * S, 62 * S, T("HAFTALIK RAPOR"), _mix(ACC, 0.25), ACC, f(16, "xb"),
          px=12 * S, py=7 * S)
    ar = T(H.get("aralik", ""))
    d.text(((W - 64) * S - d.textlength(ar, font=f(20, "sb")), 66 * S), ar, fill=MUT, font=f(20, "sb"))
    d.text((x0, 140 * S), T("Bu hafta"), fill=TXT, font=f(78, "xb"))
    d.text((x0 + 4 * S, 238 * S), T("Telegram'a giden her sinyalin gerçek sonucu"), fill=MUT, font=f(22, "m"))
    # 2x2 kutular
    avg = H.get("avg")
    kut = [("Paylaşılan sinyal", str(H.get("paylasilan") or 0), TXT), ("Sonuçlanan", str(H.get("n") or 0), TXT),
           ("Kazanan", "—" if H.get("wr") is None else f"%{H['wr']}", TXT),
           ("İşlem başı ort.", fpct(avg, 2) if avg is not None else "—", UP if (avg or 0) > 0 else DN if (avg or 0) < 0 else TXT)]
    bw, bh, gap, y = (W - 128 - 20) / 2, 150, 20, 296
    for i, (lab, val, col) in enumerate(kut):
        bx = (64 + (i % 2) * (bw + gap)) * S
        by = (y + (i // 2) * (bh + gap)) * S
        d.rounded_rectangle([bx, by, bx + bw * S, by + bh * S], radius=26 * S, fill=PANEL + (235,), outline=GRID, width=S)
        d.text((bx + 26 * S, by + 24 * S), T(lab), fill=MUT, font=f(20, "sb"))
        d.text((bx + 24 * S, by + 58 * S), T(val), fill=col, font=f(58, "xb"))
    # en iyiler
    y = (y + 2 * bh + gap + 40) * S
    d.text((x0, y), T("Haftanın en iyileri"), fill=TXT, font=f(26, "b"))
    y += 46 * S
    ei = H.get("en_iyi") or []
    if not ei:
        d.text((x0, y), T("Bu hafta sonuçlanan sinyal yok."), fill=MUT, font=f(22, "m"))
        y += 44 * S
    for b in ei[:3]:
        d.rounded_rectangle([x0, y, (W - 64) * S, y + 62 * S], radius=18 * S, fill=PANEL + (220,))
        d.text((x0 + 22 * S, y + 14 * S), T("#" + b["sym"]), fill=TXT, font=f(26, "xb"))
        sw = d.textlength(T("#" + b["sym"]), font=f(26, "xb"))
        d.text((x0 + 36 * S + sw, y + 20 * S), T((b.get("ad") or "")[:34]), fill=MUT, font=f(18, "m"))
        pt = T(fpct(b["pct"], 1))
        c_ = UP if b["pct"] > 0 else DN
        d.text(((W - 86) * S - d.textlength(pt, font=f(28, "xb")), y + 13 * S), pt, fill=c_, font=f(28, "xb"))
        y += 74 * S
    # çipler
    y += 6 * S
    x = x0
    if H.get("kademe"):
        x = _pill(d, x, y, T(f"{H['kademe']} kez tüm kademeler tamam"), _mix(UP, 0.18), UP, f(19, "b"), px=16 * S, py=10 * S) + 12 * S
    g = H.get("girseydin")
    if g:
        _pill(d, x, y, T(f"En iyi girseydin #{g['sym']} {fpk(g['en'])}"), _mix(GOLD, 0.18), GOLD, f(19, "b"), px=16 * S, py=10 * S)
    nt = T("Kazanan da kaybeden de sayıldı · paper trade · yatırım tavsiyesi değildir")
    d.text(((W * S - d.textlength(nt, font=f(17, "m"))) / 2, (Hh - 54) * S), nt, fill=DIM, font=f(17, "m"))
    return _png(img.resize((W, Hh), Image.LANCZOS))
