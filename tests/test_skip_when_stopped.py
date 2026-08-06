"""
Prev/next while nothing is playing.

librespot has nothing to skip when the device is stopped or paused, so the
buttons used to be inert. They now walk the focused album's track list —
fetching it if need be, wrapping at both ends — and play what they land on.
prev/next should move *and* sound.
"""
import os
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

from mello.models import NowPlaying, PlayState   # noqa: E402

CONTEXT = 'spotify:album:x'
TRACKS = [SimpleNamespace(uri=f'spotify:track:{i}', name=f'Track {i}') for i in range(3)]


def _app(playing=False, reference_uri='spotify:track:1', tracks=TRACKS, fetched=None):
    sys.modules.setdefault('pygame', types.ModuleType('pygame'))
    sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))
    from mello.app import Mello
    from mello.api.tracklist import TrackListStore

    app = Mello.__new__(Mello)
    app._now_playing_lock = threading.Lock()
    app.now_playing = NowPlaying(playing=playing)
    app.calls = []
    app.played = []
    app.toasts = []
    app.api = SimpleNamespace(next=lambda: app.calls.append('next') or True,
                              prev=lambda: app.calls.append('prev') or True)
    app.volume = SimpleNamespace(unmute=lambda: None)
    app.renderer = SimpleNamespace(invalidate=lambda: None)
    app.playback = SimpleNamespace(
        last_user_play_time=0,
        play_state=PlayState(),
        save_progress=lambda np, force=False: None,
        play_item=lambda uri, skip_to_uri=None: app.played.append((uri, skip_to_uri)),
    )
    app._manual_pause_lock = False
    app._show_toast = app.toasts.append
    app._focused_context = lambda: (CONTEXT, reference_uri)

    app.track_lists = TrackListStore.__new__(TrackListStore)
    app.track_lists.get = lambda uri: tracks if uri == CONTEXT else None
    app.track_lists.fetch = lambda uri: fetched
    app.track_lists.index_of = TrackListStore.index_of.__get__(app.track_lists)
    return app


class TestWalkingTheList:

    def test_next_plays_the_following_track(self):
        app = _app()
        app._skip_while_stopped(1)
        assert app.played == [(CONTEXT, 'spotify:track:2')]

    def test_prev_plays_the_preceding_track(self):
        app = _app()
        app._skip_while_stopped(-1)
        assert app.played == [(CONTEXT, 'spotify:track:0')]

    def test_prev_on_the_first_track_wraps_to_the_last(self):
        """Albums already loop on repeat-context, so the button never dies."""
        app = _app(reference_uri='spotify:track:0')
        app._skip_while_stopped(-1)
        assert app.played == [(CONTEXT, 'spotify:track:2')]

    def test_next_on_the_last_track_wraps_to_the_first(self):
        app = _app(reference_uri='spotify:track:2')
        app._skip_while_stopped(1)
        assert app.played == [(CONTEXT, 'spotify:track:0')]

    def test_an_uncached_list_is_fetched_first(self):
        app = _app(tracks=None, fetched=TRACKS)
        app._skip_while_stopped(1)
        assert app.played == [(CONTEXT, 'spotify:track:2')]


class TestFallback:
    """No list to walk — shared quota, not logged in, unlistable context."""

    def test_librespot_is_asked_instead(self):
        app = _app(tracks=None, fetched=None)
        app._skip_while_stopped(1)
        assert app.played == [] and app.calls == ['next']

    def test_a_dead_device_says_so(self):
        app = _app(tracks=None, fetched=None)
        app.api = SimpleNamespace(next=lambda: False, prev=lambda: False)
        app._skip_while_stopped(1)
        assert app.toasts == ['Not connected']

    def test_the_play_icon_does_not_stay_stuck(self):
        app = _app(tracks=None, fetched=None)
        app.playback.play_state.set_pending('play')
        app._skip_while_stopped(1)
        assert app.playback.play_state.pending_action is None


class TestRouting:

    def test_stopped_goes_local_and_shows_intent_immediately(self):
        app = _app()
        app._skip_track(1)
        _drain()
        assert app.played == [(CONTEXT, 'spotify:track:2')]
        assert app._user_activated_playback is True

    def test_paused_counts_as_stopped(self):
        """Paused mid-album: prev/next moves and resumes, it doesn't stay silent."""
        app = _app()
        app.now_playing.paused = True
        app._skip_track(1)
        _drain()
        assert app.played == [(CONTEXT, 'spotify:track:2')]

    def test_playing_still_goes_through_librespot(self):
        """Regression guard: a live skip must not restart the context locally."""
        app = _app(playing=True)
        app._skip_track(1)
        _drain()
        assert app.played == [] and app.calls == ['next']


def _drain():
    """The skip path runs in the shared thread pool."""
    import time
    time.sleep(0.3)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
