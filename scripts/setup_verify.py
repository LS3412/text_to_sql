#!/usr/bin/env python3
"""
Setup verification script to check all infrastructure components.
Run this after docker-compose up to verify everything is ready.
"""

import asyncio
import sys
from typing import Tuple

# Import configuration
sys.path.insert(0, '.')
from config.settings import get_settings
from config.database import DatabaseManager
from config.cache import RedisManager


async def check_database() -> Tuple[bool, str]:
    """Check database connectivity"""
    try:
        settings = get_settings()
        DatabaseManager.init()
        
        # Try to get a session and run a simple query
        async for session in DatabaseManager.get_session():
            result = await session.execute("SELECT 1")
            await session.close()
        
        return True, "✓ PostgreSQL connected successfully"
    except Exception as e:
        return False, f"✗ PostgreSQL error: {str(e)}"


async def check_redis() -> Tuple[bool, str]:
    """Check Redis connectivity"""
    try:
        await RedisManager.init()
        await RedisManager.set("test_key", "test_value", ttl=10)
        value = await RedisManager.get("test_key")
        await RedisManager.delete("test_key")
        
        if value == "test_value":
            return True, "✓ Redis connected successfully"
        else:
            return False, "✗ Redis test failed: incorrect value"
    except Exception as e:
        return False, f"✗ Redis error: {str(e)}"


async def check_database_schema() -> Tuple[bool, str]:
    """Check if database schema is initialized"""
    try:
        from sqlalchemy import text
        
        DatabaseManager.init()
        async for session in DatabaseManager.get_session():
            # Check if tables exist
            result = await session.execute(text("""
                SELECT COUNT(*) as table_count
                FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name IN ('chat_history', 'sql_queries', 'database_schema_context', 'text_to_sql_metrics')
            """))
            count = result.scalar()
            await session.close()
        
        if count == 4:
            return True, f"✓ Database schema initialized ({count}/4 tables found)"
        else:
            return False, f"✗ Database schema incomplete ({count}/4 tables found)"
    except Exception as e:
        return False, f"✗ Schema check error: {str(e)}"


async def main():
    """Run all verification checks"""
    print("\n" + "="*60)
    print("A2A Text-to-SQL Setup Verification")
    print("="*60 + "\n")
    
    results = []
    
    # Check settings
    settings = get_settings()
    print(f"Configuration:")
    print(f"  Environment: {settings.app.env}")
    print(f"  Database URL: {settings.database.url[:50]}...")
    print(f"  Redis URL: {settings.redis.url[:50]}...")
    print()
    
    # Run checks
    print("Running health checks...\n")
    
    print("1. Database Connectivity")
    success, message = await check_database()
    print(f"   {message}")
    results.append(success)
    
    print("\n2. Redis Connectivity")
    success, message = await check_redis()
    print(f"   {message}")
    results.append(success)
    
    print("\n3. Database Schema")
    success, message = await check_database_schema()
    print(f"   {message}")
    results.append(success)
    
    # Summary
    print("\n" + "="*60)
    if all(results):
        print("✓ All checks passed! System is ready.")
        print("="*60 + "\n")
        return 0
    else:
        print("✗ Some checks failed. Please review the errors above.")
        print("="*60 + "\n")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
