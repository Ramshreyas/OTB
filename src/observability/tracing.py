"""Langfuse tracing client — singleton, configured from environment."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_langfuse_client: Optional["Langfuse"] = None


def get_langfuse_client() -> Optional["Langfuse"]:
    """Get or create the Langfuse client singleton.

    Returns None if Langfuse is not configured (missing env vars).
    """
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    host = os.getenv("LANGFUSE_HOST", "")

    if not public_key or not secret_key or not host:
        logger.info("Langfuse not configured — tracing disabled.")
        return None

    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        logger.info("Langfuse client initialized (host=%s)", host)
        return _langfuse_client
    except ImportError:
        logger.warning("langfuse package not installed — tracing disabled.")
        return None
    except Exception as e:
        logger.warning("Failed to initialize Langfuse: %s", e)
        return None


def flush() -> None:
    """Flush any pending Langfuse events. Call before process exit."""
    client = get_langfuse_client()
    if client:
        try:
            client.flush()
        except Exception as e:
            logger.warning("Langfuse flush failed: %s", e)
