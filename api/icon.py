# -*- coding: utf-8 -*-
"""
主畫面圖示（180x180 PNG）。

用 function 服務而不是放靜態檔，是因為 vercel.json 把 public/ 整個排除了——
那個目錄是 GitHub Actions 產給 Cloudflare 的，不能讓 Vercel 拿去當靜態網站，
否則首頁會變成排程留下的舊 index.html，蓋掉即時版。

圖示直接 base64 內嵌（988 bytes），省掉 includeFiles 的打包不確定性。
"""

import base64
from http.server import BaseHTTPRequestHandler

ICON = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAADo0lEQVR42u3bsW4cVRSA4XvGdxx7Z7NSUtBGltImBTVvwNNQUNCClHfIg1BT0CAooLMEBQ0FoiCWIiXBO3MPhWMJCXaLJItmvN8ntx7dHf++MzuaE48uHhf4L51TgDgQB+JAHIgDcSAOxIE4EAeIA3EgDsSBOBAH4kAciANxgDgQB+JAHIgDcSAOxIE4EAeIA3EgDsSBOBAH4mBh6uEOHQs8HbnIRS8wjm3m4s7zybS8NqKLA/0j1sNtGw/7GhG5hC3kZpFZyqs+lrV5RInpzTbHPMRZrofIYpv58LT/+pMnm76Wpewfrbw66z7/9KOX97rayjIW3TLO68/Pvr366fe66rPlknaO4bRfUBxnp11//7SeLSmO7ryPGgda7mHvOW4s47KSZZuZY+aYuZw4csxF3pDG7d3GIi7ib1cb/1j3YhbtOQfFQzDEgTgQB+JAHCAOxIE4EAfiQByIA3EgDsSBOEAclLm8QzpHEXvfx4woNz9l96vIKY7yIV6NLm1mb593re0ZTShTTjmNmXsS6EpElrlMarX84LMq/9PcylC7rs5nqClLRNbTndOmreRZN9RVO+n2DIJc16l1GTPZQFp2qz66Q4Uajy4eH2IMpI/4+MH6JLoyg9MYEdM4rTebL58/HzabaZpix6Vjzzhka7k6P//sqy+++/GHYTVktlnM23Tx6tcX25fXcfLhR5vqgbaNbeY3f1zlXO40YhzHB3+16/PVMNzPcbvrvmLYPXPaog398OaXF39+/9v2/qa1No8Pl929Gt1Bht4OeFnZ9LXMJ44om75Gm8o0lmnaFccU+6fLprhX63Bah342cZTMXN445DSbKdkoZcqcMku5/TqyI47Yd5CMEiUz29sfzzkoHoKBOBAH4kAciANxIA7EgTgQB4gDcSAOxIE4EAfiQByIA3GAOBAH4kAciANxIA7EgTgQB+IAcSAOxIE4EAfiQByIA3EgDhAH4kAciANxIA7EgTgQB+IAcSAOxIE4EAfiQByIA3EgDsQB4kAciANxIA7EgTgQB3dXPapP225FxLv9rjjupogYhtUwDOM4vlsc6/XQdUe018aji8fH8Dkzs9b69OmTWmtmxjscoZST7uTy8vLF1dXNQcRxp/p4/fr1+/xRs5Tzs7MjKePoLivr9fo9j9BaO5IyjvGG1HcQX2URB+JAHIgDcSAOxIE4QByIA3EgDsSBOBAH4kAciAPEgTgQB+JAHIgDcSAOxIE4EAf829/FDyCZOcIERQAAAABJRU5ErkJggg==")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        # 圖示不會變，讓它長期快取，省掉每次開 App 的往返
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(ICON)))
        self.end_headers()
        self.wfile.write(ICON)
