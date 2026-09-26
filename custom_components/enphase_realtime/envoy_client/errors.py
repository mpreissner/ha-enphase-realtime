"""Exceptions raised by the Envoy client."""

from __future__ import annotations


class EnvoyError(Exception):
    """Base class for everything the Envoy client raises."""


class EnvoyConnectionError(EnvoyError):
    """The Envoy (or Entrez) couldn't be reached, timed out, or answered with a server error."""


class EnvoyAuthError(EnvoyError):
    """The owner token was refused and a fresh one didn't help, or Entrez wouldn't issue one."""


class EnvoyStreamUnavailable(EnvoyError):
    """`/stream/meter` answered 401 or 404 with a valid token: this Envoy won't stream to us.

    Setup skips the stream entities and power falls back to `livedata` (spec 3.1).
    """


class EnvoyParseError(EnvoyError, ValueError):
    """A payload didn't have the shape its parser expects."""
