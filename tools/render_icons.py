#!/usr/bin/env python3
"""Render the SVG logo into the hicolor PNG sizes using gdk-pixbuf."""
from __future__ import annotations

import os
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
NAME = "com.local.UbuntuAppControl"


def render(svg_path: str, hicolor_dir: str) -> list[str]:
    os.makedirs(hicolor_dir, exist_ok=True)
    written = []
    for size in SIZES:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(svg_path, size, size)
        directory = os.path.join(hicolor_dir, f"{size}x{size}", "apps")
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, f"{NAME}.png")
        pixbuf.savev(target, "png", [], [])
        written.append(target)

    scalable = os.path.join(hicolor_dir, "scalable", "apps")
    os.makedirs(scalable, exist_ok=True)
    link = os.path.join(scalable, f"{NAME}.svg")
    with open(svg_path, encoding="utf-8") as source, open(link, "w", encoding="utf-8") as out:
        out.write(source.read())
    written.append(link)
    return written


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    svg = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "data", f"{NAME}.svg")
    destination = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.path.join(root, "build", "icons", "hicolor")
    )
    if not os.path.exists(svg):
        print(f"missing logo source: {svg}", file=sys.stderr)
        return 1
    for path in render(svg, destination):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
