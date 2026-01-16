"""
认证端点 - 处理登录和Parse配置下发
"""
import uuid
import json
import secrets
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from eth_account.messages import encode_defunct
from web3 import Web3

from app.core.parse_client import parse_client
from app.core.redis_client import redis_client
from app.core.security import (
    create_access_token,
    verify_jwt_token,
    generate_sms_code,
)
from app.core.config import settings
from app.core.logger import logger

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


class EmailRegisterRequest(BaseModel):
    """邮箱注册请求"""
    email: str
    password: str


class EmailLoginRequest(BaseModel):
    """邮箱登录请求"""
    email: str
    password: str


class Web3InitRequest(BaseModel):
    """Web3 登录初始化请求"""
    address: str


class Web3LoginRequest(BaseModel):
    """Web3 登录请求"""
    address: str
    signature: str
    message: str
    password: Optional[str] = None  # 内置钱包需要密码，MetaMask 不需要


class LoginResponse(BaseModel):
    success: bool
    token: str  # FastAPI JWT Token
    user: dict
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
    logger.info(f"[登录] 用户尝试登录: {request.username}")
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
            logger.warning(f"[登录] 登录失败: {request.username}")
            
            # 登录失败，检查用户是否存在
            try:
                existing = await parse_client.query_users(where={"username": request.username})
                if not existing.get("results"):
                    # 用户不存在
                    raise HTTPException(status_code=404, detail="该用户名未注册")
                else:
                    # 用户存在，密码错误
                    raise HTTPException(status_code=401, detail="密码错误")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[登录] 检查用户失败: {str(e)}")
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
        
        # 更新最后登录时间（使用 session token）
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.put(
                    f"{settings.parse_server_url}/users/{user_id}",
                    json={"lastLoginAt": datetime.now().isoformat()},
                    headers={
                        "X-Parse-Application-Id": settings.parse_app_id,
                        "X-Parse-REST-API-Key": settings.parse_rest_api_key,
                        "X-Parse-Session-Token": session_token,
                        "Content-Type": "application/json",
                    },
                    timeout=10.0
                )
        except Exception:
            pass
        
        # 构建用户信息（过滤敏感字段）
        safe_user = {
            "objectId": user_data.get("objectId"),
            "username": user_data.get("username"),
            "email": user_data.get("email"),
            "phone": user_data.get("phone"),
            "role": user_data.get("role", "user"),
            "level": user_data.get("level", 1),
            "memberLevel": user_data.get("memberLevel", "normal"),
            "coins": user_data.get("coins", 0),  # 金币余额
            "avatar": user_data.get("avatar"),
            "avatarKey": user_data.get("avatarKey"),
            "web3Address": user_data.get("web3Address"),
            "inviteCount": user_data.get("inviteCount", 0),
        }
        
        logger.info(f"[登录] 登录成功: {request.username} (ID: {user_id})")
        return LoginResponse(
            success=True,
            token=jwt_token,
            user=safe_user,
            message="登录成功"
        )
        
    except HTTPException as e:
        logger.warning(f"[登录] 登录失败: {request.username} - {e.detail}")
        raise
    except Exception as e:
        logger.error(f"[登录] 登录异常: {request.username} - {str(e)}")
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
    
    # 获取 Parse session token
    session_token = user_data.get("sessionToken")
    
    # 更新最后登录时间（使用 session token）
    if session_token:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.put(
                    f"{settings.parse_server_url}/users/{user_id}",
                    json={"lastLoginAt": datetime.now().isoformat()},
                    headers={
                        "X-Parse-Application-Id": settings.parse_app_id,
                        "X-Parse-REST-API-Key": settings.parse_rest_api_key,
                        "X-Parse-Session-Token": session_token,
                        "Content-Type": "application/json",
                    },
                    timeout=10.0
                )
        except Exception:
            pass
    
    # 构建用户信息
    safe_user = {
        "objectId": user_data.get("objectId"),
        "username": user_data.get("username"),
        "email": user_data.get("email"),
        "phone": user_data.get("phone"),
        "role": user_data.get("role", "user"),
        "level": user_data.get("level", 1),
        "memberLevel": user_data.get("memberLevel", "normal"),
        "coins": user_data.get("coins", 0),
        "avatar": user_data.get("avatar"),
        "avatarKey": user_data.get("avatarKey"),
        "web3Address": user_data.get("web3Address"),
        "inviteCount": user_data.get("inviteCount", 0),
    }
    
    return {
        "success": True,
        "token": jwt_token,
        "user": safe_user,
        "message": "登录成功"
    }


