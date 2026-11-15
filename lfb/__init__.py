"""linux-file-bridge: bridge project files between Android and Linux environments.

The package intentionally has no third-party dependencies so it can run inside
a bare Ubuntu proot/chroot on an Android phone.
"""

from .version import __version__, PROTOCOL_VERSION

__all__ = ["__version__", "PROTOCOL_VERSION"]
