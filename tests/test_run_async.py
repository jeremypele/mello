"""run_async must never keep the app alive after the main loop ends."""
import subprocess
import sys
from pathlib import Path


def test_exit_does_not_wait_for_a_running_task():
    # The status poller never returns. Before the fix, the process sat here
    # until systemd sent SIGKILL on every restart.
    script = (
        'import threading\n'
        'from mello.utils import run_async\n'
        'ran = threading.Event()\n'
        'run_async(lambda: (ran.set(), threading.Event().wait()))\n'
        'assert ran.wait(2), "task never ran"\n'
    )
    result = subprocess.run([sys.executable, '-c', script], timeout=5,
                            cwd=Path(__file__).parent.parent, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
