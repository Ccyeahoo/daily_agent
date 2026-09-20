# 在 Windows 上搭一个无人值守的定时 Claude Code 任务

以 `bilibili_daily`（每天 09:00 抓 B站排行榜、写日报、画 5 张图、发邮件）为例，
把「让 agent 自己跑完一件事」这套做法整理成可复刻的方法。

---

## 一、它解决的是什么问题

想让 Claude Code 定时、无人值守地干一件固定的事：到点自己起来，自己把活干完，
把结果交出去，全程没有人在旁边回答问题、点「允许」。

难点不在「调用 claude」，而在四个地方：

1. **没人应答** —— 权限提示、澄清提问都可能让进程卡死到天亮；
2. **环境不干净** —— 计划任务拿到的 `PATH` 和你手敲命令时的不是同一个；
3. **编码** —— Windows 上 PowerShell 脚本、子进程输出、日志文件三处编码各不相同；
4. **凭据** —— 无人值守意味着密钥必须落在磁盘上，得接受这件事并把它管好。

---

## 二、整体结构

```
Windows 计划任务（定时触发器）
  └─ powershell -ExecutionPolicy Bypass -File run_daily.ps1
       ├─ 1. 从 .env 读 ANTHROPIC_* 注入进程环境
       ├─ 2. 把 python / npm-global 前置到 PATH
       ├─ 3. 以 UTF-8 读入 prompt_daily.txt（任务卡）
       └─ 4. Start-Process claude.cmd -p --dangerously-skip-permissions
              └─ agent 干活：抓数据 → 画图 → 写报告 → 发邮件
       └─ 5. 把 exit code、耗时、产物是否生成写进 logs\run_*.log
```

职责切得很干净：**计划任务只管准时点火，`.ps1` 只管把环境摆对并拉起 agent，
agent 的能力全部由任务卡描述**。换一件要做的事，通常只改任务卡。

---

## 三、四件套

| 文件 | 职责 | 关键要求 |
|---|---|---|
| `run_daily.ps1` | 启动器 | **纯 ASCII、无 BOM** |
| `prompt_daily.txt` | 任务卡（runbook） | UTF-8，读的时候显式指定编码 |
| `.env` | 凭据（`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`） | 明文，**不要进 git** |
| `logs\run_*.log` | 证据 | 出问题时唯一能看的东西 |

---

## 四、`run_*.ps1` 的写法要点

每一条都对应真实踩过的坑，不是风格偏好。

### 1. `.env` 逐行解析，注入 **Process** 作用域

```powershell
foreach ($line in [System.IO.File]::ReadAllLines($EnvFile, [System.Text.Encoding]::UTF8)) {
    $t = $line.Trim()
    if ($t.Length -eq 0 -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
    $i = $t.IndexOf('=')
    $k = $t.Substring(0, $i).Trim()
    $v = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
    [Environment]::SetEnvironmentVariable($k, $v, 'Process')
}
```

**为什么不用 `Get-Content` + 正则**：`.env` 里的值可能含 `=`、`#`、引号，用
`SetEnvironmentVariable` 逐行手工切比猜分隔符可靠。

**缺 token 就直接 `exit 1`**，不要带着半个配置去调 agent —— 那样失败会晚很久才暴露。

### 2. 读文件一律显式 UTF-8

```powershell
$Prompt = [System.IO.File]::ReadAllText($PromptFile, [System.Text.Encoding]::UTF8)
```

PS 5.1 的 `Get-Content` 默认按 ANSI/GBK 解码，中文任务卡会变成乱码喂给 agent。

### 3. `.ps1` 本身必须纯 ASCII、无 BOM

PS 5.1 读 `.ps1` 时，没有 BOM 就按系统 ANSI 代码页（简体中文是 GBK）解码。
脚本里写中文注释或中文字符串 → 乱码，**而且通常不报错**，症状是产物里全是
`涓□姞瀚瘑` 这类东西。

推论：**启动器不构造任何要显示的文本**。所有中文都放在 `prompt_daily.txt` 和
运行时传入的数据里。自查：

```powershell
python -c "b=open(r'run_daily.ps1','rb').read(); print('BOM',b[:3]==b'\xef\xbb\xbf','non-ascii',sum(1 for c in b if c>127))"
# 必须打印： BOM False non-ascii 0
```

### 4. 写日志要自己管 BOM

```powershell
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, $Utf8NoBom)
```

PS 5.1 的 `Add-Content -Encoding UTF8` **每次追加都写一个 BOM**，日志第一行之后
每行开头都会多出 ``，用编辑器打开看不出，`grep` 会全部失配。

### 5. PATH 要显式前置

计划任务的 `PATH` 不等于你交互式 shell 的 `PATH`。python 和 npm-global 都要
探测存在后再前置：

