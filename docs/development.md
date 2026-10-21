# Working on Comic Archiver

Notes for building the app, checking a change and making a release. Using the app is covered in the
[README](../README.md) and on the [Releases page](https://github.com/bcrute/comic-archiver/releases/latest).

Android is the next planned platform. Its implementation blocks, feasibility gates and release criteria are in
the [Android delivery roadmap](android-roadmap.md). The commands below cover the existing desktop app.

## Why the history here is short

This GitHub repository is a public mirror. Comic Archiver is developed in a private, self-hosted Gitea server,
where it went through more than 200 commits before this copy was published. That history held personal details,
such as local paths, machine names and notes from testing with real accounts. So the public copy starts again from
one clean commit (October 2026), and new work arrives here as it is committed there.

Every release is checked on the project's own build machines before publication. More than 1,500 automated tests
run, and the window's pages are drawn by Qt off-screen and clicked through. A mutation check then puts more than 300
deliberate faults into the code, one at a time, and the tests must catch every one. The Windows package and its
installer are tested on Windows 11.

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
uv run --extra test python scripts/build_linux_app.py --smoke
```

This writes `dist/ComicArchiver-<version>-linux-x86_64.tar.gz` with its own Python and Qt, so the person using it
installs nothing else. `--smoke` starts the built app off-screen and checks that it comes up.

## Build the Windows package

```sh
uv run --extra test python scripts/build_windows_app.py                       # on Linux or Windows: the app folder and its zip
uv run --extra test python scripts/build_windows_app.py --smoke --installer    # on Windows, with Inno Setup 6 or 7 installed
```

The first line writes `dist/ComicArchiver-<version>-windows-x86_64.zip`. The folder in it is put together from files
alone (the Python for Windows and the Windows packages), so a Linux host can make it. Of Qt and of Python only
what the app loads goes into it. Starting the app (`--smoke`) and building
`ComicArchiver-<version>-windows-x86_64-setup.exe` (`--installer`) need Windows. The installer puts the app in the
person's own programs folder and never asks for an administrator.

Windows uses Python's signed embeddable runtime, pinned to its published SHA-256.
On a CI host with restricted downloads, stage that exact vendor archive in a directory and set
`COMIC_ARCHIVER_BUILD_DOWNLOAD_CACHE` to that directory; the packager verifies its checksum before using it.
The installer itself is not signed yet.

On Windows the tests are run directly, since some of them are for Linux only and are skipped there:

```sh
python -m unittest discover -s tests
```

Many of them make symlinks, which an ordinary Windows account may do only with Developer Mode switched on.

## Check a change

Run these commands through the shared CI fleet helper, using its private host configuration. All automated
suites and builds run on the project's build machines, including a Windows VM; unavailable capacity leaves checks
pending. Install the test and desktop extras and uv in that remote environment first.

```sh
python3 scripts/validate.py --prepare      # tests, coverage floors, Firefox checks, a fresh install
python3 scripts/mutation_check.py          # every listed fault must be caught by a test
python3 scripts/qt_engine_smoke.py         # the app's real web engine against a local stand-in site
```

None of these sign in anywhere or contact a comic site. They are heavy; this project runs them on its CI hosts
rather than on a development PC. Validation includes Ruff lint and security rules, and pip-audit.

Desktop dependencies are constrained by `packaging/linux-requirements.txt` and
`packaging/windows-requirements.txt`. Both packagers audit the exact installed third-party versions before
writing a download, and include `requirements-frozen.txt` and `dependency-audit.json` in the app folder.
Update the constraints deliberately when updating dependencies.

For a release, validate and build the exact commit on the fleet, stage all three downloads and
`release-evidence.json` in a draft GitHub release authored by the repository owner. Set the draft's target to
the full validated commit SHA. Upload the evidence using the owner's release credential: GitHub records its
uploader and SHA-256, which the publisher checks against the downloaded evidence. Contents write permission
is sufficient; no commit-status permission is needed.
The evidence records the commit, version, successful gates, suite totals without skips, and each download's
size and SHA-256. The draft may also hold `SHA256SUMS.txt` listing exactly those three hashes; any other asset
stops publication. `scripts/verify_release.py` defines and checks this contract. Push the matching version tag
to Gitea so its mirror retains it. GitHub's Release workflow verifies the owner upload and downloads, then publishes
the draft. It performs no builds or suites.

## Where things are

| Path | What it holds |
|---|---|
| `comic_archive/` | The app. `desktop_app.py` is the window, `app_runtime.py` starts and stops the pieces behind it, `qt_engine.py` is the web engine, `worker.py` does the saving, `window_pages.py` draws the window's pages from the plain models beside it (`dashboard_model.py`, `storage_model.py`, `library_model.py`), and `app_actions.py` is what they ask of the library. `osdep.py` holds what Linux and Windows do differently with files, locks and processes. |
| `comic_archive/adapters/` | What is specific to one site. |
| `packaging/`, `scripts/build_linux_app.py` | The Linux package. |
| `packaging/windows/`, `scripts/build_windows_app.py` | The Windows package and its installer. `scripts/make_windows_icon.py` redraws its icon. |
| `scripts/` | The checks above and the helpers they use. |
| `tests/` | Unit tests, and `mutations.json` for the mutation check. |
