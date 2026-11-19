"""Manual read-only FYERS smoke test: python run_fyers_smoke.py [--option-expiry 2026-11-03 --option-strike 24000 --option-type CE]."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from niftypaper.providers.fyers_smoke import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