```powershell
$env:Path = (($pathParts + @($env:Path)) -join ';')
```

并且**先解析出可执行文件的绝对路径**再调用，别指望 `python` 这个名字能解析对。

### 6. 给子进程设 `PYTHONIOENCODING=utf-8`

```powershell
$env:PYTHONIOENCODING = 'utf-8'
```

否则子脚本往 GBK 控制台打中文会抛 `UnicodeEncodeError: 'gbk' codec can't encode...`，
一个跟业务完全无关的崩溃。

### 7. 任务卡走 **stdin**，不要走命令行参数

```powershell
$proc = Start-Process -FilePath $ClaudeCmd `
    -ArgumentList '-p', '--dangerously-skip-permissions' `
    -RedirectStandardInput  $PromptFile `
    -RedirectStandardOutput $AgentOut `
    -RedirectStandardError  $AgentErr `
    -NoNewWindow -Wait -PassThru
```

任务卡是多行长中文，经 `claude.cmd` 这个批处理 shim 的命令行传递，会在编码和
长度限制上出问题。走 stdin 则是干净的字节流。

**必须是 `Start-Process -Redirect*`，不能用 PowerShell 管道**（`... | claude`）。
管道会把子进程的输出按 PS 自己的规则再解码一遍，结果像「UTF-16 被当单字节读」，
并且会把原生的 stderr 包装成 `NativeCommandError` 记录。

### 8. 收尾要留下证据

```powershell
Write-Log ("agent finished, exit code = " + $agentExit + ", elapsed = " + $elapsed + "s")
Write-Log ("daily_brief.md exists = " + (Test-Path $Brief))
exit $agentExit
```

注意 `Test-Path` 这几行**只是日志，不是断言**——没有 `if`、没有 `exit`。想让
缺产物变成硬失败，得自己加 `if (-not (Test-Path ...)) { exit 1 }`。

---

## 五、`prompt_*.txt` 的写法要点

它是**给无人值守 agent 的 runbook**，含糊的指令会安静且昂贵地失败。要点：

1. **写清「怎么算成功」**，不要只写「做一份报告」。产物路径、文件名、必须存在的文件
   都写死。
2. **写清边界**：产物写在哪个目录、哪些文件不许改。本次任务卡里的原话：
   > 所有文件写入 `C:\demo_deploy\bilibili_daily` 下，不要写到别处。
   > 不要修改 `run_daily.ps1`、`prompt_daily.txt`、`.env`。
3. **明确禁止反问**：
   > 遇到问题时自己判断并如实说明，不要向用户提问。
   无人值守时提问等于挂起。
4. **明确禁止伪造**：
   > 不要伪造任何邮箱、授权码或"发送成功"的结论。
   并要求把**实际执行的完整命令行**和**真实退出码/stderr**原样记账。
5. **把已知的坑直接写进任务卡**，省得 agent 现场踩：本次写了 pip 无法联网、
   B站接口需要先种 `buvid3` cookie 否则返回 `-352`、大 JSON 要分块读、
   标题里的 `|` 要转义。
6. **给自检命令**，让 agent 能自己判断有没有做对（如「确认 PNG 存在且大于 0 字节」）。

任务卡是纯文本、UTF-8、无 BOM，`run_*.ps1` 用显式 UTF-8 读它。

---

## 六、注册定时任务

### 两种注册方式

```powershell
# 方式 A：schtasks（简单，但有的开关建不出来）
schtasks /create /tn "demo_bilibili_daily_report" /f /sc DAILY /st 09:00 `
    /tr "powershell -ExecutionPolicy Bypass -File C:\demo_deploy\bilibili_daily\run_daily.ps1"
```

```powershell
# 方式 B：PowerShell（能设电池策略，推荐）
$action  = New-ScheduledTaskAction -Execute 'powershell' `
    -Argument '-ExecutionPolicy Bypass -File C:\demo_deploy\bilibili_daily\run_daily.ps1'
$trigger = New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour 19 -Minute 0 -Second 0)
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'demo_bilibili_daily_report_19' `
    -Action $action -Trigger $trigger -Settings $set -Force
```

### 三个必须知道的点

**① `DisallowStartIfOnBatteries` 默认是 `true`。**
**在笔记本上，没插电时任务静默不触发，而且没有任何提示。** 这是最阴的一个坑：
你以为是 agent 挂了，其实是任务压根没启动。`schtasks` 命令行建不出这个开关，
所以用方式 B 的 `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries`。

> 参数名注意：是 `-DontStopIfGoingOnBatteries`，不是 `-DontStopIfOnBatteries`。
> 写错的话 `New-ScheduledTaskSettingsSet` 会抛 `NamedParameterNotFound`，
> 整个注册失败——脚本第一次跑就挂，反而比踩电池坑好发现。

