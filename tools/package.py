"""Create a portable source ZIP using an explicit credential-free allowlist."""

from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from autodl_gpu_watch import __version__

    files = [root / name for name in (
        "README.md", "LICENSE", "pyproject.toml", "run.py",
        "config.example.json", ".env.example", ".gitignore", ".gitattributes", "VALIDATION.md",
        "tools/package.py", ".github/workflows/check.yml", "desktop_launcher.pyw",
        "tools/build_desktop.py", "tools/prepare_portable.py", "AutoDLGPUWatch.spec", ".github/workflows/desktop-build.yml",
        "THIRD_PARTY_NOTICES.md",
        "assets/labubu.jpg", "assets/app.ico", "assets/app.icns", "tools/make_icon.py",
        "src/autodl_gpu_watch/assets/app.png",
        "src/autodl_gpu_watch/assets/app.ico",
    ) if (root / name).is_file()]
    files.extend((root / "src" / "autodl_gpu_watch").glob("*.py"))
    output = root / "dist" / f"autodl-aoao-{__version__}.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            archive.write(path, "autodl-aoao/" + path.relative_to(root).as_posix())
    print(f"Created {output.name} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
