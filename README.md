<div align="center">

# Codex to Telegram

Run your local Codex CLI from Telegram, keep Codex sessions per chat, and receive the final answer back on your phone.

[中文](#中文) · [English](#english)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=flat-square&logo=telegram&logoColor=white)
![Codex](https://img.shields.io/badge/Codex-CLI-111827?style=flat-square)

</div>

---

## 中文

### 项目介绍

**Codex to Telegram** 是一个把 Telegram Bot 和本地 Codex CLI 连接起来的轻量桥接插件。配置完成后，你可以直接在 Telegram 里给 Codex 发送任务，Codex 会在指定工作目录中执行请求，并把最终回答返回到 Telegram。

它适合把 Codex 放在开发机、服务器或家用主机上长期运行，然后用手机或 Telegram 客户端随时发起代码分析、修改、排查和解释任务。

### 它可以做什么

| 能力 | 说明 |
| --- | --- |
| 通过 Telegram 调用 Codex | 发送普通文本消息，或使用 `/codex <request>` 发起 Codex 请求。 |
| 保持连续会话 | 每个 Telegram chat 会保存自己的 Codex session，后续消息会自动 resume。 |
| 会话管理 | 使用 `/session` 查看当前 session，`/new` 开启新会话，`/history` 查看历史，`/resume` 恢复指定会话。 |
| 用户访问控制 | 只有 `TELEGRAM_ALLOWED_USER_IDS` 中的 Telegram 用户可以运行 Codex。 |
| 权限模式切换 | 使用 `/grant` 在只读、工作区写入和完整访问模式之间切换。 |
| 长任务友好 | 支持超时配置、并发限制和 Telegram 长消息自动分段。 |
| 适合常驻运行 | 可通过 systemd 部署成后台服务。 |

### 工作原理

```text
Telegram message
       |
       v
Python Telegram bot
       |
       v
codex exec --cd "$CODEX_WORKDIR" -
       |
       v
Final Codex answer
       |
       v
Telegram reply
```

首次请求会创建一个新的 Codex session。之后同一个 Telegram chat 中的请求会通过 `codex exec resume <session_id> -` 继续之前的上下文。

### 环境要求

- Python 3.10 或更高版本
- 已安装并可在 `PATH` 中访问的 Codex CLI
- 一个 Telegram Bot Token
- 允许访问该 Bot 的 Telegram 用户 ID

### 安装

```bash
git clone https://github.com/TRM-coding/Codex-in-Telegram.git
cd Codex-in-Telegram

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 配置

1. 在 Telegram 中通过 [@BotFather](https://t.me/BotFather) 创建 Bot，并复制 Bot Token。
2. 先只配置 `TELEGRAM_BOT_TOKEN`，启动一次机器人。
3. 给机器人发送 `/id`，获取你的 Telegram user id。
4. 把 user id 写入 `TELEGRAM_ALLOWED_USER_IDS`，重新启动机器人。

最小配置示例：

```bash
export TELEGRAM_BOT_TOKEN="123456:your-telegram-bot-token"
export TELEGRAM_ALLOWED_USER_IDS="123456789"
export CODEX_WORKDIR="/path/to/your/project"
```

启动：

```bash
python bot.py
```

### Telegram 命令

| 命令 | 说明 |
| --- | --- |
| `/start` | 查看机器人状态和当前用户授权状态。 |
| `/help` | 显示命令帮助。 |
| `/id` | 显示 Telegram user id 和 chat id。 |
| `/codex <request>` | 向 Codex 发送请求。普通文本消息也会作为 Codex 请求处理。 |
| `/session` | 查看当前 chat 绑定的 Codex session id。 |
| `/new` | 清除当前 session，下次请求会开启新会话。 |
| `/history` | 查看当前 chat 最近保存的 Codex sessions。 |
| `/resume <session_id>` | 切换回指定 Codex session。 |
| `/grant status` | 查看当前 Codex 权限模式。 |
| `/grant workspace` | 使用 `workspace-write` sandbox。 |
| `/grant read-only` | 使用只读 sandbox。 |
| `/grant full` | 使用 `--dangerously-bypass-approvals-and-sandbox`。 |

`/permission`、`/permit`、`/allow` 和 `/xxx` 是 `/grant` 的别名。

### 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 无 | Telegram Bot Token，必填。 |
| `TELEGRAM_ALLOWED_USER_IDS` | 空 | 允许使用 Codex 的 Telegram user id，多个 ID 用逗号分隔。为空时 Codex 命令会被禁用。 |
| `CODEX_BINARY` | `codex` | Codex CLI 可执行文件名或路径。 |
| `CODEX_WORKDIR` | 当前目录 | Codex 执行任务的工作目录。 |
| `CODEX_MODEL` | 空 | 传给 Codex CLI 的模型参数。 |
| `CODEX_PROFILE` | 空 | 传给 Codex CLI 的 profile 参数。 |
| `CODEX_SANDBOX` | `workspace-write` | Codex sandbox 模式。 |
| `CODEX_APPROVAL` | `never` | Codex approval 模式，适合 Telegram 非交互执行。 |
| `CODEX_TIMEOUT_SECONDS` | `900` | 单次 Codex 请求的最长运行时间。 |
| `CODEX_SESSION_DB` | `CODEX_WORKDIR/codex_sessions.sqlite3` | 保存 Telegram chat 与 Codex session 映射的 SQLite 文件。 |
| `CODEX_EXTRA_ARGS` | 空 | 附加到 `codex exec` 的额外参数。 |
| `CODEX_SKIP_GIT_REPO_CHECK` | `true` | 如果 Codex CLI 支持，自动添加 `--skip-git-repo-check`。 |
| `CODEX_DANGEROUSLY_BYPASS` | `false` | 启动时是否绕过 Codex approvals 和 sandbox。只应在隔离环境中使用。 |
| `MAX_CONCURRENT_CODEX_JOBS` | `1` | 同时运行的 Codex 任务数量。 |
| `LOG_LEVEL` | `INFO` | 应用日志级别。 |
| `HTTPX_LOG_LEVEL` | `WARNING` | HTTPX 日志级别。 |

### systemd 部署示例

创建 `/etc/systemd/system/codex-telegram.service`：

```ini
[Unit]
Description=Codex Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your-linux-user
Group=your-linux-group
WorkingDirectory=/opt/codex-to-telegram
EnvironmentFile=/opt/codex-to-telegram/.env
ExecStart=/opt/codex-to-telegram/.venv/bin/python -u /opt/codex-to-telegram/bot.py
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codex-telegram.service
sudo systemctl status codex-telegram.service --no-pager
sudo journalctl -u codex-telegram.service -f
```

### 安全提示

- 不要把 `TELEGRAM_BOT_TOKEN` 提交到 GitHub。
- 严格限制 `TELEGRAM_ALLOWED_USER_IDS`，不要把 Bot 暴露给不可信用户。
- `CODEX_WORKDIR` 中的文件可能被 Codex 读取或修改，部署前请确认目录范围。
- 谨慎使用 `/grant full` 或 `CODEX_DANGEROUSLY_BYPASS=1`，它会绕过 Codex approvals 和 sandbox。
- 如果 Bot Token 泄露，请立刻在 BotFather 中重新生成。

---

## English

### Overview

**Codex to Telegram** is a lightweight bridge between a Telegram bot and your local Codex CLI. Once configured, you can send Codex tasks from Telegram, run them inside a chosen working directory, and receive the final Codex answer back in the chat.

It is useful when Codex runs on a development machine, server, or home lab box, while you control it from your phone or any Telegram client.

### Features

| Feature | Description |
| --- | --- |
| Run Codex from Telegram | Send a plain text message, or use `/codex <request>` to start a Codex task. |
| Persistent sessions | Each Telegram chat keeps its own Codex session and resumes it automatically. |
| Session management | Use `/session`, `/new`, `/history`, and `/resume` to inspect and switch sessions. |
| User allowlist | Only Telegram users listed in `TELEGRAM_ALLOWED_USER_IDS` can run Codex. |
| Runtime permission control | Use `/grant` to switch between read-only, workspace-write, and full-access modes. |
| Long-task friendly | Supports request timeouts, concurrency limits, and Telegram message chunking. |
| Service ready | Can be deployed as a long-running systemd service. |

### How It Works

```text
Telegram message
       |
       v
Python Telegram bot
       |
       v
codex exec --cd "$CODEX_WORKDIR" -
       |
       v
Final Codex answer
       |
       v
Telegram reply
```

The first request creates a new Codex session. Later requests in the same Telegram chat continue the context with `codex exec resume <session_id> -`.

### Requirements

- Python 3.10 or newer
- Codex CLI installed and available on `PATH`
- A Telegram Bot Token
- The Telegram user ID that should be allowed to use the bot

### Installation

```bash
git clone https://github.com/TRM-coding/Codex-in-Telegram.git
cd Codex-in-Telegram

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the Bot Token.
2. Start the bot once with only `TELEGRAM_BOT_TOKEN` configured.
3. Send `/id` to the bot to get your Telegram user ID.
4. Add that user ID to `TELEGRAM_ALLOWED_USER_IDS`, then restart the bot.

Minimal example:

```bash
export TELEGRAM_BOT_TOKEN="123456:your-telegram-bot-token"
export TELEGRAM_ALLOWED_USER_IDS="123456789"
export CODEX_WORKDIR="/path/to/your/project"
```

Start the bot:

```bash
python bot.py
```

### Telegram Commands

| Command | Description |
| --- | --- |
| `/start` | Show bot status and your authorization status. |
| `/help` | Show the command reference. |
| `/id` | Show your Telegram user ID and chat ID. |
| `/codex <request>` | Send a request to Codex. Plain text messages are also treated as Codex requests. |
| `/session` | Show the current Codex session ID for this chat. |
| `/new` | Clear the current session. The next request starts a fresh session. |
| `/history` | List recent Codex sessions saved for this chat. |
| `/resume <session_id>` | Switch back to a specific Codex session. |
| `/grant status` | Show the current Codex permission mode. |
| `/grant workspace` | Use the `workspace-write` sandbox. |
| `/grant read-only` | Use the read-only sandbox. |
| `/grant full` | Use `--dangerously-bypass-approvals-and-sandbox`. |

`/permission`, `/permit`, `/allow`, and `/xxx` are aliases for `/grant`.

### Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | none | Telegram Bot Token. Required. |
| `TELEGRAM_ALLOWED_USER_IDS` | empty | Comma-separated Telegram user IDs allowed to run Codex. Codex commands are disabled when empty. |
| `CODEX_BINARY` | `codex` | Codex CLI binary name or path. |
| `CODEX_WORKDIR` | current directory | Working directory where Codex runs. |
| `CODEX_MODEL` | empty | Model argument passed to Codex CLI. |
| `CODEX_PROFILE` | empty | Profile argument passed to Codex CLI. |
| `CODEX_SANDBOX` | `workspace-write` | Codex sandbox mode. |
| `CODEX_APPROVAL` | `never` | Codex approval mode, suitable for non-interactive Telegram execution. |
| `CODEX_TIMEOUT_SECONDS` | `900` | Maximum runtime for one Codex request. |
| `CODEX_SESSION_DB` | `CODEX_WORKDIR/codex_sessions.sqlite3` | SQLite file used to map Telegram chats to Codex sessions. |
| `CODEX_EXTRA_ARGS` | empty | Extra arguments appended to `codex exec`. |
| `CODEX_SKIP_GIT_REPO_CHECK` | `true` | Adds `--skip-git-repo-check` when supported by the Codex CLI. |
| `CODEX_DANGEROUSLY_BYPASS` | `false` | Start with approvals and sandbox bypassed. Use only in an isolated environment. |
| `MAX_CONCURRENT_CODEX_JOBS` | `1` | Number of Codex jobs allowed to run at the same time. |
| `LOG_LEVEL` | `INFO` | Application log level. |
| `HTTPX_LOG_LEVEL` | `WARNING` | HTTPX log level. |

### systemd Example

Create `/etc/systemd/system/codex-telegram.service`:

```ini
[Unit]
Description=Codex Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your-linux-user
Group=your-linux-group
WorkingDirectory=/opt/codex-to-telegram
EnvironmentFile=/opt/codex-to-telegram/.env
ExecStart=/opt/codex-to-telegram/.venv/bin/python -u /opt/codex-to-telegram/bot.py
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Enable the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codex-telegram.service
sudo systemctl status codex-telegram.service --no-pager
sudo journalctl -u codex-telegram.service -f
```

### Security Notes

- Never commit `TELEGRAM_BOT_TOKEN` to GitHub.
- Keep `TELEGRAM_ALLOWED_USER_IDS` restricted to trusted users.
- Codex may read or modify files inside `CODEX_WORKDIR`; choose that directory carefully.
- Be careful with `/grant full` and `CODEX_DANGEROUSLY_BYPASS=1`; they bypass Codex approvals and sandboxing.
- Regenerate the token in BotFather immediately if it leaks.

---

<div align="center">

Built for people who want to use Codex from wherever Telegram is available.

</div>
