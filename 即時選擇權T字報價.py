# -*- coding: utf-8 -*-
"""
即時選擇權T字報價.py

用 TAIFEX MIS 即時報價，產出台指選擇權 (TXO) T 字報價網頁 (CALL 紅 / PUT 綠)，
並可推播摘要到 iPhone (ntfy)。設計給 GitHub Actions 排程在雲端定時執行，
你的電腦關機時也會更新網頁與推播。

網頁分成「週三結算」「週五結算」「下週三結算」三個分頁，依到期日由近到遠取合約
（週三＝W 系列週選或月選，週五＝F 系列週選），量比、價平、▲▼ 增減都各算各的。

即時欄位（MIS，盤中/夜盤約每 5 秒更新）：權利金、成交量、成交金額、損益兩平。
盤後欄位（前一日 TAIFEX 收盤檔）：未平倉 OI → 支撐壓力牆。MIS 盤中不提供 OI。

用法：
    python3 即時選擇權T字報價.py                       # 產出 public/index.html
    python3 即時選擇權T字報價.py --notify              # 產出網頁並推播 ntfy（全量摘要）
    python3 即時選擇權T字報價.py --out 選擇權T字報價_當日.html
    python3 即時選擇權T字報價.py --radius 1500         # 顯示價平 ±N 點（預設 1500）

雲端：設環境變數 NTFY_TOPIC（GitHub Actions Secret）即會推播。
"""

import io
import os
import re
import csv
import sys
import json
import argparse
from datetime import datetime, date, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TW_TZ    = ZoneInfo("Asia/Taipei")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MIS_QUOTE_URL  = "https://mis.taifex.com.tw/futures/api/getQuoteList"
MIS_OPT_DAY    = "https://mis.taifex.com.tw/futures/RegularSession/EquityIndices/OptionsDomestic/"
MIS_OPT_NIGHT  = "https://mis.taifex.com.tw/futures/AfterHoursSession/EquityIndices/OptionsDomestic/"
MIS_FUT_DAY    = "https://mis.taifex.com.tw/futures/RegularSession/EquityIndices/FuturesDomestic/"
MIS_FUT_NIGHT  = "https://mis.taifex.com.tw/futures/AfterHoursSession/EquityIndices/FuturesDomestic/"
TAIFEX_DL_OPT  = "https://www.taifex.com.tw/cht/3/dlOptDataDown"
TAIFEX_DL_PAGE = "https://www.taifex.com.tw/cht/3/dlOptDailyMarketView"

CALL_MON = "ABCDEFGHIJKL"      # 買權月份碼 A=1月 … L=12月
PUT_MON  = "MNOPQRSTUVWX"      # 賣權月份碼 M=1月 … X=12月
SID_PAT  = re.compile(r'^(TX[A-Z0-9])(\d{3,5})([A-Z])(\d)$')


# ── 時段判斷 ────────────────────────────────────────────────────────────────

def current_session():
    """
    回傳 (顯示用時段, MIS MarketType)。
    MarketType 才是 MIS 決定日/夜盤的參數：'0' = 一般（日盤）、'1' = 盤後（夜盤）；
    Referer 只是擺樣子，不影響回傳內容。用錯會在日盤拿到前一夜盤的殘留報價。
    非交易時段沿用剛結束那個時段的最後成交價：
      13:45~15:00 → 日盤收盤；05:00~08:45 → 前一夜盤收盤。
    """
    now = datetime.now(TW_TZ)
    h, m = now.hour, now.minute
    if (h == 8 and m >= 45) or (9 <= h <= 12) or (h == 13 and m <= 45):
        return "日盤", "0"
    if h >= 15 or h < 5 or (h == 5 and m == 0):
        return "夜盤", "1"
    return ("非交易", "0") if 13 <= h < 15 else ("非交易", "1")


