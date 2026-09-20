---
name: smtp-mail
description: 通过 SMTP 发送邮件（纯标准库 Python 脚本）。当用户需要发邮件、发送带附件的邮件、发 HTML 邮件、配置发件邮箱和 SMTP 授权码，或排查 535/553 等发信报错时使用。
---

# smtp-mail

用 `send_mail.py` 通过任一家邮箱服务商的 SMTP 服务器发信。只用 Python 标准库
（`smtplib` / `email`），本机无需安装任何第三方包。

本机 Python 必须用完整路径调用，裸 `python` 是 Windows Store 的占位程序：

```bash
PY="C:/Users/yexinyu/AppData/Local/Python/bin/python.exe"
SM="C:/Users/yexinyu/.claude/skills/smtp-mail/send_mail.py"
```

## 一、配置发件人邮箱和授权（首次必做）

### 1. 建配置文件

```bash
cd "C:/Users/yexinyu/.claude/skills/smtp-mail"
cp smtp.conf.example smtp.conf
```

然后编辑 `smtp.conf`，最少只要三行：

```ini
provider = qq                       # 服务商预设，自动带出 host/port/加密方式
user     = your_name@qq.com         # 登录账号，就是完整邮箱地址
password = abcdefghijklmnop         # SMTP 授权码，不是登录密码！
```

可选的 `from` / `from_name` 用来控制收件人看到的名字：

```ini
from      = your_name@qq.com
from_name = 张三
```

`from` 缺省等于 `user`。**注意**：部分服务商（163、腾讯企业邮等）强制要求
发件人必须与登录账号一致，填了不一致的 `from` 会报 `553`。

收件人也可以写在配置里，定时/自动化任务就不必把地址硬编码进命令行：

```ini
to = someone@example.com          # 多个用逗号分隔；命令行 --to 可覆盖
cc = other@example.com            # 可选
```

### 2. 拿到授权码

**授权码（App Password）不是邮箱登录密码**，是单独生成的一串字符，专供第三方
客户端使用。好处是能随时重置、且泄露后不影响邮箱本身。各家的拿法：

| provider | 服务器 | 端口/加密 | 授权码获取方式 |
|---|---|---|---|
| `qq` | smtp.qq.com | 465 / SSL | 邮箱 → 设置 → 账户 → 开启「POP3/SMTP服务」（需短信验证）→ 生成授权码（16 位） |
| `163` | smtp.163.com | 465 / SSL | 邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP 服务 → 新增授权密码（需短信验证） |
| `126` | smtp.126.com | 465 / SSL | 同 163 |
| `sina` | smtp.sina.com | 465 / SSL | 邮箱 → 设置 → 客户端/POP3/SMTP → 开启并生成授权码 |
| `exmail` | smtp.exmail.qq.com | 465 / SSL | 腾讯企业邮 → 管理后台开启 SMTP → 客户端专用密码 |
| `aliyun` | smtp.mxhichina.com | 465 / SSL | 阿里邮箱 → 设置 → 客户端设置 → 开启 SMTP → 生成客户端密码 |
| `gmail` | smtp.gmail.com | 587 / STARTTLS | **先开启两步验证**，再在 <https://myaccount.google.com/apppasswords> 生成 16 位应用专用密码（国内网络需能访问 Google） |
| `outlook` | smtp.office365.com | 587 / STARTTLS | 见下方注意事项 |

几点容易踩的坑：

- QQ 邮箱的授权码**只完整显示一次**，当场复制，之后只能重置。
- 163 / 126 / QQ 开启 SMTP 服务本身可能要求短信验证；有些新账号需要注册满一定
  时间才能开启。
- **Outlook / Microsoft 365 基本已停用基本身份验证**，个人账号即使有应用密码也常
  报 `535`，组织账号还需要管理员开启 SMTP AUTH。若急着发信，建议换 QQ 或 163。
- 授权码泄露了就在邮箱后台重置一次，旧码立刻失效。

### 3. 不想写配置文件时（CI、临时用）

配置优先级：**命令行参数 > 环境变量 > `smtp.conf` > provider 预设**。

```bash
export SMTP_PROVIDER=qq
export SMTP_USER=your_name@qq.com
export SMTP_PASS=abcdefghijklmnop
```

或一次性传参（注意 `--password` 会留在 shell 历史里，不推荐）：

```bash
"$PY" "$SM" --provider qq --user your_name@qq.com --to a@b.com --subject hi --body hello
```

也可以让脚本在终端里问你要授权码——加 `--ask-password`，输入不回显、不进 shell
历史：

```bash
"$PY" "$SM" --to a@b.com --subject "测试" --body "你好" --ask-password
```

授权码缺失且没加 `--ask-password` 时，脚本会直接报错退出（exit 2）并提示配置方式，
**不会**停下来等你输入。这样脚本被自动化调用时不会卡住。

## 二、发送

```bash
# 纯文本
"$PY" "$SM" --to a@b.com --subject "测试" --body "你好"

# 多收件人 / 抄送 / 密送
"$PY" "$SM" --to "a@b.com,c@d.com" --cc boss@d.com --bcc log@d.com \
            --subject "周报" --body "见正文"

# 正文来自文件；.html 结尾会作为 HTML 正文发送（同时自动生成纯文本兜底）
"$PY" "$SM" --to a@b.com --subject "日报" --body-file report.html

# 管道输入
echo "正文内容" | "$PY" "$SM" --to a@b.com --subject "你好" --body-file -

# 带附件（可重复多次）
"$PY" "$SM" --to a@b.com --subject "报表" --body "见附件" --attach out/report.pdf

# 先看看解析出的配置对不对（授权码会打码）
"$PY" "$SM" --print-config

# 只组装不发送，打印完整邮件原文，用来检查编码和附件
"$PY" "$SM" --to a@b.com --subject "测试" --body "你好" --dry-run
```

调试建议：内容有中文、附件打不开、收件人显示乱码时，一律先加 `--dry-run` 看
`Content-Type` 和 `Content-Transfer-Encoding`，确认无误再真发。

## 三、常见报错

| 报错 | 原因与处理 |
|---|---|
| `535 Authentication failed` | 用的不是授权码，或 SMTP 服务没开启，或授权码已被重置。最常见的就是填了登录密码。 |
| `553 Mail from must equal authorized user` | 发件人 `from` 与登录账号不一致。163、腾讯企业邮会强制校验，把 `from` 改成和 `user` 一样。 |
| 连接超时 / 无法连接 465 | 出站端口被网络或公司防火墙拦了。换 `--security starttls --port 587` 试试；有些公司网络把 25/465/587 全封，只能换网络或找 IT。 |
| `SMTPNotSupportedError: STARTTLS` | 服务器不支持 STARTTLS。改用 465 + `ssl`。 |
| `554` / 被当垃圾邮件 | 新账号有发信频率限制，或正文里链接/敏感词太多。降低频率、别群发。 |
| 收件人看到的正文或附件名乱码 | 本脚本已统一用 UTF-8 + base64 编码；若仍乱码，用 `--dry-run` 核对邮件原文。 |

## 说明

- 脚本只负责发信，不保存任何发送记录。
- `smtp.conf` 里是明文授权码。别把该目录提交进 git 仓库；泄露后到邮箱后台重置即可。
- 附件直接读进内存，发超大附件（几十 MB 以上）前建议先压缩。
