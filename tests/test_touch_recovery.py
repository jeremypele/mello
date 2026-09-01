"""
Tests for touch-controller recovery — the dark screen that ignores every tap.

The panel has no interrupt line: the kernel polls the chip and clears its
coordinate-buffer flag on each read. When that polling stops, the chip still
answers a product-id probe, so only the buffer flag tells us taps are going
nowhere.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import threading
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.app import Mello


class FakeTouch:
    def __init__(self, alive=True, unread=False):
        self.alive = alive
        self.unread = unread
        self.wake_event = threading.Event()
        self.device_name = 'Goodix Capacitive TouchScreen'
        self.device_path = '/dev/input/event2'
        self.rebinds = 0
        self.recovers_to = True

    def is_controller_alive(self):
        return self.alive

    def has_unread_touch(self):
        return self.unread

    def recover(self):
        self.rebinds += 1
        self.unread = False
        return self.recovers_to


def _app(touch: FakeTouch, sleeping=True) -> Mello:
    app = Mello.__new__(Mello)
    app.evdev_touch = touch
    app.sleep_manager = SimpleNamespace(
        is_sleeping=sleeping,
        sleep_enabled=True,
        sleep_disabled_reason=None,
        enable_sleep=MagicMock(),
    )
    app.quiet_hours = SimpleNamespace(active=False)
    app._touch_probe_misses = 0
    app._last_touch_rebind = 0.0
    app._wake_from_sleep = MagicMock()
    app._disable_sleep_for_touch = MagicMock()
    return app


def test_unread_touch_while_asleep_rebinds_and_wakes():
    """The reported bug: chip answers, nobody collects, screen stays dark."""
    touch = FakeTouch(alive=True, unread=True)
    app = _app(touch)
    app._check_touch_controller()
    assert touch.rebinds == 1
    app._wake_from_sleep.assert_called_once()


def test_touch_that_did_get_through_is_not_a_wedge():
    """A real tap raises wake_event within a poll — don't rebind on it."""
    touch = FakeTouch(alive=True, unread=True)
    touch.wake_event.set()
    app = _app(touch)
    app._check_touch_controller()
    assert touch.rebinds == 0
    app._wake_from_sleep.assert_not_called()


def test_awake_panel_is_not_checked_for_unread_touches():
    """Awake, a held finger keeps the flag busy and wake_event is stale."""
    touch = FakeTouch(alive=True, unread=True)
    app = _app(touch, sleeping=False)
    app._check_touch_controller()
    assert touch.rebinds == 0


def test_a_chip_that_stays_wedged_does_not_loop_on_rebinds():
    touch = FakeTouch(alive=True, unread=True)
    app = _app(touch)
    app._check_touch_controller()
    touch.unread = True
    app._check_touch_controller()
    assert touch.rebinds == 1
