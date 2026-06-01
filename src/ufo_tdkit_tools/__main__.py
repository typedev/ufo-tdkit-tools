# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""``python -m ufo_tdkit_tools`` entry point."""

from __future__ import annotations

import sys

from ufo_tdkit_tools.cli import main

if __name__ == "__main__":
    sys.exit(main())
