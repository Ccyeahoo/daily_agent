#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取 B 站全站排行榜，原样保存 JSON 到本地。

只用标准库（本机 pip 无法联网装包）。

B 站风控要点：直接请求 ranking 接口会返回 HTTP 200 但 body 是 {"code":-352}。
必须先请求一次 https://www.bilibili.com/ 拿到 buvid3 cookie，再带上
User-Agent 和 Referer 请求接口，才会返回 code:0。

用法:
    python fetch_rank.py              抓取并保存
    python fetch_rank.py --print-top 10
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"

RANK_API = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"
HOME_URL = "https://www.bilibili.com/"
REFERER = "https://www.bilibili.com/v/popular/rank/all"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CST = timezone(timedelta(hours=8))
RETRIES = 3
TIMEOUT = 25


def build_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA), ("Referer", REFERER),
                         ("Accept", "application/json, text/plain, */*")]
    return opener


def warm_up_cookies(opener: urllib.request.OpenerDirector) -> bool:
    """先访问首页，种下 buvid3 cookie。这是绕开 -352 的关键一步。"""
    try:
        with opener.open(HOME_URL, timeout=TIMEOUT) as resp:
            resp.read(2048)
        return True
    except (urllib.error.URLError, OSError) as exc:
        print(f"[warn] 预热首页失败：{exc}", file=sys.stderr)
        return False


def fetch_ranking() -> dict:
    opener = build_opener()
    warm_up_cookies(opener)

    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(RANK_API, headers={
                "User-Agent": UA, "Referer": REFERER,
                "Accept": "application/json, text/plain, */*",
            })
            with opener.open(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_err = exc
            print(f"[warn] 第 {attempt} 次请求失败：{exc}", file=sys.stderr)
            continue

        code = payload.get("code")
        if code == 0:
            return payload
        if code == -352:
            # 风控拦截：重新预热 cookie 再试
            print(f"[warn] 第 {attempt} 次被风控拦截(-352)，重新预热 cookie",
                  file=sys.stderr)
            last_err = RuntimeError("code -352 风控拦截")
            warm_up_cookies(build_opener())
            continue
        last_err = RuntimeError(f"接口返回 code={code} message={payload.get('message')}")
        print(f"[warn] {last_err}", file=sys.stderr)

    raise SystemExit(f"[error] 抓取失败：{last_err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 B 站全站排行榜")
    parser.add_argument("--print-top", type=int, default=0, help="顺带打印前 N 名")
    parser.add_argument("--out", help="输出路径，默认 data/raw/YYYY-MM-DD.json")
    args = parser.parse_args()

    payload = fetch_ranking()
    items = payload.get("data", {}).get("list", [])

    today = datetime.now(CST).strftime("%Y-%m-%d")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else RAW_DIR / f"{today}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    total_views = sum(int(i.get("stat", {}).get("view", 0) or 0) for i in items)
    print(f"[ok] 抓取时间：{datetime.now(CST):%Y-%m-%d %H:%M:%S} (CST)")
    print(f"[ok] 共 {len(items)} 条，总播放量 {total_views:,}")
    print(f"[ok] 已保存：{out_path}  ({out_path.stat().st_size / 1024:.0f} KB)")

    for rank, item in enumerate(items[: args.print_top], 1):
        stat = item.get("stat", {})
        print(f"  #{rank:>2} {item.get('bvid','')} "
              f"{int(stat.get('view', 0) or 0):>12,} "
              f"[{item.get('tname','')}] {item.get('title','')[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
