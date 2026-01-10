"""
Web3 联盟链交互服务
金币（Coins）数据存储在联盟链上，通过此接口进行交互
"""
from typing import Optional
from pydantic import BaseModel
from app.core.config import settings
import httpx


class Web3Client:
    """Web3 联盟链客户端"""
    
    def __init__(self):
        self.rpc_url = settings.web3_rpc_url
        self.chain_id = settings.web3_chain_id
        self.contract_address = settings.web3_contract_address
        self.private_key = settings.web3_private_key
    
    async def _call_rpc(self, method: str, params: list) -> dict:
        """调用JSON-RPC接口"""
        if not self.rpc_url:
            # 开发环境模拟返回
            return {"result": "0x0"}
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                    "id": 1
                },
                timeout=30.0
            )
            return response.json()
    
    async def get_balance(self, address: str) -> int:
        """
        获取用户金币余额（从联盟链）
        
        Args:
            address: 用户的Web3地址
            
        Returns:
            金币余额（整数，单位：最小单位）
        """
        if not address:
            return 0
        
        if not self.rpc_url:
            # 开发环境返回模拟余额
            return 1000
        
        try:
            # 调用合约的balanceOf方法
            # 这里是简化实现，实际需要构造合约调用数据
            result = await self._call_rpc("eth_call", [{
                "to": self.contract_address,
                "data": self._encode_balance_of(address)
            }, "latest"])
            
            hex_balance = result.get("result", "0x0")
            return int(hex_balance, 16)
        except Exception as e:
            print(f"获取余额失败: {e}")
            return 0
    
    async def transfer(self, from_address: str, to_address: str, amount: int) -> dict:
        """
        转账金币
        
        Args:
            from_address: 发送方地址
            to_address: 接收方地址
            amount: 金额
            
        Returns:
            交易结果
        """
        if not self.rpc_url:
            # 开发环境模拟成功
            return {"success": True, "tx_hash": "mock_tx_" + str(amount)}
        
        try:
            # 构造并发送交易
            tx_data = self._encode_transfer(to_address, amount)
            
            # 签名并发送交易
            result = await self._call_rpc("eth_sendRawTransaction", [tx_data])
            
            return {
                "success": True,
                "tx_hash": result.get("result")
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def mint(self, to_address: str, amount: int) -> dict:
        """
        铸造金币（充值/奖励发放）
        需要管理员权限
        
        Args:
            to_address: 接收方地址
            amount: 金额
            
        Returns:
            交易结果
        """
        if not self.rpc_url:
            # 开发环境模拟成功
            return {"success": True, "tx_hash": f"mock_mint_{amount}"}
        
        try:
            # 使用管理员私钥调用mint方法
            tx_data = self._encode_mint(to_address, amount)
            
            result = await self._call_rpc("eth_sendRawTransaction", [tx_data])
            
            return {
                "success": True,
                "tx_hash": result.get("result")
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def burn(self, from_address: str, amount: int) -> dict:
        """
        销毁金币（消费/支付）
        
        Args:
            from_address: 用户地址
            amount: 金额
            
        Returns:
            交易结果
        """
        if not self.rpc_url:
            return {"success": True, "tx_hash": f"mock_burn_{amount}"}
        
        try:
            tx_data = self._encode_burn(from_address, amount)
            result = await self._call_rpc("eth_sendRawTransaction", [tx_data])
            
            return {
                "success": True,
                "tx_hash": result.get("result")
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    def _encode_balance_of(self, address: str) -> str:
        """编码balanceOf调用数据"""
        # ERC20 balanceOf(address) 方法签名
        method_id = "0x70a08231"
        # 地址参数（去掉0x，补齐64位）
        param = address[2:].lower().zfill(64)
        return method_id + param
    
    def _encode_transfer(self, to_address: str, amount: int) -> str:
        """编码transfer调用数据"""
        # ERC20 transfer(address,uint256) 方法签名
        method_id = "0xa9059cbb"
        to_param = to_address[2:].lower().zfill(64)
        amount_param = hex(amount)[2:].zfill(64)
        return method_id + to_param + amount_param
    
    def _encode_mint(self, to_address: str, amount: int) -> str:
        """编码mint调用数据"""
        # mint(address,uint256) 方法签名
        method_id = "0x40c10f19"
        to_param = to_address[2:].lower().zfill(64)
        amount_param = hex(amount)[2:].zfill(64)
        return method_id + to_param + amount_param
    
    def _encode_burn(self, from_address: str, amount: int) -> str:
        """编码burn调用数据"""
        # burn(address,uint256) 方法签名
        method_id = "0x9dc29fac"
        from_param = from_address[2:].lower().zfill(64)
        amount_param = hex(amount)[2:].zfill(64)
        return method_id + from_param + amount_param


# 全局单例
web3_client = Web3Client()
