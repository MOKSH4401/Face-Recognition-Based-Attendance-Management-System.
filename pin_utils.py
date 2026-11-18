"""Cryptographically secure rotating 6-digit PIN (HMAC time window)."""
import hashlib
import hmac
import secrets
import time

from attendance_config import PIN_PERIOD_SECONDS


def new_pin_secret():
    return secrets.token_hex(32)


def _window(at=None, period=None):
    period = int(period or PIN_PERIOD_SECONDS)
    now = int(at if at is not None else time.time())
    return now // period, period - (now % period), period


def pin_for_window(pin_secret, session_id, window):
    if not pin_secret:
        return None
    material = f"{session_id}:{int(window)}".encode("utf-8")
    digest = hmac.new(str(pin_secret).encode("utf-8"), material, hashlib.sha256).digest()
    number = int.from_bytes(digest[:4], "big") % 1000000
    return f"{number:06d}"


def current_pin(pin_secret, session_id, at=None, period=None):
    window, remaining, period = _window(at=at, period=period)
    return pin_for_window(pin_secret, session_id, window), remaining, period


def verify_pin(submitted, pin_secret, session_id, at=None, period=None):
    """Accept the current window or the previous window (rotation clock skew)."""
    if submitted is None:
        return False
    digits = "".join(ch for ch in str(submitted) if ch.isdigit())
    if len(digits) != 6:
        return False
    window, _remaining, period = _window(at=at, period=period)
    for candidate_window in (window, window - 1):
        expected = pin_for_window(pin_secret, session_id, candidate_window)
        if expected and hmac.compare_digest(digits, expected):
            return True
    return False
