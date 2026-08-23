# -*- coding: utf-8 -*-
"""
Vercel serverless function：打開網址當場抓報價、當場產 T 字報價網頁。

跟 GitHub Actions 那條路的差別只在「什麼時候跑」，計算邏輯完全共用同一支
即時選擇權T字報價.py：

  - Actions：排程／手動觸發時跑，產物推 gh-pages 給 Cloudflare 發佈。
             負責 ntfy 推播與莊家意圖歷史 CSV 累積，從 2–4 分鐘前的快照看起。
  - 這裡：  每次有人打開網址才跑，回應就是那一瞬間的報價（約 2–5 秒）。
             不推播、不寫任何檔案。

**兩條路並存，不互相取代。** 這支掛掉時 Cloudflare 那個網址照樣有排程產出的頁，
所以下面的錯誤頁一定要把備援網址印出來。

只呼叫 build_page() / render_html()，這兩個函式都不碰檔案系統——Vercel 唯讀，
會寫檔的 --out、歷史 CSV、ntfy 都在原腳本的 main() 裡，繞過即可。
"""

import os
import sys
import html
import base64
import traceback
import importlib.util
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT    = os.path.join(BASE_DIR, "即時選擇權T字報價.py")
FALLBACK  = "https://txo-live.pages.dev/"

# Vercel 的 Python preset 是 catch-all：**所有**路徑都進這支 function，
# vercel.json 的 rewrites 與相鄰的 api/*.py 一律被忽略。所以圖示不能另開一支
# function，得在這裡自己分派路徑。
#
# 這同時擋掉一個真實的浪費：iOS 加到主畫面時會自動探測好幾個 icon 路徑，
# 在 catch-all 之下每一次探測都會跑完整的 build_page()——白抓一次 MIS、
# 花掉兩三秒。下面在最前面就攔下來直接回 PNG，完全不碰報價。
ICON = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAADo0lEQVR42u3bsW4cVRSA4XvGdxx7Z7NSUtBGltImBTVvwNNQUNCClHfIg1BT0CAooLMEBQ0FoiCWIiXBO3MPhWMJCXaLJItmvN8ntx7dHf++MzuaE48uHhf4L51TgDgQB+JAHIgDcSAOxIE4EAeIA3EgDsSBOBAH4kAciANxgDgQB+JAHIgDcSAOxIE4EAeIA3EgDsSBOBAH4mBh6uEOHQs8HbnIRS8wjm3m4s7zybS8NqKLA/0j1sNtGw/7GhG5hC3kZpFZyqs+lrV5RInpzTbHPMRZrofIYpv58LT/+pMnm76Wpewfrbw66z7/9KOX97rayjIW3TLO68/Pvr366fe66rPlknaO4bRfUBxnp11//7SeLSmO7ryPGgda7mHvOW4s47KSZZuZY+aYuZw4csxF3pDG7d3GIi7ib1cb/1j3YhbtOQfFQzDEgTgQB+JAHCAOxIE4EAfiQByIA3EgDsSBOEAclLm8QzpHEXvfx4woNz9l96vIKY7yIV6NLm1mb593re0ZTShTTjmNmXsS6EpElrlMarX84LMq/9PcylC7rs5nqClLRNbTndOmreRZN9RVO+n2DIJc16l1GTPZQFp2qz66Q4Uajy4eH2IMpI/4+MH6JLoyg9MYEdM4rTebL58/HzabaZpix6Vjzzhka7k6P//sqy+++/GHYTVktlnM23Tx6tcX25fXcfLhR5vqgbaNbeY3f1zlXO40YhzHB3+16/PVMNzPcbvrvmLYPXPaog398OaXF39+/9v2/qa1No8Pl929Gt1Bht4OeFnZ9LXMJ44om75Gm8o0lmnaFccU+6fLprhX63Bah342cZTMXN445DSbKdkoZcqcMku5/TqyI47Yd5CMEiUz29sfzzkoHoKBOBAH4kAciANxIA7EgTgQB4gDcSAOxIE4EAfiQByIA3GAOBAH4kAciANxIA7EgTgQB+IAcSAOxIE4EAfiQByIA3EgDhAH4kAciANxIA7EgTgQB+IAcSAOxIE4EAfiQByIA3EgDsQB4kAciANxIA7EgTgQB3dXPapP225FxLv9rjjupogYhtUwDOM4vlsc6/XQdUe018aji8fH8Dkzs9b69OmTWmtmxjscoZST7uTy8vLF1dXNQcRxp/p4/fr1+/xRs5Tzs7MjKePoLivr9fo9j9BaO5IyjvGG1HcQX2URB+JAHIgDcSAOxIE4QByIA3EgDsSBOBAH4kAciAPEgTgQB+JAHIgDcSAOxIE4EAf829/FDyCZOcIERQAAAABJRU5ErkJggg==")
ICON_PATHS = {"/icon.png", "/favicon.ico", "/apple-touch-icon.png",
              "/apple-touch-icon-precomposed.png"}

