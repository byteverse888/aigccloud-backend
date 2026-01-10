"""
依赖注入
"""
from typing import Optional
from fastapi import Header, HTTPException, status
from app.core.security import verify_jwt_token
from app.core.parse_client import parse_client


async def get_current_user_id(
    authorization: Optional[str] = Header(None, alias="Authorization")
) -> str:
    """从Authorization Header获取当前用户ID"""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header"
        )
    
    # Bearer token
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = authorization
    
    user_id = verify_jwt_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )
    
    return user_id


async def get_optional_user_id(
    authorization: Optional[str] = Header(None, alias="Authorization")
) -> Optional[str]:
    """可选的用户ID获取"""
    if not authorization:
        return None
    
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = authorization
    
    return verify_jwt_token(token)


async def verify_admin_user(
    user_id: str
) -> bool:
    """验证是否为管理员用户"""
    try:
        user = await parse_client.get_user(user_id)
        return user.get("role") == "admin"
    except Exception:
        return False


async def get_admin_user_id(
    authorization: Optional[str] = Header(None, alias="Authorization")
) -> str:
    """获取并验证管理员用户ID"""
    user_id = await get_current_user_id(authorization)
    
    if not await verify_admin_user(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required"
        )
    
    return user_id
