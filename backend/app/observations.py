"""Public observations prepared from the source downloads."""

import json

from .config import get_settings


def load_observations(filename: str = "observations.json") -> dict:
    path = get_settings().data_dir / "processed" / filename
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
