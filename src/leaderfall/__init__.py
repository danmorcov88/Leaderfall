"""Leaderfall: break a PostgreSQL HA cluster on purpose, and prove with numbers that it survives."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("leaderfall")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
