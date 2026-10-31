#!/usr/bin/env python3
"""Remove the themed GRUB unlock screen and restore the original setup.

  sudo ./uninstall.py                                 asks which GRUB
  sudo ./uninstall.py --grub-location /boot/efi/EFI/cachyos-menu/grubx64.efi --yes
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer"))

import app  # noqa: E402

if __name__ == "__main__":
    app.main_uninstall()
