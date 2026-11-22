import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gobgp"))
sys.path.insert(0, str(ROOT / "web"))
os.environ.setdefault("BGP_DASHBOARD_NO_APP", "1")

MONGO_URI = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
SAMPLE = ROOT / "log" / "sample_data.log"
