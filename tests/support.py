"""What the tests expect differently on Windows."""
import os
import sys
import unittest

# Windows keeps no permission bits beyond read-only: a file there is private through the folder it lies in,
# and Python reports every file as readable and writable by all.
PRIVATE_FILE, PRIVATE_FOLDER = (0o666, 0o777) if os.name == 'nt' else (0o600, 0o700)
# Where the app keeps its own settings, by the name of the variable that moves it and its place in the home folder.
STATE_HOME, STATE_DEFAULT = ('LOCALAPPDATA', 'AppData/Local') if os.name == 'nt' else ('XDG_STATE_HOME', '.local/state')


def home_environment(home, state=None):
    """Variables that make this folder the person's home, and that one the place for the app's settings."""
    values = {'HOME': str(home), 'USERPROFILE': str(home)}
    return values if state is None else values | {STATE_HOME: str(state)}


def linux_only(reason):
    """Leave a whole test file out on Windows; called at the top of a file whose subject Windows does not have."""
    if os.name == 'nt':
        raise unittest.SkipTest(reason)


def helper_program(path, body):
    """A stand-in for a program the app starts, written as Python. Windows starts it through a batch file."""
    if os.name == 'nt':
        script = path.with_name(path.name + '.py')
        script.write_text(body)
        program = path.with_name(path.name + '.cmd')
        program.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return program
    path.write_text(f'#!{sys.executable}\n' + body)
    path.chmod(0o700)
    return path


# How a helper that will not stop is stopped is POSIX signalling. Windows ends a program outright, and a batch
# file's own Python would outlive it, so there the real helper is tried instead (in the Windows test machine).
posix_signals = unittest.skipIf(os.name == 'nt', 'Stopping a helper is POSIX signalling; Windows ends it outright.')


# A download keeps the file it verified even when the library replaces it meanwhile. Windows gives the same
# promise the other way round: it refuses the replacement while the file is being read, and the library waits.
replaces_open_files = unittest.skipIf(os.name == 'nt', 'Windows does not replace a file that is being read.')
