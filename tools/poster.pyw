"""Background poster launched by the Windows task `stream-clipper-poster` (see `publish install`).
Runs without a console window; output goes to work/publish/poster.log."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
log_dir = ROOT / "work" / "publish"
log_dir.mkdir(parents=True, exist_ok=True)
sys.stdout = sys.stderr = open(log_dir / "poster.log", "a", encoding="utf-8", buffering=1)

from clipper.__main__ import main  # noqa: E402

main(["publish", "daemon"])
