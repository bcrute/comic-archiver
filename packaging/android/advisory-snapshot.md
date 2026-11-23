# Android release advisory inputs

Prepare the advisory snapshot independently of the unsigned build. Use the
approved Linux OSV-Scanner executable and the dated OSV ecosystem exports.
Record each file's SHA-256 in `snapshot.json`. Approve the SHA-256 of that
manifest separately from the directory that carries it.

| File | Required manifest field |
| --- | --- |
| `osv-scanner` | `scanner_sha256` |
| `db/osv-scalibr/Maven/all.zip` | `databases.Maven` |
| `db/osv-scalibr/PyPI/all.zip` | `databases.PyPI` |

Set `schema_version` to `1` and `snapshot_date` to the capture date in
`YYYY-MM-DD` form. See the [OSV snapshot layout](https://google.github.io/osv-scanner/usage/offline-mode/).

Pass the directory to `android_feasibility.py --phase release-prepare` with
`--advisory-snapshot` and the approved manifest digest with
`--advisory-snapshot-sha256`, alongside the approved wheel and Gradle
inputs. This phase runs before the signing job. Its report identifies the
aligned APK, hashed dependency inventory, advisory snapshot, and findings.

In the `--release-inputs` directory, stage the pinned Temurin, Gradle, and
CPython bundles as `jdk.tar.gz`, `gradle.zip`, and `python.tar.gz`. Include
`sdk.tar` with the provisioned SDK's `build-tools`, `platforms`, `ndk`,
`platform-tools`, and `licenses` at its root. Verify the SDK's vendor inputs
and approve the resulting archive SHA-256 separately, then pass that digest
with `--release-sdk-sha256`. The job verifies the four archives before build
tools run and mounts the extracted toolchain read-only. The preparation
report records the verified toolchain digests.

Put the approved `verification-metadata.xml` in the same input directory.
Under `gradle-cache/modules-2/files-2.1`, provide the warmed Gradle artifact store.
Release preparation copies only files matching the approved Gradle checksums
into a read-only Maven repository inside the network-isolated build workspace.
This removes the dependency on the cache's original repository URLs.
The release build starts with an empty Gradle home and disables Gradle build
and configuration caching. Only the verified dependency artifacts are reused.
The APK is ready for signing after the offline scan reports every expected
package and finds zero advisories in that snapshot.

For `--phase release-apk`, pass the saved `android-release-prepare.json` with
`--preparation-receipt` and its independently approved SHA-256 with
`--preparation-receipt-sha256`. The signer validates the approved receipt
against the APK and current wheel lock before materializing the signing key.
The receipt also needs a successful build-toolchain verification.
Pass the pinned Temurin tarball with `--signing-jdk-archive` and the Android SDK
Build Tools ZIP with `--signing-build-tools-archive`. Approve the SHA-256 of the
vendor SDK archive independently of the file and pass it with
`--signing-build-tools-sha256`. The signing job verifies both archives before
extracting them into its private workspace. The SDK tools run from read-only
mounts, and the signing report records the verified archive digests.

Copy `phases.release_apk.provenance` from the successful signing report to
`android.provenance` in the owner-uploaded `release-evidence.json`. The
publication gate checks the signed APK digest against the staged download,
the wheel lock against the tagged source, and the presence of the preparation,
Gradle verification, inventory and dated advisory snapshot digests. Keep the
full preparation and signing reports with the private fleet evidence.

To reproduce the advisory result without rebuilding, use
`python3 scripts/android_advisories.py --help` with the saved inventory and
the approved digests from the build receipt. For the desktop portion of the
audit, run `python3 scripts/desktop_advisories.py --snapshot PATH
--snapshot-sha256 DIGEST` on the fleet with the same approved snapshot.
It checks the exact Linux, macOS and Windows release locks and reports the
hash and package versions for each manifest. Retain the report with the
fleet source receipt.

The Android report covers Python wheels
and external JVM modules observed during Gradle resolution. The separately
provisioned JDK, Android SDK, and native libraries bundled with the Python
runtime need their own inventory and advisory sources.
