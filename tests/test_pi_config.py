"""
Static checks for Pi deployment files.
"""
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_librespot_service_is_not_part_of_ui_service():
    service = (ROOT / 'pi/systemd/mello-librespot.service.template').read_text()

    assert 'PartOf=mello-native.service' not in service


def test_migration_014_registered_for_librespot_dependency_update():
    migrate = (ROOT / 'pi/migrate.sh').read_text()

    assert '_migrate_014()' in migrate
    assert 'run_migration "014" "Keep librespot independent of UI sleep/restarts"' in migrate


# --- Ported from upstream mello: hung-engine restart permission, WiFi power save ---

def _migration_body(n: str) -> str:
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    return migrate.split(f'_migrate_{n}() {{', 1)[1].split('\n}', 1)[0]


def _run_migration_017(sudoers_dir: Path, migration: str = '017'):
    # Like /etc/sudoers.d (root only, mode 750), the folder is closed to the
    # app user: only sudo sees inside. A plain `[ -f ]` there always failed on
    # devices, while an open test folder hid that.
    script = textwrap.dedent(
        f'''\
        set -euo pipefail
        MELLO_SUDOERS_DIR={str(sudoers_dir)!r}
        log() {{ :; }}
        sudo() {{
          chmod 755 "$MELLO_SUDOERS_DIR"
          local rc=0
          [ "$1" = visudo ] || "$@" || rc=$?
          chmod 000 "$MELLO_SUDOERS_DIR"
          return $rc
        }}
        _migrate_017() {{
        {_migration_body('017')}
        }}
        _migrate_018() {{
        {_migration_body('018')}
        }}
        _migrate_{migration}
        '''
    )
    sudoers_dir.chmod(0o000)
    try:
        return subprocess.run(['bash'], input=script, text=True, capture_output=True)
    finally:
        sudoers_dir.chmod(0o755)


def test_librespot_recovery_permission_is_installed_and_migrated():
    setup = (ROOT / 'pi/setup.sh').read_text()
    migrate = (ROOT / 'pi/migrate.sh').read_text()

    assert '/bin/systemctl restart mello-librespot' in setup
    assert '/bin/systemctl restart mello-librespot' in _migration_body('017')
    assert 'run_migration "017" "Allow automatic librespot recovery"' in migrate
    assert 'run_migration "018"' in migrate


def test_migration_018_installs_the_rule_017_skipped(tmp_path):
    sudoers = tmp_path / 'mello-wifi'
    sudoers.write_text('mello ALL=(ALL) NOPASSWD: /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native\n')

    result = _run_migration_017(tmp_path, migration='018')

    assert result.returncode == 0, result.stderr
    assert sudoers.read_text().count('/bin/systemctl restart mello-librespot') == 1


def test_migration_017_updates_current_sudoers_rule(tmp_path):
    sudoers = tmp_path / 'mello-wifi'
    sudoers.write_text('mello ALL=(ALL) NOPASSWD: /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native\n')

    result = _run_migration_017(tmp_path)

    assert result.returncode == 0, result.stderr
    assert sudoers.read_text().count('/bin/systemctl restart mello-librespot') == 1


def test_migration_017_updates_legacy_sudoers_rule(tmp_path):
    sudoers = tmp_path / 'berry-wifi'
    sudoers.write_text('berry ALL=(ALL) NOPASSWD: /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native\n')

    result = _run_migration_017(tmp_path)

    assert result.returncode == 0, result.stderr
    assert '/bin/systemctl restart mello-librespot' in sudoers.read_text()


def test_migration_017_is_idempotent(tmp_path):
    sudoers = tmp_path / 'mello-wifi'
    sudoers.write_text('mello ALL=(ALL) NOPASSWD: /bin/systemctl start mello-librespot, /bin/systemctl restart mello-native\n')

    assert _run_migration_017(tmp_path).returncode == 0
    assert _run_migration_017(tmp_path).returncode == 0

    assert sudoers.read_text().count('/bin/systemctl restart mello-librespot') == 1


def test_migration_017_rejects_unexpected_rule_format(tmp_path):
    sudoers = tmp_path / 'mello-wifi'
    original = 'mello ALL=(ALL) NOPASSWD: /bin/systemctl restart mello-native\n'
    sudoers.write_text(original)

    result = _run_migration_017(tmp_path)

    assert result.returncode != 0
    assert sudoers.read_text() == original


def test_wifi_power_save_is_switched_off_on_new_and_existing_speakers(tmp_path):
    setup = (ROOT / 'pi/setup.sh').read_text()
    migrate = (ROOT / 'pi/migrate.sh').read_text()
    conf = (ROOT / 'pi/networkmanager/99-mello-wifi-powersave.conf').read_text()
    assert 'wifi.powersave = 2' in conf
    assert '99-mello-wifi-powersave.conf /etc/NetworkManager/conf.d/' in setup
    assert 'run_migration "016" "Keep the WiFi chip awake (no power save)"' in migrate

    home = tmp_path / 'home'
    (home / 'mello/pi/networkmanager').mkdir(parents=True)
    (home / 'mello/pi/networkmanager/99-mello-wifi-powersave.conf').write_text(conf)
    conf_dir = tmp_path / 'nm'
    script = textwrap.dedent(f'''\
        set -euo pipefail
        HOME={str(home)!r}
        MELLO_NM_CONF_DIR={str(conf_dir)!r}
        log() {{ :; }}
        sudo() {{ case "$1" in *iw|nmcli|apt-get) return 0;; esac; "$@"; }}
        _migrate_016() {{
        {_migration_body('016')}
        }}
        _migrate_016
        ''')
    result = subprocess.run(['bash'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (conf_dir / '99-mello-wifi-powersave.conf').read_text() == conf


def test_menu_update_runs_outside_mello_so_the_restart_does_not_cut_it_off(monkeypatch, tmp_path):
    import mello.managers.setup_menu as sm
    monkeypatch.setattr(sm.shutil, 'which', lambda name: '/usr/bin/systemd-run')
    cmd = sm._update_command(tmp_path / 'mello-update.log')
    assert cmd[:3] == ['systemd-run', '--user', '--wait']   # a job of its own, result still reported
    assert f'StandardOutput=append:{tmp_path}/mello-update.log' in cmd
    assert cmd[-2:] == ['bash', f'{sm._REPO_DIR}/pi/auto-update.sh']


def test_menu_update_still_runs_without_systemd_run(monkeypatch, tmp_path):
    import mello.managers.setup_menu as sm
    monkeypatch.setattr(sm.shutil, 'which', lambda name: None)
    cmd = sm._update_command(tmp_path / 'u.log')
    assert cmd[0] == 'bash' and 'auto-update.sh' in cmd[-1] and str(tmp_path / 'u.log') in cmd[-1]
