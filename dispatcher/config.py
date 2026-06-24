"""Config loading with ${ENV_VAR} expansion."""
from __future__ import annotations

import os
import re

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str) -> dict:
    """Load YAML config and expand ${ENV_VAR} references from the environment."""
    with open(path, "r", encoding="utf-8") as fh:
        return _expand(yaml.safe_load(fh) or {})
