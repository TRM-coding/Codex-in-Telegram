"""Telegram command and message handlers."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from codex_common import CodexBackend, CodexResult
from session_store import SessionStore
from settings import Settings, is_authorized
from text_utils import (
    chunk_text,
    format_codex_status,
    format_recovery_suggestions,
    format_permission_status,
    format_response,
    format_timestamp,
    grant_usage,
    help_text,
)


DEFAULT_IMAGE_PROMPT = "请分析这张图片。"
TYPING_REFRESH_SECONDS = 4
STATUS_UPDATE_SECONDS = 2.0
UPLOAD_DIR_NAME = ".codex-telegram/uploads"
IMAGE_DOCUMENT_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


@dataclass(frozen=True)
class DownloadedImage:
    path: Path
    source: str
    note: str


def register_chat_request(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    request_id = int(context.application.bot_data.get("codex_request_counter", 0)) + 1
    context.application.bot_data["codex_request_counter"] = request_id
    requests_by_chat = context.application.bot_data.setdefault("codex_requests_by_chat", {})
    requests_by_chat.setdefault(chat_id, set()).add(request_id)
    return request_id


def unregister_chat_request(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, request_id: int
) -> None:
    requests_by_chat = context.application.bot_data.setdefault("codex_requests_by_chat", {})
    request_ids = requests_by_chat.get(chat_id)
    if request_ids is not None:
        request_ids.discard(request_id)
        if not request_ids:
            requests_by_chat.pop(chat_id, None)

    stopped_by_chat = context.application.bot_data.setdefault("codex_stopped_requests", {})
    stopped_ids = stopped_by_chat.get(chat_id)
    if stopped_ids is not None:
        stopped_ids.discard(request_id)
        if not stopped_ids:
            stopped_by_chat.pop(chat_id, None)


def stop_chat_requests(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    requests_by_chat = context.application.bot_data.setdefault("codex_requests_by_chat", {})
    request_ids = set(requests_by_chat.get(chat_id) or ())
    if not request_ids:
        return 0

    stopped_by_chat = context.application.bot_data.setdefault("codex_stopped_requests", {})
    stopped_by_chat.setdefault(chat_id, set()).update(request_ids)
    return len(request_ids)


def is_chat_request_stopped(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, request_id: int
) -> bool:
    stopped_by_chat = context.application.bot_data.setdefault("codex_stopped_requests", {})
    return request_id in stopped_by_chat.get(chat_id, set())


def update_user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def update_chat_id(update: Update) -> int | None:
    return update.effective_chat.id if update.effective_chat else None


def authorized(settings: Settings, update: Update) -> bool:
    return is_authorized(settings, update_user_id(update))


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


async def edit_status_message(message, text: str) -> None:
    try:
        await message.edit_text(text, disable_web_page_preview=True)
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        logging.exception("Telegram rejected status update")
    except TelegramError:
        logging.exception("Failed to update Telegram status message")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    logging.info(
        "Received /start from user_id=%s chat_id=%s",
        user.id if user else "unknown",
        update_chat_id(update) or "unknown",
    )

    if settings.allowed_user_ids:
        auth_status = "authorized" if authorized(settings, update) else "not authorized"
    else:
        auth_status = "not configured"

    text = (
        "Codex Telegram bridge is running.\n\n"
        f"Your Telegram user id: {user.id if user else 'unknown'}\n"
        f"Access status: {auth_status}\n"
        f"Codex backend: {settings.codex_backend}\n\n"
        "Send a plain text message or use /codex followed by your request.\n"
        "Use /help to see all commands."
    )
    if not settings.allowed_user_ids:
        text += "\n\nSet TELEGRAM_ALLOWED_USER_IDS before enabling Codex commands."

    await send_text(update, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info(
        "Received /help from user_id=%s chat_id=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
    )
    await send_text(update, help_text())


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info(
        "Received /id from user_id=%s chat_id=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
    )
    await send_text(
        update,
        "User id: {user_id}\nChat id: {chat_id}".format(
            user_id=update_user_id(update) or "unknown",
            chat_id=update_chat_id(update) or "unknown",
        ),
    )


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["session_store"]
    chat = update.effective_chat

    logging.info(
        "Received /new from user_id=%s chat_id=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
    )

    if not authorized(settings, update):
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

    logging.info(
        "Received /session from user_id=%s chat_id=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
    )

    if not authorized(settings, update):
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

    logging.info(
        "Received /history from user_id=%s chat_id=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
    )

    if not authorized(settings, update):
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
    thread_id = context.args[0].strip() if context.args else ""

    logging.info(
        "Received /resume from user_id=%s chat_id=%s thread_id_len=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
        len(thread_id),
    )

    if not authorized(settings, update):
        await send_text(update, "You are not authorized to use this bot.")
        return

    if chat is None:
        await send_text(update, "No chat is available for this update.")
        return

    if not thread_id:
        await send_text(update, "Usage: /resume <session_id>")
        return

    store.set_thread_id(chat.id, update_user_id(update), thread_id)
    await send_text(update, f"Switched current Codex session to:\n{thread_id}")


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    mode = context.args[0].strip().lower() if context.args else "status"

    logging.info(
        "Received /grant from user_id=%s chat_id=%s mode=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
        mode,
    )

    if not authorized(settings, update):
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
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
        len(prompt),
    )
    if not prompt:
        await send_text(update, "Usage: /codex <your request>")
        return
    await handle_codex_prompt(update, context, prompt)


async def steer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    backend: CodexBackend = context.application.bot_data["codex_backend"]
    chat = update.effective_chat
    text = " ".join(context.args).strip()

    logging.info(
        "Received /steer from user_id=%s chat_id=%s text_len=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
        len(text),
    )

    if not authorized(settings, update):
        await send_text(update, "You are not authorized to use this bot.")
        return
    if chat is None:
        await send_text(update, "No chat is available for this update.")
        return
    if not text:
        await send_text(update, "Usage: /steer <instruction>")
        return

    try:
        message = await backend.steer(chat.id, text)
    except Exception as exc:
        logging.exception("Failed to steer Codex turn")
        message = f"Failed to steer Codex: {exc}"
    await send_text(update, message)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    backend: CodexBackend = context.application.bot_data["codex_backend"]
    chat = update.effective_chat

    logging.info(
        "Received /stop from user_id=%s chat_id=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
    )

    if not authorized(settings, update):
        await send_text(update, "You are not authorized to use this bot.")
        return
    if chat is None:
        await send_text(update, "No chat is available for this update.")
        return

    stopped_requests = stop_chat_requests(context, chat.id)
    try:
        message = await backend.stop(chat.id)
    except Exception as exc:
        logging.exception("Failed to stop Codex turn")
        message = f"Failed to stop Codex: {exc}"

    if stopped_requests and message.startswith("No running Codex"):
        message = (
            f"Stop requested. Cancelled {stopped_requests} queued Codex "
            "request(s) for this chat."
        )
    elif stopped_requests:
        message = (
            f"{message}\n\n"
            f"Also marked {stopped_requests} queued/current Codex request(s) "
            "for this chat as stopped."
        )
    await send_text(update, message)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    logging.info(
        "Received text from user_id=%s chat_id=%s text_len=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
        len(message.text),
    )
    await handle_codex_prompt(update, context, message.text.strip())


def sanitize_filename(name: str) -> str:
    sanitized = "".join(
        char if char.isalnum() or char in {".", "-", "_"} else "_" for char in name
    ).strip("._")
    return sanitized or "image"


def is_supported_image_document(document) -> bool:
    mime_type = document.mime_type or ""
    if mime_type.startswith("image/"):
        return True
    suffix = Path(document.file_name or "").suffix.lower()
    return suffix in IMAGE_DOCUMENT_EXTENSIONS


def format_uploaded_image_context(images: Iterable[DownloadedImage]) -> str:
    lines = [
        "",
        "Telegram uploaded image files are available on disk for this turn:",
    ]
    for index, image in enumerate(images, start=1):
        lines.append(f"{index}. {image.path} ({image.source}; {image.note})")
    lines.append(
        "If the user asks to use or copy the original image file, use these local paths directly."
    )
    return "\n".join(lines)


async def download_message_images(message, target_dir: Path) -> list[DownloadedImage]:
    target_dir.mkdir(parents=True, exist_ok=True)
    images: list[DownloadedImage] = []

    if message.photo:
        image_path = target_dir / f"telegram-photo-{message.message_id}.jpg"
        photo = message.photo[-1]
        telegram_file = await photo.get_file()
        await telegram_file.download_to_drive(custom_path=str(image_path))
        dimensions = (
            f"{photo.width}x{photo.height}"
            if photo.width and photo.height
            else "unknown size"
        )
        images.append(
            DownloadedImage(
                image_path,
                "Telegram photo",
                f"largest available Telegram photo variant, usually compressed, {dimensions}",
            )
        )

    document = message.document
    if document and is_supported_image_document(document):
        suffix = Path(document.file_name or "").suffix
        if not suffix:
            suffix = mimetypes.guess_extension(document.mime_type or "") or ".img"
        filename = sanitize_filename(
            document.file_name or f"telegram-image-{message.message_id}{suffix}"
        )
        image_path = target_dir / filename
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=str(image_path))
        images.append(
            DownloadedImage(
                image_path,
                "Telegram document",
                "original uploaded file when the sender used Telegram file/document mode",
            )
        )

    return images


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    settings: Settings = context.application.bot_data["settings"]

    logging.info(
        "Received image from user_id=%s chat_id=%s has_caption=%s",
        update_user_id(update) or "unknown",
        update_chat_id(update) or "unknown",
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

    if not authorized(settings, update):
        logging.warning(
            "Rejected unauthorized image from user_id=%s chat_id=%s",
            update_user_id(update) or "unknown",
            update_chat_id(update) or "unknown",
        )
        await send_text(update, "You are not authorized to use this bot.")
        return

    chat = update.effective_chat
    chat_dir = str(chat.id) if chat is not None else "unknown-chat"
    target_dir = settings.codex_workdir / UPLOAD_DIR_NAME / chat_dir / str(message.message_id)
    try:
        images = await download_message_images(message, target_dir)
    except TelegramError:
        logging.exception("Failed to download Telegram image")
        await send_text(update, "Failed to download the image from Telegram.")
        return

    if not images:
        await send_text(update, "No supported image was found in this message.")
        return

    prompt = (message.caption or "").strip() or DEFAULT_IMAGE_PROMPT
    prompt = f"{prompt}\n{format_uploaded_image_context(images)}"
    await handle_codex_prompt(
        update,
        context,
        prompt,
        image_paths=[image.path for image in images],
    )


async def handle_codex_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    image_paths: Iterable[Path] = (),
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    semaphore: asyncio.Semaphore = context.application.bot_data["codex_semaphore"]
    store: SessionStore = context.application.bot_data["session_store"]
    backend: CodexBackend = context.application.bot_data["codex_backend"]

    if not settings.allowed_user_ids:
        logging.warning("Rejected Codex request because TELEGRAM_ALLOWED_USER_IDS is empty")
        await send_text(
            update,
            "Codex access is disabled until TELEGRAM_ALLOWED_USER_IDS is configured. "
            "Use /id to get your Telegram user id.",
        )
        return

    if not authorized(settings, update):
        logging.warning(
            "Rejected unauthorized user_id=%s chat_id=%s",
            update_user_id(update) or "unknown",
            update_chat_id(update) or "unknown",
        )
        await send_text(update, "You are not authorized to use this bot.")
        return

    image_paths = tuple(image_paths)
    if not prompt and not image_paths:
        await send_text(update, "Send a non-empty Codex request.")
        return

    message = update.effective_message
    chat = update.effective_chat
    chat_request_id = (
        register_chat_request(context, chat.id) if chat is not None else None
    )
    status_message = None
    thread_id = store.get_thread_id(chat.id) if chat else None
    stopped_after_run = False
    try:
        if message is not None:
            status_message = await message.reply_text(
                format_codex_status("Queued. Waiting for an available Codex slot...")
            )

        last_status_text = ""
        last_status_at = 0.0
        pending_status_text: str | None = None
        status_lock = asyncio.Lock()

        async def publish_status(text: str, force: bool = False) -> None:
            nonlocal last_status_at, last_status_text, pending_status_text
            async with status_lock:
                if status_message is None:
                    return

                pending_status_text = text
                now = time.monotonic()
                if not force and now - last_status_at < STATUS_UPDATE_SECONDS:
                    return
                if pending_status_text == last_status_text:
                    return

                await edit_status_message(status_message, pending_status_text)
                last_status_text = pending_status_text
                last_status_at = now

        typing_task = (
            asyncio.create_task(keep_typing(context, chat.id))
            if chat is not None
            else None
        )
        try:
            async with semaphore:
                if (
                    chat is not None
                    and chat_request_id is not None
                    and is_chat_request_stopped(context, chat.id, chat_request_id)
                ):
                    await publish_status(
                        format_codex_status("Stopped before Codex started."),
                        True,
                    )
                    result = CodexResult(
                        130,
                        "Codex request was stopped before it started.",
                        "",
                        thread_id,
                    )
                else:
                    await publish_status(
                        format_codex_status("Starting Codex..."),
                        True,
                    )
                    result = await backend.run(
                        settings,
                        prompt,
                        chat_id=chat.id if chat else None,
                        thread_id=thread_id,
                        image_paths=image_paths,
                        status_callback=publish_status,
                    )
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
    finally:
        stopped_after_run = (
            chat is not None
            and chat_request_id is not None
            and is_chat_request_stopped(context, chat.id, chat_request_id)
        )
        if chat is not None and chat_request_id is not None:
            unregister_chat_request(context, chat.id, chat_request_id)

    if stopped_after_run and result.returncode == 0:
        result = CodexResult(
            130,
            "Codex request was stopped.",
            "",
            result.thread_id or thread_id,
        )

    if result.returncode == 0 and chat is not None and result.thread_id:
        store.set_thread_id(chat.id, update_user_id(update), result.thread_id)

    logging.info(
        "Codex completed for user_id=%s chat_id=%s returncode=%s thread_id=%s images=%s",
        update_user_id(update) or "unknown",
        chat.id if chat else "unknown",
        result.returncode,
        result.thread_id or thread_id or "none",
        len(image_paths),
    )
    await send_text(update, format_response(result.returncode, result.answer, result.stderr))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled Telegram bot error", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Internal bot error.\n\n"
                f"{format_recovery_suggestions(1, str(context.error or ''))}"
            )
        except BadRequest:
            pass
