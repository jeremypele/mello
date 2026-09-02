"""Tests for alarms: the weekly/one-shot schedule, the no-RTC guard, the ramp."""
import datetime
from pathlib import Path

import pytest
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.managers import quiet_hours as qh
from mello.managers.alarms import (AlarmManager, cycle_sound, expire_stale,
                                   is_due, next_fire, next_upcoming,
                                   resolve_date, ring_volume_pct)
from mello.config import ALARM_RAMP_SECONDS, ALARM_START_VOLUME_PCT, ALARM_TIMEOUT
from mello.models import Alarm

# 2026-08-03 is a Monday, so weekday() lines up with the day index in `days`.
MONDAY = datetime.date(2026, 8, 3)


@pytest.fixture(autouse=True)
def trusted_clock():
    """clock_is_trusted latches — force it on, since these dates are all 2026."""
    qh._trusted = True
    yield
    qh._trusted = False


def at(day_offset=0, hour=6, minute=0):
    return datetime.datetime.combine(
        MONDAY + datetime.timedelta(days=day_offset),
        datetime.time(hour, minute))


def weekly(hour=7, minute=0, days=(0, 1, 4), **kw):
    return Alarm(id='a1', hour=hour, minute=minute, days=list(days), repeat=True, **kw)


def once(hour=9, minute=0, date: Optional[str] = '2026-08-08', **kw):
    return Alarm(id='b1', hour=hour, minute=minute, days=[5], repeat=False,
                 date=date, **kw)


class FakeSettings:
    """Stand-in exposing just what AlarmManager touches."""

    def __init__(self, alarms=None):
        self.alarms = alarms or []
        self.saves = 0

    def save_alarms(self):
        self.saves += 1


class FakePlayer:
    def __init__(self):
        self.plays = []
        self.stops = 0

    def play(self, sound):
        self.plays.append(sound)
        return True

    def stop(self):
        self.stops += 1


# --- Recurring schedule ---

def test_recurring_fires_today_when_time_still_ahead():
    # Monday 06:00, alarm Mon/Tue/Fri at 07:00.
    assert next_fire(weekly(), at(0, 6)) == at(0, 7)


def test_recurring_skips_to_next_selected_day_once_today_has_passed():
    # Monday 08:00 — Monday's 07:00 is gone, so Tuesday.
    assert next_fire(weekly(), at(0, 8)) == at(1, 7)


def test_recurring_wraps_across_the_weekend_to_next_monday():
    # Friday 08:00, days are Mon/Tue/Fri — next is Monday.
    assert next_fire(weekly(), at(4, 8)) == at(7, 7)


def test_recurring_with_no_days_selected_is_daily():
    daily = weekly(days=())
    assert next_fire(daily, at(0, 8)) == at(1, 7)


def test_disabled_alarm_never_fires():
    assert next_fire(weekly(enabled=False), at(0, 6)) is None


def test_alarm_on_only_its_own_weekday_lands_a_week_later():
    saturday = weekly(days=(5,))
    assert next_fire(saturday, at(5, 8)) == at(12, 7)


# --- One-shot schedule ---

def test_one_shot_fires_on_its_date():
    assert next_fire(once(), at(0, 6)) == at(5, 9)   # Saturday 09:00


def test_one_shot_in_the_past_never_fires_again():
    assert next_fire(once(date='2026-08-01'), at(0, 6)) is None


def test_one_shot_without_a_date_never_fires():
    assert next_fire(once(date=None), at(0, 6)) is None


def test_one_shot_with_an_unreadable_date_is_ignored_not_raised():
    assert next_fire(once(date='not-a-date'), at(0, 6)) is None


def test_resolve_date_picks_the_next_matching_weekday():
    # Monday, wanting Saturday.
    assert resolve_date(9, 0, [5], at(0, 6)) == '2026-08-08'


def test_resolve_date_uses_today_when_the_time_is_still_ahead():
    assert resolve_date(9, 0, [0], at(0, 6)) == '2026-08-03'


def test_resolve_date_skips_today_when_the_time_has_gone():
    # Monday 10:00, wanting Monday 09:00 — that's next Monday.
    assert resolve_date(9, 0, [0], at(0, 10)) == '2026-08-10'


def test_resolve_date_with_no_days_falls_to_tomorrow_once_time_has_passed():
    assert resolve_date(9, 0, [], at(0, 10)) == '2026-08-04'


# --- Due, and the double-fire guard ---

def test_is_due_only_on_the_exact_minute():
    alarm = weekly()
    assert not is_due(alarm, at(0, 6, 59))
    assert is_due(alarm, at(0, 7, 0))
    assert not is_due(alarm, at(0, 7, 1))


def test_is_due_ignores_seconds_within_the_firing_minute():
    now = at(0, 7).replace(second=42)
    assert is_due(weekly(), now)


