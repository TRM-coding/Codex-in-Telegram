"""Runtime settings for the Telegram bridge."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 900
GRANT_COMMAND_ALIASES = ("grant", "permission", "permit", "allow", "xxx")
SUPPORTED_BACKENDS = {"app-server", "exec"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    codex_binary: str
    codex_backend: str
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

    codex_backend = os.environ.get("CODEX_BACKEND", "app-server").strip() or "app-server"
    if codex_backend not in SUPPORTED_BACKENDS:
        raise SystemExit(
            "CODEX_BACKEND must be one of: " + ", ".join(sorted(SUPPORTED_BACKENDS))
        )

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
        codex_backend=codex_backend,
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


def is_authorized(settings: Settings, user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.allowed_user_ids
