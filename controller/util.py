"""Small file helpers shared by main.py and the profile runner."""
import json
import os


def write_json_atomic(path: str, obj) -> None:
    """Write JSON so readers never see a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic on the same filesystem


def read_json(path: str, default=None):
    """Read JSON; return `default` if the file is missing or unreadable."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default
