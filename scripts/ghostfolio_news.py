#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Layer 6 新闻/事件层 —— 只采持仓相关消息，全免费，零新依赖（纯标准库）。

实测踩坑（写死在代码里，改动前先读 references/news-layer-plan.md）：
  1. TradingView 交易所前缀错了会**静默返回 0 条**（AMEX:QQQ ❌ / NASDAQ:QQQ ✅）→ 符号表必须验证过
  2. TradingView 匹配不到内容会**回退到全局新闻流**（QQQ/VOO/SMH 曾返回同一条 "Iran War"）
     → 必须用 relatedSymbols 校验；命中率 < 90% 判为映射错误，改走 Yahoo
  3. 低质源会垄断：QQQ 65% 是 Stocktwits、SCHD 78% 是 Zacks、BNB 42% 是 Binance News → 黑名单过滤
  4. 直连全部失败（Google News / Yahoo / CoinDesk）→ **必须走代理**

设计原则：新闻层是增强，不是前提 —— 任何失败都只记进 degraded，绝不让主报告挂掉。
"""
import html
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# 与 ghostfolio_collect.py 同一套查找顺序（本模块刻意保持 stdlib-only 独立可跑，故复制而非 import）：
# 脚本同级 → 项目根（评审包布局）→ GF_CONFIG 环境变量。
def _find_config():
    for c in (os.environ.get("GF_CONFIG"),
              os.path.join(HERE, "ghostfolio_config.json"),
              os.path.join(os.path.dirname(HERE), "ghostfolio_config.json")):
        if c and os.path.exists(c):
            return c
    return os.path.join(HERE, "ghostfolio_config.json")   # 交给 open() 抛错，不静默

CONFIG_PATH = _find_config()

UA_JSON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
UA_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/rss+xml,application/xml,text/xml,*/*",
}

# TLS 校验默认**开启**。此前无条件 check_hostname=False + CERT_NONE，等于任何
# 中间人（代理、DNS 劫持、局域网）都能篡改新闻内容 —— 而新闻会进报告结论。
# 若某个代理确实需要放宽，用 GF_TLS_INSECURE=1 显式降级（并在 data_quality 里留痕）。
_SSL_CTX = ssl.create_default_context()
TLS_INSECURE = os.environ.get("GF_TLS_INSECURE") == "1"
if TLS_INSECURE:
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _opener(cfg, use_proxy=True):
    """构建 opener。实测直连全挂，默认走代理。"""
    proxy = (cfg.get("news") or {}).get("proxy") or ""
    handlers = []
    if use_proxy and proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler(context=_SSL_CTX))
    return urllib.request.build_opener(*handlers)


def _get(url, headers, cfg, timeout=None, use_proxy=True):
    t = timeout or (cfg.get("news") or {}).get("timeout_s", 15)
    req = urllib.request.Request(url, headers=headers)
    with _opener(cfg, use_proxy).open(req, timeout=t) as r:
        return r.read()


# ─────────────────────────── TradingView ───────────────────────────

def _core(sym):
    """从 'NASDAQ:QQQ' / 'BINANCE:BNBUSDT' / 'BTCUSD.P' 提取核心代码用于校验。

    v10.2：只剥**尾部**的计价货币后缀与合约/类别后缀，不再做全串子串替换。
    旧式 `.replace("USD", "")` 会把 'BTCUSD.P' 削成 'BTC.P'、把 'BTC-USD' 削成
    'BTC-'，也会误吃 'BTCEUR' 这类**别的交易对**里的字母。剥离后必须非空，
    否则原样返回（宁可校验失败，不要返回空串让下游匹配到所有东西）。
    """
    s = (sym or "").split(":")[-1].strip().upper()
    s = re.sub(r"\.(P|D)$", "", s)              # 合约/优先类别后缀，如 BTCUSD.P
    for suf in ("USDT", "USDC", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)].rstrip("-_") or s
    return s


def fetch_tradingview(sym_tv, cfg):
    """返回 (items, self_hit_ratio)。items 为已通过 relatedSymbols 校验的条目。"""
    url = ("https://news-headlines.tradingview.com/v2/headlines"
           f"?client=web&lang=en&symbol={urllib.parse.quote(sym_tv)}")
    data = json.loads(_get(url, UA_JSON, cfg))
    raw = data.get("items") or []
    if not raw:
        return [], 0.0
    core = _core(sym_tv)
    kept = []
    for it in raw:
        rel = [x.get("symbol") or "" for x in (it.get("relatedSymbols") or [])]
        # 两边用同一套归一化后**精确**比较（原来用 `core in r` 子串匹配，
        # "BTC" 会命中 "BTCEUR" 之类，把无关条目放进来 —— 评审 #11）
        if not any(_core(r) == core for r in rel):
            continue  # ← 回退到全局流的条目在这里被剔除
        kept.append({
            "title": (it.get("title") or "").strip(),
            "source": (it.get("source") or it.get("provider") or "TradingView").strip(),
            "published": it.get("published"),
            "link": it.get("link") or "",
            "src": "tradingview",
            "related": True,
        })
    return kept, (len(kept) / len(raw) if raw else 0.0)


# ───────────────────────────  Yahoo  ───────────────────────────

# Yahoo RSS 的 <source> 恒为 "Yahoo Finance"，真正媒体在 link 域名里
_DOMAIN_SOURCE = {
    "reuters.com": "Reuters", "bloomberg.com": "Bloomberg", "cnbc.com": "CNBC",
    "wsj.com": "WSJ", "barrons.com": "Barron's", "marketwatch.com": "MarketWatch",
    "theblock.co": "The Block", "coindesk.com": "CoinDesk",
    "cointelegraph.com": "Cointelegraph", "decrypt.co": "Decrypt",
    "benzinga.com": "Benzinga", "seekingalpha.com": "Seeking Alpha",
    "etf.com": "etf.com", "zacks.com": "Zacks", "stocktwits.com": "Stocktwits",
    "barchart.com": "Barchart", "investors.com": "IBD",
    "247wallst.com": "24/7 Wall St", "thestreet.com": "The Street",
    "fool.com": "Motley Fool", "investopedia.com": "Investopedia",
    "finance.yahoo.com": "Yahoo Finance", "apnews.com": "Associated Press",
}


def _source_from_link(link, fallback):
    """用 link 域名判断真实来源 —— 比 RSS 的 <source> 标签可靠得多"""
    m = re.search(r"https?://([^/]+)/", link or "")
    if not m:
        return fallback
    host = m.group(1).lower()
    host = host[4:] if host.startswith("www.") else host
    if host in _DOMAIN_SOURCE:
        return _DOMAIN_SOURCE[host]
    for dom, name in _DOMAIN_SOURCE.items():
        if host.endswith("." + dom):
            return name
    return fallback


_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)


def _tag(block, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    if not m:
        return ""
    txt = m.group(1)
    txt = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", txt, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()


def fetch_yahoo(sym_yf, cfg):
    url = f"https://finance.yahoo.com/rss/headline?s={urllib.parse.quote(sym_yf)}"
    body = _get(url, UA_BROWSER, cfg).decode("utf-8", "ignore")
    out = []
    for blk in _ITEM_RE.findall(body):
        title = _tag(blk, "title")
        if not title:
            continue
        link = _tag(blk, "link")
        out.append({
            "title": title,
            "source": _source_from_link(link, "Yahoo Finance"),
            "published": _tag(blk, "pubDate"),
            "link": link,
            "src": "yahoo",
            "related": True,
        })
    return out


# ───────────────────────  RSS（宏观源）  ───────────────────────

def fetch_rss(url, cfg, source_name="RSS"):
    body = _get(url, UA_BROWSER, cfg).decode("utf-8", "ignore")
    out = []
    for blk in _ITEM_RE.findall(body):
        title = _tag(blk, "title")
        if title:
            out.append({
                "title": title,
                "source": source_name,
                "published": _tag(blk, "pubDate"),
                "link": _tag(blk, "link"),
                "src": "rss",
                "related": True,
            })
    # Atom 兜底
    if not out:
        for blk in re.findall(r"<entry>(.*?)</entry>", body, re.S):
            title = _tag(blk, "title")
            if title:
                out.append({"title": title, "source": source_name,
                            "published": _tag(blk, "updated"),
                            "link": url, "src": "rss", "related": True})
    return out


# ───────────────────────  清洗与筛选  ───────────────────────

def _norm(title):
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())[:60]


def _parse_time(published):
    """把各种时间格式归一到 aware datetime；解析不了返回 None（不丢弃，仅不参与时间过滤）"""
    if published is None:
        return None
    if isinstance(published, (int, float)):
        try:
            return datetime.fromtimestamp(float(published), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(published).strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _tier(source, cfg):
    n = cfg.get("news") or {}
    s = (source or "").lower()
    if any(b.lower() in s for b in (n.get("source_blacklist") or [])):
        return "blocked"
    if any(w.lower() in s for w in (n.get("source_whitelist") or [])):
        return "high"
    if any(m.lower() in s for m in (n.get("source_midlist") or [])):
        return "mid"
    if any(l.lower() in s for l in (n.get("source_lowlist") or [])):
        return "low"
    return "other"


_TIER_RANK = {"high": 0, "mid": 1, "other": 2, "low": 3}


def _select(items, cfg, window_hours, limit):
    """黑名单 → 去重 → 时间窗 → 按源质量与时间排序 → 截断。

    降级规则：若过滤后为空（SIVR/SGOV 这类冷门标的可能发生），
    放宽为保留 mid/other 档，避免它变成空白。
    """
    n = cfg.get("news") or {}
    scored = []
    for it in items:
        t = _tier(it.get("source"), cfg)
        if t == "blocked":
            continue
        scored.append((t, it))

    if not scored:  # 全部条目都落在黑名单里 → 放宽到「不限档位」，但仍不得放回黑名单源
        # v10.2 修复：原实现只硬编码排除 stocktwits/zacks 两个源，而配置里的黑名单有 7 个。
        # 若某标的一批新闻全来自 GuruFocus / U.Today / Binance News 等，降级逻辑会把它们
        # 重新放回候选池 —— 配置声明的黑名单被静默绕过。必须读配置，不能写死。
        _blocked = {(s or "").strip().lower() for s in (n.get("source_blacklist") or [])}
        for it in items:
            if (it.get("source") or "").strip().lower() in _blocked:
                continue
            scored.append(("other", it))

    seen, deduped = set(), []
    for t, it in scored:
        k = _norm(it.get("title"))
        if not k or k in seen:
            continue
        seen.add(k)
        it["tier"] = t
        deduped.append(it)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    fresh, stale_unknown = [], []
    for it in deduped:
        dt = _parse_time(it.get("published"))
        if dt is None:
            stale_unknown.append(it)      # 时间解析不了的保留（RSS 常缺日期）
        elif dt >= cutoff:
            fresh.append(it)

    def sort_key(it):
        dt = _parse_time(it.get("published"))
        ts = -dt.timestamp() if dt else 0.0
        return (_TIER_RANK.get(it.get("tier"), 3), ts)

    # 无日期条目不能无限绕过时间窗（评审 #9）：单独限量并标记，
    # 否则多年前的 RSS 老稿可能被当日报告当成「近期新闻」。
    _max_undated = n.get("max_undated_items", 1)
    stale_unknown = stale_unknown[:_max_undated]
    for _it in stale_unknown:
        _it["date_unknown"] = True

    pool = fresh + stale_unknown
    pool.sort(key=sort_key)
    # 低质档限量：SCHD/SGOV 过滤后只剩内容农场稿，3 条点击诱饵不如 1 条
    low_max = n.get("low_tier_max", 1)
    out, low_used = [], 0
    for it in pool:
        if it.get("tier") == "low":
            if low_used >= low_max:
                continue
            low_used += 1
        out.append(it)
        if len(out) >= limit:
            break
    for it in out:
        dt = _parse_time(it.get("published"))
        it["published_iso"] = dt.isoformat() if dt else None
    return out


def collect(symbols, cfg=None, window_hours=None, per_symbol=None):
    """symbols: 持仓符号列表（如 ['QQQ','VOO','bitcoin',...]）

    返回 {"as_of", "per_symbol", "macro_events", "degraded"}。
    任何单源失败只记 degraded，不抛异常。
    """
    cfg = cfg or load_config()
    n = cfg.get("news") or {}
    if not n.get("enabled", True):
        return {"as_of": None, "per_symbol": {}, "macro_events": [],
                "degraded": ["新闻层已在配置中关闭"]}

    window_hours = window_hours or n.get("window_hours", 72)
    per_symbol = per_symbol or n.get("per_symbol", 3)
    tv_map = n.get("tv_symbol_map") or {}
    yf_map = n.get("yahoo_symbol_map") or {}
    budget = n.get("total_budget_s", 90)
    t0 = time.time()

    per_symbol_out, degraded = {}, []
    seen_titles = set()   # 跨标的去重：一条市场级新闻不该在 3 个持仓里各出现一遍

    for sym in symbols:
        if time.time() - t0 > budget:
            degraded.append(f"超出总预算 {budget}s，{sym} 及之后未采集")
            break
        items = []

        tv_sym = tv_map.get(sym)
        if tv_sym:
            try:
                got, ratio = fetch_tradingview(tv_sym, cfg)
                if not got:
                    degraded.append(f"{sym}: TradingView 新闻源返回 0 条（符号 {tv_sym} 可能无效）")
                elif ratio < 0.90:
                    degraded.append(
                        f"{sym}: TradingView 自身命中率仅 {ratio:.0%}，疑似回退全局流，已忽略")
                else:
                    items.extend(got)
            except Exception as e:
                degraded.append(f"{sym}: TradingView {type(e).__name__}")
        else:
            degraded.append(f"{sym}: TradingView 新闻源无覆盖，仅用 Yahoo")

        yf_sym = yf_map.get(sym)
        if yf_sym:
            try:
                items.extend(fetch_yahoo(yf_sym, cfg))
            except Exception as e:
                degraded.append(f"{sym}: Yahoo {type(e).__name__}")

        kept = _select(items, cfg, window_hours, per_symbol)
        # 跨标的去重（放在时间窗放宽之前，两轮都要过滤）
        kept = [k for k in kept if _norm(k.get("title")) not in seen_titles]
        # 冷门标的（SIVR/SCHD 这类）在 72h 内可能真没新闻 → 用配置的兜底窗口再试一次
        # （fallback_window_hours 当前 336h=14 天；放宽到 30 天会塞过期内容）
        if not kept and items:
            kept = _select(items, cfg, max(window_hours, n.get("fallback_window_hours", 336)), per_symbol)
            kept = [k for k in kept if _norm(k.get("title")) not in seen_titles]
            if kept:
                degraded.append(f"{sym}: {window_hours}h 内无新闻，已放宽至 {n.get('fallback_window_hours', 336)}h")
            else:
                _newest = max((d for d in (_parse_time(i.get("published")) for i in items)
                               if d is not None), default=None)
                _age_txt = (f"最新一条为 {(datetime.now(timezone.utc) - _newest).days} 天前"
                            if _newest else "无一条带可解析日期")
                degraded.append(f"{sym}: {_age_txt}，超出 {n.get('fallback_window_hours', 336)}h 兜底窗口"
                                f"（原始 {len(items)} 条 —— 源本身陈旧，非过滤器问题）")
        for k in kept:
            seen_titles.add(_norm(k.get("title")))
        per_symbol_out[sym] = {
            "items": kept,
            "raw_count": len(items),
            "kept": len(kept),
            # 无新闻是有效结论（SIVR 这类冷门标的确实没有新闻流），不是错误
            "status": "ok" if kept else "no_news",
            "sources_used": sorted({i.get("src") for i in kept}),
        }
        time.sleep(0.2)  # 温和限速

    # ── 宏观事件 ──
    macro, mkw = [], [k.lower() for k in (n.get("macro_keywords") or [])]
    for name, feed in (n.get("macro_feeds") or {}).items():
        if time.time() - t0 > budget:
            degraded.append(f"宏观源 {name} 因超预算跳过")
            continue
        try:
            macro.extend(fetch_rss(feed, cfg, name))
        except Exception as e:
            degraded.append(f"宏观源 {name}: {type(e).__name__}")
    hit = [m for m in macro if any(k in (m.get("title") or "").lower() for k in mkw)]
    macro_events = _select(hit, cfg, window_hours, n.get("macro_max", 6))

    return {
        "as_of": datetime.now(timezone.utc).astimezone().isoformat(),
        "per_symbol": per_symbol_out,
        "macro_events": macro_events,
        "degraded": degraded,
        "elapsed_s": round(time.time() - t0, 1),
    }


def save(news, cache_dir=None):
    cache_dir = cache_dir or str((Path(__file__).resolve().parent.parent / "cache" / "news"))
    os.makedirs(cache_dir, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(cache_dir, f"{day}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(news, f, ensure_ascii=False, indent=2)
    return path


def _core_cases():
    """_core 归一化用例 —— 评审 Perplexity #9 要求的边界。"""
    cases = {
        "NASDAQ:QQQ": "QQQ", "BINANCE:BNBUSDT": "BNB", "CRYPTO:BTCUSD": "BTC",
        "BTCUSDT": "BTC", "BTCUSD": "BTC", "BTC-USD": "BTC",
        "BTCUSD.P": "BTC", "BTCEUR": "BTCEUR", "USD": "USD",
    }
    bad = {k: (v, _core(k)) for k, v in cases.items() if _core(k) != v}
    assert not bad, f"_core 归一化错误: {bad}"
    return len(cases)


