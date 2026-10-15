# Android reference prototype

> Parked on 2026-10-13: not being developed, not shipped, and not part of the project's gate. Everything it needs
> is in this folder. Its checks run with `python3 -m unittest discover -s parked/android/tests` from the top of
> the repository; the Java check is skipped unless a JDK is present (`build_android.py --setup` fetches one).

This is a small native Java implementation of the desktop import contract. It handles one episode at a time, using an app-owned WebView for ordinary Tapas sign-in and authenticated metadata, then sequential HTTPS image retrieval and original-byte CBZ/JSON export.

The development APK has been installed on one test phone running Android 17/API 37. The app opens Tapas's normal `/account/signin` page. Six offline archive checks passed on that phone; an authenticated Tapas import and system-picker export remain to test. Do not treat a loading sign-in page, synthetic fixture tests or a saved desktop archive as proof of mobile source compatibility.

## Use the installed app

1. Open **Comic Archiver prototype** and tap **Sign in**. Use the account's usual login method on Tapas itself. No Tapas password, cookie or token belongs in a terminal or chat.
2. Enter an exact `https://tapas.io/episode/<number>` URL. The initial test URL is `https://tapas.io/episode/2864705`.
3. Tap **Test & archive**. The tool verifies identity and current episode authorization, reads the exact episode's ordered manifest, and downloads original image bytes without scrolling. A refusal or unknown response holds the import.
4. Use **Export CBZ** and **Export JSON** to choose destinations with Android's system document picker. Both formats remain usable independently of this application or Tapas. Private working files survive an app restart but are removed by uninstalling/clearing app data; export files before doing that.

**Cancel** preserves previously verified pages. A later attempt checks fresh authorization before accessing the cache. The first slice has no account crawler, bulk series import, mobile daily scheduler, cover archiving or background service. Leave it in the foreground while importing. Screen rotation/process termination cancels work; start the episode again to reuse verified pages. Continuous operation with the screen off is untested.

OAuth providers may refuse embedded-browser sign-in. Do not change browser fingerprints, move cookies from unrelated phone apps or bypass a provider refusal. Record the limitation and evaluate an ordinary supported session-acquisition route before expanding the mobile importer.

## Build on the CI hosts

The current bootstrap targets Linux x86-64. Queue builds and automated suites on
the CI hosts; unavailable capacity leaves them pending. Do not build or run suites
on the development PC.
The job downloads pinned official Google SDK/build tools 36 and an Adoptium JDK
17 into its ignored private toolchain directory, verifies upstream checksums,
then uses standalone `javac`, `d8`, `aapt2`, `zipalign` and `apksigner`. It does
not install global tools or touch devices.

```sh
# On a CI host:
python3 -m venv --without-pip .venv
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python parked/android/build_android.py --setup
# Read-only device selection on the development PC, after verified build retrieval:
.venv/bin/python parked/android/android_preflight.py
```

The result is `parked/android/build/comic-archive-debug.apk` in the remote job,
signed with a job development key. Retrieve the verified artifact before
installing it on an explicitly selected device. An upgrade must preserve the
existing app signing identity; do not uninstall or clear a user's working data
to work around a signing mismatch. This debug build is for testing, not a store
or public release. Minimum API 26 and target/compile API 36 are declared; actual
runtime testing is currently limited to API 37. Other Android versions and
distribution routes require their own checks.

Preflight enumerates transports without exposing device serials. With multiple ADB targets it selects a unique USB transport, or requires an explicit current candidate number. A connected Pixel/Nexus without an ADB interface is reported rather than silently replaced with an emulator. Only the offered physical phone was used for this test; the separate emulator was untouched.

To install on an intentionally selected authorized device with standard ADB:

```sh
adb -s YOUR_SELECTED_DEVICE_SERIAL install -r parked/android/build/comic-archive-debug.apk
adb -s YOUR_SELECTED_DEVICE_SERIAL shell am start -n org.comicarchive.prototype/.MainActivity
```

Do not run ambiguous device commands or stop/disconnect other ADB targets. USB debugging requires the ordinary on-device developer-options and computer-authorization setup.

## Boundaries and components

