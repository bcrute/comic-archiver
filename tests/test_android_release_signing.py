"""The owner's release key in scripts/android_feasibility.py --phase release-apk.

Every update must carry the one release identity, so the job never generates a key,
refuses one whose certificate differs, never puts the password in an argument list or
receipt, and deletes its copy of the key however the build ends. keytool, Gradle and
the SDK are all faked; the "keystores" here are arbitrary bytes.
"""

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch

from scripts import android_feasibility as feasibility
from test_android_debug_signing import fake_jdk

FINGERPRINT = "9a" * 32
PASSWORD = "correct horse battery staple"
IDENTITY = {"package_id": "io.github.bcrute.comicarchiver", "version_name": "0.3.0-beta1",
            "version_code": 1, "min_sdk": 29, "target_sdk": 36, "abis": ["arm64-v8a", "x86_64"]}


def keytool_fake(fail_list=False, fingerprint=FINGERPRINT):
    calls = []

    def run(argv, timeout=None, **kwargs):
        calls.append(list(argv))
        if "-list" not in argv or fail_list:
            return {"returncode": 1, "tail": ""}
        if argv[argv.index("-alias") + 1] != "comic-archiver":
            raise AssertionError("the release certificate must be read from the release alias")
        if Path(argv[argv.index("-storepass:file") + 1]).read_text() != PASSWORD:
            raise AssertionError("keytool must read the injected password from its file")
        colonated = ":".join(fingerprint[i:i + 2] for i in range(0, 64, 2)).upper()
        return {"returncode": 0, "tail": f"Alias name: comic-archiver\n    SHA256: {colonated}\n"}

    run.calls = calls
    return run


class ReleaseSigningTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.java = fake_jdk(self.root / "jdk/bin")
        self.folder = self.root / "job/release-signing"
        self.store = os.urandom(64)
        self.env = {feasibility.RELEASE_KEYSTORE_ENV: base64.b64encode(self.store).decode(),
                    feasibility.RELEASE_PASSWORD_ENV: PASSWORD,
                    feasibility.RELEASE_CERT_SHA256_ENV: ":".join(FINGERPRINT[i:i + 2] for i in range(0, 64, 2))}
        phases = dict(feasibility.report["phases"])
        self.addCleanup(lambda: (feasibility.report["phases"].clear(), feasibility.report["phases"].update(phases)))

    def phase(self, env=None, runner=None):
        return feasibility.phase_release_signing({"java": self.java}, env=self.env if env is None else env,
                                                 runner=runner or keytool_fake(), directory=self.folder)

    def recorded(self):
        return feasibility.report["phases"]["release_signing"]

    @staticmethod
    def quiet_gradle():
        """No pip probe, proxy flags or mirror init script: only the argv matters here."""
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(feasibility, "run_tool", return_value={"returncode": 1, "tail": ""}))
        stack.enter_context(patch.object(feasibility, "_jvm_net_options", return_value=[]))
        stack.enter_context(patch.object(feasibility, "_ci_download_mirror", return_value=None))
        return stack


class InjectionTests(ReleaseSigningTestCase):
    def test_each_missing_part_fails_without_generating_or_writing_anything(self):
        for name in (feasibility.RELEASE_KEYSTORE_ENV, feasibility.RELEASE_PASSWORD_ENV,
                     feasibility.RELEASE_CERT_SHA256_ENV):
            with self.subTest(missing=name):
                runner = keytool_fake()
                self.assertIsNone(self.phase({**self.env, name: "  "}, runner))
                self.assertEqual(self.recorded()["status"], "failed")
                self.assertEqual(runner.calls, [])
                self.assertFalse(self.folder.exists())

    def test_a_damaged_store_fails(self):
        self.assertIsNone(self.phase({**self.env, feasibility.RELEASE_KEYSTORE_ENV: "### not base64 ###"}))
        self.assertEqual(self.recorded()["reason"], "not strict base64")
        self.assertFalse(self.folder.exists())

    def test_the_injected_key_is_materialized_privately_and_read_with_its_password_by_file(self):
        runner = keytool_fake()
        store, password = self.phase(runner=runner)
        self.assertEqual(self.recorded(), {"status": "passed", "cert_sha256": FINGERPRINT})
        self.assertEqual(Path(store).read_bytes(), self.store)
        self.assertEqual(Path(password).read_text(), PASSWORD)
        for path in (store, password):
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertFalse(any(PASSWORD in argument for call in runner.calls for argument in call))

    def test_another_certificate_fails_and_the_copy_is_deleted(self):
        self.assertIsNone(self.phase(runner=keytool_fake(fingerprint="cd" * 32)))
        self.assertEqual(self.recorded()["reason"], "certificate fingerprint differs from the expected value")
        self.assertFalse(self.folder.exists())

    def test_a_store_keytool_cannot_open_fails_and_the_copy_is_deleted(self):
        self.assertIsNone(self.phase(runner=keytool_fake(fail_list=True)))
        self.assertEqual(self.recorded()["reason"], "keytool could not read the release key back")
        self.assertFalse(self.folder.exists())

    def test_no_secret_reaches_the_receipt(self):
        self.phase()
        receipt = json.dumps(feasibility.report["phases"]["release_signing"])
        for secret in (PASSWORD, self.env[feasibility.RELEASE_KEYSTORE_ENV], self.store.hex()):
            self.assertNotIn(secret, receipt)

    def test_gradle_never_receives_release_signing_paths(self):
        paths = {"sdk": self.root / "sdk", "gradle": "gradle", "java": self.java, "buildpython": "python3",
                 "release_keystore": "/job/release.p12", "release_password_file": "/job/release.pass"}
        with self.quiet_gradle():
            _env, argv, _flags, _url = feasibility._gradle_invocation(paths, "assembleRelease", "wheels")
        self.assertFalse(any(argument.startswith("-PreleaseKeystore") for argument in argv))


