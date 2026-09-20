#!/usr/bin/env python3
"""通过 SMTP 发送邮件。仅使用标准库，无需安装任何第三方包。

配置优先级（高 -> 低）：
    命令行参数  >  环境变量  >  smtp.conf  >  provider 预设默认值
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
CONF_FILE = SKILL_DIR / "smtp.conf"

#: provider 预设。用户只需在配置里写 provider = qq，再填账号和授权码即可。
PRESETS: dict[str, dict[str, object]] = {
    "qq":      {"host": "smtp.qq.com",         "port": 465, "security": "ssl"},
    "163":     {"host": "smtp.163.com",        "port": 465, "security": "ssl"},
    "126":     {"host": "smtp.126.com",        "port": 465, "security": "ssl"},
    "sina":    {"host": "smtp.sina.com",       "port": 465, "security": "ssl"},
    "aliyun":  {"host": "smtp.mxhichina.com",  "port": 465, "security": "ssl"},
    "exmail":  {"host": "smtp.exmail.qq.com",  "port": 465, "security": "ssl"},
    "gmail":   {"host": "smtp.gmail.com",      "port": 587, "security": "starttls"},
    "outlook": {"host": "smtp.office365.com",  "port": 587, "security": "starttls"},
}

EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_SEND = 4


def die(message: str, code: int = EXIT_CONFIG) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# 配置读取
# --------------------------------------------------------------------------

def read_conf_file(path: Path) -> dict[str, str]:
    """读取 key = value 形式的配置文件，# 开头为注释。"""
    if not path.is_file():
        return {}
    conf: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if "=" not in line:
            print(f"[warn] {path.name}:{lineno} 不是 key = value，已忽略：{line}",
                  file=sys.stderr)
            continue
        key, _, value = line.partition("=")
        conf[key.strip().lower().replace("-", "_")] = value.strip().strip("'\"")
    return conf


def env_overrides() -> dict[str, str]:
    """SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM / SMTP_PROVIDER ..."""
    mapping = {
        "SMTP_PROVIDER": "provider",
        "SMTP_HOST": "host",
        "SMTP_PORT": "port",
        "SMTP_SECURITY": "security",
        "SMTP_USER": "user",
        "SMTP_PASS": "password",
        "SMTP_FROM": "from",
        "SMTP_FROM_NAME": "from_name",
        "SMTP_TO": "to",
        "SMTP_CC": "cc",
    }
    return {dst: os.environ[src] for src, dst in mapping.items() if os.environ.get(src)}


def split_addrs(value: str) -> list[str]:
    """按逗号 / 分号 / 空白切分收件人。"""
    return [a for a in re.split(r"[,;\s]+", value or "") if a]


def build_config(args: argparse.Namespace) -> dict[str, object]:
    conf = read_conf_file(Path(args.conf) if args.conf else CONF_FILE)
    conf.update(env_overrides())

    provider = (args.provider or conf.get("provider") or "").lower()
    preset = PRESETS.get(provider, {})

    def pick(cli_value, key, default=None):
        if cli_value not in (None, ""):
            return cli_value
        if conf.get(key) not in (None, ""):
            return conf[key]
        if preset.get(key) not in (None, ""):
            return preset[key]
        return default

    port = pick(args.port, "port", 465)
    security = str(pick(args.security, "security", "ssl")).lower()
    if provider and provider not in PRESETS:
        print(f"[warn] 未知 provider '{provider}'，已知：{', '.join(sorted(PRESETS))}",
              file=sys.stderr)

    cfg: dict[str, object] = {
        "provider": provider,
        "host": pick(args.host, "host"),
        "port": int(port),
        "security": security,
        "user": pick(args.user, "user") or pick(args.from_addr, "from"),
        "password": pick(args.password, "password", ""),
        "from_addr": pick(args.from_addr, "from") or pick(args.user, "user"),
        "from_name": pick(args.from_name, "from_name", ""),
        # 收件人也可以写在配置文件里，这样定时任务不必把地址硬编码在命令行
        "to": pick(args.to, "to", ""),
        "cc": pick(args.cc, "cc", ""),
    }
    return cfg


def resolve_password(cfg: dict[str, object], ask: bool) -> str:
    """授权码缺失时的处理。

    只有在显式传了 --ask-password 时才会交互询问。默认直接报错，这样脚本被
    自动化调用时不会卡在一个没人回答的提示上。
    """
    password = str(cfg.get("password") or "")
    if password:
        return password
    if not ask:
        return ""
    import getpass
    try:
        return getpass.getpass(f"请输入 {cfg['user']} 的 SMTP 授权码（不回显）：")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return ""


# --------------------------------------------------------------------------
# 邮件构造
# --------------------------------------------------------------------------

