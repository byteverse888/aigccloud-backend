"""
推广系统端点
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from app.core.parse_client import parse_client
from app.core.web3_client import web3_client
from app.core.deps import get_current_user_id, get_current_user_id_compat
from app.core.config import settings
from app.core.incentive_service import incentive_service, INCENTIVE_CONFIG
from app.core.logger import logger
from app.core.promotion_code import (
    get_or_create_invite_code,
    resolve_inviter_by_code,
)

router = APIRouter()


# ============ 模型 ============

class PromotionStats(BaseModel):
    invite_count: int
    success_reg_count: int
    total_invite_reward: float  # 邀请奖励总额（从日志查询）
    invite_link: str
    invite_code: str
    web3_address: Optional[str] = None


class InviteRecord(BaseModel):
    id: str
    invitee_id: str
    invitee_name: str
    status: str  # registered, first_recharged
    reward: float
    created_at: datetime


# ============ 端点 ============

@router.get("/link")
async def get_promotion_link(user_id: str = Depends(get_current_user_id_compat)):
    """
    获取用户的推广链接
    """
    # 使用 inviteCode 短码作为邀请码，避免对外暴露内部 objectId
    # 首次访问时懒生成 8 位 base32 短码并写入 _User.inviteCode
    invite_code = await get_or_create_invite_code(user_id)
    
    # 获取前端基础URL
    base_url = settings.frontend_url.rstrip("/")
    invite_link = f"{base_url}/register?ref={invite_code}"
    
    return {
        "invite_link": invite_link,
        "invite_code": invite_code,
        "qr_code": f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={invite_link}",
    }


@router.get("/stats", response_model=PromotionStats)
async def get_promotion_stats(user_id: str = Depends(get_current_user_id_compat)):
    """
    获取用户的推广统计
    """
    try:
        user = await parse_client.get_user(user_id)
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    invite_count = user.get("inviteCount", 0)
    success_reg_count = user.get("successRegCount", 0)
    
    # 统计邀请奖励总额（注册奖励 + 首充返利），从 AccountRecord 流水累加
    total_invite_reward = 0.0
    try:
        result = await parse_client.query_objects(
            "AccountRecord",
            where={
                "userId": user_id,
                "type": "reward",
                "category": {"$in": ["invite_register", "invite_first_recharge"]},
                "amount": {"$gt": 0},
                "status": "success",
            },
            limit=1000,
        )
        total_invite_reward = sum(float(item.get("amount", 0) or 0) for item in result.get("results", []))
    except Exception:
        total_invite_reward = 0.0
    
    invite_code = await get_or_create_invite_code(user_id)
    base_url = settings.frontend_url.rstrip("/")
    
    return PromotionStats(
        invite_count=invite_count,
        success_reg_count=success_reg_count,
        total_invite_reward=total_invite_reward,
        invite_link=f"{base_url}/register?ref={invite_code}",
        invite_code=invite_code,
        web3_address=user.get("web3Address"),
    )


@router.get("/records")
async def get_promotion_records(
    page: int = 1,
    limit: int = 20,
    user_id: str = Depends(get_current_user_id_compat)
):
    """
    获取推广记录列表
    """
    # 查询被邀请的用户
    skip = (page - 1) * limit
    # 容错：若当前账号从未邀请过任何人，_User.inviterId 列可能从未被写入（
    # PG 后端 lazy schema 下可能返 500）——该场景应该返回空记录而非报错。
    try:
        result = await parse_client.query_users(
            where={"inviterId": user_id},
            order="-createdAt",
            limit=limit,
            skip=skip
        )
    except Exception as exc:
        logger.warning("query invitees failed (treat as empty) user=%s: %s", user_id, exc)
        return {"data": [], "total": 0, "page": page, "limit": limit}

    try:
        total = await parse_client.count_objects("_User", {"inviterId": user_id})
    except Exception as exc:
        logger.warning("count invitees failed user=%s: %s", user_id, exc)
        total = len(result.get("results", []))
    
    records = []
    for invitee in result.get("results", []):
        invitee_id = invitee["objectId"]
        invitee_username = invitee.get("username", "")
        # 注册奖励：精准查 relatedId=invite_register_<invitee_id>
        register_reward = 0.0
        try:
            reg = await parse_client.query_objects(
                "AccountRecord",
                where={
                    "userId": user_id,
                    "category": "invite_register",
                    "relatedId": f"invite_register_{invitee_id}",
                    "status": "success",
                },
                limit=1,
            )
            register_reward = sum(float(r.get("amount", 0) or 0) for r in reg.get("results", []))
        except Exception:
            pass
        # 首充返利：优先按结构化字段 inviteeId 查询（准确且高效），
        # 存量旧数据未写 inviteeId 时回退到 description 模糊匹配以保障展示完整性。
        recharge_reward = 0.0
        try:
            rec_struct = await parse_client.query_objects(
                "AccountRecord",
                where={
                    "userId": user_id,
                    "category": "invite_first_recharge",
                    "inviteeId": invitee_id,
                    "status": "success",
                },
                limit=10,
            )
            recharge_reward = sum(
                float(r.get("amount", 0) or 0) for r in rec_struct.get("results", [])
            )
        except Exception:
            recharge_reward = 0.0

        if recharge_reward <= 0 and invitee_username:
            # 回退：存量账本未写 inviteeId 时使用 description 模糊匹配
            try:
                # 转义正则特殊字符，避免用户名含元字符导致查询异常
                import re as _re
                safe_pattern = _re.escape(invitee_username)
                rec_legacy = await parse_client.query_objects(
                    "AccountRecord",
                    where={
                        "userId": user_id,
                        "category": "invite_first_recharge",
                        "description": {"$regex": safe_pattern},
                        "status": "success",
                    },
                    limit=10,
                )
                recharge_reward = sum(
                    float(r.get("amount", 0) or 0) for r in rec_legacy.get("results", [])
                )
            except Exception:
                pass
        total_reward = round(register_reward + recharge_reward, 2)
        
        status = "first_recharged" if invitee.get("firstRechargeRewarded") else "registered"
        
        records.append({
            "id": invitee["objectId"],
            "invitee_id": invitee["objectId"],
            "invitee_name": invitee["username"],
            "status": status,
            "reward": total_reward,
            # 拆分字段：便于前端区分展示“奖励”与“可兑换积分”
            # 按业务定义：可兑换积分仅来自首充返利，与 _User.exchangeableBalance 的累加口径一致
            "register_reward": round(register_reward, 2),
            "recharge_reward": round(recharge_reward, 2),
            "exchangeable_reward": round(recharge_reward, 2),
            "created_at": invitee["createdAt"],
        })
    
    return {
        "data": records,
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/leaderboard")
async def get_promotion_leaderboard(limit: int = 10):
    """
    获取推广排行榜
    """
    # 按邀请成功人数排序
    result = await parse_client.query_users(
        where={"successRegCount": {"$gt": 0}},
        order="-successRegCount",
        limit=limit
    )
    
    leaderboard = []
    rank = 1
    for user in result.get("results", []):
        leaderboard.append({
            "rank": rank,
            "user_id": user["objectId"],
            "username": user["username"],
            "invite_count": user.get("inviteCount", 0),
            "success_reg_count": user.get("successRegCount", 0),
        })
        rank += 1
    
    return {"leaderboard": leaderboard}


@router.post("/bind-inviter")
async def bind_inviter(
    invite_code: str,
    user_id: str = Depends(get_current_user_id_compat)
):
    """
    绑定邀请人(注册后补绑定)
    """
    # 获取当前用户
    try:
        user = await parse_client.get_user(user_id)
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 检查是否已有邀请人
    if user.get("inviterId"):
        raise HTTPException(status_code=400, detail="已绑定邀请人")
    
    # 查找邀请人：优先按 inviteCode 短码反查，未命中时回退 objectId。
    # 这里主动增加了对“存量已分发以 objectId 为邀请码”的链接的兼容。
    inviter = await resolve_inviter_by_code(invite_code)
    if inviter is None:
        raise HTTPException(status_code=404, detail="邀请码无效")
    
    # 不能自己邀请自己
    if inviter["objectId"] == user_id:
        raise HTTPException(status_code=400, detail="不能使用自己的邀请码")
    
    # 绑定邀请人（_User 系统类必须 Master Key，否则报 Parse code 206 / HTTP 400）
    await parse_client.update_user_with_master_key(user_id, {"inviterId": inviter["objectId"]})
    
    # 更新邀请人统计（同样必须 Master Key）
    await parse_client.update_user_with_master_key(inviter["objectId"], {
        "inviteCount": parse_client.increment(1),
        "successRegCount": parse_client.increment(1)
    })
    
    # 发放邀请奖励（通过激励服务）
    # 必须传入 invitee_id，否则 AccountRecord 不会写 relatedId=invite_register_<id> 与 inviteeId，
    # 导致客户端 /promotion/records 按 relatedId 精准查询不到、“激励”列始终显示 -
    reward_result = await incentive_service.grant_invite_register_reward(
        inviter_id=inviter["objectId"],
        invitee_name=user.get("username", "新用户"),
        invitee_id=user_id,
    )
    
    return {
        "success": True,
        "message": "邀请人绑定成功",
        "inviter_name": inviter["username"],
        "reward": {
            "granted": reward_result.get("success"),
            "amount": reward_result.get("amount") if reward_result.get("success") else None
        }
    }


@router.get("/rewards-config")
async def get_rewards_config():
    """
    获取推广奖励配置（来自 SystemConfig.category=promotion）
    """
    cfg = await incentive_service._get_promotion_config()
    rate = float(cfg.get("inviteFirstRechargeRate") or 0)
    enabled = bool(cfg.get("enabled", True))
    # disabled_reason 仅在 enabled=False 时输出，供前端展示具体原因
    # （如 "临时调整中"、"预算耗尽"、"宝藏中" 等运营含义文案）
    disabled_reason = str(cfg.get("disabledReason") or "") if not enabled else ""
    return {
        "register_reward": float(cfg.get("inviteRegisterReward") or 0),
        "first_recharge_rate": rate,
        "enabled": enabled,
        "disabled_reason": disabled_reason,
        "rules": list(cfg.get("rules") or []),
    }
