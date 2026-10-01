"""One masking policy for engine logs and public stop diagnostics."""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path

from av_config.errors import ConfigError
from av_config.sources import source_subset


class SecretSet:
    """Mask literal/env sources, resolved values and private run-state strings.

    Source collection never resolves file/cmd sources or executes commands. Only
    environment values referenced by env: sources are collected; public config
    and unrelated environment values remain readable. Raw and JSON-escaped
    strings are masked longest-first. Stop details include short secrets; engine
    logs collect only values at least four characters long.
    """

    def __init__(self, directory: Path, *configs: Mapping[str, object], minimum_length: int = 1) -> None:
        self._values: set[str] = set()
        self._minimum_length = minimum_length
        for config in configs:
            for source in source_subset(config).values():
                if isinstance(source, str):
                    if source.startswith("literal:") and source != "literal:***":
                        self.remember(source[8:])
                    elif source.startswith("env:"):
                        self.remember(os.environ.get(source[4:]))
        for name in ("accounts.private.json", "secrets.json"):
            try:
                value = json.loads((directory / name).read_text())
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError, ValueError) as error:
                raise ConfigError(f"{name}: private state unavailable") from error
            self.remember(value)

    def remember(self, value: object) -> None:
        """Add resolved or private values, including strings in nested objects."""
        if isinstance(value, str) and len(value) >= self._minimum_length:
            self._values.add(value)
            self._values.add(json.dumps(value, ensure_ascii=False)[1:-1])
        elif isinstance(value, Mapping):
            for item in value.values():
                self.remember(item)
        elif isinstance(value, list):
            for item in value:
                self.remember(item)

    def mask(self, text: str) -> str:
        """Replace complete known values before callers truncate or flatten text."""
        for value in sorted(self._values, key=lambda item: (-len(item), item)):
            text = text.replace(value, "***")
        return text
