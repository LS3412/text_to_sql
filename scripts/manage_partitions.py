#!/usr/bin/env python3
"""
Pre-create tomorrow's chat_history partition (§5.1).

Run daily from cron / a scheduler so a fresh range partition always exists before
midnight (the DEFAULT partition is only a safety net — see IMPLEMENTATION_NOTES.md):

    0 23 * * *  cd /app && python scripts/manage_partitions.py

Calls the create_daily_partition() SQL function defined in database/schema.sql.
"""

import asyncio
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from config.database import DatabaseManager


async def main() -> int:
    DatabaseManager.init()
    try:
        async for session in DatabaseManager.get_session():
            await session.execute(
                text("SELECT create_daily_partition((CURRENT_DATE + INTERVAL '1 day')::date)")
            )
        print("✓ Ensured tomorrow's chat_history partition exists")
    finally:
        await DatabaseManager.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))