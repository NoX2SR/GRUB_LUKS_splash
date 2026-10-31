#!/usr/bin/env python3
"""Install the themed GRUB unlock screen, or change its theme.

  sudo ./install.py                                   asks for everything
  sudo ./install.py --theme cyberpunk                 asks only for the GRUB
  sudo ./install.py --grub-location /boot/efi/EFI/cachyos-menu/grubx64.efi \
                    --theme cyberpunk --yes           no questions (scripts)
  ./install.py --list                                 show the GRUB images found
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer"))

import app  # noqa: E402

if __name__ == "__main__":
    app.main_install()