def _num(v):
    v = (v or "").replace(",", "").strip()
    if v in ("", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ── 1. MIS 即時選擇權報價 ─────────────────────────────────────────────────────

def fetch_mis_options(mkt):
    """回傳 MIS QuoteList；夜盤/非交易皆可取（非交易時為最後成交價）。"""
    ref = MIS_OPT_NIGHT if mkt == "1" else MIS_OPT_DAY
    r = requests.post(
        MIS_QUOTE_URL,
        json={"MarketType": mkt, "SymbolType": "O", "KindID": "1", "CID": "TXO",
              "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"},
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "Accept": "application/json, text/plain, */*",
                 "Referer": ref, "User-Agent": UA},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("RtData", {}).get("QuoteList", [])


def quote_key(cdate, ctime, night):
    """
    把 CDate + CTime 併成可比大小的整數，用來找出整份報價「最後更新」的那一筆。
    只比 CTime 字串會踩到夜盤跨午夜的坑：23:59 的字串大於凌晨 04:59，
    會把已經過去 5 小時的成交當成最新行情時間。
    """
    if not ctime or len(ctime) != 6:
        return -1
    try:
        d, t = int(cdate or 0), int(ctime)
    except ValueError:
        return -1
    if night and t < 80000:          # 夜盤 00:00~05:00 屬於隔天
        t += 240000
    return d * 1000000 + t


EXP_PAT = re.compile(r'\((\d{4})/(\d{2})/(\d{2})\)')


def third_wednesday(y, mon):
    """月選（TXO）到期日 = 該月第三個星期三。"""
    d = date(y, mon, 1)
    d += timedelta(days=(2 - d.weekday()) % 7)      # 該月第一個星期三
    return d + timedelta(days=14)


def parse_expiry(item, mon, yr):
    """
    取這檔合約的契約到期日（YYYYMMDD），用來跟 TAIFEX 盤後檔精準對上同一到期別。
    週選的 MIS 名稱直接帶日期，例 '臺指選W1 (2026/08/05)'、'臺指選F5 (2026/07/31)'；
    月選（TXO086）沒帶，改算該月第三個星期三。
    """
    for fld in ("DispEName", "DispCName"):
        m = EXP_PAT.search(item.get(fld, "") or "")
        if m:
            return "".join(m.groups())
    y = datetime.now(TW_TZ).year
    y = y - (y % 10) + int(yr)                      # 年碼是西元年個位數
    if y < datetime.now(TW_TZ).year - 1:            # 跨十年進位，例 2029 的 '0' → 2030
        y += 10
    return third_wednesday(y, mon).strftime("%Y%m%d")


SERIES_PAT = re.compile(r'臺指選\s*([A-Z]\d)')


def contract_label(name, mon):
    """把 MIS 中文名轉成短代號：'臺指選W1 (2026/08/05)' → W1；月選 '臺指選086' → 8月選。"""
    m = SERIES_PAT.search(name or "")
    return m.group(1) if m else f"{mon}月選"


def expiry_weekday(exp):
    """契約到期日是星期幾（0=一 … 6=日）；解析不出來回 -1。"""
    try:
        return date(int(exp[:4]), int(exp[4:6]), int(exp[6:8])).weekday()
    except Exception:
        return -1


def collect_groups(quote_list, night=False):
    """
    解析 MIS SymbolID（例 TXY44300G6 = root TXY / 履約 44300 / G=7月買權 / 年碼 6）。
    以 (root, 月, 年碼) 分群 = 同一到期別，回傳所有到期別 {gkey: grp}。
    """
    groups = defaultdict(lambda: {"C": {}, "P": {}, "vol": 0, "time": "", "date": "",
                                  "key": -1, "exp": "", "name": ""})
    for it in quote_list:
        sid = it.get("SymbolID", "").split("-")[0]
        m = SID_PAT.match(sid)
        if not m:
            continue
        root, strike, ltr, yr = m.groups()
        strike = int(strike)
        if ltr in CALL_MON:
            cp, mon = "C", CALL_MON.index(ltr) + 1
        elif ltr in PUT_MON:
            cp, mon = "P", PUT_MON.index(ltr) + 1
        else:
            continue
        gkey = (root, mon, yr)
        vol  = int(_num(it.get("CTotalVolume")) or 0)
        groups[gkey][cp][strike] = {
            "px":  _num(it.get("CLastPrice")),
            "vol": vol,
            "bid": _num(it.get("CBidPrice1")),
            "ask": _num(it.get("CAskPrice1")),
            # 今日相對昨收的權利金變動。MIS 直接給，不必自己存快照比對，
            # 而且是「今日累積」而非上一版的 5 分鐘差，訊號比 ▲▼ 強。
            "ref":  _num(it.get("CRefPrice")),
            "diff": _num(it.get("CDiff")),
            "rate": _num(it.get("CDiffRate")),
            "open": _num(it.get("COpenPrice")),
            "high": _num(it.get("CHighPrice")),
            "low":  _num(it.get("CLowPrice")),
        }
        groups[gkey]["vol"] += vol
        if not groups[gkey]["exp"]:
            groups[gkey]["exp"] = parse_expiry(it, mon, yr)
        if not groups[gkey]["name"]:
            groups[gkey]["name"] = contract_label(it.get("DispCName", ""), mon)
        t, dt = it.get("CTime", ""), it.get("CDate", "")
        kk = quote_key(dt, t, night)
        if kk > groups[gkey]["key"]:
            groups[gkey].update(key=kk, time=t, date=dt)

    if not groups:
        raise ValueError("MIS 未回傳可解析的選擇權報價")
    return dict(groups)


def picks_by_weekday(groups, weekday):
    """
    挑出到期日落在指定星期的到期別，依到期日由近到遠排成 list。
    以前只回「量最大」那一個，加了下週三分頁後不能再這樣挑：週三到期的除了
    最近的週選，還有月選與更遠的月份，量最大不等於第二近（例 08/26 W4 之後
    下一個週三是 09/02 W1 才 597 口，但 09/16 月選有 2694 口）。
    同一個到期日可能同時有兩組（月選遇上同日的週選），取量大的那組代表。
    """
    today = datetime.now(TW_TZ).strftime("%Y%m%d")
    by_exp = {}
    for g, v in groups.items():
        exp = v["exp"]
        # 已結算的合約 MIS 照理不會再回，但真的回了會讓「最近」那頁停在死合約
        if expiry_weekday(exp) != weekday or v["vol"] <= 0 or exp < today:
            continue
        cur = by_exp.get(exp)
        if cur is None or v["vol"] > cur[1]["vol"]:
            by_exp[exp] = (g, v)
    return [by_exp[e] for e in sorted(by_exp)]


def pick_nth(groups, weekday, nth):
    """該星期到期的第 nth 近（0 = 最近）到期別；沒有就回 None。"""
    lst = picks_by_weekday(groups, weekday)
    return lst[nth] if len(lst) > nth else None


# ── 2. 即時標的價（大台指期 TXF） ─────────────────────────────────────────────

def fetch_txf_price(session, mkt):
    # 期貨的 SymbolID 後綴會跟著時段變：日盤 TXFH6-F、夜盤 TXFH6-M。
    ref  = MIS_FUT_NIGHT if mkt == "1" else MIS_FUT_DAY
    sfx  = "-M" if mkt == "1" else "-F"
    try:
        r = requests.post(
            MIS_QUOTE_URL,
            json={"MarketType": mkt, "SymbolType": "F", "KindID": "1", "CID": "",
                  "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"},
            headers={"Content-Type": "application/json;charset=UTF-8",
                     "Referer": ref, "User-Agent": UA},
            timeout=12,
        )
        items = r.json().get("RtData", {}).get("QuoteList", [])
        txf = [i for i in items if i.get("SymbolID", "").startswith("TXF")
               and i.get("SymbolID", "").endswith(sfx)]
        txf.sort(key=lambda i: int((_num(i.get("CTotalVolume")) or 0)), reverse=True)
        for it in txf:
            px = _num(it.get("CLastPrice"))
            if px and px > 10000:
                return px, f"TXF近月({session})"
    except Exception:
        pass
    return None, ""


# ── 3. 前一交易日未平倉 OI（支撐壓力用；MIS 盤中無 OI） ────────────────────────

_OI_CACHE = {}          # 一次下載、多個到期別共用（各分頁都要查同一份收盤檔）


def load_oi_buckets():
    """
    下載 TAIFEX 最近一個交易日的選擇權收盤檔，照「契約到期日」分桶。
    回傳 (buckets, 資料日期)；抓不到回 (None, None)。結果快取於行程內。
    """
    if "v" in _OI_CACHE:
        return _OI_CACHE["v"]
    _OI_CACHE["v"] = (None, None)
    d = datetime.now(TW_TZ).date()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": TAIFEX_DL_PAGE})
    try:
        sess.get(TAIFEX_DL_PAGE, timeout=15)
    except Exception:
        return _OI_CACHE["v"]
    for _ in range(8):
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        try:
            r = sess.post(TAIFEX_DL_OPT, data={
                "down_type": "1", "commodity_id": "TXO", "commodity_id2": "all",
                "queryStartDate": d.strftime("%Y/%m/%d"), "queryEndDate": d.strftime("%Y/%m/%d"),
                "commodity_id2t": "",
            }, timeout=25)
            text = r.content.decode("ms950", errors="ignore")
            # 先照到期日分桶，再挑出要的那一個到期別
            buckets = defaultdict(lambda: {"C": defaultdict(int), "P": defaultdict(int)})
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("交易時段", "").strip() != "一般":
                    continue
                try:
                    dt = row["契約到期日"].strip()          # 例 20260724
                    if not dt:
                        continue
                    cp = "C" if row["買賣權"].strip() == "買權" else "P"
                    k  = int(float(row["履約價"]))
                    oi = int(_num(row["未沖銷契約數"]) or 0)
                    buckets[dt][cp][k] += oi
                except Exception:
                    pass
            if not buckets:
                d -= timedelta(days=1)
                continue
            _OI_CACHE["v"] = (buckets, d)
            return _OI_CACHE["v"]
        except Exception:
            pass
        d -= timedelta(days=1)
    return _OI_CACHE["v"]


def fetch_prev_oi(expiry, mon):
    """
    從 TAIFEX 每日收盤檔取「同一契約到期日」各履約價 OI。
    以前只比對月份，同月的週選（TXZ/TX1/TXU…）與月選會被加總成同一面牆，
    支撐壓力因此失真；改用契約到期日精準比對。
    找不到該到期日時（例如遇假日調整），退回同月合計並標記 exact=False。
    回傳 {'C':{k:oi}, 'P':{k:oi}, 'date':d, 'exp':YYYYMMDD, 'exact':bool}，失敗回 None。
    """
    buckets, d = load_oi_buckets()
    if not buckets:
        return None
    if expiry in buckets:
        b = buckets[expiry]
        return {"C": b["C"], "P": b["P"], "date": d, "exp": expiry, "exact": True}
    # 對不到（假日調整或名稱解析失誤）：退回同月合計，並在畫面上標示
    same = [dt for dt in buckets if len(dt) == 8 and int(dt[4:6]) == mon]
    if same:
        out = {"C": defaultdict(int), "P": defaultdict(int),
               "date": d, "exp": "、".join(sorted(same)), "exact": False}
        for dt in same:
            for cp in ("C", "P"):
                for k, v in buckets[dt][cp].items():
                    out[cp][k] += v
        return out
    return None


# ── 4. 組報告資料 ─────────────────────────────────────────────────────────────

def group_fwd(grp):
    """用買賣權 parity 推當組的遠期價；沒有可配對的買賣權回 None。"""
    calls, puts = grp["C"], grp["P"]
    common = [k for k in calls if k in puts and calls[k]["px"] and puts[k]["px"]]
    if not common:
        return None
    atm_parity = min(common, key=lambda k: abs(calls[k]["px"] - puts[k]["px"]))
    return atm_parity + calls[atm_parity]["px"] - puts[atm_parity]["px"]


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def moneyness(k, under, is_call):
    """價內 in-the-money／價外 out-of-the-money。買權履約價低於標的為價內，賣權相反。"""
    itm = (k < under) if is_call else (k > under)
    return "itm" if itm else "otm"


MONEY_TXT = {"itm": "價內", "otm": "價外"}


def add_excess(rows, under, is_call):
    """
    算「超額漲跌」：該履約價今日漲跌% 減掉「同側、同價內外」履約價漲跌%的中位數。

    為什麼不能直接看漲跌%：指數一漲，全部 CALL 一起漲、全部 PUT 一起跌，
    那是方向 beta，不是誰在這個履約價出手。扣掉中位數之後剩下的，
    才是這一檔相對同儕的異常強弱 —— 跌得比同儕兇 = 有人在壓價收租（築牆），
    漲得比同儕兇 = 有人在追價（想突破）。跟細族群輪動要剔除大盤 beta 同一個道理。

    為什麼還要分價內／價外：兩者的 % 變動根本不是同一種東西。價內權利金主要跟著
    內含價值走（指數跌 1,000 點，價內賣權就多 1,000 點內含價值），價外動的則是
    時間價值與隱含波動率，基數又小，%天生大得多。把兩群混在一起取中位數，
    等於拿內含價值的變動去當時間價值的基準，價內檔會被系統性誤判。

    顯著與否用 |超額| 的中位數當尺度（MAD 概念），自適應盤中波動大小，不硬編閾值。
    每一組各自算自己的中位數與尺度；樣本不足 MIN_GROUP_N 檔就整組不判定 ——
    分組後樣本變少，三五檔算出來的中位數沒有代表性，寧可留白也不要給假訊號。
    """
    # 沒成交的檔位 rate 可能是前一筆的陳舊值，不能拿來定基準
    live = [r for r in rows.values() if r["rate"] is not None and r["vol"] > 0]
    for r in rows.values():
        r["tag"] = ""
        r.setdefault("excess", None)
        r.setdefault("grp", "")
        r.setdefault("core", False)      # 量夠大、有資格被判定的檔
        r.setdefault("scale", None)      # 該組的顯著門檻（單檔用）
        r.setdefault("mad", None)        # 該組的原始離散度，側分數的門檻要用它
    if not live:
        return

    # 量門檻取「整側總量 1%」與絕對下限的較大值。只用相對門檻會在清淡時失效：
    # 剛掛牌的週選一側可能總共才 24 口，1% = 0.24 口，於是 1 口成交就被當成大戶，
    # 判出「賣方築牆」。單一履約價要有解讀價值，先得有起碼的口數。
    vol_gate = max((sum(r["vol"] for r in live) or 1) * 0.01, MIN_TAG_VOL)

    for g in ("itm", "otm"):
        grp = [r for r in live if moneyness(r["K"], under, is_call) == g]
        # 基準只用量夠大的檔算，且判定對象就是同一批。兩者必須對齊：
        # 冷門檔的報價更新慢，跟不上最新行情，拿它們一起算中位數會把基準拖低，
        # 結果量大的檔全部呈現正超額 —— 那是基準偏掉，不是市場真的在追價。
        core = [r for r in grp if r["vol"] >= vol_gate]
        if len(core) < MIN_GROUP_N:
            continue
        med = _median([r["rate"] for r in core])
        mad = _median([abs(r["rate"] - med) for r in core]) or 1.0
        scale = max(mad * EXCESS_K, MIN_EXCESS_PT)
        for r in grp:                      # 超額全組都算，冷門檔也看得到自己的位置
            r["excess"] = r["rate"] - med
            r["grp"] = g
            r["scale"] = scale
            r["mad"] = mad
        for r in core:                     # 但只有量夠大的才下買賣方判定
            r["core"] = True
            if r["excess"] <= -scale:
                r["tag"] = "sell"    # 相對同儕被壓 → 賣方築牆
            elif r["excess"] >= scale:
                r["tag"] = "buy"     # 相對同儕被追 → 買方挑戰


TAG_TXT = {"sell": "賣方築牆", "buy": "買方追價", "": "中性"}

# 一側價外合計成交量低於此數，就不把口數最大的履約價當成牆
MIN_ZONE_VOL = 500

# 超額顯著門檻 = EXCESS_K × |超額|中位數。
# 1.0 太鬆：1 MAD ≈ 0.67σ，統計上約一半的履約價都會超過，標記滿版等於沒標。
# 2.0 ≈ 1.35σ，只有約兩成會亮燈，標到的才是真的異常。
EXCESS_K = 2.0

# 價內／價外分組後，一組有效樣本少於此數就整組不判定
MIN_GROUP_N = 5

# 超額門檻的絕對下限（百分點）。純自適應會在同質性高的組裡失控：
# 價外買權有時 30 檔全部同步跌 -50~-56%，MAD 只有 1pt，門檻自動縮到 ±2pt，
# 於是連報價跳動的雜訊都會被判成「買方追價」。低於這個幅度的超額沒有解讀價值。
MIN_EXCESS_PT = 5.0

# 單一履約價要被判買賣方，至少要有的成交口數（絕對下限）
MIN_TAG_VOL = 50


# ── 4.5 莊家意圖：把兩側壓成四象限 ───────────────────────────────────────────
#
# 側分數的顯著門檻。關鍵是它不能直接沿用單檔的 scale ——
# 側分數是幾十檔的加權平均，平均會把離散度壓掉 √N_eff 倍，
# 拿單檔的尺去量平均值等於要求 4σ 以上，卡片會整天卡在「觀望」。
# 正確的尺是加權平均自己的標準誤：MAD × √Σw²（w = 各檔的成交量佔比）。
# Σw² 的倒數就是有效樣本數，量能越集中在少數履約價，門檻自動放寬 ——
# 這正好對應「錢押得越集中，訊號越該被採信」。
SIDE_GATE_K  = 2.0
MIN_SIDE_PT  = 2.0
# 一側價外核心檔合計低於此口數就不判定（與撐壓卡同一把尺）
MIN_SIDE_VOL    = MIN_ZONE_VOL


def side_stance(rows, under):
    """
    把一側的價外檔位壓成一個「今天誰主導」的分數：量加權超額。

    為什麼一定要「量加權」而不是平均：excess 是扣掉同組中位數算出來的，
    等權平均必然≈0 —— 中位數的定義就保證一半在上、一半在下，
    拿等權平均當側分數等於在看雜訊。量加權問的是完全不同的問題：
    **今天的錢押在被壓的檔，還是被追的檔**。
      · 錢集中在超額為負的履約價 → 賣方主導（買權側=SC、賣權側=SP）
      · 錢集中在超額為正的履約價 → 買方主導（買權側=BC、賣權側=BP）

    只取價外：價內權利金主要跟著內含價值走，%變動不反映誰在出手；
    而且對台指方向判讀有意義的牆本來就都在價外。

    只取 core（量 ≥ 門檻）：冷門檔報價更新慢，超額是陳舊值，
    納進來只會把分數往雜訊拉。判定對象與基準必須是同一批，理由同 add_excess。

    門檻不是固定值，是這個加權平均自己的標準誤 ×2（見 SIDE_GATE_K 上方說明）。
    """
    core = [r for r in rows.values()
            if r.get("core") and r.get("grp") == "otm" and r.get("excess") is not None]
    vol  = sum(r["vol"] for r in core)
    if len(core) < MIN_GROUP_N or vol < MIN_SIDE_VOL:
        return {"score": None, "stance": None, "vol": vol, "n": len(core), "top": None}
    score = sum(r["vol"] * r["excess"] for r in core) / vol
    mad   = _median([r["mad"] for r in core if r["mad"]]) or 1.0
    sew   = mad * (sum((r["vol"] / vol) ** 2 for r in core) ** 0.5)   # 加權平均的標準誤
    gate  = max(SIDE_GATE_K * sew, MIN_SIDE_PT)
    stance = "buy" if score >= gate else ("sell" if score <= -gate else "")
    return {"score": score, "stance": stance, "vol": vol, "n": len(core),
            "top": max(core, key=lambda r: r["vol"])["K"], "gate": gate}


# 四腳代號：(側, 主導方) → 代號。買權側被壓＝有人賣買權（SC），依此類推。
LEG = {("C", "sell"): "SC", ("C", "buy"): "BC",
       ("P", "sell"): "SP", ("P", "buy"): "BP"}
LEG_TXT = {"SC": "賣方蓋天花板", "BC": "買方賭突破",
           "SP": "賣方不怕跌",   "BP": "有人買保險"}

# 四象限：(買權側主導, 賣權側主導) → (代號, 標題, 台指怎麼做)
QUAD = {
    ("sell", "sell"): ("range", "區間盤",
        "賣方兩面收租，上下都守得住。台指在上下緣之間逆勢做 —— "
        "碰上緣偏空、碰下緣偏多，不追中間。"),
    ("buy", "sell"): ("bull", "偏多",
        "上方被追價、下方賣方不怕跌，四種裡最順的多方盤。"
        "台指拉回下緣找多，不要追高。"),
    ("sell", "buy"): ("bear", "偏空",
        "上方被蓋死、下方有人買保險。台指反彈到上緣找空，不要接刀。"),
    ("buy", "buy"): ("vol", "待變盤",
        "兩側都是買方 —— 在買波動不是買方向，市場自己也不知道要往哪。"
        "不要做區間，等突破上下緣任一端再順勢進場。"),
}


def build_mind(crows, prows, under, zone):
    """
    莊家意圖卡：兩側的量加權超額 → 四象限 → 台指的方向、區間與失效條件。

    這張卡是給「只做台指、不做選擇權」的人用的：選擇權的四個腳
    （BC/SC/BP/SP）在這裡不是要你去下單，是拿來反推莊家把牆蓋在哪、
    哪一面願意扛。綠底（賣方築牆）比較接近法人立場，粉底（買方追價）
    散戶成分高，所以「牆在不在」比「誰在追」更值得當方向依據。

    刻意保留「觀望」這個結果：有一側沒表態時不硬湊象限。
    沒有訊號本來就是一種訊號，硬給結論才是虧錢的來源。
    """
    c = side_stance(crows, under)
    p = side_stance(prows, under)

    # 上下緣優先用盤中成交口數重心（今天的戰場），沒有才退回昨日 OI 牆（結構）
    hi = zone.get("res_k") or zone.get("c_wall")
    lo = zone.get("sup_k") or zone.get("p_wall")
    hi_src = "盤中" if zone.get("res_k") else ("昨日OI" if zone.get("c_wall") else None)
    lo_src = "盤中" if zone.get("sup_k") else ("昨日OI" if zone.get("p_wall") else None)

    def kx(v):
        return f"{v:,}" if v else "—"

    q = QUAD.get((c["stance"], p["stance"]))
    if q:
        code, title, how = q
        if code == "range":
            bad = (f'上緣 {kx(hi)} 站上或下緣 {kx(lo)} 失守，'
                   f'且該側由綠翻粉（賣方棄守）→ 停掉逆勢單改順勢。')
        elif code == "bull":
            bad = f'下緣 {kx(lo)} 失守，或上方買權由粉翻綠（追價的人退了）。'
        elif code == "bear":
            bad = f'上緣 {kx(hi)} 站上，或下方賣權由粉翻綠（買保險的人退了）。'
        else:
            bad = f'站上 {kx(hi)} 做多、跌破 {kx(lo)} 做空；在區間內不進場。'
    else:
        code, title = "flat", "觀望"
        reason = []
        if c["stance"] is None: reason.append("買權側量能／樣本不足")
        elif not c["stance"]:   reason.append("買權側中性")
        if p["stance"] is None: reason.append("賣權側量能／樣本不足")
        elif not p["stance"]:   reason.append("賣權側中性")
        how = (f'{"、".join(reason)}，四象限不成立。'
               '莊家今天沒有表態，這種盤最容易兩面被巴 —— 先不做。')
        bad = "等任一側出現明確的綠（築牆）或粉（追價）再看。"

    # 牆的位移是獨立於超額的第二個維度：陣地往哪邊挪，代表結構在往哪邊讓
    shifts = []
    for sh, up, dn in ((zone.get("res_shift"), "買權陣地上移", "買權陣地下移"),
                       (zone.get("sup_shift"), "支撐上移", "支撐下移")):
        if sh is not None and abs(sh) > 100:
            shifts.append(f'{up if sh > 0 else dn} {abs(sh):,} 點'
                          f'（偏{"多" if sh > 0 else "空"}）')

    return {
        "code": code, "title": title, "how": how, "bad": bad,
        "c": c, "p": p, "hi": hi, "lo": lo, "hi_src": hi_src, "lo_src": lo_src,
        "c_leg": LEG.get(("C", c["stance"])) if c["stance"] else None,
        "p_leg": LEG.get(("P", p["stance"])) if p["stance"] else None,
        "shifts": shifts,
    }


def build_zone(crows, prows, under, oi, lo, hi, top=3):
    """
    盤中撐壓：用「今日累積成交口數」的分布抓牆的位置。

    為什麼排序用口數而不是金額：這張卡片要跟昨日 OI 牆對照，而 OI 的單位就是口數，
    用金額排就是拿兩把尺在比。更實際的問題是金額 = 權利金×50×口數，權利金隨著
    接近價平而變大，用金額排幾乎必然選出離標的最近的那一檔 —— 那是價平的定義，
    不是市場押注的位置。口數才是部位規模，也才是「牆有多高」。
    金額仍然並列顯示，看的是資金投入強度，兩個問題分兩欄回答。

    範圍限制在畫面的 ±radius 視窗內，順便壓掉深價外樂透票對口數的灌水。
    再用 add_excess 的 tag 標記那道牆是賣方在築（硬）還是買方在打（可能破）。
    """
    def pick(rows, keep):
        cand = [r for r in rows.values() if keep(r["K"]) and r["vol"] > 0]
        cand.sort(key=lambda r: r["vol"], reverse=True)
        return cand[:top], sum(r["vol"] for r in cand)

    res, res_vol = pick(crows, lambda k: k >= under)      # 壓力：價外買權
    sup, sup_vol = pick(prows, lambda k: k <= under)      # 支撐：價外賣權

    # 量能門檻：剛掛牌的週選一側常只有個位數口成交，那種「牆」是雜訊。
    # 寧可標成量能不足也不要給一個看起來很篤定的價位。
    res_thin = res_vol < MIN_ZONE_VOL
    sup_thin = sup_vol < MIN_ZONE_VOL

    # OI 牆要跟盤中重心放在同一個尺上比，否則會憑空生出「移動了三千點」的假訊號：
    #   1. 限制在標的同一側
    #   2. 限制在畫面顯示的 ±radius 視窗內 —— 台指深價外買權長年有賣方遠期收租的
    #      巨量存量 OI（例如標的 42,300 時最大 OI 在 46,500），那不是今天的壓力。
    def wall_of(d, keep):
        c = {k: v for k, v in d.items() if lo <= k <= hi and keep(k) and v > 0}
        return max(c, key=lambda k: c[k]) if c else None

    c_wall = wall_of(oi["C"], lambda k: k >= under) if oi else None
    p_wall = wall_of(oi["P"], lambda k: k <= under) if oi else None
    return {
        "res": res, "sup": sup,
        "res_vol": res_vol, "sup_vol": sup_vol,
        "res_thin": res_thin, "sup_thin": sup_thin,
        "res_k": res[0]["K"] if (res and not res_thin) else None,
        "sup_k": sup[0]["K"] if (sup and not sup_thin) else None,
        "c_wall": c_wall, "p_wall": p_wall,
        # 盤中金額第一名 vs 昨日同側 OI 牆差幾點；差距大代表牆在移動
        "res_shift": (res[0]["K"] - c_wall) if (res and c_wall and not res_thin) else None,
        "sup_shift": (sup[0]["K"] - p_wall) if (sup and p_wall and not sup_thin) else None,
    }


def build_report(gkey, grp, session, under, usrc, tab_id, tab_name, radius=1500):
    """把單一到期別（一個分頁）的資料整理成畫面要的形狀。"""
    root, mon, yr = gkey
    calls, puts = grp["C"], grp["P"]

    fwd = group_fwd(grp)
    # 價平只是拿來標熱區，不需要買賣權「成對」才算得出來。
    # 週五合約冷清（量約週三的 2%），夜盤剛開盤時常常整組找不到任何一個履約價
    # 兩邊都有成交價；以前這裡直接 raise，害整支程式掛掉、連週三那頁也一起沒了。
    # 改成：先取成對的（parity 較準），沒有就退到任一邊有價的履約價。
    common = [k for k in calls if k in puts and calls[k]["px"] and puts[k]["px"]]
    if not common:
        common = [k for k in set(calls) | set(puts)
                  if (calls.get(k) or {}).get("px") or (puts.get(k) or {}).get("px")]
    if not common:
        raise ValueError(f"{tab_name}：整組無任何成交價可定價")
    atm = min(common, key=lambda k: abs(k - under))

    expiry = grp["exp"]
    oi = fetch_prev_oi(expiry, mon)

    lo, hi = under - radius, under + radius
    strikes = sorted(k for k in set(calls) | set(puts) if lo <= k <= hi)

    def mk(side, k, is_call):
        d = side.get(k)
        if not d or not d["px"]:
            return None
        px, vol = d["px"], d["vol"]
        return {"K": k, "px": px, "vol": vol, "amt": int(px * 50 * vol),
                "be": (k + px) if is_call else (k - px),
                "bid": d["bid"], "ask": d["ask"],
                "rate": d["rate"], "diff": d["diff"], "ref": d["ref"]}

    crows = {k: mk(calls, k, True)  for k in strikes if mk(calls, k, True)}
    prows = {k: mk(puts,  k, False) for k in strikes if mk(puts,  k, False)}

    add_excess(crows, under, True)
    add_excess(prows, under, False)
    zone = build_zone(crows, prows, under, oi, lo, hi)
    mind = build_mind(crows, prows, under, zone)

    # MIS CTime 例 213907 → 21:39:07；非今日的資料把日期一起標出來，
    # 免得像 07/31 早上那次：產生時間是今天早上、行情時間卻是昨晚而看不出來。
    t, dt = grp["time"], grp["date"]
    tstr = f"{t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) == 6 else "-"
    today = datetime.now(TW_TZ).strftime("%Y%m%d")
    stale = bool(dt) and dt != today
    if stale:
        tstr = f"{dt[4:6]}/{dt[6:8]} {tstr}"

    return {
        "id": tab_id, "tab": tab_name, "series": grp["name"],
        "session": session, "root": root, "mon": mon, "yr": yr,
        "under": under, "usrc": usrc, "atm": atm, "fwd": fwd,
        "strikes": strikes, "crows": crows, "prows": prows,
        "oi": oi, "time": tstr, "stale": stale, "expiry": expiry,
        "vol": grp["vol"], "zone": zone, "mind": mind,
    }


# 分頁定義：(分頁 id, 到期日星期, 第幾近的到期日, 分頁標題)。
# 月選也是星期三到期，排在週三那條時間線上（第三個星期三那週就是月選當家）。
# 排列照結算先後：本週三 → 週五 → 下週三。
TABS = [("wed", 2, 0, "週三結算"), ("fri", 4, 0, "週五結算"), ("wed2", 2, 1, "下週三結算")]


def build_page(radius=1500):
    """抓一次 MIS，拆出各分頁對應的到期別，組成整頁資料。"""
    session, mkt = current_session()

    ql = fetch_mis_options(mkt)
    groups = collect_groups(ql, night=(mkt == "1"))

    picks = [(tid, name) + (pick_nth(groups, wd, nth) or (None, None))
             for tid, wd, nth, name in TABS]
    picks = [(tid, name, g, v) for tid, name, g, v in picks if g]
    if not picks:
        raise ValueError("MIS 未回傳週三／週五到期的選擇權報價")

    # 標的價：TXF 即時優先；抓不到就用成交量最大那組的 parity 推算
    under, usrc = fetch_txf_price(session, mkt)
    if not under:
        top = max(picks, key=lambda p: p[3]["vol"])
        under, usrc = group_fwd(top[3]), "價平parity"
        if not under:
            raise ValueError("無標的價可用（TXF 與 parity 皆失敗）")

    # 一頁做不出來不該拖垮另一頁：週五冷清時整支程式會死，網頁不更新、推播也不發，
    # 而週三那頁其實資料好好的。改成逐頁隔離，全部失敗才放棄。
    # 下週三剛掛牌時常常只有個位數口成交，本來就可能做不出來，這裡會自己略過。
    reps, errs = [], []
    for tid, name, g, v in picks:
        try:
            reps.append(build_report(g, v, session, under, usrc, tid, name, radius=radius))
        except Exception as e:
            errs.append(f"{name}：{e}")
            print(f"  ⚠ 略過分頁 {name}：{e}")
    if not reps:
        raise ValueError("所有分頁都無法產生（" + "；".join(errs) + "）")

    # 這裡本來還會收一個抓櫃買體溫的背景 thread。改放多空空間卡之後，
    # 那張卡的資料是瀏覽器端直接跟 Cloudflare Worker 拿的，本頁一個 HTTP 都不必多打。
    return {
        "session": session, "under": under, "usrc": usrc, "reps": reps,
        "now": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        # 給網頁算「資料幾分鐘前」用。存 epoch 秒而非字串，才不會因為看的人
        # 所在時區不同而把台北時間誤判成當地時間。
        "epoch": int(datetime.now(TW_TZ).timestamp()),
    }


# ── 4.9 盤中多空空間（全市場三線）───────────────────────────────────────────
#
# 為什麼這張卡的資料不是這支程式自己抓的：三條線要掃全市場約 2,700 檔的即時報價，
# 掃一輪要 20 秒。放進本頁的請求裡一定拖爆 —— Vercel 的 function 有執行時間上限，
# 而使用者打開這頁的耐心預算是 10 秒。所以掃描交給常駐在 Cloudflare 的另一支
# Worker（本工作區的 盤中多空空間App/，cron 每 5 分鐘掃一輪把結果存進 KV），
# 這頁只在**瀏覽器端**去讀那份 KV 快照畫圖。因此：
#   1. 這張卡的即時性由 Worker 的 5 分鐘 cron 決定，跟本頁的產生時間無關。
#      本頁 60 秒自動重整會順便重畫，卡片自己另外也每 60 秒重抓一次 ——
#      所以就算使用者把頁面開著不動，圖也會自己往前長。
#   2. 兩條線互不牽連：本頁抓不到 MIS 時這張卡照樣是活的，反之亦然。
#   3. Worker 那邊一定要開 CORS（已開 Access-Control-Allow-Origin: *），
#      否則瀏覽器會靜靜擋掉，卡片永遠空白而且不會有任何錯誤畫面可看。
#
# 三條線的定義（與 分析_盤中多空空間.py、盤中多空空間App/public/app.js 一致）：
#   多空空間（紅綠棒）＝上漲家數 − 下跌家數
#   快動能（黃線）    ＝多空空間 − 前 6 根（30 分鐘）移動平均
#   慢動能（藍線）    ＝多空空間 − 前 20 根（100 分鐘）移動平均，
#                      每 150 家跳一階、夾在 ±200
#
# 這張卡**刻意不搬型態判讀過來**（開盤衰竭、反彈品質、背離那一整套）。
# 那些規則現在已經有本機 Python 與 App 前端兩份要同步，再多一份必然走針。
# 卡片只給事實：現值、日內最強最弱、紅綠棒根數；要判讀就點右上角進 App。
DUOKONG_URL = "https://duokong.emma198574.workers.dev"


def render_duokong():
    """多空空間卡的外殼。數字與圖都由 DUOKONG_JS 在瀏覽器端填。

    刻意不做成分頁：全市場廣度跟選擇權到期別無關，三個分頁該看到同一份。
    預設 hidden，抓到資料才顯示 —— 讀不到 Worker 時整張卡不出現，
    不要在頁面最上面留一個空框讓人以為是壞掉。
    """
    return '''<div class="dk" id="dk" hidden>
  <div class="dkh">盤中多空空間<small>全市場家數</small>
    <span class="dkq" id="dkq">—</span>
    <span class="dkd" id="dkd"></span>
    <a class="dka" href="__URL__" target="_blank" rel="noopener">完整判讀 ↗</a>
  </div>
  <div class="dkwrap"><canvas id="dkcv"></canvas></div>
  <div class="dkfact" id="dkfact"></div>
  <div class="dkkpis" id="dkkpi"></div>
  <div class="dknote">紅綠棒＝<b>多空空間</b>（上漲家數 − 下跌家數），
    黃線＝<b>快動能</b>（減前 30 分鐘均值），藍階＝<b>慢動能</b>（減前 100 分鐘均值）。
    每 5 分鐘一根，與本頁的選擇權報價各自獨立更新。<b>未經回測，不是進出場訊號。</b></div>
</div>'''.replace("__URL__", DUOKONG_URL)


# ── 5. HTML 產出 ─────────────────────────────────────────────────────────────

def heat(amt, mx, base):
    """金額格背景：熱度（資金強度），開 0.55 次方壓縮讓中小值也看得見。"""
    t = (amt / mx) ** 0.55 if mx else 0
    r, g, b = base
    return f"rgb({int(255+(r-255)*t)},{int(255+(g-255)*t)},{int(255+(b-255)*t)})"


def bar(vol, mx, base, to_left):
    """
    口數格背景畫成成交口數分布條：CALL 由右往左長、PUT 由左往右長，
    以履約價欄為中軸，整張表就是一張左右對開的盤中部位分布圖。
    刻意用線性比例（不像熱度圖壓縮），分布圖要能直接目測倍數關係 ——
    這欄回答「牆有多高」，隔壁金額欄回答「押了多少錢」。
    """
    t = (vol / mx) if mx else 0
    pct = max(0.0, min(100.0, t * 100))
    r, g, b = base
    d = "left" if to_left else "right"
    return (f"background:linear-gradient(to {d},"
            f"rgba({r},{g},{b},.42) 0 {pct:.1f}%,transparent {pct:.1f}% 100%)")

CALL_BASE = (214, 52, 52)
PUT_BASE  = (30, 160, 70)

# 在瀏覽器端用 localStorage 記住上一版（上一個「產生時間」）的金額與權利金，
# 每次載入就地算出各履約價相對上一版的增減（▲紅=增加、▼綠=減少）。
# 金額 ▲▼ 搭配權利金 ▲▼ 可判斷主導方：
#   金額增 + 權利金漲 → 買方（BC/BP）追價；金額增 + 權利金跌 → 賣方（SC/SP）壓價收租。
# 雲端每 5 分鐘換一版新資料、網頁每 60 秒重整；同一版重整不會洗掉差額。
DELTA_JS = """
<script>
(function(){
  var wrap = document.querySelector('.wrap');
  if(!wrap) return;
  var GEN = wrap.getAttribute('data-gen') || '';
  // 金額格與權利金格一起記；key = 分頁:側別:履約價:a(金額)/p(權利金)
  // 分頁要進 key，否則週三／週五同一履約價會互相蓋掉基準。
  var cells = [].slice.call(document.querySelectorAll('td[data-amt],td[data-px]'));
  function keyOf(td){
    return td.getAttribute('data-tab') + ':' + td.getAttribute('data-side') + ':' +
           td.getAttribute('data-k') + (td.hasAttribute('data-amt') ? ':a' : ':p');
  }
  var cur = {};
  cells.forEach(function(td){
    cur[keyOf(td)] = +(td.getAttribute('data-amt') || td.getAttribute('data-px'));
  });
  var prev = null;
  try { prev = JSON.parse(localStorage.getItem('txo_snap2') || 'null'); } catch(e){}
  var base = null;
  if(!prev){
    localStorage.setItem('txo_snap2', JSON.stringify({gen:GEN, cur:cur, base:cur}));
  } else if(prev.gen === GEN){
    base = prev.base;                        // 同一版重整：沿用既有基準
  } else {
    base = prev.cur;                         // 換新版：上一版數字成為新基準
    localStorage.setItem('txo_snap2', JSON.stringify({gen:GEN, cur:cur, base:prev.cur}));
  }
  if(!base) return;                          // 第一次看：尚無可比較的基準
  function fmtAmt(n){ return n.toLocaleString('en-US'); }
  function fmtPx(n){ return String(Math.round(n * 100) / 100); }   // 權利金保留小數
  cells.forEach(function(td){
    var key = keyOf(td);
    if(!(key in base)) return;
    var d = cur[key] - base[key];
    if(Math.abs(d) < 1e-9) return;
    var s = td.querySelector('.delta');
    if(!s) return;
    var isAmt = td.hasAttribute('data-amt');
    s.textContent = (d > 0 ? '▲ ' : '▼ ') + (isAmt ? fmtAmt(Math.abs(d)) : fmtPx(Math.abs(d)));
    s.className = 'delta' + (isAmt ? '' : ' plain') + (d > 0 ? ' up' : ' down');
    s.style.display = 'inline-block';
  });
})();
// 顯示「這份資料是幾分鐘前抓的」。雲端排程會被 GitHub 跳過，光看產生時間不容易
// 察覺已經停更很久，所以每 10 秒重算一次年齡：15 分鐘以上轉黃、40 分鐘以上轉紅。
(function(){
  var el = document.getElementById('age');
  var wrap = document.querySelector('.wrap');
  if(!el || !wrap) return;
  var epoch = parseInt(wrap.getAttribute('data-epoch') || '0', 10);
  if(!epoch) return;
  // 莊家意圖卡跟資料年齡綁在一起：表格慢 15 分鐘還能看，方向判讀慢 15 分鐘
  // 會害人做反，所以過期就整張轉灰、蓋上警語，不留一個看起來很篤定的結論。
  var minds = [].slice.call(document.querySelectorAll('.mind'));
  function tick(){
    var sec = Math.max(0, Math.floor(Date.now()/1000) - epoch);
    minds.forEach(function(m){ m.classList.toggle('expired', sec >= 900); });
    var txt;
    if(sec < 60)            txt = sec + ' 秒前';
    else if(sec < 3600)     txt = Math.floor(sec/60) + ' 分鐘前';
    else                    txt = Math.floor(sec/3600) + ' 小時 ' + Math.floor((sec%3600)/60) + ' 分前';
    el.textContent = '資料 ' + txt;
    el.className = sec >= 2400 ? 'dead' : (sec >= 900 ? 'stale' : '');
  }
  tick();
  setInterval(tick, 10000);
})();
// 每 60 秒帶時間戳重新載入：繞過 iPhone 主畫面 App 與 CDN 的快取，永遠抓最新那版。
setTimeout(function(){
  location.replace(location.pathname + '?t=' + Date.now());
}, 60000);
</script>
"""


def money(a):
    """金額用億／萬顯示；盤中動輒上億，逐位數字反而讀不出量級。"""
    return f"{a/1e8:.2f} 億" if a >= 1e8 else f"{a/1e4:,.0f} 萬"


def chg_td(r):
    """今日漲跌%（相對昨收）。底色標的是『超額』而非漲跌本身：
    綠＝相對同側同儕被壓（賣方築牆）、紅＝被追價（買方挑戰）。"""
    rate = r.get("rate")
    if rate is None:
        return '<td class="chg"></td>'
    tag = r.get("tag", "")
    ex  = r.get("excess")
    g   = MONEY_TXT.get(r.get("grp", ""), "")
    tip = (f'今日 {rate:+.0f}%，對照{g}同儕超額 {ex:+.0f}pt → {TAG_TXT[tag]}'
           if ex is not None else f'今日 {rate:+.0f}%（樣本不足或無成交，不判定）')
    return f'<td class="chg {tag}" title="{tip}">{rate:+.0f}%</td>'


def render_mind(rep):
    """莊家意圖卡：兩腳判定 → 四象限結論 → 台指區間與失效條件。

    資料一過期整張卡就會被 JS 加上 .expired 轉灰並蓋上警語（見 AGE_JS）。
    表格慢 15 分鐘還能看，方向判讀慢 15 分鐘會害人做反 —— 兩者不該同樣對待。
    """
    m = rep["mind"]
    if not m:
        return ""

    def leg(side, st, label):
        if st["stance"] is None:
            body = f'<span class="lgn">量能／樣本不足（{st["vol"]:,} 口 / {st["n"]} 檔），不判定</span>'
        elif not st["stance"]:
            body = (f'<b class="mid">中性</b>'
                    f'<span class="lgn">{st["score"]:+.1f}pt，未達 ±{st["gate"]:.1f} 門檻</span>')
        else:
            code = LEG[(side, st["stance"])]
            body = (f'<b class="{st["stance"]}">{code}</b>'
                    f'<span class="lgd">{LEG_TXT[code]}</span>'
                    f'<span class="lgn">{st["score"]:+.1f}pt・最大量 {st["top"]:,}</span>')
        return f'<div class="leg"><span class="lgl">{label}</span>{body}</div>'

    def kx(v, src):
        return f'{v:,}<small>{src}</small>' if v else "—"

    rng = ""
    if m["hi"] or m["lo"]:
        rng = (f'<div class="mrange">台指參考區間　'
               f'<b>{kx(m["lo"], m["lo_src"])}</b> ～ <b>{kx(m["hi"], m["hi_src"])}</b></div>')
    shift = (f'<div class="mshift">{"　·　".join(m["shifts"])}</div>') if m["shifts"] else ""

    return f'''<div class="mind {m["code"]}">
  <div class="mh">莊家意圖<span class="mq">{m["title"]}</span></div>
  <div class="mlegs">
    {leg("C", m["c"], "標的之上・買權")}
    {leg("P", m["p"], "標的之下・賣權")}
  </div>
  <div class="mhow">{m["how"]}</div>
  {rng}{shift}
  <div class="mnote">失效條件：{m["bad"]}</div>
  <div class="mexp">⚠ 資料已超過 15 分鐘，方向判讀不可用 —— 到 Actions 手動 Run 一次再看</div>
</div>'''


def render_zone(rep):
    """盤中撐壓卡片：今日成交金額分布抓出來的牆，並與昨日 OI 牆對照。"""
    z = rep["zone"]
    if not z:
        return ""

    def rows(items, thin, vol):
        if thin:
            return (f'<div class="zrow thin"><span class="zm">量能不足（此側價外合計 {vol:,} 口），'
                    f'不做撐壓判讀</span></div>')
        out = []
        for r in items:
            tag = r.get("tag", "")
            rt  = f'{r["rate"]:+.0f}%' if r.get("rate") is not None else "—"
            out.append(
                f'<div class="zrow"><b>{r["K"]:,}</b>'
                f'<span class="zm">{r["vol"]:,} 口</span>'
                f'<span class="zv">{money(r["amt"])}</span>'
                f'<span class="zr {tag}">{rt}</span>'
                f'<span class="zt {tag}">{TAG_TXT[tag]}</span></div>')
        return "\n".join(out) or '<div class="zrow"><span class="zm">此側無成交</span></div>'

    def note(shift, wall, label):
        # 這裡的 OI 牆已限定在標的同一側，跟盤中重心是可比的範圍
        if wall is None:
            return f'昨日 {label} OI 牆：視窗內無資料'
        if shift is None:
            return f'昨日 {label} OI 牆 {wall:,}'
        if abs(shift) <= 100:
            return f'昨日 {label} OI 牆 {wall:,}　·　盤中重心與其一致'
        d = "上移" if shift > 0 else "下移"
        return f'昨日 {label} OI 牆 {wall:,}　·　盤中重心{d} {abs(shift):,} 點'

    return f'''<div class="zones">
  <div class="zone res">
    <div class="zh">盤中壓力區<small>標的之上・買權今日成交口數</small></div>
    {rows(z["res"], z["res_thin"], z["res_vol"])}
    <div class="zn">{note(z["res_shift"], z["c_wall"], "買權")}</div>
  </div>
  <div class="zone sup">
    <div class="zh">盤中支撐區<small>標的之下・賣權今日成交口數</small></div>
    {rows(z["sup"], z["sup_thin"], z["sup_vol"])}
    <div class="zn">{note(z["sup_shift"], z["p_wall"], "賣權")}</div>
  </div>
</div>'''


def render_panel(rep):
    """單一到期別（一個分頁）的 KPI + T 字表 + 說明。"""
    tid = rep["id"]
    crows, prows = rep["crows"], rep["prows"]
    cmax  = max((r["amt"] for r in crows.values()), default=1)
    pmax  = max((r["amt"] for r in prows.values()), default=1)
    cvmax = max((r["vol"] for r in crows.values()), default=1)
    pvmax = max((r["vol"] for r in prows.values()), default=1)
    c_vol = sum(r["vol"] for r in crows.values())
    p_vol = sum(r["vol"] for r in prows.values())
    c_amt = sum(r["amt"] for r in crows.values())
    p_amt = sum(r["amt"] for r in prows.values())
    pcr_v = (p_vol / c_vol) if c_vol else 0
    c_top = max(crows.values(), key=lambda r: r["vol"], default=None)
    p_top = max(prows.values(), key=lambda r: r["vol"], default=None)

    oi = rep["oi"]
    c_wall = p_wall = pcr_oi = oi_date = None
    if oi:
        c_oi_all = oi["C"]; p_oi_all = oi["P"]
        if c_oi_all: c_wall = max(c_oi_all, key=lambda k: c_oi_all[k])
        if p_oi_all: p_wall = max(p_oi_all, key=lambda k: p_oi_all[k])
        tc = sum(c_oi_all.values()); tp = sum(p_oi_all.values())
        pcr_oi = (tp / tc) if tc else None
        oi_date = oi["date"].strftime("%m/%d")

    def fmt(n): return f"{n:,}"

    trs = []
    for k in rep["strikes"]:
        c = crows.get(k); p = prows.get(k)
        atm_cls = " atm" if k == rep["atm"] else ""
        c_oiv = oi["C"].get(k) if oi else None
        p_oiv = oi["P"].get(k) if oi else None
        if c:
            cc = (f'<td class="amt" data-tab="{tid}" data-side="C" data-k="{k}" data-amt="{c["amt"]}" '
                  f'style="background:{heat(c["amt"], cmax, CALL_BASE)}">'
                  f'<span class="amtnum">{fmt(c["amt"])}</span><span class="delta"></span></td>'
                  f'<td class="vol" style="{bar(c["vol"], cvmax, CALL_BASE, True)}">{fmt(c["vol"])}</td>'
                  f'<td class="oi">{fmt(c_oiv) if c_oiv else ""}</td>'
                  f'{chg_td(c)}'
                  f'<td class="px" data-tab="{tid}" data-side="C" data-k="{k}" data-px="{c["px"]:g}">'
                  f'<span class="pxnum">{c["px"]:g}</span><span class="delta plain"></span></td>'
                  f'<td class="be">{c["be"]:,.0f}</td>')
        else:
            cc = '<td class="e"></td>'*6
        if p:
            pc = (f'<td class="be">{p["be"]:,.0f}</td>'
                  f'<td class="px" data-tab="{tid}" data-side="P" data-k="{k}" data-px="{p["px"]:g}">'
                  f'<span class="pxnum">{p["px"]:g}</span><span class="delta plain"></span></td>'
                  f'{chg_td(p)}'
                  f'<td class="oi">{fmt(p_oiv) if p_oiv else ""}</td>'
                  f'<td class="vol" style="{bar(p["vol"], pvmax, PUT_BASE, False)}">{fmt(p["vol"])}</td>'
                  f'<td class="amt" data-tab="{tid}" data-side="P" data-k="{k}" data-amt="{p["amt"]}" '
                  f'style="background:{heat(p["amt"], pmax, PUT_BASE)}">'
                  f'<span class="amtnum">{fmt(p["amt"])}</span><span class="delta"></span></td>')
        else:
            pc = '<td class="e"></td>'*6
        trs.append(f'<tr class="drow{atm_cls}">{cc}<td class="strike">{k:,}</td>{pc}</tr>')
    rows_html = "\n".join(trs)

    e = rep["expiry"]
    exp_txt = f'{e[4:6]}/{e[6:8]} 到期（{rep["series"]}）'
    live = rep["session"] != "非交易" and not rep["stale"]
    time_txt = f'行情時間 {rep["time"]}' if live else f'最後成交 {rep["time"]}'
    if rep["stale"]:
        time_txt = f'⚠ 非今日行情　最後成交 {rep["time"]}'

    oi_note = ""
    if oi:
        # 標出 OI 是哪一個到期別，免得跟畫面上的週選搞混
        if oi["exact"]:
            oe = oi["exp"]
            scope = f'{oe[4:6]}/{oe[6:8]} 到期'
        else:
            scope = '⚠ 同月各週合計（對不到單一到期日）'
        oi_note = (f'未平倉牆（{oi_date} 收盤・{scope}）：買權壓力 <b>{c_wall:,}</b>、'
                   f'賣權支撐 <b>{p_wall:,}</b>'
                   f'{"、Put/Call 未平倉比 <b>%.2f</b>" % pcr_oi if pcr_oi else ""}。')
    else:
        oi_note = "未平倉牆：暫無前一日 OI 資料。"

    ctop_txt = f'{c_top["K"]:,}（{c_top["vol"]:,} 口）' if c_top else "-"
    ptop_txt = f'{p_top["K"]:,}（{p_top["vol"]:,} 口）' if p_top else "-"
    pcr_oi_kpi = f"{pcr_oi:.2f}" if pcr_oi else "—"

    return f'''<section class="panel" data-panel="{tid}">
<div class="sub sub-tab">{exp_txt}　·　{time_txt}　·　本到期別成交 {rep["vol"]:,} 口</div>
<div class="kpis">
  <div class="kpi call"><div class="l">CALL 成交量</div><div class="v">{c_vol:,}<small> 口</small></div></div>
  <div class="kpi put"><div class="l">PUT 成交量</div><div class="v">{p_vol:,}<small> 口</small></div></div>
  <div class="kpi"><div class="l">Put/Call 量比</div><div class="v">{pcr_v:.2f}</div></div>
  <div class="kpi"><div class="l">P/C 未平倉比</div><div class="v">{pcr_oi_kpi}</div></div>
  <div class="kpi"><div class="l">價平</div><div class="v">{rep["atm"]:,}</div></div>
</div>
{render_mind(rep)}
{render_zone(rep)}
<div class="tblwrap">
<table>
<thead><tr>
  <th class="grp-c">CALL 金額</th><th class="grp-c">口數</th><th class="grp-c">OI</th><th class="grp-c">今日</th><th class="grp-c">權利金</th><th class="grp-c">損益兩平</th>
  <th>履約價</th>
  <th class="grp-p">損益兩平</th><th class="grp-p">權利金</th><th class="grp-p">今日</th><th class="grp-p">OI</th><th class="grp-p">口數</th><th class="grp-p">PUT 金額</th>
</tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
</div>
<div class="note">
  <b>本頁重點</b>：買權最大量 {ctop_txt}、賣權最大量 {ptop_txt}。{oi_note}
</div>
</section>'''


# 分頁切換：按鈕控制哪一個 panel 顯示，選擇存 localStorage，
# 這樣每 60 秒自動重整回來時還停在原本看的那一頁。
DUOKONG_JS = """
<script>
/* 盤中多空空間卡：跟 Cloudflare Worker 要一份 KV 快照，在瀏覽器端算兩條線並畫圖。
   為什麼放前端：掃全市場一輪 20 秒，本頁的請求扛不住（見 4.9 節註解）。
   抓不到就整張卡不顯示 —— 頁面最上面留一個空框比沒有這張卡更糟。 */
(function(){
  var API  = '__URL__/api/day';
  var 快   = 6;      // 黃線：多空空間 − 前 6 根（30 分鐘）均值
  var 慢   = 20;     // 藍線：多空空間 − 前 20 根（100 分鐘）均值
  var 階   = 150;    // 藍線每 150 家跳一階，夾在 ±200
  var 全格 = 54;     // 09:00~13:30 共 54 格。x 軸永遠鋪滿一整天，
                     // 盤中資料一根根長出來時比例才不會一直跳動。

  var box = document.getElementById('dk'), cv = document.getElementById('dkcv');
  if(!box || !cv) return;
  var rows = [], 布局 = null;

  function 色(k){ return getComputedStyle(document.documentElement).getPropertyValue(k).trim(); }
  function 簽(v){ return (v > 0 ? '+' : '') + Math.round(v).toLocaleString('en-US'); }

  /* 家數 − 前 k 根（含當根）移動平均。去趨勢後線講的是動能不是水位。 */
  function 離均差(s, k){
    return s.map(function(_, i){
      var seg = s.slice(Math.max(0, i - k + 1), i + 1);
      var m = 0; seg.forEach(function(v){ m += v; });
      return s[i] - m / seg.length;
    });
  }

  function 算三線(rs){
    var s = rs.map(function(r){ return r.s; });
    var f = 離均差(s, 快), w = 離均差(s, 慢);
    rs.forEach(function(r, i){
      r.y = Math.round(f[i]);
      r.b = Math.max(-2, Math.min(2, Math.round(w[i] / 階))) * 100;
    });
    return rs;
  }

  function 畫(){
    var dpr = window.devicePixelRatio || 1;
    var W = cv.parentNode.clientWidth || 320, H = 152;
    cv.width = W * dpr; cv.height = H * dpr;
    var c = cv.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, W, H);
    if(!rows.length) return;

    var L = 36, R = 6, T = 8, B = 18, pw = W - L - R, ph = H - T - B;
    var vals = [0];
    rows.forEach(function(r){ vals.push(r.s, r.y, r.b); });
    var mx = Math.max.apply(null, vals), mn = Math.min.apply(null, vals);
    var pad = (mx - mn) * 0.08 || 50; mx += pad; mn -= pad;
    var step = pw / 全格;
    var X = function(i){ return L + step * (i + 0.5); };
    var Y = function(v){ return T + ph * (mx - v) / (mx - mn); };
    布局 = { L: L, step: step, X: X };

    c.strokeStyle = 色('--line'); c.lineWidth = 1;
    [200, -200].forEach(function(v){
      if(v > mx || v < mn) return;
      c.setLineDash([4, 4]); c.beginPath();
      c.moveTo(L, Y(v)); c.lineTo(W - R, Y(v)); c.stroke(); c.setLineDash([]);
    });

    var bw = Math.max(step * 0.62, 1.6);
    rows.forEach(function(r, i){
      c.fillStyle = r.s >= 0 ? 色('--call') : 色('--put');
      var y0 = Y(0), y1 = Y(r.s);
      c.fillRect(X(i) - bw / 2, Math.min(y0, y1), bw, Math.max(Math.abs(y1 - y0), 1));
    });

    c.strokeStyle = 色('--ink'); c.lineWidth = 1.2;
    c.beginPath(); c.moveTo(L, Y(0)); c.lineTo(W - R, Y(0)); c.stroke();

    c.strokeStyle = '#d8a13a'; c.lineWidth = 2; c.lineJoin = 'round'; c.beginPath();
    rows.forEach(function(r, i){ i ? c.lineTo(X(i), Y(r.y)) : c.moveTo(X(i), Y(r.y)); });
    c.stroke();

    /* 藍線畫成階梯：它本來就是離散的四階，畫成斜線會看起來像連續值。 */
    c.strokeStyle = '#4a8fe0'; c.lineWidth = 2.4; c.beginPath();
    rows.forEach(function(r, i){
      var x0 = X(i) - step / 2, x1 = X(i) + step / 2, yy = Y(r.b);
      i ? c.lineTo(x0, yy) : c.moveTo(x0, yy);
      c.lineTo(x1, yy);
    });
    c.stroke();

    c.fillStyle = 色('--muted'); c.font = '10px -apple-system'; c.textAlign = 'right';
    [mx, 0, mn].forEach(function(v){ c.fillText(Math.round(v), L - 5, Y(v) + 3); });
    c.textAlign = 'center';
    for(var m = 540, i = 0; m <= 805; m += 5, i++){
      if(i % 6) continue;
      var lab = ('0' + Math.floor(m / 60)).slice(-2) + ':' + ('0' + (m % 60)).slice(-2);
      c.fillText(lab, L + step * (i + 0.5), H - 5);
    }
  }

  function 台北日(){
    var t = new Date(Date.now() + (new Date().getTimezoneOffset() * 60000) + 8 * 3600000);
    return '' + t.getFullYear() + ('0' + (t.getMonth() + 1)).slice(-2) + ('0' + t.getDate()).slice(-2);
  }

  function 渲染(day){
    rows = 算三線((day && day.rows) || []);
    if(!rows.length) return;                    // 開盤前／連假：整張卡不出現
    box.hidden = false;

    var 末 = rows[rows.length - 1];
    box.className = 'dk ' + (末.s >= 0 ? 'up' : 'down');
    document.getElementById('dkq').textContent = 簽(末.s);

    var d = day.date || '';
    var 當天 = (d === 台北日());
    document.getElementById('dkd').textContent =
      d.slice(4, 6) + '/' + d.slice(6) + ' ' + (末.t || '') +
      (day.updated ? '（更新 ' + day.updated + '）' : '') +
      (當天 ? '' : '　最近交易日');

    var hi = 0, lo = 0, 紅 = 0, 綠 = 0;
    rows.forEach(function(r, i){
      if(r.s > rows[hi].s) hi = i;
      if(r.s < rows[lo].s) lo = i;
      if(r.s > 0) 紅++; else if(r.s < 0) 綠++;
    });
    document.getElementById('dkfact').innerHTML =
      '日內最強 <b>' + rows[hi].t + ' ' + 簽(rows[hi].s) + '</b>　最弱 <b>' + rows[lo].t + ' ' +
      簽(rows[lo].s) + '</b>　紅棒 <b>' + 紅 + '</b> 根／綠棒 <b>' + 綠 + '</b> 根　掃描池 ' +
      (day.pool || '?') + ' 檔';

    function 格(k, v, cls){
      return '<div class="dks"><span class="l">' + k + '</span><b class="' + (cls || '') +
             '">' + v + '</b></div>';
    }
    document.getElementById('dkkpi').innerHTML =
      格('多空空間', 簽(末.s), 末.s >= 0 ? 'up' : 'down') +
      格('上漲 / 下跌', 末.up + ' / ' + 末.dn, 'dim') +
      格('快動能', 簽(末.y), 末.y >= 0 ? 'up' : 'down') +
      格('慢動能', 簽(末.b), 末.b >= 0 ? 'up' : 'down') +
      格('這 5 分成交', (末.amt || 0).toLocaleString('en-US') + ' 億', 'dim');

    畫();
  }

  /* 點圖看某一根的數字。手機上沒有 hover，點擊是唯一能讀出單根數值的方式。 */
  cv.addEventListener('click', function(e){
    if(!布局 || !rows.length) return;
    var r = cv.getBoundingClientRect();
    var i = Math.round((e.clientX - r.left - 布局.L) / 布局.step - 0.5);
    var row = rows[i];
    if(!row) return;
    document.getElementById('dkfact').innerHTML =
      '<b>' + row.t + '</b>　多空空間 <b>' + 簽(row.s) + '</b>（漲 ' + row.up + ' / 跌 ' + row.dn +
      '）　快動能 <b>' + 簽(row.y) + '</b>　慢動能 <b>' + 簽(row.b) + '</b>　這 5 分 ' +
      (row.amt || 0).toLocaleString('en-US') + ' 億';
  });

  function 載入(){
    fetch(API, { cache: 'no-store' })
      .then(function(r){ return r.json(); })
      .then(渲染)
      .catch(function(){});                     // 讀不到就維持隱藏，不影響選擇權主表
  }

  載入();
  /* 本頁 60 秒會整頁重整，但使用者常常把頁面切到背景，重整不一定準時發生；
     卡片自己也每 60 秒重抓一次，圖才會真的自己往前長。 */
  setInterval(載入, 60000);
  window.addEventListener('resize', 畫);
})();
</script>
""".replace("__URL__", DUOKONG_URL)


TAB_JS = """
<script>
(function(){
  var btns = [].slice.call(document.querySelectorAll('.tab'));
  if(!btns.length) return;
  var panels = [].slice.call(document.querySelectorAll('.panel'));
  function show(id){
    btns.forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-tab') === id); });
    panels.forEach(function(p){ p.classList.toggle('on', p.getAttribute('data-panel') === id); });
    try { localStorage.setItem('txo_tab', id); } catch(e){}
  }
  var ids = btns.map(function(b){ return b.getAttribute('data-tab'); });
  var saved = null;
  try { saved = localStorage.getItem('txo_tab'); } catch(e){}
  show(ids.indexOf(saved) >= 0 ? saved : ids[0]);
  btns.forEach(function(b){
    b.addEventListener('click', function(){ show(b.getAttribute('data-tab')); });
  });
})();
</script>
"""


def render_html(page):
    """整頁：共用表頭 + 各結算日分頁。"""
    reps = page["reps"]
    live = page["session"] != "非交易" and not any(r["stale"] for r in reps)
    dot = "#e0392b" if live else "#9a9790"
    sess_txt = page["session"] if live else f'{page["session"]}（顯示最後成交價）'

    tabs = []
    for r in reps:
        e = r["expiry"]
        tabs.append(f'<button class="tab" data-tab="{r["id"]}">{r["tab"]}'
                    f'<small>{e[4:6]}/{e[6:8]} {r["series"]}</small></button>')
    tabs_html   = "\n  ".join(tabs)
    panels_html = "\n".join(render_panel(r) for r in reps)

    return f'''<meta charset="utf-8">
<title>台指選擇權即時 T 字報價</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<!-- 加到 iPhone 主畫面後全螢幕開啟（沒有這行會一直帶著 Safari 的網址列與工具列）。
     status bar 設成透明是因為本頁 light/dark 自動切換：設死 black 或 default
     一定會有一種模式下狀態列跟頁面撞色，透明才能讓 --bg 直接透上來。
     代價是內容會延伸到瀏海底下，靠下面 .wrap 的 safe-area-inset 補回來。 -->
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="台指T字">
<meta name="theme-color" content="#17181a">
<!-- /icon.png 由 Vercel 的 api/icon.py 服務。排程版（Cloudflare）沒有這個路徑會 404，
     iOS 就退回用網頁截圖當圖示——備援頁不需要漂亮圖示，故意不為它多改 workflow。 -->
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="/icon.png">
<style>
:root{{--bg:#f7f6f3;--panel:#fff;--ink:#1c1b19;--muted:#6b6862;--line:#e7e4dd;
  --call:#c0392b;--put:#1e7a3c;--atm:#fff6d8;--hair:#efece5;}}
@media(prefers-color-scheme:dark){{:root{{--bg:#17181a;--panel:#1f2124;--ink:#ececec;
  --muted:#9a9790;--line:#2e3033;--call:#ff6b5c;--put:#54c777;--atm:#3a3418;--hair:#26282b;}}}}
:root[data-theme=dark]{{--bg:#17181a;--panel:#1f2124;--ink:#ececec;--muted:#9a9790;
  --line:#2e3033;--call:#ff6b5c;--put:#54c777;--atm:#3a3418;--hair:#26282b;}}
:root[data-theme=light]{{--bg:#f7f6f3;--panel:#fff;--ink:#1c1b19;--muted:#6b6862;
  --line:#e7e4dd;--call:#c0392b;--put:#1e7a3c;--atm:#fff6d8;--hair:#efece5;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,"PingFang TC","Helvetica Neue",Arial,sans-serif;
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;}}
.wrap{{max-width:1080px;margin:0 auto;
  padding:calc(24px + env(safe-area-inset-top)) calc(14px + env(safe-area-inset-right))
          calc(60px + env(safe-area-inset-bottom)) calc(14px + env(safe-area-inset-left));}}
h1{{font-size:20px;margin:0 0 4px;font-weight:700;letter-spacing:.3px}}
.sub{{color:var(--muted);font-size:12.5px;margin-bottom:6px;line-height:1.6}}
/* 資料新鮮度：雲端排程常被 GitHub 跳過，這裡讓「這份資料多舊」一眼可見 */
#age{{font-weight:600}}
#age.stale{{color:#d8b24a}}
#age.dead{{color:#ff6a5c}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:{dot};margin-right:6px;
  vertical-align:middle;animation:pulse 1.6s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
@media(prefers-reduced-motion:reduce){{.dot{{animation:none}}}}
.kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;margin:14px 0}}
@media(max-width:640px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.kpi .l{{font-size:10.5px;color:var(--muted);letter-spacing:.4px;margin-bottom:4px}}
.kpi .v{{font-size:18px;font-weight:700}} .kpi .v small{{font-size:11px;font-weight:500;color:var(--muted)}}
.kpi.call .v{{color:var(--call)}} .kpi.put .v{{color:var(--put)}}
/* 莊家意圖卡：四象限結論。過期時整張轉灰並蓋警語，見 AGE_JS */
.mind{{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--muted);
  border-radius:10px;padding:12px 14px;margin:0 0 10px}}
.mind.bull{{border-left-color:var(--call)}} .mind.bear{{border-left-color:var(--put)}}
.mind.range,.mind.vol{{border-left-color:#d8b24a}}
.mh{{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.4px}}
.mq{{color:var(--ink);font-size:16px;margin-left:9px;letter-spacing:0}}
.mind.bull .mq{{color:var(--call)}} .mind.bear .mq{{color:var(--put)}}
.mind.range .mq,.mind.vol .mq{{color:#c99a1e}}
.mlegs{{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;margin:9px 0 8px}}
@media(max-width:640px){{.mlegs{{grid-template-columns:1fr}}}}
.leg{{display:flex;align-items:baseline;gap:7px;font-size:12px;flex-wrap:wrap}}
.lgl{{color:var(--muted);font-size:10.5px;min-width:88px}}
.leg b{{font-size:13px;font-weight:800;letter-spacing:.5px}}
.leg b.sell{{color:var(--put)}} .leg b.buy{{color:var(--call)}} .leg b.mid{{color:var(--muted)}}
.lgd{{font-size:11.5px}} .lgn{{color:var(--muted);font-size:10.5px}}
.mhow{{font-size:12.5px;line-height:1.7;padding-top:8px;border-top:1px solid var(--hair)}}
.mrange{{font-size:12.5px;margin-top:7px}} .mrange b{{font-size:15px}}
.mrange small{{font-size:9.5px;color:var(--muted);font-weight:500;margin-left:3px}}
.mshift{{font-size:11px;color:var(--muted);margin-top:5px}}
.mnote{{color:var(--muted);font-size:10.5px;margin-top:8px;padding-top:7px;
  border-top:1px solid var(--hair);line-height:1.6}}
.mexp{{display:none;font-size:11.5px;font-weight:700;color:#ff6a5c;
  margin-top:8px;padding-top:7px;border-top:1px solid var(--hair)}}
.mind.expired{{border-left-color:var(--muted)!important}}
.mind.expired .mlegs,.mind.expired .mhow,.mind.expired .mrange,
.mind.expired .mshift,.mind.expired .mnote,.mind.expired .mq{{
  filter:grayscale(1);opacity:.32}}
.mind.expired .mexp{{display:block}}
.zones{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:0 0 14px}}
@media(max-width:640px){{.zones{{grid-template-columns:1fr}}}}
.zone{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}}
.zone.res{{border-left:3px solid var(--call)}} .zone.sup{{border-left:3px solid var(--put)}}
.zh{{font-size:12px;font-weight:700;margin-bottom:7px}}
.zh small{{font-weight:500;color:var(--muted);font-size:10.5px;margin-left:7px}}
.zrow{{display:flex;align-items:baseline;gap:8px;font-size:12px;padding:3px 0;flex-wrap:wrap}}
.zrow b{{font-size:14px;min-width:58px}}
.zm{{color:var(--ink);min-width:56px}} .zv,.zr,.zt{{color:var(--muted);font-size:11px}}
.zr.sell,.zt.sell{{color:var(--put)}} .zr.buy,.zt.buy{{color:var(--call)}}
.zt{{margin-left:auto;font-weight:600}}
.zrow.thin .zm{{color:var(--muted);font-size:11.5px}}
.zn{{color:var(--muted);font-size:10.5px;margin-top:7px;padding-top:7px;border-top:1px solid var(--hair)}}
.tblwrap{{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;min-width:960px}}
thead th{{position:sticky;top:0;background:var(--panel);color:var(--muted);font-weight:600;
  font-size:10.5px;letter-spacing:.3px;padding:8px 7px;border-bottom:2px solid var(--line)}}
.grp-c{{color:var(--call)}} .grp-p{{color:var(--put)}}
.drow td{{padding:4px 7px;border-bottom:1px solid var(--hair);text-align:right;white-space:nowrap}}
.strike{{text-align:center!important;font-weight:700;background:var(--bg);
  border-left:1px solid var(--line);border-right:1px solid var(--line)}}
.be,.oi{{color:var(--muted)}} .px{{font-weight:600}}
.chg{{font-size:11px;color:var(--muted)}}
.chg.sell{{color:var(--put);font-weight:700;background:rgba(30,160,70,.13)}}
.chg.buy{{color:var(--call);font-weight:700;background:rgba(214,52,52,.13)}}
.amt .amtnum{{display:block}} .px .pxnum{{display:block}}
.delta{{display:none;font-size:9.5px;font-weight:700;line-height:1.4;margin-top:1px;
  padding:0 4px;border-radius:3px;background:rgba(0,0,0,.34);letter-spacing:.2px}}
.delta.plain{{background:transparent;padding:0}}
.delta.plain.up{{color:var(--call)}} .delta.plain.down{{color:var(--put)}}
.delta.up{{color:#ff6a5c}} .delta.down{{color:#37d67a}}
.e{{background:transparent!important}}
.drow.atm .strike{{background:var(--atm)}}
.drow.atm td{{border-top:1px solid #d8b24a;border-bottom:1px solid #d8b24a}}
.note{{color:var(--muted);font-size:11.5px;line-height:1.75;margin-top:16px}}
.note b{{color:var(--ink)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:10px 2px 0}}
.sw{{display:inline-block;width:24px;height:10px;border-radius:2px;vertical-align:middle;margin-right:5px}}
.tabs{{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 10px}}
.tab{{appearance:none;font:inherit;font-size:13px;font-weight:700;cursor:pointer;
  background:var(--panel);color:var(--muted);border:1px solid var(--line);
  border-radius:999px;padding:8px 16px;line-height:1.3}}
.tab small{{display:block;font-size:10.5px;font-weight:500;opacity:.8;margin-top:2px}}
.tab.on{{background:var(--ink);color:var(--bg);border-color:var(--ink)}}
.sub-tab{{margin:0 2px 4px}}
.panel{{display:none}} .panel.on{{display:block}}
/* 盤中多空空間卡：全市場家數的三條線。放在分頁之外，因為它跟到期別無關，
   三個分頁看到的都該是同一份。內容全由 DUOKONG_JS 在瀏覽器端填（見 4.9 節註解）。 */
.dk{{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--muted);
  border-radius:10px;padding:12px 14px;margin:12px 0 4px}}
.dk.up{{border-left-color:var(--call)}} .dk.down{{border-left-color:var(--put)}}
.dkh{{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.4px;
  display:flex;align-items:baseline;flex-wrap:wrap;gap:0 8px}}
.dkh small{{font-size:10px;font-weight:500;opacity:.75;margin-left:-4px}}
.dkq{{color:var(--ink);font-size:17px;letter-spacing:0}}
.dk.up .dkq{{color:var(--call)}} .dk.down .dkq{{color:var(--put)}}
.dkd{{font-size:10.5px;font-weight:500;color:var(--muted)}}
.dka{{margin-left:auto;font-size:11px;font-weight:600;color:var(--muted);text-decoration:none;
  border:1px solid var(--line);border-radius:999px;padding:2px 9px;white-space:nowrap}}
.dka:hover{{color:var(--ink);border-color:var(--muted)}}
/* 圖高度寫死 152px：手機直放時再高就把下面的表格擠出畫面外，
   而這張卡的角色是「一眼看今天的骨架」，不是拿來細看的主圖。 */
.dkwrap{{position:relative;height:152px;margin:8px 0 2px}}
#dkcv{{width:100%;height:152px;display:block;touch-action:manipulation}}
.dkfact{{font-size:11.5px;color:var(--muted);line-height:1.7;min-height:20px}}
.dkfact b{{color:var(--ink);font-weight:600}}
/* 數字列刻意不用頁面上那組 .kpi 方塊：這張卡是最上面的附掛讀數，
   五個方塊在手機上要吃掉三列高度，會把主角（T 字表）擠出第一屏。 */
.dkkpis{{display:flex;flex-wrap:wrap;gap:5px 18px;margin:9px 0 0;
  padding-top:8px;border-top:1px solid var(--hair)}}
.dks{{display:flex;align-items:baseline;gap:6px}}
.dks .l{{font-size:10.5px;color:var(--muted)}}
.dks b{{font-size:15px;font-weight:700}}
.dks b.up{{color:var(--call)}} .dks b.down{{color:var(--put)}}
.dks b.dim{{color:var(--ink);font-weight:600;font-size:14px}}
.dknote{{color:var(--muted);font-size:10.5px;margin-top:9px;padding-top:7px;
  border-top:1px solid var(--hair);line-height:1.65}}
</style>
<div class="wrap" data-gen="{page["now"]}" data-epoch="{page["epoch"]}">
<h1>台指選擇權即時 T 字報價</h1>
<div class="sub"><span class="dot"></span>{sess_txt}　·　標的 {page["under"]:,.0f}（{page["usrc"]}）　·　產生 {page["now"]}　·　<span id="age">—</span></div>
{render_duokong()}
<div class="tabs">
  {tabs_html}
</div>
{panels_html}
<div class="legend">
  <span><span class="sw" style="background:linear-gradient(90deg,transparent,rgba(214,52,52,.42))"></span>買權口數分布（由右往左）</span>
  <span><span class="sw" style="background:linear-gradient(90deg,rgba(30,160,70,.42),transparent)"></span>賣權口數分布（由左往右）</span>
  <span><span class="sw" style="background:linear-gradient(90deg,#fff,rgb(214,52,52))"></span>金額熱度</span>
  <span><span class="sw" style="background:var(--atm);border:1px solid #d8b24a"></span>價平</span>
  <span><b style="color:#ff6a5c">▲</b> 較上一版增加　<b style="color:#37d67a">▼</b> 較上一版減少（金額與權利金皆有）</span>
  <span>網頁每 60 秒自動重新整理</span>
</div>
<div class="note">
  <b>即時欄位</b>（MIS）：權利金、口數、金額、今日漲跌、損益兩平；金額 = 權利金 × 50 × 口數，為今日累積。<br>
  <b>盤中撐壓怎麼來的</b>：OI 盤中不更新，所以牆的位置改用<b>今日累積成交口數</b>抓 ——
  壓力取標的之上口數最大的買權、支撐取標的之下口數最大的賣權，範圍限在畫面的 ±1,500 點視窗內。
  用口數而非金額，是因為要跟 OI 牆對照，而 OI 的單位就是口數；而且金額 = 權利金×50×口數，
  權利金隨著接近價平而變大，用金額排幾乎必然選出離標的最近那一檔，那是價平的定義而非市場押注的位置。
  口數欄的分布條回答「牆有多高」，金額欄的熱度回答「押了多少錢」。
  一側價外合計不足 500 口時直接標示量能不足，不硬給價位。<br>
  <b>賣方築牆／買方追價</b>：直接看漲跌%會誤判 —— 指數一漲，全部買權一起漲、全部賣權一起跌，那是方向 beta。
  本表扣掉<b>同側所有履約價漲跌%的中位數</b>，剩下的「超額」才是這一檔相對同儕的異常強弱：
  跌得比同儕兇（綠）＝有人壓價收租，牆較硬；漲得比同儕兇（紅）＝有人追價，該價位可能被挑戰。
  顯著門檻用超額絕對值的中位數自適應，不是固定值。<br>
  <b>莊家意圖（四象限）</b>：給只做台指、不做選擇權的人用。把每一側價外檔位的超額做<b>量加權</b>
  壓成一個分數 —— 等權平均必然≈0（中位數的定義就保證一半在上一半在下），量加權問的才是
  「今天的錢押在被壓的檔還是被追的檔」。錢集中在被壓的檔＝賣方主導（買權側 SC、賣權側 SP），
  集中在被追的檔＝買方主導（BC／BP）。兩側交叉成四象限：SC+SP＝區間盤、BC+SP＝偏多、
  SC+BP＝偏空、BC+BP＝待變盤（買波動不買方向，等突破）。任一側中性或量能不足就顯示<b>觀望</b>，
  不硬湊結論。上下緣優先取盤中口數重心，沒有才退回昨日 OI 牆。
  <b>資料超過 15 分鐘整張卡會轉灰停用</b>，因為方向判讀對新鮮度的要求比表格高得多。<br>
  <b>牆在移動</b>：卡片下緣比對盤中金額重心與昨日 OI 牆。兩者背離超過 100 點，代表今天的資金押在別的價位，
  昨日那道支撐壓力已經不是同一個位置。<br>
  <b>分頁</b>：三個分頁是不同結算日的合約，照結算先後排（本週三＝W 系列或月選、
  本週五＝F 系列、下週三＝下一個週三到期的合約），各自獨立計算量比、價平與 ▲▼ 增減；
  切換後的選擇會記住，自動重整不會跳回去。<b>下週三</b>是還沒進入結算週的部位，
  量通常只有本週的零頭，牆的位置常是先卡好的初始陣地，看的是下一段的區間預期，
  不要拿來當今天的當沖依據；量能不足時撐壓會自動擋掉不顯示。<br>
  <b>盤中多空空間</b>（最上面那張卡）：全市場約 2,700 檔的即時報價壓成三條線 ——
  紅綠棒是<b>上漲家數 − 下跌家數</b>（狀態量），黃藍兩線是同一個數字減掉自己前 30 分鐘／
  前 100 分鐘的移動平均。<b>兩條線刻意做成去趨勢</b>：直接畫水位跟紅綠棒的相關性 r=+0.88，
  等於把同一件事畫兩次；減掉自己的均線之後，線講的才是「相對於剛才是在加速還是退潮」。
  藍線離散成 ±100／±200 四階，因為大型股本來就黏，階梯化之後「中期力道站哪邊」才一眼可讀。
  資料來自另一支常駐 Cloudflare 的 Worker（cron 每 5 分鐘掃一輪存進 KV），
  瀏覽器直接向它拿，所以<b>這張卡的新舊跟本頁的產生時間無關</b>，它自己每 60 秒重抓一次；
  Worker 掛掉時整張卡不顯示，不影響下面的選擇權報價。點卡片右上角可以進到完整版看型態判讀。
  台指選擇權讀的是「錢押在哪個價位」，這張卡讀的是「多少家公司站在多方」，兩者互為補充。<br>
  <b>限制</b>：OI 為期交所盤後公布，盤中沿用前一日；成交金額只知道成交，不知道那一口是新倉還是平倉，
  所以「築牆」是傾向推論而非事實。此表為 TAIFEX MIS 約每 5 秒的準即時報價，非逐筆。
</div>
</div>''' + TAB_JS + DELTA_JS + DUOKONG_JS


# ── 6. ntfy 推播 ─────────────────────────────────────────────────────────────

def load_ntfy_topic():
    t = os.environ.get("NTFY_TOPIC", "")
    if t:
        return t
    p = os.path.join(BASE_DIR, "ntfy_config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("topic", "")
    return ""


def push_ntfy(page, page_url=None):
    topic = load_ntfy_topic()
    if not topic:
        print("  ⚠ 無 ntfy topic，略過推播")
        return
    reps = page["reps"]
    lines = [f"標的 {page['under']:,.0f}　{page['session']} {reps[0]['time']}"
             + ("　⚠ 非今日行情" if any(r["stale"] for r in reps) else "")]
    # 推播沒有「上一版」可比，做不到網頁的 ▲▼；改帶絕對數字，
    # 讓手機上不開網頁也看得出牆在哪個履約價、押了多重。
    def wall(label, r):
        # 帶上今日漲跌%與買賣方研判，手機上不開網頁也看得出這道牆硬不硬
        rt = f"　今日 {r['rate']:+.0f}%（{TAG_TXT.get(r.get('tag',''),'')}）" if r.get("rate") is not None else ""
        return (f"{label} {r['K']:,}（{r['vol']:,}口）"
                f"　權利金 {r['px']:g}　金額 {money(r['amt'])}{rt}")
    for rep in reps:                      # 每個分頁各一段
        crows, prows = rep["crows"], rep["prows"]
        c_vol = sum(r["vol"] for r in crows.values())
        p_vol = sum(r["vol"] for r in prows.values())
        pcr_v = (p_vol / c_vol) if c_vol else 0
        e = rep["expiry"]
        z = rep["zone"]
        lines.append(f"── {rep['tab']} {e[4:6]}/{e[6:8]}（{rep['series']}）　價平 {rep['atm']:,}")
        lines.append(f"CALL {c_vol:,}口 / PUT {p_vol:,}口　P/C量比 {pcr_v:.2f}")
        # 只推盤中撐壓。以前另外推的「買權／賣權最大量」現在跟撐壓同樣以口數排序，
        # 幾乎必然是同一檔，徒增重複；更糟的是量能不足時撐壓已被擋掉，
        # 那兩行卻會把只有個位數口的雜訊從後門推出來。
        if z and (z["sup_k"] or z["res_k"]):
            sup = f"{z['sup_k']:,}" if z["sup_k"] else "—"
            res = f"{z['res_k']:,}" if z["res_k"] else "—"
            lines.append(f"盤中撐壓　支撐 {sup}　壓力 {res}")
        if z and z["res"] and not z["res_thin"]:
            lines.append(wall("盤中壓力", z["res"][0]))
        elif z and z["res_thin"]:
            lines.append(f"盤中壓力　量能不足（價外合計 {z['res_vol']:,} 口）")
        if z and z["sup"] and not z["sup_thin"]:
            lines.append(wall("盤中支撐", z["sup"][0]))
        elif z and z["sup_thin"]:
            lines.append(f"盤中支撐　量能不足（價外合計 {z['sup_vol']:,} 口）")
    body = "\n".join(lines)
    headers = {"Title": "選擇權即時 T 字報價".encode("utf-8"), "Tags": "chart_with_upwards_trend"}
    if page_url:
        headers["Click"] = page_url
    try:
        # 一定要看狀態碼。以前只送不看，ntfy 回 4xx/5xx（topic 打錯、被限流）
        # 也照印「推播成功」，手機收不到卻完全查不出來。
        r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers=headers, timeout=10)
        if r.status_code >= 400:
            print(f"  ⚠ ntfy 推播失敗：HTTP {r.status_code} {r.text[:200]}")
        else:
            print("  ✓ ntfy 推播成功")
    except Exception as e:
        print(f"  ⚠ ntfy 推播失敗：{e}")


# ── 6.5 四象限歷史紀錄 ───────────────────────────────────────────────────────
#
# 每次產頁就把每個分頁的結論追加一行。目的只有一個：先累積再驗證。
# 「四象限能不能預測隔日方向」現在沒有答案，跑滿 30 天有樣本才算得出來 ——
# 在那之前這張卡是參考，不是加碼部位的理由（發動候選那次的教訓）。
#
# 存在 --out 的同一個資料夾（雲端＝public/，會被 workflow 一起發佈到 gh-pages，
# 所以每次 Actions 跑完不會連同 runner 一起被丟掉）。

HIST_NAME = "莊家意圖歷史.csv"
HIST_COLS = ["產生時間", "epoch", "時段", "分頁", "到期日", "行情時間", "非今日行情", "標的",
             "買權側分數", "買權側主導", "買權側口數",
             "賣權側分數", "賣權側主導", "賣權側口數",
             "象限", "結論", "上緣", "上緣來源", "下緣", "下緣來源", "陣地位移"]


def _stance_txt(side, st):
    if st["stance"] is None:
        return "不判定"
    if not st["stance"]:
        return "中性"
    return LEG[(side, st["stance"])]


def append_history(page, path):
    """把這一輪的四象限結論追加進 CSV（每個分頁一行）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HIST_COLS)
        for rep_ in page["reps"]:
            m = rep_["mind"]
            c, p = m["c"], m["p"]
            w.writerow([
                page["now"], page["epoch"], page["session"], rep_["tab"],
                rep_["expiry"], rep_["time"], "Y" if rep_["stale"] else "",
                f'{page["under"]:.0f}',
                "" if c["score"] is None else f'{c["score"]:.2f}',
                _stance_txt("C", c), c["vol"],
                "" if p["score"] is None else f'{p["score"]:.2f}',
                _stance_txt("P", p), p["vol"],
                m["code"], m["title"],
                m["hi"] or "", m["hi_src"] or "", m["lo"] or "", m["lo_src"] or "",
                "；".join(m["shifts"]),
            ])
    print(f"  ✓ 四象限已記錄 {path}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "public", "index.html"),
                    help="HTML 輸出路徑（預設 public/index.html）")
    ap.add_argument("--radius", type=int, default=1500, help="顯示價平 ±N 點（預設 1500）")
    ap.add_argument("--notify", action="store_true", help="推播摘要到 ntfy")
    ap.add_argument("--track", action="store_true",
                    help="順便記錄『今日』欄的粉綠籌碼位置並判斷莊家處境"
                         "（追蹤_選擇權籌碼區.py；配 --notify 才會推事件）")
    ap.add_argument("--history", default="",
                    help=f"四象限歷史 CSV 路徑（預設 <--out 同目錄>/{HIST_NAME}；填 off 不記錄）")
    ap.add_argument("--page-url", default=os.environ.get("PAGE_URL", ""),
                    help="推播點擊要開的網頁網址（GitHub Pages 網址）")
    args = ap.parse_args()

    print(f"[{datetime.now(TW_TZ):%H:%M:%S}] 抓取 MIS 即時報價…")
    page = build_page(radius=args.radius)
    print(f"  時段 {page['session']}　標的 {page['under']:,.0f}（{page['usrc']}）")
    for rep in page["reps"]:
        oi = rep["oi"]
        oi_txt = "無前一日 OI" if not oi else (
            f"OI {oi['date']:%m/%d} 收盤・到期 {oi['exp']}"
            f"{'' if oi['exact'] else '（⚠ 同月合計，非單一到期日）'}")
        print(f"  [{rep['tab']}] {rep['series']}　到期 {rep['expiry']}　價平 {rep['atm']:,}　"
              f"行情時間 {rep['time']}{'　⚠ 非今日行情' if rep['stale'] else ''}")
        print(f"      CALL {len(rep['crows'])} 檔 / PUT {len(rep['prows'])} 檔　{oi_txt}")

    html = render_html(page)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ 已輸出 {args.out}")

    for rep in page["reps"]:
        m = rep["mind"]
        print(f'  [{rep["tab"]}] 莊家意圖：{m["title"]}　'
              f'買權側 {_stance_txt("C", m["c"])}／賣權側 {_stance_txt("P", m["p"])}　'
              f'區間 {m["lo"] or "—"} ～ {m["hi"] or "—"}')

    # 記錄失敗不該連累網頁 —— 網頁已經寫好了，這裡只是累積驗證用的樣本
    if args.history.lower() != "off":
        try:
            append_history(page, args.history or
                           os.path.join(os.path.dirname(os.path.abspath(args.out)), HIST_NAME))
        except Exception as e:
            print(f"  ⚠ 四象限歷史寫入失敗（不影響網頁）：{e}")

    if args.notify:
        push_ntfy(page, page_url=args.page_url or None)

    if args.track:
        # 同一份 page 直接餵給追蹤模組，不必重抓一次 MIS。
        # 追蹤失敗不該連累網頁 —— 網頁已經寫好了，這裡只是加值。
        try:
            import importlib.util
            tp = os.path.join(BASE_DIR, "追蹤_選擇權籌碼區.py")
            spec = importlib.util.spec_from_file_location("txo_track", tp)
            trk = importlib.util.module_from_spec(spec)
            sys.modules["txo_track"] = trk
            spec.loader.exec_module(trk)
            print("\n[籌碼區追蹤]")
            trk.track(page, notify=args.notify, page_url=args.page_url or None)
        except Exception as e:
            print(f"  ⚠ 籌碼區追蹤失敗（不影響網頁）：{e}")


if __name__ == "__main__":
    main()
