"""Alarm screens actually draw, and their taps land on the rects they draw."""
import os
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

pygame = pytest.importorskip('pygame')

from mello.models import Alarm, MenuState, NowPlaying
from mello.ui.context import RenderContext
from mello.ui.renderer import Renderer


def _renderer():
    pygame.init()
    pygame.font.init()
    screen = pygame.Surface((720, 1280))
    return Renderer(screen, image_cache=None, icons={})


def _ctx(**kw):
    base = dict(
        items=[], selected_index=0, now_playing=NowPlaying(), scroll_x=0.0,
        drag_offset=0.0, dragging=False, is_sleeping=False,
        volume_pct=60, volume_icon='volume_low', volume_slider_open=False,
        delete_mode_id=None, pressed_button=None, is_loading=False, is_playing=False,
    )
    base.update(kw)
    return RenderContext(**base)


def _alarms():
    return [
        Alarm(id='aa', hour=7, minute=0, days=[0, 1, 4], repeat=True),
        Alarm(id='bb', hour=9, minute=0, days=[5], repeat=False,
              date='2026-08-08', enabled=False),
    ]


def _painted(renderer) -> int:
    """Count non-black pixels — a screen that drew nothing is a broken screen."""
    surf = pygame.surfarray.array3d(renderer.screen)
    return int((surf.sum(axis=2) > 0).sum())


# --- The list ---

def test_alarm_list_draws_a_row_per_alarm():
    r = _renderer()
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_LIST, alarms=_alarms()))

    for alarm_id in ('aa', 'bb'):
        assert f'alarm_open_{alarm_id}' in r.menu_button_rects
        assert f'alarm_toggle_{alarm_id}' in r.menu_button_rects
    assert 'alarm_add' in r.menu_button_rects


def test_alarm_list_row_and_toggle_do_not_overlap():
    r = _renderer()
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_LIST, alarms=_alarms()))

    label = r.menu_button_rects['alarm_open_aa']
    toggle = r.menu_button_rects['alarm_toggle_aa']
    assert not label.colliderect(toggle), 'tapping the label would also hit the toggle'


def test_empty_alarm_list_still_offers_add():
    r = _renderer()
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_LIST, alarms=[]))
    assert 'alarm_add' in r.menu_button_rects
    assert _painted(r) > 0


# --- The editor ---

def test_alarm_edit_draws_every_control():
    r = _renderer()
    alarm = _alarms()[0]
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_EDIT, alarm_edit=alarm))

    expected = {'alarm_hour_minus', 'alarm_hour_plus',
                'alarm_minute_minus', 'alarm_minute_plus',
                'alarm_repeat', 'alarm_sound', 'alarm_save', 'alarm_delete'}
    assert expected <= set(r.menu_button_rects)


def test_a_new_draft_offers_save_but_not_delete():
    r = _renderer()
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_EDIT,
                            alarm_edit=_alarms()[0], alarm_is_new=True))

    assert 'alarm_save' in r.menu_button_rects
    assert 'alarm_delete' not in r.menu_button_rects


def test_alarm_edit_draws_seven_day_chips_that_do_not_overlap():
    r = _renderer()
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_EDIT, alarm_edit=_alarms()[0]))

    chips = [r.menu_button_rects[f'alarm_day_{d}'] for d in range(7)]
    assert len(chips) == 7
    for i, a in enumerate(chips):
        for b in chips[i + 1:]:
            assert not a.colliderect(b), 'day chips overlap — taps would be ambiguous'


def test_chips_stay_inside_the_row_width():
    r = _renderer()
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_EDIT, alarm_edit=_alarms()[0]))

    chips = [r.menu_button_rects[f'alarm_day_{d}'] for d in range(7)]
    span = max(c.y + c.height for c in chips) - min(c.y for c in chips)
    assert span <= r._MENU_BTN_W


def test_alarm_edit_with_no_alarm_open_draws_nothing_and_does_not_crash():
    r = _renderer()
    r._draw_menu_frame(_ctx(menu_state=MenuState.ALARM_EDIT, alarm_edit=None))
    assert not [k for k in r.menu_button_rects if k.startswith('alarm_')]


def test_one_shot_editor_shows_the_resolved_date():
    r = _renderer()
    alarm = _alarms()[1]
    items = r._build_alarm_edit_content(
        _ctx(menu_state=MenuState.ALARM_EDIT, alarm_edit=alarm,
             alarm_edit_when='Sat 8 Aug'))
    assert any(i[0] == 'text' and 'Sat 8 Aug' in i[1] for i in items)


def test_repeating_editor_hides_the_date_line():
    r = _renderer()
    items = r._build_alarm_edit_content(
        _ctx(menu_state=MenuState.ALARM_EDIT, alarm_edit=_alarms()[0],
             alarm_edit_when='Sat 8 Aug'))
    assert not any(i[0] == 'text' for i in items)


# --- Rows must not paint over each other ---

@pytest.mark.parametrize('state,builder,extra', [
    (MenuState.ALARM_LIST, '_build_alarm_list_content', {'alarms': _alarms()}),
    (MenuState.ALARM_EDIT, '_build_alarm_edit_content', {'alarm_edit': _alarms()[0]}),
])
def test_rows_do_not_overlap(state, builder, extra):
    r = _renderer()
    items = getattr(r, builder)(_ctx(menu_state=state, **extra))

    extents = [r._menu_row_extent(i[0]) for i in items]
    top = r._MENU_CONTENT_TOP + extents[0]
    prev_low = None
    for extent in extents:
        x = top - extent
        if prev_low is not None:
            assert x + extent <= prev_low, 'a row paints over the one above it'
        prev_low = x
        top = x - r._MENU_BTN_GAP


# --- The ringing face ---

def test_ringing_face_paints_and_claims_no_buttons():
    r = _renderer()
    ctx = _ctx(alarm_ringing=True, alarm_time_label='07:00')
    r.draw(ctx)

    assert _painted(r) > 0
    # Nothing tappable: every tap is a dismiss, handled in the app loop.
    assert not r.menu_button_rects


def test_ringing_face_wins_over_sleep_and_menu():
    r = _renderer()
    ctx = _ctx(alarm_ringing=True, alarm_time_label='07:00',
               is_sleeping=True, menu_state=MenuState.MAIN,
               sleep_clock_text='07:00')
    r.draw(ctx)
    assert not r.menu_button_rects, 'menu drew underneath a ringing alarm'


# --- The sleep clock's bell ---

def test_sleep_clock_draws_the_bell_when_an_alarm_is_near():
    r = _renderer()
    with_bell = _ctx(is_sleeping=True, sleep_clock_text='22:15',
                     next_alarm_label='07:00')
    r.draw(with_bell)
    lit = _painted(r)

    r2 = _renderer()
    r2.draw(_ctx(is_sleeping=True, sleep_clock_text='22:15'))
    assert lit > _painted(r2), 'bell added no pixels'


def test_bell_and_moon_both_draw():
    r = _renderer()
    r.draw(_ctx(is_sleeping=True, sleep_clock_text='22:15',
                sleep_icon='moon', next_alarm_label='07:00'))
    moon_and_bell = _painted(r)

    r2 = _renderer()
    r2.draw(_ctx(is_sleeping=True, sleep_clock_text='22:15', sleep_icon='moon'))
    assert moon_and_bell > _painted(r2), 'bell was hidden by the moon'
