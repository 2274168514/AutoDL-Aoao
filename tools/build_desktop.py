"""Build a native, self-contained desktop client without bundling user data.

Run on the destination operating system with Python, Tk, and PyInstaller:
    python -m pip install PyInstaller==6.18.0 '.[desktop]'
    python tools/build_desktop.py

Windows/macOS releases are ZIP archives; Linux uses tar.gz to preserve modes.
An existing output is never overwritten; use --output-dir for another build.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "AutoDL Aoao"


def _version() -> str:
    source = PROJECT_ROOT / "src" / "autodl_gpu_watch" / "__init__.py"
    for statement in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", value):
                return value
    raise RuntimeError("Cannot read a valid package version from __init__.py.")


def _platform_label() -> tuple[str, str]:
    systems = {"win32": "windows", "darwin": "macos", "linux": "linux"}
    if sys.platform not in systems:
        raise RuntimeError("Only native Windows, macOS, and Linux builds are supported.")
    machine = platform.machine().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    if not re.fullmatch(r"[a-z0-9_-]+", architecture):
        raise RuntimeError("Cannot determine the build architecture.")
    return systems[sys.platform], architecture


def _python_license_path(override: Path | None = None) -> Path:
    """Find the original license beside the selected Python runtime only."""
    if override is not None:
        candidates = [override.expanduser().resolve()]
    else:
        prefixes = (Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).resolve().parent)
        standard_library = Path(sysconfig.get_path("stdlib"))
        candidates = [folder / name for folder in (*prefixes, standard_library) for name in ("LICENSE.txt", "LICENSE")]
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        candidates += [prefix / "share" / "doc" / version / "copyright" for prefix in prefixes]
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        with path.open("rb") as source:
            data = source.read(2 * 1024 * 1024 + 1)
        if len(data) <= 2 * 1024 * 1024 and b"PYTHON SOFTWARE FOUNDATION" in data.upper() and b"LICENSE" in data.upper():
            return path
    raise RuntimeError(
        "Cannot find this Python runtime's original license. "
        "Provide its LICENSE.txt using --python-license PATH."
    )


def _copy_release_documents(package: Path, python_license: Path) -> None:
    # Explicit allowlist: never copy configs, browser sessions or credentials.
    for name in ("README.md", "VALIDATION.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        source = PROJECT_ROOT / name
        if source.is_file():
            shutil.copy2(source, package / name)
    license_directory = package / "licenses"
    license_directory.mkdir(exist_ok=True)
    shutil.copy2(python_license, license_directory / "PYTHON-LICENSE.txt")


def _archive(package: Path, destination: Path) -> None:
    if sys.platform == "darwin":
        # Native ditto preserves executable permissions and framework symlinks.
        subprocess.run(
            ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(package), str(destination)],
            check=True,
        )
    elif sys.platform == "linux":
        with tarfile.open(destination, "w:gz", dereference=False) as archive:
            archive.add(package, arcname=APP_NAME)
    else:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    archive.write(path, arcname=path.relative_to(package.parent).as_posix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a native AutoDL Aoao desktop bundle.")
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "dist" / "desktop",
        help="Destination for the application folder and archive (existing outputs are not overwritten).",
    )
    parser.add_argument(
        "--python-license", type=Path,
        help="Original license for this Python runtime, if it cannot be found in its installation.",
    )
    args = parser.parse_args(argv)
    try:
        pyinstaller_version = importlib.metadata.version("pyinstaller")
        importlib.metadata.version("websocket-client")
        import tkinter

        python_license = _python_license_path(args.python_license)
        system, architecture = _platform_label()
        version = _version()
        extension = "tar.gz" if sys.platform == "linux" else "zip"
        filename = f"{APP_NAME.replace(' ', '-')}-{version}-{system}-{architecture}.{extension}"
        output = args.output_dir.expanduser().resolve()
        final_package = output / APP_NAME
        final_archive = output / filename
        if final_package.exists() or final_archive.exists():
            raise RuntimeError("Build output already exists. Select a new --output-dir; existing files were left intact.")
        entrypoint = PROJECT_ROOT / "src" / "autodl_gpu_watch" / "gui.py"
        if not entrypoint.is_file():
            raise RuntimeError("The desktop GUI source is missing; finish the source tree before building.")
        print(
            f"Building {APP_NAME} {version} for {system}/{architecture}; "
            f"Python {platform.python_version()}, Tk {tkinter.TkVersion}, PyInstaller {pyinstaller_version}.",
            flush=True,
        )
        with tempfile.TemporaryDirectory(prefix="autodl-desktop-build-", ignore_cleanup_errors=True) as folder:
            staging = Path(folder)
            built = staging / "dist"
            subprocess.run(
                [
                    sys.executable, "-m", "PyInstaller", "--noconfirm",
                    "--distpath", str(built), "--workpath", str(staging / "work"),
                    str(PROJECT_ROOT / "AutoDLGPUWatch.spec"),
                ],
                cwd=PROJECT_ROOT,
                check=True,
            )
            package = staging / "package" / APP_NAME
            if sys.platform == "darwin":
                package.mkdir(parents=True)
                shutil.copytree(built / f"{APP_NAME}.app", package / f"{APP_NAME}.app", symlinks=True)
            else:
                shutil.copytree(built / APP_NAME, package, symlinks=True)
            _copy_release_documents(package, python_license)
            archive_path = staging / filename
            _archive(package, archive_path)
            output.mkdir(parents=True, exist_ok=True)
            shutil.copytree(package, final_package, symlinks=True)
            with archive_path.open("rb") as source, final_archive.open("xb") as target:
                shutil.copyfileobj(source, target)
        executable = final_package / (
            f"{APP_NAME}.exe" if sys.platform == "win32"
            else f"{APP_NAME}.app" if sys.platform == "darwin"
            else APP_NAME
        )
        print(f"Application: {executable}")
        print(f"Archive: {final_archive}")
        if sys.platform == "darwin":
            print("Share the complete archive. The .app bundle contains its Python runtime.")
        else:
            print("Share the complete archive. The executable requires the accompanying _internal folder.")
        return 0
    except importlib.metadata.PackageNotFoundError:
        print('Build dependency missing. Run: python -m pip install PyInstaller==6.18.0 ".[desktop]"', file=sys.stderr)
    except ImportError:
        print("Tk is unavailable in this Python installation. Install a Python build with Tk support.", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
