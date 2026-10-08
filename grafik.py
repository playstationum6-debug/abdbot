"""
Telegram mesajları için grafik resmi (PNG).
------------------------------------------
5 dakikalık mumlar + destek/direnç + formasyon çizgileri + alıcı bölgesi + giriş/stop/hedef.
Sadece Pillow kullanır (tarayıcı gerekmez, Render'ın küçük makinesinde ~0,1 sn).
"""
import io
import os
from bisect import bisect_left
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

TR = ZoneInfo("Europe/Istanbul")
W, H = 1080, 680
PAD_L, PAD_R, PAD_T, PAD_B = 18, 112, 92, 34
VOL_H = 70

BG = (13, 16, 26)
GRID = (32, 38, 56)
TXT = (230, 234, 245)
MUT = (140, 150, 175)
UP = (61, 220, 151)
DN = (255, 107, 107)
AC = (124, 134, 255)
WA = (255, 184, 77)
PUR = (185, 160, 255)

_FONTS = {}
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE = "/tmp/abdbot_font"
# Render'da sistem yazı tipi yok; Türkçe harfler için DejaVu açılışta bir kez indirilir.
FONT_URLS = {
    False: ["https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans.ttf",
            "https://unpkg.com/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans.ttf"],
    True: ["https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans-Bold.ttf",
           "https://unpkg.com/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans-Bold.ttf"],
}
_TTF_OK = [None]


def _ttf_paths(bold):
    n = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return [os.path.join(_HERE, n), os.path.join(_CACHE, n), "/usr/share/fonts/truetype/dejavu/" + n, n,
            "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"]


def indir():
    """Açılışta çağrılır (ayrı iş parçacığında). Yazı tipi yoksa indirir. Dönüş: durum metni."""
    import httpx
    os.makedirs(_CACHE, exist_ok=True)
    got = 0
    for bold in (False, True):
        if any(os.path.exists(p) for p in _ttf_paths(bold)[:3]):
            got += 1
            continue
        for u in FONT_URLS[bold]:
            try:
                r = httpx.get(u, timeout=30, follow_redirects=True)
                if r.status_code == 200 and len(r.content) > 100000:
                    with open(os.path.join(_CACHE, os.path.basename(u)), "wb") as f:
                        f.write(r.content)
                    got += 1
                    break
            except Exception:
                continue
    _FONTS.clear()
    _TTF_OK[0] = None
    return "hazır" if got == 2 else ("kısmen" if got else "yazı tipi indirilemedi (Türkçe harfler sadeleştirilir)")


_TR_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜ–·", "cgiosuCGIOSU-.")


def _t(s):
    """TrueType yazı tipi yoksa yerleşik yazı tipi Türkçe harfleri kutu çizer: sadeleştir."""
    if _TTF_OK[0] is None:
        font(15)
    return s if _TTF_OK[0] else str(s).translate(_TR_ASCII)


def font(size, bold=False):
    k = (size, bold)
    if k in _FONTS:
        return _FONTS[k]
    f = None
    for n in _ttf_paths(bold):
        try:
            f = ImageFont.truetype(n, size)
            break
        except Exception:
            continue
    if f is None and bold:
        for n in _ttf_paths(False):
            try:
                f = ImageFont.truetype(n, size)
                break
            except Exception:
                continue
    if _TTF_OK[0] is None or f is not None:
        _TTF_OK[0] = f is not None
    if f is None:
        try:
            f = ImageFont.load_default(size=size)       # Pillow 10.1+: ölçeklenebilir yerleşik yazı tipi
        except Exception:
            f = ImageFont.load_default()
    _FONTS[k] = f
    return f


def fp(x):
    if x is None:
        return "—"
    return f"{x:.2f}" if x >= 1 else f"{x:.4f}"


def _dash(d, x0, y0, x1, y1, col, w=2, on=9, off=7):
    import math
    L = math.hypot(x1 - x0, y1 - y0)
    if L == 0:
        return
    dx, dy = (x1 - x0) / L, (y1 - y0) / L
    s = 0.0
    while s < L:
        e = min(L, s + on)
        d.line([(x0 + dx * s, y0 + dy * s), (x0 + dx * e, y0 + dy * e)], fill=col, width=w)
        s += on + off


