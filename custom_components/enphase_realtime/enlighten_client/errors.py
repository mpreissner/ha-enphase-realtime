"""Exceptions raised by the Enlighten client."""

from __future__ import annotations


class EnlightenError(Exception):
    """Base class for everything the Enlighten client raises."""


class EnlightenConnectionError(EnlightenError):
    """Enlighten couldn't be reached, timed out, or answered with a server error."""


class EnlightenAuthError(EnlightenError):
    """The login was refused, or the session stayed rejected after one fresh login.

    Home Assistant turns this into a reauth flow. Nothing retries a refused login: repeated
    failures can lock the account (spec 4.2).
    """


class EnlightenParseError(EnlightenError, ValueError):
    """A payload didn't have the shape its parser expects."""
