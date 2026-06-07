"""
Redis cache manager for the A2A application.
Handles caching of schema information, query results, and other data.
"""

import json
from typing import Any, Optional
# Import the officially supported async module from redis package
import redis.asyncio as aioredis
from config.settings import get_settings


class RedisManager:
    """Manages Redis connections and caching"""
    
    _redis_client: Optional[aioredis.Redis] = None
    
    @classmethod
    async def init(cls) -> None:
        """Initialize Redis connection using credentials from settings"""
        settings = get_settings()
        if cls._redis_client is None:
            try:
                cls._redis_client = await aioredis.from_url(
                    settings.redis.url,
                    encoding="utf8",
                    decode_responses=True,
                )
                await cls._redis_client.ping()
                print("✓ Redis connected successfully")
            except Exception as e:
                print(f"✗ Redis connection failed: {e}")
                raise
    
    @classmethod
    async def close(cls) -> None:
        """Close Redis connection gracefully"""
        if cls._redis_client:
            await cls._redis_client.close()
            cls._redis_client = None
    
    @classmethod
    async def get(cls, key: str) -> Optional[Any]:
        """Get value from cache and deserialize from JSON"""
        if cls._redis_client is None:
            await cls.init()
        
        value = await cls._redis_client.get(key)
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return None
    
    @classmethod
    async def set(cls, key: str, value: Any, ttl: int = 3600) -> None:
        """Set value in cache serialized to JSON with a custom TTL"""
        if cls._redis_client is None:
            await cls.init()
        
        if isinstance(value, (dict, list)):
            serialized_value = json.dumps(value)
        else:
            serialized_value = str(value)
        
        await cls._redis_client.setex(key, ttl, serialized_value)
    
    @classmethod
    async def delete(cls, key: str) -> None:
        """Delete value from cache"""
        if cls._redis_client is None:
            await cls.init()
        await cls._redis_client.delete(key)
