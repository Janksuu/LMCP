"""Allow running LMCP with: python -m lmcp"""

import sys
from .daemon import run

sys.exit(run())
