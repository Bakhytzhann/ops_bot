import sys
from pathlib import Path


def project_directory():
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent

    executable_dir = Path(sys.executable).resolve().parent
    locator = executable_dir / "ops-bot-home.txt"
    if locator.is_file():
        location = locator.read_text(encoding="utf-8-sig").strip()
        if not location:
            raise ValueError("ops-bot-home.txt must contain the OPS Bot data directory")
        return (executable_dir / location).resolve()
    return executable_dir
