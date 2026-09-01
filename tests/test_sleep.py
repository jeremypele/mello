"""
Tests for SleepManager and touch-wake safety guards.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.handlers.evdev_touch import EvdevTouchHandler
from mello.managers.sleep import SleepManager


def make_sleep_manager(monkeypatch):
    monkeypatch.setattr(SleepManager, '_detect_backlight', lambda self: None)
    monkeypatch.setattr(SleepManager, '_detect_drm_connector', lambda self: None)
    monkeypatch.setattr(SleepManager, '_set_low_power_cpu', lambda self, low: None)
    monkeypatch.setattr(SleepManager, '_set_led', lambda self, on: None)
    monkeypatch.setattr(SleepManager, '_set_wifi_power_save', lambda self, on: None)
    return SleepManager()


def test_sleep_allowed_when_enabled(monkeypatch):
    mgr = make_sleep_manager(monkeypatch)
    mgr.last_activity = 100

    with patch('mello.managers.sleep.time.time', return_value=100 + 121):
        assert mgr.check_sleep(is_playing=False) is True

    assert mgr.is_sleeping is True


def test_sleep_keeps_wifi_awake(monkeypatch):
    wifi_power_save = MagicMock()
    monkeypatch.setattr(SleepManager, '_detect_backlight', lambda self: None)
    monkeypatch.setattr(SleepManager, '_detect_drm_connector', lambda self: None)
    monkeypatch.setattr(SleepManager, '_set_low_power_cpu', lambda self, low: None)
    monkeypatch.setattr(SleepManager, '_set_led', lambda self, on: None)
    monkeypatch.setattr(SleepManager, '_set_wifi_power_save', wifi_power_save)

    mgr = SleepManager()
    mgr.enter_sleep()
    mgr.wake_up()

    wifi_power_save.assert_not_called()


def test_sleep_blocked_when_disabled(monkeypatch):
    mgr = make_sleep_manager(monkeypatch)
    mgr.last_activity = 100
    mgr.disable_sleep('touch wake unavailable')

    with patch('mello.managers.sleep.time.time', return_value=100 + 121):
        assert mgr.check_sleep(is_playing=False) is False

    assert mgr.is_sleeping is False
    assert mgr.sleep_enabled is False
    assert mgr.sleep_disabled_reason == 'touch wake unavailable'


def test_disable_sleep_wakes_display(monkeypatch):
    display = MagicMock()
    monkeypatch.setattr(SleepManager, '_detect_backlight', lambda self: None)
    monkeypatch.setattr(SleepManager, '_detect_drm_connector', lambda self: None)
    monkeypatch.setattr(SleepManager, '_set_low_power_cpu', lambda self, low: None)
    monkeypatch.setattr(SleepManager, '_set_led', lambda self, on: None)
    monkeypatch.setattr(SleepManager, '_set_wifi_power_save', lambda self, on: None)
    monkeypatch.setattr(SleepManager, '_set_display', display)

    mgr = SleepManager()
    mgr.is_sleeping = True
    mgr.disable_sleep('touch read error')

    assert mgr.is_sleeping is False
    display.assert_called_with(True)


def test_touch_failure_reason_is_consumed_once():
    handler = EvdevTouchHandler(720, 1280)

    handler._mark_failed('touch read loop exited')

    assert handler.is_available is False
    assert handler.consume_failure_reason() == 'touch read loop exited'
    assert handler.consume_failure_reason() is None


def make_dimmable_manager(monkeypatch, max_brightness=255):
    """Sleep manager with a fake sysfs backlight (brightness + bl_power)."""
    fake = {'brightness': str(max_brightness), 'bl_power': '0'}

    monkeypatch.setattr(SleepManager, '_detect_backlight', lambda self: 'bl_power')
    monkeypatch.setattr(SleepManager, '_detect_brightness',
                        lambda self: ('brightness', max_brightness))
    monkeypatch.setattr(SleepManager, '_detect_drm_connector', lambda self: None)
    monkeypatch.setattr(SleepManager, '_set_low_power_cpu', lambda self, low: None)
    monkeypatch.setattr(SleepManager, '_set_led', lambda self, on: None)
    monkeypatch.setattr(SleepManager, '_read_sysfs', lambda self, path: fake.get(path))
    monkeypatch.setattr(SleepManager, '_write_sysfs',
                        lambda self, path, value: fake.__setitem__(path, value))
    monkeypatch.setattr(SleepManager, '_set_display',
                        lambda self, on: fake.__setitem__('bl_power', '0' if on else '1'))
    return SleepManager(), fake


def test_wake_restores_full_brightness_after_a_missed_restore(monkeypatch):
    """Regression: a wake that skipped the restore used to make dim permanent.

    The old code saved the level it read at enter_sleep, so once the panel was
    left dim the dim value became the new "pre-sleep" brightness and every
    later wake restored 6%. Black screen with a working touch UI, until reboot.
    """
    mgr, fake = make_dimmable_manager(monkeypatch)

    mgr.enter_sleep()
    assert int(fake['brightness']) < 255
    mgr.is_sleeping = False  # wake_up lost the race and never restored

    mgr.enter_sleep()
    mgr.wake_up()

    assert fake['brightness'] == '255'
    assert fake['bl_power'] == '0'


def test_probe_is_skipped_when_there_is_no_goodix_driver(monkeypatch):
    """None means "can't be asked" — a desktop must not trigger a rebind."""
    handler = EvdevTouchHandler(720, 1280)
    monkeypatch.setattr(EvdevTouchHandler, 'DRIVER_DIR', '/nonexistent/goodix')

    assert handler.is_controller_alive() is None


def test_probe_fails_when_the_chip_fell_off_the_bus(monkeypatch, tmp_path):
    """Driver loaded with nothing bound: deaf, and rebind is the answer."""
    handler = EvdevTouchHandler(720, 1280)
    monkeypatch.setattr(EvdevTouchHandler, 'DRIVER_DIR', str(tmp_path))
    (tmp_path / 'bind').write_text('')  # sysfs control file, not a device

    assert handler.is_controller_alive() is False


def test_driver_entry_is_remembered_for_rebinding(monkeypatch, tmp_path):
    handler = EvdevTouchHandler(720, 1280)
    monkeypatch.setattr(EvdevTouchHandler, 'DRIVER_DIR', str(tmp_path))
    (tmp_path / '10-005d').mkdir()

    assert handler._driver_entry() == '10-005d'

    # Chip drops off the bus: the name has to survive so bind has a target
    (tmp_path / '10-005d').rmdir()
    assert handler._driver_entry() is None
    assert handler._i2c_entry == '10-005d'


def _touch_probe_stub(alive_sequence):
    """Minimal stand-in for Mello, for the escalation logic only."""
    stub = MagicMock()
    stub._touch_probe_misses = 0
    stub._last_touch_rebind = 0.0
    stub.evdev_touch.is_controller_alive.side_effect = alive_sequence
    return stub


def test_one_silent_probe_does_not_rebind():
    """A single colliding I2C transfer must not rebind a healthy panel."""
    from mello.app import Mello

    stub = _touch_probe_stub([False])
    Mello._check_touch_controller(stub)

    stub._rebind_touch.assert_not_called()
    assert stub._touch_probe_misses == 1


def test_two_silent_probes_rebind_the_driver():
    from mello.app import Mello

    stub = _touch_probe_stub([False, False])
    Mello._check_touch_controller(stub)
    Mello._check_touch_controller(stub)

    stub._rebind_touch.assert_called_once()
    assert stub._touch_probe_misses == 0


def test_a_live_probe_clears_earlier_misses():
    from mello.app import Mello

    stub = _touch_probe_stub([False, True, False])
    for _ in range(3):
        Mello._check_touch_controller(stub)

    stub._rebind_touch.assert_not_called()


def test_a_malformed_driver_entry_cannot_drive_a_rebind(monkeypatch, tmp_path):
    """Unparseable entry means "can't ask", not "chip is dead"."""
    handler = EvdevTouchHandler(720, 1280)
    monkeypatch.setattr(EvdevTouchHandler, 'DRIVER_DIR', str(tmp_path))
    (tmp_path / 'not-a-chip').mkdir()

    assert handler.is_controller_alive() is None


def test_restart_forgets_a_finger_that_was_down(monkeypatch):
    """A rebind mid-touch never sees the release, and a stuck hold overrides bedtime."""
    handler = EvdevTouchHandler(720, 1280)
    handler._touching = True
    monkeypatch.setattr(EvdevTouchHandler, 'start', lambda self: True)

    assert handler.restart() is True
    assert handler.is_touching is False


def test_recovery_re_enables_sleep_that_a_failed_rebind_turned_off():
    """Otherwise one failed rebind keeps the screen lit for the rest of the session."""
    from mello.app import Mello

    stub = _touch_probe_stub([True])
    stub.evdev_touch.recover.return_value = True
    stub.sleep_manager.sleep_enabled = False
    stub.sleep_manager.sleep_disabled_reason = 'touch wake unavailable: rebind failed'

    Mello._rebind_touch(stub)

    stub.sleep_manager.enable_sleep.assert_called_once()


def test_recovery_leaves_a_deliberate_sleep_block_alone():
    """Sleep off for another reason (setup menu, dev flag) is not ours to undo."""
    from mello.app import Mello

    stub = _touch_probe_stub([True])
    stub.evdev_touch.recover.return_value = True
    stub.sleep_manager.sleep_enabled = False
    stub.sleep_manager.sleep_disabled_reason = 'wifi setup in progress'

    Mello._rebind_touch(stub)

    stub.sleep_manager.enable_sleep.assert_not_called()
