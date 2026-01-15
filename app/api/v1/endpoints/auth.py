"""
认证端点 - 处理登录和Parse配置下发
"""
import uuid
import json
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


# ============ 邮箱认证 ============

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


async def send_activation_email(email: str, token: str):
    """发送邮箱激活链接"""
    if not settings.smtp_host or not settings.smtp_user:
        logger.warning("[Email] SMTP未配置，跳过发送邮件")
        logger.info(f"[Email] 激活链接: http://localhost:3000/activate?token={token}")
        return
    
    try:
        msg = MIMEMultipart()
        msg['From'] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
        msg['To'] = email
        msg['Subject'] = "欢迎注册巴特星球 - 请激活您的账户"
        
        # 激活链接（前端域名应该从配置读取）
        activation_url = f"http://localhost:3000/activate?token={token}"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>欢迎来到巴特星球！</h2>
            <p>感谢您注册我们的平台。请点击下面的按钮激活您的账户：</p>
            <p style="margin: 30px 0;">
                <a href="{activation_url}" 
                   style="background-color: #4CAF50; color: white; padding: 14px 28px; 
                          text-decoration: none; border-radius: 4px; display: inline-block;">
                    激活账户
                </a>
            </p>
            <p>或复制以下链接到浏览器：</p>
            <p style="color: #666;">{activation_url}</p>
            <p style="margin-top: 40px; color: #999; font-size: 12px;">
                此链接在24小时内有效。如果您没有注册该账户，请忽略此邮件。
            </p>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        # 发送邮件
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        
        logger.info(f"[Email] 激活邮件已发送: {email}")
        
    except Exception as e:
        logger.error(f"[Email] 发送邮件失败: {email} - {str(e)}")
        raise HTTPException(status_code=500, detail="发送激活邮件失败")