@router.post("/logout")
async def logout(
    token: str = Depends(verify_jwt_token),
    parse_session: Optional[str] = Header(None, alias="X-Parse-Session-Token")
):
    """
    用户登出
    """
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 清除 Parse session
    if parse_session:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{settings.parse_server_url}/logout",
                    headers={
                        "X-Parse-Application-Id": settings.parse_app_id,
                        "X-Parse-Session-Token": parse_session,
                    },
                    timeout=10.0
                )
            if response.status_code == 200:
                logger.info(f"[Auth] Parse session 已清除")
            else:
                logger.warning(f"[Auth] Parse session 清除失败: {response.status_code} - {response.text}")
        except Exception as e:
            logger.warning(f"[Auth] Parse session 清除异常: {e}")
    
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
                "memberLevel": user.get("memberLevel", "normal"),
                "coins": user.get("coins", 0),
                "avatar": user.get("avatar"),
                "avatarKey": user.get("avatarKey"),
                "web3Address": user.get("web3Address"),
            }
        }
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")


@router.get("/config")
async def get_parse_config(authorization: Optional[str] = Header(None)):
    """
    获取Parse配置（需要JWT认证）
    返回 parse_config 包含 appId 和 jsKey
    """
    # 验证 JWT Token
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail="未提供认证Token")
    
    token = authorization.replace('Bearer ', '')
    try:
        verify_jwt_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Token无效")
    
    return {
        "parse_config": {
            "serverUrl": settings.parse_server_url,
            "appId": settings.parse_app_id,
            "jsKey": settings.parse_js_key,
        }
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


# ============ Web3 认证 ============

def validate_eth_address(address: str) -> str:
    """验证并返回 checksum 格式地址"""
    try:
        return Web3.to_checksum_address(address)
    except Exception:
        raise HTTPException(status_code=400, detail="无效的钱包地址")


def verify_signature(message: str, signature: str, expected_address: str) -> str:
    """
    验证签名并返回恢复的地址 (checksum 格式)
    失败时返回空字符串
    """
    try:
        w3 = Web3()
        message_hash = encode_defunct(text=message)
        recovered = w3.eth.account.recover_message(message_hash, signature=signature)
        return Web3.to_checksum_address(recovered)
    except Exception:
        return ""


async def _generate_unique_nonce(address: str) -> str:
    """
    生成 128 位强度 nonce，使用 SETNX 原子操作确保全局唯一
    """
    max_attempts = 10
    for _ in range(max_attempts):
        # 128 位 = 16 字节 = 32 字符 hex
        nonce = secrets.token_hex(16)
        key = f"nonce:{nonce}"
        # 原子操作，仅当键不存在时设置
        if await redis_client.setnx(key, address, ex=900):  # 15 分钟有效
            return nonce
    raise HTTPException(status_code=500, detail="Nonce 生成失败，请重试")


@router.get("/web3/nonce")
async def web3_get_nonce(address: str):
    """
    申请 Nonce - Electron 客户端专用
    
    - 生成 128 位高强度随机数
    - 存入 Redis，15 分钟有效
    - 返回 nonce 和过期时间
    """
    logger.info(f"[Web3] 申请Nonce: {address[:10]}...")
    
    address = validate_eth_address(address)
    nonce = await _generate_unique_nonce(address)
    
    logger.info(f"[Web3] Nonce已生成: {address[:10]}... -> {nonce[:8]}...")
    return {
        "success": True,
        "nonce": nonce,
        "expires_in": 900,  # 15分钟
        "message": f"Sign in to AIGCCloud: {nonce}"
    }


# 带密码的签名验证逻辑
async def _verify_web3_signature(request: Web3LoginRequest):
    """
    Web3 签名验证逻辑 (带密码的前端接口)
    返回: (address, username)
    """
    # 1. 标准化地址
    address = validate_eth_address(request.address)
    
    # 2. 从 message 中提取 nonce
    # 消息格式: "Sign in to AIGCCloud: {nonce}"
    nonce_from_message = None
    if ": " in request.message:
        nonce_from_message = request.message.split(": ")[-1].strip()
    
    # 3. 尝试新格式验证: nonce:{nonce} -> address
    used_key = None
    is_new_format = False
    
    if nonce_from_message:
        new_key = f"nonce:{nonce_from_message}"
        stored_address = await redis_client.get(new_key)
        if stored_address:
            # 新格式：验证地址匹配
            if stored_address.lower() != address.lower():
                logger.warning(f"[Web3] Nonce地址不匹配: {address[:10]}...")
                raise HTTPException(status_code=400, detail="无效的签名消息")
            used_key = new_key
            is_new_format = True
    
    # 4. 尝试旧格式验证: web3_nonce:{address} -> nonce
    if not used_key:
        old_key = f"web3_nonce:{address.lower()}"
        stored_nonce = await redis_client.get(old_key)
        if stored_nonce:
            # 旧格式：验证 message 包含 nonce
            if stored_nonce not in request.message:
                raise HTTPException(status_code=400, detail="无效的签名消息")
            used_key = old_key
    
    if not used_key:
        logger.warning(f"[Web3] Nonce过期或不存在: {address[:10]}...")
        raise HTTPException(status_code=400, detail="验证已过期，请重新获取")
    
    # 5. 验证签名
    recovered = verify_signature(request.message, request.signature, address)
    if not recovered or recovered.lower() != address.lower():
        logger.warning(f"[Web3] 签名验证失败: {address[:10]}...")
        raise HTTPException(status_code=400, detail="签名验证失败")
    
    # 6. 删除 nonce（一次性使用，防止重放攻击）
    await redis_client.delete(used_key)
    
    # 7. 验证密码
    if not request.password:
        raise HTTPException(status_code=400, detail="请输入登录密码")
    if len(request.password) < 6:
        raise HTTPException(status_code=400, detail="登录密码至少6位")
    
    username = address.lower()
    return address, username

async def _update_last_login(user_id: str, session_token: str, address: str):
    """更新最后登录时间（使用 session token）"""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            await client.put(
                f"{settings.parse_server_url}/users/{user_id}",
                json={"lastLoginAt": datetime.now().isoformat()},
                headers={
                    "X-Parse-Application-Id": settings.parse_app_id,
                    "X-Parse-REST-API-Key": settings.parse_rest_api_key,
                    "X-Parse-Session-Token": session_token,
                    "Content-Type": "application/json",
                },
                timeout=10.0
            )
        logger.debug(f"[Web3] 更新登录时间成功: {address[:10]}...")
    except Exception as e:
        logger.warning(f"[Web3] 更新登录时间失败: {e}")


def _build_user_response(user_data: dict, session_token: str, address: str):
    """构建用户响应数据"""
    user_id = user_data.get("objectId")
    safe_user = {
        "objectId": user_id,
        "sessionToken": session_token,
        "username": user_data.get("username"),
        "email": user_data.get("email"),
        "phone": user_data.get("phone"),
        "role": user_data.get("role", "user"),
        "level": user_data.get("level", 1),
        "memberLevel": user_data.get("memberLevel", "normal"),
        "coins": user_data.get("coins", 0),
        "avatar": user_data.get("avatar"),
        "avatarKey": user_data.get("avatarKey"),
        "web3Address": address,
        "inviteCount": user_data.get("inviteCount", 0),
    }
    # Parse 配置 - 登录后动态下发，客户端无需静态配置
    parse_config = {
        "serverUrl": settings.parse_server_url,
        "appId": settings.parse_app_id,        # X-Parse-Application-Id
        "jsKey": settings.parse_js_key,         # X-Parse-Javascript-Key
    }
    return safe_user, parse_config


@router.post("/web3/register")
async def web3_register(request: Web3LoginRequest):
    """
    Web3 注册 - 验证签名并创建新用户
    
    流程：
    1. 验证签名
    2. 创建 Parse User
    3. 返回 session token
    """
    import httpx
    
    logger.info(f"[Web3] 注册请求: {request.address[:10]}...")
    
    # 验证签名
    address, username = await _verify_web3_signature(request)
    
    # 创建新用户
    try:
        create_result = await parse_client.create_user({
            "username": username,
            "password": request.password,
            "web3Address": address,
            "role": "user",
            "level": 1,
            "coins": 100,  # 新用户赠送 100 金币
            "memberLevel": "normal",
        })
        
        if not create_result.get("objectId"):
            raise HTTPException(status_code=500, detail="创建用户失败")
        
        user_data = create_result
        session_token = create_result.get("sessionToken")
        user_id = create_result.get("objectId")
        
        logger.info(f"[Web3] 注册成功: {address[:10]}... (ID: {user_id})")
        
    except httpx.HTTPStatusError as e:
        error_data = e.response.json() if e.response.headers.get("content-type", "").startswith("application/json") else {}
        if error_data.get("code") == 202:  # 用户已存在
            logger.warning(f"[Web3] 用户已存在: {address[:10]}...")
            raise HTTPException(status_code=400, detail="该地址已注册，请直接登录")
        raise
    
    # 更新登录时间
    if session_token and user_id:
        await _update_last_login(user_id, session_token, address)
    
    # 生成 JWT（包含 session_token）
    jwt_token = create_access_token(data={
        "user_id": user_id,
        "address": address,
        "session_token": session_token,
    })
    
    # 构建响应
    safe_user, parse_config = _build_user_response(user_data, session_token, address)
    
    return {
        "success": True,
        "token": jwt_token,  # 返回 JWT
        "user": safe_user,
        "parse_config": parse_config,
        "is_new_user": True,
        "message": "注册成功"
    }


@router.post("/web3/login")
async def web3_login(request: Web3LoginRequest):
    """
    Web3 登录 - 验证签名并登录已有用户
    
    流程：
    1. 验证签名
    2. 登录 Parse User
    3. 返回 session token
    """
    import httpx
    
    logger.info(f"[Web3] 登录请求: {request.address[:10]}...")
    
    # 验证签名
    address, username = await _verify_web3_signature(request)
    
    # 登录 Parse
    login_url = f"{settings.parse_server_url}/login"
    login_headers = {
        "X-Parse-Application-Id": settings.parse_app_id,
        "X-Parse-REST-API-Key": settings.parse_rest_api_key,
        "X-Parse-Revocable-Session": "1",
    }
    
    logger.debug(f"[Web3] 登录Parse: URL={login_url}, username={username}")
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            login_url,
            params={"username": username, "password": request.password},
            headers=login_headers,
            timeout=30.0
        )
    
    logger.debug(f"[Web3] 登录响应: status={response.status_code}")
    
    if response.status_code == 200:
        user_data = response.json()
        session_token = user_data.get("sessionToken")
        user_id = user_data.get("objectId")
        
        logger.info(f"[Web3] 登录成功: {address[:10]}... (ID: {user_id})")
        
        # 更新登录时间
        if session_token and user_id:
            await _update_last_login(user_id, session_token, address)
        
        # 生成 JWT（包含 session_token）
        jwt_token = create_access_token(data={
            "user_id": user_id,
            "address": address,
            "session_token": session_token,
        })
        
        # 构建响应
        safe_user, parse_config = _build_user_response(user_data, session_token, address)
        
        return {
            "success": True,
            "token": jwt_token,  # 返回 JWT
            "user": safe_user,
            "parse_config": parse_config,
            "is_new_user": False,
            "message": "登录成功"
        }
    else:
        # 解析 Parse Server 错误信息
        try:
            error_data = response.json()
            error_code = error_data.get("code")
            error_msg = error_data.get("error", "登录失败")
            
            logger.warning(f"[Web3] 登录失败: {address[:10]}... - code={error_code}, error={error_msg}")
            
            # Parse 错误码：101=用户名密码错误
            if error_code == 101:
                raise HTTPException(status_code=401, detail="该地址未注册或密码错误")
            else:
                raise HTTPException(status_code=401, detail=error_msg)
        except ValueError:
            # 无法解析 JSON
            error_text = response.text
            logger.warning(f"[Web3] 登录失败: {address[:10]}... - status={response.status_code}, error={error_text}")
            raise HTTPException(status_code=401, detail="登录失败，请检查账户和密码")



