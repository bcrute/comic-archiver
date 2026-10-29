# zstandard Android wheel recipe (pinned, cibuildwheel)

The Android app keeps the repository's `zstandard>=0.25,<1` requirement. The Chaquopy
package repository tops out below that (0.23.0 for cp313) and PyPI publishes no Android
wheels, so cp313 Android wheels are built **on the fleet** from the pinned sdist with
[cibuildwheel](https://cibuildwheel.pypa.io/en/stable/platforms/#android) — the builder
Chaquopy's own packaging documentation names for Python 3.13+
(chaquo/chaquopy `server/pypi/README.md`). The version is never downgraded to fit an old
wheel.

**What does not happen:** Chaquopy does not cross-build native sdsts. Chaquopy 17's pip
block accepts local *source* installs only when they are pure Python, so the sdist can
never be handed to Gradle directly. Feeding a wheel directory through `--find-links` plus
an exact requirement is the supported path.

- Package: `zstandard` 0.25.0 (sdist `zstandard-0.25.0.tar.gz`)
- SHA-256: `7713e1179d162cf5c7906da876ec2ccb9c3a9dcbdffef0cc7f70c3667a205f0b`
- Source of the digest: the PyPI JSON API (`https://pypi.org/pypi/zstandard/0.25.0/json`),
  recorded 2026-10-21.
- Builder: cibuildwheel 4.3.3 (wheel digest in `../toolchain-pins.json`), run from a
  task-private CPython 3.13 (`python-build-standalone`, pinned in the same file) that also
  serves as Chaquopy's `buildPython` — its major.minor must match the app's embedded 3.13;
  the executor host's default 3.14 is never used.
- Canonical index: PyPI only. Every input (sdist, cibuildwheel wheel, host Python, JDK,
  Gradle, Android command-line tools) is fetched by `scripts/android_feasibility.py` from
  the URLs pinned in `../toolchain-pins.json`, verified against the committed sha256
  **before extraction**, and kept job-private under `android/.fleet-sdk`.

Nothing under `cache/`, `dist/` or a wheel output directory is committed.

## How the build happens (and how it is proven)

`scripts/android_feasibility.py`'s `wheels` phase unpacks the verified sdist with the
tar data filter and runs, against the job-private SDK:

```
cibuildwheel --platform android --arch arm64-v8a,x86_64 --output-dir <wheels> <sdist-dir>
```

with `CIBW_BUILD=cp313-*` and `CIBW_BUILD_FRONTEND=build` (Android does not support the
pip frontend). The phase passes only when the output is exactly two
`zstandard-0.25.0-cp313-cp313-android_<api>_<arch>.whl` files covering `arm64_v8a` and
`x86_64`. Wheel unit tests are not run in this phase: emulator qualification is the
in-app phase's own evidence, and a build is not a test pass.

The Gradle build then receives `-PzstandardWheels=<wheel dir>` and
`-PbuildPython=<task-private python3.13>`; `android/app/build.gradle.kts` passes the
directory to Chaquopy's pip as `options("--find-links", ...)` with the exact requirement
`install("zstandard==0.25.0")`, so pip resolves to those wheels only and never consults
the Chaquopy repository. A build without the wheel directory fails configuration rather
than silently shipping an APK without zstandard.

Proof is threefold, all from the remote job: the `wheels` phase's own output listing;
`assembleDebug` succeeding with the wheels attached; and the in-app self-check on a
job-owned emulator importing zstandard >=0.25 and passing the full hostile-frame set
(checksummed round-trip, trailing-frame, truncation, corrupted-checksum and over-window
rejection) with the device ABI and page size recorded. If the wheel build fails, the job
records the phase `failed` with the complete builder tail and the pinned inputs unchanged;
the >=0.25 floor is never lowered to make it pass.

An x86_64 emulator run is never arm64-v8a proof, and a 4 KB device run is never 16 KB proof;
the report records which ABI and page size actually ran, and anything unexercised stays
pending. Until that receipt exists this recipe is a pin, not proof.

## Pivot (only on a recorded failure)

If the sdist cannot be built for Android with the pinned cibuildwheel toolchain, the
options are a vendored newer sdist with a new pinned digest (still >=0.25), or a
reviewed archive-format pivot for the Android build. Either is reviewed with the failure
receipt before any contract changes; the version floor is not one of the choices.
