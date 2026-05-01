"""Smoke test for cc-chat — DEPRECATED.

The local CCChatOrchestrator / Daytona client was removed in the
agent-sandbox-service cutover (Plan 3 B4).  End-to-end cc-chat smoke
testing now lives in the agent-sandbox-service repo.
"""

import sys

if __name__ == "__main__":
    print("skip: smoke_cc_chat.py is deprecated; see agent-sandbox-service for e2e tests")
    sys.exit(0)
