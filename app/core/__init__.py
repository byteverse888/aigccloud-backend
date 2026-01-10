"""
Core modules
"""
from app.core.config import settings, get_settings
from app.core.logger import logger
from app.core.parse_client import parse_client, ParseClient
from app.core.redis_client import redis_client, RedisClient
from app.core.email_client import email_client, EmailClient
from app.core.security import (
    hash_password,
    verify_password,
    generate_token,
    generate_activation_token,
    generate_reset_token,
    generate_order_no,
    generate_task_id,
    create_access_token,
    decode_access_token,
    verify_jwt_token,
    is_valid_ethereum_address,
    checksum_address,
    generate_sign,
    verify_sign,
)
from app.core.deps import (
    get_current_user_id,
    get_optional_user_id,
    verify_admin_user,
    get_admin_user_id,
)

__all__ = [
    # Config
    "settings",
    "get_settings",
    # Logger
    "logger",
    # Clients
    "parse_client",
    "ParseClient",
    "redis_client", 
    "RedisClient",
    "email_client",
    "EmailClient",
    # Security
    "hash_password",
    "verify_password",
    "generate_token",
    "generate_activation_token",
    "generate_reset_token",
    "generate_order_no",
    "generate_task_id",
    "create_access_token",
    "decode_access_token",
    "verify_jwt_token",
    "is_valid_ethereum_address",
    "checksum_address",
    "generate_sign",
    "verify_sign",
    # Dependencies
    "get_current_user_id",
    "get_optional_user_id",
    "verify_admin_user",
    "get_admin_user_id",
]
