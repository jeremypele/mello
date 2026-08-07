"""
Mello Data Models - Core data structures.
"""
import datetime
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Literal


class MenuState(Enum):
    """Setup menu states (replaces 3 separate booleans)."""
    CLOSED = auto()
    MAIN = auto()
    WIFI_LIST = auto()
    WIFI_AP = auto()
    BT_LIST = auto()
    VOLUME_LEVELS = auto()
    TRACK_LIST = auto()
    BEDTIME_LIST = auto()
    ALARM_LIST = auto()
    ALARM_EDIT = auto()


DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


@dataclass
class Alarm:
    """One alarm. Recurring by weekday, or a one-shot pinned to a date.

    A recurring alarm is `days` + a time and rings forever. A one-shot resolves
    its chosen days to a single `date` when it is saved, so a missed one expires
    instead of ambushing the family a week later. `days` is still kept on a
    one-shot because it is what the editor's chips show.
    """
    id: str
    hour: int
    minute: int
    days: List[int] = field(default_factory=list)  # 0=Mon .. 6=Sun
    repeat: bool = True
    enabled: bool = True
    sound: str = 'marimba'
    date: Optional[str] = None       # 'YYYY-MM-DD', one-shots only

    @property
    def time_label(self) -> str:
        return f'{self.hour:02d}:{self.minute:02d}'

    @property
    def days_label(self) -> str:
        """How the alarm's schedule reads on a list row."""
        if not self.repeat:
            if not self.date:
                return 'Once'
            d = datetime.date.fromisoformat(self.date)
            return f'{DAY_NAMES[d.weekday()]} {d.day} {MONTH_NAMES[d.month - 1]}'
        days = sorted(set(self.days))
        if not days or len(days) == 7:
            return 'Every day'
        if days == [0, 1, 2, 3, 4]:
            return 'Weekdays'
        if days == [5, 6]:
            return 'Weekend'
        return ' '.join(DAY_NAMES[d] for d in days)

    def copy(self) -> 'Alarm':
        """A detached copy for the editor to scribble on until Save.

        `days` is copied explicitly — sharing the list would let every chip tap
        mutate the saved alarm, which is the whole thing a draft prevents.
        """
        return Alarm(id=self.id, hour=self.hour, minute=self.minute,
                     days=list(self.days), repeat=self.repeat,
                     enabled=self.enabled, sound=self.sound, date=self.date)

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'hour': self.hour, 'minute': self.minute,
            'days': list(self.days), 'repeat': self.repeat,
            'enabled': self.enabled, 'sound': self.sound, 'date': self.date,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Alarm':
        """Build from stored JSON, clamping anything a hand-edit could break."""
        days = [int(d) for d in data.get('days', []) if isinstance(d, int) and 0 <= d <= 6]
        return cls(
            id=str(data.get('id', '')),
            hour=max(0, min(23, int(data.get('hour', 7)))),
            minute=max(0, min(59, int(data.get('minute', 0)))),
            days=days,
            repeat=bool(data.get('repeat', True)),
            enabled=bool(data.get('enabled', True)),
            sound=str(data.get('sound') or 'marimba'),
            date=data.get('date'),
        )