@router.post("/web3/logout")
async def web3_logout(
    authorization: Optional[str] = Header(None)
):
    """
    Web3 登出 - 清除 JWT 和 Parse sessionToken
    
    流程：
    1. 验证 JWT ，获取 session_token
    2. 调用 Parse Server 撤销 sessionToken
    3. 客户端清除本地存储
    """
    import httpx
    
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证Token")
    
    token = authorization[7:]
    
    try:
        # 解析 JWT 获取 session_token
        payload = verify_jwt_token(token)
        session_token = payload.get("session_token")
        
        if not session_token:
            logger.warning("[Web3] 登出失败: JWT 中未找到 session_token")
            raise HTTPException(status_code=400, detail="无效的认证Token")
        
        # 调用 Parse Server 登出接口撤销 sessionToken
        logout_url = f"{settings.parse_server_url}/logout"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                logout_url,
                headers={
                    "X-Parse-Application-Id": settings.parse_app_id,
                    "X-Parse-REST-API-Key": settings.parse_rest_api_key,
                    "X-Parse-Session-Token": session_token,
                },
                timeout=10.0
            )
        
        if response.status_code == 200:
            logger.info(f"[Web3] 登出成功: session_token={session_token[:20]}...")
        else:
            logger.warning(f"[Web3] Parse 登出失败: status={response.status_code}")
        
        # 可选：将 JWT 加入黑名单
        # await redis_client.set(f"jwt_blacklist:{token}", "1", ex=86400)
        
        return {
            "success": True,
            "message": "登出成功"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Web3] 登出异常: {e}")
        raise HTTPException(status_code=500, detail="登出失败")
