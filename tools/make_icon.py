"""Compile the supplied project photo into native application icon formats.

Pillow is a developer-only dependency; the desktop client loads the generated PNG.
The original assets/labubu.jpg is preserved unchanged.
"""
from pathlib import Path


def main():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise SystemExit('Icon generation needs Pillow: python -m pip install ".[icons]"') from None

    root = Path(__file__).resolve().parents[1]
    source = root / "assets" / "labubu.jpg"
    with Image.open(source) as image:
        original = ImageOps.exif_transpose(image).convert("RGBA")
    # Preserve the whole image and its proportions; add transparent padding only
    # when needed to produce the square formats expected by native app icons.
    photo = ImageOps.contain(original, (1024, 1024), Image.Resampling.LANCZOS)
    square = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    square.paste(photo, ((1024 - photo.width) // 2, (1024 - photo.height) // 2))
    runtime = root / "src" / "autodl_gpu_watch" / "assets"
    runtime.mkdir(parents=True, exist_ok=True)
    square.resize((256, 256), Image.Resampling.LANCZOS).save(runtime / "app.png", optimize=True)
    square.save(root / "assets" / "app.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    (runtime / "app.ico").write_bytes((root / "assets" / "app.ico").read_bytes())
    square.save(root / "assets" / "app.icns")
    print("Generated Windows ICO, macOS ICNS and runtime PNG from assets/labubu.jpg")


if __name__ == "__main__":
    main()
