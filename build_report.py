#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把日报 Markdown 转成邮件正文 HTML（仅标准库）。

为什么需要它：`send_mail.py --body-file x.md` 只会把 Markdown 当纯文本塞进
`text/plain` 部件，手机上的 QQ 邮箱看到的就是一堆 `|` 和 `**`。而 `--html`
需要一个 HTML 文件，所以中间这一步必须有人做。

图片引用 `![](chart_donut.png)` 会被改写成 `cid:chart_donut`，由
`send_mail.py --inline` 把同名 PNG 以 Content-ID 内联进正文——收件人打开邮件
就能看到图，不必下载附件。

用法:
    python build_report.py --md daily_out/2026-09-18/daily_brief.md \\
                           --out daily_out/2026-09-18/daily_brief.html \\
                           --check-dir daily_out/2026-09-18
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

#: 表格单元格里的转义竖线先换成哨兵再切分。用「切分时不能有后顾断言」这类正则
#: 技巧都不可靠，换成哨兵后「剩下的竖线都是分隔符」这句话就是真的了。
SENTINEL = "\x01PIPE\x01"

#: 图片路径可能用 / 也可能用 \，两种都要能剥出文件名
PATH_SPLIT = re.compile(r"[/\\]")

_used_cids: list[str] = []


# --------------------------------------------------------------------------
# 样式：全部内联
#
# QQ 邮箱、Gmail、Outlook 都会剥掉 <style> 块和 class，只有 style="" 属性能活到
# 渲染那一刻。所以这里没有 CSS 类，每条样式都跟着标签走。
# --------------------------------------------------------------------------

S_WRAP = ("font-family:'Microsoft YaHei','PingFang SC','Helvetica Neue',Arial,sans-serif;"
          "line-height:1.75;max-width:820px;margin:0 auto;color:#212128;font-size:15px;")
S_H1 = ("font-size:22px;font-weight:700;color:#212128;border-bottom:3px solid #FB7299;"
        "padding-bottom:8px;margin:0 0 18px;")
S_H2 = ("font-size:17px;font-weight:700;color:#212128;margin:26px 0 10px;"
        "padding-left:10px;border-left:4px solid #00A1D6;")
S_H3 = "font-size:15px;font-weight:700;margin:20px 0 8px;"
S_P = "margin:10px 0;"
S_HR = "border:none;border-top:1px solid #E4E4EB;margin:20px 0;"
S_TABLE = ("border-collapse:collapse;width:100%;margin:12px 0;font-size:14px;"
           "table-layout:auto;")
S_TH = ("border:1px solid #E4E4EB;background:#FAFAFC;padding:8px 10px;"
        "font-weight:700;color:#212128;")
S_TD = "border:1px solid #E4E4EB;padding:8px 10px;vertical-align:top;"
S_QUOTE = ("margin:12px 0;padding:8px 14px;border-left:4px solid #00A1D6;"
           "background:#F5FAFD;color:#4A4A55;font-size:14px;")
S_IMG = ("max-width:100%;height:auto;display:block;margin:14px 0;"
         "border:1px solid #E4E4EB;border-radius:8px;")
S_CODE = ("background:#F2F2F5;padding:1px 5px;border-radius:3px;color:#D6336C;"
          "font-family:Consolas,'Courier New',monospace;font-size:13px;")
S_UL = "margin:10px 0;padding-left:22px;"
S_LI = "margin:4px 0;"


# --------------------------------------------------------------------------
# 行内规则
# --------------------------------------------------------------------------

def _img_tag(match: re.Match) -> str:
    alt, path = match.group(1), match.group(2)
    stem = PATH_SPLIT.split(path.strip())[-1].rsplit(".", 1)[0]
    if not stem.isascii():
        # 中文 cid 会被 RFC-2047 编码成 =?utf-8?b?...?=，而没有任何邮件客户端会去
        # 解码 Content-ID 里的 RFC-2047，结果是必然裂图。这里就断掉，别等到收件人看。
        raise SystemExit(f"[error] 图片文件名必须是纯 ASCII：{path.strip()}\n"
                         "        文件名会被当作 cid，中文名必定裂图。")
    _used_cids.append(stem)
    return f'<img src="cid:{stem}" alt="{alt}" style="{S_IMG}">'


