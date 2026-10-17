"""`python -m agent_energy_profiler ...` is the same as the `agent-energy-profiler` command."""

import sys

from .cli import main

sys.exit(main())
