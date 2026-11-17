"""Tanios: a personal AI assistant reached over WhatsApp."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tanios")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
