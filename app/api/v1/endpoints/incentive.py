"""
激励系统端点
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from enum import Enum
from datetime import datetime

from app.core.parse_client import parse_client
from app.core.redis_client import redis_client
from app.core.web3_client import web3_client
from app.core.deps import get_current_user_id
from app.core.incentive_service import incentive_service, IncentiveType, INCENTIVE_CONFIG

router = APIRouter()


# ============ 模型 ============

class ClaimDailyRequest(BaseModel):
    pass  # 无需额外参数


class GrantIncentiveRequest(BaseModel):
    user_id: str
    type: str  # IncentiveType 字符串
    amount: float
    description: str


class IncentiveRecord(BaseModel):
    id: str
    type: str
    amount: float
    description: str
    created_at: datetime


# ============ 端点 ============

@router.post("/daily")
async def claim_daily_reward(user_id: str = Depends(get_current_user_id)):
    """
    领取每日登录奖励
    """
    # 1. 检查今日是否已领取
    already_claimed = await redis_client.check_daily_claim(user_id)
    if already_claimed:
        raise HTTPException(status_code=400, detail="今日奖励已领取")
    
    # 2. 通过激励服务发放奖励
    result = await incentive_service.grant_daily_login(user_id)
    
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "发放奖励失败"))
    
    # 3. 设置Redis领取标记
    await redis_client.set_daily_claim_flag(user_id)
    
    return {
        "success": True,
        "amount": result.get("amount"),
        "tx_hash": result.get("tx_hash"),
        "message": result.get("message", f"领取成功，获得 {result.get('amount')} 金币"),
    }


@router.get("/daily/status")
async def check_daily_status(user_id: str = Depends(get_current_user_id)):
    """
    检查今日领取状态
    """
    claimed = await redis_client.check_daily_claim(user_id)
    
    # 获取用户信息
    try:
        user = await parse_client.get_user(user_id)
        member_level = user.get("memberLevel", "normal")
        is_vip = member_level in ("vip", "svip")
    except Exception:
        is_vip = False
    
    amount = INCENTIVE_CONFIG["daily_login_paid"] if is_vip else INCENTIVE_CONFIG["daily_login_normal"]
    
    return {
        "claimed": claimed,
        "amount": amount,
        "member_level": member_level if 'member_level' in dir() else "normal",
    }


@router.get("/history")
async def get_incentive_history(
    page: int = 1,
    limit: int = 20,
    type: Optional[str] = None,
    user_id: str = Depends(get_current_user_id)
):
    """
    获取用户激励历史
    """
    where = {"userId": user_id}
    if type:
        where["type"] = type
    
    skip = (page - 1) * limit
    result = await parse_client.query_objects(
        "IncentiveLog",
        where=where,
        order="-createdAt",
        limit=limit,
        skip=skip
    )
    
    total = await parse_client.count_objects("IncentiveLog", where)
    
    records = []
    for item in result.get("results", []):
        records.append({
            "id": item["objectId"],
            "type": item["type"],
            "amount": item["amount"],
            "description": item.get("description", ""),
            "created_at": item["createdAt"],
        })
    
    return {
        "data": records,
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/balance")
async def get_balance(user_id: str = Depends(get_current_user_id)):
    """
    获取用户金币余额（从联盟链查询）
    """
    try:
        user = await parse_client.get_user(user_id)
        web3_address = user.get("web3Address")
        
        coins = 0
        if web3_address:
            coins = await web3_client.get_balance(web3_address)
        
        return {
            "coins": coins,
            "web3_address": web3_address,
            "member_level": user.get("memberLevel", "normal"),
        }
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")


@router.get("/stats")
async def get_incentive_stats(user_id: str = Depends(get_current_user_id)):
    """
    获取激励统计
    """
    try:
        user = await parse_client.get_user(user_id)
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 从联盟链获取当前余额
    web3_address = user.get("web3Address")
    coins = 0
    if web3_address:
        coins = await web3_client.get_balance(web3_address)
    
    # 统计各类型奖励（从日志表）
    stats = {}
    for itype in IncentiveType:
        count = await parse_client.count_objects("IncentiveLog", {
            "userId": user_id,
            "type": itype.value
        })
        stats[itype.value] = count
    
    # 计算总获得金币（从日志表）
    result = await parse_client.query_objects(
        "IncentiveLog",
        where={"userId": user_id, "amount": {"$gt": 0}},
        limit=1000
    )
    total_earned = sum(item.get("amount", 0) for item in result.get("results", []))
    
    return {
        "coins": coins,
        "web3_address": web3_address,
        "total_earned": total_earned,
        "by_type": stats,
    }


@router.post("/grant")
async def grant_incentive(request: GrantIncentiveRequest):
    """
    发放激励(内部接口) - 通过Web3接口铸造金币到联盟链
    """
    # 验证用户存在
    try:
        user = await parse_client.get_user(request.user_id)
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    web3_address = user.get("web3Address")
    if not web3_address:
        raise HTTPException(status_code=400, detail="用户未绑定Web3地址")
    
    # 通过Web3接口铸造金币
    mint_result = await web3_client.mint(web3_address, int(request.amount))
    if not mint_result.get("success"):
        raise HTTPException(status_code=500, detail="发放奖励失败: " + mint_result.get("error", ""))
    
    # 记录激励日志
    await parse_client.create_object("IncentiveLog", {
        "userId": request.user_id,
        "web3Address": web3_address,
        "type": request.type,
        "amount": request.amount,
        "txHash": mint_result.get("tx_hash"),
        "description": request.description
    })
    
    # 获取新余额
    new_balance = await web3_client.get_balance(web3_address)
    
    return {
        "success": True,
        "user_id": request.user_id,
        "amount": request.amount,
        "tx_hash": mint_result.get("tx_hash"),
        "new_coins": new_balance
    }


@router.post("/consume")
async def consume_coins(
    amount: float,
    description: str = "金币消费",
    user_id: str = Depends(get_current_user_id)
):
    """
    消费金币 - 通过Web3接口销毁金币
    """
    # 获取用户信息
    try:
        user = await parse_client.get_user(user_id)
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    web3_address = user.get("web3Address")
    if not web3_address:
        raise HTTPException(status_code=400, detail="用户未绑定Web3地址")
    
    # 检查联盟链上的余额
    balance = await web3_client.get_balance(web3_address)
    if balance < amount:
        raise HTTPException(status_code=400, detail="余额不足")
    
    # 通过Web3接口销毁金币
    burn_result = await web3_client.burn(web3_address, int(amount))
    if not burn_result.get("success"):
        raise HTTPException(status_code=500, detail="消费失败: " + burn_result.get("error", ""))
    
    # 记录消费日志
    await parse_client.create_object("IncentiveLog", {
        "userId": user_id,
        "web3Address": web3_address,
        "type": "consume",
        "amount": -amount,
        "txHash": burn_result.get("tx_hash"),
        "description": description
    })
    
    # 获取新余额
    new_balance = await web3_client.get_balance(web3_address)
    
    return {
        "success": True,
        "consumed": amount,
        "tx_hash": burn_result.get("tx_hash"),
        "new_coins": new_balance
    }
