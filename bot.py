#!/usr/bin/env python3
"""Telegram bridge for running the local Codex CLI."""

from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from codex_app_server import AppServerCodexBackend
from codex_common import CodexBackend
from codex_exec import ExecCodexBackend
from handlers import (
    codex_command,
    error_handler,
    grant_command,
    handle_image,
    handle_text,
    help_command,
    history_command,
    id_command,
    new_command,
    resume_command,
    session_command,
    start_command,
    steer_command,
    stop_command,
)
from session_store import SessionStore
from settings import GRANT_COMMAND_ALIASES, Settings, load_settings


def build_backend(settings: Settings) -> CodexBackend:
    if settings.codex_backend == "exec":
        return ExecCodexBackend()
    return AppServerCodexBackend()


async def shutdown_backend(application: Application) -> None:
    backend: CodexBackend | None = application.bot_data.get("codex_backend")
    if backend is not None:
        await backend.shutdown()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(os.environ.get("HTTPX_LOG_LEVEL", "WARNING"))

    settings = load_settings()
    application = (
        Application.builder()
        .token(settings.telegram_token)
        .concurrent_updates(True)
        .post_shutdown(shutdown_backend)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["session_store"] = SessionStore(settings.codex_session_db)
    application.bot_data["codex_semaphore"] = asyncio.Semaphore(
        settings.max_concurrent_jobs
    )
    application.bot_data["codex_backend"] = build_backend(settings)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("session", session_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("steer", steer_command))
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
        "Starting Codex Telegram bridge. Workdir: %s Session DB: %s Backend: %s",
        settings.codex_workdir,
        settings.codex_session_db,
        settings.codex_backend,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
