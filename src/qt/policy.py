"""Loads policy.yaml: the numbers the code enforces."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

POLICY_PATH = Path(__file__).with_name("policy.yaml")


class Policy(dict):
    """Nested dict with attribute access: policy.stop.k_default."""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as e:
            raise AttributeError(name) from e
        return Policy(value) if isinstance(value, dict) else value


@lru_cache(maxsize=None)
def load_policy(path: str | None = None) -> Policy:
    with open(path or POLICY_PATH, encoding="utf-8") as f:
        return Policy(yaml.safe_load(f))
