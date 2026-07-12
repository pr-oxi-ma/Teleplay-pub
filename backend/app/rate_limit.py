"""Shared rate limiter configuration.

If REDIS_URL is set, limits are shared across processes/instances.
If REDIS_URL is empty, SlowAPI falls back to the normal in-memory limiter.
"""
from slowapi import Limiter
from .config import get_settings
from .auth import request_ip

settings = get_settings()

limiter_kwargs = {"key_func": lambda request: request_ip(request) or "unknown"}
if settings.rate_limit_storage_uri:
    limiter_kwargs["storage_uri"] = settings.rate_limit_storage_uri

limiter = Limiter(**limiter_kwargs)
