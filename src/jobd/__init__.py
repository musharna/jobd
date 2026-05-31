"""jobd — self-hostable GPU-aware job broker with native MCP integration."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jobd")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
