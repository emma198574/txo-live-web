# -*- coding: utf-8 -*-
"""
雙市場摘要.py

把「台指選擇權 T 字」「那斯達克 NDX 選擇權 T 字」「小那期貨價格」合在同一頁：
上面一條摘要列同時看三邊，下面兩個分頁各自是原本完整的 T 字報價。

只給 Vercel 的 api/index.py 用（路徑 /dual），不寫檔、不推播。

架構（為什麼這樣拆）：
  - 外框頁 /dual 本身不抓任何報價，打開是 0 秒。
  - 兩個分頁用 iframe 各自載入 /（台指）與 /ndx（那斯達克），是兩次獨立的
    function 呼叫、平行跑，不會疊加成一次逼近 Vercel 時間上限的長請求。
    兩頁原本的 CSS／JS／60 秒自動重整完全不用改，iframe 裡的
    location.replace(location.pathname) 只會重整那個 iframe。
  - 摘要數字不另外抓：api/index.py 產 / 與 /ndx 時，順手把本檔 txo_summary()／
    ndx_summary() 算出的 JSON 塞進頁尾。外框在 iframe 每次 load（含 60 秒重整）
    時讀出來更新摘要列 —— 同網域，讀得到 contentDocument。
  - 小那期貨另走 /mnq（Yahoo chart，約延遲 10 分鐘），外框每 60 秒輪詢一次。
    MNQ 的選擇權只在 CME、官網擋自動抓取，沒有免費來源；這裡只有期貨價格。

摘要列只放「事實」：價格、漲跌、資料時間、OI／成交量最大的履約價、P/C 量比。
OI 牆當撐壓已回測否證（三種牆回測），莊家意圖四象限尚未驗證，
所以畫面上一律標成「位置」與「未驗證」，不寫成支撐／壓力。
"""

import json
import html
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")
ET_TZ = ZoneInfo("America/New_York")

# Yahoo chart 端點會擋完整的 Chrome UA（實測回 429），短版 "Mozilla/5.0" 反而會過
YAHOO_UA  = "Mozilla/5.0"
YAHOO_URL = "https://{host}.finance.yahoo.com/v8/finance/chart/{sym}"


def _pc(rep):
    """P/C 量比：畫面視窗內賣權總口數 ÷ 買權總口數。"""
    cv = sum(r["vol"] for r in rep["crows"].values())
    pv = sum(r["vol"] for r in rep["prows"].values())
    return round(pv / cv, 2) if cv else None


def txo_summary(page):
    """台指摘要：取第一個分頁（最近結算）。"""
    r = page["reps"][0]
    z, m = r["zone"], r["mind"]
    return {
        "under": page["under"], "usrc": page["usrc"], "session": page["session"],
        "tab": r["tab"], "time": r["time"], "stale": r["stale"], "epoch": page["epoch"],
        "mind": m["title"],
        "res_k": z["res_k"], "sup_k": z["sup_k"], "c_wall": z["c_wall"], "p_wall": z["p_wall"],
        "pc": _pc(r),
    }


