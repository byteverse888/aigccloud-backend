"""
认证端点 - 处理登录和Parse配置下发
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from app.core.parse_client import parse_client
from app.core.redis_client import redis_client
from app.core.security import create_access_token, verify_jwt_token, generate_sms_code
from app.core.config import settings

router = APIRouter()


# ============ 请求/响应模型 ============

class LoginRequest(BaseModel):
    username: str
    password: str


class PhoneLoginRequest(BaseModel):
    phone: str
    code: str


class SendSmsRequest(BaseModel):
    phone: str
    type: str = "login"  # login, register


class LoginResponse(BaseModel):
    success: bool
    token: str  # FastAPI JWT Token
    user: dict
    parse_config: dict  # 登录后下发Parse配置
    message: Optional[str] = None


class ParseConfigResponse(BaseModel):
    server_url: str
    app_id: str
    # 注意：不下发 Master Key，只下发 JS Key（如果需要客户端直连）
    # 但根据新架构，客户端不再直连Parse


# ============ 端点 ============

@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """
    用户登录
    - 验证用户名密码
    - 返回JWT Token和Parse配置
    """
    try:
        # 通过Parse验证登录
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.parse_server_url}/login",
                params={"username": request.username, "password": request.password},
                headers={
                    "X-Parse-Application-Id": settings.parse_app_id,
                    "X-Parse-Revocable-Session": "1"
                },
                timeout=30.0
            )
        
        if response.status_code != 200:
            error_data = response.json()
            raise HTTPException(
                status_code=401, 
                detail=error_data.get("error", "用户名或密码错误")
            )
        
        user_data = response.json()
        user_id = user_data.get("objectId")
        session_token = user_data.get("sessionToken")
        
        # 生成FastAPI JWT Token
        jwt_token = create_access_token(data={
            "sub": user_id,
            "username": user_data.get("username"),
            "role": user_data.get("role", "user"),
            "parse_session": session_token,  # 包含Parse session以便后续使用
        })
        
        # 更新最后登录时间
        await parse_client.update_user(user_id, {
            "lastLoginAt": datetime.now().isoformat()
        })
        
        # 构建用户信息（过滤敏感字段）
        safe_user = {
            "objectId": user_data.get("objectId"),
            "username": user_data.get("username"),
            "email": user_data.get("email"),
            "phone": user_data.get("phone"),
            "role": user_data.get("role", "user"),
            "level": user_data.get("level", 1),
            "isPaid": user_data.get("isPaid", False),
            "coins": user_data.get("coins", 0),  # 金币余额
            "avatar": user_data.get("avatar"),
            "avatarKey": user_data.get("avatarKey"),
            "web3Address": user_data.get("web3Address"),
            "inviteCount": user_data.get("inviteCount", 0),
        }
        
        # 下发Parse配置（不包含Master Key）
        parse_config = {
            "serverUrl": settings.parse_server_url,
            "appId": settings.parse_app_id,
            # 不下发 JS Key - 所有Parse操作都通过Server Actions或FastAPI
        }
        
        return LoginResponse(
            success=True,
            token=jwt_token,
            user=safe_user,
            parse_config=parse_config,
            message="登录成功"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send-sms")
async def send_sms_code(request: SendSmsRequest):
    """
    发送短信验证码
    """
    phone = request.phone
    sms_type = request.type
    
    # 验证手机号格式
    if not phone or len(phone) != 11 or not phone.isdigit():
        raise HTTPException(status_code=400, detail="请输入有效的手机号")
    
    # 检查发送频率限制（60秒内只能发一次）
    rate_key = f"sms_rate:{phone}"
    if await redis_client.get(rate_key):
        raise HTTPException(status_code=429, detail="发送过于频繁，请60秒后重试")
    
    # 如果是注册，检查手机号是否已存在
    if sms_type == "register":
        existing = await parse_client.query_users(where={"phone": phone})
        if existing.get("results"):
            raise HTTPException(status_code=400, detail="该手机号已注册")
    
    # 如果是登录，检查手机号是否存在
    if sms_type == "login":
        existing = await parse_client.query_users(where={"phone": phone})
        if not existing.get("results"):
            raise HTTPException(status_code=400, detail="该手机号未注册")
    
    # 生成验证码
    code = generate_sms_code()
    
    # 存储验证码（5分钟有效）
    code_key = f"sms_code:{sms_type}:{phone}"
    await redis_client.set(code_key, code, ex=300)
    
    # 设置发送频率限制
    await redis_client.set(rate_key, "1", ex=60)
    
    # TODO: 实际发送短信（对接短信服务商）
    # 开发环境直接打印验证码
    print(f"[SMS] Phone: {phone}, Code: {code}, Type: {sms_type}")
    
    return {
        "success": True,
        "message": "验证码已发送",
        # 开发环境返回验证码方便测试
        "code": code if settings.debug else None
    }


@router.post("/phone-login")
async def phone_login(request: PhoneLoginRequest):
    """
    手机号验证码登录
    """
    phone = request.phone
    code = request.code
    
    # 验证验证码
    code_key = f"sms_code:login:{phone}"
    stored_code = await redis_client.get(code_key)
    
    if not stored_code or stored_code != code:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    
    # 删除验证码
    await redis_client.delete(code_key)
    
    # 查找用户
    users = await parse_client.query_users(where={"phone": phone})
    if not users.get("results"):
        raise HTTPException(status_code=400, detail="用户不存在")
    
    user_data = users["results"][0]
    user_id = user_data.get("objectId")
    
    # 生成JWT Token
    jwt_token = create_access_token(data={
        "sub": user_id,
        "username": user_data.get("username"),
        "role": user_data.get("role", "user"),
    })
    
    # 更新最后登录时间
    await parse_client.update_user(user_id, {
        "lastLoginAt": datetime.now().isoformat()
    })
    
    # 构建用户信息
    safe_user = {
        "objectId": user_data.get("objectId"),
        "username": user_data.get("username"),
        "email": user_data.get("email"),
        "phone": user_data.get("phone"),
        "role": user_data.get("role", "user"),
        "level": user_data.get("level", 1),
        "isPaid": user_data.get("isPaid", False),
        "coins": user_data.get("coins", 0),
        "avatar": user_data.get("avatar"),
        "avatarKey": user_data.get("avatarKey"),
        "web3Address": user_data.get("web3Address"),
        "inviteCount": user_data.get("inviteCount", 0),
    }
    
    parse_config = {
        "serverUrl": settings.parse_server_url,
        "appId": settings.parse_app_id,
    }
    
    return {
        "success": True,
        "token": jwt_token,
        "user": safe_user,
        "parse_config": parse_config,
        "message": "登录成功"
    }


@router.post("/logout")
async def logout(token: str = Depends(verify_jwt_token)):
    """
    用户登出
    """
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 可以将token加入黑名单（Redis）
    # await redis_client.set(f"blacklist:{token}", "1", ex=86400)
    
    return {"success": True, "message": "登出成功"}


@router.get("/me")
async def get_current_user(token: str = Depends(verify_jwt_token)):
    """
    获取当前用户信息
    """
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    
    # token 是 user_id
    try:
        user = await parse_client.get_user(token)
        return {
            "success": True,
            "user": {
                "objectId": user.get("objectId"),
                "username": user.get("username"),
                "email": user.get("email"),
                "phone": user.get("phone"),
                "role": user.get("role", "user"),
                "level": user.get("level", 1),
                "isPaid": user.get("isPaid", False),
                "coins": user.get("coins", 0),
                "avatar": user.get("avatar"),
                "avatarKey": user.get("avatarKey"),
                "web3Address": user.get("web3Address"),
            }
        }
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")


@router.get("/config")
async def get_parse_config():
    """
    获取Parse配置（公开信息）
    注意：不包含敏感信息
    """
    return {
        "serverUrl": settings.parse_server_url,
        "appId": settings.parse_app_id,
        # 不返回 master_key 或 js_key
    }


@router.post("/refresh")
async def refresh_token(current_token: str = Depends(verify_jwt_token)):
    """
    刷新JWT Token
    """
    if not current_token:
        raise HTTPException(status_code=401, detail="Token无效")
    
    # 获取用户信息
    try:
        user = await parse_client.get_user(current_token)
        
        # 生成新Token
        new_token = create_access_token(data={
            "sub": user.get("objectId"),
            "username": user.get("username"),
            "role": user.get("role", "user"),
        })
        
        return {
            "success": True,
            "token": new_token,
        }
    except Exception:
        raise HTTPException(status_code=401, detail="Token无效")