@router.post("/email/register")
async def email_register(request: EmailRegisterRequest):
    """
    邮箱注册
    
    流程：
    1. 验证邮箱格式
    2. 检查邮箱是否已注册
    3. 创建 Parse User (emailVerified=false)
    4. 生成激活 token 并存入 Redis
    5. 发送激活邮件
    """
    logger.info(f"[Email] 注册请求: {request.email}")
    
    # 1. 验证邮箱格式
    import re
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(email_regex, request.email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    
    # 2. 检查邮箱是否已注册
    existing = await parse_client.query_users(where={"email": request.email})
    if existing.get("results"):
        raise HTTPException(status_code=400, detail="该邮箱已注册")
    
    # 3. 创建 Parse User
    try:
        create_result = await parse_client.create_user({
            "username": request.email,  # 使用邮箱作为用户名
            "email": request.email,
            "password": request.password,
            # emailVerified 由 Parse Server 自动管理，不能手动设置
            "role": "user",
            "level": 1,
            "coins": 100,
            "memberLevel": "normal",
        })
        
        if not create_result.get("objectId"):
            raise HTTPException(status_code=500, detail="创建用户失败")
        
        user_id = create_result.get("objectId")
        logger.info(f"[Email] 用户创建成功: {request.email} (ID: {user_id})")
        
    except Exception as e:
        logger.error(f"[Email] 创建用户失败: {str(e)}")
        raise HTTPException(status_code=500, detail="注册失败")
    
    # 4. 生成激活 token
    activation_token = secrets.token_urlsafe(32)
    token_key = f"activation:{activation_token}"
    # 存储: token -> user_id + email，24小时有效
    await redis_client.set(token_key, f"{user_id}:{request.email}", ex=86400)
    
    logger.info(f"[Email] 激活 token 已生成: {user_id} -> {activation_token[:16]}...")
    
    # 5. 发送激活邮件
    await send_activation_email(request.email, activation_token)
    
    return {
        "success": True,
        "message": "注册成功，请查收激活邮件",
        "email": request.email,
    }


@router.get("/email/activate")
async def email_activate(token: str):
    """
    邮箱激活
    
    流程：
    1. 验证 token 是否有效
    2. 获取 user_id 和 email
    3. 更新 Parse User: emailVerified=true
    4. 删除 token
    """
    logger.info(f"[Email] 激活请求: {token[:16]}...")
    
    # 1. 验证 token
    token_key = f"activation:{token}"
    token_data = await redis_client.get(token_key)
    
    if not token_data:
        logger.warning(f"[Email] Token无效或已过期: {token[:16]}...")
        raise HTTPException(status_code=400, detail="激活链接无效或已过期")
    
    # 2. 解析 token 数据
    try:
        user_id, email = token_data.split(":")
    except:
        raise HTTPException(status_code=400, detail="无效的激活信息")
    
    logger.info(f"[Email] 开始激活: user_id={user_id}, email={email}")
    
    # 3. 更新 Parse User
    try:
        # 使用 Master Key 更新 emailVerified
        await parse_client.update_user_with_master_key(user_id, {
            "emailVerified": True,
        })
        logger.info(f"[Email] 用户信息已更新: {user_id} - emailVerified=True")
        
    except Exception as e:
        logger.error(f"[Email] 更新用户失败: {str(e)}")
        raise HTTPException(status_code=500, detail="激活失败")
    
    # 4. 删除 token
    await redis_client.delete(token_key)
    logger.info(f"[Email] 激活完成: {email}")
    
    return {
        "success": True,
        "message": "邮箱激活成功，请登录",
        "email": email,
    }

import secrets


@router.post("/email/login")
async def email_login(request: EmailLoginRequest):
    """
    邮箱登录
    
    流程：
    1. 验证邮箱+密码
    2. 检查 emailVerified
    3. 返回用户信息和 Parse 配置
    """
    import httpx
    
    logger.info(f"[Email] 登录请求: {request.email}")
    
    # 验证邮箱+密码
    login_url = f"{settings.parse_server_url}/login"
    login_headers = {
        "X-Parse-Application-Id": settings.parse_app_id,
        "X-Parse-REST-API-Key": settings.parse_rest_api_key,
        "X-Parse-Revocable-Session": "1",
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            login_url,
            params={"username": request.email, "password": request.password},
            headers=login_headers,
            timeout=30.0
        )
    
    if response.status_code != 200:
        logger.warning(f"[Email] 登录失败: {request.email}")
        
        # 登录失败，检查用户是否存在
        try:
            existing = await parse_client.query_users(where={"email": request.email})
            if not existing.get("results"):
                # 用户不存在
                raise HTTPException(status_code=404, detail="该邮箱未注册")
            else:
                # 用户存在，密码错误
                raise HTTPException(status_code=401, detail="密码错误")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[Email] 检查用户失败: {str(e)}")
            raise HTTPException(status_code=401, detail="邮箱或密码错误")
    
    user_data = response.json()
    
    # 检查邮箱是否已激活
    if not user_data.get("emailVerified"):
        logger.warning(f"[Email] 邮箱未激活: {request.email}")
        raise HTTPException(status_code=403, detail="请先激活邮箱")
    
    user_id = user_data.get("objectId")
    session_token = user_data.get("sessionToken")
    
    logger.info(f"[Email] 登录成功: {request.email} (ID: {user_id})")
    
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
    
    # 构建响应
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
        "web3Address": user_data.get("web3Address"),
        "inviteCount": user_data.get("inviteCount", 0),
    }
    
    return {
        "success": True,
        "token": session_token,
        "user": safe_user,
        "message": "登录成功",
    }

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


@router.post("/web3/nonce")
async def web3_get_nonce(request: Web3InitRequest):
    """
    申请 Nonce - Electron 客户端专用
    
    - 生成 128 位高强度随机数
    - 存入 Redis，15 分钟有效
    - 返回 nonce 和过期时间
    """
    logger.info(f"[Web3] 申请Nonce: {request.address[:10]}...")
    
    address = validate_eth_address(request.address)
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
    
    # 构建响应
    safe_user, parse_config = _build_user_response(user_data, session_token, address)
    
    return {
        "success": True,
        "token": session_token,
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
        
        # 构建响应
        safe_user, parse_config = _build_user_response(user_data, session_token, address)
        
        return {
            "success": True,
            "token": session_token,
            "user": safe_user,
            "parse_config": parse_config,
            "is_new_user": False,
            "message": "登录成功"
        }
    else:
        # Parse 错误码 101 不区分用户不存在还是密码错误
        logger.warning(f"[Web3] 登录失败: {address[:10]}...")
        raise HTTPException(status_code=401, detail="该地址未注册或密码不对，请确认")



@router.post("/web3/logout")
async def web3_logout(
    authorization: Optional[str] = Header(None)
):
    """
    Web3 登出 - 撤销 JWT
    
    客户端应该:
    1. 清除本地存储的 JWT
    2. 清除内存中的 Parse 密钥缓存
    
    服务端可选:
    - 将 JWT 加入黑名单 (Redis)
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        # 可选: 将 token 加入黑名单
        # await redis_client.set(f"jwt_blacklist:{token}", "1", ex=86400)
        logger.info(f"[Web3] 登出: token={token[:20]}...")
    
    return {
        "success": True,
        "message": "登出成功"
    }