def ciz(bars, fx=None, title="", sub="", lines=None, pat=None, zone=None, max_bars=130, tf="5 dk", kart=None):
    """
    bars: [(t, o, h, l, c, v)] 5 dk mumlar (eskiden yeniye)
    fx: formasyon.analyze çıktısı (lv, pat, zones)
    lines: {"giris": p, "stop": p, "hedef": p}   (plan / sinyal / işlem)
    pat: gösterilecek formasyonun adı (None → oluşan ilk formasyon)
    zone: True → alıcı bölgesini göster
    """
    fx = fx or {}
    lines = {k: v for k, v in (lines or {}).items() if v}
    if len(bars) < 10:
        return None
    # formasyon başlangıcı görünsün diye pencereyi geriye uzat
    P = None
    pats = fx.get("pat") or []
    if pat:
        P = next((p for p in pats if p.get("ad") == pat), None)
    elif not lines:
        P = next((p for p in pats if p.get("durum") == "oluşuyor"), None)
    n0 = max(0, len(bars) - max_bars)
    if P and P.get("pts"):
        t_first = min(x[0] for x in P["pts"])
        i_first = bisect_left([b[0] for b in bars], t_first)
        n0 = max(0, min(n0, i_first - 8), len(bars) - 220)
    B = bars[n0:]
    ts = [b[0] for b in B]
    n = len(B)

    hi = max(b[2] for b in B)
    lo = min(b[3] for b in B)
    extra = list(lines.values())
    if P:
        extra += [P.get("hedef"), P.get("neck_now")]
    Z = (fx.get("zones") or [None])[0] if zone else None
    if Z:
        extra += [Z["lo"], Z["hi"]]
    for v in extra:
        if v and lo * 0.7 < v < hi * 1.35:
            hi, lo = max(hi, v), min(lo, v)
    rng = (hi - lo) or hi * 0.01
    hi += rng * 0.06
    lo -= rng * 0.06

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img, "RGBA")
    x0, x1 = PAD_L, W - PAD_R
    pad_t = 150 if kart else PAD_T
    y0, y1 = pad_t, H - PAD_B - VOL_H - 8
    cw = (x1 - x0) / n

    def X(i):
        return x0 + (i + 0.5) * cw

    def Y(p):
        return y1 - (p - lo) / (hi - lo) * (y1 - y0)

    def Xt(t):
        i = bisect_left(ts, t)
        if i >= n:
            return X(n - 1)
        return X(i)

    # ızgara + fiyat ekseni
    f12, f14, f15b = font(15), font(17), font(17, True)
    tag_ys = [Y(v) for v in lines.values() if lo <= v <= hi] + [Y(B[-1][4])]
    for k in range(6):
        p = lo + (hi - lo) * k / 5
        y = Y(p)
        d.line([(x0, y), (x1, y)], fill=GRID, width=1)
        if all(abs(y - ty) > 22 for ty in tag_ys):
            d.text((x1 + 8, y - 9), fp(p), fill=MUT, font=f12)

    def label(x, y, txt, col, f=None):
        f = f or f12
        bb = d.textbbox((x, y), _t(txt), font=f)
        d.rectangle([bb[0] - 4, bb[1] - 3, bb[2] + 4, bb[3] + 3], fill=BG + (215,))
        d.text((x, y), _t(txt), fill=col, font=f)
    # zaman etiketleri (TR saati)
    step = max(1, n // 6)
    for i in range(0, n, step):
        lbl = datetime.fromtimestamp(ts[i], TR).strftime("%H:%M")
        d.text((X(i) - 18, H - PAD_B + 8), lbl, fill=MUT, font=f12)

    # alıcı bölgesi
    if Z:
        d.rectangle([x0, Y(Z["hi"]), x1, Y(Z["lo"])], fill=(61, 220, 151, 34), outline=(61, 220, 151, 90))
        d.text((x0 + 8, Y(Z["hi"]) + 3), _t(f"Alıcı bölgesi {fp(Z['lo'])}–{fp(Z['hi'])}"), fill=UP, font=f12)

    # FVG boşlukları (dolmamış): oluştuğu mumdan sağa kutu
    for z in (fx.get("fvg") or [])[:3]:
        if not (lo <= z["lo"] <= hi or lo <= z["hi"] <= hi):
            continue
        xs = Xt(z["t"]) if z["t"] >= ts[0] else x0
        col = (56, 189, 248) if z["bull"] else (244, 114, 182)
        d.rectangle([xs, Y(min(hi, z["hi"])), x1, Y(max(lo, z["lo"]))], fill=col + (30,), outline=col + (120,))
        d.text((xs + 4, Y(min(hi, z["hi"])) + 2), _t("Boğa FVG" if z["bull"] else "Ayı FVG"), fill=col, font=f12)
    # Fibonacci altın bölge (0,5–0,618)
    FB = fx.get("fib")
    if FB and lo <= FB["altin"][0] <= hi:
        d.rectangle([x0, Y(min(hi, FB["altin"][1])), x1, Y(FB["altin"][0])], fill=(250, 204, 21, 22))
        d.text((x1 - 190, Y(FB["altin"][0]) - 18), _t("Fibo 0,5–0,618"), fill=(250, 204, 21), font=f12)
    # trend çizgisi / kanal
    KN = fx.get("kanal")
    if KN:
        col = (167, 139, 250)
        (ta, pa), (tb, pb) = KN["ana"]
        (tc, pc_), (td, pd) = KN["karsi"]
        xa = Xt(ta) if ta >= ts[0] else x0
        if ta < ts[0] and tb > ta:      # pencerenin solunda başlıyorsa çizgiyi pencere başına kaydır
            pa = pa + (pb - pa) * (ts[0] - ta) / (tb - ta)
            pc_ = pc_ + (pd - pc_) * (ts[0] - tc) / (td - tc) if td > tc else pc_
        def clip(xa_, ya, xb_, yb):
            """Çizgiyi fiyat alanının içinde kalan parçasıyla sınırla."""
            pts = []
            for t_ in [i / 40 for i in range(41)]:
                yy = ya + (yb - ya) * t_
                if lo <= yy <= hi:
                    pts.append((xa_ + (xb_ - xa_) * t_, Y(yy)))
            return (pts[0], pts[-1]) if len(pts) >= 2 else None
        seg = clip(xa, pa, x1, pb)
        if seg:
            d.line(list(seg), fill=col, width=3)
        seg2 = clip(xa, pc_, x1, pd)
        if seg2:
            _dash(d, seg2[0][0], seg2[0][1], seg2[1][0], seg2[1][1], col, w=2)
        if seg:
            label(max(x0 + 8, seg[1][0] - 200), seg[1][1] + (8 if KN["yon"] > 0 else -26),
                  KN["ad"] + (" (kırıldı)" if KN.get("kirildi") else ""), col)

    # destek / direnç (en yakın 2'şer)
    last = B[-1][4]
    lv = fx.get("lv") or []
    res = sorted([x for x in lv if x["p"] > last], key=lambda x: x["p"])[:2]
    sup = sorted([x for x in lv if x["p"] < last], key=lambda x: -x["p"])[:2]
    lv_lbl = []
    for x in res + sup:
        if not (lo < x["p"] < hi):
            continue
        col = DN if x["p"] > last else UP
        _dash(d, x0, Y(x["p"]), x1, Y(x["p"]), col + (150,), w=1, on=5, off=5)
        lv_lbl.append((Y(x["p"]) - 18, ("Direnç " if x["p"] > last else "Destek ") + fp(x["p"]), col))

    # hacim
    vmax = max(b[5] for b in B) or 1
    vb = H - PAD_B
    for i, b in enumerate(B):
        col = UP if b[4] >= b[1] else DN
        hgt = b[5] / vmax * VOL_H
        d.rectangle([X(i) - cw * 0.35, vb - hgt, X(i) + cw * 0.35, vb], fill=col + (70,))

    # mumlar
    for i, b in enumerate(B):
        o, h, l, c = b[1], b[2], b[3], b[4]
        col = UP if c >= o else DN
        d.line([(X(i), Y(h)), (X(i), Y(l))], fill=col, width=1)
        top, bot = Y(max(o, c)), Y(min(o, c))
        if bot - top < 1:
            bot = top + 1
        d.rectangle([X(i) - cw * 0.36, top, X(i) + cw * 0.36, bot], fill=col)

    for y, t_, col in lv_lbl:
        label(x0 + 8, y, t_, col)

    # formasyon
    if P:
        pts = [(Xt(t), Y(p)) for t, p in P.get("pts") or []]
        if len(pts) >= 2:
            d.line(pts, fill=PUR, width=3, joint="curve")
            for (px, py) in pts:
                d.ellipse([px - 5, py - 5, px + 5, py + 5], fill=PUR)
        if P.get("neck"):
            (ta, pa), (tb, pb) = P["neck"]
            d.line([(Xt(ta), Y(pa)), (x1, Y(pb))], fill=WA, width=3)
            label(x1 - 150, Y(pb) - 24, f"Boyun {fp(P['neck_now'])}", WA, f15b)
        if P.get("alt"):
            (ta, pa), (tb, pb) = P["alt"]
            d.line([(Xt(ta), Y(pa)), (x1, Y(pb))], fill=WA + (170,), width=2)
        tg = P.get("hedef")
        if tg and lo <= tg <= hi:
            col = UP if P.get("bull") else DN
            _dash(d, x0 + (x1 - x0) * 0.55, Y(tg), x1, Y(tg), col, w=2)
            label(x1 - 150, Y(tg) + (6 if not P.get("bull") else -24), f"Hedef {fp(tg)}", col, f15b)

    # giriş / stop / hedef (sağ etiketler üst üste binmesin)
    tags = []
    for k, col, ad in (("giris", AC, "Giriş"), ("stop", DN, "Stop"), ("hedef", UP, "Hedef")):
        v = lines.get(k)
        if not v or not (lo <= v <= hi):
            continue
        _dash(d, x0, Y(v), x1, Y(v), col, w=2)
        tags.append([Y(v), f"{ad} {fp(v)}" if len(fp(v)) < 7 else fp(v), col])
    tags.append([Y(B[-1][4]), fp(B[-1][4]), (255, 255, 255)])
    tags.sort(key=lambda x: x[0])
    for i in range(1, len(tags)):
        if tags[i][0] - tags[i - 1][0] < 26:
            tags[i][0] = tags[i - 1][0] + 26
    if lines.get("giris") and lines.get("stop"):
        a, s_ = lines["giris"], lines["stop"]
        d.rectangle([x1 - 70, min(Y(a), Y(s_)), x1, max(Y(a), Y(s_))], fill=(255, 107, 107, 40))
        if lines.get("hedef"):
            h_ = lines["hedef"]
            d.rectangle([x1 - 70, min(Y(a), Y(h_)), x1, max(Y(a), Y(h_))], fill=(61, 220, 151, 40))

    for y, t_, col in tags:
        d.rectangle([x1 + 2, y - 12, W - 4, y + 12], fill=col)
        d.text((x1 + 7, y - 10), _t(t_), fill=BG, font=f12)

    # başlık
    if kart:
        _kart(d, kart, tf)
    else:
        d.rectangle([0, 0, W, PAD_T - 14], fill=(18, 22, 36))
        d.text((PAD_L, 14), _t(title[:70]), fill=TXT, font=font(26, True))
        d.text((PAD_L, 50), _t(sub[:110]), fill=MUT, font=f14)
        d.text((W - 150, 18), _t(f"ABD·BOT · {tf}"), fill=MUT, font=f12)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _kart(d, k, tf):
    """Sinyal kartı başlığı: yön + sembol, kurgu, güven çubuğu, risk/ödül, potansiyel."""
    al = k.get("yon", 1) > 0
    col = UP if al else DN
    d.rectangle([0, 0, W, 134], fill=(18, 22, 36))
    d.rectangle([0, 0, 8, 134], fill=col)
    d.rounded_rectangle([24, 18, 112, 58], radius=10, fill=col)
    d.text((40, 24), _t(k.get("etiket") or ("AL" if al else "SAT")), fill=BG, font=font(24, True))
    d.text((126, 16), _t("#" + k.get("sym", "")), fill=TXT, font=font(34, True))
    d.text((26, 72), _t(k.get("ad", "")[:46]), fill=TXT, font=font(19))
    if k.get("alt"):
        d.text((26, 102), _t(k["alt"][:70]), fill=MUT, font=font(16))
    # sağ blok: güven çubuğu + R/Ö + potansiyel
    rx = W - 360
    if k.get("conf") is not None:
        c = int(k["conf"])
        d.text((rx, 16), _t(f"Güven {c}"), fill=TXT, font=font(19, True))
        for i in range(10):
            on = i < round(c / 10)
            d.rounded_rectangle([rx + i * 33, 46, rx + i * 33 + 26, 58], radius=3,
                                fill=(col if on else (45, 52, 72)))
    y = 72
    for lbl, val, cc in (("Risk/ödül", k.get("rr"), TXT), ("Potansiyel", k.get("pot"), UP), ("Risk", k.get("risk"), DN)):
        if val:
            d.text((rx, y), _t(lbl), fill=MUT, font=font(15))
            d.text((rx + 110, y - 2), _t(val), fill=cc, font=font(17, True))
            y += 22
    d.text((W - 150, 112), _t(f"ABD·BOT · {tf}"), fill=MUT, font=font(14))


def ciz_karne(eq, spy, cap0, title="", sub=""):
    """Sermaye eğrisi (bot) + aynı parayla SPY tutsaydın eğrisi. eq/spy: [[t, değer], ...]"""
    if not eq or len(eq) < 2:
        return None
    spy = spy if spy and len(spy) == len(eq) else None
    vals = [v for _, v in eq] + [cap0] + ([v for _, v in spy] if spy else [])
    hi, lo = max(vals), min(vals)
    rng = (hi - lo) or 1
    hi += rng * 0.08
    lo -= rng * 0.08
    img = Image.new("RGB", (W, 560), BG)
    d = ImageDraw.Draw(img, "RGBA")
    x0, x1, y0, y1 = PAD_L + 6, W - PAD_R, PAD_T + 10, 560 - 50
    n = len(eq)
    X = lambda i: x0 + i / (n - 1) * (x1 - x0)
    Y = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)
    f12 = font(15)
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        d.line([(x0, Y(v)), (x1, Y(v))], fill=GRID, width=1)
        d.text((x1 + 8, Y(v) - 9), f"{v:.0f} $", fill=MUT, font=f12)
    _dash(d, x0, Y(cap0), x1, Y(cap0), MUT + (160,), w=1, on=5, off=5)
    last = eq[-1][1]
    col = UP if last >= cap0 else DN
    pts = [(X(i), Y(v)) for i, (_, v) in enumerate(eq)]
    d.polygon(pts + [(x1, y1), (x0, y1)], fill=col + (30,))
    if spy:
        sp = [(X(i), Y(v)) for i, (_, v) in enumerate(spy)]
        for i in range(len(sp) - 1):
            _dash(d, sp[i][0], sp[i][1], sp[i + 1][0], sp[i + 1][1], PUR, w=2, on=6, off=4)
    d.line(pts, fill=col, width=4, joint="curve")
    for i in range(0, n, max(1, n // 6)):
        d.text((X(i) - 18, y1 + 12), datetime.fromtimestamp(eq[i][0], TR).strftime("%d.%m"), fill=MUT, font=f12)
    bp = (last / cap0 - 1) * 100
    leg = f"Bot {bp:+.1f}%"
    if spy:
        leg += f"     SPY tutsaydın {(spy[-1][1] / cap0 - 1) * 100:+.1f}%"
    d.rectangle([0, 0, W, PAD_T - 14], fill=(18, 22, 36))
    d.text((PAD_L, 14), _t(title[:70]), fill=TXT, font=font(26, True))
    d.text((PAD_L, 50), _t(sub[:110]), fill=MUT, font=font(17))
    d.text((x0 + 4, y0 - 4), _t(leg), fill=TXT, font=font(17, True))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
