# -*- coding: utf-8 -*-
"""
產生_台指攻防地圖.py

每個交易日下午跑一次，把三層公開資料壓成一頁「明天日盤的攻防地圖」：

    1. 今日收盤 OI（期交所盤後檔，約 15:00 出）＋前一交易日 → 牆在哪、今天誰加倉、Max Pain
    2. 夜盤即時報價（MIS，15:00 開盤）→ 今晚往哪一邊試、實際打到哪
    3. 今日日盤波段（近月台指期自己的日 K）→ 黃金切割

── 這支腳本的定位 ──────────────────────────────────────────────────────────
只做「資料抓取＋事實呈現＋條件式劇本」。**不做方向預測、不宣稱勝率。**

本專案已經用回測否證過三種牆（ΔOI／OI 存量／成交量）的撐壓預測力，
所以這裡的牆一律當「流動性集中處／目標區」，不是「會擋住」。
每個劇本的觸發價都用**今天真的成交過的價**（日盤高低、夜盤高低、跳空缺口），
牆只決定目標放哪。黃金切割一律歸在「未檢定」——它唯一的價值是與 OI 完全獨立，
重合處代表不同的人用不同方法看到同一價位，是很弱的獨立確認，不是證據。

── 口徑：這套的成敗在這裡 ──────────────────────────────────────────────────
黃金切割算在**期指**上（你下單的東西），OI 牆長在**履約價**上（結算對加權指數），
兩者差一個價差。比對前一定要把牆 +價差換到期指口徑，否則整張圖是錯位的。
頁面上有一根滑桿讓你當天填實際價差，整張階梯跟著平移。

用法：
    python3 產生_台指攻防地圖.py                    # 抓資料 → 產網頁
    python3 產生_台指攻防地圖.py --json 地圖.json   # 同時輸出算好的數字（給敘事層用）
    python3 產生_台指攻防地圖.py --date 20260902    # 指定資料日（補跑用）
    python3 產生_台指攻防地圖.py --out 攻防.html    # 指定輸出檔名
    python3 產生_台指攻防地圖.py --open             # 產完自動開瀏覽器

非交易日（抓不到當日收盤檔）直接結束、不產出，回傳碼 0——排程可以無腦每天跑。
"""

import io
import os
import re
import csv
import sys
import json
import argparse
import webbrowser
from collections import defaultdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TW_TZ    = ZoneInfo("Asia/Taipei")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

OPT_DL   = "https://www.taifex.com.tw/cht/3/dlOptDataDown"
OPT_PAGE = "https://www.taifex.com.tw/cht/3/dlOptDailyMarketView"
FUT_DL   = "https://www.taifex.com.tw/cht/3/dlFutDataDown"
FUT_PAGE = "https://www.taifex.com.tw/cht/3/dlFutDailyMarketView"
INST_FUT_DL   = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
INST_FUT_PAGE = "https://www.taifex.com.tw/cht/3/futContractsDate"
INST_OPT_DL   = "https://www.taifex.com.tw/cht/3/callsAndPutsDateDown"
INST_OPT_PAGE = "https://www.taifex.com.tw/cht/3/callsAndPutsDate"
MIS_QUOTE     = "https://mis.taifex.com.tw/futures/api/getQuoteList"
MIS_OPT_DAY   = "https://mis.taifex.com.tw/futures/RegularSession/EquityIndices/OptionsDomestic/"
MIS_OPT_NIGHT = "https://mis.taifex.com.tw/futures/AfterHoursSession/EquityIndices/OptionsDomestic/"
MIS_FUT_DAY   = "https://mis.taifex.com.tw/futures/RegularSession/EquityIndices/FuturesDomestic/"
MIS_FUT_NIGHT = "https://mis.taifex.com.tw/futures/AfterHoursSession/EquityIndices/FuturesDomestic/"
TWSE_INDEX    = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"

OUT_HTML = os.path.join(BASE_DIR, "台指攻防地圖.html")

# 推播點下去要開的那一頁。固定同一個網址、每天更新同一頁，
# 手機上的書籤才不會失效。環境變數 PAGE_URL 或 --url 可以蓋掉。
PAGE_URL = "https://claude.ai/code/artifact/ee15a424-8e56-4158-a93e-eb42f2c19c3a"

# ── 參數 ─────────────────────────────────────────────────────────────────────
# 哪些到期別要進階梯。不能只取「最近的 N 個」——實測 09/02 那天最近的三個是
# 0904/0909/0911，會把 OI 最厚的 0916 月選整個漏掉（0911 只有 603 口）。
# 規則：最近的那一個一定要，加上所有 OI 夠厚的，且不超過「最厚那個」的到期日。
EXPIRY_MIN_OI  = 3000
# 階梯上要畫出來的檔位：合併 OI 低於這個就不佔一行（純雜訊）
LADDER_MIN_OI  = 250
# 階梯只畫現價 ±這個比例的範圍。不能用固定點數——指數 45,000 時 ±1,500 只有 ±3.3%，
# 會把真正的牆切在視窗外；也不能不設，深價外的尾部避險（40,000 賣權那種）
# 常常是全場最大的一根，畫進來會把近價的結構壓成一片扁平。
LADDER_RADIUS_PCT = 0.05
LADDER_STEP    = 100
# 共振：|黃金切割位 − 牆(換算後)| 在這個範圍內算重合。
# 履約價每 100 點一檔，任何位置都能在 ±50 內找到一檔，所以共振本身不值錢，
# 一定要同時看牆的厚度才算數（見 wall_grade）。
RESONANCE_PT   = 60
WALL_THICK     = 800     # 厚
WALL_MEDIUM    = 450     # 中等
# 波段偵測：只用「當前近月合約自己的日 K」，避免換月跳價。
# 合約成為近月之前成交量很小，用這個門檻把那段切掉。
SWING_MIN_VOL  = 10000


