"""Small helpers shared by the GLib/D-Bus services."""

import functools
import logging
import time

import dbus

log = logging.getLogger("venus_ble")


def mono():
    return time.monotonic()


def guarded(default):
    """Log and swallow exceptions from GLib/D-Bus callbacks. An exception
    escaping a GLib timeout callback removes the timer for good, so timers get
    default=True and one-shots default=False."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except Exception:
                log.exception("%s failed", getattr(fn, "__qualname__", fn))
                return default
            return default if result is None else result
        return inner
    return wrap


def dbus_error(e):
    if isinstance(e, dbus.DBusException):
        return e.get_dbus_message() or e.get_dbus_name()
    return str(e)
