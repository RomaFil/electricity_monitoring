"""Make server/app and power-guard importable from tests."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for sub in ("server", "power-guard"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