def test_manager_fires_once_per_minute_not_once_per_frame():
    settings = FakeSettings([weekly()])
    mgr = AlarmManager(settings, player=FakePlayer(), monotonic=lambda: 0.0)

    assert mgr.update(at(0, 7)) is not None
    mgr.dismiss()
    # Same minute, a frame later: must not ring again.
    assert mgr.update(at(0, 7).replace(second=30)) is None


def test_manager_does_not_fire_while_the_clock_is_untrusted():
    qh._trusted = False
    settings = FakeSettings([weekly()])
    mgr = AlarmManager(settings, player=FakePlayer(), monotonic=lambda: 0.0)
    # 1970: what a Pi reads before NTP lands.
    assert mgr.update(datetime.datetime(1970, 1, 1, 7, 0)) is None


def test_missed_alarm_is_not_fired_late():
    settings = FakeSettings([weekly()])
    mgr = AlarmManager(settings, player=FakePlayer(), monotonic=lambda: 0.0)
    # Booted at 08:20 with a 07:00 alarm — nothing to catch up on.
    assert mgr.update(at(0, 8, 20)) is None


# --- One-shot lifecycle ---

def test_one_shot_disarms_itself_after_ringing():
    alarm = once()
    settings = FakeSettings([alarm])
    mgr = AlarmManager(settings, player=FakePlayer(), monotonic=lambda: 0.0,
                       now=at(0, 6))

    assert mgr.update(at(5, 9)) is alarm
    assert alarm.enabled is False
    assert alarm in settings.alarms      # stays in the list, re-armable in one tap


def test_recurring_stays_armed_after_ringing():
    alarm = weekly()
    mgr = AlarmManager(FakeSettings([alarm]), player=FakePlayer(), monotonic=lambda: 0.0)
    mgr.update(at(0, 7))
    assert alarm.enabled is True


def test_expire_stale_disarms_a_one_shot_whose_day_has_gone():
    alarm = once(date='2026-08-01')
    assert expire_stale([alarm], at(0, 6)) is True
    assert alarm.enabled is False


def test_expire_stale_disarms_a_one_shot_from_earlier_today():
    alarm = once(hour=5, date='2026-08-03')
    assert expire_stale([alarm], at(0, 6)) is True
    assert alarm.enabled is False


def test_expire_stale_leaves_future_and_recurring_alarms_alone():
    future, repeating = once(), weekly()
    assert expire_stale([future, repeating], at(0, 6)) is False
    assert future.enabled and repeating.enabled


def test_manager_expires_stale_one_shots_at_startup():
    alarm = once(date='2026-08-01')
    settings = FakeSettings([alarm])
    AlarmManager(settings, player=FakePlayer(), now=at(0, 6))
    assert alarm.enabled is False
    assert settings.saves == 1


def test_rearming_a_stale_one_shot_moves_it_forward():
    alarm = once(date='2026-08-01', enabled=False)
    mgr = AlarmManager(FakeSettings([alarm]), player=FakePlayer(), now=at(0, 6))
    mgr.toggle(alarm.id, now=at(0, 6))
    assert alarm.enabled is True
    assert alarm.date == '2026-08-08'     # next Saturday, not the dead date


# --- The sleep clock's bell ---

def test_bell_shows_an_alarm_inside_the_horizon():
    assert next_upcoming([weekly()], at(0, 6), 24) == at(0, 7)


def test_bell_hides_an_alarm_beyond_the_horizon():
    # Saturday-only alarm, seen on Monday: five days out.
    assert next_upcoming([weekly(days=(5,))], at(0, 6), 24) is None


def test_bell_picks_the_soonest_of_several():
    early = Alarm(id='x', hour=6, minute=30, days=[0], repeat=True)
    late = Alarm(id='y', hour=7, minute=0, days=[0], repeat=True)
    assert next_upcoming([late, early], at(0, 5), 24) == at(0, 6, 30)


def test_bell_ignores_disabled_alarms():
    assert next_upcoming([weekly(enabled=False)], at(0, 6), 24) is None


def test_bell_spans_midnight():
    # Monday 23:00, alarm Tuesday 07:00 — eight hours out, still inside 24h.
    assert next_upcoming([weekly()], at(0, 23), 24) == at(1, 7)


# --- Volume ramp ---

