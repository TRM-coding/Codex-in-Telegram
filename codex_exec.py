"""Codex backend using `codex exec` subprocesses."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from codex_common import CodexResult, StatusCallback
from settings import Settings
from text_utils import format_codex_status, format_command_output, truncate_text


STOP_GRACE_SECONDS = 5


@dataclass
class ExecActiveJob:
    process: asyncio.subprocess.Process
    stopped: bool = False


class ExecCodexBackend:
    def __init__(self) -> None:
        self._active_jobs: dict[int, ExecActiveJob] = {}
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
        return await run_codex(
            settings,
            prompt,
            thread_id=thread_id,
            image_paths=image_paths,
            status_callback=status_callback,
            chat_id=chat_id,
            backend=self,
        )

    async def steer(self, chat_id: int, text: str) -> str:
        raise RuntimeError(
            "The exec backend cannot steer a running task. Set CODEX_BACKEND=app-server."
        )

    async def stop(self, chat_id: int) -> str:
        async with self._lock:
            job = self._active_jobs.get(chat_id)
            if job is None or job.process.returncode is not None:
                return "No running Codex task is active for this chat."
            job.stopped = True
            signal_process_group(job.process, signal.SIGINT)
        try:
            await asyncio.wait_for(job.process.wait(), timeout=STOP_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logging.warning(
                "Codex exec process did not stop within %s seconds; killing it",
                STOP_GRACE_SECONDS,
            )
            signal_process_group(job.process, signal.SIGKILL)
            await job.process.wait()
        return "Stop requested for the running Codex task."

    async def shutdown(self) -> None:
        async with self._lock:
            jobs = list(self._active_jobs.values())
        for job in jobs:
            if job.process.returncode is None:
                signal_process_group(job.process, signal.SIGTERM)

    async def register(self, chat_id: int | None, process: asyncio.subprocess.Process) -> None:
        if chat_id is None:
            return
        async with self._lock:
            self._active_jobs[chat_id] = ExecActiveJob(process)

    async def unregister(self, chat_id: int | None, process: asyncio.subprocess.Process) -> bool:
        if chat_id is None:
            return False
        async with self._lock:
            job = self._active_jobs.get(chat_id)
            if job is None or job.process is not process:
                return False
            stopped = job.stopped
            del self._active_jobs[chat_id]
            return stopped


def signal_process_group(
    process: asyncio.subprocess.Process, sig: signal.Signals
) -> None:
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except ProcessLookupError:
        return
    except OSError:
        if process.returncode is None:
            process.send_signal(sig)


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


def format_codex_progress_event(event: dict) -> str | None:
    event_type = event.get("type")

    if event_type == "thread.started":
        thread_id = event.get("thread_id")
        if thread_id:
            return f"Started session\n{thread_id}"
        return "Started a new session"

    if event_type == "turn.started":
        return "Working on the request..."

    if event_type == "turn.completed":
        return "Completed. Sending the final answer..."

    if event_type not in {"item.started", "item.updated", "item.completed"}:
        return None

    item = event.get("item")
    if not isinstance(item, dict):
        return None

    item_type = item.get("type")
    if item_type == "command_execution":
        command = str(item.get("command") or "").strip()
        command = truncate_text(command, 350)
        exit_code = item.get("exit_code")

        if event_type == "item.completed" or item.get("status") == "completed":
            status = "Finished command"
            if exit_code is not None:
                status += f" (exit {exit_code})"
        else:
            status = "Running command"

        parts = [status]
        if command:
            parts.append(command)

        output = format_command_output(str(item.get("aggregated_output") or ""))
        if output:
            parts.append(f"Latest output:\n{output}")
        return "\n\n".join(parts)

    if item_type == "agent_message":
        return "Writing the final answer..."

    if item_type in {"reasoning", "agent_reasoning"}:
        return "Reasoning..."

    if isinstance(item_type, str) and item_type:
        label = item_type.replace("_", " ")
        if event_type == "item.completed":
            return f"Completed {label}."
        return f"Working on {label}..."

    return None


async def write_process_stdin(
    process: asyncio.subprocess.Process, prompt: str
) -> None:
    if process.stdin is None:
        return

    try:
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()
        try:
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def read_codex_stdout(
    stream: asyncio.StreamReader,
    lines: list[str],
    status_callback: StatusCallback | None,
) -> None:
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break

        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        lines.append(line)

        if status_callback is None:
            continue

        try:
            event = json.loads(line.strip())
        except json.JSONDecodeError:
            continue

        status = format_codex_progress_event(event)
        if status:
            await status_callback(format_codex_status(status), False)


async def run_codex(
    settings: Settings,
    prompt: str,
    *,
    thread_id: str | None = None,
    image_paths: Iterable[Path] = (),
    status_callback: StatusCallback | None = None,
    chat_id: int | None = None,
    backend: ExecCodexBackend | None = None,
) -> CodexResult:
    image_paths = tuple(image_paths)
    codex_options = settings.codex_resume_options if thread_id else settings.codex_exec_options
    if image_paths and "--image" not in codex_options:
        return CodexResult(
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
            start_new_session=True,
        )
        if backend is not None:
            await backend.register(chat_id, process)

        stdout_lines: list[str] = []
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[bytes] | None = None

        try:
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("Codex process streams were not created")

            stdout_task = asyncio.create_task(
                read_codex_stdout(process.stdout, stdout_lines, status_callback)
            )
            stderr_task = asyncio.create_task(process.stderr.read())
            await write_process_stdin(process, prompt)
            await asyncio.wait_for(
                process.wait(),
                timeout=settings.codex_timeout_seconds,
            )
            await stdout_task
            stderr_bytes = await stderr_task
        except asyncio.TimeoutError:
            signal_process_group(process, signal.SIGKILL)
            await process.wait()
            tasks = [task for task in (stdout_task, stderr_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return CodexResult(
                124,
                "",
                f"Codex timed out after {settings.codex_timeout_seconds} seconds.",
                thread_id,
            )
        finally:
            stopped = False
            if backend is not None:
                stopped = await backend.unregister(chat_id, process)
            if stopped:
                logging.info("Codex exec process was stopped for chat_id=%s", chat_id)

        stdout = "\n".join(stdout_lines).strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        parsed_thread_id, json_answer = parse_codex_json_stdout(stdout)

        final_answer = ""
        if output_file.exists():
            final_answer = output_file.read_text(encoding="utf-8", errors="replace").strip()

        if status_callback is not None:
            await status_callback(
                format_codex_status("Completed. Sending the final answer..."), True
            )

        returncode = process.returncode or 0
        if returncode == -int(signal.SIGINT):
            return CodexResult(130, "Codex task was stopped.", "", parsed_thread_id or thread_id)
        if stopped and returncode != 0:
            return CodexResult(130, "Codex task was stopped.", "", parsed_thread_id or thread_id)

        return CodexResult(
            returncode,
            final_answer or json_answer or stdout,
            stderr,
            parsed_thread_id or thread_id,
        )
