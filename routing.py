"""Sender allow/trust routing helpers.

Kept dependency-free so it can be unit-tested without the heavy QR/PDF libs.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SenderRoute:
    pattern: str
    to_addrs: Optional[list[str]] = None  # None means "default recipient" (admin)


def parse_allowed_senders(specs: list[str]) -> list[str]:
    """Parse allowed sender patterns (globs)."""
    return [s.strip() for s in specs if s.strip()]


def parse_allowed_sender_routes(specs: list[str]) -> list[SenderRoute]:
    """Parse sender routing rules.

    Each entry is:
      - "<glob>=a@b.com,c@d.com"

    Whitespace is ignored around '=' and ',' separators.
    """
    routes: list[SenderRoute] = []
    for raw in specs:
        s = raw.strip()
        if not s:
            continue
        if "=" not in s:
            raise ValueError(f"invalid route rule (missing '='): {raw!r}")
        left, right = s.split("=", 1)
        pattern = left.strip()
        if not pattern:
            raise ValueError(f"invalid route rule (missing pattern): {raw!r}")
        to_addrs = [p.strip() for p in right.split(",") if p.strip()]
        if not to_addrs:
            raise ValueError(f"invalid route rule (missing recipients): {raw!r}")
        routes.append(SenderRoute(pattern=pattern, to_addrs=to_addrs))
    return routes


def matches(addr: str, pattern: str) -> bool:
    return fnmatch.fnmatch(addr.lower(), pattern.lower())


def is_allowed_sender(addr: str, allowed_patterns: list[str]) -> bool:
    return any(matches(addr, p) for p in allowed_patterns)


def find_route(addr: str, routes: list[SenderRoute]) -> Optional[SenderRoute]:
    """Return the first matching route for addr, or None."""
    for r in routes:
        if matches(addr, r.pattern):
            return r
    return None
