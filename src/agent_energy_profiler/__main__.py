"""`python -m agent_energy_profiler ...` is the same as the `aep` command."""

import sys

from .cli import main

sys.exit(main())
