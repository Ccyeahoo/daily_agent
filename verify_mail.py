#!/usr/bin/env python3
"""回读收件箱，核验一封已发出的邮件。仅使用标准库，无需安装任何第三方包。

「已发送」这句打印不能当证据——它只说明 smtplib 没抛异常。本脚本按 METHOD.md
第九节第 3 层，让外部系统作证：

    1. 连 IMAP 收件箱，按 Subject 找到那封邮件（默认在最近 30 封里找）；
    2. 按 UID 取原文，且用 BODY.PEEK[]——用 BODY[] 会置 \\Seen 并改动邮箱状态，
       而「重跑核验」这件事本身依赖邮箱状态不变，等于把核验手段弄坏了；
    3. 断言 MIME 结构，以及内联部件的 Content-ID 与正文里的 cid: 引用一一对应
       （对不上就是客户端裂图）；
    4. 给了 --dir 时，把每个内联图 / 附件解码后与磁盘文件逐字节比对（md5）。

能证明的边界：这只说明「发送端做对了、服务端收下了」。**收件人的客户端有没有把
图渲染出来，机器证不了**，最后那一步只能人工打开邮件看。

用法：

    python scripts/verify_mail.py --subject "B站排行榜日报 2026-09-18" \\
        --dir daily_out/2026-09-18

配置来源与 send_mail.py 一致（命令行 > 环境变量 > smtp.conf > provider 预设），
所以两边读的是同一份 smtp.conf，不会出现「发的用一个账号、验的用另一个」。
"""

from __future__ import annotations

import argparse
import hashlib
import imaplib
import re
import sys
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from pathlib import Path

from send_mail import CONF_FILE, die, env_overrides, read_conf_file

EXIT_CONFIG = 2
EXIT_AUTH = 3
#: 4 已经被 send_mail.py 用作「发送失败」，这里另起一个，别混。
EXIT_VERIFY = 5
#: IMAP 连接被服务端中断（可重试）。QQ 上实测见过 imaplib 解析不了的未标记响应，
#: 属瞬时故障，重连一次通常就好——这类失败不该以 traceback 的形式冒出来。
EXIT_IMAP = 6

#: provider -> (IMAP 主机, 端口, 加密)。与 send_mail.PRESETS 一一对应。
#: 注意 163/126 还要先发一条 IMAP ID 命令，否则登录会以 "Unsafe Login" 被拒——
#: 本仓库用的是 QQ，没做这一层；换 163 时记得补。
IMAP_PRESETS: dict[str, tuple[str, int, str]] = {
    "qq":      ("imap.qq.com",           993, "ssl"),
    "163":     ("imap.163.com",          993, "ssl"),
    "126":     ("imap.126.com",          993, "ssl"),
    "sina":    ("imap.sina.com",         993, "ssl"),
    "aliyun":  ("imap.mxhichina.com",    993, "ssl"),
    "exmail":  ("imap.exmail.qq.com",    993, "ssl"),
    "gmail":   ("imap.gmail.com",        993, "ssl"),
    "outlook": ("outlook.office365.com", 993, "ssl"),
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def decode_value(raw: str | None) -> str:
    """解 encoded-word。Subject 里有中文时来信头是 =?utf-8?b?...?=。"""
    if not raw:
        return ""
    out: list[str] = []
    for text, charset in decode_header(raw):
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out)


def fetch_payload(data) -> bytes:
    """从 imaplib 的 fetch 响应里取出字节体。

    响应形如 [(b'1 (UID 1073 BODY[...] {123}', b'<123 bytes>'), b')']，只有元组
    的第二项才是正文；这里不假设它一定在第一项。
    """
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return b""


def md5(blob: bytes) -> str:
    return hashlib.md5(blob).hexdigest()


def walk(message: Message, depth: int = 0):
    """按深度展开 MIME 树，产出 (depth, part)。"""
    yield depth, message
    if message.is_multipart():
        for child in message.get_payload():
            yield from walk(child, depth + 1)


