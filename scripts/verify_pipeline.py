#!/usr/bin/env python3
"""Step 7 verification: fetch -> match -> tailor -> prospect -> compose -> queue -> dry-run send.

    python scripts/verify_pipeline.py          # offline, bundled mock API responses
    python scripts/verify_pipeline.py --live   # try the real Greenhouse/Lever APIs first
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from referralpilot.verify import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
