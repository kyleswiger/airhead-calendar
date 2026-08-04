"""Run the API locally against a seeded in-memory SQLite database.

    python dev_server.py
    cd ../frontend && VITE_API_BASE=http://localhost:8000 npm run dev

The deployed API gets its CORS headers from API Gateway, and that allowlist is
pinned to the site origin - so the production app deliberately carries no CORS
middleware. It is added here instead, for localhost only, rather than shipping a
permissive setting that could follow the app into Lambda.

Development only. Nothing imports this module.
"""

from __future__ import annotations

import argparse
from datetime import date

import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from airhead.api import deps
from airhead.api.app import app
from airhead.repo.seed import HOUSEHOLD_ID, seed_household
from airhead.repo.sqlite import SqliteEventRepo, SqliteMemberRepo, SqliteSourceRepo, connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=date.today(),
        help="Anchor date for the seeded week (YYYY-MM-DD).",
    )
    args = parser.parse_args()

    conn = connect(":memory:")
    events, members, sources = (
        SqliteEventRepo(conn),
        SqliteMemberRepo(conn),
        SqliteSourceRepo(conn),
    )
    seed_household(events, members, today=args.today)

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.dependency_overrides[deps.get_household_id] = lambda: HOUSEHOLD_ID
    app.dependency_overrides[deps.get_event_repo] = lambda: events
    app.dependency_overrides[deps.get_member_repo] = lambda: members
    app.dependency_overrides[deps.get_source_repo] = lambda: sources

    roster = ", ".join(m.member_id for m in members.list(HOUSEHOLD_ID))
    print(f"seeded {HOUSEHOLD_ID} week of {args.today} - members: {roster}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