def ndx_summary(page):
    """NDX 摘要：取第一個分頁（最近到期）。"""
    r = page["reps"][0]
    z = r["zone"]
    # 新鮮度用「最後成交時間」而不是抓取時間：CBOE 本身延遲 15 分，用抓取時間
    # 會顯示「幾秒前」，看起來比實際新。r["time"] 是美東 "MM-DD HH:MM:SS"。
    try:
        y  = datetime.now(ET_TZ).year
        qt = datetime.strptime(f'{y}-{r["time"]}', "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET_TZ)
        q_epoch = int(qt.timestamp())
    except ValueError:
        q_epoch = page["epoch"]
    return {
        "under": page["under"], "chg": page["chg"], "session": page["session"],
        "iv30": page["iv30"], "tab": r["tab"], "time": r["time"], "stale": r["stale"],
        "epoch": q_epoch,
        "res_k": z["res_k"], "sup_k": z["sup_k"], "c_wall": z["c_wall"], "p_wall": z["p_wall"],
        "pc": _pc(r),
    }


def inject(html_out, summary):
    """把摘要 JSON 塞進頁面；外框從 iframe 裡用 id 讀。單獨打開這頁時看不到，不影響畫面。"""
    js = json.dumps(summary, ensure_ascii=False, default=str).replace("</", "<\\/")
    return html_out + f'\n<script type="application/json" id="dual-sum">{js}</script>\n'


def fetch_mnq(sym="MNQ=F"):
    """小那期貨（CME Globex 幾乎 23 小時交易）。query1 被擋就換 query2。"""
    last = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(YAHOO_URL.format(host=host, sym=sym),
                             params={"interval": "1m", "range": "1d"},
                             headers={"User-Agent": YAHOO_UA}, timeout=8)
            r.raise_for_status()
            m = r.json()["chart"]["result"][0]["meta"]
            px, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
            ts = m.get("regularMarketTime")
            t = datetime.fromtimestamp(ts, timezone.utc) if ts else None
            return {
                "ok": True, "px": px, "prev": prev,
                "chg": round((px / prev - 1) * 100, 2) if px and prev else None,
                "hi": m.get("regularMarketDayHigh"), "lo": m.get("regularMarketDayLow"),
                "epoch": ts,
                "t_tw": t.astimezone(TW_TZ).strftime("%H:%M") if t else None,
                "t_et": t.astimezone(ET_TZ).strftime("%H:%M") if t else None,
            }
        except Exception as e:
            last = e
    return {"ok": False, "err": str(last)}


def render_shell():
    """外框頁：摘要列＋兩個分頁（iframe）。不含任何報價，資料全由瀏覽器端組。"""
    return SHELL


SHELL = '''<!doctype html>
<meta charset="utf-8">
<title>雙市場 T 字</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="雙市場T字">
<meta name="theme-color" content="#17181a">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="/icon.png">
<style>
:root{--bg:#f7f6f3;--panel:#fff;--ink:#1c1b19;--muted:#6b6862;--line:#e7e4dd;
  --up:#c0392b;--down:#1e7a3c;--warn:#b8860b;--accent:#2f6fdb;}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#17181a;--panel:#1f2124;
  --ink:#ececec;--muted:#9a9790;--line:#2e3033;--up:#ff6b5c;--down:#54c777;--warn:#d8b24a;--accent:#5b8ff0;}}
:root[data-theme=dark]{--bg:#17181a;--panel:#1f2124;--ink:#ececec;--muted:#9a9790;
  --line:#2e3033;--up:#ff6b5c;--down:#54c777;--warn:#d8b24a;--accent:#5b8ff0;}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);display:flex;flex-direction:column;
  height:100dvh;overflow:hidden;
  font-family:-apple-system,"PingFang TC","Helvetica Neue",Arial,sans-serif;
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;}
header{flex:none;padding:calc(8px + env(safe-area-inset-top)) calc(10px + env(safe-area-inset-right))
  6px calc(10px + env(safe-area-inset-left));border-bottom:1px solid var(--line);background:var(--bg)}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;max-width:1080px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-width:0}
.card h2{margin:0;font-size:11.5px;font-weight:600;color:var(--muted);display:flex;
  justify-content:space-between;gap:4px;white-space:nowrap}
.card h2 .age{font-weight:500}
.age.stale{color:var(--warn)} .age.dead{color:var(--up)}
.px{font-size:19px;font-weight:700;margin:2px 0 1px;white-space:nowrap}
.px small{font-size:12px;font-weight:600;margin-left:4px}
.up{color:var(--up)} .down{color:var(--down)}
.row{display:flex;justify-content:space-between;gap:6px;font-size:11.5px;line-height:1.65;white-space:nowrap}
.row > span:first-child{color:var(--muted)}
.row b{font-weight:600}
.tag{display:inline-block;font-size:9.5px;padding:0 4px;border-radius:4px;border:1px solid var(--line);
  color:var(--muted);margin-left:3px;vertical-align:1px}
.note{max-width:1080px;margin:5px auto 0;font-size:10.5px;color:var(--muted);line-height:1.5}
#info{display:none;flex:none!important;width:34px;font-weight:700;color:var(--muted)!important}
nav{display:flex;gap:6px;max-width:1080px;margin:7px auto 0}
nav button{flex:1;border:1px solid var(--line);background:var(--panel);color:var(--ink);
  border-radius:8px;padding:7px 0;font-size:13.5px;font-weight:600;font-family:inherit;cursor:pointer}
nav button.on{background:var(--accent);border-color:var(--accent);color:#fff}
main{flex:1;position:relative;min-height:0}
iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:var(--bg);visibility:hidden}
iframe.on{visibility:visible}
@media(max-width:640px){
  .cards{gap:5px} .card{padding:6px 7px}
  .px{font-size:15px} .px small{display:block;margin:0;font-size:11px}
  .card h2{font-size:10.5px}
  /* 窄螢幕上「50,000 / 48,200」跟標籤擠一行會被截斷，改成標籤在上、數值在下 */
  .row{display:block;font-size:10.5px;line-height:1.3;margin-top:3px}
  .row > span:first-child{display:block;font-size:9.5px}
  .tag{font-size:8.5px;padding:0 3px}
  .hide-sm{display:none}
  .note{display:none}
  .note.open{display:block}
  #info{display:inline-block}
}
</style>
<header>
  <div class="cards">
    <div class="card" id="c-txo">
      <h2><span>台指 <span id="txo-sess"></span></span><span class="age" id="txo-age">載入中</span></h2>
      <div class="px" id="txo-px">—</div>
      <div class="row"><span>四象限<span class="tag">未驗證</span></span><b id="txo-mind">—</b></div>
      <div class="row"><span>量最大 C/P</span><b id="txo-vol">—</b></div>
      <div class="row"><span>OI最大 C/P</span><b id="txo-oi">—</b></div>
      <div class="row"><span>P/C量比</span><b id="txo-pc">—</b></div>
      <div class="row hide-sm"><span>依據分頁</span><b id="txo-tab">—</b></div>
    </div>
    <div class="card" id="c-ndx">
      <h2><span>NDX <span id="ndx-sess"></span></span><span class="age" id="ndx-age">載入中</span></h2>
      <div class="px" id="ndx-px">—</div>
      <div class="row"><span>IV30</span><b id="ndx-iv">—</b></div>
      <div class="row"><span>量最大 C/P</span><b id="ndx-vol">—</b></div>
      <div class="row"><span>OI最大 C/P</span><b id="ndx-oi">—</b></div>
      <div class="row"><span>P/C量比</span><b id="ndx-pc">—</b></div>
      <div class="row hide-sm"><span>依據分頁</span><b id="ndx-tab">—</b></div>
    </div>
    <div class="card" id="c-mnq">
      <h2><span>小那 MNQ</span><span class="age" id="mnq-age">載入中</span></h2>
      <div class="px" id="mnq-px">—</div>
      <div class="row"><span>日高</span><b id="mnq-hi">—</b></div>
      <div class="row"><span>日低</span><b id="mnq-lo">—</b></div>
      <div class="row"><span>對NDX價差</span><b id="mnq-basis">—</b></div>
      <div class="row"><span>報價時間</span><b id="mnq-t">—</b></div>
      <div class="row hide-sm"><span>來源</span><b>Yahoo 延遲約10分</b></div>
    </div>
  </div>
  <div class="note">台指＝MIS 即時；NDX＝CBOE 延遲 15 分；小那＝Yahoo 延遲約 10 分。三邊時間不同，別拿來判斷誰領先誰。
    「量最大／OI最大」只是位置，當支撐壓力用已回測否證。</div>
  <nav>
    <button data-k="txo">台指 T 字</button>
    <button data-k="ndx">那斯達克 T 字</button>
    <button id="info" aria-label="資料說明">ⓘ</button>
  </nav>
</header>
<main>
  <iframe id="f-txo" src="/" title="台指 T 字"></iframe>
  <iframe id="f-ndx" src="/ndx" title="那斯達克 T 字"></iframe>
</main>
<script>
(function(){
  function $(id){ return document.getElementById(id); }
  function fmt(v, d){ return (v === null || v === undefined) ? '—'
    : Number(v).toLocaleString('en-US', {minimumFractionDigits: d||0, maximumFractionDigits: d||0}); }
  function pair(a, b){ return fmt(a) + ' / ' + fmt(b); }
  function chgHtml(c){
    if(c === null || c === undefined) return '';
    var cls = c > 0 ? 'up' : (c < 0 ? 'down' : '');
    return '<small class="' + cls + '">' + (c > 0 ? '+' : '') + c.toFixed(2) + '%</small>';
  }

  // ── 新鮮度：各自的門檻沿用原頁（台指 15/40 分、NDX 30/60 分），小那 20/60 分 ──
  var ages = {};
  function tickAge(){
    var now = Date.now() / 1000;
    Object.keys(ages).forEach(function(k){
      var a = ages[k], el = $(k + '-age');
      if(!a.epoch){ return; }
      var sec = Math.max(0, Math.floor(now - a.epoch));
      el.textContent = sec < 60 ? sec + '秒前' : (sec < 3600 ? Math.floor(sec/60) + '分前'
                       : Math.floor(sec/3600) + '時' + Math.floor((sec%3600)/60) + '分前');
      el.className = 'age' + (sec >= a.dead ? ' dead' : (sec >= a.stale ? ' stale' : ''));
    });
  }
  setInterval(tickAge, 10000);

  // ── 從 iframe 讀摘要 JSON ──
  var ndxSum = null, mnq = null;
  function readSum(frame){
    try{
      var el = frame.contentDocument && frame.contentDocument.getElementById('dual-sum');
      return el ? JSON.parse(el.textContent) : null;
    }catch(e){ return null; }
  }
  function onTxo(){
    var s = readSum($('f-txo'));
    if(!s){ $('txo-age').textContent = '取不到'; return; }
    $('txo-sess').textContent = s.session;
    $('txo-px').innerHTML = fmt(s.under) + (s.stale ? '<small>最後成交</small>' : '');
    $('txo-mind').textContent = s.mind;
    $('txo-vol').textContent = pair(s.res_k, s.sup_k);
    $('txo-oi').textContent = pair(s.c_wall, s.p_wall);
    $('txo-pc').textContent = fmt(s.pc, 2);
    $('txo-tab').textContent = s.tab;
    ages.txo = {epoch: s.epoch, stale: 900, dead: 2400};
    tickAge();
  }
  function onNdx(){
    var s = readSum($('f-ndx'));
    if(!s){ $('ndx-age').textContent = '取不到'; return; }
    ndxSum = s;
    $('ndx-sess').textContent = s.session.replace('美股', '');
    $('ndx-px').innerHTML = fmt(s.under, 1) + chgHtml(s.chg);
    $('ndx-iv').textContent = s.iv30 ? s.iv30.toFixed(1) + '%' : '—';
    $('ndx-vol').textContent = pair(s.res_k, s.sup_k);
    $('ndx-oi').textContent = pair(s.c_wall, s.p_wall);
    $('ndx-pc').textContent = fmt(s.pc, 2);
    $('ndx-tab').textContent = s.tab;
    ages.ndx = {epoch: s.epoch, stale: 1800, dead: 3600};
    tickAge(); drawBasis();
  }
  // 小那對 NDX 價差：只在美股盤中才算（盤外 NDX 是昨收，算出來沒意義）。
  // 兩邊延遲不同（約 5 分鐘時間差），所以只當參考、不給正常範圍。
  function drawBasis(){
    var el = $('mnq-basis');
    if(!mnq || !mnq.ok || !ndxSum || !ndxSum.under){ el.textContent = '—'; return; }
    if(ndxSum.session !== '美股盤中'){ el.textContent = 'NDX休市'; return; }
    var b = mnq.px - ndxSum.under;
    el.textContent = (b > 0 ? '+' : '') + fmt(b, 0) + ' 點';
  }
  function loadMnq(){
    fetch('/mnq?t=' + Date.now(), {cache: 'no-store'}).then(function(r){ return r.json(); })
    .then(function(d){
      mnq = d;
      if(!d.ok){ $('mnq-age').textContent = '取不到'; return; }
      $('mnq-px').innerHTML = fmt(d.px, 2) + chgHtml(d.chg);
      $('mnq-hi').textContent = fmt(d.hi, 2);
      $('mnq-lo').textContent = fmt(d.lo, 2);
      $('mnq-t').innerHTML = d.t_tw ? d.t_tw + '<span class="hide-sm">（美東' + d.t_et + '）</span>' : '—';
      ages.mnq = {epoch: d.epoch, stale: 1200, dead: 3600};
      tickAge(); drawBasis();
    }).catch(function(){ $('mnq-age').textContent = '取不到'; });
  }

  // iframe 每次 load（含頁內 60 秒自動重整）都會觸發，摘要跟著更新
  $('f-txo').addEventListener('load', onTxo);
  $('f-ndx').addEventListener('load', onNdx);
  loadMnq();
  setInterval(loadMnq, 60000);

  // ── 分頁切換；記住上次看哪一頁（只是便利，存不了也照常運作）──
  function show(k){
    ['txo', 'ndx'].forEach(function(x){
      $('f-' + x).classList.toggle('on', x === k);
      document.querySelector('nav button[data-k=' + x + ']').classList.toggle('on', x === k);
    });
    try{ localStorage.setItem('dual-tab', k); }catch(e){}
  }
  $('info').addEventListener('click', function(){ document.querySelector('.note').classList.toggle('open'); });
  document.querySelectorAll('nav button[data-k]').forEach(function(b){
    b.addEventListener('click', function(){ show(b.getAttribute('data-k')); });
  });
  var saved = null;
  try{ saved = localStorage.getItem('dual-tab'); }catch(e){}
  show(saved === 'ndx' ? 'ndx' : 'txo');
})();
</script>
'''
