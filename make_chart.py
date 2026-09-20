#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把排行榜 JSON 渲染成图表 PNG（hbar / donut / scatter / histogram / column）。

本机 pip 无法联网（装不了 matplotlib），所以走"Python 算数据 +
PowerShell System.Drawing 画图"的路子：Python 生成 UTF-8 的中间 JSON，
再由 render_chart.ps1 用系统字体画中文标签，全程零第三方依赖。

用法:
    # 兼容旧调用：单张 Top 10 柱状图
    python make_chart.py --raw data/raw/2026-09-18.json --out daily_out/2026-09-18/daily_top10.png

    # 一次产出全部 5 张，固定 ASCII 文件名
    python make_chart.py --all --raw data/raw/2026-09-18.json --outdir daily_out/2026-09-18
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RENDER_PS1 = Path(__file__).resolve().parent / "render_chart.ps1"
CST = timezone(timedelta(hours=8))

KINDS = ("hbar", "donut", "scatter", "histogram", "column")

#: 输出文件名固定为 ASCII。文件名同时就是邮件 HTML 里的 cid，
#: 中文名会被编码成 =?utf-8?b?...?= ，而没有任何邮件客户端会去解码
#: Content-ID 里的 RFC-2047 —— 结果必然是裂图。
OUTPUT_NAMES = {
    "hbar":      "daily_top10.png",
    "donut":     "chart_donut.png",
    "scatter":   "chart_scatter.png",
    "histogram": "chart_hist.png",
    "column":    "chart_hour.png",
}

TITLES = {
    "hbar":      "B站全站排行榜 Top 10 播放量",
    "donut":     "分区播放量分布",
    "scatter":   "播放量 × 点赞率",
    "histogram": "视频时长分布",
    "column":    "发布时间分布（按小时）",
}

#: 画布尺寸。hbar 的高度由柱子数决定（与 render_chart.ps1 的兜底公式一致）。
CANVAS = {
    "donut":     (1280, 760),
    "scatter":   (1280, 820),
    "histogram": (1280, 700),
    "column":    (1280, 700),
}

#: 散点图绘图区（像素）。绘图区几何只在载荷里定义这一次，render_chart.ps1 优先
#: 采用它、兜底才是自己那份同值的硬编码——两边各写一份迟早会漂移，而漂移的症状
#: 只是标签偏几像素，不会有任何报错。
SCATTER_PLOT = {"left": 130, "top": 132, "right_pad": 60, "bottom": 100}

#: 标签避让用的估算量。10pt 微软雅黑在 96dpi 下一个 em 约 13.3px；
#: 真正的文本宽度仍由 .ps1 的 MeasureString 精确测量，这里只是用来判断
#: 「两个标签会不会撞上」，差几个像素不影响结论。
LABEL_EM = 13.3
LABEL_ROW = 22.0    # 相邻候选行的间距，比字高大，避免两条标签看起来像一段折行文字
LABEL_H = 15.0      # 标签碰撞盒高度
LABEL_PAD = 6.0     # 碰撞盒之间要求的最小间隙

DURATION_BINS = [
    (0, 60, "<1分钟"),
    (60, 180, "1-3分钟"),
    (180, 300, "3-5分钟"),
    (300, 600, "5-10分钟"),
    (600, 1200, "10-20分钟"),
    (1200, None, ">20分钟"),
]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def today_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def fmt_wan(value: int) -> str:
    """播放量按「万」显示。"""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    return f"{value / 10_000:.1f}万"


def fmt_tick(value: float) -> str:
    """坐标轴刻度用的短格式，比 fmt_wan 少一位小数。"""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.0f}万"
    return f"{value:.0f}"


def truncate(text: str, limit: int = 22) -> str:
    # 标题里的 emoji 在 System.Drawing 下会渲染成豆腐块，去掉非 BMP 字符和
    # 变体选择符（U+FE00–FE0F）这类不可见修饰符
    text = "".join(ch for ch in str(text)
                   if ord(ch) <= 0xFFFF and not 0xFE00 <= ord(ch) <= 0xFE0F
                   and ord(ch) != 0x200D)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def view_of(item: dict) -> int:
    return int(item.get("stat", {}).get("view", 0) or 0)


