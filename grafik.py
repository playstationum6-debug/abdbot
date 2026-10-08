"""
Telegram mesajları için grafik resmi (PNG).
------------------------------------------
5 dakikalık mumlar + destek/direnç + formasyon çizgileri + alıcı bölgesi + giriş/stop/hedef.
Sadece Pillow kullanır (tarayıcı gerekmez, Render'ın küçük makinesinde ~0,1 sn).
"""
import io
import math
import os
from bisect import bisect_left
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

TR = ZoneInfo("Europe/Istanbul")
W, H = 1080, 680
PAD_L, PAD_R, PAD_T, PAD_B = 18, 112, 92, 34
VOL_H = 70

BG = (19, 23, 34)
GRID = (32, 38, 56)
TXT = (230, 234, 245)
MUT = (140, 150, 175)
UP = (38, 166, 154)
DN = (239, 83, 80)
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


# ----------------------------------------------------------------- profesyonel grafik (TradingView benzeri, 2x örneklemeli)
TV = {"bg": (19, 23, 34), "pane": (19, 23, 34), "grid": (36, 41, 54), "txt": (209, 212, 220), "mut": (120, 128, 148),
      "up": (38, 166, 154), "dn": (239, 83, 80), "vwap": (255, 152, 0), "ema": (41, 98, 255), "ext": (25, 30, 44),
      "hdr": (24, 29, 42), "acc": (41, 98, 255), "pur": (178, 140, 255), "yel": (255, 214, 0)}
SS = 2   # süper örnekleme: 2 kat büyük çizip küçültünce çizgiler pürüzsüz olur


