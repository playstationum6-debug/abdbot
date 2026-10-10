"""
$ABDBOT tanıtım sayfası (şifresiz, hızlı açılır): TikTok / Instagram profil linki için.
Veriyi /api/tanitim'den çeker: canlı durum, şeffaf sicil özeti, son kademeler ve girseydin kartı.
"""

OG = """<meta property="og:type" content="website"><meta property="og:site_name" content="$ABDBOT">
<meta property="og:title" content="$ABDBOT · Kırılımı bot yakalasın">
<meta property="og:description" content="ABD borsasında kırılım seviyelerini önceden çizen, hacimle onaylayan ve sonuçlarını açıkça paylaşan yapay zekâ destekli bot. Paper trade · yatırım tavsiyesi değildir.">
<meta property="og:image" content="__SITE__/og.jpg"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta property="og:url" content="__SITE__/tanitim"><meta name="twitter:card" content="summary_large_image">
<meta name="description" content="ABD borsasında kırılım seviyelerini önceden çizen, hacimle onaylayan ve sonuçlarını açıkça paylaşan yapay zekâ destekli bot. Paper trade · yatırım tavsiyesi değildir.">"""

HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0A1124">
<title>$ABDBOT · Kırılımı bot yakalasın</title>
""" + OG + """
<link rel="icon" href="/ikon-192.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--up:#22C55E;--up2:#4ADE80;--dn:#F43F5E;--acc:#5B97FF;--tg:#2AABEE;--txt:#EEF2F8;--mut:#9AA6BC;--dim:#5E6A82;--card:rgba(18,25,41,.86);--line:rgba(160,185,235,.14)}
html{background:#060911}
body{font-family:Inter,system-ui,-apple-system,sans-serif;color:var(--txt);-webkit-font-smoothing:antialiased;min-height:100vh;
 background:radial-gradient(90% 50% at 10% 0%,rgba(51,102,240,.35),transparent 65%),radial-gradient(70% 40% at 90% 18%,rgba(34,197,94,.16),transparent 70%),linear-gradient(180deg,#0A1124 0%,#060911 60%)}
.w{max-width:560px;margin:0 auto;padding:22px 18px 40px}
.top{display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:10px;font-weight:900;font-size:19px;letter-spacing:-.4px}
.brand img{width:36px;height:36px;border-radius:10px;box-shadow:0 6px 18px rgba(51,102,240,.5)}
.pill{font-size:10.5px;font-weight:800;letter-spacing:1.4px;color:#B9CCF2;border:1px solid rgba(155,180,230,.28);padding:6px 10px;border-radius:99px;display:flex;align-items:center;gap:6px}
.pill i{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.pill.on i{background:var(--up);box-shadow:0 0 8px var(--up)}
h1{margin:34px 0 0;font-size:48px;line-height:.98;font-weight:900;letter-spacing:-2px}
h1 em{font-style:normal;background:linear-gradient(90deg,#4ADE80,#22C55E 60%,#A3E635);-webkit-background-clip:text;background-clip:text;color:transparent}
.k{font-size:11.5px;font-weight:800;letter-spacing:2.4px;color:var(--acc);margin-top:28px}
.s{margin-top:14px;font-size:16px;line-height:1.5;color:var(--mut)}.s b{color:var(--txt)}
.cta{display:flex;flex-direction:column;gap:10px;margin-top:22px}
.btn{display:flex;align-items:center;justify-content:center;gap:10px;height:54px;border-radius:16px;font-weight:800;font-size:16px;text-decoration:none;color:#fff}
.btn.tg{background:var(--tg);box-shadow:0 12px 30px rgba(42,171,238,.4)}
.btn.sec{background:rgba(255,255,255,.06);border:1px solid var(--line);color:var(--txt)}
.btn svg{width:22px;height:22px}
.dur{margin-top:14px;font-size:13px;color:var(--mut);text-align:center}
h2{font-size:22px;letter-spacing:-.5px;margin:38px 0 12px}
.st{display:grid;gap:10px}
.st div{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;display:flex;gap:12px;align-items:flex-start}
.st b{display:block;font-size:15px}.st small{display:block;color:var(--mut);font-size:13px;line-height:1.45;margin-top:3px}
.st span{width:30px;height:30px;border-radius:10px;background:rgba(91,151,255,.15);color:var(--acc);font-weight:900;display:flex;align-items:center;justify-content:center;flex:none}
.oz{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.oz div{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px 14px}
.oz small{display:block;font-size:11.5px;color:var(--mut);font-weight:600}.oz b{font-size:24px;font-weight:900;letter-spacing:-.5px}
.up{color:var(--up)}.dn{color:var(--dn)}
.not{color:var(--dim);font-size:12px;line-height:1.5;margin-top:10px}
.kart img{width:100%;display:block;border-radius:16px;border:1px solid var(--line);margin-bottom:10px;background:#0b1020}
.son{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:4px 14px}
.son div{display:flex;align-items:center;gap:10px;padding:11px 0;border-top:1px solid var(--line);font-size:14px}
.son div:first-child{border-top:0}.son i{font-style:normal;color:var(--mut);font-size:12px;width:76px}
.son b{flex:1}.son span{font-weight:800}
footer{margin-top:40px;color:var(--dim);font-size:11.5px;line-height:1.6;text-align:center}
footer a{color:var(--mut)}
</style></head><body><div class="w">
<div class="top"><div class="brand"><img src="/ikon-192.png" alt="">$ABDBOT</div><div class="pill" id="dp"><i></i><span id="dt">BAĞLANIYOR</span></div></div>
<div class="k">YAPAY ZEKÂ · ABD BORSASI · KIRILIM TAKİBİ</div>
<h1>Kırılımı<br><em>bot yakalasın.</em></h1>
<p class="s">Her gün 1.200+ ABD hissesini tarar. Seviyeyi <b>önceden çizer</b>, kırılımı <b>hacimle onaylar</b>, giriş · stop · K1·K2·K3 kademelerini <b>Telegram'a yazar</b>. Her sinyalin sonucu açıkça ölçülür.</p>
<div class="cta" id="cta">
 <a class="btn tg" id="bTg" href="#" target="_blank" rel="noopener" style="display:none"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M21.9 4.6 18.8 19c-.2 1-.9 1.3-1.7.8l-4.7-3.5-2.3 2.2c-.3.3-.5.5-1 .5l.3-4.8 8.8-7.9c.4-.3-.1-.5-.6-.2L6.8 12.9l-4.6-1.4c-1-.3-1-1 .2-1.5L20.5 3c.9-.3 1.6.2 1.4 1.6z"/></svg>Telegram kanalına katıl</a>
 <a class="btn sec" id="bGrup" href="#" target="_blank" rel="noopener" style="display:none">💬 Sohbet grubu</a>
 <a class="btn sec" href="/">Canlı siteyi aç →</a>
</div>
<div class="dur" id="dur"></div>

<h2>Nasıl çalışır?</h2>
<div class="st">
 <div><span>1</span><p><b>Tarar</b><small>Haberi, hacmi ve hareketi olan hisseleri öne çıkarır. Açılıştan önce "Günün Listesi"ni hazırlar.</small></p></div>
 <div><span>2</span><p><b>Seviyeyi önceden çizer</b><small>"X kırılırsa giriş" planını kırılımdan önce paylaşır: gün tepesi, taban, tam sayı, kaybedilen seviye.</small></p></div>
 <div><span>3</span><p><b>Onaylar, yazar</b><small>Hacim ve mum kapanışıyla onaylanan kırılım 5 denetçiden geçer; uygunsa Telegram'a resimli bildirim düşer.</small></p></div>
 <div><span>4</span><p><b>Açıkça ölçer</b><small>Kazanan da kaybeden de şeffaf sicile işlenir; zarar ettiren kurguyu bot kendisi susturur.</small></p></div>
</div>

<h2>Şeffaf sicil · son 30 gün</h2>
<div class="oz" id="oz"><div><small>Yükleniyor…</small><b>—</b></div></div>
<p class="not">Telegram'a giden her sinyal gönderildiği saatle kaydedilir; kazananlar da kaybedenler de sayılır. Sonuç = girişten çıkışa yüzde değişim (masraf ve kayma payı dahil).</p>
<div id="sonW" style="display:none"><h2>Son sonuçlar</h2><div class="son" id="son"></div></div>
<div id="kW" style="display:none"><h2>Son "tüm kademeler tamam"</h2><div class="kart" id="k"></div></div>
<div id="gW" style="display:none"><h2>Girseydin</h2><div class="kart" id="g"></div><p class="not">Geriye dönük hesap: girişten sonraki en yüksek fiyata göre. Zirveden satmak her zaman mümkün değildir.</p></div>

<footer>Burada yer alan bilgi, yorum ve sinyaller yatırım danışmanlığı kapsamında değildir; kurallara dayalı bir yazılımın ürettiği otomatik senaryolardır ve mali durumunuza uygun olmayabilir. Bot sanal (paper) hesapla çalışır; gerçek para kullanılmaz. Geçmiş sonuçlar geleceği garanti etmez.<br><br>$ABDBOT · <a href="/">Canlı site</a></footer>
</div>
<script>
const $=i=>document.getElementById(i);
const fP=v=>v==null?'—':(v>0?'+':v<0?'−':'')+Math.abs(v).toFixed(Math.abs(v)<10?2:1).replace('.',',')+'%';
const fp=v=>v==null?'—':(v>=1?v.toFixed(2):v.toFixed(4)).replace('.',',');
const ne=t=>{const d=Math.max(0,Math.round(Date.now()/1000-t));return d<90?'az önce':d<3600?Math.round(d/60)+' dk önce':d<86400?Math.round(d/3600)+' sa önce':Math.round(d/86400)+' gün önce'};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
fetch('/api/tanitim'+location.search).then(r=>r.json()).then(d=>{
  if(d.tg_link){$('bTg').href=d.tg_link;$('bTg').style.display='flex'}
  if(d.grup_link){$('bGrup').href=d.grup_link;$('bGrup').style.display='flex'}
  const D=d.durum||{};$('dp').classList.toggle('on',!!D.calisiyor);$('dt').textContent=D.calisiyor?'CANLI':'BEKLEMEDE';
  $('dur').textContent=[D.calisiyor?'🟢 Bot çalışıyor':'⚪ Bot beklemede',D.son_sinyal?'son sinyal '+ne(D.son_sinyal):'',D.seans].filter(Boolean).join(' · ');
  const o=(d.sicil||{}).ozet||{};
  $('oz').innerHTML=`<div><small>Paylaşılan sinyal</small><b>${o.paylasilan||0}</b></div><div><small>Sonuçlanan</small><b>${o.n||0}</b></div>
   <div><small>Kazanan</small><b>${o.wr==null?'—':'%'+o.wr}</b></div><div><small>İşlem başı ort.</small><b class="${o.avg>0?'up':o.avg<0?'dn':''}">${fP(o.avg)}</b></div>`;
  const S=d.son||[];if(S.length){$('sonW').style.display='';$('son').innerHTML=S.map(x=>`<div><i>${new Date(x.t*1000).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}</i><b>#${esc(x.sym)} <small style="color:var(--mut);font-weight:500">${esc(x.ad||'')}</small></b><span class="${x.pct>0?'up':'dn'}">${fP(x.pct)}</span></div>`).join('')}
  const K=d.kademe||[];if(K.length){$('kW').style.display='';$('k').innerHTML=K.map(k=>`<img loading="lazy" src="/kart/kademe/${encodeURIComponent(k.sid)}.png" alt="#${esc(k.sym)} tüm kademeler tamam">`).join('')}
  if(d.girseydin){$('gW').style.display='';$('g').innerHTML=`<img loading="lazy" src="/kart/girseydin/${encodeURIComponent(d.girseydin)}.png" alt="Girseydin">`}
}).catch(()=>{$('dur').textContent='Bağlantı kurulamadı, birazdan tekrar dene.'});
</script></body></html>"""


def sayfa(site):
    return HTML.replace("__SITE__", site)
