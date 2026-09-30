"""Exact-origin URL resolution for testers and engine requests."""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urljoin
from urllib.parse import urlsplit

from av_config import ConfigError
from av_config import Origin
from av_config import is_loopback
from av_config import parse_origin


class Targets:
    """Resolve only configured HTTP origins, with non-loopback trust gates."""

    def __init__(self, targets: Mapping[str, str], *, trusted: bool) -> None:
        self.targets = dict(targets)
        self.trusted = trusted
        self.origins = {parse_origin(url, origin_only=True) for url in targets.values()}

    def allowed(self, origin: str | Origin) -> bool:
        try:
            parsed = parse_origin(origin) if isinstance(origin, str) else origin
        except ConfigError:
            return False
        return parsed in self.origins and (is_loopback(parsed[1]) or self.trusted)

    def resolve(self, url_or_path: str, target: str) -> str:
        """Resolve a relative URL, or refuse an absolute off-target URL."""
        try:
            parts = urlsplit(url_or_path)
        except ValueError as error:
            raise ConfigError("off-target URL refused") from error
        if parts.netloc and not parts.scheme:
            raise ConfigError("off-target URL refused")
        if parts.scheme:
            url = url_or_path
        else:
            if target not in self.targets:
                raise ConfigError("undefined target")
            url = urljoin(self.targets[target] + "/", url_or_path)
        try:
            origin = parse_origin(url)
        except ConfigError as error:
            raise ConfigError("off-target URL refused") from error
        if not self.allowed(origin):
            raise ConfigError("off-target URL refused")
        return url
