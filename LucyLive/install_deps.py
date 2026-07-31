"""Offline dependency installer for LucyLive's Cinema 4D Python 3.11 runtime."""

import json
import os
import platform
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "vendor"
WHEELS_ROOT = ROOT / "wheels"
READY_MARKER = TARGET / ".lucy_live_ready"
LOCK_PATH = ROOT / ".lucy_live_install.lock"
WINDOWS_BUNDLE = "win_amd64_py311"
MACOS_ARM64_BUNDLE = "macos_arm64_py311"
MIN_MACOS_MAJOR = 14


def runtime_bundle(sys_platform=None, machine=None, version_info=None,
                   maxsize=None, macos_version=None):
    """Return the matching offline bundle and a user-facing compatibility error."""
    sys_platform = sys.platform if sys_platform is None else str(sys_platform)
    machine = (
        platform.machine() if machine is None else str(machine)
    ).strip().lower()
    version_info = sys.version_info if version_info is None else version_info
    maxsize = sys.maxsize if maxsize is None else int(maxsize)

    if tuple(version_info[:2]) != (3, 11) or maxsize <= 2 ** 32:
        return None, (
            "LucyLive requires Cinema 4D 2024-2026 with 64-bit Python 3.11."
        )

    if sys_platform == "win32":
        if machine not in ("", "amd64", "x86_64"):
            return None, "LucyLive for Windows requires a 64-bit x86 processor."
        return WINDOWS_BUNDLE, ""

    if sys_platform == "darwin":
        if machine not in ("arm64", "aarch64"):
            return None, (
                "LucyLive for Mac requires Apple Silicon in native mode. "
                "Turn off 'Open using Rosetta' for Cinema 4D."
            )
        if macos_version is None:
            macos_version = platform.mac_ver()[0]
        try:
            macos_major = int(str(macos_version).split(".", 1)[0])
        except (TypeError, ValueError):
            macos_major = None
        if macos_major is not None and macos_major < MIN_MACOS_MAJOR:
            return None, (
                "LucyLive for Apple Silicon requires macOS 14 or newer."
            )
        return MACOS_ARM64_BUNDLE, ""

    return None, "LucyLive supports 64-bit Windows and Apple Silicon macOS."


def wheel_target(member_name, target=TARGET):
    """Map a wheel member to its install path, rejecting unsafe entries."""
    pure_path = PurePosixPath(member_name)
    parts = pure_path.parts
    if (not parts or pure_path.is_absolute() or "\\" in member_name or
            any(part in ("", ".", "..") or ":" in part for part in parts)):
        raise RuntimeError("Unsafe wheel member: %s" % member_name)
    if parts[0].endswith(".data"):
        if len(parts) <= 2 or parts[1] not in ("purelib", "platlib"):
            return None
        parts = parts[2:]
    destination = target.joinpath(*parts).resolve()
    try:
        destination.relative_to(target.resolve())
    except ValueError:
        raise RuntimeError("Unsafe wheel member: %s" % member_name)
    return destination


def extract_wheel(path, target=TARGET):
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            destination = wheel_target(member.filename, target)
            if destination is None:
                continue
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, open(destination, "wb") as output:
                shutil.copyfileobj(source, output)


def acquire_install_lock(os_name=None):
    """Hold a non-blocking OS file lock until the installer exits."""
    os_name = os.name if os_name is None else str(os_name)
    try:
        handle = open(LOCK_PATH, "a+b")
    except PermissionError as exc:
        raise RuntimeError(
            "The LucyLive folder is read-only. Install it inside your "
            "Cinema 4D user preferences plugins folder."
        ) from exc
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os_name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_install_lock(handle, os_name=None):
    os_name = os.name if os_name is None else str(os_name)
    try:
        handle.seek(0)
        if os_name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def main():
    bundle, compatibility_error = runtime_bundle()
    if compatibility_error:
        print(compatibility_error)
        return 2
    wheel_directory = WHEELS_ROOT / bundle
    wheels = sorted(wheel_directory.glob("*.whl"))
    if not wheels:
        print("Bundled wheels are missing:", wheel_directory)
        return 2

    try:
        lock = acquire_install_lock()
    except RuntimeError as exc:
        print(exc)
        return 2
    if lock is None:
        print("Another LucyLive dependency installer is already running.")
        return 2

    staging = ROOT / (".vendor.installing-%d" % os.getpid())
    backup = ROOT / (".vendor.backup-%d" % os.getpid())
    print("LucyLive offline dependency installer")
    print("Runtime:", bundle)
    print("Target:", TARGET)
    try:
        for temporary in (staging, backup):
            if temporary.exists():
                shutil.rmtree(temporary)
        staging.mkdir(parents=True)
        for index, wheel in enumerate(wheels, 1):
            print("[%d/%d] %s" % (index, len(wheels), wheel.name))
            extract_wheel(wheel, staging)

        sys.path.insert(0, str(staging))
        import aiortc
        import av
        import fal_client
        import PIL
        from aiortc.codecs import h264, vpx
        av.Codec("libx264", "w")
        print("\nDependencies verified.")
        (staging / READY_MARKER.name).write_text(
            json.dumps(
                {
                    "python": "3.11",
                    "runtime": bundle,
                    "schema": 1,
                },
                sort_keys=True,
            ) + "\n",
            encoding="ascii",
        )

        if TARGET.exists():
            os.replace(str(TARGET), str(backup))
        try:
            os.replace(str(staging), str(TARGET))
        except Exception:
            if backup.exists() and not TARGET.exists():
                os.replace(str(backup), str(TARGET))
            raise
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                print("Warning: old vendor backup could not be removed:", exc)
        print("Done. Restart Cinema 4D, then open AI Render and press Start.")
        result = 0
    except Exception as exc:
        print("\nInstallation failed:", repr(exc))
        result = 1
    finally:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                pass
        release_install_lock(lock)
    if sys.stdin is not None and sys.stdin.isatty():
        input("Press Enter to close…")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