def like_of(item: dict) -> int:
    return int(item.get("stat", {}).get("like", 0) or 0)


def duration_of(item: dict) -> int:
    return int(item.get("duration", 0) or 0)


def pubdate_of(item: dict) -> int:
    return int(item.get("pubdate", 0) or 0)


def nice_axis_max(peak: int) -> tuple[int, int]:
    """把峰值向上取整到一个好读的轴上限，返回 (上限, 刻度步长)。"""
    step = 5
    while peak / step > 6:
        step += 5
    top = max(step, math.ceil(peak / step) * step)
    return top, step


def count_ticks(top: int, step: int) -> list[dict]:
    return [{"pos": v / top, "text": str(v)} for v in range(0, top + 1, step)]


def est_text_width(text: str) -> float:
    """估算这段文字在 10pt 微软雅黑下的宽度（像素）。CJK 按 1 em、其余按 0.55 em。"""
    return sum(LABEL_EM if ord(ch) > 0x2E80 else LABEL_EM * 0.55 for ch in text)


def place_labels(spots: list[dict], plot: dict, width: int, height: int) -> None:
    """给标注点各挑一个互不打架的标签位置，就地写入 dx / dy / align。

    离群点常常挨在一起（实测 3 个里有两个相距 60px），只按「点在轴线上方就往下
    写、下方就往上写」摆，这两条标签会落在相邻两行、横向又互相交叠，读起来像一
    段被折行的文字，看不出哪条属于哪个点。这里按行由近及远试，取第一个既不越出
    绘图区、也不与已放标签相交的位置。
    """
    left, top = plot["left"], plot["top"]
    right = width - plot["right_pad"]
    bottom = height - plot["bottom"]

    placed: list[tuple[float, float, float, float]] = []
    for spot in sorted(spots, key=lambda s: s["fx"]):     # 从左到右，结果稳定
        w = est_text_width(spot["label"])
        fx, fy = spot["fx"], spot["fy"]
        chosen = None
        for row in (-1, 1, -2, 2, -3, 3):
            y = fy + row * LABEL_ROW - (LABEL_H if row < 0 else 0)
            if y < top + 2 or y + LABEL_H > bottom - 2:
                continue
            for align in ("start", "end"):
                x = fx + 12 if align == "start" else fx - 12 - w
                if x < left + 4 or x + w > right - 4:
                    continue
                box = (x - LABEL_PAD, y - LABEL_PAD,
                       x + w + LABEL_PAD, y + LABEL_H + LABEL_PAD)
                if any(box[0] < b[2] and b[0] < box[2]
                       and box[1] < b[3] and b[1] < box[3] for b in placed):
                    continue
                chosen = (x, y, align, box)
                break
            if chosen:
                break
        if chosen is None:
            # 每个候选都放不下（点挤在角落）：退回原来的摆放，让 .ps1 自己贴边
            chosen = (fx + 12, fy - LABEL_ROW - LABEL_H, "start", None)
        x, y, align, box = chosen
        if box is not None:
            placed.append(box)
        spot["dx"] = round(x - fx, 1)      # align=end 时 .ps1 用 dx 定位标签右端
        spot["dy"] = round(y - fy, 1)
        spot["align"] = align


def load_items(raw_path: str) -> tuple[list[dict], str]:
    payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    items = payload.get("data", {}).get("list", [])
    if not items:
        raise SystemExit("[error] JSON 里没有排行榜数据")
    # 接口的 list 是「榜单单名次」而非播放量降序（实测 top10 里有 8 个位置对不上），
    # 直接切片会让标题写着的「Top 10 播放量」名不副实，柱长也不单调。
    items = sorted(items, key=view_of, reverse=True)
    # data.note 是一句说明文案，不是日期；数据截止日取抓取当天的日期
    stem = Path(raw_path).stem
    cutoff = stem if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem) else today_cst()
    return items, cutoff


