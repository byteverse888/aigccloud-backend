"""
用户管理端点
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

from app.core.parse_client import parse_client
from app.core.redis_client import redis_client
from app.core.email_client import email_client
from app.core.web3_client import web3_client
from app.core.security import (
    hash_password, 
    generate_activation_token, 
    generate_reset_token,
    is_valid_ethereum_address,
    checksum_address
)
from app.core.deps import get_current_user_id, get_admin_user_id
from app.core.config import settings

router = APIRouter()


# ============ 请求/响应模型 ============

class UserRegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    invite_code: Optional[str] = None


class PhoneRegisterRequest(BaseModel):
    phone: str
    code: str
    password: str
    username: Optional[str] = None
    invite_code: Optional[str] = None


class UserActivateRequest(BaseModel):
    token: str


class UserBindWeb3Request(BaseModel):
    web3_address: str


class ResetPasswordRequest(BaseModel):
    email: EmailStr


class SetNewPasswordRequest(BaseModel):
    token: str
    new_password: str


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    role: str
    level: int
    is_paid: bool
    paid_expire_at: Optional[datetime] = None
    web3_address: Optional[str] = None
    invite_count: int = 0
    success_reg_count: int = 0
    # 金币余额通过 web3_address 从联盟链获取，不存储在Parse


# ============ 端点 ============

@router.post("/register", response_model=dict)
async def register_user(request: UserRegisterRequest, req: Request):
    """
    用户注册 - 邮箱注册方式
    发送激活邮件到用户邮箱
    """
    # 1. 检查用户名是否已存在
    existing_users = await parse_client.query_users(
        where={"$or": [{"username": request.username}, {"email": request.email}]}
    )
    if existing_users.get("results"):
        raise HTTPException(status_code=400, detail="用户名或邮箱已存在")
    
    # 2. 生成激活Token
    token = generate_activation_token()
    
    # 3. 存储注册信息到Redis
    user_data = {
        "username": request.username,
        "email": request.email,
        "password": request.password,  # 存储原始密码，激活时再hash
        "invite_code": request.invite_code,
        "created_at": datetime.now().isoformat()
    }
    await redis_client.set_activation_token(token, user_data, ex=86400)
    
    # 4. 发送激活邮件
    base_url = str(req.base_url).rstrip("/")
    await email_client.send_activation_email(
        to=request.email,
        username=request.username,
        token=token,
        base_url=base_url
    )
    
    return {
        "success": True,
        "message": "注册成功，请查收激活邮件",
    }


@router.post("/register-phone", response_model=dict)
async def register_phone(request: PhoneRegisterRequest):
    """
    用户注册 - 手机号注册方式
    验证短信验证码后直接创建用户
    """
    phone = request.phone
    code = request.code
    
    # 1. 验证验证码
    code_key = f"sms_code:register:{phone}"
    stored_code = await redis_client.get(code_key)
    
    if not stored_code or stored_code != code:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    
    # 2. 检查手机号是否已存在
    existing_users = await parse_client.query_users(where={"phone": phone})
    if existing_users.get("results"):
        raise HTTPException(status_code=400, detail="该手机号已注册")
    
    # 3. 生成用户名（如果未提供）
    username = request.username or f"user_{phone[-4:]}{datetime.now().strftime('%m%d%H%M')}"
    
    # 检查用户名是否已存在
    existing_username = await parse_client.query_users(where={"username": username})
    if existing_username.get("results"):
        username = f"{username}_{datetime.now().strftime('%S')}"
    
    # 4. 创建用户
    extra_data = {
        "phone": phone,
        "role": "user",
        "level": 1,
        "isPaid": False,
        "inviteCount": 0,
        "successRegCount": 0,
        "totalIncentive": 0,
    }
    
    # 处理邀请码
    if request.invite_code:
        inviter = await parse_client.query_users(
            where={"objectId": {"$regex": f"^{request.invite_code}"}}
        )
        if inviter.get("results"):
            inviter_user = inviter["results"][0]
            extra_data["inviterId"] = inviter_user["objectId"]
            await parse_client.update_user(
                inviter_user["objectId"],
                {
                    "inviteCount": parse_client.increment(1),
                    "successRegCount": parse_client.increment(1)
                }
            )
    
    new_user = await parse_client.create_user(
        username=username,
        email=f"{phone}@phone.local",  # 临时邮箱
        password=request.password,
        extra_data=extra_data
    )
    
    # 5. 发放注册奖励
    await parse_client.create_object("Incentive", {
        "userId": new_user["objectId"],
        "type": "register",
        "amount": 100,
        "description": "注册奖励"
    })
    await parse_client.update_user(new_user["objectId"], {
        "totalIncentive": parse_client.increment(100)
    })
    
    # 6. 删除验证码
    await redis_client.delete(code_key)
    
    return {
        "success": True,
        "message": "注册成功，您已获得100金币注册奖励",
        "user": {
            "objectId": new_user["objectId"],
            "username": username,
            "phone": phone,
        }
    }


@router.get("/activate/{token}")
async def activate_user(token: str):
    """
    激活用户账号
    """
    # 1. 从Redis获取注册信息
    user_data = await redis_client.get_activation_token(token)
    if not user_data:
        raise HTTPException(status_code=400, detail="激活链接无效或已过期")
    
    # 2. 再次检查用户名/邮箱是否被占用
    existing_users = await parse_client.query_users(
        where={"$or": [{"username": user_data["username"]}, {"email": user_data["email"]}]}
    )
    if existing_users.get("results"):
        await redis_client.delete_activation_token(token)
        raise HTTPException(status_code=400, detail="用户名或邮箱已被注册")
    
    # 3. 创建用户
    extra_data = {
        "role": "user",
        "level": 1,
        "isPaid": False,
        "inviteCount": 0,
        "successRegCount": 0,
        "totalIncentive": 0,
    }
    
    # 处理邀请码
    if user_data.get("invite_code"):
        # 查找邀请人
        inviter = await parse_client.query_users(
            where={"objectId": {"$regex": f"^{user_data['invite_code']}"}}
        )
        if inviter.get("results"):
            inviter_user = inviter["results"][0]
            extra_data["inviterId"] = inviter_user["objectId"]
            # 更新邀请人的统计
            await parse_client.update_user(
                inviter_user["objectId"],
                {
                    "inviteCount": parse_client.increment(1),
                    "successRegCount": parse_client.increment(1)
                }
            )
    
    new_user = await parse_client.create_user(
        username=user_data["username"],
        email=user_data["email"],
        password=user_data["password"],
        extra_data=extra_data
    )
    
    # 4. 发放注册奖励
    await parse_client.create_object("Incentive", {
        "userId": new_user["objectId"],
        "type": "register",
        "amount": 100,
        "description": "注册奖励"
    })
    await parse_client.update_user(new_user["objectId"], {
        "totalIncentive": parse_client.increment(100)
    })
    
    # 5. 删除Redis中的Token
    await redis_client.delete_activation_token(token)
    
    # 返回HTML页面提示激活成功
    return {
        "success": True,
        "message": "账号激活成功，您已获得100金币注册奖励",
        "redirect": "/login"
    }


@router.post("/forgot-password")
async def forgot_password(request: ResetPasswordRequest, req: Request):
    """
    忘记密码 - 发送重置邮件
    """
    # 查找用户
    users = await parse_client.query_users(where={"email": request.email})
    if not users.get("results"):
        # 为了安全，不暴露邮箱是否存在
        return {"success": True, "message": "如果邮箱存在，您将收到重置密码的邮件"}
    
    user = users["results"][0]
    
    # 生成重置Token
    token = generate_reset_token()
    await redis_client.set_reset_password_token(token, user["objectId"], ex=3600)
    
    # 发送重置邮件
    base_url = str(req.base_url).rstrip("/")
    await email_client.send_reset_password_email(
        to=request.email,
        username=user["username"],
        token=token,
        base_url=base_url
    )
    
    return {"success": True, "message": "如果邮箱存在，您将收到重置密码的邮件"}


@router.post("/reset-password")
async def reset_password(request: SetNewPasswordRequest):
    """
    重置密码
    """
    # 获取Token对应的用户ID
    user_id = await redis_client.get_reset_password_token(request.token)
    if not user_id:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")
    
    # 更新密码 - Parse会自动hash
    await parse_client.update_user(user_id, {"password": request.new_password})
    
    # 删除Token
    await redis_client.delete(f"reset_pwd:{request.token}")
    
    return {"success": True, "message": "密码重置成功"}


@router.post("/bind-web3")
async def bind_web3_address(
    request: UserBindWeb3Request,
    user_id: str = Depends(get_current_user_id)
):
    """
    绑定Web3地址到用户账号
    """
    # 验证地址格式
    if not is_valid_ethereum_address(request.web3_address):
        raise HTTPException(status_code=400, detail="无效的以太坊地址")
    
    # 转换为校验和格式
    address = checksum_address(request.web3_address)
    
    # 检查地址是否已被绑定
    existing = await parse_client.query_users(where={"web3Address": address})
    if existing.get("results"):
        raise HTTPException(status_code=400, detail="该地址已被其他账号绑定")
    
    # 更新用户
    await parse_client.update_user(user_id, {"web3Address": address})
    
    return {
        "success": True,
        "message": "Web3地址绑定成功",
        "address": address
    }


@router.get("/verify-web3/{address}")
async def verify_web3_address(address: str):
    """
    验证Web3地址是否有效
    """
    is_valid = is_valid_ethereum_address(address)
    return {
        "success": True,
        "valid": is_valid,
        "address": checksum_address(address) if is_valid else address,
    }


@router.get("/me", response_model=UserResponse)
async def get_current_user(user_id: str = Depends(get_current_user_id)):
    """
    获取当前用户信息
    """
    try:
        user = await parse_client.get_user(user_id)
        return UserResponse(
            id=user["objectId"],
            username=user["username"],
            email=user.get("email", ""),
            role=user.get("role", "user"),
            level=user.get("level", 1),
            is_paid=user.get("isPaid", False),
            paid_expire_at=user.get("paidExpireAt"),
            web3_address=user.get("web3Address"),
            invite_count=user.get("inviteCount", 0),
            success_reg_count=user.get("successRegCount", 0),
        )
    except Exception:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: str):
    """
    获取用户信息
    """
    try:
        user = await parse_client.get_user(user_id)
        return UserResponse(
            id=user["objectId"],
            username=user["username"],
            email=user.get("email", ""),
            role=user.get("role", "user"),
            level=user.get("level", 1),
            is_paid=user.get("isPaid", False),
            paid_expire_at=user.get("paidExpireAt"),
            web3_address=user.get("web3Address"),
            invite_count=user.get("inviteCount", 0),
            success_reg_count=user.get("successRegCount", 0),
        )
    except Exception:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/{user_id}/balance")
async def get_user_balance(user_id: str):
    """
    获取用户金币余额（从联盟链查询）
    """
    try:
        user = await parse_client.get_user(user_id)
        web3_address = user.get("web3Address")
        
        if not web3_address:
            return {
                "coins": 0,
                "web3_address": None,
                "message": "用户未绑定Web3地址"
            }
        
        # 从联盟链获取余额
        balance = await web3_client.get_balance(web3_address)
        
        return {
            "coins": balance,
            "web3_address": web3_address,
        }
    except Exception:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/{user_id}/check-membership")
async def check_membership(user_id: str):
    """
    检查用户会员状态
    """
    try:
        user = await parse_client.get_user(user_id)
        is_paid = user.get("isPaid", False)
        paid_expire_at = user.get("paidExpireAt")
        
        # 检查是否过期
        if is_paid and paid_expire_at:
            expire_date = datetime.fromisoformat(paid_expire_at.replace("Z", "+00:00"))
            if expire_date < datetime.now(expire_date.tzinfo):
                is_paid = False
                # 更新用户状态
                await parse_client.update_user(user_id, {"isPaid": False})
        
        # 从联盟链获取余额
        web3_address = user.get("web3Address")
        coins = 0
        if web3_address:
            coins = await web3_client.get_balance(web3_address)
        
        return {
            "is_paid": is_paid,
            "paid_expire_at": paid_expire_at,
            "coins": coins,
            "web3_address": web3_address,
        }
    except Exception:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/admin/list")
async def list_users(
    page: int = 1,
    limit: int = 20,
    role: Optional[str] = None,
    admin_id: str = Depends(get_admin_user_id)
):
    """
    获取用户列表(管理员)
    """
    where = {}
    if role:
        where["role"] = role
    
    skip = (page - 1) * limit
    result = await parse_client.query_users(
        where=where if where else None,
        order="-createdAt",
        limit=limit,
        skip=skip
    )
    
    total = await parse_client.count_objects("_User", where if where else None)
    
    return {
        "data": result.get("results", []),
        "total": total,
        "page": page,
        "limit": limit
    }
    
    total = await parse_client.count_objects("_User", where if where else None)
    
    return {
        "data": result.get("results", []),
        "total": total,
        "page": page,
        "limit": limit
    }
