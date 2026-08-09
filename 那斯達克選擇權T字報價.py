# -*- coding: utf-8 -*-
"""
那斯達克選擇權T字報價.py

用 CBOE 免費延遲報價（延遲約 15 分鐘），產出那斯達克 100 指數選擇權（NDX）
T 字報價網頁（CALL 紅 / PUT 綠），格式沿用「即時選擇權T字報價.py」的台指版。

為什麼是 NDX 而不是 MNQ：小那（MNQ）的期貨選擇權只在 CME 交易，CME 官網對
自動抓取直接回 403 封鎖 IP，沒有免費公開來源。NDX 是那斯達克 100 指數本身的
選擇權，點位跟小那幾乎一比一對應（MNQ 就是追蹤同一個指數），算出來的支撐
壓力牆可以直接拿去看小那的價位。

網頁分成「最近到期」「本週五」兩個分頁，各取該到期日 OI 最大的商品別
（NDXP＝PM 結算的日選／週選、NDX＝AM 結算的月選／季選），量比、價平、
▲▼ 增減都各算各的。

即時欄位（CBOE 延遲 15 分）：權利金、成交量、成交金額、損益兩平、IV、Delta。
盤後欄位：未平倉 OI（前一交易日收盤，CBOE 隨報價一起給，不必另外下載）。

用法：
    python3 那斯達克選擇權T字報價.py                    # 產出 NDX選擇權T字報價.html
    python3 那斯達克選擇權T字報價.py --notify           # 產出網頁並推播 ntfy
    python3 那斯達克選擇權T字報價.py --radius 1200      # 顯示價平 ±N 點（預設 800）
    python3 那斯達克選擇權T字報價.py --symbol QQQ       # 改抓 QQQ ETF 選擇權
"""

import os
import re
import json
import argparse
from datetime import datetime, date, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TW_TZ    = ZoneInfo("Asia/Taipei")
ET_TZ    = ZoneInfo("America/New_York")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"

# CBOE 的檔名規則：指數要加底線前綴（_NDX、_SPX），ETF／個股不用（QQQ、AAPL）。
INDEX_SYMS = {"NDX", "SPX", "RUT", "VIX", "DJX", "XSP"}

# 契約乘數（每點多少美元）。NDX $100/點、QQQ 每口 100 股。
MULTIPLIER = {"NDX": 100, "SPX": 100, "QQQ": 100, "SPY": 100}

# OCC 符號：root + YYMMDD + C/P + 8 位履約價（單位 1/1000 美元）
OCC_PAT = re.compile(r'^([A-Z]+)(\d{6})([CP])(\d{8})$')

# 商品別中文說明：同一天到期的 NDX 與 NDXP 是兩種結算方式，不能混在一起算。
ROOT_DESC = {
    "NDX":  "AM結算（月/季選，結算日開盤價）",
    "NDXP": "PM結算（日選/週選，結算日收盤價）",
}


# ── 時段判斷 ────────────────────────────────────────────────────────────────

def current_session():
    """
    以美東時間判斷時段，回傳顯示用字串。
    NDX 指數選擇權正規交易時段 09:30–16:15 ET；台北時間換算約 21:30–04:15（夏令）。
    非交易時段沿用最後成交價，跟台指版的處理一致。
    """
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5:
        return "週末休市"
    hm = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= hm <= 16 * 60 + 15:
        return "美股盤中"
    if hm < 9 * 60 + 30:
        return "美股盤前"
    return "美股盤後"


