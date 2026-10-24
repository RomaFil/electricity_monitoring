#!/usr/bin/env python3
"""Makes the firmware signing keys. Run ONCE, on your own PC, from the firmware/ folder:

    python make_keys.py

  private.key  - stays on this PC only (git-ignored). Keep an encrypted copy (KeePass, a USB stick
                 in a drawer). Lost key = new firmware only over USB; stolen key = anyone can flash
                 the board from far away.
  public.key   - goes to git; every build puts it into the firmware (signing.py).

Needs "pip install cryptography" or openssl (Git for Windows has one).
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRIV = os.path.join(HERE, "private.key")
PUB = os.path.join(HERE, "public.key")


def main():
    if os.path.exists(PRIV):
        sys.exit("private.key already exists: not overwriting it (a new key would lock out the boards "
                 "that carry the old public.key)")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    except ImportError:
        tool = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
        if not os.path.exists(tool) and not shutil.which(tool):
            sys.exit("need either 'pip install cryptography' or openssl")
        priv = subprocess.run([tool, "genrsa", "2048"], capture_output=True, check=True).stdout
        pub = subprocess.run([tool, "rsa", "-pubout"], input=priv, capture_output=True, check=True).stdout
    with open(PRIV, "wb") as f:
        f.write(priv)
    try:
        os.chmod(PRIV, 0o600)
    except OSError:
        pass
    with open(PUB, "wb") as f:
        f.write(pub)
    print("private.key: keep it on this PC only and make an encrypted copy")
    print("public.key:  commit it (git add firmware/public.key) and rebuild the firmware")


if __name__ == "__main__":
    main()
