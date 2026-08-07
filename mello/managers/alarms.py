"""
Alarms - Wake-up chimes on a weekly schedule, or as a one-shot on a date.

Rings a sound file on the built-in speaker until someone taps the screen. The
ring gets louder as it goes: a steady tone is easy to sleep through, one that
keeps climbing is not.

Deliberately *not* a Spotify feature. A chime works with the internet down, with
Spotify logged out, and with the album someone picked six months ago deleted.
"""
import datetime
import logging
import subprocess
import time
import uuid
from typing import Callable, List, Optional

from ..config import (ALARM_RAMP_SECONDS, ALARM_REPEAT_GAP, ALARM_SOUNDS,
                      ALARM_START_VOLUME_PCT, ALARM_TIMEOUT, SOUNDS_DIR,
                      WM8960_SINK)
from ..models import Alarm
from .quiet_hours import clock_is_trusted

logger = logging.getLogger(__name__)


def _floor_minute(now: datetime.datetime) -> datetime.datetime:
    return now.replace(second=0, microsecond=0)


def next_fire(alarm: Alarm, now: datetime.datetime) -> Optional[datetime.datetime]:
    """When this alarm next rings, or None if it never will again.

    "Next" includes the current minute, so is_due() can be expressed in terms
    of this and the two can never disagree about what counts as due.
    """
    if not alarm.enabled:
        return None

    at = datetime.time(alarm.hour, alarm.minute)
    floor = _floor_minute(now)

    if not alarm.repeat:
        if not alarm.date:
            return None
        try:
            day = datetime.date.fromisoformat(alarm.date)
        except ValueError:
            logger.warning(f'Alarm {alarm.id} has an unreadable date: {alarm.date!r}')
            return None
        when = datetime.datetime.combine(day, at)
        return when if when >= floor else None

    # Recurring. No days chosen means every day — the editor lets the chips all
    # be cleared, and a repeating alarm that can never fire would be a trap.
    days = set(alarm.days) or set(range(7))
    for offset in range(8):          # 8, so "same weekday next week" is reachable
        day = now.date() + datetime.timedelta(days=offset)
        if day.weekday() not in days:
            continue
        when = datetime.datetime.combine(day, at)
        if when >= floor:
            return when
    return None


def is_due(alarm: Alarm, now: datetime.datetime) -> bool:
    """True during the exact minute this alarm should start ringing."""
    return next_fire(alarm, now) == _floor_minute(now)


def resolve_date(hour: int, minute: int, days: List[int],
                 now: datetime.datetime) -> str:
    """The date a one-shot with these chips should land on, as 'YYYY-MM-DD'.

    Called when a one-shot is saved, so the alarm stops being "next Saturday"
    (which drifts) and becomes a fixed day that can expire.
    """
    at = datetime.time(hour, minute)
    wanted = set(days)
    for offset in range(8):
        day = now.date() + datetime.timedelta(days=offset)
        if wanted and day.weekday() not in wanted:
            continue
        if datetime.datetime.combine(day, at) > now:
            return day.isoformat()
    # Only reachable with no days and a time already gone today.
    return (now.date() + datetime.timedelta(days=1)).isoformat()


def expire_stale(alarms: List[Alarm], now: datetime.datetime) -> bool:
    """Switch off one-shots whose moment has passed. True if anything changed.

    Runs at startup. Without it, a Saturday alarm the device slept through stays
    armed on a date in the past, and 'missed is missed' would quietly become
    'fires whenever the code next looks'.
    """
    changed = False
    for alarm in alarms:
        if alarm.repeat or not alarm.enabled:
            continue
        if next_fire(alarm, now) is None:
            alarm.enabled = False
            changed = True
            logger.info(f'Alarm {alarm.time_label} on {alarm.date} has passed — disarmed')
    return changed


def next_upcoming(alarms: List[Alarm], now: datetime.datetime,
                  horizon_hours: int) -> Optional[datetime.datetime]:
    """Soonest alarm inside the horizon — what the sleep clock's bell shows."""
    limit = now + datetime.timedelta(hours=horizon_hours)
    times = [t for t in (next_fire(a, now) for a in alarms) if t is not None and t <= limit]
    return min(times) if times else None