def describe(part: Message) -> str:
    cid = (part.get("Content-ID") or "").strip().strip("<>")
    disp = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
    name = part.get_filename()
    bits = [part.get_content_type().ljust(24)]
    if disp:
        bits.append(f"disp={disp}")
    if cid:
        bits.append(f"cid={cid}")
    if name:
        bits.append(f"file={decode_value(name)}")
    return "  ".join(bits)


# --------------------------------------------------------------------------
# 参数与配置
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify_mail.py",
        description="回读收件箱核验一封已发出的邮件（IMAP，只读）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--subject", help="要核验的邮件标题，精确匹配（建议带上日期）")
    p.add_argument("--uid", help="直接核验指定 UID，跳过标题搜索")
    p.add_argument("--scan", type=int, default=30,
                   help="在收件箱最新的 N 封里找，默认 30。不用 IMAP SEARCH："
                        "标题含中文时各服务器对 SEARCH 的字符集处理不一致，"
                        "按 UID 倒序取信头自己比更可靠")
    p.add_argument("--dir", help="产物目录；给了就把内联图/附件与磁盘逐字节比对")
    p.add_argument("--expect-inline", action="append", metavar="NAME",
                   help="断言必须作为内联图存在的文件名，可重复")
    p.add_argument("--no-attachment-check", action="store_true",
                   help="跳过附件与磁盘的比对")
    p.add_argument("--retry", type=int, default=2,
                   help="IMAP 连接被服务端中断时重连重试的次数，默认 2")

    g = p.add_argument_group("IMAP 配置（覆盖 smtp.conf 里的同名字段）")
    g.add_argument("--conf", help=f"配置文件路径，默认 {CONF_FILE}")
    g.add_argument("--provider", help=f"预设：{', '.join(sorted(IMAP_PRESETS))}")
    g.add_argument("--imap-host", dest="imap_host", help="IMAP 服务器")
    g.add_argument("--imap-port", dest="imap_port", type=int, help="IMAP 端口，默认 993")
    g.add_argument("--user", help="登录账号（通常就是邮箱地址）")
    g.add_argument("--password", help="授权码；建议改用 smtp.conf 或环境变量 SMTP_PASS")
    return p


def resolve_account(args: argparse.Namespace) -> dict[str, object]:
    """按 send_mail.py 同样的优先级凑出登录信息。"""
    conf = read_conf_file(Path(args.conf) if args.conf else CONF_FILE)
    conf.update(env_overrides())

    provider = (args.provider or conf.get("provider") or "").lower()
    preset_host, preset_port, preset_security = IMAP_PRESETS.get(provider, ("", 993, "ssl"))
    if provider and provider not in IMAP_PRESETS:
        print(f"[warn] 未知 provider '{provider}'，请用 --imap-host 显式指定",
              file=sys.stderr)

    user = args.user or conf.get("user") or conf.get("from") or ""
    return {
        "provider": provider,
        "host": args.imap_host or preset_host,
        "port": args.imap_port or int(conf.get("imap_port") or preset_port),
        "security": str(conf.get("imap_security") or preset_security).lower(),
        "user": user,
        # 与 send_mail 共用 smtp.conf 里的 password / SMTP_PASS
        "password": args.password or conf.get("password") or "",
    }


# --------------------------------------------------------------------------
# 取信
# --------------------------------------------------------------------------

def connect(account: dict[str, object]) -> imaplib.IMAP4_SSL:
    host, port = str(account["host"]), int(account["port"])
    if not host:
        die("[error] 没有 IMAP 服务器。用 --provider qq 或 --imap-host 指定。")
    if str(account["security"]) != "ssl":
        print(f"[warn] IMAP 只用 SSL 实现，忽略 security={account['security']}", file=sys.stderr)
    try:
        conn = imaplib.IMAP4_SSL(host, port)
    except OSError as exc:
        die(f"[error] 连不上 {host}:{port} —— {exc}")

    try:
        conn.login(str(account["user"]), str(account["password"]))
    except imaplib.IMAP4.error as exc:
        die(f"[error] IMAP 登录失败：{exc}\n"
            "        常见原因：1) 邮箱后台没有开启 IMAP 服务；\n"
            "                  2) 填的是登录密码而不是授权码；\n"
            "                  3) 授权码不是 16 位（QQ 邮箱）。",
            EXIT_AUTH)
    # readonly：核验不该改动邮箱任何状态（含 \\Seen）
    conn.select("INBOX", readonly=True)
    return conn