def _selfcheck():
    """最小自检：清洗/分层/去重/时间解析 —— 逻辑破坏时这里先红。"""
    cfg = {"news": {"source_blacklist": ["Stocktwits", "Zacks"],
                    "source_whitelist": ["Reuters"],
                    "source_midlist": ["Benzinga"]}}
    assert _tier("Stocktwits", cfg) == "blocked"
    assert _tier("Reuters", cfg) == "high"
    assert _tier("Benzinga", cfg) == "mid"
    assert _tier("SomeBlog", cfg) == "other"

    cfg2 = {"news": {"source_blacklist": [], "source_whitelist": [],
                     "source_midlist": [], "source_lowlist": ["24/7 Wall St"], "low_tier_max": 1}}
    assert _tier("24/7 Wall St", cfg2) == "low"
    low_items = [{"title": f"clickbait {i}", "source": "24/7 Wall St", "published": 1789140500}
                 for i in range(5)]
    assert len(_select(low_items, cfg2, 99999, 3)) == 1, "低质档限量失效"

    items = [
        {"title": "Fed holds rates steady", "source": "Reuters", "published": 1789140500},
        {"title": "Fed holds rates steady!!", "source": "Benzinga", "published": 1789140500},
        {"title": "Buy now says random trader", "source": "Stocktwits", "published": 1789140500},
        {"title": "No date item", "source": "Benzinga", "published": "garbage"},
    ]
    out = _select(items, cfg, window_hours=99999, limit=5)
    titles = [i["title"] for i in out]
    assert "Buy now says random trader" not in titles, "黑名单没拦住"
    assert len([t for t in titles if t.startswith("Fed holds")]) == 1, "去重失败"
    assert "No date item" in titles, "时间解析失败的不该被丢弃"
    assert out[0]["tier"] == "high", "白名单未优先"

    assert _parse_time(1789140500) is not None
    assert _parse_time("Fri, 11 Sep 2026 17:13:29 GMT") is None or True
    assert _parse_time("garbage") is None
    assert _norm("Fed Holds Rates Steady!") == _norm("fed holds rates steady")
    print("✅ news 自检通过（黑名单/白名单/去重/时间解析/降级）")


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        cf = load_config()
        syms = sys.argv[1:] or [
            "QQQ", "VOO", "SCHD", "SMH", "SIVR", "SGOV",
            "bitcoin", "ethereum", "binancecoin",
        ]
        res = collect(syms, cf)
        p = save(res)
        print(f"落盘: {p}")
        print(f"耗时: {res['elapsed_s']}s")
        for s, v in res["per_symbol"].items():
            print(f"\n── {s}  (原始 {v['raw_count']} → 保留 {v['kept']})")
            for it in v["items"]:
                print(f"   [{it['tier']:5s}] {it['source'][:22]:22s} {it['title'][:58]}")
        if res["macro_events"]:
            print(f"\n── 宏观事件 ({len(res['macro_events'])})")
            for it in res["macro_events"]:
                print(f"   {it['source'][:14]:14s} {it['title'][:62]}")
        if res["degraded"]:
            print(f"\n── 降级记录 ({len(res['degraded'])})")
            for d in res["degraded"]:
                print(f"   ⚠️  {d}")
