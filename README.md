# Codex to Telegram

Run local Codex CLI requests from a Telegram bot.

## Setup

1. Create a bot with Telegram `@BotFather` and copy the token.
2. Create and activate a Python environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Create your local environment file:

   ```bash
   cp .env.example .env
   nano .env
   ```

4. Fill `TELEGRAM_BOT_TOKEN`.
5. Start the bot once and send `/id` to it:

   ```bash
   set -a
   source .env
   set +a
   python bot.py
   ```

6. Put your numeric user id into `TELEGRAM_ALLOWED_USER_IDS`, then restart the bot.

## Usage

Send a normal text message to the bot, or use:

```text
/codex explain this repository
```

Conversation state is persisted per Telegram chat. The first request creates a Codex session, and later requests in the same chat resume it.

```text
/help
/start
/id
/codex <request>
/session
/new
/history
/resume <session_id>
```

Use `/help` in Telegram to show the command reference. Use `/session` to show the current Codex session id. Use `/new` to clear the current session and start a fresh conversation on the next request. Use `/history` to list recent sessions for the chat, and `/resume <session_id>` to switch back to a previous session.

The script runs:

```bash
codex exec --cd "$CODEX_WORKDIR" -
codex exec resume "$THREAD_ID" -
```

The Telegram message is passed to Codex through stdin. The final Codex answer is sent back to Telegram.

## Important Settings

- `TELEGRAM_ALLOWED_USER_IDS`: comma-separated Telegram user ids allowed to run Codex. Codex commands are disabled when this is empty.
- `CODEX_WORKDIR`: directory where Codex runs.
- `CODEX_SANDBOX`: defaults to `workspace-write`.
- `CODEX_APPROVAL`: defaults to `never`, which is suitable for non-interactive Telegram execution.
- `CODEX_TIMEOUT_SECONDS`: maximum runtime per request.
- `CODEX_SESSION_DB`: SQLite file used to persist Telegram chat to Codex session mappings.
- `CODEX_DANGEROUSLY_BYPASS`: set to `1` only if the machine is otherwise isolated and you accept the risk.
- `CODEX_EXTRA_ARGS`: extra arguments appended before `--cd`.

## systemd Example

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

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codex-telegram.service
sudo systemctl status codex-telegram.service --no-pager
sudo journalctl -u codex-telegram.service -f
```

## Security Notes

This bot can ask Codex to read and modify files in `CODEX_WORKDIR`. Keep `TELEGRAM_ALLOWED_USER_IDS` restricted, do not share your bot token, and avoid `CODEX_DANGEROUSLY_BYPASS=1` unless you understand the consequences.
