"""
Naming the focused cover while nothing is playing.

The title area only ever showed the playing track, so browsing a stopped
device was a silent wall of covers — no album, playlist or show name anywhere.
The name now falls back to the focused item, but only once the carousel has
landed on it: labelling every cover that flies past during a scroll would
repaint the whole screen per item on a Pi, and flicker while it did.
"""
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

from mello.models import CatalogItem, NowPlaying


def _item(name='Peter and the Wolf', artist='Prokofiev', item_type='album'):
    return CatalogItem(id='1', uri='spotify:album:x', name=name,
                       type=item_type, artist=artist)


class TestFocusGate:
    """App side: which cover, if any, is allowed to name itself."""

    @staticmethod
    def _app(settled=True, dragging=False):
        # pygame isn't importable on the Pi's test path either; app.py only
        # needs the module to exist to be imported.
        sys.modules.setdefault('pygame', types.ModuleType('pygame'))
        sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))
        from mello.app import Mello

        app = Mello.__new__(Mello)
        app.carousel = SimpleNamespace(settled=settled)
        app.touch = SimpleNamespace(dragging=dragging)
        return app

    def test_settled_cover_is_named(self):
        assert self._app()._focus_label(_item()) == ('Peter and the Wolf', 'Prokofiev')

    def test_playlist_without_artist_shows_just_its_name(self):
        item = _item(name='Bedtime', artist=None, item_type='playlist')
        assert self._app()._focus_label(item) == ('Bedtime', '')

    def test_nothing_while_the_carousel_is_still_moving(self):
        """The throttle: a fling passes a dozen covers, none get labelled."""
        assert self._app(settled=False)._focus_label(_item()) is None

    def test_nothing_while_a_finger_is_down(self):
        """Settled but dragged: the cover under the finger isn't chosen yet."""
        assert self._app(dragging=True)._focus_label(_item()) is None

    def test_no_item_no_label(self):
        assert self._app()._focus_label(None) is None


pygame = pytest.importorskip('pygame')

from mello.config import COLORS, TRACK_INFO_X  # noqa: E402
from mello.ui.context import RenderContext  # noqa: E402
from mello.ui.renderer import Renderer  # noqa: E402


def _ctx(**kwargs):
    ctx = RenderContext(
        items=[_item()], selected_index=0, now_playing=NowPlaying(),
        scroll_x=0.0, drag_offset=0.0, dragging=False, is_sleeping=False,
        volume_pct=60, volume_icon='volume_low', volume_slider_open=False,
        delete_mode_id=None, pressed_button=None, is_loading=False,
        is_playing=False)
    for key, value in kwargs.items():
        setattr(ctx, key, value)
    return ctx


class TestTitlePriority:
    """Renderer side: the cover's name is the last resort, never an override."""

    def test_focus_label_used_when_nothing_is_playing(self):
        key = Renderer._get_track_key(
            item=_item(), now_playing=NowPlaying(),
            is_loading=False, pending_focus_uri=None,
            requested_focus_uri=None, play_in_progress=False,
            focus_label=('Peter and the Wolf', 'Prokofiev'))
        assert key == ('Peter and the Wolf', 'Prokofiev')

    def test_playing_track_still_wins(self):
        """Regression guard: the album name must not replace the song."""
        now = NowPlaying(playing=True, stopped=False,
                         context_uri='spotify:album:x',
                         track_name='Chapter 2', track_artist='Author')
        key = Renderer._get_track_key(
            item=_item(), now_playing=now,
            is_loading=False, pending_focus_uri=None,
            requested_focus_uri=None, play_in_progress=False,
            focus_label=('Peter and the Wolf', 'Prokofiev'))
        assert key == ('Chapter 2', 'Author')

    def test_saved_track_still_wins(self):
        """Where we left off is more use than the album name we already see."""
        item = _item()
        item.current_track = {'name': 'Chapter 5', 'artist': 'Author'}
        key = Renderer._get_track_key(
            item=item, now_playing=NowPlaying(),
            is_loading=False, pending_focus_uri=None,
            requested_focus_uri=None, play_in_progress=False,
            focus_label=('Peter and the Wolf', 'Prokofiev'))
        assert key == ('Chapter 5', 'Author')


class TestTitleIsActuallyPainted:
    """A label that changes state but never repaints is the PR #13 failure."""

    @staticmethod
    def _renderer():
        pygame.init()
        pygame.font.init()
        # A real display surface: draw() calls convert(), which needs a video mode.
        screen = pygame.display.set_mode((720, 1280))
        return Renderer(screen, image_cache=_FakeCovers(), icons={})

    @staticmethod
    def _title_pixels(renderer):
        # Rotated text: the title runs along Y at a fixed X (see the portrait
        # convention in config.py), so its column is the one to scan.
        return sum(renderer.screen.get_at((TRACK_INFO_X, y))[:3] == COLORS['text_primary']
                   for y in range(1280))

    def test_label_reaches_the_screen(self):
        r = self._renderer()
        r.draw(_ctx(focus_label=('Peter and the Wolf', 'Prokofiev')))
        assert self._title_pixels(r) > 5

    def test_scrolling_leaves_the_title_area_blank(self):
        r = self._renderer()
        r.draw(_ctx(focus_label=None))
        assert self._title_pixels(r) == 0

    def test_settling_on_a_new_cover_repaints(self):
        """Without the repaint the old name stays under the new cover."""
        r = self._renderer()
        r.draw(_ctx(focus_label=None))
        assert r.draw(_ctx(focus_label=('Bedtime', ''))) is None, 'no full redraw'
        assert self._title_pixels(r) > 5


class _FakeCovers:
    """Enough of ImageCache to let a frame draw."""

    def get(self, path, size):
        return pygame.Surface((size, size))

    def get_dimmed(self, path, size):
        return self.get(path, size)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
