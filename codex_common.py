"""Shared Codex backend types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from settings import Settings


StatusCallback = Callable[[str, bool], Awaitable[None]]


@dataclass(frozen=True)
class CodexResult:
    returncode: int
    answer: str
    stderr: str
    thread_id: str | None


class CodexBackend(Protocol):
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
        """Run a Codex turn and return its final result."""

    async def steer(self, chat_id: int, text: str) -> str:
        """Steer the active turn for a Telegram chat."""

    async def stop(self, chat_id: int) -> str:
        """Stop the active turn for a Telegram chat."""

    async def shutdown(self) -> None:
        """Release backend resources."""
