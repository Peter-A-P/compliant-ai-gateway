"""The OpenAI-compatible proxy (0.13, PLAN.md B2.1). Needs the `server` extra.

Not imported by `boundary` itself, so a library caller never pays for FastAPI.
"""

from boundary.server.app import create_app
from boundary.server.teams import TeamsConfig, hash_key, load_teams, new_key

__all__ = ["TeamsConfig", "create_app", "hash_key", "load_teams", "new_key"]
