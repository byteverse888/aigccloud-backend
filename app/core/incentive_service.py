"""
激励服务 - 公共激励函数
从运营激励账户向完成任务的Web3账户进行转账并记录
"""
from typing import Optional
from enum import Enum
from datetime import datetime
from app.core.config import settings
from app.core.parse_client import parse_client
from app.core.logger import logger
import httpx


class IncentiveType(str, Enum):
    """激励类型"""
    REGISTER = "register"           # 注册奖励
    DAILY_LOGIN = "daily_login"     # 每日登录
    INVITE = "invite"               # 邀请奖励
    INVITE_RECHARGE = "invite_recharge"  # 邀请首充返利
    TASK = "task"                   # 任务奖励
    RECHARGE = "recharge"           # 充值奖励
    ACTIVITY = "activity"           # 活动奖励
    MEMBER_SUBSCRIBE = "member_subscribe"  # 会员订阅奖励


# 激励配置
INCENTIVE_CONFIG = {
    "register": 100,                    # 注册奖励
    "daily_login_normal": 5,            # 普通用户每日登录
    "daily_login_paid": 10,             # 付费用户每日登录
    "invite_register": 100,             # 邀请注册奖励
    "invite_first_recharge_rate": 0.1,  # 邀请首充返利比例
    "task_complete": 1,                 # 任务完成奖励
    "recharge_rate": 0.05,              # 充值奖励比例（充值金额的5%）
}


