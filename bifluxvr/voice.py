# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""The app's two voices: funny (default) and serious.

Every user-visible string is written as t(serious, funny), so the two
versions sit side by side. Switching voices rebuilds the window."""

_serious = False


def set_serious(on):
    global _serious
    _serious = bool(on)


def is_serious():
    return _serious


def t(serious, funny):
    return serious if _serious else funny
