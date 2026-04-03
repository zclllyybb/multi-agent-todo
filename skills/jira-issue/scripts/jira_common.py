from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import click


def read_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    config.update(read_env_file(Path.home() / ".env"))

    # Shell environment should win over any file-based defaults.
    config.update(os.environ)
    return config


def resolve_value(
    cli_value: Optional[str],
    config: dict[str, str],
    env_key: str,
    required: bool = False,
) -> str:
    resolved = cli_value or config.get(env_key, "")
    if required and not resolved:
        raise click.UsageError(f"Missing required value for {env_key}.")
    return resolved


def read_text_input(file_path: Path, option_name: str) -> str:
    if file_path == Path("-"):
        return click.get_text_stream("stdin").read().strip()
    try:
        return file_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise click.UsageError(f"Missing file for {option_name}: {file_path}") from exc
