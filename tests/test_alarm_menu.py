"""Alarm menu taps: what each control does to the alarm underneath it."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.managers.alarms import AlarmManager
from mello.managers.setup_menu import SetupMenu
from mello.models import Alarm, MenuState


class Rect:
    """Stand-in for pygame.Rect that only the tapped key collides with."""

    def __init__(self, hit):
        self.hit = hit

    def collidepoint(self, x, y):
        return self.hit


def taps(*keys):
    """button_rects where exactly `keys[0]` is under the finger."""
    return {k: Rect(i == 0) for i, k in enumerate(keys)}


class FakeSettings:
    def __init__(self, alarms=None):
        self.alarms = alarms or []

    def save_alarms(self):
        pass


class FakePlayer:
    def __init__(self):
        self.plays = []

    def play(self, sound):
        self.plays.append(sound)
        return True

    def stop(self):
        pass


def _menu(alarms=None):
    player = FakePlayer()
    manager = AlarmManager(FakeSettings(alarms or []), player=player)
    menu = SetupMenu(
        catalog_manager=None, settings=manager.settings,
        on_toast=lambda msg: None, on_invalidate=lambda: None,
        on_library_cleared=lambda: None, alarm_manager=manager,
    )
    return menu, manager, player


def _editing(alarm):
    """Open the editor on `alarm`. Returns the draft, not the stored object."""
    menu, manager, player = _menu([alarm])
    menu._open_alarm_edit(alarm)
    return menu, manager, player, menu.alarm_edit


def tap(menu, key, *others):
    menu.handle_tap((0, 0), taps(key, *others))


# --- List ---

def test_add_opens_a_draft_without_creating_anything():
    menu, manager, _ = _menu()
    menu.state = MenuState.ALARM_LIST
    tap(menu, 'alarm_add')

    assert menu.state == MenuState.ALARM_EDIT
    assert menu.alarm_edit is not None
    assert menu.alarm_is_new is True
    assert manager.alarms == [], 'Add must not create an alarm until Save'


def test_backing_out_of_a_new_alarm_leaves_nothing_behind():
    menu, manager, _ = _menu()
    menu.state = MenuState.ALARM_LIST
    tap(menu, 'alarm_add')
    tap(menu, 'alarm_hour_plus')
    tap(menu, 'close')

    assert manager.alarms == []
    assert menu.alarm_edit is None
    assert menu.state == MenuState.ALARM_LIST


def test_saving_a_new_alarm_commits_it():
    menu, manager, _ = _menu()
    menu.state = MenuState.ALARM_LIST
    tap(menu, 'alarm_add')
    tap(menu, 'alarm_hour_plus')
    tap(menu, 'alarm_save')

    assert len(manager.alarms) == 1
    assert manager.alarms[0].hour == 8
    assert menu.state == MenuState.ALARM_LIST
    assert menu.alarm_edit is None


def test_toggle_arms_and_disarms_without_opening_the_editor():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    menu, _, _ = _menu([alarm])
    menu.state = MenuState.ALARM_LIST

    tap(menu, 'alarm_toggle_aa')
    assert alarm.enabled is False
    assert menu.state == MenuState.ALARM_LIST

    tap(menu, 'alarm_toggle_aa')
    assert alarm.enabled is True


def test_tapping_a_row_opens_that_alarm():
    first = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    second = Alarm(id='bb', hour=8, minute=0, days=[1], repeat=True)
    menu, _, _ = _menu([first, second])
    menu.state = MenuState.ALARM_LIST

    tap(menu, 'alarm_open_bb')
    assert menu.alarm_edit is not None
    assert menu.alarm_edit.id == 'bb'
    assert menu.alarm_edit is not second, 'editor must hold a copy, not the live alarm'


# --- Time stepping ---

@pytest.mark.parametrize('key,start,expected', [
    ('alarm_hour_plus', 7, 8),
    ('alarm_hour_minus', 7, 6),
    ('alarm_hour_plus', 23, 0),      # wraps rather than sticking
    ('alarm_hour_minus', 0, 23),
])
def test_hour_steps_and_wraps(key, start, expected):
    alarm = Alarm(id='aa', hour=start, minute=0, days=[0], repeat=True)
    menu, _, _, draft = _editing(alarm)
    tap(menu, key)
    assert draft.hour == expected
    assert alarm.hour == start, 'stored alarm changed before Save'


@pytest.mark.parametrize('key,start,expected', [
    ('alarm_minute_plus', 0, 5),
    ('alarm_minute_minus', 5, 0),
    ('alarm_minute_plus', 55, 0),
    ('alarm_minute_minus', 0, 55),
])
def test_minute_steps_by_five_and_wraps(key, start, expected):
    alarm = Alarm(id='aa', hour=7, minute=start, days=[0], repeat=True)
    menu, _, _, draft = _editing(alarm)
    tap(menu, key)
    assert draft.minute == expected
    assert alarm.minute == start, 'stored alarm changed before Save'


def test_minute_wrap_does_not_change_the_hour():
    alarm = Alarm(id='aa', hour=7, minute=55, days=[0], repeat=True)
    menu, _, _, draft = _editing(alarm)
    tap(menu, 'alarm_minute_plus')
    assert (draft.hour, draft.minute) == (7, 0)


# --- Days, repeat, sound ---

def test_day_chip_toggles_on_and_off():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    menu, _, _, draft = _editing(alarm)

    tap(menu, 'alarm_day_5')
    assert draft.days == [0, 5]

    tap(menu, 'alarm_day_0')
    assert draft.days == [5]
    assert alarm.days == [0], 'chip taps leaked into the stored alarm'


def test_days_stay_sorted_however_they_are_tapped():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[], repeat=True)
    menu, _, _, draft = _editing(alarm)
    for day in (4, 0, 2):
        tap(menu, f'alarm_day_{day}')
    assert draft.days == [0, 2, 4]


def test_repeat_toggle_pins_a_date_when_switched_off():
    alarm = Alarm(id='aa', hour=9, minute=0, days=[5], repeat=True)
    menu, manager, _, draft = _editing(alarm)

    tap(menu, 'alarm_repeat')
    assert draft.repeat is False
    tap(menu, 'alarm_save')
    saved = manager.alarms[0]
    assert saved.repeat is False
    assert saved.date is not None      # resolved to a real day, not left floating


def test_repeat_toggle_clears_the_date_when_switched_back_on():
    alarm = Alarm(id='aa', hour=9, minute=0, days=[5], repeat=False,
                  date='2026-08-08')
    menu, manager, _, draft = _editing(alarm)

    tap(menu, 'alarm_repeat')
    tap(menu, 'alarm_save')
    saved = manager.alarms[0]
    assert saved.repeat is True
    assert saved.date is None


def test_sound_cycles_and_plays_a_preview():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True, sound='marimba')
    menu, _, player, draft = _editing(alarm)

    tap(menu, 'alarm_sound')
    assert draft.sound == 'riser'
    assert player.plays == ['riser'], 'the Sound row must be audible, not just a label'


def test_editing_a_disarmed_alarm_arms_it():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True, enabled=False)
    menu, manager, _, _ = _editing(alarm)
    tap(menu, 'alarm_hour_plus')
    tap(menu, 'alarm_save')
    assert manager.alarms[0].enabled is True


# --- Delete ---

def test_delete_asks_before_it_deletes():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    menu, manager, _, _ = _editing(alarm)

    tap(menu, 'alarm_delete')
    assert menu.alarm_delete_pending is True
    assert manager.alarms == [alarm], 'first tap must not delete'

    tap(menu, 'alarm_delete')
    assert manager.alarms == []
    assert menu.state == MenuState.ALARM_LIST
    assert menu.alarm_edit is None


def test_any_other_tap_cancels_a_pending_delete():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    menu, manager, _, _ = _editing(alarm)

    tap(menu, 'alarm_delete')
    tap(menu, 'alarm_hour_plus')
    assert menu.alarm_delete_pending is False

    tap(menu, 'alarm_delete')
    assert manager.alarms == [alarm], 'delete went through without a fresh confirm'


# --- Navigation ---

def test_back_from_the_editor_returns_to_the_list():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    menu, _, _, _ = _editing(alarm)

    tap(menu, 'close')
    assert menu.state == MenuState.ALARM_LIST
    assert menu.alarm_edit is None


def test_back_from_the_list_returns_to_settings():
    menu, _, _ = _menu()
    menu.state = MenuState.ALARM_LIST
    tap(menu, 'close')
    assert menu.state == MenuState.MAIN


def test_settings_row_opens_the_alarm_list():
    menu, _, _ = _menu()
    menu.state = MenuState.MAIN
    tap(menu, 'alarms')
    assert menu.state == MenuState.ALARM_LIST


# --- The live one-shot date shown in the editor ---

def test_editor_reports_the_date_a_one_shot_will_land_on():
    alarm = Alarm(id='aa', hour=9, minute=0, days=[5], repeat=False)
    menu, _, _, _ = _editing(alarm)

    label = menu.alarm_edit_when
    assert label is not None
    # Whatever today is, the chips say Saturday — so must the label.
    assert label.startswith('Sat')


def test_editor_reports_no_date_for_a_repeating_alarm():
    alarm = Alarm(id='aa', hour=9, minute=0, days=[5], repeat=True)
    menu, _, _, _ = _editing(alarm)
    assert menu.alarm_edit_when is None


# --- Draft isolation: the reason the editor holds a copy ---

def test_backing_out_discards_edits_to_an_existing_alarm():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True, sound='marimba')
    menu, manager, _, _ = _editing(alarm)

    tap(menu, 'alarm_hour_plus')
    tap(menu, 'alarm_day_3')
    tap(menu, 'alarm_sound')
    tap(menu, 'close')

    saved = manager.alarms[0]
    assert (saved.hour, saved.days, saved.sound) == (7, [0], 'marimba')


def test_saving_an_existing_alarm_replaces_it_rather_than_duplicating():
    alarm = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    menu, manager, _, _ = _editing(alarm)

    tap(menu, 'alarm_hour_plus')
    tap(menu, 'alarm_save')

    assert len(manager.alarms) == 1
    assert manager.alarms[0].hour == 8


def test_saving_keeps_the_alarm_in_its_original_position():
    first = Alarm(id='aa', hour=7, minute=0, days=[0], repeat=True)
    second = Alarm(id='bb', hour=8, minute=0, days=[1], repeat=True)
    menu, manager, _ = _menu([first, second])
    menu._open_alarm_edit(first)

    tap(menu, 'alarm_minute_plus')
    tap(menu, 'alarm_save')

    assert [a.id for a in manager.alarms] == ['aa', 'bb']


def test_a_draft_cannot_ring_before_it_is_saved():
    menu, manager, _ = _menu()
    menu.state = MenuState.ALARM_LIST
    tap(menu, 'alarm_add')
    assert manager.alarms == [], 'an unsaved draft is reachable by the fire check'


def test_delete_on_a_draft_just_discards_it():
    menu, manager, _ = _menu()
    menu.state = MenuState.ALARM_LIST
    tap(menu, 'alarm_add')
    # Delete isn't drawn for a draft, but a stale rect must not corrupt the list.
    tap(menu, 'alarm_delete')
    tap(menu, 'alarm_delete')

    assert manager.alarms == []
    assert menu.state == MenuState.ALARM_LIST
