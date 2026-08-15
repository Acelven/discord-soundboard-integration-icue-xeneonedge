#!/usr/bin/env python3
"""Generates icon.ico (used for both the tray icon and the built .exe's file
icon) from the same drawing hotkey_client.py uses at runtime. Run once before
building, or whenever you want to regenerate it.
"""
from hotkey_client import make_icon_image

if __name__ == "__main__":
    img = make_icon_image()
    img.save("icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Wrote icon.ico")
