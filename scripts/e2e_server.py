from __future__ import annotations

import os

from seed_e2e import main as seed_main
from serve import main as serve_main


if __name__ == "__main__":
    os.environ.setdefault("BETTER_AGENT_GOAL_REVIEW_DELAY", "0")
    seed_main()
    serve_main()