class SdistSourceTests(unittest.TestCase):
    """A disposable job reaches PyPI only through the job's package index."""

    def listing(self, body):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = body.encode()
        return response

    def test_the_pinned_sdist_is_found_on_the_jobs_package_index(self):
        body = ('<a href="../../+f/1a/zstandard-0.24.0.tar.gz#sha256=00">old</a>\n'
                f'<a href="../../+f/77/{feasibility.PINNED_SDIST}#sha256=7713">pinned</a>\n'
                '<a href="https://elsewhere/zstandard-0.25.0-cp313-cp313-manylinux.whl">wheel</a>')
        with patch.dict(os.environ, {"PIP_INDEX_URL": "https://index.example/root/pypi/+simple/"}), \
                patch.object(feasibility.urllib.request, "urlopen", return_value=self.listing(body)) as opened:
            url = feasibility._sdist_url()
        self.assertEqual(opened.call_args.args[0], "https://index.example/root/pypi/+simple/zstandard/")
        self.assertEqual(url, f"https://index.example/root/pypi/+f/77/{feasibility.PINNED_SDIST}#sha256=7713")

    def test_an_index_without_the_pinned_sdist_fails(self):
        with patch.dict(os.environ, {"PIP_INDEX_URL": "https://index.example/simple"}), \
                patch.object(feasibility.urllib.request, "urlopen",
                             return_value=self.listing('<a href="zstandard-0.24.0.tar.gz">old</a>')):
            with self.assertRaises(ValueError):
                feasibility._sdist_url()


