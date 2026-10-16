"""Compatibility entry point for PyCharm's default run configuration."""

import asyncio

from bot import main


if __name__ == "__main__":
    asyncio.run(main())
