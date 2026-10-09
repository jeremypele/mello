"""
A tap on play/cover right after a resume.

The status poll runs every few seconds, so for a moment after a resume the app
still says "paused". On the device (2026-10-09) three pause taps in that window
each sent play again. A WebSocket event must refresh the status at once, and a
tap during a pending play must never force play.
"""
import os
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

from mello.models import NowPlaying   # noqa: E402

ITEM = SimpleNamespace(uri='spotify:album:x', name='X', is_temp=False)


def _app(monkeypatch, paused=True, pending=False):
    sys.modules.setdefault('pygame', types.ModuleType('pygame'))
    sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))
    from mello.app import Mello

    monkeypatch.setattr(Mello, 'display_items', property(lambda self: [ITEM]))
    app = Mello.__new__(Mello)
    app._now_playing_lock = threading.Lock()
    app.now_playing = NowPlaying(paused=paused, context_uri=ITEM.uri)
    app.mock_mode = False
    app.selected_index = 0
    app.calls = []
    app._set_manual_pause_lock = lambda reason: app.calls.append('pause_lock')
    app._clear_manual_pause_lock = lambda reason: app.calls.append('clear_lock')
    app._play_item = lambda uri, from_beginning=False: app.calls.append('force_play')
    app.playback = SimpleNamespace(
        has_pending_play=pending,
        toggle_play=lambda items, index, np: app.calls.append('toggle'),
    )
    return app


def test_tap_while_loading_is_a_pause_even_if_status_says_paused(monkeypatch):
    app = _app(monkeypatch, paused=True, pending=True)
    app._toggle_play()
    assert app.calls == ['pause_lock', 'toggle']


def test_tap_when_paused_and_idle_still_forces_play(monkeypatch):
    app = _app(monkeypatch, paused=True, pending=False)
    app._toggle_play()
    assert app.calls == ['clear_lock', 'force_play']


def test_websocket_event_refreshes_status_while_awake(monkeypatch):
    app = _app(monkeypatch)
    app._poll_wake_event = threading.Event()
    app.events = SimpleNamespace(context_uri=ITEM.uri)
    app.sleep_manager = SimpleNamespace(is_sleeping=False)
    app._on_ws_update()
    assert app._poll_wake_event.is_set()
