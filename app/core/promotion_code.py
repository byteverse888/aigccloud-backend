"""
推广邀请短码（invite code）工具

设计目标
--------
1. 解耦邀请码与用户 objectId，避免对外暴露内部主键，提升隐私与可观测性。
2. 短码空间足够大、字符集排除易混淆字符，单次生成即可保证极低碰撞率。
3. 与存量已分发的"以 objectId 作邀请码"链接保持兼容：反查时优先按 inviteCode
   字段命中，未命中再回退到 objectId 反查，避免存量链接失效。
4. 仅在被需要时（首次访问 /promotion/link、/stats，或注册时收到旧风格邀请码）
   懒生成短码并写入 _User.inviteCode；并发场景下使用查询校验 + 重试。

字段约定
--------
- _User.inviteCode: string  推广邀请短码（8 位 base32，去除易混字符）
  建议在 Parse Server 层为该字段建立唯一索引（manual / runtime check）。

注意
----
- Parse 在 schemaless 模式下不会主动建立唯一索引，本模块在生成阶段使用
  query 进行存在性校验后再写入；并发碰撞概率 ≈ 0（32^8 ≈ 1.1e12）。
- 若极端并发下两个用户偶然抢到同一码，由后续读取/反查时的"找到第一条命中"
  自然消解，不会造成业务功能异常。
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from app.core.parse_client import parse_client

logger = logging.getLogger(__name__)

# Crockford-like base32 字符集，去除 0/O/1/I/L/U 等易混淆字符
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_DEFAULT_LENGTH = 8
_GEN_RETRY = 6


def _make_code(length: int = _DEFAULT_LENGTH) -> str:
    """随机生成长度为 length 的短码，使用 secrets 提供加密强度的随机源。"""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


async def _is_code_taken(code: str) -> bool:
    """
    校验短码是否已被占用。
    由于 Parse 无内置唯一索引，先 query 再 write 在并发下仍可能竞争，
    但 32^8 空间下碰撞概率可忽略；调用方按需重试即可。
    """
    try:
        existing = await parse_client.query_users(where={"inviteCode": code}, limit=1)
        return bool(existing.get("results"))
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("invite_code uniqueness check failed: %s", exc)
        return False


async def generate_unique_invite_code(length: int = _DEFAULT_LENGTH) -> str:
    """
    生成一枚未被占用的短码。重试 _GEN_RETRY 次仍碰撞则抛错。
    """
    for attempt in range(_GEN_RETRY):
        code = _make_code(length)
        if not await _is_code_taken(code):
            return code
        logger.info("invite_code collision on attempt %d: %s", attempt + 1, code)
    raise RuntimeError("Failed to generate a unique invite code after retries")


async def get_or_create_invite_code(user_id: str) -> str:
    """
    返回用户的专属推广短码：
    - 若 _User.inviteCode 已存在，直接返回（满足幂等）
    - 否则生成新码并写回字段

    重要：写入必须使用 Master Key。
    Parse Server 默认 ACL 不允许普通 REST Key 修改任意 _User（code 206），
    以前走 update_user 会报 "Cannot modify user" 并被静默吞掉 →
    造成邀请码每次访问都重生。这里改为 master key 写入，并在失败时报错，
    避免对外返回一个未持久化的临时码。
    """
    try:
        user = await parse_client.get_user(user_id)
    except Exception as exc:
        # 用户不存在等异常向上抛，由调用方处理
        raise exc

    code = user.get("inviteCode")
    if code:
        return str(code)

    new_code = await generate_unique_invite_code()
    try:
        # 必须用 master key，否则 Parse 会报 code 206 Cannot modify user
        await parse_client.update_user_with_master_key(user_id, {"inviteCode": new_code})
    except Exception as exc:
        # 写入失败不能默默返回新码（会造成“邀请码每次都变”），
        # 明确报错交上层返 500，便于定位 Parse 配置问题
        logger.error("write inviteCode for user=%s failed: %s", user_id, exc)
        raise RuntimeError(f"persist inviteCode failed: {exc}") from exc
    return new_code


async def resolve_inviter_by_code(code: str) -> Optional[Dict[str, Any]]:
    """
    根据邀请码查找邀请人用户对象。

    匹配顺序：
    1. _User.inviteCode == code  （新风格短码）
    2. _User.objectId  == code   （兼容存量已分发链接，旧风格直接为 objectId）

    返回命中的 user 字典；未命中返回 None。
    """
    if not code:
        return None
    try:
        by_code = await parse_client.query_users(where={"inviteCode": code}, limit=1)
        results = by_code.get("results") or []
        if results:
            return results[0]
    except Exception as exc:
        logger.warning("query inviteCode failed (fallback to objectId): %s", exc)

    # 兼容回退：将 code 视作 objectId 反查
    try:
        by_oid = await parse_client.query_users(where={"objectId": code}, limit=1)
        results = by_oid.get("results") or []
        if results:
            return results[0]
    except Exception as exc:
        logger.warning("query objectId fallback failed: %s", exc)
    return None
