"""TASI Market Breadth Engine."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_config(path: str | Path | None = None) -> dict:
    """Load config.yaml (all thresholds live there)."""
    p = Path(path) if path else ROOT / "config" / "config.yaml"
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
