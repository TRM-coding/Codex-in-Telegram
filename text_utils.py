"""Text formatting helpers for Telegram messages."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from settings import Settings


TELEGRAM_LIMIT = 4096
RESERVED_SUFFIX = "\n\n[output truncated]"
STATUS_MESSAGE_LIMIT = 1200


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


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def format_command_output(output: str, *, max_lines: int = 6, max_chars: int = 500) -> str:
    output = output.strip()
    if not output:
        return ""

    lines = output.splitlines()
    if len(lines) > max_lines:
        lines = ["..."] + lines[-max_lines:]
    return truncate_text("\n".join(lines), max_chars)


def format_codex_status(text: str) -> str:
    return truncate_text(f"Codex status:\n{text.strip()}", STATUS_MESSAGE_LIMIT)


def format_response(returncode: int, answer: str, stderr: str) -> str:
    if returncode == 0:
        return answer or "(Codex completed without a final message.)"
    if returncode == 130:
        return answer or stderr or "Codex task was interrupted."

    details = stderr or answer or "No error details were returned."
    return f"Codex failed with exit code {returncode}.\n\n{details}"


def format_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def format_permission_status(settings: Settings) -> str:
    mode = "full" if settings.codex_dangerously_bypass else settings.codex_sandbox
    return (
        "Codex permission status:\n"
        f"Mode: {mode}\n"
        f"Backend: {settings.codex_backend}\n"
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
        "`/grant full` runs Codex with full filesystem access until the bot "
        "restarts or another /grant mode is selected."
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
        "/steer <instruction>\n"
        "Steer the currently running Codex turn.\n\n"
        "/stop\n"
        "Interrupt the currently running Codex turn.\n\n"
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
        "/steer 先别改代码，只给我方案\n"
        "/stop\n"
        "/new\n"
        "/history\n"
        "/resume 00000000-0000-0000-0000-000000000000\n"
        "/grant full"
    )