_MOD = None


def _load():
    """
    載入中文檔名的主腳本。模組名不能直接 import，走 importlib 指定路徑。
    載入結果快取在行程內：Vercel 的 lambda 會重用，第二次之後省掉解析成本
    （報價本身不會被快取，build_page() 每次都重抓）。
    """
    global _MOD
    if _MOD is None:
        spec = importlib.util.spec_from_file_location("txo_tbar", SCRIPT)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules["txo_tbar"] = mod
        spec.loader.exec_module(mod)
        _MOD = mod
    return _MOD


def _error_page(exc):
    """
    抓不到報價時的頁面。這裡刻意不只印錯誤，而是把 Cloudflare 備援網址做成
    一顆大按鈕——盤中出事時要的是「馬上有東西可以看」，不是除錯訊息。
    """
    detail = html.escape("".join(traceback.format_exception_only(type(exc), exc)).strip())
    return f'''<meta charset="utf-8">
<title>報價暫時取不到</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<style>
body{{margin:0;background:#17181a;color:#ececec;font-family:-apple-system,"PingFang TC",sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;}}
.box{{max-width:420px;text-align:center}}
h1{{font-size:19px;margin:0 0 10px}}
p{{color:#9a9790;font-size:13.5px;line-height:1.7;margin:0 0 18px}}
a.btn{{display:block;background:#2f6fdb;color:#fff;text-decoration:none;padding:14px;
  border-radius:10px;font-size:15px;font-weight:600;margin-bottom:10px}}
a.re{{color:#9a9790;font-size:13px}}
code{{display:block;background:#1f2124;color:#ff6b5c;padding:10px;border-radius:8px;
  font-size:11.5px;text-align:left;margin-top:18px;word-break:break-all}}
</style>
<div class="box">
  <h1>即時報價暫時取不到</h1>
  <p>可能是期交所 MIS 沒回應，或這台伺服器連不出去。<br>
     排程產出的版本不受影響，點下面就能看。</p>
  <a class="btn" href="{FALLBACK}">改看排程版（Cloudflare）</a>
  <a class="re" href="javascript:location.reload()">↻ 重試一次即時版</a>
  <code>{detail}</code>
</div>'''


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        # iOS 也可能探測帶尺寸的檔名（/apple-touch-icon-180x180.png），一併吃掉
        if path in ICON_PATHS or (path.startswith("/apple-touch-icon") and path.endswith(".png")):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(ICON)))
            self.end_headers()
            self.wfile.write(ICON)
            return

        try:
            q = parse_qs(urlparse(self.path).query)
            radius = int(q.get("radius", ["1500"])[0])
        except Exception:
            radius = 1500

        try:
            m    = _load()
            html_out = m.render_html(m.build_page(radius=radius))
            code = 200
        except Exception as e:
            html_out = _error_page(e)
            code = 200          # 回 200，否則手機瀏覽器可能顯示自己的錯誤頁蓋掉備援連結

        body = html_out.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 一定要 no-store：這頁的全部價值就是「現在」，被 CDN 或瀏覽器快取就沒意義了
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
