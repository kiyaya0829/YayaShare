"""Deterministically package the user-supplied artwork; no generated artwork.

Run once with Pillow installed when replacing assets/source.jpg. Preserve the
whole image and its aspect ratio; add white padding for square OS icon formats.
"""
from pathlib import Path
from PIL import Image, ImageOps

assets = Path(__file__).resolve().parents[1] / "src/yayashare/assets"
with Image.open(assets / "source.jpg") as source:
    icon = ImageOps.pad(source.convert("RGB"), (1024, 1024), color="white")
    icon.save(assets / "icon.png")
    icon.save(assets / "icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    icon.save(assets / "icon.icns")
