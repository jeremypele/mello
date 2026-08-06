"""
Track rows must tell two episodes of the same show apart.

Podcast episodes in a series share a long prefix ("Les Odyssées - ..."), and
the old rows cut every label at 22 characters — so a screen full of episodes
read as the same line repeated. Rows now run wider and wrap onto two lines.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

pygame = pytest.importorskip('pygame')

from mello.models import MenuState, NowPlaying   # noqa: E402
from mello.ui.context import RenderContext   # noqa: E402
from mello.ui.renderer import Renderer   # noqa: E402

EPISODES = [
    "Les Odyssées - La véritable histoire de Toutankhamon, pharaon d'Égypte",
    "Les Odyssées - Marie Curie, la femme aux deux prix Nobel",
]


def _renderer():
    pygame.init()
    pygame.font.init()
    return Renderer(pygame.Surface((720, 1280)), image_cache=None, icons={})


def _ctx(names, index=0):
    return RenderContext(
        items=[], selected_index=0, now_playing=NowPlaying(), scroll_x=0.0,
        drag_offset=0.0, dragging=False, is_sleeping=False, volume_pct=60,
        volume_icon='volume_low', volume_slider_open=False, delete_mode_id=None,
        pressed_button=None, is_loading=False, is_playing=False,
        menu_state=MenuState.TRACK_LIST, track_list_index=index,
        track_list=[SimpleNamespace(uri=f'u{i}', name=n) for i, n in enumerate(names)])


class TestLabels:

    def test_two_episodes_of_a_series_read_differently(self):
        """The whole point: the part that differs must reach the screen."""
        r = _renderer()
        rows = r._build_track_list_content(_ctx(EPISODES))
        assert rows[0][2] != rows[1][2]
        assert 'Toutankhamon' in ' '.join(rows[0][2])
        assert 'Marie Curie' in ' '.join(rows[1][2])

    def test_a_short_name_stays_on_one_line(self):
        r = _renderer()
        rows = r._build_track_list_content(_ctx(['Intro']))
        assert rows[0][2] == ['1. Intro']

    def test_the_current_track_is_still_the_accented_one(self):
        r = _renderer()
        rows = r._build_track_list_content(_ctx(EPISODES, index=1))
        assert rows[1][3] != rows[0][3]

    def test_tapping_still_targets_the_right_track(self):
        r = _renderer()
        r._draw_menu_frame(_ctx(EPISODES))
        assert 'track_0' in r.menu_button_rects and 'track_1' in r.menu_button_rects

    def test_rows_are_wider_than_the_old_menu_row(self):
        r = _renderer()
        r._draw_menu_frame(_ctx(EPISODES))
        # Rect(x, Y, H, W): height is the row's length along the text.
        assert r.menu_button_rects['track_0'].height > r._MENU_BTN_W

    def test_rows_stay_on_screen(self):
        r = _renderer()
        r._draw_menu_frame(_ctx(EPISODES))
        rect = r.menu_button_rects['track_0']
        assert rect.top >= 0 and rect.bottom <= 1280


class TestWrapping:

    @staticmethod
    def _font():
        pygame.font.init()
        return pygame.font.Font(None, 32)

    def test_nothing_overflows_the_row(self):
        font = self._font()
        for name in EPISODES:
            for line in Renderer._wrap_to_width(name, font, 400, 2):
                assert font.size(line)[0] <= 400

    def test_what_does_not_fit_is_marked_as_cut(self):
        font = self._font()
        lines = Renderer._wrap_to_width(' '.join(EPISODES * 3), font, 400, 2)
        assert len(lines) == 2 and lines[-1].endswith('…')

    def test_an_unbreakable_word_is_clipped_not_overflowed(self):
        font = self._font()
        lines = Renderer._wrap_to_width('A' * 200, font, 400, 2)
        assert font.size(lines[0])[0] <= 400

    def test_it_uses_the_room_it_has(self):
        """Regression guard on the 22-character cut that started this."""
        font = self._font()
        lines = Renderer._wrap_to_width(EPISODES[1], font, 600, 2)
        assert len(lines[0]) > 22


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