def find_uid(conn: imaplib.IMAP4_SSL, subject: str, scan: int) -> bytes | None:
    typ, data = conn.uid("search", None, "ALL")
    if typ != "OK":
        die(f"[error] 无法列出收件箱 UID：{data}")
    uids = (data[0] or b"").split()
    if not uids:
        return None
    print(f"[info] 收件箱共 {len(uids)} 封，倒序扫描最新 {min(scan, len(uids))} 封的信头")
    want = subject.strip()
    for uid in reversed(uids[-scan:]):
        typ, data = conn.uid(
            "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
        if typ != "OK":
            continue
        header = message_from_bytes(fetch_payload(data))
        if decode_value(header.get("Subject")).strip() == want:
            return uid
    return None


def show_header(conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
    typ, data = conn.uid(
        "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
    header = message_from_bytes(fetch_payload(data))
    for label, key in (("Subject", "Subject"), ("From", "From"),
                       ("Date", "Date"), ("Message-ID", "Message-ID")):
        print(f"  {label:<10}: {decode_value(header.get(key))}")


# --------------------------------------------------------------------------
# 核验
# --------------------------------------------------------------------------

def check(message: Message, daydir: Path | None, expect_inline: list[str],
          check_attachments: bool = True) -> list[str]:
    fails: list[str] = []

    def fail(msg: str) -> None:
        fails.append(msg)

    # --- 结构 -------------------------------------------------------------
    print("\n结构：")
    for depth, part in walk(message):
        print("  " + "  " * depth + describe(part))

    if not message.is_multipart():
        fail("顶层不是 multipart/*，正文里不可能有内联图")

    html_parts = [p for p in message.walk() if p.get_content_type() == "text/html"]
    has_related = any(p.get_content_type() == "multipart/related" for p in message.walk())
    has_alternative = any(p.get_content_type() == "multipart/alternative" for p in message.walk())
    if not has_alternative:
        fail("没有 multipart/alternative，收件人拿不到纯文本兜底")

    # --- cid 引用 与 内联部件 ---------------------------------------------
    inline: dict[str, Message] = {}
    attachments: dict[str, Message] = {}
    for part in message.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        name = decode_value(part.get_filename())
        if cid:
            inline[cid] = part
            if disp != "inline":
                fail(f"cid={cid} 的 Content-Disposition 是 {disp or '(缺失)'}，"
                     "客户端会当成附件而不是内嵌图")
        elif disp == "attachment":
            attachments[name] = part

    if not html_parts:
        fail("没有 text/html 部件")
        refs: list[str] = []
    else:
        html = html_parts[0].get_payload(decode=True).decode(
            html_parts[0].get_content_charset() or "utf-8", "replace")
        refs = re.findall(r'cid:([^"\'\s>)]+)', html)
        print(f"\n正文里的 cid 引用（{len(refs)} 个）：{refs}")
        if refs and not has_related:
            fail("正文引用了 cid: 但没有 multipart/related 容器，图会裂")

    for ref in refs:
        if ref in inline:
            print(f"  cid:{ref:<18} -> 有对应内联部件")
        else:
            fail(f"正文引用了 cid:{ref}，但邮件里没有这个内联部件（必裂图）")

    for cid in inline:
        if cid not in refs:
            fail(f"内联部件 cid:{cid} 没有被正文引用，白占体积")

    for name in expect_inline:
        stem = Path(name).stem
        if stem not in inline:
            fail(f"要求内嵌的 {name} 没有作为内联部件出现（cid:{stem}）")

    # --- 与磁盘逐字节比对 -------------------------------------------------
    if daydir is None:
        print("\n[info] 未给 --dir，跳过与磁盘的逐字节比对")
        return fails

    if not daydir.is_dir():
        fail(f"--dir 指向的目录不存在：{daydir}")
        return fails

    print(f"\n内联图 vs 磁盘（{daydir}）：")
    for cid, part in sorted(inline.items()):
        candidates = [p for p in daydir.iterdir() if p.is_file() and p.stem == cid]
        if not candidates:
            fail(f"cid:{cid} 在 {daydir} 里找不到对应文件（按文件名主名匹配）")
            print(f"  cid:{cid:<18} 磁盘上没有对应文件")
            continue
        blob = part.get_payload(decode=True) or b""
        disk = candidates[0].read_bytes()
        if md5(blob) == md5(disk):
            print(f"  {candidates[0].name:<20} 一致  md5={md5(disk)[:12]}  ({len(disk):,} B)")
        else:
            fail(f"cid:{cid} 与磁盘 {candidates[0].name} 内容不一致 "
                 f"（邮件 {len(blob):,} B / 磁盘 {len(disk):,} B）")
            print(f"  {candidates[0].name:<20} 不一致 邮件={md5(blob)[:12]} 磁盘={md5(disk)[:12]}")

    if attachments and not check_attachments:
        print(f"\n附件：{', '.join(sorted(attachments))}（--no-attachment-check，跳过比对）")
    elif attachments:
        print("\n附件：")
        for name, part in sorted(attachments.items()):
            size = len(part.get_payload(decode=True) or b"")
            path = daydir / name
            if not path.is_file():
                print(f"  {name:<24} {size:,} B  (磁盘上没有同名文件，无法比对)")
                continue
            blob = part.get_payload(decode=True) or b""
            disk = path.read_bytes()
            if md5(blob) == md5(disk):
                print(f"  {name:<24} {size:,} B  与磁盘一致")
            else:
                fail(f"附件 {name} 与磁盘文件内容不一致")
                print(f"  {name:<24} {size:,} B  与磁盘不一致")

    return fails


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文输出会乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    if not args.subject and not args.uid:
        die("[error] 至少要给 --subject 或 --uid 之一，否则不知道要核验哪封。")

    account = resolve_account(args)
    if not account["user"] or not account["password"]:
        die("[error] 缺少 IMAP 登录信息。检查 smtp.conf 里的 user / password，"
            "或用 --user / --password / 环境变量 SMTP_USER / SMTP_PASS 指定。")

    for attempt in range(1, max(0, args.retry) + 2):
        try:
            return verify_once(args, account)
        except imaplib.IMAP4.error as exc:
            # IMAP4.abort 是 IMAP4.error 的子类，一并接住。这类失败意味着连接的
            # 协议流被服务端弄脏了，重连是唯一的出路，不是我们请求写错了。
            if attempt <= max(0, args.retry):
                print(f"[warn] IMAP 连接中断（第 {attempt} 次）：{exc}；重连重试",
                      file=sys.stderr)
                continue
            print(f"[error] IMAP 连接反复中断，已放弃：{exc}", file=sys.stderr)
            return EXIT_IMAP
    return EXIT_IMAP


def verify_once(args: argparse.Namespace, account: dict[str, object]) -> int:
    conn = connect(account)
    try:
        uid = str(args.uid).encode() if args.uid else find_uid(
            conn, args.subject, args.scan)
        if uid is None:
            print(f"[error] 最近 {args.scan} 封里没有标题为 {args.subject!r} 的邮件。"
                  f"\n        可能还没投递到，或标题与预期不符。", file=sys.stderr)
            return EXIT_VERIFY

        print(f"\n[found] uid={uid.decode()}")
        show_header(conn, uid)

        typ, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK":
            die(f"[error] 取邮件原文失败：{data}")
        raw = fetch_payload(data)
        print(f"  原文        : {len(raw):,} bytes")

        fails = check(message_from_bytes(raw),
                      Path(args.dir) if args.dir else None,
                      args.expect_inline or [],
                      check_attachments=not args.no_attachment_check)

        print("\n" + "=" * 64)
        if fails:
            print(f"[error] 核验不通过，{len(fails)} 项：")
            for item in fails:
                print(f"  - {item}")
            return EXIT_VERIFY
        print("[ok] 核验通过：结构、cid 引用与内联图字节全部对得上")
        print("[note] 这只证明发送端做对了、服务端收下了。收件人的客户端有没有把图")
        print("       渲染出来，机器证不了——最后那一步只能人工打开邮件看。")
        return 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