def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read_body(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (纯文本, HTML)。"""
    text = args.body or ""
    html = ""

    if args.body_file:
        # 只有显式写 --body-file - 才读 stdin，避免在无输入时阻塞
        if args.body_file == "-":
            text = sys.stdin.read()
        else:
            content = Path(args.body_file).read_text(encoding="utf-8")
            if str(args.body_file).lower().endswith((".html", ".htm")):
                html = content
            else:
                text = content
    if args.html:
        html = Path(args.html).read_text(encoding="utf-8")
    elif args.html_string:
        html = args.html_string

    if html and not text:
        text = html_to_text(html)
    return text, html


def build_message(args: argparse.Namespace, cfg: dict[str, object]) -> EmailMessage:
    text, html = read_body(args)
    to_addrs = split_addrs(str(cfg.get("to") or ""))
    cc_addrs = split_addrs(str(cfg.get("cc") or ""))
    bcc_addrs = split_addrs(args.bcc)

    if not to_addrs and not cc_addrs and not bcc_addrs:
        die("[error] 至少需要一个收件人（--to / --cc / --bcc）")

    from_addr = str(cfg["from_addr"] or "")
    from_name = str(cfg["from_name"] or "")
    if not from_addr:
        die("[error] 未配置发件人地址（smtp.conf 里的 from 或 user）")

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = args.subject or "(无主题)"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    if html:
        # 顺序敏感：set_content -> add_alternative -> add_related -> add_attachment。
        # 在 add_alternative 之前调 add_related 的话，get_payload()[-1] 还是
        # text/plain，会搭出一棵语法合法但根本没有图的 related(plain, png) 树，
        # 而且不报错——所以下面那句 elif 是必须的，不是随手加的防御。
        msg.set_content(text or "")
        msg.add_alternative(html, subtype="html")
        if args.inline:
            related = msg.get_payload()[-1]        # 就是刚加进去的 text/html 部件
            for path_str in args.inline:
                path = Path(path_str).expanduser()
                if not path.is_file():
                    raise SystemExit(f"[error] 内联图片不存在：{path}")
                if not path.stem.isascii():
                    die(f"[error] 内联图的 cid 必须是纯 ASCII，否则邮件里必定裂图："
                        f"{path.name}\n"
                        "        正文里的 cid 取自文件名，中文名会被 RFC-2047 编码成\n"
                        "        =?utf-8?b?...?= ，而客户端不会解码 Content-ID。")
                ctype, _ = mimetypes.guess_type(path.name)
                maintype, subtype = (ctype.split("/", 1) if ctype
                                     else ("application", "octet-stream"))
                # 千万别在这里传 filename=：set_content 已经写过
                # Content-Disposition，_add_multipart 不会再覆盖它，结果是
                # 静默从 inline 降级成 attachment——图不显示，也不报错。
                # 尖括号必须带，只有 <x> 才符合 RFC 2045 的 msg-id 形式。
                related.add_related(path.read_bytes(), maintype=maintype,
                                    subtype=subtype,
                                    cid="<" + path.stem + ">")
    elif args.inline:
        die("[error] --inline 需要同时提供 HTML 正文（--html）；"
            "否则图片没有任何地方引用它")
    else:
        msg.set_content(text or "")

    for path_str in args.attach or []:
        path = Path(path_str).expanduser()
        if not path.is_file():
            raise SystemExit(f"[error] 附件不存在：{path}")
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype.split("/", 1) if ctype else ("application", "octet-stream"))
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                           filename=path.name)

    return msg


def envelope_recipients(cfg: dict[str, object], args: argparse.Namespace) -> list[str]:
    """Bcc 不能出现在头里，但必须在信封收件人里。"""
    return (split_addrs(str(cfg.get("to") or ""))
            + split_addrs(str(cfg.get("cc") or ""))
            + split_addrs(args.bcc))


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------

def send(msg: EmailMessage, cfg: dict[str, object], recipients: list[str]) -> None:
    host, port = str(cfg["host"] or ""), int(cfg["port"])
    security = str(cfg["security"])
    if not host:
        die("[error] 未配置 SMTP 服务器地址（provider 或 host）")

    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
    except (OSError, smtplib.SMTPException) as exc:
        die(f"[error] 无法连接 {host}:{port}（{security}）：{exc}", EXIT_SEND)

    # 登录单独处理。认证被拒时服务器往往先回一个 535，然后在 smtplib 换用下一种
    # AUTH 机制时直接掐断连接；若跟 send_message 共用一个 try，最后抛出的是
    # 「连接被关闭」，真正的原因（535 授权码错误）就被盖掉了。
    try:
        if cfg["user"] and cfg["password"]:
            server.login(str(cfg["user"]), str(cfg["password"]))
        elif cfg["user"]:
            print("[warn] 没有授权码，跳过登录（仅对免认证的中继有效）", file=sys.stderr)
    except smtplib.SMTPAuthenticationError as exc:
        die(f"[error] 认证失败（{getattr(exc, 'smtp_code', '?')}）。请确认使用的是"
            f"「SMTP 授权码 / 应用专用密码」，而不是邮箱登录密码。\n"
            f"        服务器返回：{exc}", EXIT_AUTH)
    except smtplib.SMTPServerDisconnected as exc:
        die("[error] 认证阶段被服务器断开，授权码几乎肯定不对。常见原因：\n"
            "        1) 填的是邮箱登录密码，而不是邮箱后台生成的「SMTP 授权码」\n"
            "        2) 该邮箱还没有开启 IMAP/SMTP 服务\n"
            f"        服务器返回：{exc}", EXIT_AUTH)

    try:
        server.send_message(msg, from_addr=str(cfg["from_addr"]), to_addrs=recipients)
    except smtplib.SMTPException as exc:
        die(f"[error] 发送失败：{exc}", EXIT_SEND)
    finally:
        try:
            server.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="通过 SMTP 发送邮件（仅标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  send_mail.py --to a@b.com --subject 测试 --body 你好\n"
               "  send_mail.py --to a@b.com --subject 日报 --body-file body.html --attach out.pdf\n"
               "  echo '正文' | send_mail.py --to a@b.com --subject 你好 --body-file -\n",
    )
    p.add_argument("--to", help="收件人，多个用逗号分隔（缺省读 smtp.conf 的 to）")
    p.add_argument("--cc", help="抄送（缺省读 smtp.conf 的 cc）")
    p.add_argument("--bcc", help="密送")
    p.add_argument("--subject", help="主题")
    p.add_argument("--body", help="纯文本正文")
    p.add_argument("--body-file", help="从文件读取正文；以 - 表示 stdin；.html 结尾按 HTML 处理")
    p.add_argument("--html", help="HTML 正文文件路径")
    p.add_argument("--html-string", help="直接给 HTML 正文")
    p.add_argument("--attach", action="append", help="附件路径，可重复")
    p.add_argument("--inline", action="append",
                   help="内联图片路径，可重复；cid 取文件名（须纯 ASCII），"
                        "需与 HTML 正文里的 cid:xxx 一致，且必须配合 --html")

    g = p.add_argument_group("SMTP 配置（覆盖 smtp.conf）")
    g.add_argument("--provider", help=f"预设：{', '.join(sorted(PRESETS))}")
    g.add_argument("--host", help="SMTP 服务器")
    g.add_argument("--port", type=int, help="端口，默认 465")
    g.add_argument("--security", choices=["ssl", "starttls"], help="加密方式")
    g.add_argument("--user", help="登录账号（通常就是邮箱地址）")
    g.add_argument("--password", help="授权码；建议改用 smtp.conf 或交互输入")
    g.add_argument("--from-addr", dest="from_addr", help="发件人地址（默认同 user）")
    g.add_argument("--from-name", dest="from_name", help="发件人显示名")
    g.add_argument("--conf", help=f"配置文件路径，默认 {CONF_FILE}")

    p.add_argument("--ask-password", action="store_true",
                   help="授权码缺失时在终端交互输入（默认不询问，避免脚本被卡住）")
    p.add_argument("--dry-run", action="store_true", help="只打印邮件内容，不发送")
    p.add_argument("--print-config", action="store_true", help="打印解析后的配置（隐藏授权码）")
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文日志会乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    cfg = build_config(args)
    cfg["password"] = resolve_password(cfg, ask=args.ask_password)

    if args.print_config:
        masked = dict(cfg)
        masked["password"] = "***" if cfg["password"] else "(未配置)"
        for key, value in masked.items():
            print(f"{key:>10} = {value}")
        if not args.to and not args.subject:
            return 0

    # --dry-run 只组装不发送，没有配置也要能用——否则「先 dry-run 检查编码和
    # 附件」这条最常用的调试路径反而最先被配置卡住
    if args.dry_run and not cfg["from_addr"]:
        cfg["from_addr"] = "dry-run@localhost"

    if not args.dry_run and not cfg["password"]:
        die("[error] 没有 SMTP 授权码。请任选一种方式配置：\n"
            f"        1) 在 {CONF_FILE} 里写 password = <授权码>\n"
            "        2) 设置环境变量 SMTP_PASS\n"
            "        3) 加 --ask-password 在终端里手动输入\n"
            "        （注意：这里要填邮箱后台生成的「SMTP 授权码」，不是登录密码）")

    msg = build_message(args, cfg)
    recipients = envelope_recipients(cfg, args)

    if args.dry_run:
        print(f"--- dry-run ---")
        print(f"server   : {cfg['host']}:{cfg['port']} ({cfg['security']})")
        print(f"login    : {cfg['user']}")
        print(f"envelope : {', '.join(recipients)}")
        print("---- 邮件原文 ----")
        print(msg.as_string())
        return 0

    send(msg, cfg, recipients)
    print(f"[ok] 已发送给 {', '.join(recipients)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