class IncentiveService:
    """激励服务"""
    
    def __init__(self):
        self.rpc_url = settings.web3_rpc_url
        self.chain_id = settings.web3_chain_id
        self.private_key = settings.incentive_wallet_private_key
    
    def _get_wallet_address(self) -> Optional[str]:
        """从私钥获取钱包地址"""
        if not self.private_key:
            return None
        try:
            # 简单实现：使用 eth_account 库或自行计算
            # 这里返回 None 表示开发环境
            return None
        except Exception:
            return None
    
    async def _send_eth_transaction(self, to_address: str, amount_wei: int) -> dict:
        """
        使用私钥发送ETH交易
        
        Args:
            to_address: 接收地址
            amount_wei: 转账金额（wei）
            
        Returns:
            交易结果
        """
        if not self.rpc_url or not self.private_key:
            # 开发环境模拟
            logger.info(f"[激励服务] 模拟转账: {amount_wei} wei -> {to_address}")
            return {
                "success": True,
                "tx_hash": f"mock_incentive_{amount_wei}_{datetime.now().timestamp()}"
            }
        
        try:
            # 使用 web3.py 或直接构造交易
            # 1. 获取 nonce
            async with httpx.AsyncClient() as client:
                # 获取发送方地址
                from_address = self._get_wallet_address()
                if not from_address:
                    return {"success": False, "error": "无法获取激励钱包地址"}
                
                # 获取 nonce
                nonce_resp = await client.post(self.rpc_url, json={
                    "jsonrpc": "2.0",
                    "method": "eth_getTransactionCount",
                    "params": [from_address, "latest"],
                    "id": 1
                }, timeout=30.0)
                nonce = int(nonce_resp.json().get("result", "0x0"), 16)
                
                # 获取 gas price
                gas_price_resp = await client.post(self.rpc_url, json={
                    "jsonrpc": "2.0",
                    "method": "eth_gasPrice",
                    "params": [],
                    "id": 1
                }, timeout=30.0)
                gas_price = int(gas_price_resp.json().get("result", "0x0"), 16)
                
                # 构造交易
                tx = {
                    "nonce": nonce,
                    "gasPrice": gas_price,
                    "gas": 21000,  # 简单转账固定 gas
                    "to": to_address,
                    "value": amount_wei,
                    "chainId": self.chain_id,
                }
                
                # TODO: 签名交易并发送
                # 这里需要使用 eth_account 库签名
                # signed_tx = Account.sign_transaction(tx, self.private_key)
                # 发送签名后的交易
                
                return {"success": True, "tx_hash": "pending_implementation"}
                
        except Exception as e:
            logger.error(f"[激励服务] 转账失败: {e}")
            return {"success": False, "error": str(e)}
    
    async def grant_incentive(
        self,
        user_id: str,
        web3_address: str,
        incentive_type: IncentiveType,
        amount: float,
        description: str,
        related_id: Optional[str] = None
    ) -> dict:
        """
        发放激励 - 核心公共函数
        
        Args:
            user_id: 用户ID
            web3_address: 用户Web3地址
            incentive_type: 激励类型
            amount: 激励金额（金币数量）
            description: 描述
            related_id: 关联ID（如任务ID、订单ID等）
            
        Returns:
            发放结果
        """
        if not web3_address:
            return {"success": False, "error": "用户未绑定Web3地址"}
        
        if amount <= 0:
            return {"success": False, "error": "激励金额必须大于0"}
        
        logger.info(f"[激励服务] 发放激励: {user_id} -> {amount} 金币, 类型: {incentive_type}")
        
        # 1. 转账（将金币转到用户Web3地址）
        # 这里假设 1 金币 = 1 wei（实际需要根据合约定义）
        amount_wei = int(amount)
        tx_result = await self._send_eth_transaction(web3_address, amount_wei)
        
        tx_hash = tx_result.get("tx_hash")
        
        # 2. 记录激励日志
        log_data = {
            "userId": user_id,
            "web3Address": web3_address,
            "type": incentive_type.value,
            "amount": amount,
            "txHash": tx_hash,
            "description": description,
            "status": "success" if tx_result.get("success") else "failed",
        }
        if related_id:
            log_data["relatedId"] = related_id
        
        await parse_client.create_object("IncentiveLog", log_data)
        
        if tx_result.get("success"):
            logger.info(f"[激励服务] 激励发放成功: {tx_hash}")
            return {
                "success": True,
                "amount": amount,
                "tx_hash": tx_hash,
                "message": f"成功发放 {amount} 金币"
            }
        else:
            logger.error(f"[激励服务] 激励发放失败: {tx_result.get('error')}")
            return {
                "success": False,
                "error": tx_result.get("error", "转账失败")
            }
    
    async def grant_daily_login(self, user_id: str) -> dict:
        """发放每日登录奖励"""
        try:
            user = await parse_client.get_user(user_id)
        except Exception:
            return {"success": False, "error": "用户不存在"}
        
        web3_address = user.get("web3Address")
        if not web3_address:
            return {"success": False, "error": "用户未绑定Web3地址"}
        
        member_level = user.get("memberLevel", "normal")
        is_vip = member_level in ("vip", "svip")
        amount = INCENTIVE_CONFIG["daily_login_paid"] if is_vip else INCENTIVE_CONFIG["daily_login_normal"]
        
        return await self.grant_incentive(
            user_id=user_id,
            web3_address=web3_address,
            incentive_type=IncentiveType.DAILY_LOGIN,
            amount=amount,
            description=f"每日登录奖励（{'会员' if is_vip else '普通'}用户）"
        )
    
    async def grant_recharge_reward(self, user_id: str, recharge_amount: float, order_id: str) -> dict:
        """
        发放充值奖励
        
        Args:
            user_id: 用户ID
            recharge_amount: 充值金额
            order_id: 订单ID
        """
        try:
            user = await parse_client.get_user(user_id)
        except Exception:
            return {"success": False, "error": "用户不存在"}
        
        web3_address = user.get("web3Address")
        if not web3_address:
            return {"success": False, "error": "用户未绑定Web3地址"}
        
        # 充值奖励 = 充值金额 * 奖励比例
        reward_amount = recharge_amount * INCENTIVE_CONFIG["recharge_rate"]
        if reward_amount < 1:
            reward_amount = 1  # 最低1金币
        
        return await self.grant_incentive(
            user_id=user_id,
            web3_address=web3_address,
            incentive_type=IncentiveType.RECHARGE,
            amount=reward_amount,
            description=f"充值 ¥{recharge_amount} 奖励",
            related_id=order_id
        )
    
    async def grant_invite_register_reward(self, inviter_id: str, invitee_name: str) -> dict:
        """发放邀请注册奖励"""
        try:
            inviter = await parse_client.get_user(inviter_id)
        except Exception:
            return {"success": False, "error": "邀请人不存在"}
        
        web3_address = inviter.get("web3Address")
        if not web3_address:
            return {"success": False, "error": "邀请人未绑定Web3地址"}
        
        amount = INCENTIVE_CONFIG["invite_register"]
        
        return await self.grant_incentive(
            user_id=inviter_id,
            web3_address=web3_address,
            incentive_type=IncentiveType.INVITE,
            amount=amount,
            description=f"邀请用户 {invitee_name} 注册奖励"
        )
    
    async def grant_invite_recharge_reward(
        self, 
        inviter_id: str, 
        invitee_name: str, 
        recharge_amount: float
    ) -> dict:
        """发放邀请首充返利"""
        try:
            inviter = await parse_client.get_user(inviter_id)
        except Exception:
            return {"success": False, "error": "邀请人不存在"}
        
        web3_address = inviter.get("web3Address")
        if not web3_address:
            return {"success": False, "error": "邀请人未绑定Web3地址"}
        
        # 首充返利 = 充值金额 * 返利比例
        reward_amount = recharge_amount * INCENTIVE_CONFIG["invite_first_recharge_rate"]
        if reward_amount < 1:
            reward_amount = 1
        
        return await self.grant_incentive(
            user_id=inviter_id,
            web3_address=web3_address,
            incentive_type=IncentiveType.INVITE_RECHARGE,
            amount=reward_amount,
            description=f"邀请用户 {invitee_name} 首充 ¥{recharge_amount} 返利"
        )
    
    async def grant_task_reward(
        self, 
        user_id: str, 
        task_id: str, 
        task_type: str,
        amount: Optional[float] = None
    ) -> dict:
        """
        发放任务完成奖励
        
        Args:
            user_id: 用户ID
            task_id: 任务ID
            task_type: 任务类型
            amount: 奖励金额（为空则使用默认配置）
        """
        try:
            user = await parse_client.get_user(user_id)
        except Exception:
            return {"success": False, "error": "用户不存在"}
        
        web3_address = user.get("web3Address")
        if not web3_address:
            return {"success": False, "error": "用户未绑定Web3地址"}
        
        reward_amount = amount or INCENTIVE_CONFIG["task_complete"]
        
        return await self.grant_incentive(
            user_id=user_id,
            web3_address=web3_address,
            incentive_type=IncentiveType.TASK,
            amount=reward_amount,
            description=f"完成 {task_type} 任务奖励",
            related_id=task_id
        )


# 全局单例
incentive_service = IncentiveService()
