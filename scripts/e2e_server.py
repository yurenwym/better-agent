from __future__ import annotations

import os

from seed_e2e import main as seed_main
from serve import main as serve_main

# Present only on the scripted server. Seed must not see them, or the
# candidate proposer calls the model boundary before the gateway is replaced.
_SCRIPTED_MODEL_KEYS = (
    "AGENT_MODEL_API_KEY",
    "AGENT_MODEL_BASE_URL",
    "AGENT_MODEL_ID",
    "AGENT_MODEL_PROVIDER",
    "AGENT_MODEL_CAPABILITIES",
    "AGENT_MODEL_CONTEXT_WINDOW",
    "AGENT_MODEL_MAX_OUTPUT_TOKENS",
    "AGENT_MODEL_TIMEOUT_SECONDS",
    "AGENT_MODEL_MAX_ATTEMPTS",
)


if __name__ == "__main__":
    from e2e_postgres import require_isolated_postgres

    require_isolated_postgres()
    os.environ.setdefault("BETTER_AGENT_GOAL_REVIEW_DELAY", "0")
    saved = {}
    if os.environ.get("BETTER_AGENT_E2E_SCRIPTED") == "1":
        for key in _SCRIPTED_MODEL_KEYS:
            if key in os.environ:
                saved[key] = os.environ.pop(key)
    seed_main()
    os.environ.update(saved)
    serve_main()
