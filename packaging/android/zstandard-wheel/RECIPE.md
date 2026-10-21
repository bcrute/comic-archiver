# zstandard Android wheel recipe (pinned)

The Android app keeps the repository's `zstandard>=0.25,<1` requirement. The Chaquopy
package repository tops out below that (0.23.0 for cp313), and PyPI publishes no Android
wheels, so the sdist is built for Android by Chaquopy's toolchain on the fleet host against a
16 KB-capable NDK. The version is never downgraded to fit an old wheel.

- Package: `zstandard` 0.25.0 (sdist `zstandard-0.25.0.tar.gz`)
- SHA-256: `7713e1179d162cf5c7906da876ec2ccb9c3a9dcbdffef0cc7f70c3667a205f0b`
- Source of the digest: the PyPI JSON API (`https://pypi.org/pypi/zstandard/0.25.0/json`),
  recorded 2026-10-21.
- Canonical index: PyPI only. The file is fetched on the fleet host by
  `scripts/android_feasibility.py`, verified against `sdist.sha256` before use, and handed to
  the build as `-PzstandardSdist=<absolute path>`; `android/app/build.gradle.kts` installs
  exactly that file.

Nothing under `cache/` or `dist/` is committed.

## How the cross-build happens (and how it is proven)

A verified sdist is not an Android wheel. The build is Chaquopy's own cross-compile: its
embedded pip builds the pinned sdist against the Chaquopy Android target using the host
buildPython and the NDK from the SDK — NDK presence is a required preflight item in
`scripts/android_feasibility.py`, and the sdist is passed to the app build only as the
verified absolute file (`-PzstandardSdist=...`), never as a version spec that could resolve
to an old wheel from the Chaquopy repository.

Proof is threefold, all from the remote job: `assembleDebug` succeeds with the sdist
attached; the APK is recorded together with the ABIs carrying a zstandard native library
(informational); and the in-app self-check on a job-owned emulator imports zstandard >=0.25
and passes the full hostile-frame set (checksummed round-trip, trailing-frame, truncation,
corrupted-checksum and over-window rejection) with the device ABI and page size recorded. If
Chaquopy's toolchain cannot compile the sdist, the job records `wheel_cross_build_failed`
with the complete compiler tail and stages an isolated remedy (job-private SDK with pinned
NDK and emulator packages); the >=0.25 floor is never lowered to make it pass.

An x86_64 emulator run is never arm64-v8a proof, and a 4 KB device run is never 16 KB proof;
the report records which ABI and page size actually ran, and anything unexercised stays
pending. Until that receipt exists this recipe is a pin, not proof.

## Pivot (only on a recorded failure)

If the sdist cannot be built for Android with a 16 KB-capable NDK, the options in
`docs/android-roadmap.md` Block 1 apply: a vendored newer sdist with a new pinned digest
(still >=0.25), or a reviewed archive-format pivot for the Android build. Both go through
Codex review with the failure receipt before any contract changes; the version floor is not
one of the choices.
