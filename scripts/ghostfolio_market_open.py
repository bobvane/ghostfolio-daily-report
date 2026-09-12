#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股开市判定 —— 用于 cron 在推送前决定今晚要不要发报告。

用途：定时任务每天 21:00（北京时间）触发前先跑本脚本。
      输出 OPEN → 正常生成并推送报告；输出 CLOSED → 直接静默，不推送。

原理：21:00 北京 = 09:00 ET（夏令时）/ 08:00 ET（冬令时），都在美股 09:30 开盘之前，
      所以「今晚开不开市」等价于「今天（美东日期）是不是纽交所交易日」。
      交易日 = 非周末 且 非 NYSE 假日。半日市（13:00 ET 收盘）算开市。

零依赖：假日用规则计算（含复活节算法），不装 pandas_market_calendars / holidays。
        改年份自动生效，无需手工维护假日表。

用法：
    python3 ghostfolio_market_open.py          # 输出 OPEN / CLOSED
    python3 ghostfolio_market_open.py --json   # 附带详细原因
    python3 ghostfolio_market_open.py --selfcheck
"""
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _nth_weekday(year, month, weekday, n):
    """当月第 n 个星期 weekday（n=1 起）。weekday: 0=周一"""
    d = dt.date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    d = d + dt.timedelta(days=shift)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    """当月最后一个星期 weekday"""
    if month == 12:
        d = dt.date(year, 12, 31)
    else:
        d = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    shift = (d.weekday() - weekday) % 7
    return d - dt.timedelta(days=shift)


def _easter(year):
    """复活节日期（Anonymous Gregorian algorithm）—— 用于倒推耶稣受难日"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(d):
    """假日落在周六→前挪到周五；周日→后挪到周一（NYSE 规则）"""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def nyse_holidays(year):
    """该年纽交所休市日（返回 {date: 名称}）"""
    h = {
        _observed(dt.date(year, 1, 1)): "元旦",
        _nth_weekday(year, 1, 0, 3): "马丁·路德·金纪念日",
        _nth_weekday(year, 2, 0, 3): "华盛顿诞辰",
        _easter(year) - dt.timedelta(days=2): "耶稣受难日",
        _last_weekday(year, 5, 0): "阵亡将士纪念日",
        _observed(dt.date(year, 7, 4)): "美国独立日",
        _nth_weekday(year, 9, 0, 1): "劳动节",
        _nth_weekday(year, 11, 3, 4): "感恩节",
        _observed(dt.date(year, 12, 25)): "圣诞节",
    }
    if year >= 2022:                       # 六月节自 2022 年起休市
        h[_observed(dt.date(year, 6, 19))] = "六月节"
    return h


def check(now_et=None):
    """返回 (是否开市, 原因说明)。now_et 不传则取当前时间。"""
    now_et = now_et or dt.datetime.now(ET)
    d = now_et.date()
    if d.weekday() >= 5:
        return False, f"{d} 是{'周六' if d.weekday() == 5 else '周日'}，美股休市"
    # 查当年 + **下一年**的假日表：元旦若落在周六，NYSE 规则是前挪到上一年的 12/31，
    # 那个日期只存在于「下一年」的字典里（如 2022-01-01 周六 → 2021-12-31 休市，
    # 2021-12-31 只会出现在 nyse_holidays(2022)）。只查当年会漏判，2032-12-31 会复发。
    hol = {**nyse_holidays(d.year), **nyse_holidays(d.year + 1)}
    if d in hol:
        return False, f"{d} 是 NYSE 假日（{hol[d]}），美股休市"
    t = now_et.time()
    if t < dt.time(9, 30):
        phase = "开盘前"
    elif t < dt.time(16, 0):
        phase = "盘中"
    else:
        phase = "已收盘"
    return True, f"{d} 是纽交所交易日（美东 {now_et:%H:%M}，{phase}）"


def _selfcheck():
    """用已知的纽交所假日/交易日验证 —— 规则算错这里先红"""
    # 已知休市日
    known_closed = [
        (dt.date(2026, 1, 1), "元旦"),
        (dt.date(2026, 1, 19), "MLK"),
        (dt.date(2026, 2, 16), "华盛顿诞辰"),
        (dt.date(2026, 4, 3), "耶稣受难日 2026"),
        (dt.date(2026, 5, 25), "阵亡将士纪念日"),
        (dt.date(2026, 6, 19), "六月节"),
        (dt.date(2026, 7, 3), "独立日(7/4 周六 → 前挪 7/3)"),
        (dt.date(2026, 9, 7), "劳动节"),
        (dt.date(2026, 11, 26), "感恩节"),
        (dt.date(2026, 12, 25), "圣诞节"),
        (dt.date(2025, 7, 4), "独立日 2025 周五"),
        (dt.date(2024, 7, 4), "独立日 2024 周四"),
        (dt.date(2024, 6, 19), "六月节 2024"),
        # ⚠️ 跨年边界：元旦落在周六 → 前挪到**上一年** 12/31。这几个日期只存在于「下一年」的
        # 假日表里，只查当年会漏判（Claude 在评审中发现，实测 2021-12-31 曾被判成开市）。
        (dt.date(2021, 12, 31), "元旦 2022 落在周六 → 前挪 2021-12-31"),
        (dt.date(2032, 12, 31), "元旦 2033 落在周六 → 前挪 2032-12-31"),
        (dt.date(2010, 12, 31), "元旦 2011 落在周六 → 前挪 2010-12-31"),
    ]
    for d, why in known_closed:
        ok, _ = check(dt.datetime(d.year, d.month, d.day, 9, 0, tzinfo=ET))
        assert not ok, f"{d} 应休市（{why}）"

    # 已知交易日（含假日前后）
    known_open = [
        dt.date(2026, 1, 2), dt.date(2026, 1, 20), dt.date(2026, 7, 6),
        dt.date(2026, 11, 27), dt.date(2026, 12, 24), dt.date(2025, 12, 26),
        dt.date(2026, 9, 11),   # 普通周五
    ]
    for d in known_open:
        ok, why = check(dt.datetime(d.year, d.month, d.day, 9, 0, tzinfo=ET))
        assert ok, f"{d} 应开市，实际判为休市（{why}）"

    # 周末
    for d in (dt.date(2026, 9, 12), dt.date(2026, 9, 13)):
        assert not check(dt.datetime(d.year, d.month, d.day, 9, 0, tzinfo=ET))[0], f"{d} 周末应休市"

    # 复活节算法抽查
    assert _easter(2026) == dt.date(2026, 4, 5), _easter(2026)
    assert _easter(2025) == dt.date(2025, 4, 20), _easter(2025)
    assert _easter(2024) == dt.date(2024, 3, 31), _easter(2024)

    print("✅ market_open 自检通过（假日规则 / 复活节算法 / 周末 / 假日前后交易日）")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        is_open, why = check()
        if "--json" in sys.argv:
            print(json.dumps({"market": "OPEN" if is_open else "CLOSED",
                              "reason": why,
                              "et_now": dt.datetime.now(ET).isoformat()},
                             ensure_ascii=False, indent=2))
        else:
            print("OPEN" if is_open else "CLOSED")
            print(f"# {why}", file=sys.stderr)
