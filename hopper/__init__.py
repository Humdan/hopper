"""Hopper: a durable priority job queue for AI agents."""

__version__ = "0.2.0"

from .util import HopperError  # noqa: E402
from .core import Hopper  # noqa: E402
from .client import RemoteHopper, connect  # noqa: E402

__all__ = ["Hopper", "RemoteHopper", "HopperError", "connect", "__version__"]