@dataclass
class LibrespotStatus:
    """Parsed response from the go-librespot /status endpoint."""
    playing: bool = False
    paused: bool = False
    stopped: bool = True
    volume: Optional[int] = None
    context_uri: Optional[str] = None
    track_name: Optional[str] = None
    track_artist: Optional[str] = None
    track_album: Optional[str] = None
    track_cover: Optional[str] = None
    track_uri: Optional[str] = None
    position: int = 0
    duration: int = 0
    repeat_context: bool = False

    @classmethod
    def from_dict(cls, data: dict, context_uri: Optional[str] = None) -> 'LibrespotStatus':
        """Parse raw API dict into a typed object."""
        track = data.get('track') or {}
        if not isinstance(track, dict):
            track = {}
        
        artist_names = track.get('artist_names', [])
        artist = ', '.join(artist_names) if artist_names else None
        
        raw_context_uri = data.get('context_uri') if isinstance(data, dict) else None
        resolved_context_uri = raw_context_uri or context_uri

        return cls(
            playing=not data.get('stopped', True) and not data.get('paused', False),
            paused=data.get('paused', False),
            stopped=data.get('stopped', True),
            volume=data.get('volume'),
            context_uri=resolved_context_uri,
            track_name=track.get('name'),
            track_artist=artist,
            track_album=track.get('album_name'),
            track_cover=track.get('album_cover_url'),
            track_uri=track.get('uri'),
            position=track.get('position', 0),
            duration=track.get('duration', 0),
            repeat_context=bool(data.get('repeat_context', False)),
        )


@dataclass
class CatalogItem:
    """Represents an album or playlist in the catalog."""
    id: str
    uri: str
    name: str
    type: str = 'album'
    artist: Optional[str] = None
    image: Optional[str] = None
    images: Optional[List[str]] = None  # For playlist composite covers
    current_track: Optional[dict] = None
    is_temp: bool = False


@dataclass
class NowPlaying:
    """Current playback state from librespot."""
    playing: bool = False
    paused: bool = False
    stopped: bool = True
    context_uri: Optional[str] = None
    track_name: Optional[str] = None
    track_artist: Optional[str] = None
    track_album: Optional[str] = None
    track_cover: Optional[str] = None
    track_uri: Optional[str] = None
    position: int = 0
    duration: int = 0
    repeat_context: bool = False
    
    @property
    def progress(self) -> float:
        """Get playback progress as 0.0-1.0."""
        if self.duration <= 0:
            return 0.0
        return min(1.0, self.position / self.duration)
    
    def __repr__(self) -> str:
        state = 'playing' if self.playing else ('paused' if self.paused else 'stopped')
        track = self.track_name or '(none)'
        return f'NowPlaying({state}, {track}, {self.position // 1000}s/{self.duration // 1000}s)'


@dataclass
class PlayState:
    """
    Unified play/loading state for UI feedback.
    
    Replaces multiple separate variables:
    - _optimistic_playing
    - _is_loading / _should_show_loading  
    - _loading_start_time
    """
    pending_action: Optional[Literal['play', 'pause']] = None
    loading_since: Optional[float] = None
    
    # Delay before showing spinner (prevents flicker)
    SPINNER_DELAY = 0.2
    
    def set_pending(self, action: Literal['play', 'pause']):
        """Set a pending play/pause action."""
        self.pending_action = action
        if action == 'play':
            self.loading_since = time.time()
        else:
            self.loading_since = None
    
    def clear(self):
        """Clear pending state (real data received)."""
        self.pending_action = None
        self.loading_since = None
    
    def start_loading(self):
        """Start loading state (for navigation pause, play timer, etc.)."""
        if self.loading_since is None:
            self.loading_since = time.time()
    
    def stop_loading(self):
        """Stop loading state."""
        self.loading_since = None
    
    @property
    def is_loading(self) -> bool:
        """True if loading long enough to show spinner (200ms delay)."""
        if self.loading_since is None:
            return False
        return time.time() - self.loading_since > self.SPINNER_DELAY
    
    @property
    def should_show_loading(self) -> bool:
        """True if in any loading state (for play button icon)."""
        if self.pending_action == 'pause':
            return False
        return self.loading_since is not None

    @property
    def pause_intent_active(self) -> bool:
        """True when a user pause intent should dominate UI state."""
        return self.pending_action == 'pause'
    
    def display_playing(self, actual_playing: bool) -> bool:
        """What the UI should show for play/pause state."""
        if self.pause_intent_active:
            return False
        if self.pending_action == 'play' or self.should_show_loading:
            return True
        return actual_playing
