"""Copy a complete Windows desktop build into this repository for direct launch.

Only AutoDL Aoao.exe, _internal/ and licenses/PYTHON-LICENSE.txt are copied.
Existing destinations are never replaced, including when a client is running.
Run this preparation tool on Windows, matching the executable being installed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import tempfile
from zipfile import BadZipFile, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TARGETS = (Path("licenses/PYTHON-LICENSE.txt"), Path("_internal"), Path("AutoDL Aoao.exe"))
_PRIVATE_NAMES = {
    "profile.json", "credentials.json", "config.json", "state.json", ".env",
    "chrome-session", "devtoolsactiveport", "cookies", "login data", "local state",
}


def _regular(path: Path, *, directory: bool = False, nonempty: bool = True) -> None:
    """Reject links/junctions so the copied runtime cannot escape its source."""
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"Links and junctions are not accepted: {path.name}")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode):
        raise ValueError(f"Expected a {'directory' if directory else 'regular file'}: {path.name}")
    if nonempty and not directory and metadata.st_size == 0:
        raise ValueError(f"Empty build file: {path.name}")


def _validate_build(source: Path) -> None:
    _regular(source, directory=True)
    executable = source / "AutoDL Aoao.exe"
    _regular(executable)
    with executable.open("rb") as handle:
        header = handle.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("AutoDL Aoao.exe is not a Windows executable.")
        handle.seek(int.from_bytes(header[60:64], "little"))
        if handle.read(4) != b"PE\0\0":
            raise ValueError("AutoDL Aoao.exe has an invalid PE header.")

    runtime = source / "_internal"
    _regular(runtime, directory=True)
    # Validate every copied entry; do not copy browser data or user settings even
    # if somebody has accidentally placed them in a build's runtime directory.
    for folder, directories, files in os.walk(runtime, followlinks=False):
        for name in directories + files:
            if name.casefold() in _PRIVATE_NAMES:
                raise ValueError("The runtime contains user settings or browser data; use a clean build.")
            _regular(Path(folder) / name, directory=name in directories, nonempty=False)
    required = (
        "base_library.zip", "_tkinter.pyd", "tcl86t.dll", "tk86t.dll",
        "autodl_gpu_watch/assets/app.png", "autodl_gpu_watch/assets/app.ico",
        "_tk_data/license.terms",
    )
    for relative in required:
        _regular(runtime / relative)
    if not any(path.is_file() for path in runtime.glob("python3[0-9]*.dll")):
        raise ValueError("The Python runtime DLL is missing.")
    with ZipFile(runtime / "base_library.zip") as archive:
        if not archive.namelist() or archive.testzip() is not None:
            raise ValueError("The bundled Python standard library is incomplete.")
    with (runtime / "autodl_gpu_watch/assets/app.png").open("rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("The application PNG resource is invalid.")
    with (runtime / "autodl_gpu_watch/assets/app.ico").open("rb") as handle:
        if handle.read(4) != b"\0\0\1\0":
            raise ValueError("The application ICO resource is invalid.")
    websocket_licenses = list(runtime.glob("websocket_client-*.dist-info/licenses/LICENSE"))
    if not websocket_licenses:
        raise ValueError("The websocket-client license is missing.")
    for license_file in websocket_licenses:
        _regular(license_file)
    _regular(source / "licenses", directory=True)
    python_license = source / "licenses/PYTHON-LICENSE.txt"
    _regular(python_license)
    with python_license.open("rb") as handle:
        text = handle.read(2 * 1024 * 1024 + 1)
    if len(text) > 2 * 1024 * 1024 or b"PYTHON SOFTWARE FOUNDATION" not in text.upper():
        raise ValueError("The original Python license is missing or invalid.")


def _check_destination(root: Path) -> None:
    for relative in _TARGETS:
        if os.path.lexists(root / relative):
            raise ValueError(
                f"Destination already exists: {relative}. Nothing was replaced. "
                "Close the client and move the previous portable files aside before preparing another build."
            )
    licenses = root / "licenses"
    if os.path.lexists(licenses):
        _regular(licenses, directory=True)


def prepare(source: Path) -> Path:
    if os.name != "nt":
        raise ValueError("Prepare the Windows portable client on Windows.")
    root = PROJECT_ROOT.resolve()
    source = source.expanduser().absolute()
    _regular(source, directory=True)
    source = source.resolve()
    if source == root or source.is_relative_to(root / "_internal") or source.is_relative_to(root / "licenses"):
        raise ValueError("Choose a separate, complete desktop build folder as --from-dir.")
    _check_destination(root)
    _validate_build(source)
    with tempfile.TemporaryDirectory(prefix=".portable-stage-", dir=root, ignore_cleanup_errors=True) as temporary:
        staging = Path(temporary).resolve()
        if staging.parent != root:
            raise RuntimeError("Unexpected staging directory; no portable files were copied.")
        (staging / "licenses").mkdir()
        shutil.copy2(source / "AutoDL Aoao.exe", staging / "AutoDL Aoao.exe")
        shutil.copytree(source / "_internal", staging / "_internal")
        shutil.copy2(source / "licenses/PYTHON-LICENSE.txt", staging / "licenses/PYTHON-LICENSE.txt")
        _validate_build(staging)
        _check_destination(root)
        (root / "licenses").mkdir(exist_ok=True)
        installed = []
        try:
            # Publish the executable last, after its runtime and license exist.
            for relative in _TARGETS:
                os.rename(staging / relative, root / relative)
                installed.append(relative)
        except OSError:
            for relative in reversed(installed):
                # These paths were created by this attempt, never existing files.
                os.rename(root / relative, staging / relative)
            raise
    return root / "AutoDL Aoao.exe"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-dir", type=Path, required=True,
                        help="Complete built AutoDL Aoao folder containing the EXE, _internal and licenses.")
    arguments = parser.parse_args(argv)
    try:
        executable = prepare(arguments.from_dir)
    except (OSError, ValueError, RuntimeError, BadZipFile) as error:
        print(f"Portable preparation failed: {error}")
        return 1
    print(f"Ready: {executable}")
    print("Keep _internal and licenses beside the executable. User settings were not copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
