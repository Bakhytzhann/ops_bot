from pathlib import Path


def project_directory():
    return Path(__file__).resolve().parent.parent
