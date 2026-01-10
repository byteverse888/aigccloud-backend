"""
Parse Server REST API 客户端
"""
import httpx
from typing import Optional, Dict, Any, List
from app.core.config import settings


class ParseClient:
    """Parse Server REST API 客户端"""
    
    def __init__(self):
        self.base_url = settings.parse_server_url
        self.app_id = settings.parse_app_id
        self.rest_api_key = settings.parse_rest_api_key
        self.headers = {
            "X-Parse-Application-Id": self.app_id,
            "X-Parse-REST-API-Key": self.rest_api_key,
            "Content-Type": "application/json",
        }
    
    async def _request(
        self, 
        method: str, 
        endpoint: str, 
        data: Optional[Dict] = None,
        params: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """发送请求到 Parse Server"""
        url = f"{self.base_url}{endpoint}"
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=url,
                headers=self.headers,
                json=data,
                params=params,
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()
    
    # ============ 对象操作 ============
    
    async def create_object(self, class_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建对象"""
        return await self._request("POST", f"/classes/{class_name}", data)
    
    async def get_object(self, class_name: str, object_id: str) -> Dict[str, Any]:
        """获取单个对象"""
        return await self._request("GET", f"/classes/{class_name}/{object_id}")
    
    async def update_object(self, class_name: str, object_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """更新对象"""
        return await self._request("PUT", f"/classes/{class_name}/{object_id}", data)
    
    async def delete_object(self, class_name: str, object_id: str) -> Dict[str, Any]:
        """删除对象"""
        return await self._request("DELETE", f"/classes/{class_name}/{object_id}")
    
    async def query_objects(
        self, 
        class_name: str, 
        where: Optional[Dict] = None,
        order: Optional[str] = None,
        limit: int = 100,
        skip: int = 0,
        count: bool = False,
        include: Optional[str] = None
    ) -> Dict[str, Any]:
        """查询对象列表"""
        import json
        params = {"limit": limit, "skip": skip}
        if where:
            params["where"] = json.dumps(where)
        if order:
            params["order"] = order
        if count:
            params["count"] = "1"
        if include:
            params["include"] = include
        return await self._request("GET", f"/classes/{class_name}", params=params)
    
    async def count_objects(self, class_name: str, where: Optional[Dict] = None) -> int:
        """统计对象数量"""
        import json
        params = {"count": "1", "limit": "0"}
        if where:
            params["where"] = json.dumps(where)
        result = await self._request("GET", f"/classes/{class_name}", params=params)
        return result.get("count", 0)
    
    async def batch_operations(self, requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量操作"""
        return await self._request("POST", "/batch", {"requests": requests})
    
    # ============ 用户操作 ============
    
    async def create_user(self, username: str, email: str, password: str, extra_data: Optional[Dict] = None) -> Dict[str, Any]:
        """创建用户"""
        data = {
            "username": username,
            "email": email,
            "password": password,
        }
        if extra_data:
            data.update(extra_data)
        return await self._request("POST", "/users", data)
    
    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """获取用户信息"""
        return await self._request("GET", f"/users/{user_id}")
    
    async def update_user(self, user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """更新用户信息"""
        return await self._request("PUT", f"/users/{user_id}", data)
    
    async def query_users(
        self, 
        where: Optional[Dict] = None,
        order: Optional[str] = None,
        limit: int = 100,
        skip: int = 0
    ) -> Dict[str, Any]:
        """查询用户列表"""
        import json
        params = {"limit": limit, "skip": skip}
        if where:
            params["where"] = json.dumps(where)
        if order:
            params["order"] = order
        return await self._request("GET", "/users", params=params)
    
    # ============ 云函数调用 ============
    
    async def call_function(self, name: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """调用云函数"""
        return await self._request("POST", f"/functions/{name}", data or {})
    
    # ============ 辅助方法 ============
    
    @staticmethod
    def pointer(class_name: str, object_id: str) -> Dict[str, str]:
        """创建指针引用"""
        return {
            "__type": "Pointer",
            "className": class_name,
            "objectId": object_id
        }
    
    @staticmethod
    def increment(amount: int = 1) -> Dict[str, Any]:
        """创建自增操作"""
        return {
            "__op": "Increment",
            "amount": amount
        }
    
    @staticmethod
    def add_relation(objects: List[Dict]) -> Dict[str, Any]:
        """创建添加关系操作"""
        return {
            "__op": "AddRelation",
            "objects": objects
        }
    
    @staticmethod
    def remove_relation(objects: List[Dict]) -> Dict[str, Any]:
        """创建移除关系操作"""
        return {
            "__op": "RemoveRelation",
            "objects": objects
        }


# 全局单例
parse_client = ParseClient()
