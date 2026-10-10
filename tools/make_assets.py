"""
tools/make_assets.py
─────────────────────
Regenerates assets/adits_voice_studio.ico (EXE, shortcuts, installer) and
assets/splash.png (PyInstaller splash shown during the slow first start)
from assets/logo.png and the app's design tokens.

    py tools/make_assets.py
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.constants import COLORS            # noqa: E402
from core.version import APP_TITLE, __version__  # noqa: E402

ASSETS = os.path.join(ROOT, "assets")
LOGO_PNG = os.path.join(ASSETS, "logo.png")


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def get_logo():
    if os.path.exists(LOGO_PNG):
        return Image.open(LOGO_PNG).convert("RGBA")
    raise FileNotFoundError(f"Missing master logo at {LOGO_PNG}")


def make_ico(ico_path):
    sizes = [16, 24, 32, 48, 64, 128, 256]
    logo = get_logo()
    icon_layers = [logo.resize((s, s), Image.Resampling.LANCZOS) for s in sizes]
    icon_layers[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=icon_layers[:-1]
    )
    print(f"wrote icon {ico_path} with sizes {sizes}")


def _font(px, bold=False):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
        try:
            return ImageFont.truetype(
                os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name), px
            )
        except OSError:
            continue
    return ImageFont.load_default()


def make_splash(splash_path):
    W, H = 540, 270
    img = Image.new("RGBA", (W, H), _hex(COLORS["bg_dark"]) + (255,))
    d = ImageDraw.Draw(img)

    accent = _hex(COLORS["accent_primary"])
    d.rectangle([0, 0, W, 4], fill=accent)
    d.rectangle([0, 4, W - 1, H - 1], outline=_hex(COLORS["border"]) + (255,), width=1)

    logo = get_logo()
    mark_size = 116
    mark = logo.resize((mark_size, mark_size), Image.Resampling.LANCZOS)
    img.paste(mark, (36, 68), mark)

    d.text((176, 76), APP_TITLE, font=_font(28, bold=True), fill=_hex(COLORS["text_primary"]))
    d.text((177, 122), "Audio FX & Voice Clone", font=_font(14, bold=True), fill=accent)
    d.text((177, 150), f"Version {__version__}", font=_font(13), fill=_hex(COLORS["text_muted"]))

    final_splash = Image.new("RGB", (W, H), _hex(COLORS["bg_dark"]))
    final_splash.paste(img, (0, 0), img)
    final_splash.save(splash_path, format="PNG")
    print(f"wrote splash {splash_path}")


if __name__ == "__main__":
    os.makedirs(ASSETS, exist_ok=True)
    make_ico(os.path.join(ASSETS, "adits_voice_studio.ico"))
    make_splash(os.path.join(ASSETS, "splash.png"))
    print("wrote", os.listdir(ASSETS))
