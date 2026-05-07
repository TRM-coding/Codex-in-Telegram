"""Codex backend using the experimental app-server JSON-RPC protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from codex_common import CodexResult, StatusCallback
from settings import Settings
from text_utils import format_codex_status, format_command_output, truncate_text


NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


class AppServerError(RuntimeError):
    def __init__(self, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.data = data


class AppServerClient:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._request_id = 0
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._notification_handlers: list[NotificationHandler] = []
        self.generation = 0

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    async def ensure_started(self, settings: Settings) -> None:
        if self._process is not None and self._process.returncode is None:
            return

        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return

            command = [settings.codex_binary]
            if settings.codex_profile:
                command.extend(["--profile", settings.codex_profile])
            command.extend(["app-server", "--listen", "stdio://"])

            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=settings.codex_workdir,
            )
            self.generation += 1
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())

            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-to-telegram",
                        "title": "Codex to Telegram",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=30,
            )
            await self.notify("initialized")

    async def request(
        self, method: str, params: Any = None, *, timeout: float | None = 60
    ) -> Any:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerError("Codex app-server is not running.")

        self._request_id += 1
        request_id = self._request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future

        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write_json(message)
        except Exception:
            self._pending.pop(request_id, None)
            future.cancel()
            raise

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write_json(message)

    async def _write_json(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerError("Codex app-server stdin is not available.")

        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            process.stdin.write(payload.encode("utf-8") + b"\n")
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        try:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logging.warning("Ignoring non-JSON app-server stdout: %s", line)
                    continue

                await self._dispatch_message(message)
        finally:
            error = AppServerError("Codex app-server exited.")
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line_bytes = await process.stderr.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                logging.info("codex app-server: %s", line)

    async def _dispatch_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = int(message["id"])
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            if "error" in message:
                error = message.get("error") or {}
                if isinstance(error, dict):
                    future.set_exception(
                        AppServerError(
                            str(error.get("message") or "Codex app-server request failed."),
                            error.get("data"),
                        )
                    )
                else:
                    future.set_exception(AppServerError(str(error)))
            else:
                future.set_result(message.get("result"))
            return

        if "method" in message:
            for handler in self._notification_handlers:
                try:
                    await handler(message)
                except Exception:
                    logging.exception("Unhandled app-server notification handler error")
            return

        logging.debug("Ignoring app-server message: %s", message)

    async def shutdown(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@dataclass
class AppServerTurnJob:
    chat_id: int | None
    thread_id: str
    status_callback: StatusCallback | None
    done: asyncio.Future[None]
    turn_id: str | None = None
    final_text: str = ""
    error_text: str = ""
    status: str = "inProgress"
    stop_requested: bool = False
    command_outputs: dict[str, str] = field(default_factory=lambda: defaultdict(str))
    agent_message_parts: dict[str, str] = field(default_factory=lambda: defaultdict(str))

    async def publish(self, text: str, force: bool = False) -> None:
        if self.status_callback is not None:
            await self.status_callback(format_codex_status(text), force)

    async def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.turn_id = str(turn.get("id") or self.turn_id or "")
            await self.publish(f"Started turn\n{self.turn_id}")
            return

        if method == "item/started":
            await self._handle_item(params.get("item"), completed=False)
            return

        if method == "item/completed":
            await self._handle_item(params.get("item"), completed=True)
            return

        if method == "item/agentMessage/delta":
            item_id = str(params.get("itemId") or "")
            self.agent_message_parts[item_id] += str(params.get("delta") or "")
            await self.publish("Writing the final answer...")
            return

        if method == "item/commandExecution/outputDelta":
            item_id = str(params.get("itemId") or "")
            self.command_outputs[item_id] += str(params.get("delta") or "")
            return

        if method == "turn/plan/updated":
            await self.publish("Updating the plan...")
            return

        if method == "error":
            error = params.get("error")
            if isinstance(error, dict):
                self.error_text = str(error.get("message") or "Codex reported an error.")
            else:
                self.error_text = "Codex reported an error."
            await self.publish(self.error_text, True)
            return

        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.status = str(turn.get("status") or "completed")
            error = turn.get("error")
            if isinstance(error, dict):
                self.error_text = str(error.get("message") or self.error_text)
            if not self.final_text:
                self.final_text = "".join(self.agent_message_parts.values()).strip()
            await self.publish("Completed. Sending the final answer...", True)
            if not self.done.done():
                self.done.set_result(None)

    async def _handle_item(self, item: Any, *, completed: bool) -> None:
        if not isinstance(item, dict):
            return

        item_type = item.get("type")
        if item_type == "commandExecution":
            command = truncate_text(str(item.get("command") or "").strip(), 350)
            item_id = str(item.get("id") or "")
            if completed or item.get("status") == "completed":
                status = "Finished command"
                if item.get("exitCode") is not None:
                    status += f" (exit {item.get('exitCode')})"
            else:
                status = "Running command"

            output = str(item.get("aggregatedOutput") or self.command_outputs.get(item_id, ""))
            parts = [status]
            if command:
                parts.append(command)
            formatted_output = format_command_output(output)
            if formatted_output:
                parts.append(f"Latest output:\n{formatted_output}")
            await self.publish("\n\n".join(parts))
            return

        if item_type == "agentMessage":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                self.final_text = text.strip()
            await self.publish("Writing the final answer...")
            return

        if item_type == "reasoning":
            await self.publish("Reasoning...")
            return

        if isinstance(item_type, str) and item_type:
            label = item_type.replace("_", " ")
            await self.publish(f"{'Completed' if completed else 'Working on'} {label}.")


class AppServerCodexBackend:
    def __init__(self) -> None:
        self._client = AppServerClient()
        self._client.add_notification_handler(self._handle_notification)
        self._loaded_threads: set[str] = set()
        self._client_generation = 0
        self._jobs_by_chat: dict[int, AppServerTurnJob] = {}
        self._jobs_by_thread: dict[str, AppServerTurnJob] = {}
        self._lock = asyncio.Lock()

    async def run(
        self,
        settings: Settings,
        prompt: str,
        *,
        chat_id: int | None,
        thread_id: str | None = None,
        image_paths: Iterable[Path] = (),
        status_callback: StatusCallback | None = None,
    ) -> CodexResult:
        await self._client.ensure_started(settings)
        if self._client_generation != self._client.generation:
            self._loaded_threads.clear()
            self._client_generation = self._client.generation

        try:
            active_thread_id = await self._ensure_thread(settings, thread_id)
        except AppServerError as exc:
            return CodexResult(1, "", str(exc), thread_id)

        loop = asyncio.get_running_loop()
        job = AppServerTurnJob(
            chat_id=chat_id,
            thread_id=active_thread_id,
            status_callback=status_callback,
            done=loop.create_future(),
        )
        await self._register_job(job)

        try:
            await job.publish("Starting Codex...", True)
            turn = await self._start_turn(settings, job, prompt, image_paths)
            job.turn_id = str(turn.get("id") or job.turn_id or "")
            if job.stop_requested and job.turn_id:
                await self._interrupt(job)

            await asyncio.wait_for(job.done, timeout=settings.codex_timeout_seconds)
        except asyncio.TimeoutError:
            await self._interrupt(job)
            return CodexResult(
                124,
                "",
                f"Codex timed out after {settings.codex_timeout_seconds} seconds.",
                active_thread_id,
            )
        except AppServerError as exc:
            return CodexResult(1, "", str(exc), active_thread_id)
        finally:
            await self._unregister_job(job)

        if job.status == "completed":
            return CodexResult(0, job.final_text, "", active_thread_id)
        if job.status == "interrupted":
            return CodexResult(130, "Codex turn was interrupted.", "", active_thread_id)
        return CodexResult(1, "", job.error_text or f"Codex turn ended with status {job.status}.", active_thread_id)

    async def steer(self, chat_id: int, text: str) -> str:
        async with self._lock:
            job = self._jobs_by_chat.get(chat_id)
        if job is None:
            return "No running Codex turn is active for this chat."
        if not job.turn_id:
            return "Codex is still starting this turn. Try /steer again in a moment."

        result = await self._client.request(
            "turn/steer",
            {
                "threadId": job.thread_id,
                "expectedTurnId": job.turn_id,
                "input": [make_text_input(text)],
            },
        )
        if isinstance(result, dict) and result.get("turnId"):
            job.turn_id = str(result["turnId"])
        await job.publish("Steer instruction received.", True)
        return "Steer instruction sent to the running Codex turn."

    async def stop(self, chat_id: int) -> str:
        async with self._lock:
            job = self._jobs_by_chat.get(chat_id)
            if job is not None:
                job.stop_requested = True
        if job is None:
            return "No running Codex turn is active for this chat."
        if not job.turn_id:
            return "Stop requested. Codex will be interrupted once the turn starts."
        await self._interrupt(job)
        return "Stop requested for the running Codex turn."

    async def shutdown(self) -> None:
        await self._client.shutdown()

    async def _ensure_thread(self, settings: Settings, thread_id: str | None) -> str:
        if not thread_id:
            response = await self._client.request("thread/start", thread_params(settings))
            thread = response.get("thread") if isinstance(response, dict) else None
            if not isinstance(thread, dict) or not thread.get("id"):
                raise AppServerError("Codex app-server did not return a thread id.")
            new_thread_id = str(thread["id"])
            self._loaded_threads.add(new_thread_id)
            return new_thread_id

        if thread_id not in self._loaded_threads:
            params = thread_params(settings)
            params["threadId"] = thread_id
            params["excludeTurns"] = True
            await self._client.request("thread/resume", params)
            self._loaded_threads.add(thread_id)
        return thread_id

    async def _start_turn(
        self,
        settings: Settings,
        job: AppServerTurnJob,
        prompt: str,
        image_paths: Iterable[Path],
    ) -> dict[str, Any]:
        params = {
            "threadId": job.thread_id,
            "input": make_input_items(prompt, image_paths),
            "cwd": str(settings.codex_workdir),
            "approvalPolicy": settings.codex_approval,
            "sandboxPolicy": sandbox_policy(settings),
            "model": settings.codex_model,
        }
        params = {key: value for key, value in params.items() if value is not None}
        try:
            response = await self._client.request("turn/start", params)
        except AppServerError:
            self._loaded_threads.discard(job.thread_id)
            await self._ensure_thread(settings, job.thread_id)
            response = await self._client.request("turn/start", params)

        turn = response.get("turn") if isinstance(response, dict) else None
        if not isinstance(turn, dict):
            raise AppServerError("Codex app-server did not return a turn.")
        return turn

    async def _interrupt(self, job: AppServerTurnJob) -> None:
        if not job.turn_id:
            return
        try:
            await self._client.request(
                "turn/interrupt",
                {"threadId": job.thread_id, "turnId": job.turn_id},
                timeout=20,
            )
        except AppServerError as exc:
            logging.warning("Failed to interrupt Codex turn: %s", exc)

    async def _register_job(self, job: AppServerTurnJob) -> None:
        async with self._lock:
            if job.chat_id is not None:
                self._jobs_by_chat[job.chat_id] = job
            self._jobs_by_thread[job.thread_id] = job

    async def _unregister_job(self, job: AppServerTurnJob) -> None:
        async with self._lock:
            if job.chat_id is not None and self._jobs_by_chat.get(job.chat_id) is job:
                del self._jobs_by_chat[job.chat_id]
            if self._jobs_by_thread.get(job.thread_id) is job:
                del self._jobs_by_thread[job.thread_id]

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            return

        thread_id = params.get("threadId")
        if not thread_id and isinstance(params.get("thread"), dict):
            thread_id = params["thread"].get("id")
        if not thread_id:
            return

        async with self._lock:
            job = self._jobs_by_thread.get(str(thread_id))
        if job is None:
            return

        turn_id = params.get("turnId")
        if not turn_id and isinstance(params.get("turn"), dict):
            turn_id = params["turn"].get("id")
        if turn_id and job.turn_id and str(turn_id) != job.turn_id:
            return

        await job.handle_notification(method, params)


def thread_params(settings: Settings) -> dict[str, Any]:
    sandbox = "danger-full-access" if settings.codex_dangerously_bypass else settings.codex_sandbox
    params: dict[str, Any] = {
        "cwd": str(settings.codex_workdir),
        "approvalPolicy": settings.codex_approval,
        "sandbox": sandbox,
        "model": settings.codex_model,
    }
    return {key: value for key, value in params.items() if value is not None}


def sandbox_policy(settings: Settings) -> dict[str, Any]:
    if settings.codex_dangerously_bypass or settings.codex_sandbox == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if settings.codex_sandbox == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    return {
        "type": "workspaceWrite",
        "writableRoots": [str(settings.codex_workdir)],
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def make_text_input(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "text_elements": []}


def make_input_items(prompt: str, image_paths: Iterable[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if prompt:
        items.append(make_text_input(prompt))
    for image_path in image_paths:
        items.append({"type": "localImage", "path": str(image_path)})
    return items
