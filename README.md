# Comic Archiver

Comic Archiver is a desktop app that saves the comics and novels your own account can already read, as ordinary
files on your computer: one CBZ per comic episode, one EPUB per novel chapter, each with a JSON description. You
sign in on the site itself inside the app's window, press Go, and it works through your library. It can be
stopped and started again at any time and carries on where it left off.

Tapas and Tappytoon work. On Tappytoon, Go reads your bought comics first, then your subscribed and recently
read ones, and saves the free chapters and the ones you bought or rent; a rented chapter is noted with the end
of its rental. Sign in with your email and password (a password manager's copy and paste works; browser
extensions do not run in the app's window). Webtoon is listed in the app as "coming later".

## Download

Get the newest version from the [Releases page](https://github.com/bcrute/comic-archiver/releases/latest):
the setup file for Windows 10 or 11, or the `.tar.gz` for Linux. Nothing else needs installing, and nothing has
to be built. The release notes say what to do the first time, including the warning Windows shows because the
app is not signed yet.

## Status

| | |
|---|---|
| Linux (x86_64) | Self-contained package builds and passes its self-test. |
| Windows (x86_64) | In progress. The package and its installer build; on Windows 11 the tests pass and the installer installs, starts and uninstalls the app without an administrator. Nothing in it is signed yet: Windows stops the downloaded installer with "Windows protected your PC" (choose More info, then Run anyway), and the installer's window and the app's first start can each take up to a minute while Windows checks the files. |
| macOS | Not planned yet. |

## Why the history here is short

This GitHub repository is a public mirror. Comic Archiver is developed in a private, self-hosted Gitea server,
where it went through more than 200 commits before this copy was published. That history held personal details,
such as local paths, machine names and notes from testing with real accounts. So the public copy starts again from
one clean commit (October 2026), and new work arrives here as it is committed there.

Every change is checked before it is committed. About 1,800 automated tests run, and the app's pages are driven
in a real browser in four layouts. A mutation check then puts more than 300 deliberate faults into the code, one
at a time, and the tests must catch every one. The Windows package and its installer are tested on Windows 11.

## Run it from this folder

```sh
uv tool install --editable . --with 'PySide6>=6.11.2,<6.12' --with 'playwright==1.63.0' --with 'Pillow>=10'
comic-archiver
```

The library is the folder `Comic Archiver` in your home folder unless you choose another in the app. The app's own
settings are in `~/.local/state/comic-archiver`. The sign-in is kept in a private folder inside the library and
never leaves the computer.

## Build the Linux package

```sh
python3 scripts/build_linux_app.py --smoke
```

This writes `dist/ComicArchiver-<version>-linux-x86_64.tar.gz` with its own Python and Qt, so the person using it
installs nothing else. `--smoke` starts the built app off-screen and checks that it comes up.

## Build the Windows package

```sh
python3 scripts/build_windows_app.py                       # on Linux or Windows: the app folder and its zip
python scripts/build_windows_app.py --smoke --installer    # on Windows, with Inno Setup 6 or 7 installed
```

The first line writes `dist/ComicArchiver-<version>-windows-x86_64.zip`. The folder in it is put together from files
alone (the Python for Windows and the Windows packages), so a Linux host can make it. Of Qt and of Python only
what the app loads goes into it. Starting the app (`--smoke`) and building
`ComicArchiver-<version>-windows-x86_64-setup.exe` (`--installer`) need Windows. The installer puts the app in the
person's own programs folder and never asks for an administrator.

On Windows the tests are run directly, since some of them are for Linux only and are skipped there:

```sh
python -m unittest discover -s tests
```

Many of them make symlinks, which an ordinary Windows account may do only with Developer Mode switched on.

## Check a change

```sh
python3 scripts/validate.py --prepare      # tests, coverage floors, page checks in Firefox, a fresh install
python3 scripts/mutation_check.py          # every listed fault must be caught by a test
python3 scripts/qt_engine_smoke.py         # the app's real web engine against a local stand-in site
```

None of these sign in anywhere or contact a comic site. They are heavy; this project runs them on its CI hosts
rather than on a development PC.

## Where things are

| Path | What it holds |
|---|---|
| `comic_archive/` | The app. `desktop_app.py` is the window, `app_runtime.py` starts and stops the pieces behind it, `qt_engine.py` is the web engine, `worker.py` does the saving, `static/` holds the pages. `osdep.py` holds what Linux and Windows do differently with files, locks and processes. |
| `comic_archive/adapters/` | What is specific to one site. |
| `tapas_archive/` | The package's old name, kept so existing installs keep working. |
| `packaging/`, `scripts/build_linux_app.py` | The Linux package. |
| `packaging/windows/`, `scripts/build_windows_app.py` | The Windows package and its installer. `scripts/make_windows_icon.py` redraws its icon. |
| `scripts/` | The checks above and the helpers they use. |
| `tests/` | Unit tests, and `mutations.json` for the mutation check. |
| `parked/` | Work that is set aside and not part of the app or its checks. Currently the Android prototype. |

## The web version and bringing its experiments over

New ideas, such as cloud storage, are tried first in an experimental web version that lives on its own line of
history, not in this tree. The tag `web-split-point` marks the commit of that line this tree was built from.

```sh
git log --oneline web-split-point..<web branch>                    # what has been tried there since
git diff web-split-point <web branch> -- comic_archive/cloud.py    # what changed in one file
git checkout <web branch> -- comic_archive/some_module.py          # take one file over as it is
```

Files this tree no longer has (the browser version's folder chooser, the `desktop`, `status`, `list-series` and
`prepare-*` commands, the throughput experiments) are still there. The code that saves episodes, including the
cloud upload path, is shared, so most experiments arrive as changes to files that exist in both places. The pages
in `comic_archive/static/` have diverged; changes to them are carried over by hand.

That version's own documents (its guide, the research on the site and the older plans) stay with it and are not
in this tree.

## Licence

MIT; see [LICENSE](LICENSE). Bundled components are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
