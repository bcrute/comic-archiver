"""Native worker identity and the actual app window against synthetic local data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from comic_archive import desktop_app, osdep
from comic_archive.macos import network_probe
from build_macos_app import build_network_probe


def main():
    if sys.platform != 'darwin':
        raise RuntimeError('This acceptance check requires macOS')
    output = Path(os.environ['CI_ARTIFACTS_DIR'])/'macos-native'
    output.mkdir(parents=True, exist_ok=True)
    build_network_probe(Path(desktop_app.__file__).parent/'native')
    observed = network_probe()
    if observed != {'connection':'wired', 'metered':False}:
        raise RuntimeError('The native VM connection should be unmetered Ethernet: '+repr(observed))
    marker = 'comic archiver worker identity fixture'
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', marker])
    try:
        # Process creation has completed; psutil may briefly observe exec still in flight.
        import time
        until = time.monotonic()+10
        while not osdep.runs_command(process.pid, [marker]) and time.monotonic() < until:
            time.sleep(.05)
        if not osdep.runs_command(process.pid, [marker]) or osdep.runs_command(process.pid, ['worker identity']):
            raise RuntimeError('Native process argument identity failed')
    finally:
        process.terminate()
        process.wait(timeout=10)
    if osdep.runs_command(process.pid, [marker]):
        raise RuntimeError('Exited worker still identified as active')
    quit_window = desktop_app.Shell.quit

    def capture(shell):
        if not shell.window.grab().save(str(output/'app-window.png')):
            raise RuntimeError('App window capture failed')
        return quit_window(shell)

    with tempfile.TemporaryDirectory(prefix='comic native home ') as temporary:
        home = Path(temporary)
        with patch.dict(os.environ, {'HOME':str(home), 'XDG_STATE_HOME':str(home/'state'),
                                     'QT_QPA_PLATFORM':'cocoa'}), patch.object(desktop_app.Shell, 'quit', capture):
            status = desktop_app.main(['--self-test'])
    report = {'native_worker_identity':True, 'native_network':observed, 'native_cocoa_app_self_test':status == 0,
              'screenshot':'app-window.png', 'browser_sandbox_disabled':False}
    (output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    if status:
        raise RuntimeError('Native app self-test failed')


if __name__ == '__main__':
    main()
