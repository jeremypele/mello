"""The speaker gets itself back onto a WiFi network it already knows."""
from mello.managers.wifi_keeper import WifiKeeper, OFFLINE_GRACE, RETRY_EVERY, QUICK_TRIES
from mello.managers.setup_menu import SetupMenu
from mello.models import MenuState


class World:
    """A speaker whose connection, clock and saved networks the test controls."""

    def __init__(self, saved=('Home',)):
        self.now = 1000.0
        self.online = True
        self.busy = False
        self.saved = list(saved)
        self.joins = 0
        self.join_fixes_it = False
        self.keeper = WifiKeeper(
            is_online=lambda: self.online, is_busy=lambda: self.busy,
            saved=lambda: self.saved, join=self._join, clock=lambda: self.now,
            link_summary=lambda: 'ap aa:bb, signal -60 dBm',
        )

    def _join(self):
        self.joins += 1
        if self.join_fixes_it:
            self.online = True
            return ''
        return 'Error: Connection activation failed'

    def wait(self, seconds):
        self.now += seconds
        self.keeper.check()


def test_leaves_a_working_connection_alone():
    w = World()
    for _ in range(20):
        w.wait(15)
    assert w.joins == 0
    assert w.keeper.take_restored() is False


def test_gives_networkmanager_a_moment_then_asks_a_few_times_every_minute():
    w = World()
    w.online = False
    w.wait(0)
    w.wait(OFFLINE_GRACE - 1)
    assert w.joins == 0                 # still NetworkManager's turn
    w.wait(1)
    assert w.joins == QUICK_TRIES       # a few attempts straight away
    w.wait(RETRY_EVERY - 1)
    assert w.joins == QUICK_TRIES       # not hammering the WiFi chip
    w.wait(1)
    assert w.joins == 2 * QUICK_TRIES
    for _ in range(8 * 60):             # a whole night of router trouble
        w.wait(60)
    assert w.joins == (2 + 8 * 60) * QUICK_TRIES   # it never gives up


def test_a_flaky_join_is_retried_straight_away():
    w = World()
    w.online = False
    w.wait(0)
    outcomes = iter(['Error: Connection activation failed', ''])

    def join():
        w.joins += 1
        problem = next(outcomes)
        w.online = not problem
        return problem
    w.keeper._join = join
    w.wait(OFFLINE_GRACE)
    assert w.joins == 2                 # second attempt, no minute lost
    w.wait(15)
    assert w.keeper.take_restored() is True


def test_stops_retrying_when_the_owner_opens_the_hotspot_mid_round():
    w = World()
    w.online = False
    w.wait(0)

    def join():
        w.joins += 1
        w.busy = True                   # owner started "+ New network"
        return 'Error: Connection activation failed'
    w.keeper._join = join
    w.wait(OFFLINE_GRACE)
    assert w.joins == 1


def test_reports_once_when_the_connection_is_back():
    w = World()
    w.online = False
    w.wait(0)
    w.join_fixes_it = True
    w.wait(OFFLINE_GRACE)
    assert w.joins == 1
    w.wait(15)
    assert w.keeper.take_restored() is True
    assert w.keeper.take_restored() is False
    w.wait(600)
    assert w.joins == 1


def test_keeps_off_the_wifi_chip_while_the_owner_sets_up_a_new_network():
    w = World()
    w.online = False
    w.busy = True
    for _ in range(10):
        w.wait(60)
    assert w.joins == 0
    w.busy = False                      # backed out of the hotspot screen
    w.wait(15)
    assert w.joins == QUICK_TRIES


def test_a_mello_that_never_joined_a_network_has_nothing_to_rejoin():
    w = World(saved=())
    w.online = False
    for _ in range(10):
        w.wait(60)
    assert w.joins == 0

def test_hotspot_counts_as_wifi_setup_running():
    menu = SetupMenu(
        catalog_manager=None, settings=None,
        on_toast=lambda m: None, on_invalidate=lambda: None, on_library_cleared=lambda: None,
    )
    assert menu.wifi_setup_running is False
    menu.state = MenuState.WIFI_AP
    assert menu.wifi_setup_running is True


def test_log_says_which_access_point_and_signal_when_wifi_drops(caplog):
    w = World()
    caplog.set_level('INFO', logger='mello.managers.wifi_keeper')
    w.online = False
    w.wait(15)
    w.online = True
    w.wait(15)
    lines = [r.getMessage() for r in caplog.records]
    assert 'WiFi keeper: offline (ap aa:bb, signal -60 dBm)' in lines
    assert any(l.startswith('WiFi keeper: back online') and 'signal -60 dBm' in l for l in lines)


def test_link_summary_reads_access_point_signal_and_power_save(monkeypatch):
    from types import SimpleNamespace
    from mello.managers import wifi_keeper
    outputs = {
        'link': 'Connected to 08:36:c9:73:ed:8f (on wlan0)\n\tSSID: Home\n\tfreq: 2452.0\n\tsignal: -66 dBm\n',
        'get': 'Power save: off\n',
    }
    monkeypatch.setattr(wifi_keeper.subprocess, 'run',
                        lambda cmd, **kw: SimpleNamespace(stdout=outputs[cmd[3]]))
    assert wifi_keeper.wifi_link_summary() == 'ap 08:36:c9:73:ed:8f, freq 2452.0, signal -66 dBm, power save: off'
