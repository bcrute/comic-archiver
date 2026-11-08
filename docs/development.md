# Working on Comic Archiver

Notes for building the app, checking a change and making a release. Using the app is covered in the
[README](../README.md) and on the [Releases page](https://github.com/bcrute/comic-archiver/releases/latest).

Android is under development. Its implementation blocks, evidence and release criteria are in
the [Android delivery roadmap](android-roadmap.md). The built-in reader and the independently developed
the web reader connection have their own scope and evidence in the
[reader and connected-library roadmap](reader-and-connected-library-roadmap.md). Connected source-checkout
acceptance does not qualify an Android reader or a public release. The commands below cover the existing
desktop app.

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

### What image verification checks

The importer identifies JPEG, PNG, GIF, and WebP from their bytes and checks the
format's structure before caching or publishing a page. It follows JPEG markers
and counted segments, PNG chunks and CRCs, GIF blocks, and WebP's RIFF chunk
boundaries. It stops at the image stream's logical end rather than requiring
that end to coincide with the end of the downloaded file. Source bytes,
including any trailing data, are preserved and included in the saved size and
SHA-256 hash. No image is repaired, stripped, or re-encoded in Original mode.

These checks catch incomplete structures and non-image responses. They do not
decode compressed pixels or prove that every codec value is valid. Archive
verification separately checks membership, order, sizes, and hashes; the
optional `--decode-images` check uses Pillow to verify and load every frame.
Pillow is already included in desktop packages. The structural checks use only
the standard library so the shared Android importer needs no new native codec.

This distinction follows [JPEG's marker syntax](https://www.w3.org/Graphics/JPEG/itu-t81.pdf),
the [PNG chunk format](https://www.w3.org/TR/png/), the
[GIF block grammar](https://www.w3.org/Graphics/GIF/spec-gif89a.txt), and
[WebP's container rules](https://developers.google.com/speed/webp/docs/riff_container).
WebP explicitly permits readers to ignore data beyond the declared container.
See also [Pillow's distinction between verification and pixel loading](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.verify).

### Install from this folder

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

Desktop dependencies are pinned, with the hash of each file, by `packaging/linux-requirements.txt` and
`packaging/windows-requirements.txt`. Both packagers audit the exact installed third-party versions before
writing a download, and include `requirements-frozen.txt` and `dependency-audit.json` in the app folder.
Regenerate them, as their first lines say, when updating dependencies.

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

Schema2 adds the universal Android APK while retaining all three desktop downloads
and required checks. It verifies the actual APK's original approved signing
certificate, compiled identity and ZIP alignment, and requires Android native,
device, real-output, update and audit evidence. Beta drafts remain prereleases
and are not promoted to latest. See the [Android release contract](android-release-contract.md)
for the exact fields, owner-configured fingerprint and SDK requirements. The
current debug phone build is not a publishable release artifact.

## Where things are

| Path | What it holds |
|---|---|
| `comic_archive/` | The app. `desktop_app.py` is the window, `app_runtime.py` starts and stops the pieces behind it, `qt_engine.py` is the web engine, `worker.py` does the saving, `window_pages.py` draws the window's pages from the plain models beside it (`dashboard_model.py`, `storage_model.py`), and `app_actions.py` is what they ask of the library. `osdep.py` holds what Linux and Windows do differently with files, locks and processes. |
| `comic_archive/adapters/` | What is specific to one site. |
| `packaging/`, `scripts/build_linux_app.py` | The Linux package. |
| `packaging/windows/`, `scripts/build_windows_app.py` | The Windows package and its installer. `scripts/make_windows_icon.py` redraws its icon. |
| `scripts/` | The checks above and the helpers they use. |
| `tests/` | Unit tests, and `mutations.json` for the mutation check. |

Image diagnostics use the downloader log collected by **Save log for support**.
A rejected page records its site, series/episode identifiers and page number,
detected format, received byte count, parser offset and fixed rejection reason
(for example `missing_eoi`, `truncated_chunk` or `crc_mismatch`). Accepted images
with trailing bytes record the logical image end, trailing byte count and zero/
nonzero breakdown, plus the original SHA-256 and confirmation that bytes were
preserved. Covers use `page=cover`. Routine images without tails do not add a
line. These records contain no image payload, titles, paths or asset URLs;
generic exception text remains excluded from support logs.

While a download is running, the desktop dashboard reads local progress once a
minute and recounts saved files hourly. It also reads on opening and after a
completed download or explicit storage change. Stopped, paused, finished and
unstarted sites receive no automatic dashboard reads or recounts. Sign-in and
explicit cloud tasks continue to receive status updates while they run. A pause
finishes its in-flight step before status reads stop. Saved-file scans run
separately from progress reads, with at most one
scan per site in flight. They read local archives, metadata and upload receipts;
these dashboard reads do not poll the content or cloud providers. Provider
request scheduling is unchanged. Progress becomes stale only after three missed
minutes. Slow library operations record their site, operation and duration in
the application log without recording request arguments or private results.

Medium and Slow use fixed image admission rates and concurrency limits at 50%
and 25% of Fast, respectively, for each provider. Metadata request spacing is
also increased for those choices. Network latency, archive work and provider
responses affect observed throughput, so the labels describe approximate rates.
Fast keeps its existing policy. Ordinary Android readers can use Medium/Slow
without requiring the native Fast engine. Explicit CLI `normal` pacing and queue
argument validation retain their existing limits.

Dashboard cards keep the same fields and row order while loading, downloading,
waiting, paused or finished. Status and Progress carry changing details; unknown
values use an em dash. Values and messages stay on one line, with long plain
text elided visually and preserved in tooltips and accessible text. Empty
messages and optional hints retain their layout space.

The desktop has Dashboard and Settings pages. **Open library folder** opens the
chosen library root in the system file manager. Storage counts are informational;
there is no in-app file browser or viewer. Settings retain folder selection,
cloud configuration and the provider link. The shared archive/export APIs remain
available to the independently approved file source and platform integrations.

During the final local integrity check, the existing dashboard rows show the
archive count checked, archive bytes checked and time spent checking. The note
explains that large libraries can take minutes and that this phase makes no
provider requests. Progress checkpoints and diagnostic counts are published at
most once per minute, plus phase boundaries; they reuse the verification pass
and do not trigger recounts. Logs include the stage, checked/total archives,
bytes, integrity failures and elapsed seconds without filenames or account data.

The desktop distinguishes possible downloads in unchecked series from episodes
actually queued. If only speculative work remains, Time left is unknown and
Space says no downloads are queued, while retaining the free-space figure. The
hint explains that those series may contain nothing new. Mixed estimates are
labeled as estimates; the underlying conservative planning figures are retained.

The selected library location is a shared root. Tapas, Webtoon and Tappytoon
each use a sibling provider directory containing their Library and private
state. The original Tapas layout is moved by same-filesystem directory renames
when opened while idle. A journal blocks new writers and worker launches during
an interrupted move and allows the next start to resume. Shared settings and
other providers stay at the root; conflicting destinations are never merged.
There is no copy fallback.

Migration durability checks terminate a separate process at journal publication,
each directory rename and journal removal, then verify recovery with every
original file's bytes, identity and timestamp intact. Injected directory-flush
failures also retain recoverable state. These development checks do not replace
real Windows filesystem validation or the full release gates.

On Windows, existing private state trees receive protected access rules for the
current account, SYSTEM and administrators before use. This includes copied
sign-ins, browser profiles and cloud keys. Repair never walks archive folders;
new state inherits those rules. A location without persistent Windows access
controls cannot hold sign-in state. Worker refreshes load this protection too.

For isolated CI, the helper-install smoke accepts
`COMIC_ARCHIVER_CI_HELPER_DOWNLOAD_BASE` for the existing acquiring cache.
The upstream version, size bounds, archive checks and pinned SHA-256 remain
mandatory. Ordinary application downloads use the upstream address.
