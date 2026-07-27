"""Offline dependency installer for AI Render on Windows / C4D Python 3.11."""

import os
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "vendor"
WHEELS = ROOT / "wheels" / "win_amd64_py311"
READY_MARKER = TARGET / ".lucy_live_ready"
LOCK_PATH = ROOT / ".lucy_live_install.lock"


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


def acquire_install_lock():
    """Hold a one-byte Windows file lock until the installer exits."""
    import msvcrt

    handle = open(LOCK_PATH, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def release_install_lock(handle):
    import msvcrt

    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def main():
    if (sys.version_info[:2] != (3, 11) or sys.platform != "win32" or
            sys.maxsize <= 2 ** 32):
        print("AI Render requires Cinema 4D 2024–2026 on 64-bit Windows (Python 3.11).")
        return 2
    wheels = sorted(WHEELS.glob("*.whl"))
    if not wheels:
        print("Bundled wheels are missing:", WHEELS)
        return 2

    lock = acquire_install_lock()
    if lock is None:
        print("Another AI Render dependency installer is already running.")
        return 2

    staging = ROOT / (".vendor.installing-%d" % os.getpid())
    backup = ROOT / (".vendor.backup-%d" % os.getpid())
    print("AI Render offline dependency installer")
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
        print("\nDependencies verified.")
        (staging / READY_MARKER.name).write_text("ok\n", encoding="ascii")

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
