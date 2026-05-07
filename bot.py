#!/usr/bin/env python3
"""Telegram bridge for running the local Codex CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


TELEGRAM_LIMIT = 4096
RESERVED_SUFFIX = "\n\n[output truncated]"
DEFAULT_TIMEOUT_SECONDS = 900
TYPING_REFRESH_SECONDS = 4
DEFAULT_IMAGE_PROMPT = "请分析这张图片。"
GRANT_COMMAND_ALIASES = ("grant", "permission", "permit", "allow", "xxx")


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    codex_binary: str
    codex_workdir: Path
    codex_model: str | None
    codex_profile: str | None
    codex_sandbox: str
    codex_approval: str
    codex_timeout_seconds: int
    codex_session_db: Path
    codex_extra_args: tuple[str, ...]
    codex_skip_git_repo_check: bool
    codex_dangerously_bypass: bool
    codex_exec_options: frozenset[str]
    codex_resume_options: frozenset[str]
    max_concurrent_jobs: int


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value: str | None, *, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid integer value: {value!r}") from exc


def parse_allowed_user_ids(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()

    ids: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise SystemExit(
                "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers"
            ) from exc
    return frozenset(ids)


def get_codex_exec_options(codex_binary: str) -> frozenset[str]:
    return get_codex_options(codex_binary, ["exec"])


def get_codex_resume_options(codex_binary: str) -> frozenset[str]:
    return get_codex_options(codex_binary, ["exec", "resume"])


def get_codex_options(codex_binary: str, subcommand: list[str]) -> frozenset[str]:
    try:
        result = subprocess.run(
            [codex_binary, *subcommand, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()

    help_text = f"{result.stdout}\n{result.stderr}"
    return frozenset(
        option
        for option in (
            "--ask-for-approval",
            "--sandbox",
            "--skip-git-repo-check",
            "--output-last-message",
            "--color",
            "--json",
            "--image",
        )
        if option in help_text
    )


def load_settings() -> Settings:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")

    codex_binary = os.environ.get("CODEX_BINARY", "codex").strip() or "codex"
    if shutil.which(codex_binary) is None:
        raise SystemExit(f"Codex binary not found on PATH: {codex_binary!r}")

    workdir = Path(os.environ.get("CODEX_WORKDIR", os.getcwd())).expanduser().resolve()
    if not workdir.exists() or not workdir.is_dir():
        raise SystemExit(f"CODEX_WORKDIR is not a directory: {workdir}")

    session_db = Path(
        os.environ.get("CODEX_SESSION_DB", str(workdir / "codex_sessions.sqlite3"))
    ).expanduser()
    if not session_db.is_absolute():
        session_db = workdir / session_db

    timeout = parse_int(
        os.environ.get("CODEX_TIMEOUT_SECONDS"), default=DEFAULT_TIMEOUT_SECONDS
    )
    if timeout <= 0:
        raise SystemExit("CODEX_TIMEOUT_SECONDS must be greater than 0")

    max_jobs = parse_int(os.environ.get("MAX_CONCURRENT_CODEX_JOBS"), default=1)
    if max_jobs <= 0:
        raise SystemExit("MAX_CONCURRENT_CODEX_JOBS must be greater than 0")

    return Settings(
        telegram_token=token,
        allowed_user_ids=parse_allowed_user_ids(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS")
        ),
        codex_binary=codex_binary,
        codex_workdir=workdir,
        codex_model=os.environ.get("CODEX_MODEL") or None,
        codex_profile=os.environ.get("CODEX_PROFILE") or None,
        codex_sandbox=os.environ.get("CODEX_SANDBOX", "workspace-write"),
        codex_approval=os.environ.get("CODEX_APPROVAL", "never"),
        codex_timeout_seconds=timeout,
        codex_session_db=session_db.resolve(),
        codex_extra_args=tuple(shlex.split(os.environ.get("CODEX_EXTRA_ARGS", ""))),
        codex_skip_git_repo_check=parse_bool(
            os.environ.get("CODEX_SKIP_GIT_REPO_CHECK"), default=True
        ),
        codex_dangerously_bypass=parse_bool(
            os.environ.get("CODEX_DANGEROUSLY_BYPASS"), default=False
        ),
        codex_exec_options=get_codex_exec_options(codex_binary),
        codex_resume_options=get_codex_resume_options(codex_binary),
        max_concurrent_jobs=max_jobs,
    )


def is_authorized(settings: Settings, update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id in settings.allowed_user_ids


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self, *, initialized: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        if initialized:
            self._init_schema(conn)
            conn.commit()
        return conn

    def _init_db(self) -> None:
        with self._connect(initialized=True):
            pass

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                thread_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_history (
                chat_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL,
                user_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, thread_id)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO session_history (
                chat_id, thread_id, user_id, created_at, updated_at
            )
            SELECT chat_id, thread_id, user_id, created_at, updated_at
            FROM chat_sessions
            """
        )

    def get_thread_id(self, chat_id: int) -> str | None:
        with self._connect(initialized=True) as conn:
            row = conn.execute(
                "SELECT thread_id FROM chat_sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def set_thread_id(self, chat_id: int, user_id: int | None, thread_id: str) -> None:
        now = int(time.time())
        with self._connect(initialized=True) as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (chat_id, user_id, thread_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    thread_id = excluded.thread_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, user_id, thread_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO session_history (chat_id, thread_id, user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, thread_id, user_id, now, now),
            )

    def clear(self, chat_id: int) -> None:
        with self._connect(initialized=True) as conn:
            conn.execute("DELETE FROM chat_sessions WHERE chat_id = ?", (chat_id,))

    def list_history(self, chat_id: int, limit: int = 10) -> list[tuple[str, int, int]]:
        with self._connect(initialized=True) as conn:
            rows = conn.execute(
                """
                SELECT thread_id, created_at, updated_at
                FROM session_history
                WHERE chat_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]


def append_image_options(command: list[str], image_paths: Iterable[Path]) -> None:
    for image_path in image_paths:
        command.extend(["--image", str(image_path)])


def build_codex_command(
    settings: Settings, output_file: Path, image_paths: Iterable[Path] = ()
) -> list[str]:
    command = [
        settings.codex_binary,
        "exec",
    ]

    if "--color" in settings.codex_exec_options:
        command.extend(["--color", "never"])
    if "--output-last-message" in settings.codex_exec_options:
        command.extend(["--output-last-message", str(output_file)])
    if "--json" in settings.codex_exec_options:
        command.append("--json")
    if settings.codex_model:
        command.extend(["--model", settings.codex_model])
    if settings.codex_profile:
        command.extend(["--profile", settings.codex_profile])
    if settings.codex_dangerously_bypass:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        if "--sandbox" in settings.codex_exec_options:
            command.extend(["--sandbox", settings.codex_sandbox])
        if "--ask-for-approval" in settings.codex_exec_options:
            command.extend(["--ask-for-approval", settings.codex_approval])
    if (
        settings.codex_skip_git_repo_check
        and "--skip-git-repo-check" in settings.codex_exec_options
    ):
        command.append("--skip-git-repo-check")
    if settings.codex_extra_args:
        command.extend(settings.codex_extra_args)
    append_image_options(command, image_paths)

    command.extend(["--cd", str(settings.codex_workdir), "-"])
    return command


def build_codex_resume_command(
    settings: Settings,
    output_file: Path,
    thread_id: str,
    image_paths: Iterable[Path] = (),
) -> list[str]:
    command = [
        settings.codex_binary,
        "exec",
        "resume",
        thread_id,
    ]

    if "--output-last-message" in settings.codex_resume_options:
        command.extend(["--output-last-message", str(output_file)])
    if "--json" in settings.codex_resume_options:
        command.append("--json")
    if settings.codex_model:
        command.extend(["--model", settings.codex_model])
    if settings.codex_dangerously_bypass:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    if (
        settings.codex_skip_git_repo_check
        and "--skip-git-repo-check" in settings.codex_resume_options
    ):
        command.append("--skip-git-repo-check")
    append_image_options(command, image_paths)

    command.append("-")
    return command


def parse_codex_json_stdout(stdout: str) -> tuple[str | None, str]:
    thread_id: str | None = None
    final_text = ""

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "thread.started" and event.get("thread_id"):
            thread_id = str(event["thread_id"])
        elif event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final_text = text.strip()

    return thread_id, final_text


async def run_codex(
    settings: Settings,
    prompt: str,
    thread_id: str | None = None,
    image_paths: Iterable[Path] = (),
) -> tuple[int, str, str, str | None]:
    image_paths = tuple(image_paths)
    codex_options = settings.codex_resume_options if thread_id else settings.codex_exec_options
    if image_paths and "--image" not in codex_options:
        return (
            2,
            "",
            "This Codex CLI does not support image attachments. Upgrade Codex or remove the image.",
            thread_id,
        )

    with tempfile.TemporaryDirectory(prefix="codex-telegram-") as tmpdir:
        output_file = Path(tmpdir) / "last-message.txt"
        command = (
            build_codex_resume_command(settings, output_file, thread_id, image_paths)
            if thread_id
            else build_codex_command(settings, output_file, image_paths)
        )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=settings.codex_workdir,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=settings.codex_timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return (
                124,
                "",
                f"Codex timed out after {settings.codex_timeout_seconds} seconds.",
                thread_id,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        parsed_thread_id, json_answer = parse_codex_json_stdout(stdout)

        final_answer = ""
        if output_file.exists():
            final_answer = output_file.read_text(encoding="utf-8", errors="replace").strip()

        return (
            process.returncode or 0,
            final_answer or json_answer or stdout,
            stderr,
            parsed_thread_id or thread_id,
        )


def chunk_text(text: str, limit: int = TELEGRAM_LIMIT) -> Iterable[str]:
    if len(text) <= limit:
        yield text
        return

    chunk_limit = limit - len(RESERVED_SUFFIX)
    start = 0
    while start < len(text):
        end = min(start + chunk_limit, len(text))
        yield text[start:end] + (RESERVED_SUFFIX if end < len(text) else "")
        start = end


def format_response(returncode: int, answer: str, stderr: str) -> str:
    if returncode == 0:
        return answer or "(Codex completed without a final message.)"

    details = stderr or answer or "No error details were returned."
    return f"Codex failed with exit code {returncode}.\n\n{details}"


def format_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def format_permission_status(settings: Settings) -> str:
    mode = "full" if settings.codex_dangerously_bypass else settings.codex_sandbox
    return (
        "Codex permission status:\n"
        f"Mode: {mode}\n"
        f"Sandbox: {settings.codex_sandbox}\n"
        f"Approval: {settings.codex_approval}\n"
        f"Bypass approvals and sandbox: {settings.codex_dangerously_bypass}"
    )


def grant_usage(settings: Settings) -> str:
    return (
        f"{format_permission_status(settings)}\n\n"
        "Usage:\n"
        "/grant status\n"
        "/grant workspace\n"
        "/grant read-only\n"
        "/grant full\n\n"
        "`/grant full` runs Codex with "
        "`--dangerously-bypass-approvals-and-sandbox` until the bot restarts or "
        "another /grant mode is selected."
    )


def help_text() -> str:
    return (
        "Available commands:\n\n"
        "/help\n"
        "Show this help message.\n\n"
        "/start\n"
        "Show bot status and your access status.\n\n"
        "/id\n"
        "Show your Telegram user id and chat id.\n\n"
        "/codex <request>\n"
        "Send a request to Codex. Plain text messages do the same thing.\n\n"
        "Photos and image files\n"
        "Send an image with an optional caption to ask Codex about it.\n\n"
        "/session\n"
        "Show the current Codex session id for this chat.\n\n"
        "/new\n"
        "Clear the current session. The next Codex request starts a fresh session.\n\n"
        "/history\n"
        "List recent Codex sessions saved for this chat.\n\n"
        "/resume <session_id>\n"
        "Switch this chat back to a previous Codex session.\n\n"
        "/grant <status|workspace|read-only|full>\n"
        "Show or change the Codex execution permission mode.\n\n"
        "Examples:\n"
        "/codex 请解释当前项目结构\n"
        "/new\n"
        "/history\n"
        "/resume 00000000-0000-0000-0000-000000000000\n"
        "/grant full"
    )


async def send_text(update: Update, text: str) -> None:
    if update.effective_message is None:
        return
    for chunk in chunk_text(text):
        await update.effective_message.reply_text(chunk, disable_web_page_preview=True)


async def keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    while True:
        try:
            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.TYPING
            )
        except BadRequest:
            logging.exception("Telegram rejected typing action for chat_id=%s", chat_id)
            return
        except TelegramError:
            logging.exception("Failed to send typing action for chat_id=%s", chat_id)
        await asyncio.sleep(TYPING_REFRESH_SECONDS)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    logging.info(
        "Received /start from user_id=%s chat_id=%s",
        user.id if user else "unknown",
        update.effective_chat.id if update.effective_chat else "unknown",
    )

    if settings.allowed_user_ids:
        auth_status = "authorized" if is_authorized(settings, update) else "not authorized"
    else:
        auth_status = "not configured"

    text = (
        "Codex Telegram bridge is running.\n\n"
        f"Your Telegram user id: {user.id if user else 'unknown'}\n"
        f"Access status: {auth_status}\n\n"
        "Send a plain text message or use /codex followed by your request.\n"
        "Use /help to see all commands."
    )
    if not settings.allowed_user_ids:
        text += "\n\nSet TELEGRAM_ALLOWED_USER_IDS before enabling Codex commands."

    await send_text(update, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    logging.info(
        "Received /help from user_id=%s chat_id=%s",
        user.id if user else "unknown",
        chat.id if chat else "unknown",
    )
    await send_text(update, help_text())


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    logging.info(
        "Received /id from user_id=%s chat_id=%s",
        user.id if user else "unknown",
        chat.id if chat else "unknown",
    )
    await send_text(
        update,
        "User id: {user_id}\nChat id: {chat_id}".format(
            user_id=user.id if user else "unknown",
            chat_id=chat.id if chat else "unknown",
        ),
    )


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["session_store"]
    chat = update.effective_chat
    user = update.effective_user

    logging.info(
        "Received /new from user_id=%s chat_id=%s",
        user.id if user else "unknown",
        chat.id if chat else "unknown",
    )

    if not is_authorized(settings, update):
        await send_text(update, "You are not authorized to use this bot.")
        return

    if chat is None:
        await send_text(update, "No chat is available for this update.")
        return

    store.clear(chat.id)
    await send_text(update, "Started a new Codex session for this chat.")


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["session_store"]
    chat = update.effective_chat
    user = update.effective_user

    logging.info(
        "Received /session from user_id=%s chat_id=%s",
        user.id if user else "unknown",
        chat.id if chat else "unknown",
    )

    if not is_authorized(settings, update):
        await send_text(update, "You are not authorized to use this bot.")
        return

    if chat is None:
        await send_text(update, "No chat is available for this update.")
        return

    thread_id = store.get_thread_id(chat.id)
    if thread_id:
        await send_text(update, f"Current Codex session:\n{thread_id}")
    else:
        await send_text(update, "No Codex session is stored for this chat yet.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["session_store"]
    chat = update.effective_chat
    user = update.effective_user

    logging.info(
        "Received /history from user_id=%s chat_id=%s",
        user.id if user else "unknown",
        chat.id if chat else "unknown",
    )

    if not is_authorized(settings, update):
        await send_text(update, "You are not authorized to use this bot.")
        return

    if chat is None:
        await send_text(update, "No chat is available for this update.")
        return

    current_thread_id = store.get_thread_id(chat.id)
    rows = store.list_history(chat.id)
    if not rows:
        await send_text(update, "No Codex session history is stored for this chat yet.")
        return

    lines = ["Recent Codex sessions:"]
    for index, (thread_id, created_at, updated_at) in enumerate(rows, start=1):
        marker = " current" if thread_id == current_thread_id else ""
        lines.append(
            f"{index}. {thread_id}{marker}\n"
            f"   created: {format_timestamp(created_at)}\n"
            f"   updated: {format_timestamp(updated_at)}"
        )
    lines.append("\nUse /resume <session_id> to switch sessions.")
    await send_text(update, "\n".join(lines))


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["session_store"]
    chat = update.effective_chat
    user = update.effective_user
    thread_id = context.args[0].strip() if context.args else ""

    logging.info(
        "Received /resume from user_id=%s chat_id=%s thread_id_len=%s",
        user.id if user else "unknown",
        chat.id if chat else "unknown",
        len(thread_id),
    )

    if not is_authorized(settings, update):
        await send_text(update, "You are not authorized to use this bot.")
        return

    if chat is None:
        await send_text(update, "No chat is available for this update.")
        return

    if not thread_id:
        await send_text(update, "Usage: /resume <session_id>")
        return

    store.set_thread_id(chat.id, user.id if user else None, thread_id)
    await send_text(update, f"Switched current Codex session to:\n{thread_id}")


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    chat = update.effective_chat
    mode = context.args[0].strip().lower() if context.args else "status"

    logging.info(
        "Received /grant from user_id=%s chat_id=%s mode=%s",
        user.id if user else "unknown",
        chat.id if chat else "unknown",
        mode,
    )

    if not is_authorized(settings, update):
        await send_text(update, "You are not authorized to change Codex permissions.")
        return

    if mode in {"status", "show"}:
        await send_text(update, grant_usage(settings))
        return

    if mode in {"workspace", "workspace-write", "safe"}:
        new_settings = replace(
            settings,
            codex_sandbox="workspace-write",
            codex_approval="never",
            codex_dangerously_bypass=False,
        )
    elif mode in {"read-only", "readonly", "ro"}:
        new_settings = replace(
            settings,
            codex_sandbox="read-only",
            codex_approval="never",
            codex_dangerously_bypass=False,
        )
    elif mode in {"full", "danger", "danger-full-access", "bypass"}:
        new_settings = replace(
            settings,
            codex_sandbox="danger-full-access",
            codex_approval="never",
            codex_dangerously_bypass=True,
        )
    else:
        await send_text(update, grant_usage(settings))
        return

    context.application.bot_data["settings"] = new_settings
    await send_text(
        update,
        "Codex permission mode updated.\n\n"
        f"{format_permission_status(new_settings)}",
    )


async def codex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = " ".join(context.args).strip()
    logging.info(
        "Received /codex from user_id=%s chat_id=%s prompt_len=%s",
        update.effective_user.id if update.effective_user else "unknown",
        update.effective_chat.id if update.effective_chat else "unknown",
        len(prompt),
    )
    if not prompt:
        await send_text(update, "Usage: /codex <your request>")
        return
    await handle_codex_prompt(update, context, prompt)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    logging.info(
        "Received text from user_id=%s chat_id=%s text_len=%s",
        update.effective_user.id if update.effective_user else "unknown",
        update.effective_chat.id if update.effective_chat else "unknown",
        len(message.text),
    )
    await handle_codex_prompt(update, context, message.text.strip())


async def download_message_images(message, target_dir: Path) -> list[Path]:
    image_paths: list[Path] = []

    if message.photo:
        image_path = target_dir / f"telegram-photo-{message.message_id}.jpg"
        telegram_file = await message.photo[-1].get_file()
        await telegram_file.download_to_drive(custom_path=str(image_path))
        image_paths.append(image_path)

    document = message.document
    if document and document.mime_type and document.mime_type.startswith("image/"):
        suffix = Path(document.file_name or "").suffix
        if not suffix:
            suffix = mimetypes.guess_extension(document.mime_type) or ".img"
        image_path = target_dir / f"telegram-image-{message.message_id}{suffix}"
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=str(image_path))
        image_paths.append(image_path)

    return image_paths


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    settings: Settings = context.application.bot_data["settings"]

    logging.info(
        "Received image from user_id=%s chat_id=%s has_caption=%s",
        update.effective_user.id if update.effective_user else "unknown",
        update.effective_chat.id if update.effective_chat else "unknown",
        bool(message.caption),
    )

    if not settings.allowed_user_ids:
        logging.warning("Rejected image request because TELEGRAM_ALLOWED_USER_IDS is empty")
        await send_text(
            update,
            "Codex access is disabled until TELEGRAM_ALLOWED_USER_IDS is configured. "
            "Use /id to get your Telegram user id.",
        )
        return

    if not is_authorized(settings, update):
        logging.warning(
            "Rejected unauthorized image from user_id=%s chat_id=%s",
            update.effective_user.id if update.effective_user else "unknown",
            update.effective_chat.id if update.effective_chat else "unknown",
        )
        await send_text(update, "You are not authorized to use this bot.")
        return

    with tempfile.TemporaryDirectory(prefix="codex-telegram-image-") as tmpdir:
        try:
            image_paths = await download_message_images(message, Path(tmpdir))
        except TelegramError:
            logging.exception("Failed to download Telegram image")
            await send_text(update, "Failed to download the image from Telegram.")
            return

        if not image_paths:
            await send_text(update, "No supported image was found in this message.")
            return

        prompt = (message.caption or "").strip() or DEFAULT_IMAGE_PROMPT
        await handle_codex_prompt(update, context, prompt, image_paths=image_paths)


async def handle_codex_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    image_paths: Iterable[Path] = (),
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    semaphore: asyncio.Semaphore = context.application.bot_data["codex_semaphore"]
    store: SessionStore = context.application.bot_data["session_store"]

    if not settings.allowed_user_ids:
        logging.warning("Rejected Codex request because TELEGRAM_ALLOWED_USER_IDS is empty")
        await send_text(
            update,
            "Codex access is disabled until TELEGRAM_ALLOWED_USER_IDS is configured. "
            "Use /id to get your Telegram user id.",
        )
        return

    if not is_authorized(settings, update):
        logging.warning(
            "Rejected unauthorized user_id=%s chat_id=%s",
            update.effective_user.id if update.effective_user else "unknown",
            update.effective_chat.id if update.effective_chat else "unknown",
        )
        await send_text(update, "You are not authorized to use this bot.")
        return

    image_paths = tuple(image_paths)
    if not prompt and not image_paths:
        await send_text(update, "Send a non-empty Codex request.")
        return

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is not None:
        await message.reply_text("Running Codex...")

    thread_id = store.get_thread_id(chat.id) if chat else None
    typing_task = (
        asyncio.create_task(keep_typing(context, chat.id)) if chat is not None else None
    )
    try:
        async with semaphore:
            returncode, answer, stderr, new_thread_id = await run_codex(
                settings, prompt, thread_id, image_paths
            )
    finally:
        if typing_task is not None:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

    if returncode == 0 and chat is not None and new_thread_id:
        store.set_thread_id(chat.id, user.id if user else None, new_thread_id)

    logging.info(
        "Codex completed for user_id=%s chat_id=%s returncode=%s thread_id=%s images=%s",
        update.effective_user.id if update.effective_user else "unknown",
        chat.id if chat else "unknown",
        returncode,
        new_thread_id or thread_id or "none",
        len(image_paths),
    )
    await send_text(update, format_response(returncode, answer, stderr))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled Telegram bot error", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Internal bot error. Check the service logs."
            )
        except BadRequest:
            pass


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(os.environ.get("HTTPX_LOG_LEVEL", "WARNING"))

    settings = load_settings()
    application = Application.builder().token(settings.telegram_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["session_store"] = SessionStore(settings.codex_session_db)
    application.bot_data["codex_semaphore"] = asyncio.Semaphore(
        settings.max_concurrent_jobs
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("session", session_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("resume", resume_command))
    for command in GRANT_COMMAND_ALIASES:
        application.add_handler(CommandHandler(command, grant_command))
    application.add_handler(CommandHandler("codex", codex_command))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, handle_image)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_error_handler(error_handler)

    logging.info(
        "Starting Codex Telegram bridge. Workdir: %s Session DB: %s",
        settings.codex_workdir,
        settings.codex_session_db,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
