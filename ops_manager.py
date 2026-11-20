"""Windowed executable entry point for the Manager and its bot worker."""

import multiprocessing
import os
import sys


def main():
    multiprocessing.freeze_support()
    # Some Windows hosts export a compiler setting called DEBUG.
    if os.environ.get("DEBUG", "").casefold() == "release":
        os.environ.pop("DEBUG")

    if sys.argv[1:] == ["--bot-worker"]:
        from desktop.worker import run_worker

        return run_worker()

    from desktop.window import run_manager

    return run_manager()


if __name__ == "__main__":
    raise SystemExit(main())