| Component | Responsibility |
| --- | --- |
| `MainActivity.java` | Ordinary login, app UI, bounded browser evaluation, normalized validation, cancellation and document-picker export. No remote JavaScript bridge. |
| `assets/capture.js` | Same-origin GET identity → explicit episode access → exact reader HTML scope → ordered original image URLs → verified series metadata. |
| `Authorization.java` | Platform-independent strict boolean access flags, source URL/asset boundaries and mutation path rejection. |
| `GetTransport.java` | Native HTTPS GET, no redirects, one-second request spacing, size/time limits, cookies only on exact `tapas.io`. No browser user-agent override. |
| `PortableArchive.java` | Verified private page cache, image signature/pixel checks, original-byte ZIP entries, atomic publication and separate JSON. |
| `ArchiveSelfTest.java` | Synthetic PNG runtime checks with an injected offline page loader. No Tapas requests or session access. |

Authorization requires authenticated identity, matching episode metadata, explicit `free` or existing `unlocked`, `must_pay: false`, `scheduled: false`, `book: false`, and a present null `closing_date`. Missing/wrongly typed flags, novels, scheduled/temporary access and locked episodes are held before reader or cache retrieval. Series identity and positive episode order must match observed metadata. Neighboring episodes, thumbnails and duplicate page paths are excluded or rejected; IDs are never guessed.

The automation phase blocks non-GET browser requests and purchase/unlock/claim/payment paths. Ordinary interactive sign-in can submit the site's authentication forms. Browser comic preloads are blocked; image transfers occur separately after authorization. No purchase, WUF activation, ticket claim, DRM decryption or challenge bypass is implemented.

Limits are one episode, 500 pages, 25 MiB per image, 512 MiB of newly retrieved bytes, 30 minutes and one asset transfer at a time. Network transfers have 30-second connect/read timeouts and no automatic retry. Asset refusals—including expired signed URLs—hold the import; **Test & archive** obtains fresh metadata before resuming. Automatic single-episode URL refresh, stricter whole-job deadline enforcement and storage-pressure controls are future work. A blocked native identity GET is recorded; browser metadata still requires independently verified identity. Native image refusal stops the job.

Comic images retain their exact source bytes. Numeric names (`0001.jpg`, `0002.jpg`, …) follow manifest order; filenames sanitize path/control characters. JSON records episode/series IDs, title, order, source links without queries, access state, import time, page count, image sizes and SHA-256 values. Rich descriptions/creators/covers/publication dates remain to port. Archive and JSON files publish individually; interruption between them requires a reattempt. This is not a multi-file transactional store.

Cookies remain in the app-owned WebView store and memory during a request. Account-specific cache directories remain private. Portable exports omit cookies, account identity, signed image URLs and cache state. `files/diagnostic.json` contains fixed stages/codes, boolean identity results and counts; raw HTTP bodies, arbitrary exceptions, credential headers and WebView console messages are not logged. Production key management, full-resolution image validation and non-debug packaging remain release work.

## Tests and evidence

These checks are not part of the project gate (see the note at the top). Run physical-device acceptance checks on the selected
phone after retrieving a verified remote build.

The Java gate test needs a JDK; optional Firefox fixture tests need Playwright and its Firefox runtime. Fixture network routes are entirely intercepted, including unexpected requests. Tests prove locked/unknown/temporary/mismatched metadata stops before the reader, exact episode scope/order, original lazy image sources, GET-only sequencing, duplicate/foreign asset rejection and safe refusal reporting. They do not contact Tapas.

For runtime archive tests on the selected phone:

```sh
adb -s YOUR_SELECTED_DEVICE_SERIAL shell am force-stop org.comicarchive.prototype
adb -s YOUR_SELECTED_DEVICE_SERIAL shell am start -n org.comicarchive.prototype/.MainActivity --ez self_test true
adb -s YOUR_SELECTED_DEVICE_SERIAL exec-out run-as org.comicarchive.prototype cat files/self-test.json
```

Synthetic runtime tests check original image bytes/order/hashes/metadata, interrupted resume, corrupt-cache refetch, authorization before cache/network, broken-image rejection and cancellation before cache/network. Their temporary files are removed, and no source requests occur. Read only this app's test receipt; never pull other apps' private data.

Next experiment: ordinary Tapas sign-in → one full authorized import → document-picker exports → independent desktop ZIP/Pillow verification. If that succeeds, port one bounded resumable series batch before account enumeration and scheduling. iOS needs its own WKWebView prototype and a suitable macOS/Xcode device-testing environment.
