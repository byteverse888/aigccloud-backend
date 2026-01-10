"""
支付管理端点
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
from enum import Enum
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

from app.core.parse_client import parse_client
from app.core.web3_client import web3_client
from app.core.security import generate_order_no, generate_sign, verify_sign
from app.core.deps import get_current_user_id
from app.core.config import settings

router = APIRouter()


# ============ 枚举与模型 ============

class PaymentType(str, Enum):
    WECHAT = "wechat"
    ALIPAY = "alipay"


class SubscriptionPlan(str, Enum):
    TRIAL = "trial"
    MONTHLY = "monthly"
    HALFYEAR = "halfyear"
    YEARLY = "yearly"
    THREEYEAR = "threeyear"


class OrderType(str, Enum):
    SUBSCRIPTION = "subscription"  # 订阅会员
    RECHARGE = "recharge"  # 充值金币
    PURCHASE = "purchase"  # 购买商品


class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class CreateOrderRequest(BaseModel):
    user_id: str  # 用户ID（从前端传入）
    type: OrderType
    amount: float
    plan: Optional[SubscriptionPlan] = None
    product_id: Optional[str] = None
    payment_method: PaymentType = PaymentType.WECHAT


class CartItem(BaseModel):
    product_id: str
    name: str
    price: float
    quantity: int


class CreateCartOrderRequest(BaseModel):
    user_id: str
    items: list[CartItem]
    payment_method: PaymentType = PaymentType.WECHAT


class OrderResponse(BaseModel):
    order_id: str
    order_no: str
    amount: float
    status: str
    payment_url: Optional[str] = None
    qr_code: Optional[str] = None


# 订阅计划配置
SUBSCRIPTION_PLANS = {
    "trial": {
        "name": "试用会员",
        "price": 0.1,
        "days": 3,
        "description": "3天体验会员权益",
        "coins": 100,
    },
    "monthly": {
        "name": "月度会员",
        "price": 29,
        "days": 30,
        "description": "基础会员权益",
        "coins": 2900,
    },
    "halfyear": {
        "name": "半年会员",
        "price": 99,
        "days": 180,
        "description": "半年超值套餐",
        "coins": 9900,
    },
    "yearly": {
        "name": "一年会员",
        "price": 139,
        "days": 365,
        "description": "年度优惠套餐",
        "coins": 13900,
    },
    "threeyear": {
        "name": "三年会员",
        "price": 299,
        "days": 1095,
        "description": "三年长期套餐",
        "coins": 29900,
    },
}


# ============ 辅助函数 ============

def generate_sign(data: dict, api_key: str) -> str:
    """
    生成微信支付签名
    """
    import hashlib
    # 按key排序
    sorted_keys = sorted(data.keys())
    sign_str = "&".join([f"{k}={data[k]}" for k in sorted_keys if data[k]])
    sign_str += f"&key={api_key}"
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

async def create_wechat_prepay(order_no: str, amount: float, description: str, notify_url: str):
    """
    调用微信统一下单接口 (Native支付)
    """
    import hashlib
    import time
    import httpx
    
    # 检查配置
    if not settings.wechat_mch_id or not settings.wechat_api_key:
        # 开发环境返回模拟数据
        return {
            "prepay_id": f"wx_mock_{order_no}",
            "code_url": f"weixin://wxpay/bizpayurl?pr={order_no}",
            "mock": True
        }
    
    # 微信支付参数
    nonce_str = hashlib.md5(f"{order_no}{time.time()}".encode()).hexdigest()
    total_fee = int(amount * 100)  # 元转分
    
    # 构造请求数据
    request_data = {
        "appid": settings.wechat_app_id,
        "mch_id": settings.wechat_mch_id,
        "nonce_str": nonce_str,
        "body": description,
        "out_trade_no": order_no,
        "total_fee": total_fee,
        "spbill_create_ip": "127.0.0.1",
        "notify_url": notify_url,
        "trade_type": "NATIVE",  # 扫码支付
    }
    
    # 生成签名
    sign = generate_sign(request_data, settings.wechat_api_key)
    request_data["sign"] = sign
    
    # 转换为XML
    xml_data = dict_to_xml(request_data)
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.mch.weixin.qq.com/pay/unifiedorder",
                content=xml_data.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=30.0
            )
            
            result = xml_to_dict(response.text)
            
            if result.get("return_code") == "SUCCESS" and result.get("result_code") == "SUCCESS":
                return {
                    "prepay_id": result.get("prepay_id"),
                    "code_url": result.get("code_url"),  # 二维码链接
                }
            else:
                raise Exception(result.get("err_code_des") or result.get("return_msg") or "微信支付请求失败")
                
    except Exception as e:
        # 记录错误日志
        print(f"微信支付请求失败: {e}")
        raise


def dict_to_xml(data: dict) -> str:
    """字典转XML"""
    xml = ["<xml>"]
    for key, value in data.items():
        xml.append(f"<{key}><![CDATA[{value}]]></{key}>")
    xml.append("</xml>")
    return "".join(xml)


def xml_to_dict(xml_str: str) -> dict:
    """XML转字典"""
    root = ET.fromstring(xml_str)
    return {child.tag: child.text for child in root}


# ============ 端点 ============

@router.post("/create-order", response_model=OrderResponse)
async def create_order(request: CreateOrderRequest):
    """
    创建支付订单
    """
    user_id = request.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少用户ID")
    
    # 验证订阅计划
    if request.type == OrderType.SUBSCRIPTION:
        if not request.plan or request.plan not in SUBSCRIPTION_PLANS:
            raise HTTPException(status_code=400, detail="无效的订阅计划")
        plan = SUBSCRIPTION_PLANS[request.plan]
        amount = plan["price"]
        description = f"巴特星球-{plan['name']}"
    elif request.type == OrderType.PURCHASE and request.product_id:
        # 购买商品
        try:
            product = await parse_client.get_object("Product", request.product_id)
            amount = product.get("price", 0)
            description = f"购买商品-{product.get('name', '')}"
        except Exception:
            raise HTTPException(status_code=404, detail="商品不存在")
    else:
        amount = request.amount
        description = "巴特星球-金币充值"
    
    # 生成订单号
    order_no = generate_order_no()
    
    # 创建订单记录
    order_data = {
        "orderNo": order_no,
        "userId": user_id,
        "type": request.type,
        "amount": amount,
        "status": OrderStatus.PENDING,
        "paymentMethod": request.payment_method,
        "plan": request.plan,
        "productId": request.product_id,
        "description": description,
    }
    
    result = await parse_client.create_object("Order", order_data)
    order_id = result["objectId"]
    
    # 调用支付接口获取支付链接
    payment_url = None
    qr_code = None
    
    if request.payment_method == PaymentType.WECHAT:
        notify_url = settings.wechat_notify_url or f"{settings.parse_server_url}/api/v1/payment/callback/wechat"
        prepay = await create_wechat_prepay(order_no, amount, description, notify_url)
        qr_code = prepay.get("code_url")
        payment_url = f"weixin://pay?prepay_id={prepay.get('prepay_id')}"
    
    return OrderResponse(
        order_id=order_id,
        order_no=order_no,
        amount=amount,
        status=OrderStatus.PENDING,
        payment_url=payment_url,
        qr_code=qr_code,
    )


@router.post("/create-cart-order", response_model=OrderResponse)
async def create_cart_order(request: CreateCartOrderRequest):
    """
    创建购物车批量订单
    """
    user_id = request.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少用户ID")
    
    if not request.items:
        raise HTTPException(status_code=400, detail="购物车为空")
    
    # 计算总金额
    total_amount = sum(item.price * item.quantity for item in request.items)
    
    # 生成订单号
    order_no = generate_order_no()
    
    # 创建订单记录
    order_data = {
        "orderNo": order_no,
        "userId": user_id,
        "type": OrderType.PURCHASE,
        "amount": total_amount,
        "status": OrderStatus.PENDING,
        "paymentMethod": request.payment_method,
        "items": [item.dict() for item in request.items],
        "description": f"购物车商品 ({len(request.items)}件)",
    }
    
    result = await parse_client.create_object("Order", order_data)
    order_id = result["objectId"]
    
    # 调用支付接口获取支付链接
    payment_url = None
    qr_code = None
    
    if request.payment_method == PaymentType.WECHAT:
        notify_url = settings.wechat_notify_url or f"{settings.parse_server_url}/api/v1/payment/callback/wechat"
        prepay = await create_wechat_prepay(order_no, total_amount, f"购物车商品", notify_url)
        qr_code = prepay.get("code_url")
        payment_url = f"weixin://pay?prepay_id={prepay.get('prepay_id')}"
    
    return OrderResponse(
        order_id=order_id,
        order_no=order_no,
        amount=total_amount,
        status=OrderStatus.PENDING,
        payment_url=payment_url,
        qr_code=qr_code,
    )


@router.get("/order/{order_id}")
async def get_order(order_id: str, user_id: str = Depends(get_current_user_id)):
    """
    查询订单状态
    """
    try:
        order = await parse_client.get_object("Order", order_id)
    except Exception:
        raise HTTPException(status_code=404, detail="订单不存在")
    
    # 验证订单归属
    if order.get("userId") != user_id:
        raise HTTPException(status_code=403, detail="无权访问此订单")
    
    return {
        "order_id": order["objectId"],
        "order_no": order["orderNo"],
        "type": order["type"],
        "amount": order["amount"],
        "status": order["status"],
        "created_at": order["createdAt"],
        "paid_at": order.get("paidAt"),
    }


@router.get("/orders")
async def get_user_orders(
    page: int = 1,
    limit: int = 20,
    status: Optional[str] = None,
    user_id: str = Depends(get_current_user_id)
):
    """
    获取用户订单列表
    """
    where = {"userId": user_id}
    if status:
        where["status"] = status
    
    skip = (page - 1) * limit
    result = await parse_client.query_objects(
        "Order",
        where=where,
        order="-createdAt",
        limit=limit,
        skip=skip
    )
    
    total = await parse_client.count_objects("Order", where)
    
    return {
        "data": result.get("results", []),
        "total": total,
        "page": page,
        "limit": limit
    }


@router.post("/callback/wechat")
async def wechat_callback(request: Request):
    """
    微信支付回调
    """
    # 读取XML请求体
    body = await request.body()
    try:
        data = xml_to_dict(body.decode("utf-8"))
    except Exception:
        return dict_to_xml({"return_code": "FAIL", "return_msg": "XML解析失败"})
    
    # 验证签名
    sign = data.pop("sign", None)
    if not sign or not verify_sign(data, sign, settings.wechat_api_key):
        return dict_to_xml({"return_code": "FAIL", "return_msg": "签名验证失败"})
    
    # 检查支付结果
    if data.get("return_code") != "SUCCESS" or data.get("result_code") != "SUCCESS":
        return dict_to_xml({"return_code": "SUCCESS", "return_msg": "OK"})
    
    order_no = data.get("out_trade_no")
    
    # 查询订单
    orders = await parse_client.query_objects("Order", where={"orderNo": order_no})
    if not orders.get("results"):
        return dict_to_xml({"return_code": "SUCCESS", "return_msg": "订单不存在"})
    
    order = orders["results"][0]
    
    # 检查是否已处理(幂等性)
    if order.get("status") == OrderStatus.PAID:
        return dict_to_xml({"return_code": "SUCCESS", "return_msg": "OK"})
    
    # 更新订单状态
    await parse_client.update_object("Order", order["objectId"], {
        "status": OrderStatus.PAID,
        "paidAt": datetime.now().isoformat(),
        "transactionId": data.get("transaction_id"),
    })
    
    user_id = order["userId"]
    
    # 处理订阅
    if order["type"] == OrderType.SUBSCRIPTION and order.get("plan"):
        plan = SUBSCRIPTION_PLANS.get(order["plan"])
        if plan:
            # 获取用户当前会员到期时间
            user = await parse_client.get_user(user_id)
            current_expire = user.get("paidExpireAt")
            
            if current_expire:
                expire_date = datetime.fromisoformat(current_expire.replace("Z", "+00:00"))
                if expire_date > datetime.now(expire_date.tzinfo):
                    # 在当前到期时间基础上延长
                    new_expire = expire_date + timedelta(days=plan["days"])
                else:
                    new_expire = datetime.now() + timedelta(days=plan["days"])
            else:
                new_expire = datetime.now() + timedelta(days=plan["days"])
            
            # 更新用户会员状态
            await parse_client.update_user(user_id, {
                "isPaid": True,
                "paidExpireAt": new_expire.isoformat(),
            })
            
            # 通过Web3接口铸造金币到联盟链
            web3_address = user.get("web3Address")
            if web3_address and plan["coins"] > 0:
                mint_result = await web3_client.mint(web3_address, plan["coins"])
                # 记录充值日志
                await parse_client.create_object("IncentiveLog", {
                    "userId": user_id,
                    "web3Address": web3_address,
                    "type": "subscription",
                    "amount": plan["coins"],
                    "txHash": mint_result.get("tx_hash"),
                    "description": f"订阅{plan['name']}赠送金币"
                })
    
    # 处理充值
    elif order["type"] == OrderType.RECHARGE:
        coins = int(order["amount"] * 100)  # 1元=100金币
        user = await parse_client.get_user(user_id)
        web3_address = user.get("web3Address")
        
        if web3_address:
            # 通过Web3接口铸造金币到联盟链
            mint_result = await web3_client.mint(web3_address, coins)
            # 记录充值日志
            await parse_client.create_object("IncentiveLog", {
                "userId": user_id,
                "web3Address": web3_address,
                "type": "recharge",
                "amount": coins,
                "txHash": mint_result.get("tx_hash"),
                "description": f"充值{order['amount']}元获得{coins}金币"
            })
    
    # 处理商品购买（单商品或购物车）
    elif order["type"] == OrderType.PURCHASE:
        if order.get("items"):
            for item in order["items"]:
                await parse_client.create_object("Purchase", {
                    "userId": user_id,
                    "productId": item.get("product_id"),
                    "orderId": order["objectId"],
                    "name": item.get("name"),
                    "price": item.get("price"),
                    "quantity": item.get("quantity"),
                })
                if item.get("product_id"):
                    try:
                        await parse_client.update_object("Product", item["product_id"], {
                            "sales": parse_client.increment(item.get("quantity", 1))
                        })
                    except Exception:
                        pass
        elif order.get("productId"):
            await parse_client.create_object("Purchase", {
                "userId": user_id,
                "productId": order["productId"],
                "orderId": order["objectId"],
                "price": order["amount"],
            })
            await parse_client.update_object("Product", order["productId"], {
                "sales": parse_client.increment(1)
            })
    
    # 处理邀请首充奖励
    user = await parse_client.get_user(user_id)
    if user.get("inviterId") and not user.get("firstRechargeRewarded"):
        # 首次充值，给邀请人发放奖励
        reward = int(order["amount"] * 0.1)  # 10%首充奖励
        
        # 获取邀请人的Web3地址
        inviter = await parse_client.get_user(user["inviterId"])
        inviter_web3_address = inviter.get("web3Address")
        
        if inviter_web3_address:
            # 通过Web3接口铸造金币到邀请人联盟链地址
            mint_result = await web3_client.mint(inviter_web3_address, reward)
            # 记录奖励日志
            await parse_client.create_object("IncentiveLog", {
                "userId": user["inviterId"],
                "web3Address": inviter_web3_address,
                "type": "invite",
                "amount": reward,
                "txHash": mint_result.get("tx_hash"),
                "description": f"邀请用户 {user['username']} 首充奖励"
            })
        
        # 标记已发放
        await parse_client.update_user(user_id, {"firstRechargeRewarded": True})
    
    return dict_to_xml({"return_code": "SUCCESS", "return_msg": "OK"})


@router.get("/plans")
async def get_subscription_plans():
    """
    获取订阅计划列表
    """
    plans = []
    for key, plan in SUBSCRIPTION_PLANS.items():
        plans.append({
            "id": key,
            **plan
        })
    return {"plans": plans}


@router.post("/order/{order_id}/cancel")
async def cancel_order(order_id: str, user_id: str = Depends(get_current_user_id)):
    """
    取消订单
    """
    try:
        order = await parse_client.get_object("Order", order_id)
    except Exception:
        raise HTTPException(status_code=404, detail="订单不存在")
    
    if order.get("userId") != user_id:
        raise HTTPException(status_code=403, detail="无权操作此订单")
    
    if order.get("status") != OrderStatus.PENDING:
        raise HTTPException(status_code=400, detail="只有待支付订单可以取消")
    
    await parse_client.update_object("Order", order_id, {
        "status": OrderStatus.CANCELLED
    })
    
    return {"success": True, "message": "订单已取消"}


@router.post("/order/{order_id}/mock-pay")
async def mock_pay_order(order_id: str):
    """
    模拟支付成功（仅测试环境可用）
    用于测试支付流程，无需真实微信支付
    """
    if not settings.wechat_test_mode:
        raise HTTPException(status_code=403, detail="仅测试环境可用")
    
    # 获取订单
    try:
        order = await parse_client.get_object("Order", order_id)
    except Exception:
        raise HTTPException(status_code=404, detail="订单不存在")
    
    # 检查订单状态
    if order.get("status") != OrderStatus.PENDING:
        raise HTTPException(status_code=400, detail="订单已处理")
    
    # 更新订单状态
    await parse_client.update_object("Order", order_id, {
        "status": OrderStatus.PAID,
        "paidAt": datetime.now().isoformat(),
        "transactionId": f"mock_tx_{order_id}_{datetime.now().timestamp()}",
    })
    
    user_id = order["userId"]
    
    # 处理订阅
    if order["type"] == OrderType.SUBSCRIPTION and order.get("plan"):
        plan = SUBSCRIPTION_PLANS.get(order["plan"])
        if plan:
            user = await parse_client.get_user(user_id)
            current_expire = user.get("paidExpireAt")
            
            if current_expire:
                expire_date = datetime.fromisoformat(current_expire.replace("Z", "+00:00"))
                if expire_date > datetime.now(expire_date.tzinfo):
                    new_expire = expire_date + timedelta(days=plan["days"])
                else:
                    new_expire = datetime.now() + timedelta(days=plan["days"])
            else:
                new_expire = datetime.now() + timedelta(days=plan["days"])
            
            await parse_client.update_user(user_id, {
                "isPaid": True,
                "paidExpireAt": new_expire.isoformat(),
            })
            
            # 发放金币到联盟链
            web3_address = user.get("web3Address")
            if web3_address and plan["coins"] > 0:
                mint_result = await web3_client.mint(web3_address, plan["coins"])
                await parse_client.create_object("IncentiveLog", {
                    "userId": user_id,
                    "web3Address": web3_address,
                    "type": "subscription",
                    "amount": plan["coins"],
                    "txHash": mint_result.get("tx_hash"),
                    "description": f"订阅{plan['name']}赠送金币"
                })
    
    # 处理充值
    elif order["type"] == OrderType.RECHARGE:
        coins = int(order["amount"] * 100)
        user = await parse_client.get_user(user_id)
        web3_address = user.get("web3Address")
        
        if web3_address:
            mint_result = await web3_client.mint(web3_address, coins)
            await parse_client.create_object("IncentiveLog", {
                "userId": user_id,
                "web3Address": web3_address,
                "type": "recharge",
                "amount": coins,
                "txHash": mint_result.get("tx_hash"),
                "description": f"充值{order['amount']}元获得{coins}金币"
            })
    
    # 处理商品购买（单商品或购物车）
    elif order["type"] == OrderType.PURCHASE:
        if order.get("items"):
            for item in order["items"]:
                await parse_client.create_object("Purchase", {
                    "userId": user_id,
                    "productId": item.get("product_id"),
                    "orderId": order["objectId"],
                    "name": item.get("name"),
                    "price": item.get("price"),
                    "quantity": item.get("quantity"),
                })
                if item.get("product_id"):
                    try:
                        await parse_client.update_object("Product", item["product_id"], {
                            "sales": parse_client.increment(item.get("quantity", 1))
                        })
                    except Exception:
                        pass
        elif order.get("productId"):
            await parse_client.create_object("Purchase", {
                "userId": user_id,
                "productId": order["productId"],
                "orderId": order["objectId"],
                "price": order["amount"],
            })
            await parse_client.update_object("Product", order["productId"], {
                "sales": parse_client.increment(1)
            })
    
    # 处理邀请首充奖励
    user = await parse_client.get_user(user_id)
    if user.get("inviterId") and not user.get("firstRechargeRewarded"):
        reward = int(order["amount"] * 10)  # 10元=1000金币，奖励比例10%
        inviter = await parse_client.get_user(user["inviterId"])
        inviter_web3_address = inviter.get("web3Address")
        
        if inviter_web3_address:
            mint_result = await web3_client.mint(inviter_web3_address, reward)
            await parse_client.create_object("IncentiveLog", {
                "userId": user["inviterId"],
                "web3Address": inviter_web3_address,
                "type": "invite",
                "amount": reward,
                "txHash": mint_result.get("tx_hash"),
                "description": f"邀请用户 {user['username']} 首充奖励"
            })
        
        await parse_client.update_user(user_id, {"firstRechargeRewarded": True})
    
    return {
        "success": True,
        "message": "模拟支付成功",
        "order_id": order_id,
        "status": "paid"
    }