def test_ramp_starts_gentle_and_reaches_full():
    assert ring_volume_pct(0) == ALARM_START_VOLUME_PCT
    assert ring_volume_pct(ALARM_RAMP_SECONDS / 2) == pytest.approx(
        ALARM_START_VOLUME_PCT + (100 - ALARM_START_VOLUME_PCT) // 2, abs=1)
    assert ring_volume_pct(ALARM_RAMP_SECONDS) == 100
    assert ring_volume_pct(ALARM_TIMEOUT) == 100


def test_ramp_never_goes_backwards():
    levels = [ring_volume_pct(t) for t in range(0, 60)]
    assert levels == sorted(levels)


# --- Ringing ---

def test_ring_repeats_the_sound_with_a_gap():
    clock = [0.0]
    player = FakePlayer()
    mgr = AlarmManager(FakeSettings([weekly()]), player=player,
                       monotonic=lambda: clock[0])
    mgr.update(at(0, 7))
    assert player.plays == ['marimba']

    clock[0] = 0.5              # mid-sound: no repeat yet
    mgr.update(at(0, 7, 30))
    assert len(player.plays) == 1

    clock[0] = 100.0            # well past sound + gap
    mgr.update(at(0, 7, 30))
    assert len(player.plays) == 2


def test_ring_pushes_the_escalating_volume():
    clock = [0.0]
    levels = []
    mgr = AlarmManager(FakeSettings([weekly()]), player=FakePlayer(),
                       on_volume=levels.append, monotonic=lambda: clock[0])
    mgr.update(at(0, 7))
    assert levels == [ALARM_START_VOLUME_PCT]

    clock[0] = ALARM_RAMP_SECONDS
    mgr.update(at(0, 7, 30))
    assert levels[-1] == 100


def test_ring_gives_up_at_the_timeout():
    clock = [0.0]
    player = FakePlayer()
    mgr = AlarmManager(FakeSettings([weekly()]), player=player,
                       monotonic=lambda: clock[0])
    mgr.update(at(0, 7))
    assert mgr.ringing is not None

    clock[0] = ALARM_TIMEOUT
    mgr.update(at(0, 7, 30))
    assert mgr.ringing is None
    assert player.stops == 1


def test_dismiss_cuts_the_sound_immediately():
    player = FakePlayer()
    mgr = AlarmManager(FakeSettings([weekly()]), player=player, monotonic=lambda: 0.0)
    mgr.update(at(0, 7))
    mgr.dismiss()
    assert mgr.ringing is None
    assert player.stops == 1


def test_a_second_alarm_cannot_interrupt_one_already_ringing():
    first = Alarm(id='x', hour=7, minute=0, days=[0], repeat=True)
    second = Alarm(id='y', hour=7, minute=0, days=[0], repeat=True)
    mgr = AlarmManager(FakeSettings([first, second]), player=FakePlayer(),
                       monotonic=lambda: 0.0)
    assert mgr.update(at(0, 7)) is first
    assert mgr.update(at(0, 7)) is None
    assert mgr.ringing is first


# --- Editing ---

def test_save_edit_pins_a_one_shot_to_a_date():
    alarm = Alarm(id='n', hour=9, minute=0, days=[5], repeat=False)
    mgr = AlarmManager(FakeSettings(), player=FakePlayer())
    mgr.save_edit(alarm, now=at(0, 6))
    assert alarm.date == '2026-08-08'
    assert alarm in mgr.alarms


def test_save_edit_clears_the_date_when_switched_to_repeating():
    alarm = Alarm(id='n', hour=9, minute=0, days=[5], repeat=True, date='2026-08-08')
    mgr = AlarmManager(FakeSettings(), player=FakePlayer())
    mgr.save_edit(alarm, now=at(0, 6))
    assert alarm.date is None


def test_save_edit_updates_in_place_rather_than_duplicating():
    alarm = weekly()
    mgr = AlarmManager(FakeSettings([alarm]), player=FakePlayer())
    alarm.hour = 8
    mgr.save_edit(alarm, now=at(0, 6))
    assert len(mgr.alarms) == 1


def test_remove_drops_only_the_named_alarm():
    keep, drop = weekly(), once()
    mgr = AlarmManager(FakeSettings([keep, drop]), player=FakePlayer(), now=at(0, 6))
    mgr.remove(drop.id)
    assert mgr.alarms == [keep]


def test_cycle_sound_wraps_and_recovers_from_an_unknown_name():
    assert cycle_sound('marimba') == 'riser'
    assert cycle_sound('chime') == 'marimba'
    assert cycle_sound('deleted-sound') == 'marimba'


# --- Labels, since they're what the parent actually reads ---

@pytest.mark.parametrize('days,expected', [
    ([0, 1, 4], 'Mon Tue Fri'),
    ([], 'Every day'),
    ([0, 1, 2, 3, 4, 5, 6], 'Every day'),
    ([0, 1, 2, 3, 4], 'Weekdays'),
    ([5, 6], 'Weekend'),
])
def test_days_label(days, expected):
    assert weekly(days=days).days_label == expected


def test_one_shot_label_reads_as_a_date():
    assert once().days_label == 'Sat 8 Aug'


def test_time_label_pads():
    assert weekly(hour=7, minute=5).time_label == '07:05'


# --- Persistence ---

def test_alarm_survives_a_round_trip():
    alarm = once()
    assert Alarm.from_dict(alarm.to_dict()) == alarm


def test_from_dict_clamps_a_hand_edited_file():
    alarm = Alarm.from_dict({'id': 'x', 'hour': 99, 'minute': -5, 'days': [0, 9]})
    assert alarm.hour == 23 and alarm.minute == 0
    assert alarm.days == [0]