def ring_volume_pct(elapsed: float) -> int:
    """Share of the speaker's usable band this far into a ring, 0-100.

    Climbs from ALARM_START_VOLUME_PCT to full over ALARM_RAMP_SECONDS. This is
    the whole reason the feature works on a heavy sleeper.
    """
    if elapsed >= ALARM_RAMP_SECONDS:
        return 100
    span = 100 - ALARM_START_VOLUME_PCT
    return int(ALARM_START_VOLUME_PCT + span * max(0.0, elapsed) / ALARM_RAMP_SECONDS)


def new_alarm() -> Alarm:
    """A sensible blank alarm for the Add row: 07:00 on weekdays."""
    return Alarm(id=uuid.uuid4().hex[:8], hour=7, minute=0, days=[0, 1, 2, 3, 4])


class SoundPlayer:
    """Plays a chime through the built-in speaker, bypassing Bluetooth.

    Targets the WM8960 sink by name rather than the default sink: when a BT
    speaker is connected it *becomes* the default, and an alarm that comes out
    of a speaker asleep in another room has failed at its one job.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None

    def play(self, sound: str) -> bool:
        path = SOUNDS_DIR / f'{sound}.wav'
        if not path.exists():
            logger.error(f'Alarm sound missing: {path}')
            return False
        self.stop()
        try:
            self._proc = subprocess.Popen(
                ['paplay', f'--device={WM8960_SINK}', str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, FileNotFoundError) as e:
            logger.error(f'Could not play alarm sound: {e}')
            return False

    def stop(self):
        """Cut playback immediately. A tap must not wait out a 7-second chime."""
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
        except OSError:
            pass
        self._proc = None


class AlarmManager:
    """Decides when an alarm fires, then drives the ring until it's dismissed.

    Firing is gated on clock_is_trusted() for the same reason quiet hours is:
    the Pi has no RTC, so before NTP lands the clock can read 1970 and a
    time-of-day decision made on it would go off at random.
    """

    def __init__(self, settings, player=None, on_volume: Optional[Callable[[int], None]] = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.settings = settings
        self._player = player if player is not None else SoundPlayer()
        self._on_volume = on_volume
        self._monotonic = monotonic

        self.ringing: Optional[Alarm] = None
        self._started_at = 0.0
        self._next_play_at = 0.0
        self._last_volume = -1
        # 'id@YYYY-MM-DDTHH:MM' of the last alarm started, so a loop running at
        # 60fps doesn't restart the same alarm sixty times a second.
        self._last_fired: Optional[str] = None

        if expire_stale(self.settings.alarms, datetime.datetime.now()):
            self.settings.save_alarms()

    @property
    def alarms(self) -> List[Alarm]:
        return self.settings.alarms

    def update(self, now: Optional[datetime.datetime] = None) -> Optional[Alarm]:
        """Called every frame. Returns the alarm that *just* started ringing."""
        now = now or datetime.datetime.now()
        if self.ringing is not None:
            self._drive_ring()
            return None
        return self._check_due(now)

    def _check_due(self, now: datetime.datetime) -> Optional[Alarm]:
        if not clock_is_trusted(now):
            return None
        for alarm in self.alarms:
            if not is_due(alarm, now):
                continue
            key = f'{alarm.id}@{now:%Y-%m-%dT%H:%M}'
            if key == self._last_fired:
                continue
            self._last_fired = key
            self._start(alarm)
            return alarm
        return None

    def _start(self, alarm: Alarm):
        logger.info(f'Alarm ringing: {alarm.time_label} ({alarm.days_label}, {alarm.sound})')
        self.ringing = alarm
        self._started_at = self._monotonic()
        self._next_play_at = self._started_at
        self._last_volume = -1

        # A one-shot has done its job. It stays in the list, switched off, so
        # re-arming it is one tap instead of building it again.
        if not alarm.repeat:
            alarm.enabled = False
            self.settings.save_alarms()

        # Ring on this frame, not the next one. _drive_ring sets the volume
        # before it plays, so waiting would let the first playthrough out at
        # whatever the slider was left on — silent, if that was zero.
        self._drive_ring()

    def _drive_ring(self):
        """Repeat the sound and push the volume ramp; stop at the timeout."""
        alarm = self.ringing
        if alarm is None:
            return
        elapsed = self._monotonic() - self._started_at
        if elapsed >= ALARM_TIMEOUT:
            logger.info('Alarm timed out unanswered')
            self.dismiss()
            return

        volume = ring_volume_pct(elapsed)
        if volume != self._last_volume:
            self._last_volume = volume
            if self._on_volume:
                self._on_volume(volume)

        if self._monotonic() >= self._next_play_at:
            self._player.play(alarm.sound)
            self._next_play_at = self._monotonic() + self._sound_seconds(alarm.sound) + ALARM_REPEAT_GAP

    @staticmethod
    def _sound_seconds(sound: str) -> float:
        """How long the sound runs. Cached per file, read once.

        ponytail: parses the WAV header directly rather than pulling in a
        library — these are the four files we ship, all PCM.
        """
        if sound in _DURATIONS:
            return _DURATIONS[sound]
        seconds = _wav_seconds(SOUNDS_DIR / f'{sound}.wav')
        _DURATIONS[sound] = seconds
        return seconds

    def dismiss(self):
        """Stop ringing. Any tap anywhere gets here."""
        if self.ringing is None:
            return
        logger.info(f'Alarm dismissed after {self._monotonic() - self._started_at:.0f}s')
        self.ringing = None
        self._player.stop()

    # --- Editing, all of which persists immediately ---

    def add(self, alarm: Alarm):
        self.alarms.append(alarm)
        self.settings.save_alarms()
        logger.info(f'Alarm added: {alarm.time_label} ({alarm.days_label})')

    def remove(self, alarm_id: str):
        self.settings.alarms = [a for a in self.alarms if a.id != alarm_id]
        self.settings.save_alarms()
        logger.info(f'Alarm removed: {alarm_id}')

    def toggle(self, alarm_id: str, now: Optional[datetime.datetime] = None):
        """Arm or disarm from the list row.

        Re-arming a one-shot whose date has passed re-resolves it forward,
        otherwise the toggle would appear to do nothing.
        """
        now = now or datetime.datetime.now()
        for alarm in self.alarms:
            if alarm.id != alarm_id:
                continue
            alarm.enabled = not alarm.enabled
            if alarm.enabled and not alarm.repeat:
                alarm.date = resolve_date(alarm.hour, alarm.minute, alarm.days, now)
            self.settings.save_alarms()
            logger.info(f'Alarm {alarm.time_label} {"armed" if alarm.enabled else "disarmed"}')
            return

    def save_edit(self, alarm: Alarm, now: Optional[datetime.datetime] = None):
        """Persist an edited alarm, pinning a one-shot to a concrete date."""
        now = now or datetime.datetime.now()
        if alarm.repeat:
            alarm.date = None
        else:
            alarm.date = resolve_date(alarm.hour, alarm.minute, alarm.days, now)
        if alarm.id not in {a.id for a in self.alarms}:
            self.alarms.append(alarm)
        self.settings.save_alarms()
        logger.info(f'Alarm saved: {alarm.time_label} ({alarm.days_label})')

    def preview(self, sound: str):
        """Play a sound once so the Sound row gives immediate feedback."""
        if self.ringing is None:
            self._player.play(sound)


_DURATIONS: dict = {}


def _wav_seconds(path) -> float:
    """Length of a PCM WAV, from its header. 2.0s if it can't be read."""
    try:
        import wave
        with wave.open(str(path), 'rb') as w:
            return w.getnframes() / float(w.getframerate())
    except Exception as e:
        logger.warning(f'Could not read length of {path}: {e}')
        return 2.0


def cycle_sound(current: str) -> str:
    """Next sound in the list, for the Sound row."""
    idx = ALARM_SOUNDS.index(current) if current in ALARM_SOUNDS else -1
    return ALARM_SOUNDS[(idx + 1) % len(ALARM_SOUNDS)]