def _f(v):
    """CBOE 用 0.0 表示「沒有這個值」（盤前 bid/ask/IV 全是 0），統一轉成 None。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v else None


# ── 1. CBOE 延遲報價 ─────────────────────────────────────────────────────────

def fetch_cboe(sym):
    """抓一次 CBOE 全鏈報價（NDX 全鏈約 7MB，含所有到期別、OI 與希臘字母）。"""
    key = f"_{sym}" if sym in INDEX_SYMS else sym
    r = requests.get(CBOE_URL.format(sym=key),
                     headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=60)
    r.raise_for_status()
    d = r.json()
    if "data" not in d:
        raise ValueError(f"CBOE 未回傳 {sym} 的報價資料")
    return d


def parse_expiry(yymmdd):
    """OCC 的 YYMMDD → date。"""
    return date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))


def collect_groups(data):
    """
    解析 OCC 符號（例 NDXP260807C29400000 = root NDXP / 到期 2026-08-07 / 買權 / 履約 29400）。
    以 (root, 到期日) 分群 = 同一到期別，回傳 {gkey: grp}。

    權利金取價的規則：有今日成交就用成交價，沒成交就退回買賣價中價。
    冷門檔的 last_trade_price 可能是好幾天前的殘值（實測有 08/04 的成交價混在
    08/07 的資料裡），拿它當今天的權利金會讓損益兩平與 parity 全部偏掉；
    中價至少是此刻做市商願意報的價。兩者都沒有就整檔跳過。
    """
    groups = defaultdict(lambda: {"C": {}, "P": {}, "vol": 0, "oi": 0, "last": ""})
    for it in data["data"]["options"]:
        m = OCC_PAT.match(it.get("option", ""))
        if not m:
            continue
        root, yymmdd, cp, kraw = m.groups()
        strike = int(kraw) / 1000.0
        strike = int(strike) if strike == int(strike) else strike

        vol  = int(_f(it.get("volume")) or 0)
        bid, ask = _f(it.get("bid")), _f(it.get("ask"))
        mid  = (bid + ask) / 2 if (bid and ask) else None
        last = _f(it.get("last_trade_price"))
        px   = last if vol > 0 else (mid or last)
        if px is None:
            continue

        ref  = _f(it.get("prev_day_close"))
        rate = ((px - ref) / ref * 100) if (ref and ref > 0) else None

        oi = int(_f(it.get("open_interest")) or 0)
        g = groups[(root, yymmdd)]
        g[cp][strike] = {
            "px": px, "vol": vol, "bid": bid, "ask": ask, "mid": mid, "oi": oi,
            "ref": ref, "rate": rate,
            "iv": _f(it.get("iv")), "delta": _f(it.get("delta")),
            # 沒有今日成交、只能拿中價頂替的檔位要標出來，撐壓判讀不採信它
            "quoted": vol == 0,
        }
        g["vol"] += vol
        g["oi"]  += oi
        t = it.get("last_trade_time") or ""
        if t > g["last"]:
            g["last"] = t

    if not groups:
        raise ValueError("CBOE 未回傳可解析的選擇權報價")
    return dict(groups)


def pick_expiry(groups, want, exclude=None):
    """
    挑指定到期日、OI 最大的那個商品別（同一天可能同時有 NDX 與 NDXP）。
    用 OI 而不是成交量：開盤前成交量全是 0，只有 OI 分得出哪個是主流商品。
    """
    cand = {g: v for g, v in groups.items()
            if parse_expiry(g[1]) == want and g != exclude and v["oi"] > 0}
    if not cand:
        return None
    gkey = max(cand, key=lambda g: cand[g]["oi"])
    return gkey, cand[gkey]


def next_expiries(groups):
    """
    回傳要顯示的兩個到期日：(最近到期, 本週五)。
    本週五若剛好就是最近到期（例如週四晚上看，最近到期是明天週五），
    第二個分頁改用下一個週五，否則兩頁會長得一模一樣。
    """
    today = datetime.now(ET_TZ).date()
    exps = sorted({parse_expiry(g[1]) for g, v in groups.items()
                   if parse_expiry(g[1]) >= today and v["oi"] > 0})
    if not exps:
        raise ValueError("沒有到期日在今天之後的合約")
    near = exps[0]

    fri = today + timedelta(days=(4 - today.weekday()) % 7)   # 本週五（今天就是週五則為今天）
    if fri < near or fri == near:
        fri += timedelta(days=7)
    # 那個週五不一定有掛牌（假日），取最接近且不早於它的到期日
    later = [e for e in exps if e >= fri]
    return near, (later[0] if later else None)


# ── 2. 撐壓與超額強弱（沿用台指版邏輯，門檻依 NDX 量能調整） ────────────────

def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def moneyness(k, under, is_call):
    """價內 in-the-money／價外 out-of-the-money。買權履約價低於標的為價內，賣權相反。"""
    return "itm" if ((k < under) if is_call else (k > under)) else "otm"


MONEY_TXT = {"itm": "價內", "otm": "價外"}
TAG_TXT   = {"sell": "賣方築牆", "buy": "買方追價", "": "中性"}

# 超額顯著門檻 = EXCESS_K × |超額|中位數（MAD 概念）。2.0 ≈ 1.35σ，只有約兩成會亮燈。
EXCESS_K      = 2.0
# 價內／價外分組後，一組有效樣本少於此數就整組不判定
MIN_GROUP_N   = 5
# 超額門檻的絕對下限（百分點）；低於這個幅度的超額沒有解讀價值
MIN_EXCESS_PT = 5.0
# 單一履約價要被判買賣方，至少要有的成交口數。NDX 全市場日均約 20 萬口，
# 只有台指選的兩成，台指版的 50 口門檻在這裡會把大部分有效檔位擋掉。
MIN_TAG_VOL   = 20
# 一側價外合計成交量低於此數，就不把口數最大的履約價當成牆
MIN_ZONE_VOL  = 200


def add_excess(rows, under, is_call):
    """
    算「超額漲跌」：該履約價今日漲跌% 減掉「同側、同價內外」履約價漲跌%的中位數。

    直接看漲跌%會誤判：指數一漲，全部 CALL 一起漲、全部 PUT 一起跌，那是方向 beta，
    不是誰在這個履約價出手。扣掉中位數之後剩下的，才是這一檔相對同儕的異常強弱。

    還要分價內／價外，是因為兩者的 % 變動不是同一種東西：價內權利金跟著內含價值走，
    價外動的是時間價值與 IV，基數小、%天生大得多，混在一起取中位數會系統性誤判價內檔。
    """
    live = [r for r in rows.values() if r["rate"] is not None and r["vol"] > 0]
    for r in rows.values():
        r["tag"] = ""
        r.setdefault("excess", None)
        r.setdefault("grp", "")
    if not live:
        return

    vol_gate = max((sum(r["vol"] for r in live) or 1) * 0.01, MIN_TAG_VOL)
    for g in ("itm", "otm"):
        grp  = [r for r in live if moneyness(r["K"], under, is_call) == g]
        core = [r for r in grp if r["vol"] >= vol_gate]
        if len(core) < MIN_GROUP_N:
            continue
        med   = _median([r["rate"] for r in core])
        scale = max((_median([abs(r["rate"] - med) for r in core]) or 1.0) * EXCESS_K,
                    MIN_EXCESS_PT)
        for r in grp:
            r["excess"] = r["rate"] - med
            r["grp"] = g
        for r in core:
            if r["excess"] <= -scale:
                r["tag"] = "sell"     # 相對同儕被壓 → 賣方築牆
            elif r["excess"] >= scale:
                r["tag"] = "buy"      # 相對同儕被追 → 買方挑戰


def build_zone(crows, prows, under, lo, hi, top=3):
    """
    盤中撐壓：用今日累積成交口數的分布抓牆的位置，再跟前一日 OI 牆對照。

    排序用口數而不是金額：要跟 OI 牆對照，而 OI 的單位就是口數；且金額 = 權利金×乘數×口數，
    權利金隨接近價平而變大，用金額排幾乎必然選出離標的最近那一檔 —— 那是價平的定義，
    不是市場押注的位置。金額仍並列顯示，回答的是資金投入強度。
    """
    def pick(rows, keep):
        cand = [r for r in rows.values() if keep(r["K"]) and r["vol"] > 0]
        cand.sort(key=lambda r: r["vol"], reverse=True)
        return cand[:top], sum(r["vol"] for r in cand)

    res, res_vol = pick(crows, lambda k: k >= under)      # 壓力：價外買權
    sup, sup_vol = pick(prows, lambda k: k <= under)      # 支撐：價外賣權
    res_thin = res_vol < MIN_ZONE_VOL
    sup_thin = sup_vol < MIN_ZONE_VOL

    # OI 牆限制在標的同一側 + 畫面的 ±radius 視窗內。不限範圍的話，深價外長年
    # 累積的存量 OI 會冒出來當成「今天的壓力」，憑空生出牆移動幾千點的假訊號。
    def wall_of(rows, keep):
        c = {r["K"]: r["oi"] for r in rows.values()
             if lo <= r["K"] <= hi and keep(r["K"]) and r["oi"] > 0}
        return max(c, key=lambda k: c[k]) if c else None

    c_wall = wall_of(crows, lambda k: k >= under)
    p_wall = wall_of(prows, lambda k: k <= under)
    return {
        "res": res, "sup": sup, "res_vol": res_vol, "sup_vol": sup_vol,
        "res_thin": res_thin, "sup_thin": sup_thin,
        "res_k": res[0]["K"] if (res and not res_thin) else None,
        "sup_k": sup[0]["K"] if (sup and not sup_thin) else None,
        "c_wall": c_wall, "p_wall": p_wall,
        "res_shift": (res[0]["K"] - c_wall) if (res and c_wall and not res_thin) else None,
        "sup_shift": (sup[0]["K"] - p_wall) if (sup and p_wall and not sup_thin) else None,
    }


# ── 3. 組報告資料 ─────────────────────────────────────────────────────────────

def group_fwd(grp):
    """用買賣權 parity 推當組的遠期價；沒有可配對的買賣權回 None。"""
    calls, puts = grp["C"], grp["P"]
    common = [k for k in calls if k in puts]
    if not common:
        return None
    atm = min(common, key=lambda k: abs(calls[k]["px"] - puts[k]["px"]))
    return atm + calls[atm]["px"] - puts[atm]["px"]


# 表格最多顯示幾列；超過就往上挑格點（NDX 近價平間距只有 10 點，
# ±800 點會有 160 檔，全列出來根本讀不動）
MAX_ROWS      = 72
STEP_LADDER   = [5, 10, 25, 50, 100, 250]
# 不在格點上、但今日成交量擠進前幾名的履約價一樣保留 —— 大單常常就打在
# 非整數的價位上，用格點硬篩會把當天最重要的那一檔篩掉。
KEEP_ACTIVE_N = 12


def pick_strikes(crows, prows, lo, hi, atm=None):
    """
    選出要顯示的履約價：先卡 ±radius 視窗，太多就挑格點，另外保留最活躍的幾檔。

    價平一定要留。NDX 掛到 10 點一檔，價平常常落在非 25 倍數的價位上
    （實測 29,660），純用格點篩會把整張表最重要的那條參考線篩掉。
    """
    all_k = sorted({k for k in set(crows) | set(prows) if lo <= k <= hi})
    if len(all_k) <= MAX_ROWS:
        return all_k, None

    vols = defaultdict(int)
    for rows in (crows, prows):
        for k, r in rows.items():
            if lo <= k <= hi:
                vols[k] += r["vol"]
    keep_always = {k for k, _ in sorted(vols.items(), key=lambda kv: kv[1], reverse=True)[:KEEP_ACTIVE_N]
                   if vols[k] > 0}
    if atm is not None and lo <= atm <= hi:
        keep_always.add(atm)

    for step in STEP_LADDER:
        keep = sorted({k for k in all_k if k % step == 0} | keep_always)
        if len(keep) <= MAX_ROWS:
            return keep, step
    return sorted(keep_always) or all_k[:MAX_ROWS], STEP_LADDER[-1]


def build_report(gkey, grp, under, tab_id, tab_name, mult, radius):
    """把單一到期別（一個分頁）的資料整理成畫面要的形狀。"""
    root, yymmdd = gkey
    calls, puts = grp["C"], grp["P"]
    exp = parse_expiry(yymmdd)

    common = [k for k in calls if k in puts]
    if not common:
        raise ValueError(f"{tab_name}：無有效買賣權對可定價")
    atm = min(common, key=lambda k: abs(k - under))
    fwd = group_fwd(grp)

    lo, hi = under - radius, under + radius

    def mk(side, k, is_call):
        d = side.get(k)
        if not d:
            return None
        px, vol = d["px"], d["vol"]
        return {"K": k, "px": px, "vol": vol, "amt": int(px * mult * vol),
                "be": (k + px) if is_call else (k - px),
                "bid": d["bid"], "ask": d["ask"], "oi": d["oi"],
                "rate": d["rate"], "ref": d["ref"],
                "iv": d["iv"], "delta": d["delta"], "quoted": d["quoted"]}

    call_all = {k: v for k in calls if (v := mk(calls, k, True))}
    put_all  = {k: v for k in puts  if (v := mk(puts,  k, False))}
    strikes, step = pick_strikes(call_all, put_all, lo, hi, atm=atm)
    crows = {k: call_all[k] for k in strikes if k in call_all}
    prows = {k: put_all[k]  for k in strikes if k in put_all}

    add_excess(crows, under, True)
    add_excess(prows, under, False)
    # 撐壓用視窗內全部履約價算，不受表格格點篩選影響 —— 篩選是為了畫面好讀，
    # 不該讓被篩掉的那一檔在撐壓判讀裡消失
    zw_c = {k: v for k, v in call_all.items() if lo <= k <= hi}
    zw_p = {k: v for k, v in put_all.items()  if lo <= k <= hi}
    add_excess(zw_c, under, True)
    add_excess(zw_p, under, False)
    zone = build_zone(zw_c, zw_p, under, lo, hi)

    # CBOE 的 last_trade_time 是美東時間、無時區標記
    lt = grp["last"]
    tstr = lt.replace("T", " ")[5:] if lt else "-"
    today_et = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    stale = bool(lt) and not lt.startswith(today_et)

    return {
        "id": tab_id, "tab": tab_name, "root": root, "expiry": exp,
        "desc": ROOT_DESC.get(root, root), "under": under, "atm": atm, "fwd": fwd,
        "strikes": strikes, "step": step, "crows": crows, "prows": prows,
        "time": tstr, "stale": stale, "vol": grp["vol"], "oi": grp["oi"], "zone": zone,
        "dte": (exp - datetime.now(ET_TZ).date()).days,
    }


# 沒指定 --radius 時，視窗取標的價的這個比例（NDX 29,600 → ±800 點）。
# 寫成比例而不是固定點數，換成 QQQ（約 715）時視窗才不會寬到涵蓋整條鏈。
DEFAULT_RADIUS_PCT = 0.027


def build_page(sym="NDX", radius=None):
    """抓一次 CBOE，拆出最近到期／本週五兩個到期別，組成整頁資料。"""
    session = current_session()
    data    = fetch_cboe(sym)
    groups  = collect_groups(data)
    mult    = MULTIPLIER.get(sym, 100)

    near, fri = next_expiries(groups)
    picks = []
    p = pick_expiry(groups, near)
    if p:
        dte = (near - datetime.now(ET_TZ).date()).days
        name = "當日到期" if dte == 0 else f"最近到期（{dte}天）"
        picks.append(("near", name, p[0], p[1]))
    if fri:
        p2 = pick_expiry(groups, fri)
        if p2 and (not picks or p2[0] != picks[0][2]):
            # 帶上天數：今天本身就是週五時第二頁會順延到下週五，
            # 只寫「週五到期」會讓人以為是今天收盤結算的那一檔。
            fdte = (parse_expiry(p2[0][1]) - datetime.now(ET_TZ).date()).days
            picks.append(("fri", f"週五到期（{fdte}天）", p2[0], p2[1]))
    if not picks:
        raise ValueError("找不到可顯示的到期別")

    under = _f(data["data"].get("current_price")) or _f(data["data"].get("close"))
    usrc  = f"^{sym} 指數" if sym in INDEX_SYMS else sym
    if not under:
        under, usrc = group_fwd(picks[0][3]), "價平parity"
        if not under:
            raise ValueError("無標的價可用（指數報價與 parity 皆失敗）")
    if not radius:
        radius = round(under * DEFAULT_RADIUS_PCT)

    reps = [build_report(g, v, under, tid, name, mult, radius)
            for tid, name, g, v in picks]
    return {
        "sym": sym, "mult": mult, "session": session, "under": under, "usrc": usrc,
        "reps": reps, "radius": radius,
        "iv30": _f(data["data"].get("iv30")),
        "chg": _f(data["data"].get("price_change_percent")),
        "now": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "now_et": datetime.now(ET_TZ).strftime("%m/%d %H:%M:%S"),
        # 給網頁算「資料幾分鐘前」用；存 epoch 秒，看的人在哪個時區都不會誤判
        "epoch": int(datetime.now(TW_TZ).timestamp()),
    }


# ── 4. HTML 產出 ─────────────────────────────────────────────────────────────

def heat(amt, mx, base):
    """金額格背景：熱度（資金強度），開 0.55 次方壓縮讓中小值也看得見。"""
    t = (amt / mx) ** 0.55 if mx else 0
    r, g, b = base
    return f"rgb({int(255+(r-255)*t)},{int(255+(g-255)*t)},{int(255+(b-255)*t)})"


def bar(vol, mx, base, to_left):
    """口數格背景畫成成交口數分布條：CALL 由右往左長、PUT 由左往右長，
    以履約價欄為中軸，整張表就是一張左右對開的盤中部位分布圖。
    刻意用線性比例（不像熱度圖壓縮），分布圖要能直接目測倍數關係。"""
    pct = max(0.0, min(100.0, (vol / mx if mx else 0) * 100))
    r, g, b = base
    d = "left" if to_left else "right"
    return (f"background:linear-gradient(to {d},"
            f"rgba({r},{g},{b},.42) 0 {pct:.1f}%,transparent {pct:.1f}% 100%)")


CALL_BASE = (214, 52, 52)
PUT_BASE  = (30, 160, 70)


def money(a):
    """成交金額用美元計；NDX 一檔動輒上千萬，逐位數字讀不出量級。"""
    if a >= 1e9:
        return f"${a/1e9:.2f}B"
    if a >= 1e6:
        return f"${a/1e6:.1f}M"
    if a >= 1e3:
        return f"${a/1e3:.0f}K"
    return f"${a:,.0f}"


def kfmt(k):
    """履約價：NDX 都是整數，QQQ 可能有 0.5。"""
    return f"{k:,.0f}" if float(k) == int(k) else f"{k:,.1f}"


def chg_td(r):
    """今日漲跌%（相對前收）。底色標的是『超額』而非漲跌本身：
    綠＝相對同側同儕被壓（賣方築牆）、紅＝被追價（買方挑戰）。"""
    rate = r.get("rate")
    if rate is None:
        return '<td class="chg"></td>'
    tag = r.get("tag", "")
    ex  = r.get("excess")
    g   = MONEY_TXT.get(r.get("grp", ""), "")
    q   = "（無成交，取中價）" if r.get("quoted") else ""
    tip = (f'今日 {rate:+.0f}%{q}，對照{g}同儕超額 {ex:+.0f}pt → {TAG_TXT[tag]}'
           if ex is not None else f'今日 {rate:+.0f}%{q}（樣本不足或無成交，不判定）')
    cls = f"chg {tag}" + (" q" if r.get("quoted") else "")
    return f'<td class="{cls}" title="{tip}">{rate:+.0f}%</td>'


def render_zone(rep):
    """盤中撐壓卡片：今日成交口數分布抓出來的牆，並與前一日 OI 牆對照。"""
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
                f'<div class="zrow"><b>{kfmt(r["K"])}</b>'
                f'<span class="zm">{r["vol"]:,} 口</span>'
                f'<span class="zv">{money(r["amt"])}</span>'
                f'<span class="zr {tag}">{rt}</span>'
                f'<span class="zt {tag}">{TAG_TXT[tag]}</span></div>')
        return "\n".join(out) or '<div class="zrow"><span class="zm">此側無成交</span></div>'

    def note(shift, wall, label):
        if wall is None:
            return f'前一日 {label} OI 牆：視窗內無資料'
        if shift is None:
            return f'前一日 {label} OI 牆 {kfmt(wall)}'
        if abs(shift) <= 50:
            return f'前一日 {label} OI 牆 {kfmt(wall)}　·　盤中重心與其一致'
        d = "上移" if shift > 0 else "下移"
        return f'前一日 {label} OI 牆 {kfmt(wall)}　·　盤中重心{d} {abs(shift):,.0f} 點'

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


def render_panel(rep, page):
    """單一到期別（一個分頁）的 KPI + T 字表 + 說明。"""
    tid = rep["id"]
    crows, prows = rep["crows"], rep["prows"]
    cmax  = max((r["amt"] for r in crows.values()), default=1)
    pmax  = max((r["amt"] for r in prows.values()), default=1)
    cvmax = max((r["vol"] for r in crows.values()), default=1)
    pvmax = max((r["vol"] for r in prows.values()), default=1)
    c_vol = sum(r["vol"] for r in crows.values())
    p_vol = sum(r["vol"] for r in prows.values())
    pcr_v = (p_vol / c_vol) if c_vol else 0
    c_oi  = sum(r["oi"] for r in crows.values())
    p_oi  = sum(r["oi"] for r in prows.values())
    pcr_oi = (p_oi / c_oi) if c_oi else None
    c_top = max(crows.values(), key=lambda r: r["vol"], default=None)
    p_top = max(prows.values(), key=lambda r: r["vol"], default=None)
    c_wall = max(crows, key=lambda k: crows[k]["oi"], default=None) if crows else None
    p_wall = max(prows, key=lambda k: prows[k]["oi"], default=None) if prows else None

    trs = []
    for k in rep["strikes"]:
        c, p = crows.get(k), prows.get(k)
        atm_cls = " atm" if k == rep["atm"] else ""
        if c:
            cc = (f'<td class="amt" data-tab="{tid}" data-side="C" data-k="{k}" data-amt="{c["amt"]}" '
                  f'style="background:{heat(c["amt"], cmax, CALL_BASE)}">'
                  f'<span class="amtnum">{money(c["amt"])}</span><span class="delta"></span></td>'
                  f'<td class="vol" style="{bar(c["vol"], cvmax, CALL_BASE, True)}">{c["vol"]:,}</td>'
                  f'<td class="oi">{c["oi"]:,}</td>'
                  f'{chg_td(c)}'
                  f'<td class="px{" q" if c["quoted"] else ""}" data-tab="{tid}" data-side="C" '
                  f'data-k="{k}" data-px="{c["px"]:g}">'
                  f'<span class="pxnum">{c["px"]:g}</span><span class="delta plain"></span></td>'
                  f'<td class="be">{c["be"]:,.0f}</td>')
        else:
            cc = '<td class="e"></td>' * 6
        if p:
            pc = (f'<td class="be">{p["be"]:,.0f}</td>'
                  f'<td class="px{" q" if p["quoted"] else ""}" data-tab="{tid}" data-side="P" '
                  f'data-k="{k}" data-px="{p["px"]:g}">'
                  f'<span class="pxnum">{p["px"]:g}</span><span class="delta plain"></span></td>'
                  f'{chg_td(p)}'
                  f'<td class="oi">{p["oi"]:,}</td>'
                  f'<td class="vol" style="{bar(p["vol"], pvmax, PUT_BASE, False)}">{p["vol"]:,}</td>'
                  f'<td class="amt" data-tab="{tid}" data-side="P" data-k="{k}" data-amt="{p["amt"]}" '
                  f'style="background:{heat(p["amt"], pmax, PUT_BASE)}">'
                  f'<span class="amtnum">{money(p["amt"])}</span><span class="delta"></span></td>')
        else:
            pc = '<td class="e"></td>' * 6
        trs.append(f'<tr class="drow{atm_cls}">{cc}<td class="strike">{kfmt(k)}</td>{pc}</tr>')
    rows_html = "\n".join(trs)

    e = rep["expiry"]
    dte_txt = "當日到期 0DTE" if rep["dte"] == 0 else f'{rep["dte"]} 天後到期'
    exp_txt = f'{e:%m/%d}（{e:%a}）到期・{dte_txt}　·　{rep["root"]} {rep["desc"]}'
    time_txt = (f'⚠ 非今日行情　最後成交 {rep["time"]}' if rep["stale"]
                else f'最後成交 {rep["time"]} ET')
    step_txt = (f'　·　表格每 {rep["step"]} 點一列（另補上今日最活躍檔位）'
                if rep["step"] else "")

    oi_note = (f'未平倉牆（前一交易日收盤）：買權壓力 <b>{kfmt(c_wall)}</b>、'
               f'賣權支撐 <b>{kfmt(p_wall)}</b>'
               f'{"、Put/Call 未平倉比 <b>%.2f</b>" % pcr_oi if pcr_oi else ""}。'
               ) if (c_wall and p_wall) else "未平倉牆：視窗內無 OI 資料。"

    ctop_txt = f'{kfmt(c_top["K"])}（{c_top["vol"]:,} 口）' if (c_top and c_top["vol"]) else "尚無成交"
    ptop_txt = f'{kfmt(p_top["K"])}（{p_top["vol"]:,} 口）' if (p_top and p_top["vol"]) else "尚無成交"

    return f'''<section class="panel" data-panel="{tid}">
<div class="sub sub-tab">{exp_txt}　·　{time_txt}{step_txt}</div>
<div class="kpis">
  <div class="kpi call"><div class="l">CALL 成交量</div><div class="v">{c_vol:,}<small> 口</small></div></div>
  <div class="kpi put"><div class="l">PUT 成交量</div><div class="v">{p_vol:,}<small> 口</small></div></div>
  <div class="kpi"><div class="l">Put/Call 量比</div><div class="v">{pcr_v:.2f}</div></div>
  <div class="kpi"><div class="l">P/C 未平倉比</div><div class="v">{f"{pcr_oi:.2f}" if pcr_oi else "—"}</div></div>
  <div class="kpi"><div class="l">價平</div><div class="v">{kfmt(rep["atm"])}</div></div>
</div>
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
  本到期別未平倉合計 {rep["oi"]:,} 口。
</div>
</section>'''


# 分頁切換：按鈕控制哪一個 panel 顯示，選擇存 localStorage，
# 這樣每 60 秒自動重整回來時還停在原本看的那一頁。
TAB_JS = """
<script>
(function(){
  var btns = [].slice.call(document.querySelectorAll('.tab'));
  if(!btns.length) return;
  var panels = [].slice.call(document.querySelectorAll('.panel'));
  function show(id){
    btns.forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-tab') === id); });
    panels.forEach(function(p){ p.classList.toggle('on', p.getAttribute('data-panel') === id); });
    try { localStorage.setItem('ndx_tab', id); } catch(e){}
  }
  var ids = btns.map(function(b){ return b.getAttribute('data-tab'); });
  var saved = null;
  try { saved = localStorage.getItem('ndx_tab'); } catch(e){}
  show(ids.indexOf(saved) >= 0 ? saved : ids[0]);
  btns.forEach(function(b){
    b.addEventListener('click', function(){ show(b.getAttribute('data-tab')); });
  });
})();
</script>
"""

# 在瀏覽器端用 localStorage 記住上一版（上一個「產生時間」）的金額與權利金，
# 每次載入就地算出各履約價相對上一版的增減（▲紅=增加、▼綠=減少）。
# 金額 ▲▼ 搭配權利金 ▲▼ 可判斷主導方：
#   金額增 + 權利金漲 → 買方追價；金額增 + 權利金跌 → 賣方壓價收租。
DELTA_JS = """
<script>
(function(){
  var wrap = document.querySelector('.wrap');
  if(!wrap) return;
  var GEN = wrap.getAttribute('data-gen') || '';
  // key = 分頁:側別:履約價:a(金額)/p(權利金)；分頁要進 key，
  // 否則兩個到期別的同一履約價會互相蓋掉基準。
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
  try { prev = JSON.parse(localStorage.getItem('ndx_snap') || 'null'); } catch(e){}
  var base = null;
  if(!prev){
    localStorage.setItem('ndx_snap', JSON.stringify({gen:GEN, cur:cur, base:cur}));
  } else if(prev.gen === GEN){
    base = prev.base;                        // 同一版重整：沿用既有基準
  } else {
    base = prev.cur;                         // 換新版：上一版數字成為新基準
    localStorage.setItem('ndx_snap', JSON.stringify({gen:GEN, cur:cur, base:prev.cur}));
  }
  if(!base) return;                          // 第一次看：尚無可比較的基準
  function fmtAmt(n){
    if(n >= 1e9) return '$' + (n/1e9).toFixed(2) + 'B';
    if(n >= 1e6) return '$' + (n/1e6).toFixed(1) + 'M';
    if(n >= 1e3) return '$' + Math.round(n/1e3) + 'K';
    return '$' + Math.round(n);
  }
  function fmtPx(n){ return String(Math.round(n * 100) / 100); }
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
// 顯示「這份資料是幾分鐘前抓的」。CBOE 本身就延遲 15 分鐘，所以年齡門檻放寬：
// 30 分鐘以上轉黃、60 分鐘以上轉紅。
(function(){
  var el = document.getElementById('age');
  var wrap = document.querySelector('.wrap');
  if(!el || !wrap) return;
  var epoch = parseInt(wrap.getAttribute('data-epoch') || '0', 10);
  if(!epoch) return;
  function tick(){
    var sec = Math.max(0, Math.floor(Date.now()/1000) - epoch);
    var txt;
    if(sec < 60)        txt = sec + ' 秒前';
    else if(sec < 3600) txt = Math.floor(sec/60) + ' 分鐘前';
    else                txt = Math.floor(sec/3600) + ' 小時 ' + Math.floor((sec%3600)/60) + ' 分前';
    el.textContent = '抓取於 ' + txt;
    el.className = sec >= 3600 ? 'dead' : (sec >= 1800 ? 'stale' : '');
  }
  tick();
  setInterval(tick, 10000);
})();
// 每 60 秒帶時間戳重新載入：繞過主畫面 App 與 CDN 的快取，永遠抓最新那版。
setTimeout(function(){
  location.replace(location.pathname + '?t=' + Date.now());
}, 60000);
</script>
"""


def render_html(page):
    """整頁：共用表頭 + 兩個到期別分頁。"""
    reps = page["reps"]
    live = page["session"] == "美股盤中" and not any(r["stale"] for r in reps)
    dot  = "#e0392b" if live else "#9a9790"
    sess_txt = page["session"] if live else f'{page["session"]}（顯示最後成交價）'
    sym  = page["sym"]

    tabs = []
    for r in reps:
        tabs.append(f'<button class="tab" data-tab="{r["id"]}">{r["tab"]}'
                    f'<small>{r["expiry"]:%m/%d} {r["root"]}</small></button>')
    tabs_html   = "\n  ".join(tabs)
    panels_html = "\n".join(render_panel(r, page) for r in reps)

    iv_txt  = f'　·　IV30 {page["iv30"]:.1f}%' if page["iv30"] else ""
    chg_txt = f'（{page["chg"]:+.2f}%）' if page["chg"] is not None else ""

    return f'''<meta charset="utf-8">
<title>{sym} 選擇權 T 字報價</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
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
.wrap{{max-width:1080px;margin:0 auto;padding:24px 14px 60px;}}
h1{{font-size:20px;margin:0 0 4px;font-weight:700;letter-spacing:.3px}}
.sub{{color:var(--muted);font-size:12.5px;margin-bottom:6px;line-height:1.6}}
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
.zones{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:0 0 14px}}
@media(max-width:640px){{.zones{{grid-template-columns:1fr}}}}
.zone{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}}
.zone.res{{border-left:3px solid var(--call)}} .zone.sup{{border-left:3px solid var(--put)}}
.zh{{font-size:12px;font-weight:700;margin-bottom:7px}}
.zh small{{font-weight:500;color:var(--muted);font-size:10.5px;margin-left:7px}}
.zrow{{display:flex;align-items:baseline;gap:8px;font-size:12px;padding:3px 0;flex-wrap:wrap}}
.zrow b{{font-size:14px;min-width:66px}}
.zm{{color:var(--ink);min-width:60px}} .zv,.zr,.zt{{color:var(--muted);font-size:11px}}
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
/* 沒有今日成交、權利金取自買賣中價的檔位，用淡化提醒不要當成成交價看 */
.px.q{{font-weight:500;opacity:.62;font-style:italic}}
.chg.q{{opacity:.62}}
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
</style>
<div class="wrap" data-gen="{page["now"]}" data-epoch="{page["epoch"]}">
<h1>那斯達克100 選擇權 T 字報價<span style="font-size:13px;color:var(--muted);font-weight:600;margin-left:8px">{sym}</span></h1>
<div class="sub"><span class="dot"></span>{sess_txt}　·　標的 {page["under"]:,.0f}{chg_txt}（{page["usrc"]}）{iv_txt}　·　美東 {page["now_et"]}　·　台北 {page["now"][11:]}　·　<span id="age">—</span></div>
<div class="tabs">
  {tabs_html}
</div>
{panels_html}
<div class="legend">
  <span><span class="sw" style="background:linear-gradient(90deg,transparent,rgba(214,52,52,.42))"></span>買權口數分布（由右往左）</span>
  <span><span class="sw" style="background:linear-gradient(90deg,rgba(30,160,70,.42),transparent)"></span>賣權口數分布（由左往右）</span>
  <span><span class="sw" style="background:linear-gradient(90deg,#fff,rgb(214,52,52))"></span>金額熱度</span>
  <span><span class="sw" style="background:var(--atm);border:1px solid #d8b24a"></span>價平</span>
  <span><i style="opacity:.62">斜體權利金</i> ＝ 今日無成交，取買賣中價</span>
  <span><b style="color:#ff6a5c">▲</b> 較上一版增加　<b style="color:#37d67a">▼</b> 較上一版減少</span>
  <span>網頁每 60 秒自動重新整理</span>
</div>
<div class="note">
  <b>為什麼是 NDX 不是小那（MNQ）</b>：MNQ 的期貨選擇權只在 CME 交易，官網對程式抓取直接封鎖，
  沒有免費資料源。NDX 是那斯達克 100 指數本身的選擇權，跟小那追蹤同一個指數，
  點位幾乎一比一對應，這裡算出來的支撐壓力可以直接拿去看小那的價位。<br>
  <b>資料來源與延遲</b>：CBOE 公開延遲報價，<b>延遲約 15 分鐘</b>，不是即時 ——
  跟台指版的 5 秒準即時不同，短打進出前要留意。權利金、口數、金額、今日漲跌為當日累積；
  金額 = 權利金 × {page["mult"]} × 口數（美元）。<br>
  <b>兩種商品別</b>：同一天到期可能同時掛 NDXP（PM 結算，日選／週選，用結算日收盤價）與
  NDX（AM 結算，月選／季選，用結算日開盤價）。兩者結算方式不同，分頁各取該到期日 OI 較大的那一種，
  不混在一起計算。<br>
  <b>盤中撐壓怎麼來的</b>：OI 盤中不更新，所以牆的位置改用<b>今日累積成交口數</b>抓 ——
  壓力取標的之上口數最大的買權、支撐取標的之下口數最大的賣權，範圍限在畫面的 ±{page["radius"]:,} 點視窗內。
  用口數而非金額，是因為要跟 OI 牆對照，而 OI 的單位就是口數；且金額 = 權利金×乘數×口數，
  權利金隨接近價平而變大，用金額排幾乎必然選出離標的最近那一檔 —— 那是價平的定義，不是市場押注的位置。
  口數欄的分布條回答「牆有多高」，金額欄的熱度回答「押了多少錢」。
  一側價外合計不足 {MIN_ZONE_VOL} 口時直接標示量能不足，不硬給價位。<br>
  <b>賣方築牆／買方追價</b>：直接看漲跌%會誤判 —— 指數一漲，全部買權一起漲、全部賣權一起跌，那是方向 beta。
  本表扣掉<b>同側、同價內外</b>所有履約價漲跌%的中位數，剩下的「超額」才是相對同儕的異常強弱：
  跌得比同儕兇（綠）＝有人壓價收租，牆較硬；漲得比同儕兇（紅）＝有人追價，該價位可能被挑戰。
  顯著門檻用超額絕對值的中位數自適應，不是固定值。<br>
  <b>履約價間距</b>：NDX 近價平掛到 10 點一檔，全列出來一頁會有一百多列。
  表格自動往上挑格點讓列數落在 {MAX_ROWS} 列以內，另外把今日成交量前 {KEEP_ACTIVE_N} 名的檔位補回來 ——
  大單常打在非整數價位上，純用格點篩會把當天最重要的那一檔篩掉。撐壓判讀用的是視窗內全部履約價，
  不受這層畫面篩選影響。<br>
  <b>限制</b>：OI 為前一交易日收盤數字，盤中不更新；成交量只知道成交，不知道那一口是新倉還是平倉，
  所以「築牆」是傾向推論而非事實。今日無成交的檔位權利金取買賣中價（斜體標示），漲跌%會跟著中價跳動。
</div>
</div>''' + TAB_JS + DELTA_JS


# ── 5. ntfy 推播 ─────────────────────────────────────────────────────────────

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
    lines = [f"NDX {page['under']:,.0f}　{page['session']}　美東 {page['now_et']}（延遲15分）"]

    def wall(label, r):
        rt = (f"　今日 {r['rate']:+.0f}%（{TAG_TXT.get(r.get('tag',''),'')}）"
              if r.get("rate") is not None else "")
        return (f"{label} {kfmt(r['K'])}（{r['vol']:,}口）"
                f"　權利金 {r['px']:g}　金額 {money(r['amt'])}{rt}")

    for rep in reps:
        crows, prows = rep["crows"], rep["prows"]
        c_vol = sum(r["vol"] for r in crows.values())
        p_vol = sum(r["vol"] for r in prows.values())
        pcr_v = (p_vol / c_vol) if c_vol else 0
        z = rep["zone"]
        lines.append(f"── {rep['tab']} {rep['expiry']:%m/%d}（{rep['root']}）　價平 {kfmt(rep['atm'])}")
        lines.append(f"CALL {c_vol:,}口 / PUT {p_vol:,}口　P/C量比 {pcr_v:.2f}")
        if z and (z["sup_k"] or z["res_k"]):
            sup = kfmt(z["sup_k"]) if z["sup_k"] else "—"
            res = kfmt(z["res_k"]) if z["res_k"] else "—"
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
    headers = {"Title": "那斯達克選擇權 T 字報價".encode("utf-8"),
               "Tags": "chart_with_upwards_trend"}
    if page_url:
        headers["Click"] = page_url
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers=headers, timeout=10)
        print("  ✓ ntfy 推播成功")
    except Exception as e:
        print(f"  ⚠ ntfy 推播失敗：{e}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NDX", help="標的代碼（NDX / QQQ / SPX，預設 NDX）")
    ap.add_argument("--out", default="", help="HTML 輸出路徑（預設 <標的>選擇權T字報價.html）")
    ap.add_argument("--radius", type=int, default=0,
                    help="顯示價平 ±N 點（預設依標的價自動取 ±2.7%%，NDX 約 ±800）")
    ap.add_argument("--notify", action="store_true", help="推播摘要到 ntfy")
    ap.add_argument("--page-url", default=os.environ.get("PAGE_URL", ""),
                    help="推播點擊要開的網頁網址")
    args = ap.parse_args()

    sym = args.symbol.upper()
    out = args.out or os.path.join(BASE_DIR, f"{sym}選擇權T字報價.html")

    print(f"[{datetime.now(TW_TZ):%H:%M:%S}] 抓取 CBOE {sym} 延遲報價（約 7MB，稍候）…")
    page = build_page(sym=sym, radius=args.radius)
    print(f"  時段 {page['session']}　標的 {page['under']:,.2f}（{page['usrc']}）"
          f"{'　IV30 %.1f%%' % page['iv30'] if page['iv30'] else ''}")
    for rep in page["reps"]:
        c_vol = sum(r["vol"] for r in rep["crows"].values())
        p_vol = sum(r["vol"] for r in rep["prows"].values())
        print(f"  [{rep['tab']}] {rep['root']} 到期 {rep['expiry']:%Y-%m-%d}（{rep['dte']}天）　"
              f"價平 {kfmt(rep['atm'])}　最後成交 {rep['time']}"
              f"{'　⚠ 非今日行情' if rep['stale'] else ''}")
        print(f"      顯示 {len(rep['strikes'])} 列"
              f"{'（每 %d 點一列）' % rep['step'] if rep['step'] else ''}　"
              f"CALL {len(rep['crows'])} 檔 {c_vol:,} 口 / PUT {len(rep['prows'])} 檔 {p_vol:,} 口　"
              f"到期別 OI {rep['oi']:,}")
        z = rep["zone"]
        if z:
            sup = kfmt(z["sup_k"]) if z["sup_k"] else "量能不足"
            res = kfmt(z["res_k"]) if z["res_k"] else "量能不足"
            print(f"      盤中支撐 {sup}　壓力 {res}　"
                  f"OI 牆 賣權 {kfmt(z['p_wall']) if z['p_wall'] else '—'} / "
                  f"買權 {kfmt(z['c_wall']) if z['c_wall'] else '—'}")

    html = render_html(page)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ 已輸出 {out}")

    if args.notify:
        push_ntfy(page, page_url=args.page_url or None)


if __name__ == "__main__":
    main()