def _ema(vals, n):
    k = 2 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def ciz(bars, fx=None, title="", sub="", lines=None, pat=None, zone=None, max_bars=96, tf="5 dk", kart=None,
        odak=None, sig_t=None):
    """
    bars: [(t, o, h, l, c, v[, seans])] 5 dk mumlar. seans: 0 öncesi, 1 normal, 2 sonrası (varsa arka plan gölgelenir)
    lines: {"giris","stop","hedef"} → pozisyon kutusu (yeşil hedef / kırmızı stop bölgesi)
    odak: hangi çizimin öne çıkacağı: "formasyon" | "talep" | "fvg" | "kanal" | "fib" | "yapi" | None
    sig_t: sinyal mumunun zamanı (ok işareti)
    """
    fx = fx or {}
    lines = {k: v for k, v in (lines or {}).items() if v}
    if len(bars) < 10:
        return None
    if pat and not odak:
        odak = "formasyon"
    if zone and not odak:
        odak = "talep"
    pats = fx.get("pat") or []
    P = None
    if odak == "formasyon":
        P = next((p for p in pats if p.get("ad") == pat), None) if pat else next((p for p in pats if p.get("durum") == "oluşuyor"), None)
    n0 = max(0, len(bars) - max_bars)
    if P and P.get("pts"):
        i_first = bisect_left([b[0] for b in bars], min(x[0] for x in P["pts"]))
        n0 = max(0, min(n0, i_first - 6), len(bars) - 200)
    B = bars[n0:]
    n = len(B)
    ts = [b[0] for b in B]
    closes = [b[4] for b in B]
    last = closes[-1]

    # fiyat aralığı
    hi = max(b[2] for b in B)
    lo = min(b[3] for b in B)
    extra = list(lines.values())
    Z = (fx.get("zones") or [None])[0] if odak == "talep" else None
    if P:
        extra += [P.get("hedef"), P.get("neck_now")]
    if Z:
        extra += [Z["lo"], Z["hi"]]
    for v in extra:
        if v and lo * 0.6 < v < hi * 1.5:
            hi, lo = max(hi, v), min(lo, v)
    rng = (hi - lo) or hi * 0.01
    hi += rng * 0.07
    lo -= rng * 0.05

    Wf, Hf = 1600, 1000
    hdr = 150 if kart else 112
    AX = 128                     # sağ fiyat ekseni
    VOLH = 120
    S = SS
    img = Image.new("RGB", (Wf * S, Hf * S), TV["bg"])
    d = ImageDraw.Draw(img, "RGBA")
    F = lambda sz, b=False: font(sz * S, b)
    x0, x1 = 24 * S, (Wf - AX) * S
    py0, py1 = (hdr + 18) * S, (Hf - VOLH - 46) * S
    vy0, vy1 = (Hf - VOLH - 30) * S, (Hf - 34) * S
    cw = (x1 - x0) / (n + 6)                 # sağda 6 mumluk boşluk (pozisyon kutusu için)

    def X(i):
        return x0 + (i + 0.5) * cw

    def Y(p):
        return py1 - (p - lo) / (hi - lo) * (py1 - py0)

    def Xt(t):
        return X(min(n - 1, max(0, bisect_left(ts, t))))

    # seans dışı arka plan
    if len(B[0]) > 6:
        i = 0
        while i < n:
            if B[i][6] != 1:
                j = i
                while j + 1 < n and B[j + 1][6] != 1:
                    j += 1
                d.rectangle([X(i) - cw / 2, py0, X(j) + cw / 2, vy1], fill=TV["ext"])
                i = j + 1
            else:
                i += 1
    # ızgara
    step = rng / 5
    mag = 10 ** math.floor(math.log10(step)) if step > 0 else 1
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= step), default=step)
    g = math.ceil(lo / step) * step
    grid_lbl = []
    while g < hi:
        d.line([(x0, Y(g)), (x1, Y(g))], fill=TV["grid"], width=S)
        grid_lbl.append(g)
        g += step
    for i in range(0, n, max(1, n // 7)):
        d.line([(X(i), py0), (X(i), vy1)], fill=TV["grid"] + (110,), width=S)
        d.text((X(i) - 22 * S, vy1 + 8 * S), datetime.fromtimestamp(ts[i], TR).strftime("%H:%M"), fill=TV["mut"], font=F(15))
    # filigran
    if kart and kart.get("sym"):
        wm = F(110, True)
        tw = d.textlength(kart["sym"], font=wm)
        wc = tuple(int(c_ * 0.94 + 255 * 0.06) for c_ in TV["bg"])
        d.text(((x0 + x1) / 2 - tw / 2, (py0 + py1) / 2 - 70 * S), kart["sym"], fill=wc, font=wm)

    # odak çizimleri (arka planda)
    if Z:
        d.rectangle([x0, Y(Z["hi"]), x1, Y(Z["lo"])], fill=TV["up"] + (34,))
        d.text((x0 + 10 * S, Y(Z["hi"]) + 4 * S), _t(f"Alıcı bölgesi · alımların %{Z.get('alici', '')}'i"), fill=TV["up"], font=F(15, True))
    if odak == "fvg":
        for z in (fx.get("fvg") or [])[:2]:
            if hi < z["lo"] or lo > z["hi"]:
                continue
            xs = Xt(z["t"]) if z["t"] >= ts[0] else x0
            col = (41, 182, 246) if z["bull"] else (236, 64, 122)
            d.rectangle([xs, Y(min(hi, z["hi"])), x1, Y(max(lo, z["lo"]))], fill=col + (40,), outline=col + (150,), width=S)
            d.text((xs + 8 * S, Y(min(hi, z["hi"])) + 3 * S), _t("Boğa FVG (boşluk)" if z["bull"] else "Ayı FVG (boşluk)"), fill=col, font=F(15, True))
    FB = fx.get("fib") if odak == "fib" else None
    if FB:
        d.rectangle([x0, Y(max(FB["altin"])), x1, Y(min(FB["altin"]))], fill=TV["yel"] + (26,))
        for r, v in FB["lv"].items():
            if lo <= v <= hi:
                d.line([(x0, Y(v)), (x1, Y(v))], fill=TV["yel"] + (150 if r in ("0.5", "0.618") else 70,), width=S)
                d.text((x0 + 8 * S, Y(v) - 20 * S), f"Fib {r}", fill=TV["yel"], font=F(14, True))
    KN = fx.get("kanal") if odak == "kanal" else None
    if KN:
        for key, w_ in (("ana", 3), ("karsi", 2)):
            (ta, pa), (tb, pb) = KN[key]
            if ta < ts[0] and tb > ta:
                pa = pa + (pb - pa) * (ts[0] - ta) / (tb - ta)
                ta = ts[0]
            d.line([(Xt(ta), Y(pa)), (X(n - 1), Y(pb))], fill=TV["pur"] + (230 if key == "ana" else 140,), width=w_ * S)
        d.text((X(n - 1) - 230 * S, Y(KN["ana_now"]) + 8 * S), _t(KN["ad"]), fill=TV["pur"], font=F(15, True))
    Y_ = fx.get("yapi") if odak == "yapi" else None
    if Y_ and Y_.get("son_tepe") and lo <= Y_["son_tepe"] <= hi:
        _dash(d, x0, Y(Y_["son_tepe"]), x1, Y(Y_["son_tepe"]), TV["acc"], w=2 * S, on=10 * S, off=6 * S)
        d.text((x0 + 8 * S, Y(Y_["son_tepe"]) - 22 * S), _t("Son tepe · yapı kırılımı seviyesi"), fill=TV["acc"], font=F(15, True))
    if P:
        col = TV["up"] if P.get("bull") else TV["dn"]
        pts = [(Xt(t), Y(p)) for t, p in P.get("pts") or [] if t >= ts[0]]
        if len(pts) >= 2:
            d.line(pts, fill=TV["pur"], width=3 * S, joint="curve")
            for (px, py) in pts:
                d.ellipse([px - 6 * S, py - 6 * S, px + 6 * S, py + 6 * S], fill=TV["pur"])
        if P.get("neck"):
            (ta, pa), (tb, pb) = P["neck"]
            d.line([(Xt(max(ta, ts[0])), Y(pa)), (x1, Y(pb))], fill=TV["vwap"], width=3 * S)
            d.text((x0 + 8 * S, Y(pb) - 24 * S), _t(f"{P['ad']} · boyun {fp(P['neck_now'])}"), fill=TV["vwap"], font=F(16, True))
        if P.get("hedef") and lo <= P["hedef"] <= hi:
            _dash(d, X(n - 12), Y(P["hedef"]), x1, Y(P["hedef"]), col, w=2 * S, on=10 * S, off=6 * S)

    # en yakın destek / direnç (1'er)
    lv = fx.get("lv") or []
    res = sorted([x for x in lv if x["p"] > last], key=lambda x: x["p"])[:1]
    sup = sorted([x for x in lv if x["p"] < last], key=lambda x: -x["p"])[:1]
    tags = []
    for x in res + sup:
        if lo < x["p"] < hi and not lines:
            col = TV["dn"] if x["p"] > last else TV["up"]
            _dash(d, x0, Y(x["p"]), x1, Y(x["p"]), col + (120,), w=S, on=6 * S, off=6 * S)
            tags.append([Y(x["p"]), ("Direnç " if x["p"] > last else "Destek ") + fp(x["p"]), col, (255, 255, 255)])

    # hacim
    vmax = max(b[5] for b in B) or 1
    d.line([(x0, vy0 - 6 * S), (x1, vy0 - 6 * S)], fill=TV["grid"], width=S)
    for i, b in enumerate(B):
        col = TV["up"] if b[4] >= b[1] else TV["dn"]
        h_ = b[5] / vmax * (vy1 - vy0)
        d.rectangle([X(i) - cw * 0.36, vy1 - h_, X(i) + cw * 0.36, vy1], fill=col + (95,))
    # EMA 20 + VWAP (normal seans)
    e20 = _ema(closes, 20)
    d.line([(X(i), Y(e20[i])) for i in range(min(19, n - 2), n)], fill=TV["ema"] + (200,), width=2 * S, joint="curve")
    if len(B[0]) > 6:
        pv = vv = 0.0
        day = None
        vw = []
        for i, b in enumerate(B):
            dd = datetime.fromtimestamp(b[0], TR).date()
            if dd != day:
                day, pv, vv = dd, 0.0, 0.0
            if b[6] == 1:
                pv += (b[2] + b[3] + b[4]) / 3 * b[5]
                vv += b[5]
                if vv:
                    vw.append((X(i), Y(pv / vv)))
            elif len(vw) > 1:
                d.line(vw, fill=TV["vwap"] + (210,), width=2 * S, joint="curve")
                vw = []
        if len(vw) > 1:
            d.line(vw, fill=TV["vwap"] + (210,), width=2 * S, joint="curve")
    # mumlar
    for i, b in enumerate(B):
        o, h, l, c = b[1], b[2], b[3], b[4]
        col = TV["up"] if c >= o else TV["dn"]
        d.line([(X(i), Y(h)), (X(i), Y(l))], fill=col, width=max(1, int(1.4 * S)))
        top, bot = Y(max(o, c)), Y(min(o, c))
        if bot - top < S:
            bot = top + S
        d.rectangle([X(i) - cw * 0.38, top, X(i) + cw * 0.38, bot], fill=col)
    # pozisyon kutusu (giriş / stop / hedef)
    if lines.get("giris"):
        e = lines["giris"]
        si = bisect_left(ts, sig_t) if sig_t else n - 4
        si = min(max(0, si), n - 1)
        bx0, bx1 = min(X(si) - cw / 2, x1 - 340 * S), x1
        if lines.get("hedef") and lines.get("stop"):
            h_, s_ = lines["hedef"], lines["stop"]
            d.rectangle([bx0, Y(max(e, h_)), bx1, Y(min(e, h_))], fill=TV["up"] + (48,))
            d.rectangle([bx0, Y(max(e, s_)), bx1, Y(min(e, s_))], fill=TV["dn"] + (48,))
            d.line([(bx0, Y(h_)), (bx1, Y(h_))], fill=TV["up"], width=2 * S)
            d.line([(bx0, Y(s_)), (bx1, Y(s_))], fill=TV["dn"], width=2 * S)
            pot = (h_ / e - 1) * 100
            rsk = (s_ / e - 1) * 100
            rr = abs(h_ - e) / abs(e - s_) if e != s_ else 0
            d.text((bx0 + 10 * S, Y(h_) + (6 if h_ > e else -26) * S), _t(f"Hedef {fp(h_)}  ({pot:+.1f}%)"), fill=TV["up"], font=F(16, True))
            d.text((bx0 + 10 * S, Y(s_) + (6 if s_ > e else -26) * S), _t(f"Stop {fp(s_)}  ({rsk:+.1f}%)"), fill=TV["dn"], font=F(16, True))
            d.text((bx0 + 10 * S, Y(e) - 24 * S), _t(f"Risk/ödül 1:{rr:.1f}"), fill=TV["txt"], font=F(15, True))
            tags.append([Y(h_), "Hedef " + fp(h_), TV["up"], (255, 255, 255)])
            tags.append([Y(s_), "Stop " + fp(s_), TV["dn"], (255, 255, 255)])
        d.line([(bx0, Y(e)), (bx1, Y(e))], fill=TV["acc"], width=2 * S)
        tags.append([Y(e), "Giriş " + fp(e), TV["acc"], (255, 255, 255)])
        if sig_t and ts[0] <= sig_t:
            ax_, ay_ = X(si), Y(B[si][3]) + 14 * S
            d.polygon([(ax_, ay_), (ax_ - 11 * S, ay_ + 18 * S), (ax_ + 11 * S, ay_ + 18 * S)], fill=TV["acc"])
    # son fiyat çizgisi
    lc = TV["up"] if B[-1][4] >= B[-1][1] else TV["dn"]
    _dash(d, x0, Y(last), x1, Y(last), lc + (160,), w=S, on=3 * S, off=4 * S)
    tags.append([Y(last), fp(last), lc, (255, 255, 255)])
    # fiyat ekseni
    d.rectangle([x1, 0, Wf * S, Hf * S], fill=TV["bg"])
    d.line([(x1, py0 - 10 * S), (x1, vy1)], fill=TV["grid"], width=S)
    for g in grid_lbl:
        if all(abs(Y(g) - t[0]) > 22 * S for t in tags):
            d.text((x1 + 12 * S, Y(g) - 10 * S), fp(g), fill=TV["mut"], font=F(15))
    tags.sort(key=lambda t: t[0])
    for i in range(1, len(tags)):
        if tags[i][0] - tags[i - 1][0] < 28 * S:
            tags[i][0] = tags[i - 1][0] + 28 * S
    for y, txt, bgc, fgc in tags:
        f_ = F(14, True)
        d.rounded_rectangle([x1 + 4 * S, y - 13 * S, Wf * S - 4 * S, y + 13 * S], radius=5 * S, fill=bgc)
        d.text((x1 + 10 * S, y - 10 * S), _t(txt if d.textlength(_t(txt), font=f_) < (AX - 14) * S else txt.split(" ")[-1]),
               fill=fgc, font=f_)
    # başlık
    d.rectangle([0, 0, Wf * S, hdr * S], fill=TV["hdr"])
    if kart:
        _kart2(d, kart, tf, S, Wf, hdr, last, B)
    else:
        d.text((24 * S, 18 * S), _t(title[:64]), fill=TV["txt"], font=F(30, True))
        d.text((24 * S, 62 * S), _t(sub[:110]), fill=TV["mut"], font=F(18))
    leg = [("EMA 20", TV["ema"])] + ([("VWAP", TV["vwap"])] if len(B[0]) > 6 else [])
    lx = 24 * S
    for txt, col in leg:
        d.line([(lx, (hdr + 8) * S), (lx + 22 * S, (hdr + 8) * S)], fill=col, width=3 * S)
        d.text((lx + 28 * S, (hdr - 2) * S), txt, fill=TV["mut"], font=F(14))
        lx += (d.textlength(txt, font=F(14)) + 60 * S)
    d.text(((Wf - 300) * S, (hdr - 2) * S), _t(f"ABD·BOT · {tf} · TR saati"), fill=TV["mut"], font=F(14))
    img = img.resize((Wf, Hf), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _kart2(d, k, tf, S, Wf, hdr, last, B):
    al = k.get("yon", 1) > 0
    col = TV["up"] if al else TV["dn"]
    F = lambda sz, b=False: font(sz * S, b)
    d.rectangle([0, 0, 10 * S, hdr * S], fill=col)
    lab = _t(k.get("etiket") or ("AL" if al else "SAT"))
    lw = d.textlength(lab, font=F(26, True))
    d.rounded_rectangle([30 * S, 22 * S, 30 * S + lw + 34 * S, 66 * S], radius=12 * S, fill=col)
    d.text((47 * S, 27 * S), lab, fill=(255, 255, 255), font=F(26, True))
    sx = 30 * S + lw + 52 * S
    d.text((sx, 16 * S), _t("#" + k.get("sym", "")), fill=TV["txt"], font=F(42, True))
    pw = d.textlength(_t("#" + k.get("sym", "")), font=F(42, True))
    d.text((sx + pw + 22 * S, 30 * S), fp(last), fill=TV["txt"], font=F(26, True))
    d.text((30 * S, 80 * S), _t(k.get("ad", "")[:52]), fill=TV["txt"], font=F(22, True))
    if k.get("alt"):
        d.text((30 * S, 114 * S), _t(k["alt"][:80]), fill=TV["mut"], font=F(17))
    rx = (Wf - 430) * S
    if k.get("conf") is not None:
        c = int(k["conf"])
        derece = "A+" if c >= 85 else "A" if c >= 75 else "B" if c >= 65 else "C"
        d.text((rx, 18 * S), _t(f"Güven {c}"), fill=TV["txt"], font=F(22, True))
        d.rounded_rectangle([rx + 150 * S, 16 * S, rx + 206 * S, 48 * S], radius=8 * S, fill=col)
        d.text((rx + 162 * S, 19 * S), derece, fill=(255, 255, 255), font=F(22, True))
        for i in range(10):
            d.rounded_rectangle([rx + i * 40 * S, 58 * S, rx + i * 40 * S + 32 * S, 70 * S], radius=4 * S,
                                fill=(col if i < round(c / 10) else (52, 58, 76)))
    y = 82
    for lbl, val, cc in (("Risk/ödül", k.get("rr"), TV["txt"]), ("Potansiyel", k.get("pot"), TV["up"]),
                         ("Risk", k.get("risk"), TV["dn"])):
        if val:
            d.text((rx, y * S), _t(lbl), fill=TV["mut"], font=F(16))
            d.text((rx + 130 * S, (y - 2) * S), _t(val), fill=cc, font=F(18, True))
            y += 21


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


def kolaj(items, title="", sub=""):
    """Birden çok hissenin mini grafiği tek resimde (sabah listesi). items: [(sembol, yüzde, mumlar[(t,o,h,l,c,v)])]"""
    items = [x for x in items if x[2] and len(x[2]) >= 10][:6]
    if not items:
        return None
    cols = 3 if len(items) > 4 else 2
    rows = (len(items) + cols - 1) // cols
    cw, ch, top = 360, 250, 96
    Wk = cols * cw
    img = Image.new("RGB", (Wk, top + rows * ch), BG)
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, Wk, top - 12], fill=(18, 22, 36))
    d.text((PAD_L, 14), _t(title[:60]), fill=TXT, font=font(26, True))
    d.text((PAD_L, 52), _t(sub[:90]), fill=MUT, font=font(16))
    for n, (sym, pct, bars) in enumerate(items):
        cx, cy = (n % cols) * cw, top + (n // cols) * ch
        d.rounded_rectangle([cx + 8, cy + 6, cx + cw - 8, cy + ch - 8], radius=16, fill=(18, 22, 36))
        col = UP if (pct or 0) >= 0 else DN
        d.text((cx + 24, cy + 16), _t("#" + sym), fill=TXT, font=font(22, True))
        d.text((cx + cw - 130, cy + 18), _t(f"{pct:+.1f}%" if pct is not None else ""), fill=col, font=font(20, True))
        B = bars[-60:]
        hi = max(b[2] for b in B)
        lo = min(b[3] for b in B)
        rg = (hi - lo) or hi * 0.01
        x0, x1, y0, y1 = cx + 24, cx + cw - 24, cy + 56, cy + ch - 40
        bw = (x1 - x0) / len(B)
        Y = lambda p: y1 - (p - lo) / rg * (y1 - y0)
        for i, b in enumerate(B):
            c_ = UP if b[4] >= b[1] else DN
            xm = x0 + (i + 0.5) * bw
            d.line([(xm, Y(b[2])), (xm, Y(b[3]))], fill=c_, width=1)
            t_, b_ = Y(max(b[1], b[4])), Y(min(b[1], b[4]))
            d.rectangle([xm - bw * 0.35, t_, xm + bw * 0.35, max(b_, t_ + 1)], fill=c_)
        d.text((cx + 24, cy + ch - 34), _t(f"son {fp(B[-1][4])}  ·  tepe {fp(hi)}  ·  dip {fp(lo)}"), fill=MUT, font=font(14))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