def envelope(kind: str, out_path, cutoff: str, width: int, height: int,
             title: str | None = None, subtitle: str = "") -> dict:
    return {
        "kind": kind,
        "title": title or TITLES[kind],
        "subtitle": subtitle or f"数据截止 {cutoff}",
        "note": "",
        "output": str(Path(out_path).resolve()),
        "width": width,
        "height": height,
    }


# --------------------------------------------------------------------------
# 各 kind 的载荷构造函数（纯函数，可脱开 PowerShell 单测）
# --------------------------------------------------------------------------

def payload_hbar(items: list[dict], cutoff: str, out_path,
                 top: int = 10, title: str | None = None) -> dict:
    top_items = items[: max(1, top)]
    bars = []
    for item in top_items:
        view = view_of(item)
        bars.append({
            # 柱状图值用「万」为单位，坐标轴刻度才好看
            "value": round(view / 10_000, 2),
            "value_text": fmt_wan(view),
            "label": f"{truncate(item.get('title', ''))}",
        })
    chart = envelope("hbar", out_path, cutoff, 1280, 200 + len(bars) * 62, title)
    chart["unit"] = "万"
    chart["bars"] = bars
    return chart


def payload_donut(items: list[dict], cutoff: str, out_path) -> dict:
    views: dict[str, int] = {}
    counts: dict[str, int] = {}
    for item in items:
        name = item.get("tname") or "未知分区"
        views[name] = views.get(name, 0) + view_of(item)
        counts[name] = counts.get(name, 0) + 1

    total = sum(views.values()) or 1
    ranked = sorted(views.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = ranked[:7], ranked[7:]
    if tail:
        # 「其他」永远排在最后一片：它是合并项，不该抢占 12 点钟的起始位置
        head.append(("其他", sum(v for _, v in tail)))
        counts["其他"] = sum(counts[k] for k, _ in tail)

    slices = []
    for name, value in head:
        pct = value * 100.0 / total          # 不取整：扇形扫角由它累加，必须合计 360°
        slices.append({
            "label": truncate(name, 6),
            "value": value,
            "pct": pct,
            "pct_text": f"{pct:.1f}%",
            "value_text": fmt_wan(value),
            "count_text": f"{counts.get(name, 0)} 条",
        })

    width, height = CANVAS["donut"]
    chart = envelope("donut", out_path, cutoff, width, height)
    chart["slices"] = slices
    chart["center_text"] = fmt_wan(total)
    chart["center_sub"] = f"{len(items)} 条视频总播放"
    return chart


def payload_scatter(items: list[dict], cutoff: str, out_path) -> dict:
    raw = []
    for item in items:
        view, like = view_of(item), like_of(item)
        if view <= 0:
            continue
        raw.append({"view": view, "rate": like * 100.0 / view, "item": item})
    if not raw:
        raise SystemExit("[error] 没有可用于散点图的数据")

    # 播放量跨度超过一个数量级（实测 16.4 倍），线性 x 轴会把 77/100 个点挤在
    # 左侧 30%。改用 log10 轴，Python 直接把 0..1 归一化坐标算好给 PS。
    logs = [math.log10(p["view"]) for p in raw]
    lo, hi = min(logs), max(logs)
    span = (hi - lo) or 1.0
    rates = [p["rate"] for p in raw]
    rate_max = max(rates)

    # 纵轴上限取整到 5 的倍数，刻度才落在整数百分比上
    y_top = max(5, math.ceil(rate_max / 5) * 5)

    def xy(view: int, rate: float) -> tuple[float, float]:
        x = (math.log10(view) - lo) / span
        y = rate / y_top
        return min(1.0, max(0.0, x)), min(1.0, max(0.0, y))

    for p in raw:
        p["x"], p["y"] = xy(p["view"], p["rate"])

    # 离群点：播放量前 20% 里点赞率最低的 2 条（高播放低点赞率），
    # 以及播放量后 50% 里点赞率最高的 1 条（低播放高点赞率）
    by_view = sorted(raw, key=lambda p: -p["view"])
    flagged = {id(p) for p in sorted(by_view[:20], key=lambda p: p["rate"])[:2]}
    flagged |= {id(p) for p in sorted(by_view[len(by_view) // 2:], key=lambda p: -p["rate"])[:1]}

    width, height = CANVAS["scatter"]
    plot = dict(SCATTER_PLOT)
    plot_width = width - plot["left"] - plot["right_pad"]
    plot_height = height - plot["top"] - plot["bottom"]

    points = []
    spots = []
    for p in raw:
        point = {"x": p["x"], "y": p["y"], "outlier": id(p) in flagged}
        if point["outlier"]:
            point["label"] = truncate(p["item"].get("title", ""), 14)
            # 标签避让要在像素坐标上做，所以这里先把点换算成像素。fx/fy 只是
            # 中间量，摆完位置就从载荷里删掉——.ps1 只认 x/y（比例）与 dx/dy（像素）
            point["fx"] = plot["left"] + plot_width * p["x"]
            point["fy"] = plot["top"] + plot_height * (1.0 - p["y"])
            spots.append(point)
        points.append(point)

    place_labels(spots, plot, width, height)
    for point in spots:
        del point["fx"], point["fy"]

    median = sorted(rates)[len(rates) // 2]
    x_ticks = []
    for k in range(6):
        pos = k / 5.0
        x_ticks.append({"pos": pos, "text": fmt_tick(10 ** (lo + span * pos))})

    chart = envelope("scatter", out_path, cutoff, width, height)
    chart["axes"] = {
        "x_label": "播放量（对数刻度）",
        "y_label": "点赞率（点赞 / 播放）",
        "x_ticks": x_ticks,
        "y_ticks": [{"pos": v / y_top, "text": f"{v}%"}
                    for v in range(0, y_top + 1, 5)],
    }
    chart["reference"] = {"pos": median / y_top, "text": f"点赞率中位数 {median:.1f}%"}
    chart["plot"] = plot
    chart["points"] = points
    chart["legend"] = [
        {"key": "normal", "text": f"全部视频（{len(raw)} 条）"},
        {"key": "outlier", "text": "标注点"},
    ]
    return chart


def payload_histogram(items: list[dict], cutoff: str, out_path) -> dict:
    counts = [0] * len(DURATION_BINS)
    for item in items:
        secs = duration_of(item)
        for idx, (lo, hi, _) in enumerate(DURATION_BINS):
            if secs >= lo and (hi is None or secs < hi):
                counts[idx] += 1
                break

    top, step = nice_axis_max(max(counts) or 1)
    bins = [{"label": label, "value": n, "value_text": str(n)}
            for (_, _, label), n in zip(DURATION_BINS, counts)]

    width, height = CANVAS["histogram"]
    chart = envelope("histogram", out_path, cutoff, width, height,
                     subtitle=f"数据截止 {cutoff} · 样本 {len(items)} 条")
    chart["y_ticks"] = count_ticks(top, step)
    # 柱子必须按轴上限缩放，而不是按数据峰值。轴上限是向上取整过的
    # （峰值 26 -> 轴 30），两者混用会让最高柱顶到绘图区顶端却标着 26。
    chart["y_max"] = top
    chart["y_label"] = "视频数量（条）"
    chart["x_label"] = "视频时长"
    chart["bins"] = bins
    return chart


def payload_column(items: list[dict], cutoff: str, out_path) -> dict:
    counts = [0] * 24
    sums = [0] * 24
    for item in items:
        ts = pubdate_of(item)
        if ts <= 0:
            continue
        hour = datetime.fromtimestamp(ts, CST).hour
        counts[hour] += 1
        sums[hour] += view_of(item)

    top, step = nice_axis_max(max(counts) or 1)
    columns = [{"label": f"{h:02d}", "value": n, "value_text": str(n)}
               for h, n in enumerate(counts)]

    averaged = [(h, sums[h] / counts[h]) for h in range(24) if counts[h]]
    line_max = max((v for _, v in averaged), default=1)
    line = {
        "label": "平均播放量",
        "points": [{"x": (h + 0.5) / 24, "y": v / line_max} for h, v in averaged],
    }
    right_ticks = [{"pos": k / 4.0, "text": fmt_tick(line_max * k / 4.0)}
                   for k in range(5)]

    width, height = CANVAS["column"]
    chart = envelope("column", out_path, cutoff, width, height,
                     subtitle=f"数据截止 {cutoff} · 发布时间换算为北京时间")
    chart["y_ticks"] = count_ticks(top, step)
    chart["y_max"] = top
    chart["y_label"] = "视频数量（条）"
    chart["x_label"] = "发布小时"
    chart["columns"] = columns
    chart["line"] = line
    chart["right_ticks"] = right_ticks
    # 04/05/08/14 时实测为 0 条，折线在这几处是断开的——没有数据不等于播放量为 0
    chart["note"] = "折线连接的是有投稿的小时，空档跳过"
    chart["legend"] = [
        {"key": "bar", "text": "视频条数"},
        {"key": "line", "text": "平均播放量"},
    ]
    return chart


BUILDERS = {
    "hbar": payload_hbar,
    "donut": payload_donut,
    "scatter": payload_scatter,
    "histogram": payload_histogram,
    "column": payload_column,
}


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def check_ascii_name(out_path) -> None:
    stem = Path(out_path).stem
    if not stem.isascii():
        raise SystemExit(
            f"[error] PNG 文件名必须是纯 ASCII，收到：{Path(out_path).name}\n"
            "        文件名会被当作邮件正文里的 cid，中文名会被 RFC-2047 编码成\n"
            "        =?utf-8?b?...?= ，而客户端不会解码 Content-ID，必然裂图。")


def render(chart: dict) -> Path:
    kind = chart["kind"]
    # 每个 kind 一个中间文件：固定文件名会让「PS 挂了但旧 JSON 还在」这种
    # 陈旧数据 bug 完全不可见，分文件后失败可归因，也能安全地只重跑一张
    tmp = RENDER_PS1.parent / f"_chart_data_{kind}.json"
    tmp.write_text(json.dumps(chart, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(RENDER_PS1),
           "-DataFile", str(tmp), "-Kind", kind]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"[error] 绘图失败（{kind}），退出码 {proc.returncode}")

    out = Path(chart["output"])
    if not out.is_file() or out.stat().st_size == 0:
        raise SystemExit(f"[error] 没有生成 PNG：{out}")
    print(f"[ok] {kind}：{out}  ({out.stat().st_size / 1024:.0f} KB)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 B站日报图表")
    parser.add_argument("--raw", required=True, help="fetch_rank.py 保存的原始 JSON")
    parser.add_argument("--out", help="输出 PNG 路径（单张模式）")
    parser.add_argument("--kind", choices=KINDS, default="hbar",
                        help="单张模式要画哪种图，默认 hbar")
    parser.add_argument("--all", action="store_true", help="一次产出全部 5 张图")
    parser.add_argument("--outdir", help="--all 的输出目录")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--title", default=None, help="仅覆盖 hbar 的标题")
    args = parser.parse_args()

    if args.all:
        if not args.outdir:
            raise SystemExit("[error] --all 需要同时给 --outdir")
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
    elif not args.out:
        raise SystemExit("[error] 要么给 --out，要么给 --all --outdir")

    items, cutoff = load_items(args.raw)

    if args.all:
        # 必须顺序执行：并行会在 _chart_data_*.json 和 PS 的 Add-Type 缓存上打架
        for kind in KINDS:
            out = outdir / OUTPUT_NAMES[kind]
            check_ascii_name(out)
            render(BUILDERS[kind](items, cutoff, out))
    else:
        out = Path(args.out)
        check_ascii_name(out)
        if args.kind == "hbar":
            chart = payload_hbar(items, cutoff, out, args.top, args.title)
        else:
            chart = BUILDERS[args.kind](items, cutoff, out)
        render(chart)
    return 0


if __name__ == "__main__":
    sys.exit(main())
