"""
Рисует иконку приложения и собирает .icns.

Внешних графических библиотек в проекте нет, поэтому иконка рисуется прямо
средствами Cocoa: скруглённый квадрат с градиентом и эмодзи поверх. Так
не приходится тащить ни PIL, ни готовый бинарный файл в репозиторий.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import Cocoa

SIZES = [16, 32, 64, 128, 256, 512, 1024]
GLYPH = "🚗"
TOP = (0.16, 0.42, 0.92)      # синий
BOTTOM = (0.10, 0.25, 0.62)


def draw(size: int) -> Cocoa.NSImage:
    image = Cocoa.NSImage.alloc().initWithSize_(Cocoa.NSMakeSize(size, size))
    image.lockFocus()

    inset = size * 0.06
    radius = size * 0.22
    rect = Cocoa.NSMakeRect(inset, inset, size - inset * 2, size - inset * 2)
    path = Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius
    )
    gradient = Cocoa.NSGradient.alloc().initWithStartingColor_endingColor_(
        Cocoa.NSColor.colorWithSRGBRed_green_blue_alpha_(*TOP, 1.0),
        Cocoa.NSColor.colorWithSRGBRed_green_blue_alpha_(*BOTTOM, 1.0),
    )
    gradient.drawInBezierPath_angle_(path, -90.0)

    font_size = size * 0.56
    attributes = {
        Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(font_size),
    }
    text = Cocoa.NSString.stringWithString_(GLYPH)
    measured = text.sizeWithAttributes_(attributes)
    text.drawAtPoint_withAttributes_(
        Cocoa.NSMakePoint((size - measured.width) / 2, (size - measured.height) / 2),
        attributes,
    )

    image.unlockFocus()
    return image


def png_bytes(image: Cocoa.NSImage, size: int) -> bytes:
    rep = Cocoa.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, Cocoa.NSDeviceRGBColorSpace, 0, 0
    )
    context = Cocoa.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    Cocoa.NSGraphicsContext.saveGraphicsState()
    Cocoa.NSGraphicsContext.setCurrentContext_(context)
    image.drawInRect_fromRect_operation_fraction_(
        Cocoa.NSMakeRect(0, 0, size, size),
        Cocoa.NSZeroRect, Cocoa.NSCompositingOperationSourceOver, 1.0,
    )
    Cocoa.NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(Cocoa.NSBitmapImageFileTypePNG, {})
    return bytes(data)


def main(destination: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size in SIZES:
            image = draw(size)
            (iconset / f"icon_{size}x{size}.png").write_bytes(png_bytes(image, size))
            if size <= 512:  # ретина-вариант — та же картинка в двойном размере
                double = draw(size * 2)
                (iconset / f"icon_{size}x{size}@2x.png").write_bytes(
                    png_bytes(double, size * 2)
                )
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(destination)],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