**② 登录方式决定「看得见」还是「看不见」。**
`LogonType=InteractiveToken` 表示在用户桌面会话里跑，会弹出可见的控制台窗口
（要录屏就得用这个）。代价是**要求那一刻处于已登录状态**——锁屏可以，注销不行。

**③ 改动顺序：先建新的，确认后再删旧的。**
```powershell
Get-ScheduledTask -TaskName demo_bilibili_daily_report_19 | Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime, LastTaskResult
Unregister-ScheduledTask -TaskName demo_bilibili_daily_report -Confirm:$false
```
反过来的话，万一新任务注册失败就一个都不剩了。

### 读状态

`LastTaskResult` 常见值：`0x0` 成功 · `0x41301` 正在运行 · `0x41303` 从未运行 ·
`0x1` 通用错误 · `0x41306` 被手动终止。

---

## 七、凭据怎么管

无人值守必然要把密钥落盘，接受它，然后限制影响面：

- `.env`（`ANTHROPIC_AUTH_TOKEN`）和 `smtp.conf`（邮箱 SMTP 授权码）都是**明文**；
- 整个目录**不要进 git**；如果已经在 git 里，把这两个文件加进 `.gitignore`；
- `run_*.ps1` 打日志时**只打 `***set***`，不要打 token 本身**：

```powershell
Write-Log "ANTHROPIC_AUTH_TOKEN = ***set***"
```

- 邮箱授权码用**应用专用密码 / SMTP 授权码**，不要用登录密码。QQ 邮箱的授权码是
  **16 位**，少一位的典型症状是 `535 Login fail. Account is abnormal...`。

### 关于 `--dangerously-skip-permissions`

无人值守时它是**必须的**——没有任何人会去点「允许」。代价是 agent 可以不经确认地
读写文件、执行命令。所以：

- 任务卡里必须写死文件边界（写哪、不许改哪些）；
- 任务卡里必须写「如实汇报、不得伪造」；
- 最好让 agent 在**一个专用目录**里工作，而不是你的家目录或代码仓库根。

---

## 八、本机（Windows）环境硬约束

| 约束 | 表现 | 对策 |
|---|---|---|
| PS 5.1 按 GBK 读 `.ps1` | 中文变 `涓□姞瀚瘑`，**不报错** | `.ps1` 保持纯 ASCII、无 BOM |
| `Add-Content -Encoding UTF8` 写 BOM | 日志每行开头多 `` | `UTF8Encoding($false)` + `AppendAllText` |
| PS 管道二次解码 | 子进程输出乱码、stderr 变 `NativeCommandError` | `Start-Process -Redirect*` |
| 裸 `python` 是 Store 占位程序 | 退出码 **49**，什么都不干 | 一律用完整路径，如 `C:\Users\<u>\AppData\Local\Python\bin\python.exe` |
| pip 无网络 | `No matching distribution found` | **只用标准库**；画图走 PowerShell + System.Drawing |
| 没有 pandoc、Word COM 会挂 | 生成 .docx 失败 | 手工拼 OOXML |
| 控制台默认 GBK | 脚本打中文报 `UnicodeEncodeError` | `PYTHONIOENCODING=utf-8` + `sys.stdout.reconfigure` |

---

## 九、怎么证明它真的跑通了

**不要相信「已发送」这种打印输出。** 分三层验：

### 1. 看日志
```
logs\run_<时间戳>.log         ← 启动器自己的流水
logs\agent_<时间戳>.out.txt   ← agent 的 stdout
logs\agent_<时间戳>.err.txt   ← agent 的 stderr
```
日志末尾有 `exit code = 0` 且产物 `exists = True` 才算跑完。

### 2. 看产物
文件存在、大小合理、内容能打开。**「小于某个阈值就是空白画布」**这类判据很值钱：
本项目的图小于约 5KB 就说明渲染分支走空了。

### 3. 让外部系统作证
邮件这类「发出去就收不回」的动作，**必须回读**。用 IMAP 连接收件箱，
按 UID（不要用序列号，会偏移）以 `BODY.PEEK[]`（**不要用 `BODY[]`**，那会置
`\Seen`、改动邮箱状态并使重跑核验失效）取回，然后断言：

- 结构对不对（`multipart/mixed` → `alternative` → `related`）
- 内联图的 `Content-ID` 与正文里的引用**逐一对得上**
- 每个内联部件的 `Content-Disposition` 是 `inline`
- 内联图解码后与磁盘文件**逐字节一致**（比 md5）

本项目把这一层固化成了脚本，不必每次手写：

