"""Configuration loader for pipeline YAML and .env integration."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML config file with ${ENV_VAR} interpolation.

    Environment variables in the form ${VAR_NAME} or ${VAR_NAME:-default}
    are substituted at load time.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed config dict with env vars resolved.
    """
    with open(path, encoding="utf-8") as f:
        raw = f.read()

    resolved = _interpolate_env(raw)
    return yaml.safe_load(resolved)


def _interpolate_env(text: str) -> str:
    """Replace ${VAR} and ${VAR:-default} with environment variable values."""
    def _replace(match: re.Match) -> str:
        full = match.group(1)
        if ":-" in full:
            var, default = full.split(":-", 1)
            return os.environ.get(var.strip(), default.strip())
        return os.environ.get(full, "")

    # Match ${...} patterns (non-greedy, stop at first })
    return re.sub(r'\$\{([^}]+)\}', _replace, text)