def render_inline(text: str) -> str:
    """把一条已转义的文本加上行内标签。

    铁律：**先转义，后插标签**。先把 `**x**` 换成 `<strong>x</strong>` 再
    `html.escape`，转义会把刚插进去的标签一起毁掉（实测得到
    `&lt;strong&gt;a&amp;b&lt;/strong&gt;`）。所以调用方必须先转义，这里绝不再
    转义第二次——二次转义会把 `&amp;` 变成 `&amp;amp;`。
    """
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _img_tag, text)
    text = re.sub(r"`([^`]+)`", lambda m: f'<code style="{S_CODE}">{m.group(1)}</code>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 表格外残留的转义竖线还原成字面竖线
    return text.replace("\\|", "|")


# --------------------------------------------------------------------------
# 表格
# --------------------------------------------------------------------------

def split_row(line: str) -> list[str]:
    """按未转义的竖线切分表格行。

    实测 `:15` 的 `《鸣潮》动画短片 \\| 寻心` 必须还原成 **6** 个单元格而不是 7 个，
    否则整张表都会错位。
    """
    protected = line.replace("\\|", SENTINEL)
    cells = protected.split("|")
    if cells and not cells[0].strip():
        cells = cells[1:]          # 行首的 `|` 只是边框，不是空单元格
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    # 这里绝不能再转义一次：切分前整行已经转义过了，再转一次就是 &amp;amp;
    return [c.replace(SENTINEL, "|").strip() for c in cells]


def is_delimiter(cells: list[str]) -> bool:
    """`|---:|:---|` 这样的分隔行。

    这是**唯一**安全的表格判据：绝不能用「这一行含竖线」来判断——`:23` 那个引用块
    里就有竖线，误判成表格会把整段话拆散。
    """
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def align_of(spec: str) -> str:
    left, right = spec.startswith(":"), spec.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return ""


def render_table(header: list[str], specs: list[str], rows: list[list[str]]) -> str:
    aligns = [align_of(s) for s in specs]
    out = [f'<table style="{S_TABLE}">', "<thead><tr>"]
    for idx, cell in enumerate(header):
        align = aligns[idx] if idx < len(aligns) else ""
        style = S_TH + (f"text-align:{align};" if align else "")
        out.append(f'<th style="{style}">{render_inline(cell)}</th>')
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for idx, cell in enumerate(row):
            align = aligns[idx] if idx < len(aligns) else ""
            style = S_TD + (f"text-align:{align};" if align else "")
            out.append(f'<td style="{style}">{render_inline(cell)}</td>')
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


# --------------------------------------------------------------------------
# 段落拼接
# --------------------------------------------------------------------------

#: 只有「每一行（最后一行除外）都以句末标点或加粗标记收尾」时才用 <br> 断行。
#: 这份日报里两种换行都有：`:3-4` 是「数据截止日」和「数据来源」两条独立信息，
#: 断行才对；而 `:44-50`、`:59-64` 是同一句话被硬折行，断行会把句子拦腰砍断。
CLOSERS = ("。", "！", "？", "；", "：", "**")


def join_paragraph(raw_lines: list[str], esc_lines: list[str]) -> str:
    if len(raw_lines) > 1 and all(
            r.rstrip().endswith(CLOSERS) for r in raw_lines[:-1]):
        return "<br>".join(esc_lines)
    out = esc_lines[0]
    for part in esc_lines[1:]:
        # 中文之间不能补空格，否则折行处会多出一个突兀的缝；只有两侧都是 ASCII
        # 单词字符时才需要空格。标签结尾的 `>` 不算单词字符，天然被排除。
        if out and part and out[-1].isascii() and out[-1].isalnum() \
                and part[0].isascii() and part[0].isalnum():
            out += " " + part
        else:
            out += part
    return out


# --------------------------------------------------------------------------
# 主转换
# --------------------------------------------------------------------------

def _starts_block(line: str) -> bool:
    s = line.strip()
    return (not s or s == "---" or s.startswith("#") or s.startswith(">")
            or s.startswith("- ") or s.startswith("* "))


def to_html(md: str) -> str:
    lines = md.split("\n")
    body: list[str] = []
    i, total = 0, len(lines)

    while i < total:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if stripped == "---":
            body.append(f'<hr style="{S_HR}">')
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            style = {1: S_H1, 2: S_H2, 3: S_H3}.get(level, S_H3)
            text = render_inline(html.escape(heading.group(2), quote=True))
            body.append(f'<h{level} style="{style}">{text}</h{level}>')
            i += 1
            continue

        # ---- 表格：本行含竖线，且下一行是真正的分隔行 ----
        if "|" in stripped and i + 1 < total \
                and is_delimiter(split_row(lines[i + 1].strip())):
            header = split_row(html.escape(stripped, quote=True))
            specs = split_row(lines[i + 1].strip())
            i += 2
            rows = []
            while i < total and lines[i].strip() and "|" in lines[i]:
                rows.append(split_row(html.escape(lines[i].strip(), quote=True)))
                i += 1
            body.append(render_table(header, specs, rows))
            continue

        # ---- 引用块：连续的 `>` 行合成一个块 ----
        if stripped.startswith(">"):
            quoted = []
            while i < total and lines[i].strip().startswith(">"):
                quoted.append(lines[i].strip()[1:].strip())
                i += 1
            inner = render_inline(html.escape(" ".join(quoted), quote=True))
            body.append(f'<blockquote style="{S_QUOTE}">{inner}</blockquote>')
            continue

        # ---- 无序列表 ----
        if stripped.startswith("- ") or stripped.startswith("* "):
            items = []
            while i < total and (lines[i].strip().startswith("- ")
                                 or lines[i].strip().startswith("* ")):
                text = render_inline(html.escape(lines[i].strip()[2:].strip(), quote=True))
                items.append(f'<li style="{S_LI}">{text}</li>')
                i += 1
            body.append(f'<ul style="{S_UL}">{"".join(items)}</ul>')
            continue

        # ---- 段落：连续非空行，遇到下一个块级元素为止 ----
        raw, esc = [], []
        while i < total and lines[i].strip():
            if raw and _starts_block(lines[i]):
                break
            raw.append(lines[i])
            esc.append(render_inline(html.escape(lines[i].strip(), quote=True)))
            i += 1
        body.append(f'<p style="{S_P}">{join_paragraph(raw, esc)}</p>')

    return ('<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '</head><body style="margin:0;padding:16px;background:#FFFFFF;">'
            f'<div style="{S_WRAP}">' + "".join(body) + "</div></body></html>\n")


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def check_cids(html_text: str, check_dir: Path) -> list[str]:
    """正文里引用的每个 cid，在目录里都必须有对应文件。

    这道闸把「正文引用了第 2 步根本没产出的图」变成发信之前的硬失败，而不是
    收件人手机上的一张裂图。
    """
    cids = re.findall(r'cid:([A-Za-z0-9_.-]+)', html_text)
    missing = []
    for cid in sorted(set(cids)):
        if not list(check_dir.glob(cid + ".*")):
            missing.append(cid)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description="日报 Markdown 转邮件 HTML")
    parser.add_argument("--md", required=True, help="输入 Markdown")
    parser.add_argument("--out", required=True, help="输出 HTML")
    parser.add_argument("--check-dir", help="校验正文引用的图片都在这个目录里")
    args = parser.parse_args()

    md_path = Path(args.md)
    if not md_path.is_file():
        raise SystemExit(f"[error] 找不到 Markdown：{md_path}")

    html_text = to_html(md_path.read_text(encoding="utf-8"))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")

    if args.check_dir:
        check_dir = Path(args.check_dir)
        if not check_dir.is_dir():
            raise SystemExit(f"[error] --check-dir 不是目录：{check_dir}")
        missing = check_cids(html_text, check_dir)
        if missing:
            raise SystemExit(
                "[error] 正文引用了这些图片，但目录里没有对应文件：\n"
                + "".join(f"        {c}.*\n" for c in missing)
                + f"        目录：{check_dir}")

    print(f"[ok] {out_path}  ({out_path.stat().st_size / 1024:.1f} KB, "
          f"内联图 {len(set(_used_cids))} 张)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
