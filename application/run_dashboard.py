"""Start the local simulated-data dashboard: python run_dashboard.py  (then open http://127.0.0.1:8765/)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from niftypaper.dashboard.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
