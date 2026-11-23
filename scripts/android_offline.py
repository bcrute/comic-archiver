"""Prepare a disposable, offline Gradle release workspace."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

GRADLE_INVENTORY = "gradle-resolutions.jsonl"
# Match the desktop release archive epoch. CPython then emits hash-based bytecode.
RELEASE_BUILD_EPOCH = "1767225600"


def _parse_verification(content):
    try:
        text = content.decode("utf-8")
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            raise ValueError("Gradle verification metadata must not contain DTDs or entities.")
        return ET.fromstring(text)  # noqa: S314 - UTF-8 only, with DTD and entity declarations rejected
    except (ET.ParseError, UnicodeDecodeError):
        raise ValueError("Gradle verification metadata must be valid UTF-8 XML.") from None


def _verification(path, expected_sha256):
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        raise ValueError("Gradle verification metadata differs from its approved SHA-256.")
    root = _parse_verification(content)
    config = root.find("{*}configuration")
    artifacts = root.findall("{*}components/{*}component/{*}artifact")
    if (config is None or config.findtext("{*}verify-metadata") != "true"
            or config.findall("{*}trusted-artifacts/{*}trust") or not artifacts
            or any(not any(re.fullmatch(r"[a-fA-F0-9]{64}", checksum.get("value", ""))
                           for checksum in artifact.findall("{*}sha256")) for artifact in artifacts)):
        raise ValueError("Gradle verification must cover metadata and artifacts by SHA-256.")
    modules = {tuple(component.get(key) for key in ("group", "name", "version"))
               for component in root.findall("{*}components/{*}component")
               if component.findall("{*}artifact")}
    return content, digest, modules, root


FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy_artifact(cache, source, destination, filename, allowed, aliases=()):
    target = destination / filename
    matches = {}
    for name in {filename, *(alias.name for alias in aliases)}:
        for candidate in source.glob(f"*/{name}"):
            if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(cache):
                continue
            observed = _sha256(candidate)
            if observed in allowed:
                matches[observed] = candidate
    if target.exists():
        observed = _sha256(target)
        if observed not in allowed:
            raise ValueError(f"Conflicting staged Maven artifact for {filename}.")
        matches[observed] = target
    if len(matches) > 1:
        raise ValueError(f"Multiple distinct approved bytes exist for {filename} in the staged cache.")
    if not matches:
        return None  # The generated metadata may include unused test artifacts.
    expected, candidate = matches.popitem()
    if candidate != target:
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, target)
        if _sha256(target) != expected:
            raise ValueError(f"Gradle cache bytes changed while staging {filename}.")
    for alias in aliases:
        if alias.exists():
            if _sha256(alias) != expected:
                raise ValueError(f"Conflicting staged Maven alias for {filename}.")
        else:
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.hardlink_to(target)
    return target


def _published_names(module, artifacts, approved_paths):
    """Read only verified Gradle module metadata to recover repository filenames."""
    aliases = {}
    try:
        for variant in json.loads(module.read_bytes())["variants"]:
            for entry in variant.get("files", ()):
                name, url = entry["name"], entry["url"]
                if name in artifacts and name != url:
                    parts = url.split("/")
                    if len(parts) == 3 and parts[0] == ".." and FILE_NAME.fullmatch(parts[1]) and parts[2] == name:
                        # A Gradle variant can publish the same file under a sibling version.
                        alias = module.parent.parent / parts[1] / name
                    elif FILE_NAME.fullmatch(url) and url not in artifacts:
                        alias = module.with_name(url)
                    else:
                        raise ValueError(f"Unsafe or conflicting published Maven filename for {name}.")
                    if alias in approved_paths and approved_paths[alias] != artifacts[name]:
                        raise ValueError(f"Conflicting verification digests for Maven alias {name}.")
                    aliases.setdefault(name, set()).add(alias)
    except (AttributeError, KeyError, TypeError):
        raise ValueError(f"Invalid Gradle module metadata in {module.name}.") from None
    return aliases


def _stage_repository(root, cache, repository):
    """Copy only hash-verified Gradle cache artifacts into a Maven repository."""
    cache = cache.resolve(strict=True)
    components = root.findall("{*}components/{*}component")
    entries = []
    approved_paths = {}
    for component in components:
        group, name, version = (component.get(key, "") for key in ("group", "name", "version"))
        if (not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", group)
                or not all(FILE_NAME.fullmatch(part) for part in (name, version))):
            raise ValueError("Gradle verification metadata contains an unsafe module coordinate.")
        source = cache / group / name / version
        destination = repository.joinpath(*group.split("."), name, version)
        artifacts = {}
        for artifact in component.findall("{*}artifact"):
            filename = artifact.get("name", "")
            path = destination / filename
            if not FILE_NAME.fullmatch(filename) or path in approved_paths:
                raise ValueError("Gradle verification metadata contains an unsafe or duplicate artifact name.")
            artifacts[filename] = {entry.get("value", "").lower() for entry in artifact.findall("{*}sha256")}
            approved_paths[path] = artifacts[filename]
        entries.append((source, destination, f"{name}-{version}.module", artifacts))

    staged = 0
    for source, destination, module_name, artifacts in entries:
        aliases = {}
        if module_name in artifacts:
            module = _copy_artifact(cache, source, destination, module_name, artifacts.pop(module_name))
            if module is not None:
                aliases = _published_names(module, artifacts, approved_paths)
                staged += 1
        for filename, allowed in artifacts.items():
            if _copy_artifact(cache, source, destination, filename, allowed, aliases.get(filename, ())):
                staged += 1
    if not staged:
        raise ValueError("No SHA-256-verified Gradle artifacts were available to stage.")


def release_workspace(project, work, inputs, expected_sha256, wheels, sandbox, *, jdk, gradle, python, sdk):
    if not sandbox.is_file():
        raise ValueError("Release preparation requires a networkless build sandbox.")
    metadata, digest, modules, metadata_root = _verification(inputs / "verification-metadata.xml", expected_sha256)
    gradle = gradle / "bin/gradle"
    python = python / "bin/python3.13"
    cache = inputs / "gradle-cache/modules-2/files-2.1"
    installed = sorted(path for path in (sdk / "build-tools").glob("*")
                       if (path / "aapt2").is_file() and (path / "zipalign").is_file())
    if (not installed or not cache.is_dir()
            or not all(tool.is_file() for tool in (jdk / "bin/java", gradle, python))):
        raise ValueError("Supply pre-staged Android SDK, JDK, Gradle, Python and Gradle dependency cache.")

    source = work / "source"
    source.mkdir()
    for name in ("android", "comic_archive", "packaging/licenses"):
        shutil.copytree(project / name, source / name, symlinks=True,
                        ignore=shutil.ignore_patterns(".fleet-*", ".gradle", "build", "__pycache__", "local.properties"))
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        shutil.copyfile(project / name, source / name)
    android = source / "android"
    manifest = android / "gradle/verification-metadata.xml"
    if manifest.parent.is_symlink():
        raise ValueError("The Gradle verification directory must belong to the staged source.")
    manifest.unlink(missing_ok=True)
    manifest.write_bytes(metadata)
    inventory_script = android / "gradle/release-inventory.init.gradle"
    repository_script = android / "gradle/release-repository.init.gradle"
    if not repository_script.is_file():
        raise ValueError("The source is missing its offline Gradle repository script.")
    repository = work / "maven-repo"
    _stage_repository(metadata_root, cache, repository)

    env = {"JAVA_HOME": str(jdk), "ANDROID_HOME": str(sdk), "ANDROID_SDK_ROOT": str(sdk),
           "ANDROID_USER_HOME": str(work / "android-user"), "GRADLE_USER_HOME": str(work / "gradle-home"),
           "RELEASE_MAVEN_REPOSITORY": str(repository), "HOME": str(work), "TMPDIR": str(work),
           "RELEASE_GRADLE_INVENTORY": str(work / GRADLE_INVENTORY), "SOURCE_DATE_EPOCH": RELEASE_BUILD_EPOCH,
           "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
    prefix = [sandbox, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv"]
    for path in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
        if path.is_dir():
            prefix.extend(("--ro-bind", path, path))
    tools = work / "tools"
    prefix.extend(("--proc", "/proc", "--dev", "/dev", "--bind", work, work,
                   "--ro-bind", tools, tools,
                   "--ro-bind", wheels, wheels, "--ro-bind", manifest, manifest,
                   "--ro-bind", inventory_script, inventory_script,
                   "--ro-bind", repository_script, repository_script,
                   "--ro-bind", repository, repository, "--chdir", android))
    for key, value in env.items():
        prefix.extend(("--setenv", key, value))
    prefix.append("--")
    assemble = [gradle, "--offline", "--dependency-verification", "strict", "--no-daemon",
                "--no-build-cache", "--no-configuration-cache", "--init-script", inventory_script,
                "--init-script", repository_script, "-p", android,
                "assembleRelease", f"-PbuildPython={python}", f"-PlockedPythonWheels={wheels}"]
    unsigned = android / "app/build/outputs/apk/release/app-release-unsigned.apk"
    return prefix, assemble, unsigned, installed[-1], digest, modules