```powershell
python scripts\verify_mail.py --subject "B站排行榜日报 2026-09-18" `
    --dir daily_out\2026-09-18
```

它按上面这套规则做（UID + `BODY.PEEK[]` + `readonly` 选择收件箱），断言 MIME
结构、正文 `cid:` 与内联部件一一对应，再把每个内联图和附件与磁盘文件逐字节比对。
`--dir` 不给就只验结构不验字节。退出码：`0` 通过 · `5` 核验不通过 ·
`6` IMAP 连接被服务端中断（已自动重连重试）· `2`/`3` 配置/认证问题。

注意一个反直觉之处：**拿旧邮件去比对新磁盘文件是必然失败的**。重跑一次任务后
产物被覆盖，此时再验上一封邮件，字节比对会报不一致——这不是 bug，是它真的在比。

**但要注意边界**：断言 MIME 结构只能证明「发送端做对了、服务端收下了」，
**不能证明收件人的客户端把图渲染出来了**。最后那一步只能人工打开看。

---

## 十、排错对照表

| 症状 | 最可能的原因 |
|---|---|
| 到点了什么都没发生，日志也没有 | `DisallowStartIfOnBatteries`（笔记本没插电）；或未登录；或 `NextRunTime` 其实是明天 |
| 任务在跑但一直是 0x41301 | agent 卡住了；看 `agent_*.err.txt` |
| 日志里中文全是乱码 | `.ps1` 里出现了非 ASCII，或读文件没用显式 UTF-8 |
| 子进程输出乱码 / `NativeCommandError` | 用了 PS 管道而不是 `Start-Process -Redirect*` |
| `python` 立刻退出、码 49 | 撞上了 Microsoft Store 占位程序，改用完整路径 |
| 脚本报 `UnicodeEncodeError: 'gbk' codec` | 没设 `PYTHONIOENCODING=utf-8` |
| 依赖装不上 | pip 无网络，改纯标准库实现 |
| 邮件发出去了但图不显示 | 见下 |

**邮件裂图三嫌疑（按可能性排序）**：客户端的「阻止远程图片」开关 → 图片被当成
附件而不是内联（`Content-Disposition: attachment`）→ cid 含非 ASCII 字符。

---

## 十一、从零复刻的清单

1. 建目录 `<项目>\`，放 `.env`（`ANTHROPIC_*` 三个变量）。
2. 写 `run_<名>.ps1`：**纯 ASCII 无 BOM**，照第四节的 8 条要点来。
3. 写 `prompt_<名>.txt`：UTF-8，照第五节的 6 条要点来。
4. 手动跑一次：
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\demo_deploy\<项目>\run_<名>.ps1
   ```
   看日志、看产物，把任务卡改到能稳定产出为止。
5. 注册计划任务（方式 B，带 `-AllowStartIfOnBatteries`）。
6. 确认 `NextRunTime`，然后**手动触发一次**验证点火链路：
   ```powershell
   Start-ScheduledTask -TaskName <任务名>
   ```
7. 外部系统回读核验（第九节第 3 层）。
8. 收尾：加 `.gitignore`、确认日志不打印密钥、把已知坑补进任务卡。

---

## 附：本项目实例

| 项 | 值 |
|---|---|
| 任务名 | `demo_bilibili_daily_report_1930`（**当前生效**） |
| 触发 | 每天 **19:30** |
| 动作 | `powershell -ExecutionPolicy Bypass -File C:\demo_deploy\bilibili_daily\run_daily.ps1` |
| 登录方式 | `InteractiveToken`（那一刻需处于已登录状态，锁屏可以、注销不行） |
| 产物 | `daily_out\<日期>\` 下：`daily_brief.md`、`daily_brief.html`、5 张 PNG |
| 交付 | HTML 正文内嵌 5 张图（`cid:`）+ `.md` / `daily_top10.png` 两个附件 |
| 核验 | `python scripts\verify_mail.py --subject "B站排行榜日报 <日期>" --dir daily_out\<日期>` |

> **状态（2026-09-18 19:35 更新）**：早先那条 09:00 的
> `demo_bilibili_daily_report` 已注销（定义备份在
> `backup\demo_bilibili_daily_report.xml`），当前生效的是 19:30 的
> `demo_bilibili_daily_report_1930`。当天已完整验过一遍链路：手动
> `Start-ScheduledTask` 一次（exit 0）→ 19:30 定时器自行触发一次（exit 0）→
> 两封邮件都用 `scripts\verify_mail.py` 回读核验通过。
>
> 注意本机另有 `BiliRankDaily`（`pythonw.exe C:\Users\yexinyu\bili-rank\run_daily.py`，
> 每天 09:00），那是独立的纯抓取任务，与本 demo 无关。