class CibuildwheelCacheTests(unittest.TestCase):
    """cibuildwheel's own downloads go straight out, so the job puts them in its cache."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.files = {"https://maven.example/python-3.13.15-aarch64-linux-android.tar.gz": b"arm",
                      "https://maven.example/python-3.13.15-x86_64-linux-android.tar.gz": b"x86",
                      "https://github.example/virtualenv.pyz": b"venv",
                      "https://github.example/cpython-3.13.16%2B20261020-x86_64-unknown-linux-gnu-install_only.tar.gz": b"pbs"}
        digest = {url: hashlib.sha256(body).hexdigest() for url, body in self.files.items()}
        resources = self.root / "tools/cibw-venv/lib/python3.13/site-packages/cibuildwheel/resources"
        resources.mkdir(parents=True)
        rows = "\n".join(f'  {{ identifier = "cp313-android_{arch}", url = "{url}", sha256 = "{digest[url]}" }},'
                         for arch, url in zip(("arm64_v8a", "x86_64"), list(self.files)[:2], strict=True))
        (resources / "build-platforms.toml").write_text(f"[android]\npython_configurations = [\n{rows}\n]\n")
        url = "https://github.example/virtualenv.pyz"
        (resources / "virtualenv.toml").write_text(
            f'default = {{ version = "21.14.12", url = "{url}", sha256 = "{digest[url]}" }}\n')
        standalone = "https://github.example/cpython-3.13.16%2B20261020-x86_64-unknown-linux-gnu-install_only.tar.gz"
        (resources / "python-build-standalone-releases.json").write_text(json.dumps({"releases": [
            {"tag": "20261020", "assets": [
                {"name": "cpython-3.12.13+20261020-x86_64-unknown-linux-gnu-install_only.tar.gz",
                 "url": "https://github.example/other.tar.gz", "sha256": "00"},
                {"name": "cpython-3.13.16+20261020-x86_64-unknown-linux-gnu-install_only.tar.gz",
                 "url": standalone, "sha256": digest[standalone]}]}]}))
        self.cache = self.root / "tools/cibw-cache"
        for name, value in (("TOOLS_DIR", self.root / "tools"), ("CIBW_CACHE", self.cache)):
            patcher = patch.object(feasibility, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, tampered=None):
        def download(url, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"tampered" if url == tampered else self.files[url])
            return True
        with patch.object(feasibility, "_download", side_effect=download) as fetched:
            return feasibility._seed_cibw_cache(), [call.args[0] for call in fetched.call_args_list]

    def test_both_abis_and_virtualenv_land_under_the_names_cibuildwheel_looks_for(self):
        reason, fetched = self.fetch()
        self.assertIsNone(reason)
        self.assertEqual(fetched, list(self.files))
        self.assertEqual(sorted(path.name for path in self.cache.iterdir()),
                         ["cpython-3.13.16+20261020-x86_64-unknown-linux-gnu-install_only.tar.gz",
                          "python-3.13.15-aarch64-linux-android.tar.gz",
                          "python-3.13.15-x86_64-linux-android.tar.gz", "virtualenv-21.14.12.pyz"])

    def test_a_file_that_differs_from_the_builders_pin_is_refused_and_removed(self):
        reason, _ = self.fetch(tampered="https://github.example/virtualenv.pyz")
        self.assertIn("virtualenv-21.14.12.pyz sha256 mismatch", reason)
        self.assertFalse((self.cache / "virtualenv-21.14.12.pyz").exists())


class PinnedGradleTests(unittest.TestCase):
    """The app's Android Gradle Plugin supports a bounded Gradle range, so a host's own
    Gradle is never used, whatever its version."""

    def probe(self, versions):
        def run(argv, **kwargs):
            version = versions.get(argv[0])
            return {"returncode": 0 if version else 127,
                    "tail": f"\n------------------------------------------------------------\nGradle {version}\n"
                            if version else "not found"}
        return run

    def test_only_the_provisioned_gradle_at_its_pinned_version_is_accepted(self):
        pinned = feasibility.PINS["gradle"]["version"]
        provisioned = str(feasibility.TOOLS_DIR / "gradle/bin/gradle")
        with patch.object(feasibility.shutil, "which", return_value="/usr/local/bin/gradle"), \
                patch.object(feasibility, "run_tool",
                             self.probe({"/usr/local/bin/gradle": "9.6", provisioned: pinned})):
            gradle, version, _ = feasibility._pinned_gradle({"gradle": provisioned}, None)
        self.assertEqual((gradle, version), (provisioned, pinned))

    def test_another_version_is_refused_even_when_it_is_newer(self):
        provisioned = str(feasibility.TOOLS_DIR / "gradle/bin/gradle")
        with patch.object(feasibility, "run_tool", self.probe({provisioned: "9.6"})):
            gradle, version, probes = feasibility._pinned_gradle({"gradle": provisioned}, None)
        self.assertEqual((gradle, version), (None, None))
        self.assertEqual([probe["candidate"] for probe in probes], [provisioned])


class ReleaseApkTests(ReleaseSigningTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.apk = self.repo / "android/app/build/outputs/apk/release/app-release-unsigned.apk"
        sdk = self.root / "sdk"
        self.build_tools = sdk / "build-tools/36.0.0"
        self.build_tools.mkdir(parents=True)
        for tool in ("apksigner", "aapt2", "zipalign"):
            (self.build_tools / tool).touch()
        self.jdk_archive = self.root / "jdk.tar.gz"
        with tarfile.open(self.jdk_archive, "w:gz") as bundle:
            bundle.add(Path(self.java).parent.parent, arcname="jdk-fixture")
        self.jdk_digest = hashlib.sha256(self.jdk_archive.read_bytes()).hexdigest()
        self.sdk_archive = self.root / "sdk.zip"
        with zipfile.ZipFile(self.sdk_archive, "w") as bundle:
            for tool in self.build_tools.iterdir():
                bundle.write(tool, f"build-tools-fixture/{tool.name}")
        self.sdk_digest = hashlib.sha256(self.sdk_archive.read_bytes()).hexdigest()
        self.bwrap = self.root / "bwrap"
        self.bwrap.touch()
        for name, value in (("REPO", self.repo), ("SIGNING_SANDBOX", self.bwrap)):
            patcher = patch.object(feasibility, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo.mkdir()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in feasibility.RELEASE_SIGNING_ENV:
            os.environ.pop(name, None)
        self.apk.parent.mkdir(parents=True)
        for name in ("android/gradle", "comic_archive", "packaging/licenses"):
            (self.repo / name).mkdir(parents=True)
        for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
            (self.repo / name).touch()
        (self.repo / "android/gradle/release-inventory.init.gradle").touch()
        (self.repo / "android/gradle/release-repository.init.gradle").touch()
        self.build_sdk_archive = self.root / "sdk.tar"
        with tarfile.open(self.build_sdk_archive, "w") as bundle:
            bundle.add(self.build_tools.parent, arcname="build-tools")
        self.gradle_archive = self.root / "gradle.zip"
        with zipfile.ZipFile(self.gradle_archive, "w") as bundle:
            bundle.writestr("gradle-fixture/bin/gradle", b"")
        python = self.root / "python/bin/python3.13"
        python.parent.mkdir(parents=True)
        python.touch()
        self.python_archive = self.root / "python.tar.gz"
        with tarfile.open(self.python_archive, "w:gz") as bundle:
            bundle.add(python.parent.parent, arcname="python-fixture")
        self.toolchain_digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in (
            ("jdk", self.jdk_archive), ("gradle", self.gradle_archive), ("python", self.python_archive),
            ("sdk", self.build_sdk_archive),
        )}
        artifacts = self.root / "gradle-cache/modules-2/files-2.1/fixture/fixture/1/cache-entry"
        artifacts.mkdir(parents=True)
        self.cached_jar = artifacts / "fixture-1.jar"
        self.cached_jar.write_bytes(b"approved fixture jar")
        (artifacts / "unused.jar").write_bytes(b"not approved")
        artifact_digest = hashlib.sha256(self.cached_jar.read_bytes()).hexdigest()
        self.metadata = (self.root / "verification-metadata.xml")
        self.metadata.write_text(
            '<verification-metadata><configuration><verify-metadata>true</verify-metadata></configuration>'
            '<components><component group="fixture" name="fixture" version="1">'
            f'<artifact name="fixture-1.jar"><sha256 value="{artifact_digest}"/></artifact></component></components>'
            '</verification-metadata>')
        self.gradle_digest = hashlib.sha256(self.metadata.read_bytes()).hexdigest()
        self.unsigned = b"unsigned apk"
        self.digest = hashlib.sha256(self.unsigned).hexdigest()
        self.artifact = self.root / "artifacts/ComicArchiver-0.3.0-beta1-android-universal.apk"

    @staticmethod
    def inventory_fixture():
        return [
            {"configuration": name, "modules": [["fixture", "fixture", "1"]], "unresolved": 0}
            for name in (":classpath", ":app:releaseCompileClasspath", ":app:releaseRuntimeClasspath")
        ]

    def prepare(self, gradle_ok=True, license_problems=(), digest=None, inventory=None, findings=()):
        def gradle(argv, **kwargs):
            self.assertEqual(argv[0], self.bwrap)
            self.assertIn("--unshare-all", argv)
            self.assertIn("--clearenv", argv)
            self.assertEqual(argv[argv.index("SOURCE_DATE_EPOCH") + 1], "1767225600")
            self.assertIn("--offline", argv)
            self.assertIn("--no-build-cache", argv)
            self.assertIn("--no-configuration-cache", argv)
            self.assertEqual(argv[argv.index("--dependency-verification") + 1], "strict")
            self.assertIn("assembleRelease", argv)
            self.assertTrue(any(str(arg).startswith("-PlockedPythonWheels=") for arg in argv))
            self.assertFalse(any(str(arg).startswith("-PpipIndexUrl=") for arg in argv))
            project = argv[argv.index("-p") + 1]
            self.assertNotEqual(project, self.repo / "android")
            tools = project.parent.parent / "tools"
            self.assertIn(tools, argv)
            work_bind = argv.index("--bind")
            self.assertEqual(argv[work_bind + 3:work_bind + 6], ["--ro-bind", tools, tools])
            self.assertEqual((project / "gradle/verification-metadata.xml").read_bytes(), self.metadata.read_bytes())
            self.assertEqual(argv[argv.index("--init-script") + 1],
                             project / "gradle/release-inventory.init.gradle")
            repository = Path(argv[argv.index("RELEASE_MAVEN_REPOSITORY") + 1])
            self.assertIn(project / "gradle/release-repository.init.gradle", argv)
            self.assertNotIn("GRADLE_RO_DEP_CACHE", argv)
            self.assertEqual([p.name for p in repository.rglob("*.jar")], ["fixture-1.jar"])
            self.assertEqual((repository / "fixture/fixture/1/fixture-1.jar").read_bytes(),
                             self.cached_jar.read_bytes())
            self.assertTrue(any(tuple(argv[i:i + 3]) == ("--ro-bind", repository, repository)
                                for i in range(len(argv))))
            if gradle_ok:
                output = project / "app/build/outputs/apk/release/app-release-unsigned.apk"
                output.parent.mkdir(parents=True)
                output.write_bytes(self.unsigned)
                inventory_path = Path(argv[argv.index("RELEASE_GRADLE_INVENTORY") + 1])
                rows = self.inventory_fixture() if inventory is None else inventory
                inventory_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            return {"returncode": 0 if gradle_ok else 1, "tail": "" if gradle_ok else "> Task :app:lintVitalRelease FAILED"}

        def zipalign(argv):
            argv = argv[argv.index("--") + 1:]
            staged_sdk = argv[-1].parent / "tools/sdk/build-tools" / self.build_tools.name
            self.assertEqual(argv[:4], [staged_sdk / "zipalign", "-P", "16", "4"])
            shutil.copyfile(argv[-2], argv[-1])

        def scan(path, inventory_digest, apk_digest, snapshot, snapshot_digest):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), inventory_digest)
            self.assertEqual(apk_digest, self.digest)
            self.assertEqual(snapshot, self.root / "advisories")
            self.assertEqual(snapshot_digest, "a" * 64)
            return {"apk_sha256": apk_digest, "inventory_sha256": inventory_digest,
                    "snapshot_sha256": snapshot_digest, "snapshot_date": "2026-11-01",
                    "package_count": 2, "findings": list(findings)}

        pins = {name: {"sha256": self.toolchain_digests[key]}
                for name, key in (("jdk", "jdk"), ("gradle", "gradle"), ("host_python", "python"))}
        with patch.dict(feasibility.PINS, pins), \
                patch.object(feasibility, "phase_provision", side_effect=AssertionError("provision reached release")), \
                patch.object(feasibility, "phase_release_signing", side_effect=AssertionError("signing reached build")), \
                patch.object(feasibility, "phase_wheels", side_effect=AssertionError("wheel build reached release")), \
                patch.object(feasibility.android_wheelhouse, "load_lock",
                             return_value=(self.digest, {}, {"python_fixture": "1.0"})), \
                patch.object(feasibility.android_wheelhouse, "stage_wheels", side_effect=lambda src, dst, *lock: dst.mkdir()), \
                patch.object(feasibility.android_wheelhouse, "verify_apk_packages"), \
                patch.object(feasibility, "run_tool", gradle), \
                patch.object(feasibility, "_jvm_net_options", return_value=[]), \
                patch.object(feasibility, "_ci_download_mirror", return_value=None), \
                patch.object(feasibility.android_release, "tool_output", side_effect=zipalign), \
                patch.object(feasibility.android_release, "identity_from_source", return_value=dict(IDENTITY)), \
                patch.object(feasibility.android_release, "inspect_apk_payload", return_value=dict(IDENTITY)), \
                patch.object(feasibility.android_release, "license_problems", return_value=list(license_problems)), \
                patch.object(feasibility.android_advisories, "scan", side_effect=scan), \
                patch("builtins.print"):
            return feasibility.prepare_release_apk(self.root / "release-wheels", self.root,
                                                   self.gradle_digest if digest is None else digest,
                                                   self.toolchain_digests["sdk"],
                                                   self.root / "advisories", "a" * 64)

    def sign(self, *, digest=None, observed=None, license_problems=(), fail=False, receipt_change=None, prepare=True):
        if prepare:
            self.assertEqual(self.prepare(), 0)
        self.assertEqual(feasibility.report["phases"]["build_toolchain"], {
            "status": "passed", **{f"{name}_sha256": digest for name, digest in self.toolchain_digests.items()},
        })
        preparation = self.root / "artifacts/android-release-prepare.json"
        if receipt_change is not None:
            content = json.loads(preparation.read_text())
            receipt_change(content)
            preparation.write_text(json.dumps(content), encoding="utf-8")
        prep_digest = hashlib.sha256(preparation.read_bytes()).hexdigest()
        self.apk.write_bytes(self.unsigned)
        self.signing_paths = []
        keytool = keytool_fake()

        def sdk(argv, env):
            self.assertEqual(set(env), {"JAVA_HOME", "PATH", "HOME", "TMPDIR", "LC_ALL"})
            tools = Path(env["HOME"]) / "tools"
            self.assertEqual(env["PATH"], f"{tools}/jdk/jdk-fixture/bin:/usr/bin:/bin")
            chdir = argv.index("--chdir")
            self.assertEqual(argv[chdir - 3:chdir], ["--ro-bind", str(tools), str(tools)])
            self.assertFalse(any(name in os.environ for name in feasibility.RELEASE_SIGNING_ENV))
            self.assertEqual(argv[0], self.bwrap)
            self.assertIn("--unshare-all", argv)
            argv = argv[argv.index("--") + 1:]
            self.assertFalse(any(PASSWORD in str(arg) for arg in argv))
            if Path(argv[0]).name == "keytool":
                return keytool(argv)["tail"]
            self.assertEqual(argv[1], "sign")
            store = Path(argv[argv.index("--ks") + 1])
            password = Path(argv[argv.index("--ks-pass") + 1].removeprefix("file:"))
            key_password = Path(argv[argv.index("--key-pass") + 1].removeprefix("file:"))
            self.signing_paths.extend((store, password, key_password))
            self.assertEqual(store.read_bytes(), self.store)
            self.assertEqual(password.read_text(), PASSWORD)
            self.assertEqual(key_password.read_text(), PASSWORD)
            self.assertNotEqual(password, key_password)
            self.assertNotEqual(argv[-1], self.apk)
            self.assertEqual(argv[-1].read_bytes(), self.unsigned)
            if fail:
                raise ValueError("Android SDK rejected a staged APK.")
            argv[argv.index("--out") + 1].write_bytes(b"signed release apk")

        with patch.dict(os.environ, {**self.env, "GITHUB_TOKEN": "fixture", "JAVA_TOOL_OPTIONS": "fixture"}), \
                patch.dict(feasibility.PINS, {"jdk": {"sha256": self.jdk_digest}}), \
                patch.object(feasibility, "phase_provision", side_effect=AssertionError("provision reached signer")), \
                patch.object(feasibility, "run_tool", side_effect=AssertionError("build reached signer")), \
                patch.object(feasibility.android_wheelhouse, "load_lock", return_value=(self.digest, {}, {})), \
                patch.object(feasibility.android_wheelhouse, "verify_apk_packages"), \
                patch.object(feasibility.android_release, "tool_output", side_effect=sdk), \
                patch.object(feasibility.android_release, "identity_from_source", return_value=dict(IDENTITY)), \
                patch.object(feasibility.android_release, "inspect_apk_payload", return_value=dict(IDENTITY)), \
                patch.object(feasibility.android_release, "inspect_apk",
                             return_value=observed or dict(IDENTITY, certificate_sha256=FINGERPRINT)), \
                patch.object(feasibility.android_release, "license_problems", return_value=list(license_problems)), \
                patch("builtins.print"):
            return feasibility.release_apk(self.apk, self.digest if digest is None else digest,
                                           self.jdk_archive, self.sdk_archive, self.sdk_digest,
                                           preparation, prep_digest)

    def test_unsigned_apk_bears_an_inspected_digest_and_prepare_refuses_signing_environments(self):
        with patch.dict(os.environ, self.env), patch("builtins.print"):
            self.assertEqual(self.prepare(), 1)
        self.assertFalse(self.apk.exists())
        self.assertEqual(self.prepare(), 0)
        self.assertEqual(feasibility.report["phases"]["gradle_verification"]["sha256"], self.gradle_digest)
        self.assertEqual(feasibility.report["phases"]["release_prepare"]["apk_sha256"], self.digest)
        self.assertEqual((self.root / "artifacts/android-release-unsigned.apk").read_bytes(), self.unsigned)
        receipt = feasibility.report["phases"]["dependency_inventory"]
        content = (self.root / "artifacts" / receipt["artifact"]).read_bytes()
        self.assertEqual(receipt["sha256"], hashlib.sha256(content).hexdigest())
        graph = json.loads(content)
        self.assertEqual(graph["apk_sha256"], self.digest)
        self.assertEqual(graph["wheel_lock_sha256"], self.digest)
        self.assertEqual(graph["gradle_verification_sha256"], self.gradle_digest)
        self.assertEqual(graph["python_packages"], {"python_fixture": "1.0"})
        self.assertEqual(graph["gradle_configurations"][":app:releaseRuntimeClasspath"], [["fixture", "fixture", "1"]])

    def test_unsigned_apk_requires_a_complete_verified_dependency_inventory(self):
        graph = self.inventory_fixture()
        for rows in ([], graph[:-1], [*graph, dict(graph[0], unresolved=1)],
                     [*graph, dict(graph[0], modules=[["fixture", "fixture", "2"]])]):
            with self.subTest(rows=rows):
                self.assertEqual(self.prepare(inventory=rows), 1)
                self.assertFalse((self.root / "artifacts/android-release-unsigned.apk").exists())
                self.assertIn("inventory", feasibility.report["phases"]["release_prepare"]["reason"])

    def test_unsigned_apk_is_held_when_the_offline_scan_reports_a_finding(self):
        self.assertEqual(self.prepare(findings=({"advisory": "fixture"},)), 1)
        report = feasibility.report["phases"]["dependency_advisories"]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["findings"], [{"advisory": "fixture"}])
        self.assertFalse((self.root / "artifacts/android-release-unsigned.apk").exists())

    def test_a_failed_unsigned_build_preserves_the_failure_classification(self):
        self.assertEqual(self.prepare(gradle_ok=False), 1)
        self.assertEqual(feasibility.report["phases"]["release_prepare"]["classification"],
                         "task_failed:app:lintVitalRelease")

    def test_unsigned_build_refuses_a_changed_toolchain_before_invoking_gradle(self):
        for name, archive in (("jdk", self.jdk_archive), ("gradle", self.gradle_archive),
                              ("python", self.python_archive), ("sdk", self.build_sdk_archive)):
            with self.subTest(toolchain=name):
                original = archive.read_bytes()
                try:
                    archive.write_bytes(original + b"changed")
                    self.assertEqual(self.prepare(gradle_ok=False), 1)
                    self.assertIn(f"build {name} archive differs",
                                  feasibility.report["phases"]["release_prepare"]["reason"])
                finally:
                    archive.write_bytes(original)
        self.assertFalse((self.root / "artifacts/android-release-unsigned.apk").exists())

    def test_the_local_repository_uses_verified_published_artifact_names(self):
        module = self.cached_jar.with_name("fixture-1.module")
        published = self.cached_jar.with_name("fixture-1.aar")
        self.cached_jar.rename(published)
        artifact_digest = hashlib.sha256(published.read_bytes()).hexdigest()
        for url in (published.name, "../outside.aar"):
            with self.subTest(url=url):
                module.write_text(json.dumps({"variants": [{"files": [
                    {"name": "fixture-release.aar", "url": url}
                ]}]}))
                module_digest = hashlib.sha256(module.read_bytes()).hexdigest()
                metadata = (
                    '<verification-metadata><components><component group="fixture" name="fixture" version="1">'
                    f'<artifact name="{module.name}"><sha256 value="{module_digest}"/></artifact>'
                    f'<artifact name="fixture-release.aar"><sha256 value="{artifact_digest}"/></artifact>'
                    '</component></components></verification-metadata>').encode()
                target = self.root / ("repo" if url == published.name else "unsafe-repo")
                root = feasibility.android_offline._parse_verification(metadata)
                cache = self.root / "gradle-cache/modules-2/files-2.1"
                if url == published.name:
                    feasibility.android_offline._stage_repository(root, cache, target)
                    self.assertEqual((target / "fixture/fixture/1" / url).read_bytes(), published.read_bytes())
                    self.assertEqual((target / "fixture/fixture/1/fixture-release.aar").read_bytes(),
                                     published.read_bytes())
                else:
                    with self.assertRaisesRegex(ValueError, "Unsafe or conflicting"):
                        feasibility.android_offline._stage_repository(root, cache, target)

    def test_the_local_repository_refuses_cache_artifacts_with_wrong_bytes(self):
        self.cached_jar.write_bytes(b"changed cache artifact")
        self.assertEqual(self.prepare(), 1)
        self.assertIn("No SHA-256-verified Gradle artifacts", feasibility.report["phases"]["release_prepare"]["reason"])
        self.assertFalse((self.root / "artifacts/android-release-unsigned.apk").exists())

    def test_a_verified_module_can_reference_the_same_filename_in_a_sibling_version(self):
        module = self.cached_jar.with_suffix(".module")
        filename = self.cached_jar.name
        module.write_text(json.dumps({"variants": [{"files": [
            {"name": filename, "url": f"../2/{filename}"},
        ]}]}))
        module_digest = hashlib.sha256(module.read_bytes()).hexdigest()
        jar_digest = hashlib.sha256(self.cached_jar.read_bytes()).hexdigest()
        for sibling_digest in (jar_digest, "0" * 64):
            with self.subTest(sibling_digest=sibling_digest):
                metadata = (
                    '<verification-metadata><components><component group="fixture" name="fixture" version="2">'
                    f'<artifact name="{filename}"><sha256 value="{sibling_digest}"/></artifact></component>'
                    '<component group="fixture" name="fixture" version="1">'
                    f'<artifact name="{module.name}"><sha256 value="{module_digest}"/></artifact>'
                    f'<artifact name="{filename}"><sha256 value="{jar_digest}"/></artifact>'
                    '</component></components></verification-metadata>').encode()
                root = feasibility.android_offline._parse_verification(metadata)
                cache = self.root / "gradle-cache/modules-2/files-2.1"
                target = self.root / f"staged-{sibling_digest[:8]}"
                if sibling_digest == jar_digest:
                    feasibility.android_offline._stage_repository(root, cache, target)
                    self.assertEqual((target / "fixture/fixture/2" / filename).read_bytes(),
                                     self.cached_jar.read_bytes())
                else:
                    with self.assertRaisesRegex(ValueError, "Conflicting verification digests"):
                        feasibility.android_offline._stage_repository(root, cache, target)

    def test_unapproved_or_incomplete_gradle_verification_refuses_to_build(self):
        self.assertEqual(self.prepare(digest="0" * 64), 1)
        self.assertIn("differs from its approved SHA-256", feasibility.report["phases"]["release_prepare"]["reason"])
        self.metadata.write_text(self.metadata.read_text().replace("<verify-metadata>true", "<verify-metadata>false"))
        self.assertEqual(self.prepare(digest=hashlib.sha256(self.metadata.read_bytes()).hexdigest()), 1)
        self.assertIn("must cover metadata and artifacts", feasibility.report["phases"]["release_prepare"]["reason"])
        self.assertFalse((self.root / "artifacts/android-release-unsigned.apk").exists())

    def test_verification_metadata_does_not_expand_entities_even_with_the_expected_digest(self):
        original = self.metadata.read_text()
        self.metadata.write_text('<!DOCTYPE verification-metadata [<!ENTITY check "true">]>'
                                 + original.replace('>true<', '>&check;<'))
        digest = hashlib.sha256(self.metadata.read_bytes()).hexdigest()
        self.assertEqual(self.prepare(digest=digest), 1)
        self.assertIn("must not contain DTDs or entities", feasibility.report["phases"]["release_prepare"]["reason"])
        self.assertFalse((self.root / "artifacts/android-release-unsigned.apk").exists())

    def test_a_matching_signed_apk_is_kept_under_its_release_name_and_the_key_is_deleted(self):
        self.assertEqual(self.sign(), 0)
        self.assertTrue(self.signing_paths)
        self.assertFalse(any(path.exists() for path in self.signing_paths))
        self.assertEqual(self.artifact.read_bytes(), b"signed release apk")
        self.assertEqual(feasibility.report["verdict"], "release_apk_ready")
        receipt = feasibility.report["phases"]["release_apk"]
        self.assertEqual(receipt["unsigned_apk_sha256"], self.digest)
        self.assertEqual(feasibility.report["phases"]["signing_toolchain"], {
            "status": "passed", "jdk_sha256": self.jdk_digest, "sdk_build_tools_sha256": self.sdk_digest,
        })
        self.assertEqual(receipt["provenance"], {
            "signed_apk_sha256": receipt["apk_sha256"], "unsigned_apk_sha256": self.digest,
            "preparation_receipt_sha256": feasibility.report["phases"]["preparation_approval"]["sha256"],
            "python_wheel_lock_sha256": self.digest, "gradle_verification_sha256": self.gradle_digest,
            "inventory_sha256": feasibility.report["phases"]["preparation_approval"]["inventory_sha256"],
            "snapshot_sha256": "a" * 64, "snapshot_date": "2026-11-01",
        })

    def test_a_failed_signing_command_still_deletes_the_key(self):
        self.assertEqual(self.sign(fail=True), 1)
        self.assertTrue(self.signing_paths)
        self.assertFalse(any(path.exists() for path in self.signing_paths))
        self.assertFalse(self.artifact.exists())

    def test_a_changed_artifact_is_refused_before_key_materialization(self):
        with patch.object(feasibility, "phase_release_signing", side_effect=AssertionError("unexpected signing")):
            self.assertEqual(self.sign(digest="0" * 64), 1)
        self.assertFalse(self.artifact.exists())
        self.assertIn("digest differs", feasibility.report["phases"]["release_apk"]["reason"])

    def test_tampered_tool_archives_are_refused_before_extraction_or_key_materialization(self):
        self.assertEqual(self.prepare(), 0)
        for name, archive, extractor in (("jdk", self.jdk_archive, "_extract_tar_data"),
                                         ("sdk", self.sdk_archive, "_extract_zip_data")):
            with self.subTest(toolchain=name):
                original = archive.read_bytes()
                try:
                    archive.write_bytes(original + b"changed")
                    with patch.object(feasibility, extractor, side_effect=AssertionError("unexpected extraction")), \
                            patch.object(feasibility, "phase_release_signing",
                                         side_effect=AssertionError("unexpected signing")):
                        self.assertEqual(self.sign(prepare=False), 1)
                    self.assertIn(f"{name} archive differs", feasibility.report["phases"]["release_apk"]["reason"])
                finally:
                    archive.write_bytes(original)
        self.assertFalse(self.artifact.exists())

    def test_signing_refuses_a_preparation_receipt_without_matching_advisory_evidence(self):
        def wrong_apk(receipt):
            receipt["phases"]["dependency_advisories"]["apk_sha256"] = "0" * 64

        with patch.object(feasibility, "phase_release_signing", side_effect=AssertionError("unexpected signing")):
            self.assertEqual(self.sign(receipt_change=wrong_apk), 1)
        self.assertFalse(self.artifact.exists())
        self.assertIn("does not verify this APK", feasibility.report["phases"]["release_apk"]["reason"])

    def test_signing_refuses_a_preparation_receipt_without_verified_build_tools(self):
        def remove_toolchain(receipt):
            receipt["phases"].pop("build_toolchain")

        with patch.object(feasibility, "phase_release_signing", side_effect=AssertionError("unexpected signing")):
            self.assertEqual(self.sign(receipt_change=remove_toolchain), 1)
        self.assertFalse(self.artifact.exists())
        self.assertIn("incomplete checks", feasibility.report["phases"]["release_apk"]["reason"])

    def test_an_apk_with_another_signer_or_identity_is_refused(self):
        for observed in (dict(IDENTITY, certificate_sha256="cd" * 32),
                         dict(IDENTITY, certificate_sha256=FINGERPRINT, version_code=2)):
            with self.subTest(observed=observed):
                self.assertEqual(self.sign(observed=observed), 1)
                self.assertFalse(self.artifact.exists())
                self.assertFalse(any(path.exists() for path in self.signing_paths))

    def test_an_apk_without_its_license_texts_is_refused(self):
        self.assertEqual(self.sign(license_problems=["The APK lacks the license text other/Apache-2.0.txt."]), 1)
        self.assertFalse(self.artifact.exists())
        self.assertFalse(self.signing_paths)
        self.assertEqual(feasibility.report["phases"]["release_apk"]["license_problems"],
                         ["The APK lacks the license text other/Apache-2.0.txt."])


if __name__ == "__main__":
    unittest.main()
