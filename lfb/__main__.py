"""Allow running as ``python3 -m lfb``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
