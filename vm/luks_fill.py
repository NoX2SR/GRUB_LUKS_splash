#!/usr/bin/env python3
"""Write a plaintext filesystem image into the data segment of a LUKS2 container.

Works without root: encrypts with the volume key directly (aes-xts-plain64,
512-byte sectors) instead of going through dm-crypt.

usage: luks_fill.py <luks-file> <volume-key-file> <payload-offset-bytes> <plain-image>
"""
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SECTOR = 512


def main():
    luks_path, key_path, offset, plain_path = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    with open(key_path, "rb") as f:
        key = f.read()
    with open(plain_path, "rb") as src, open(luks_path, "r+b") as dst:
        dst.seek(offset)
        sector = 0
        while True:
            chunk = src.read(SECTOR * 2048)
            if not chunk:
                break
            out = bytearray()
            for i in range(0, len(chunk), SECTOR):
                # plain64 IV: little-endian sector number, relative to the data segment
                tweak = (sector).to_bytes(16, "little")
                enc = Cipher(algorithms.AES(key), modes.XTS(tweak)).encryptor()
                out += enc.update(chunk[i:i + SECTOR]) + enc.finalize()
                sector += 1
            dst.write(out)


if __name__ == "__main__":
    main()