def num(v):
    v = str(v or "").replace(",", "").replace("%", "").strip()
    if v in ("", "-", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def nint(v):
    n = num(v)
    return int(n) if n is not None else 0


def taifex_session(ref):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": ref})
    try:
        s.get(ref, timeout=20)
    except Exception:
        pass
    return s


# ── 1. 收盤 OI（逐履約價，依契約到期日分桶）────────────────────────────────────

def fetch_oi(sess, d):
    """
    某一交易日的選擇權收盤檔，照契約到期日分桶。
    回傳 {expiry: {'C':{k:oi}, 'P':{k:oi}, 'Cv':{k:vol}, 'Pv':{k:vol}}}；當日沒有檔案回 None。

    注意：**結算日當天的檔案，該合約的 OI 還是收盤前的數字**（結算在盤後才處理），
    所以要靠到期日 > 今天來排除已結算的合約，不能靠 OI 歸零來判斷。
    """
    r = sess.post(OPT_DL, data={
        "down_type": "1", "commodity_id": "TXO", "commodity_id2": "all",
        "queryStartDate": d.strftime("%Y/%m/%d"),
        "queryEndDate":   d.strftime("%Y/%m/%d"),
        "commodity_id2t": "",
    }, timeout=60)
    text = r.content.decode("ms950", errors="ignore")
    b = defaultdict(lambda: {"C": defaultdict(int), "P": defaultdict(int),
                             "Cv": defaultdict(int), "Pv": defaultdict(int)})
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("交易時段", "").strip() != "一般":
            continue
        exp = (row.get("契約到期日") or "").strip()
        if not exp:
            continue
        try:
            cp = "C" if row["買賣權"].strip() == "買權" else "P"
            k  = int(float(row["履約價"]))
        except Exception:
            continue
        b[exp][cp][k]       += nint(row.get("未沖銷契約數"))
        b[exp][cp + "v"][k] += nint(row.get("成交量"))
    return dict(b) or None


def prev_trading_day(sess, d, back=8):
    """從 d 的前一天往回找，找到第一個抓得到收盤檔的日子。"""
    p = d - timedelta(days=1)
    for _ in range(back):
        while p.weekday() >= 5:
            p -= timedelta(days=1)
        got = fetch_oi(sess, p)
        if got:
            return p, got
        p -= timedelta(days=1)
    return None, None


# ── 2. 近月台指期日 K（波段用）────────────────────────────────────────────────

def fetch_txf_daily(sess, end, days=60):
    """
    近月台指期日 K。**只回當前近月那一個合約自己的序列**，不做連續月接續——
    換月當天會跳好幾百點（實測 08/18→08/19 從 45,085 掉到 44,330 是換月不是下跌），
    接起來算黃金切割會算出一段根本不存在的波段。
    回傳 [{'d':date, 'm':合約月份, 'o','h','l','c','vol'}]，依日期排序。
    """
    # 期交所這個下載端點的查詢區間**上限大約 31 天**，超過就回一頁 HTML 錯誤頁
    # （不是空 CSV，是 DOCTYPE 開頭的網頁，直接 DictReader 會安靜地拿到 16 列垃圾）。
    # 所以分段抓再接起來。
    rows = []
    seg_end = end
    while seg_end > end - timedelta(days=days):
        seg_start = max(end - timedelta(days=days), seg_end - timedelta(days=30))
        r = sess.post(FUT_DL, data={
            "down_type": "1", "commodity_id": "TX", "commodity_id2": "all",
            "queryStartDate": seg_start.strftime("%Y/%m/%d"),
            "queryEndDate":   seg_end.strftime("%Y/%m/%d"),
            "commodity_id2t": "",
        }, timeout=120)
        chunk = list(csv.DictReader(io.StringIO(r.content.decode("ms950", errors="ignore"))))
        if chunk and "交易日期" in chunk[0]:
            rows += chunk
        seg_end = seg_start - timedelta(days=1)
    byday = defaultdict(list)
    for x in rows:
        if x.get("交易時段", "").strip() != "一般" or x.get("契約", "").strip() != "TX":
            continue
        byday[x["交易日期"].strip()].append(x)
    if not byday:
        return []
    # 每天取成交量最大的那個合約 = 當天的近月
    daily = []
    for ds in sorted(byday):
        c = [x for x in byday[ds] if nint(x.get("成交量")) > 0]
        if not c:
            continue
        x = max(c, key=lambda y: nint(y.get("成交量")))
        daily.append({"d": ds, "m": x["到期月份(週別)"].strip(),
                      "o": num(x["開盤價"]), "h": num(x["最高價"]),
                      "l": num(x["最低價"]), "c": num(x["收盤價"]),
                      "vol": nint(x["成交量"])})
    if not daily:
        return []
    cur = daily[-1]["m"]
    return [x for x in daily if x["m"] == cur and x["vol"] >= SWING_MIN_VOL]


def find_swing(bars):
    """
    在同一個合約的日 K 上找最近一段波段。
    最高的高點與最低的低點，誰在後面誰就是這段的終點：
      低在前、高在後 → 上漲段，之後的整理是「回撤」，切割從高點往下算
      高在前、低在後 → 下跌段，之後的整理是「反彈」，切割從低點往上算
    回傳 (lo, hi, lo_date, hi_date, 'up'|'down')；資料不足回 None。
    """
    if len(bars) < 4:
        return None
    hi_i = max(range(len(bars)), key=lambda i: bars[i]["h"])
    lo_i = min(range(len(bars)), key=lambda i: bars[i]["l"])
    if hi_i == lo_i:
        return None
    direction = "up" if lo_i < hi_i else "down"
    return (bars[lo_i]["l"], bars[hi_i]["h"],
            bars[lo_i]["d"], bars[hi_i]["d"], direction)


def fib_levels(lo, hi, direction):
    """回傳 [(比例, 價位)]，由高到低排序。"""
    rng = hi - lo
    out = []
    for r in (0.236, 0.382, 0.5, 0.618, 0.786):
        out.append((r, hi - rng * r if direction == "up" else lo + rng * r))
    return sorted(out, key=lambda t: -t[1])


# ── 3. MIS 即時報價 ──────────────────────────────────────────────────────────

SID_PAT  = re.compile(r'^([A-Z]{2,3})(\d{4,5})([A-X])(\d)$')
CALL_MON = "ABCDEFGHIJKL"
PUT_MON  = "MNOPQRSTUVWX"


def mis(mkt, symbol_type, cid=""):
    """MarketType 才是決定日/夜盤的參數（'0' 一般、'1' 盤後）；Referer 只是擺樣子。"""
    if symbol_type == "O":
        ref = MIS_OPT_NIGHT if mkt == "1" else MIS_OPT_DAY
    else:
        ref = MIS_FUT_NIGHT if mkt == "1" else MIS_FUT_DAY
    try:
        r = requests.post(MIS_QUOTE, json={
            "MarketType": mkt, "SymbolType": symbol_type, "KindID": "1", "CID": cid,
            "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A",
        }, headers={"Content-Type": "application/json;charset=UTF-8",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": ref, "User-Agent": UA}, timeout=20)
        return r.json().get("RtData", {}).get("QuoteList", [])
    except Exception:
        return []


def txf_bar(mkt):
    """該時段成交量最大的 TXF 合約 OHLC。期貨 SymbolID 尾綴跟著時段變：日盤 -F、夜盤 -M。"""
    sfx = "-M" if mkt == "1" else "-F"
    items = [i for i in mis(mkt, "F")
             if i.get("SymbolID", "").startswith("TXF") and i.get("SymbolID", "").endswith(sfx)]
    if not items:
        return None
    it = max(items, key=lambda i: nint(i.get("CTotalVolume")))
    return {"sid": it.get("SymbolID", ""), "name": it.get("DispCName", ""),
            "o": num(it.get("COpenPrice")), "h": num(it.get("CHighPrice")),
            "l": num(it.get("CLowPrice")),  "c": num(it.get("CLastPrice")),
            "ref": num(it.get("CRefPrice")), "diff": num(it.get("CDiff")),
            "vol": nint(it.get("CTotalVolume")),
            "date": it.get("CDate", ""), "time": it.get("CTime", "")}


def opt_volume(mkt):
    """夜盤／日盤選擇權買賣權成交量（依合約 root 分）。用來看今晚在哪一邊下注。"""
    out = defaultdict(lambda: {"C": 0, "P": 0})
    for it in mis(mkt, "O", "TXO"):
        m = SID_PAT.match((it.get("SymbolID") or "").split("-")[0])
        if not m:
            continue
        root, _, ltr, _ = m.groups()
        v = nint(it.get("CTotalVolume"))
        out[root]["C" if ltr in CALL_MON else "P"] += v
    return {k: v for k, v in out.items() if v["C"] + v["P"] > 0}


# ── 4. 加權指數收盤與三大法人 ────────────────────────────────────────────────

def fetch_spot(d):
    """TWSE 每日成交統計，取加權指數收盤與漲跌。抓不到回 None（不阻斷）。"""
    try:
        r = requests.get(TWSE_INDEX, params={"date": d.strftime("%Y%m01"), "response": "json"},
                         headers={"User-Agent": UA}, timeout=20)
        for row in r.json().get("data", []):
            # 民國日期 115/09/02
            parts = row[0].split("/")
            if len(parts) == 3 and int(parts[0]) + 1911 == d.year \
               and int(parts[1]) == d.month and int(parts[2]) == d.day:
                return {"close": num(row[4]), "diff": num(row[5]), "amount": num(row[2])}
    except Exception:
        pass
    return None


def fetch_inst(sess_f, sess_o, d):
    """三大法人：台股期貨未平倉淨額（今日與前一日）、臺指選擇權未平倉淨額。"""
    out = {"fut": {}, "fut_prev": {}, "opt": {}}
    start = d - timedelta(days=10)
    try:
        r = sess_f.post(INST_FUT_DL, data={
            "firstDate": "", "lastDate": "", "commodityId": "",
            "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate":   d.strftime("%Y/%m/%d")}, timeout=60)
        rows = [x for x in csv.DictReader(io.StringIO(r.content.decode("ms950", errors="ignore")))
                if x.get("商品名稱", "").strip() == "臺股期貨"]
        days = sorted({x["日期"].strip() for x in rows})
        for x in rows:
            v = list(x.values())
            rec = {"trade_net": nint(v[7]), "oi_net": nint(v[13])}
            if x["日期"].strip() == days[-1]:
                out["fut"][x["身份別"].strip()] = rec
            elif len(days) > 1 and x["日期"].strip() == days[-2]:
                out["fut_prev"][x["身份別"].strip()] = rec
    except Exception:
        pass
    try:
        r = sess_o.post(INST_OPT_DL, data={
            "firstDate": "", "lastDate": "", "commodityId": "",
            "queryStartDate": d.strftime("%Y/%m/%d"),
            "queryEndDate":   d.strftime("%Y/%m/%d")}, timeout=60)
        for x in csv.DictReader(io.StringIO(r.content.decode("ms950", errors="ignore"))):
            if x.get("商品名稱", "").strip() != "臺指選擇權":
                continue
            v = list(x.values())
            out["opt"][(x["買賣權別"].strip(), x["身份別"].strip())] = {
                "trade_net": nint(v[8]), "oi_net": nint(v[14])}
    except Exception:
        pass
    return out


# ── 5. 結構計算 ──────────────────────────────────────────────────────────────

def pick_expiries(oi, today_str):
    """
    選出明天還活著、值得畫的到期別。
    不能只取最近的 N 個——實測 09/02 最近三個是 0904/0909/0911，會漏掉最厚的 0916 月選。
    """
    live = {e: b for e, b in oi.items() if e > today_str}
    if not live:
        return []
    tot = {e: sum(b["C"].values()) + sum(b["P"].values()) for e, b in live.items()}
    fat = max(tot, key=lambda e: tot[e])          # OI 最厚的那個（通常是月選）
    nearest = min(live)                            # 最近的那個（結算壓力最大）
    keep = {nearest, fat}
    keep |= {e for e in live if e <= fat and tot[e] >= EXPIRY_MIN_OI}
    return sorted(keep)


def max_pain(calls, puts):
    """所有買賣權在該結算價下的內含價值總和最小的履約價。未檢定，只當參考。"""
    ks = sorted(set(calls) | set(puts))
    if not ks:
        return None
    return min(ks, key=lambda s: sum(max(0, s - k) * v for k, v in calls.items())
                               + sum(max(0, k - s) * v for k, v in puts.items()))


def wall_grade(oi):
    if oi >= WALL_THICK:
        return "厚"
    if oi >= WALL_MEDIUM:
        return "中等"
    return "薄"


def build_ladder(oi, exps, basis, price):
    """
    合併階梯：每個 100 點檔位一列，買權／賣權各自加總三個到期別，附逐到期別明細。
    只留有厚度的檔位，太薄的整列拿掉（不然一堆個位數佔版面）。
    """
    ks = set()
    for e in exps:
        ks |= set(oi[e]["C"]) | set(oi[e]["P"])
    lo_k = price - basis - price * LADDER_RADIUS_PCT
    hi_k = price - basis + price * LADDER_RADIUS_PCT
    ks = sorted(k for k in ks if k % LADDER_STEP == 0 and lo_k <= k <= hi_k)
    rows = []
    for k in ks:
        cd = [oi[e]["C"].get(k, 0) for e in exps]
        pd = [oi[e]["P"].get(k, 0) for e in exps]
        c, p = sum(cd), sum(pd)
        if c < LADDER_MIN_OI and p < LADDER_MIN_OI:
            continue
        rows.append({"k": k, "fut": k + basis, "c": c, "p": p, "cd": cd, "pd": pd})
    return rows


def find_resonance(rows, fibs, basis):
    """
    黃金切割位與牆（換算到期指口徑後）重合的地方。
    共振本身不值錢——履約價每 100 點一檔，任何位置都能在 ±50 內找到一檔。
    所以一定要把牆的厚度一起標出來，讓人自己判斷是不是巧合等級。
    """
    out = []
    for ratio, level in fibs:
        best = None
        for r in rows:
            side = "C" if r["c"] >= r["p"] else "P"
            oi_v = max(r["c"], r["p"])
            gap  = abs(level - r["fut"])
            if gap <= RESONANCE_PT and (best is None or oi_v > best["oi"]):
                best = {"k": r["k"], "fut": r["fut"], "side": side, "oi": oi_v, "gap": round(gap)}
        if best:
            best.update(ratio=ratio, level=round(level), grade=wall_grade(best["oi"]))
            out.append(best)
    return out


def nearest_walls(rows, price, side, direction, n=3, max_pct=0.03):
    """
    從 price 往指定方向找最近的 n 道有厚度的牆（期指口徑）。
    這是**目標**用的，不是「會擋住」——三種牆的撐壓預測力已被本專案回測否證。
    距離上限 3%：再遠的牆當日到不了，寫進目標只是讓人以為那是一天的空間
    （實測 09/02 不設限會把 1,660 點外的 44,000 賣權列成第三目標）。
    """
    cand = [r for r in rows
            if (r["fut"] < price if direction == "down" else r["fut"] > price)
            and abs(r["fut"] - price) <= price * max_pct
            and r["p" if side == "P" else "c"] >= WALL_MEDIUM]
    cand.sort(key=lambda r: abs(r["fut"] - price))
    out = sorted(cand[:n * 3], key=lambda r: -r["p" if side == "P" else "c"])[:n]
    return sorted(out, key=lambda r: -r["fut"] if direction == "down" else r["fut"])


# ── 6. 版面 ──────────────────────────────────────────────────────────────────

NARRATIVE_START = "<!-- NARRATIVE:START -->"
NARRATIVE_END   = "<!-- NARRATIVE:END -->"

CSS = """
:root{
  --ground:#F1F2EF; --surface:#FBFBF9; --surface-2:#EDEEEA; --line:#D9DBD4; --line-soft:#E4E6E0;
  --ink:#161A1B; --ink-2:#40484A; --ink-3:#6E7674;
  --call:#C0392F; --put:#0E8A5F; --mark:#2E3A3C; --amber:#9A6B10; --amber-soft:#F0E4CB;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#101312; --surface:#181C1B; --surface-2:#202523; --line:#2C3331; --line-soft:#242A28;
    --ink:#E8EAE6; --ink-2:#AEB6B3; --ink-3:#7E8785;
    --call:#E4685E; --put:#2FA87E; --mark:#C3CCC9; --amber:#D2A03F; --amber-soft:#33290F;
  }
}
:root[data-theme="dark"]{
  --ground:#101312; --surface:#181C1B; --surface-2:#202523; --line:#2C3331; --line-soft:#242A28;
  --ink:#E8EAE6; --ink-2:#AEB6B3; --ink-3:#7E8785;
  --call:#E4685E; --put:#2FA87E; --mark:#C3CCC9; --amber:#D2A03F; --amber-soft:#33290F;
}
*{box-sizing:border-box}
body{background:var(--ground); color:var(--ink); margin:0; font-size:15px; line-height:1.72;
     font-family:"Noto Sans TC","PingFang TC","Hiragino Sans TC",system-ui,sans-serif;
     -webkit-font-smoothing:antialiased}
.wrap{max-width:860px; margin:0 auto; padding:34px 20px 76px}
.mono{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace; font-variant-numeric:tabular-nums}
h1,h2,h3{font-family:"Noto Serif TC",Georgia,"Songti TC",serif; text-wrap:balance; margin:0}
header{border-bottom:1px solid var(--line); padding-bottom:22px}
.eyebrow{font-size:11.5px; letter-spacing:.22em; color:var(--ink-3); margin-bottom:10px}
h1{font-size:31px; font-weight:700; line-height:1.28; letter-spacing:-.01em}
.dek{color:var(--ink-2); font-size:14.5px; margin-top:10px; max-width:60ch}
.stamp{margin-top:14px; font-size:12px; color:var(--ink-3); display:flex; flex-wrap:wrap; gap:6px 16px}
.kpis{display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line);
      border:1px solid var(--line); margin:26px 0 8px}
@media (max-width:560px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface); padding:13px 14px 15px}
.kpi .lb{font-size:11px; color:var(--ink-3); letter-spacing:.05em}
.kpi .vl{font-size:22px; font-weight:600; margin-top:3px; letter-spacing:-.01em}
.kpi .sub{font-size:11.5px; color:var(--ink-3); margin-top:1px}
.dn{color:var(--call)} .up{color:var(--put)}
section{margin-top:44px}
.sec-h{display:flex; align-items:baseline; gap:12px; border-bottom:1px solid var(--line);
       padding-bottom:9px; margin-bottom:20px}
.sec-h h2{font-size:19px; font-weight:600}
.sec-h .n{font-size:11.5px; color:var(--ink-3); letter-spacing:.05em; margin-left:auto}
p{margin:0 0 14px}
.notes{display:grid; gap:14px}
.note{background:var(--surface); border:1px solid var(--line); padding:16px 18px 15px}
.note h3{font-size:15.5px; font-weight:600; margin-bottom:6px}
.note p{font-size:14px; color:var(--ink-2); margin:0}
.note .ev{margin-top:10px; padding-top:9px; border-top:1px dashed var(--line);
          font-size:12px; color:var(--ink-3)}
.ladder-box{background:var(--surface); border:1px solid var(--line); padding:18px 14px 14px; overflow-x:auto}
.lg{display:flex; gap:20px; flex-wrap:wrap; font-size:11.5px; color:var(--ink-3); margin-bottom:14px; padding:0 6px}
.lg i{display:inline-block; width:22px; height:8px; vertical-align:-1px; margin-right:6px}
.ladder{min-width:520px; display:grid; grid-template-columns:1fr 118px 1fr; row-gap:2px; align-items:center}
.bar-l,.bar-r{display:flex; align-items:center; gap:7px; height:15px}
.bar-l{justify-content:flex-end}
.bar-r{justify-content:flex-start}
.b{height:11px; background:var(--call)}
.bar-r .b{background:var(--put)}
.bn{font-size:11px; color:var(--ink-3)}
.k{text-align:center; font-size:12.5px; font-weight:500; color:var(--ink-2)}
.k b{color:var(--ink); font-weight:600}
.k .fx{display:block; font-size:10.5px; color:var(--ink-3); font-weight:400; margin-top:-3px}
.row{display:contents}
.mk{grid-column:1/-1; display:flex; align-items:center; gap:9px; margin:4px 0; font-size:11.5px; color:var(--mark)}
.mk::before,.mk::after{content:""; flex:1; height:1px; background:var(--line)}
.mk span{white-space:nowrap}
.mk.now{color:var(--amber); font-weight:500}
.mk.now::before,.mk.now::after{background:var(--amber); opacity:.45}
.zone{grid-column:1/-1; background:var(--amber-soft); border-left:2px solid var(--amber);
      padding:9px 12px; margin:6px 0; font-size:12.5px; color:var(--ink-2)}
.zone b{color:var(--ink); font-weight:600}
.basis{display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-top:16px; padding:12px 14px;
       background:var(--surface-2); border:1px solid var(--line-soft); font-size:12.5px; color:var(--ink-2)}
.basis input[type=range]{flex:1; min-width:150px; accent-color:var(--mark)}
.basis output{font-weight:600; color:var(--ink); min-width:52px}
.plays{display:grid; gap:16px}
.play{background:var(--surface); border:1px solid var(--line); border-top:3px solid var(--mark); padding:17px 18px 16px}
.play.bear{border-top-color:var(--call)}
.play.bull{border-top-color:var(--put)}
.play-h{display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; margin-bottom:4px}
.play-h h3{font-size:16.5px; font-weight:600}
.tag{font-size:10.5px; letter-spacing:.1em; padding:2px 7px; border:1px solid var(--line); color:var(--ink-3)}
.play > p{font-size:14px; color:var(--ink-2); margin:6px 0 13px}
.rows{display:grid; grid-template-columns:auto 1fr; gap:7px 14px; font-size:13.5px; align-items:baseline}
.rows dt{font-size:11px; letter-spacing:.09em; color:var(--ink-3); padding-top:2px; white-space:nowrap}
.rows dd{margin:0; color:var(--ink-2)}
.rows dd .mono{color:var(--ink); font-weight:500}
.evid{display:grid; grid-template-columns:repeat(3,1fr); gap:14px}
@media (max-width:700px){.evid{grid-template-columns:1fr}}
.ev-c{background:var(--surface); border:1px solid var(--line); padding:15px 16px}
.ev-c h3{font-size:13px; font-weight:600; letter-spacing:.04em; margin-bottom:9px;
         padding-bottom:7px; border-bottom:1px solid var(--line-soft)}
.ev-c.untested h3{color:var(--amber)}
.ev-c.dead h3{color:var(--ink-3)}
.ev-c ul{margin:0; padding-left:16px; font-size:12.5px; color:var(--ink-2); line-height:1.62}
.ev-c li{margin-bottom:6px}
.ev-c.dead ul{color:var(--ink-3)}
.warn{margin-top:26px; background:var(--amber-soft); border:1px solid var(--amber); padding:16px 18px}
.warn h3{font-size:14.5px; font-weight:600; margin-bottom:8px; color:var(--ink)}
.warn ul{margin:0; padding-left:17px; font-size:13px; color:var(--ink-2)}
.warn li{margin-bottom:5px}
footer{margin-top:40px; padding-top:16px; border-top:1px solid var(--line); font-size:11.5px; color:var(--ink-3)}
@media (prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important}}
"""


def fmt(n):
    return f"{int(round(n)):,}" if n is not None else "—"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render(D):
    """把算好的 D 組成一頁 HTML。所有數字都從 D 來，這裡不做任何計算。"""
    rows, marks, zones = D["ladder"], D["marks"], D.get("zones", [])
    mx = max([r["c"] for r in rows] + [r["p"] for r in rows] + [1])
    exps_lbl = " ＋ ".join(f"{e[4:6]}/{e[6:8]}" for e in D["expiries"])

    # 階梯：把標記插進正確的檔位之間（標記價位介於上下兩列之間就插在中間）
    body = []
    for i, r in enumerate(rows[::-1]):          # 由高到低
        upper = rows[::-1][i - 1]["fut"] if i > 0 else 10 ** 9
        for z in zones:
            if r["fut"] < z["at"] <= upper:
                body.append(f'<div class="zone"><b>{z["title"]}</b><br>'
                            f'<span class="mono">{z["text"]}</span></div>')
        for m in marks:
            if r["fut"] < m["at"] <= upper:
                cls = "mk now" if m.get("now") else "mk"
                body.append(f'<div class="{cls}"><span>{m["txt"]}</span></div>')
        tip = (f'履約 {fmt(r["k"])}　買權 {fmt(r["c"])} 口（'
               + "／".join(f'{e[4:6]}/{e[6:8]} {fmt(v)}' for e, v in zip(D["expiries"], r["cd"]))
               + f'）　賣權 {fmt(r["p"])} 口（'
               + "／".join(f'{e[4:6]}/{e[6:8]} {fmt(v)}' for e, v in zip(D["expiries"], r["pd"]))
               + '）')
        body.append(
            f'<div class="row" title="{esc(tip)}">'
            f'<div class="bar-l"><span class="bn mono">{fmt(r["c"])}</span>'
            f'<span class="b" style="width:{r["c"]/mx*100:.2f}%"></span></div>'
            f'<div class="k mono"><b>{fmt(r["k"])}</b>'
            f'<span class="fx" data-k="{r["k"]}">期指 {fmt(r["fut"])}</span></div>'
            f'<div class="bar-r"><span class="b" style="width:{r["p"]/mx*100:.2f}%"></span>'
            f'<span class="bn mono">{fmt(r["p"])}</span></div></div>')

    kpis = "".join(
        f'<div class="kpi"><div class="lb">{k["lb"]}</div>'
        f'<div class="vl mono {k.get("cls","")}">{k["vl"]}</div>'
        f'<div class="sub mono {k.get("subcls","")}">{k["sub"]}</div></div>'
        for k in D["kpis"])

    notes = "".join(
        f'<div class="note"><h3>{esc(n["h"])}</h3><p>{n["p"]}</p>'
        + (f'<div class="ev mono">{esc(n["ev"])}</div>' if n.get("ev") else "")
        + '</div>' for n in D["notes"])

    plays = "".join(
        f'<div class="play {p.get("cls","")}"><div class="play-h"><h3>{esc(p["h"])}</h3>'
        f'<span class="tag">{esc(p["tag"])}</span></div><p>{p["lede"]}</p><dl class="rows">'
        + "".join(f'<dt>{esc(dt)}</dt><dd>{dd}</dd>' for dt, dd in p["rows"])
        + '</dl></div>' for p in D["plays"])

    warns = "".join(f"<li>{w}</li>" for w in D["warnings"])

    # charset 一定要在最前面：少了它，瀏覽器直接開本機檔會把中文全部變亂碼
    # （Artifact 的外框會自己補，但這支腳本產的是獨立檔案，得自己帶）
    return f"""<meta charset="utf-8">
<title>台指攻防地圖</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&amp;family=Noto+Sans+TC:wght@400;500;700&amp;family=Noto+Serif+TC:wght@600;700&amp;display=swap">
<style>{CSS}</style>
<div class="wrap">
<header>
  <div class="eyebrow">{D["head_date"]}</div>
  <h1>台指攻防地圖</h1>
  <p class="dek">今日收盤 OI ＋ 夜盤即時 ＋ 今日波段黃金切割，統一換到<strong>期指口徑</strong>比對。價位是硬的（OI 真的堆在那），方向是軟的。</p>
  <div class="stamp mono"><span>OI：期交所 {D["oi_date"]} 收盤檔</span><span>{D["quote_stamp"]}</span><span>產出：{D["gen_at"]}</span></div>
</header>

<div class="kpis">{kpis}</div>

<section>
  <div class="sec-h"><h2>今天發生了什麼</h2><span class="n mono">事實層</span></div>
  {NARRATIVE_START}
  <div class="notes">{notes}</div>
  {NARRATIVE_END}
</section>

<section>
  <div class="sec-h"><h2>籌碼階梯</h2><span class="n mono">履約價 + 價差 → 期指口徑</span></div>
  <div class="ladder-box">
    <div class="lg"><span><i style="background:var(--call)"></i>買權 OI</span>
      <span><i style="background:var(--put)"></i>賣權 OI</span>
      <span>{exps_lbl} 到期別加總，{D["oi_date"]} 收盤</span></div>
    <div class="ladder">{"".join(body)}</div>
    <div class="basis">
      <label for="bs">期現價差校準</label>
      <input type="range" id="bs" min="-200" max="300" step="2" value="{D["basis"]}">
      <output id="bsv" class="mono">{D["basis"]:+d}</output>
      <span style="flex-basis:100%; font-size:11.5px; color:var(--ink-3)">
        牆長在履約價上（結算對加權指數），你下單的是期指。今天收盤價差 {D["basis"]:+d} 點；
        明早開盤要重新校準，這是整張圖最大的誤差來源。</span>
    </div>
  </div>
</section>

<section>
  <div class="sec-h"><h2>{D["play_title"]}</h2><span class="n mono">觸發價一律用實際成交過的價</span></div>
  <p style="font-size:13.5px; color:var(--ink-3); margin-bottom:18px; max-width:62ch">
    牆的撐壓預測力已經被本專案自己的回測否證過，所以下面<strong>沒有一個觸發價是從 OI 來的</strong>——
    觸發用今天真的成交過的高低點，牆只用來決定目標放哪。</p>
  <div class="plays">{plays}</div>
</section>

<section>
  <div class="sec-h"><h2>這張圖裡哪些算數</h2><span class="n mono">憑據分層</span></div>
  <div class="evid">
    <div class="ev-c fact"><h3>事實</h3><ul>
      <li>逐履約價 OI 分布與今日增減（期交所收盤檔）</li>
      <li>今日日盤與夜盤的開高低收、成交量</li>
      <li>期現價差 {D["basis"]:+d} 點</li>
      <li>三大法人期貨／選擇權未平倉口數</li>
      <li>結算倒數：{D["dte_text"]}</li>
    </ul></div>
    <div class="ev-c untested"><h3>未檢定</h3><ul>
      <li>黃金切割（{fmt(D["swing"][0])} → {fmt(D["swing"][1])}）。唯一價值是它與 OI 完全獨立，
          重合處代表不同的人用不同方法看到同一價位——很弱的獨立確認，不是證據</li>
      <li>Max Pain：{D["maxpain_text"]}</li>
      <li>共振帶判定（|黃金切割位 − 牆| ≤ {RESONANCE_PT} 點）</li>
    </ul></div>
    <div class="ev-c dead"><h3>已否證</h3><ul>
      <li>三種牆（ΔOI／OI 存量／成交量）的撐壓預測力——本專案回測全數否證，
          所以牆只當「流動性集中處／目標」，不當「會擋住」</li>
      <li>綠牆（選擇權籌碼區）超額報酬</li>
      <li>Skew 的方向預測力</li>
      <li>發動候選三層合併的 alpha</li>
    </ul></div>
  </div>
  <div class="warn"><h3>下單前要知道的事</h3><ul>{warns}</ul></div>
</section>

<footer class="mono">
  資料：期交所選擇權／期貨每日收盤行情、三大法人未平倉；期交所 MIS 即時報價；TWSE 每日成交統計。
  劇本排序未經回測、不宣稱勝率，只用來決定哪個情境排第一。
</footer>
</div>
<script>
const bs=document.getElementById("bs"), bsv=document.getElementById("bsv");
const nf=n=>n.toLocaleString("en-US");
function applyBasis(){{
  const b=Number(bs.value);
  bsv.textContent=(b>=0?"+":"")+b;
  for(const s of document.querySelectorAll(".fx")) s.textContent="期指 "+nf(Number(s.dataset.k)+b);
}}
bs.addEventListener("input", applyBasis);
applyBasis();
</script>
"""


# ── 7. ntfy 推播 ─────────────────────────────────────────────────────────────

def load_ntfy_topic():
    """沿用專案既有的 ntfy 設定：環境變數優先（雲端），再退回本機 ntfy_config.json。"""
    t = os.environ.get("NTFY_TOPIC", "")
    if t:
        return t
    p = os.path.join(BASE_DIR, "ntfy_config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("topic", "")
    return ""


def notify_lines(D):
    """
    推播用的純文字摘要。手機通知就那幾行的空間，所以只留**可以直接下單的東西**：
    箱子上下緣、兩個方向的觸發／停損／目標、以及厚度夠格的共振帶。
    敘事、法人部位、OI 明細一律不進推播——那些要看網頁。
    """
    day, night = D["day"], D["night"]
    lo, hi = D["range"]
    L = [f'{D["oi_date"]}　箱子 {fmt(lo)} – {fmt(hi)}（{hi-lo:,.0f} 點）']
    if night:
        L.append(f'夜盤 {fmt(night["c"])}　{night["time"][:2]}:{night["time"][2:4]}'
                 f'　高 {fmt(night["h"])} 低 {fmt(night["l"])}')
    L.append(f'日盤收 {fmt(day["c"])}　價差 {D["basis"]:+d}')
    for p_ in D["plays"]:
        if not p_.get("cls"):          # 區間劇本不進推播，箱子上下緣上面已經有了
            continue
        arrow = "▼" if p_["cls"] == "bear" else "▲"
        row = {k: v for k, v in p_["rows"]}
        strip = lambda t: re.sub(r"<[^>]+>", "", t)
        L.append(f'{arrow} {p_["h"]}')
        L.append(f'　停損 {strip(row.get("停損", "")).replace("停損", "").strip()}')
        L.append(f'　目標 {strip(row.get("目標", ""))}')
    # 共振只推「打得到的」。全部五條切割位都列出來的話，60% 是 1,000 點外的，
    # 通知欄被灌滿卻沒有一條是當日會碰到的。
    near = [r for r in D["resonance"]
            if r["grade"] != "薄" and abs(r["level"] - D["price"]) <= D["price"] * 0.025]
    for r in sorted(near, key=lambda r: abs(r["level"] - D["price"]))[:3]:
        L.append(f'共振 {r["ratio"]*100:.1f}% {fmt(r["level"])} × {fmt(r["fut"])} '
                 f'{"C" if r["side"]=="C" else "P"}{fmt(r["oi"])}（{r["grade"]}）')
    L.append("※ 明早跳空由今晚美股決定；價差要重新校準")
    return "\n".join(L)


def push_ntfy(D, dry=False, url=None):
    body = notify_lines(D)
    url = url or os.environ.get("PAGE_URL") or PAGE_URL
    if dry:
        print("── 推播內容（--dry-run，沒有送出）──")
        print(body)
        print(f"── Click → {url}")
        return
    topic = load_ntfy_topic()
    if not topic:
        print("  ⚠ 無 ntfy topic（NTFY_TOPIC 或 ntfy_config.json），略過推播")
        return
    headers = {"Title": f'台指攻防地圖 {D["oi_date"][5:]}'.encode("utf-8"),
               "Tags": "dart"}
    if url:
        # Click 讓整則通知可以直接點開地圖那一頁；Actions 再多給一顆按鈕，
        # 因為 iOS 上從鎖定畫面滑開時 Click 不一定吃得到。
        headers["Click"] = url
        # 中文一定要自己 encode 成 utf-8 bytes：HTTP 標頭預設走 latin-1，
        # 直接塞中文字串 requests 會丟 UnicodeEncodeError，整則推播沒送出去。
        headers["Actions"] = f"view, 開地圖, {url}".encode("utf-8")
    try:
        # 一定要看狀態碼。只送不看的話，ntfy 回 4xx/5xx（topic 打錯、被限流）
        # 也會照印「推播成功」，手機收不到卻查不出來。
        r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers=headers, timeout=10)
        if r.status_code >= 400:
            print(f"  ⚠ ntfy 推播失敗：HTTP {r.status_code} {r.text[:200]}")
        else:
            print("  ✓ ntfy 推播成功")
    except Exception as e:
        print(f"  ⚠ ntfy 推播失敗：{e}")


def merge_marks(marks, gap=45):
    """價位太近的標記併成一行，不然階梯上會擠成一團看不懂。"""
    marks.sort(key=lambda m: -m["at"])
    out = []
    for m in marks:
        if out and abs(out[-1]["at"] - m["at"]) <= gap:
            out[-1]["txt"] += "　｜　" + m["txt"]
            out[-1]["now"] = out[-1].get("now") or m.get("now")
        else:
            out.append(dict(m))
    return out


def trading_days_until(d, target):
    """從 d 的隔天算到 target（含）的交易日數。不管國定假日，只扣週末，所以是上限。"""
    n, cur = 0, d + timedelta(days=1)
    while cur <= target:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="資料日 YYYYMMDD（補跑用，預設今天）")
    ap.add_argument("--out",  default=OUT_HTML)
    ap.add_argument("--json", help="同時把算好的數字寫成 JSON（給敘事層用）")
    ap.add_argument("--basis", type=int, help="手動指定期現價差，不指定就用收盤自己算")
    ap.add_argument("--open", action="store_true", help="產完自動開瀏覽器")
    ap.add_argument("--notify", action="store_true", help="推播摘要到 iPhone (ntfy)")
    ap.add_argument("--dry-run", action="store_true", help="只印推播內容，不送出")
    ap.add_argument("--url", help="推播點下去要開的網址（預設用 PAGE_URL）")
    a = ap.parse_args()

    today = (datetime.strptime(a.date, "%Y%m%d").date() if a.date
             else datetime.now(TW_TZ).date())
    tstr = today.strftime("%Y%m%d")

    s_opt = taifex_session(OPT_PAGE)
    oi_now = fetch_oi(s_opt, today)
    if not oi_now:
        print(f"｛{today}｝抓不到當日選擇權收盤檔——非交易日或盤後檔還沒出，不產出。")
        return 0
    prev_d, oi_prev = prev_trading_day(s_opt, today)

    s_fut = taifex_session(FUT_PAGE)
    bars  = fetch_txf_daily(s_fut, today)
    if not bars or bars[-1]["d"].replace("/", "") != tstr:
        print("近月期貨日 K 抓不到今天那根，不產出。")
        return 0
    day = bars[-1]
    swing = find_swing(bars)
    if not swing:
        print("波段判不出來（近月合約資料太少），不產出。")
        return 0
    lo, hi, lo_d, hi_d, direction = swing
    fibs = fib_levels(lo, hi, direction)

    spot   = fetch_spot(today)
    basis  = a.basis if a.basis is not None else (
        int(round(day["c"] - spot["close"])) if spot and spot.get("close") else 0)
    night  = txf_bar("1")
    nvol   = opt_volume("1")
    dvol   = opt_volume("0")
    inst   = fetch_inst(taifex_session(INST_FUT_PAGE), taifex_session(INST_OPT_PAGE), today)

    exps = pick_expiries(oi_now, tstr)
    if not exps:
        print("沒有還活著的到期別，不產出。")
        return 0

    mp = {e: max_pain(oi_now[e]["C"], oi_now[e]["P"]) for e in exps}
    allC, allP = defaultdict(int), defaultdict(int)
    for e in exps:
        for k, v in oi_now[e]["C"].items():
            allC[k] += v
        for k, v in oi_now[e]["P"].items():
            allP[k] += v
    mp_all = max_pain(allC, allP)

    price = (night or {}).get("c") or day["c"]
    rng_lo = (night or {}).get("l") or day["l"]
    rng_hi = (night or {}).get("h") or day["h"]

    rows = build_ladder(oi_now, exps, basis, price)
    reso = find_resonance(rows, fibs, basis)

    # ── 標記 ──
    marks = []
    marks.append({"at": day["h"], "txt": f'今日日盤高 {fmt(day["h"])}'})
    marks.append({"at": day["l"], "txt": f'今日日盤低 {fmt(day["l"])}'})
    marks.append({"at": day["c"], "txt": f'今日日盤收 {fmt(day["c"])}'})
    prev_c = bars[-2]["c"] if len(bars) > 1 else None
    gap = None
    if prev_c and abs(day["o"] - prev_c) > 80:
        gap = (min(day["o"], prev_c), max(day["o"], prev_c))
        marks.append({"at": prev_c,   "txt": f'跳空缺口{"上" if day["o"] < prev_c else "下"}緣（昨收）{fmt(prev_c)}'})
        marks.append({"at": day["o"], "txt": f'跳空缺口{"下" if day["o"] < prev_c else "上"}緣（今日開盤）{fmt(day["o"])}'})
    if night:
        marks.append({"at": night["h"], "txt": f'夜盤高 {fmt(night["h"])}'})
        marks.append({"at": night["l"], "txt": f'夜盤低 {fmt(night["l"])}'})
        marks.append({"at": night["c"], "txt": f'▸ 夜盤現價 {fmt(night["c"])}', "now": True})
    zoned = {r["ratio"] for r in reso if r["grade"] != "薄"}
    for r, lv in fibs:
        if r in zoned:          # 有共振的另外畫成高亮帶，不要重複佔一行
            continue
        marks.append({"at": lv, "txt": f'黃金切割 {r*100:.1f}% {fmt(lv)}'})
    zones = [{
        "at": (r["level"] + r["fut"]) / 2,
        "title": f'共振・{r["grade"]}牆',
        "text": f'黃金切割 {r["ratio"]*100:.1f}% <b>{fmt(r["level"])}</b> × '
                f'{fmt(r["k"])} {"買" if r["side"]=="C" else "賣"}權牆 <b>{fmt(r["fut"])}</b>'
                f'（{fmt(r["oi"])} 口）　相差 {r["gap"]} 點',
    } for r in reso if r["grade"] != "薄"]
    for e in exps:
        if mp[e] is not None:
            marks.append({"at": mp[e] + basis, "txt": f'{e[4:6]}/{e[6:8]} Max Pain {fmt(mp[e]+basis)}'})
    if mp_all is not None:
        marks.append({"at": mp_all + basis, "txt": f'合併 Max Pain {fmt(mp_all+basis)}'})
    marks = merge_marks(marks)

    # ── KPI ──
    def sign(v):
        return "dn" if (v or 0) < 0 else "up"
    kpis = []
    if spot:
        kpis.append({"lb": "加權指數 收盤", "vl": f'{spot["close"]:,.2f}',
                     "sub": f'{spot["diff"]:+,.2f}　成交 {spot["amount"]/1e8:,.0f} 億',
                     "subcls": sign(spot["diff"])})
    d_diff = day["c"] - prev_c if prev_c else None
    kpis.append({"lb": f'台指期 {day["m"][-2:]} 收盤', "vl": fmt(day["c"]),
                 "sub": (f'{d_diff:+,.0f}　量 {fmt(day["vol"])} 口' if d_diff is not None
                         else f'量 {fmt(day["vol"])} 口'),
                 "subcls": sign(d_diff)})
    if night:
        kpis.append({"lb": f'夜盤現價 {night["time"][:2]}:{night["time"][2:4]}', "vl": fmt(night["c"]),
                     "sub": f'高 {fmt(night["h"])} ／ 低 {fmt(night["l"])}'})
    kpis.append({"lb": "期現價差", "vl": f"{basis:+d}", "sub": "牆換到期指口徑用"})
    e0 = exps[0]
    kpis.append({"lb": f'{e0[4:6]}/{e0[6:8]} Max Pain', "vl": fmt(mp[e0]),
                 "sub": f'期指 {fmt(mp[e0]+basis)}（未檢定）'})
    fut_f = inst["fut"].get("外資及陸資")
    if fut_f:
        prev_f = inst["fut_prev"].get("外資及陸資", {}).get("oi_net")
        delta = (fut_f["oi_net"] - prev_f) if prev_f is not None else None
        kpis.append({"lb": "外資期貨未平倉", "vl": f'{fut_f["oi_net"]:+,}',
                     "cls": sign(fut_f["oi_net"]),
                     "sub": (f'今日淨{"空" if delta < 0 else "多"}再增 {abs(delta):,} 口'
                             if delta else "與昨日持平"),
                     "subcls": sign(delta) if delta else ""})

    # ── 敘事（模板版；敘事層會換掉整段）──
    notes = []
    gap_txt = f'，開盤跳空 {day["o"]-prev_c:+,.0f} 點' if gap else ""
    pos = (day["c"] - day["l"]) / max(1, day["h"] - day["l"])
    notes.append({
        "h": f'今日日盤：收在全日{"低檔" if pos < .3 else ("高檔" if pos > .7 else "中段")}',
        "p": f'開 {fmt(day["o"])}{gap_txt}，高 {fmt(day["h"])}、低 {fmt(day["l"])}、'
             f'收 {fmt(day["c"])}，量 {fmt(day["vol"])} 口。收盤價落在當日區間的 {pos*100:.0f}% 位置。',
        "ev": f'近月合約 {day["m"]}　期現價差 {basis:+d} 點'})
    if night:
        hits = [f'{r*100:.1f}% {fmt(lv)}' for r, lv in fibs
                if min(abs(night["l"] - lv), abs(night["h"] - lv)) <= 30]
        notes.append({
            "h": ("夜盤打到黃金切割 " + hits[0] if hits else "夜盤區間"),
            "p": f'夜盤 開 {fmt(night["o"])}、高 {fmt(night["h"])}、低 {fmt(night["l"])}、'
                 f'現 {fmt(night["c"])}，量 {fmt(night["vol"])} 口'
                 f'（日盤的 {night["vol"]/max(1,day["vol"])*100:.0f}%）。'
                 + (f'最高／最低與黃金切割 {hits[0]} 的距離在 30 點內。' if hits else ""),
            "ev": f'夜盤選擇權量 ' + "　".join(
                f'{k} C {fmt(v["C"])}/P {fmt(v["P"])}' for k, v in sorted(nvol.items(), key=lambda t: -sum(t[1].values()))[:2])})
    # 最近到期別有多少 OI 是今天現建的
    if oi_prev:
        t0 = sum(oi_now[e0]["C"].values()) + sum(oi_now[e0]["P"].values())
        p0 = (sum(oi_prev.get(e0, {"C": {}})["C"].values())
              + sum(oi_prev.get(e0, {"P": {}})["P"].values())) if e0 in oi_prev else 0
        newpct = (t0 - p0) / max(1, t0) * 100
        notes.append({
            "h": f'{e0[4:6]}/{e0[6:8]} 這個到期別，{newpct:.0f}% 的 OI 是今天現建的',
            "p": ('這種合約上的「今日 +N」沒有辨識力，<strong>只能看絕對厚度</strong>。'
                  if newpct > 50 else '存量結構為主，今日增減看得出誰在加倉。')
                 + f'真正的存量結構在 {exps[-1][4:6]}/{exps[-1][6:8]}。',
            "ev": "　｜　".join(
                f'{e[4:6]}/{e[6:8]} C {fmt(sum(oi_now[e]["C"].values()))} P {fmt(sum(oi_now[e]["P"].values()))}'
                for e in exps)})
    if fut_f:
        oc = inst["opt"].get(("CALL", "外資及陸資"), {})
        op = inst["opt"].get(("PUT", "外資及陸資"), {})
        notes.append({
            "h": "外資今天做了什麼",
            "p": f'台指期未平倉淨額 {fut_f["oi_net"]:+,} 口，今日交易淨額 {fut_f["trade_net"]:+,} 口。'
                 f'選擇權端買權未平倉淨額 {oc.get("oi_net", 0):+,} 口、賣權 {op.get("oi_net", 0):+,} 口。'
                 '這是部位事實，不是方向預測——賣買權也可能是持股的 covered call。',
            "ev": f'投信期貨淨額 {inst["fut"].get("投信", {}).get("oi_net", 0):+,} 口（結構性，日日不動）　'
                  f'自營 {inst["fut"].get("自營商", {}).get("oi_net", 0):+,} 口'})

    # ── 劇本 ──
    def wall_txt(r, side):
        v = r["p"] if side == "P" else r["c"]
        return f'<span class="mono">{fmt(r["fut"])}</span>（{fmt(r["k"])} {"賣" if side=="P" else "買"}權 {fmt(v)} 口）'
    down = nearest_walls(rows, rng_lo, "P", "down", 3)
    up   = nearest_walls(rows, rng_hi, "C", "up", 2)
    band = rng_hi - rng_lo
    play_range = {
        "h": f'區間磨　{fmt(rng_lo)} – {fmt(rng_hi)}', "tag": "結構", "cls": "",
        "lede": f'夜盤已經把上下緣都試過了。這個箱子 {band:,.0f} 點寬，'
                f'沒有新消息的話明早大概率就在裡面。',
        "rows": [("進場", f'開盤三十分鐘不破上下緣 → 靠近 <span class="mono">{fmt(rng_lo+band*0.12)}</span> 找多、'
                          f'靠近 <span class="mono">{fmt(rng_hi-band*0.12)}</span> 找空'),
                 ("停損", f'各 <span class="mono">{max(50, round(band*0.15/10)*10):,.0f}</span> 點；箱子只有 {band:,.0f} 點，停損放寬就沒得做'),
                 ("目標", "對邊，不貪，分批"),
                 ("失效", "任一邊帶量突破且 15 分鐘站穩 → 切到下面兩個劇本")]}
    play_down = {
        "h": f'續跌　跌破 {fmt(rng_lo)}', "tag": "方向", "cls": "bear",
        "lede": '牆不會擋住，只是流動性集中處；下面通常是一排中等厚度，逐級找支撐而不是一瀉千里。',
        "rows": [("進場", f'日盤跌破 <span class="mono">{fmt(rng_lo)}</span> 且 5 分 K 收破（不是插一下）'),
                 ("停損", f'站回 <span class="mono">{fmt(rng_lo+band*0.17)}</span>'),
                 ("目標", " → ".join(wall_txt(r, "P") for r in down) or "下方沒有夠厚的牆，目標自行設"),
                 ("失效", "跌破後三十分鐘內拉回箱內 → 是假跌破")]}
    play_up = {
        "h": f'反彈　站上 {fmt(rng_hi)}', "tag": "方向", "cls": "bull",
        "lede": ('上方第一道厚牆是 ' + wall_txt(up[0], "C") + '，第一次到不要追。' if up
                 else '上方沒有夠厚的牆。')
                + (f'另外 <span class="mono">{fmt(gap[0])} – {fmt(gap[1])}</span> 的跳空缺口懸在'
                   + ("頭上。" if gap[0] >= day["c"] else "腳下。") if gap else ""),
        "rows": [("進場", f'站上 <span class="mono">{fmt(rng_hi)}</span> 且不破回'),
                 ("停損", f'跌回 <span class="mono">{fmt(rng_hi-band*0.17)}</span>'),
                 ("目標", " → ".join(wall_txt(r, "C") for r in up)
                          + (f' → 缺口 <span class="mono">{fmt(gap[1])}</span>' if gap and gap[1] > rng_hi else "")),
                 ("失效", "在第一道厚牆附近出量收黑 K → 回區間劇本")]}
    plays = [play_range] + ([play_down, play_up] if pos < 0.5 else [play_up, play_down])
    plays[1]["tag"] += "・次序在前"

    # ── 警語 ──
    dte = {e: trading_days_until(today, datetime.strptime(e, "%Y%m%d").date()) for e in exps}
    warnings = [
        "<strong>這張圖不預測開盤價。</strong>明早的跳空由今晚美股決定，OI 只決定「開出來之後往哪裡走」。"
        "請在明早 08:45 用夜盤收盤價重新讀一次。",
        f'<strong>{e0[4:6]}/{e0[6:8]} 只剩 {dte[e0]} 個交易日。</strong>Gamma 大、時間價值掉得快，'
        '價外買方在區間裡不划算；要做方向請用期指或價內一檔。',
        f'<strong>期現價差今天是 {basis:+d} 點</strong>，明早要重新校準——這是整張圖最大的誤差來源，'
        '頁面上的滑桿可以當場平移整張階梯。',
    ]
    # 遠價外突然冒出來的大 OI，多半是價差單的一條腿，不是「有人在那裡佈局」
    if oi_prev:
        odd = []
        for e in exps:
            for cp, lab in (("C", "買權"), ("P", "賣權")):
                for k, v in oi_now[e][cp].items():
                    was = oi_prev.get(e, {}).get(cp, {}).get(k, 0) if e in oi_prev else 0
                    if v - was >= 1000 and abs(k + basis - price) >= 2000:
                        odd.append(f'{e[4:6]}/{e[6:8]} {fmt(k)} {lab} {fmt(v)} 口（今日 +{fmt(v-was)}）')
        if odd:
            warnings.append('<strong>遠價外今天冒出大 OI：</strong>'
                            + "、".join(odd[:3])
                            + '。離現價 2,000 點以上，幾乎確定是價差單的一條腿，不要當成有人在那裡佈壓力。')

    mp_bits = []
    for e in exps:
        if mp[e] is None:
            continue
        dist = mp[e] + basis - price
        mp_bits.append(f'{e[4:6]}/{e[6:8]} {fmt(mp[e])}（期指 {fmt(mp[e]+basis)}，距現價 {dist:+,.0f}'
                       + ('，太遠不採計）' if abs(dist) > 500 else '）'))

    D = {
        "head_date": f'{today:%Y / %m / %d} 收盤　→　下一個交易日日盤',
        "oi_date": f"{today:%Y/%m/%d}",
        "quote_stamp": (f'夜盤：MIS {night["time"][:2]}:{night["time"][2:4]}' if night else "夜盤：無報價"),
        "gen_at": datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M"),
        "basis": basis, "expiries": exps, "ladder": rows, "marks": marks,
        "zones": zones, "kpis": kpis, "notes": notes, "plays": plays, "warnings": warnings,
        "play_title": "下一個交易日的三個劇本",
        "swing": [lo, hi], "swing_dates": [lo_d, hi_d], "swing_dir": direction,
        "fibs": [{"ratio": r, "level": round(lv)} for r, lv in fibs],
        "resonance": reso,
        "maxpain": {e: mp[e] for e in exps}, "maxpain_all": mp_all,
        "maxpain_text": "；".join(mp_bits) or "算不出來",
        "dte_text": "、".join(f'{e[4:6]}/{e[6:8]} 剩 {dte[e]} 個交易日' for e in exps),
        "day": day, "night": night, "spot": spot, "gap": gap,
        "range": [rng_lo, rng_hi], "price": price,
        "inst": {"fut": inst["fut"], "fut_prev": inst["fut_prev"],
                 "opt": {f"{a_}_{b_}": v for (a_, b_), v in inst["opt"].items()}},
        "opt_vol_night": nvol, "opt_vol_day": dvol,
    }

    html = render(D)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ {a.out}")
    print(f"  到期別 {'、'.join(exps)}　階梯 {len(rows)} 列　價差 {basis:+d}")
    print(f"  波段 {fmt(lo)}（{lo_d}）→ {fmt(hi)}（{hi_d}）{direction}")
    print(f"  區間 {fmt(rng_lo)} – {fmt(rng_hi)}　現價 {fmt(price)}")
    for r in reso:
        print(f"  共振 {r['ratio']*100:.1f}% {fmt(r['level'])} × {fmt(r['fut'])} "
              f"{r['side']} {fmt(r['oi'])} 口（{r['grade']}，差 {r['gap']} 點）")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(D, f, ensure_ascii=False, indent=1, default=str)
        print(f"✓ {a.json}")
    if a.notify or a.dry_run:
        push_ntfy(D, dry=a.dry_run, url=a.url)
    if a.open:
        webbrowser.open("file://" + os.path.abspath(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
