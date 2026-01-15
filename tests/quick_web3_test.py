"""快速 Web3 认证测试 - 带密码模式"""
import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

BASE_URL = "http://localhost:8000/api/v1/auth"

# 生成测试钱包
account = Account.create()
wallet = {"address": account.address, "private_key": account.key.hex()}
password = "test123456"

print(f"测试钱包: {wallet['address']}")


def sign(private_key, message):
    """签名消息"""
    msg_hash = encode_defunct(text=message)
    signed = Account.sign_message(msg_hash, private_key=private_key)
    return signed.signature.hex()


with httpx.Client(timeout=30) as client:
    # 1. 获取 Nonce
    print("\n[1] 获取 Nonce...")
    r = client.post(f"{BASE_URL}/web3/nonce", json={"address": wallet["address"]})
    print(f"    Status: {r.status_code}")
    if r.status_code != 200:
        print(f"    ❌ Error: {r.text}")
        exit(1)
    nonce_data = r.json()
    print(f"    ✅ Nonce: {nonce_data['nonce'][:16]}...")
    print(f"    ✅ Message: {nonce_data['message'][:40]}...")

    # 2. Web3 注册（带密码）
    print("\n[2] Web3 注册（带密码）...")
    signature = sign(wallet["private_key"], nonce_data["message"])
    
    r = client.post(f"{BASE_URL}/web3/register", json={
        "address": wallet["address"],
        "signature": signature,
        "message": nonce_data["message"],
        "password": password,
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    ✅ 注册成功!")
        print(f"       User ID: {data['user'].get('objectId')}")
        print(f"       Token: {data['token'][:30]}...")
        jwt_token = data["token"]
    else:
        print(f"    ❌ {r.text}")
        exit(1)

    # 3. Web3 登录（带密码）
    print("\n[3] Web3 登录（带密码）...")
    # 需要获取新的 nonce
    r = client.post(f"{BASE_URL}/web3/nonce", json={"address": wallet["address"]})
    nonce_data = r.json()
    signature = sign(wallet["private_key"], nonce_data["message"])
    
    r = client.post(f"{BASE_URL}/web3/login", json={
        "address": wallet["address"],
        "signature": signature,
        "message": nonce_data["message"],
        "password": password,
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    ✅ 登录成功!")
        print(f"       Token: {data['token'][:30]}...")
    else:
        print(f"    ❌ {r.text}")

    # 4. 重复注册测试（应该失败）
    print("\n[4] 重复注册（应该失败）...")
    r = client.post(f"{BASE_URL}/web3/nonce", json={"address": wallet["address"]})
    nonce_data = r.json()
    signature = sign(wallet["private_key"], nonce_data["message"])
    
    r = client.post(f"{BASE_URL}/web3/register", json={
        "address": wallet["address"],
        "signature": signature,
        "message": nonce_data["message"],
        "password": password,
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 400:
        print(f"    ✅ 正确拒绝: {r.json().get('detail')}")
    else:
        print(f"    ❌ 未能阻止重复注册: {r.text}")

    # 5. 错误密码登录（应该失败）
    print("\n[5] 错误密码登录（应该失败）...")
    r = client.post(f"{BASE_URL}/web3/nonce", json={"address": wallet["address"]})
    nonce_data = r.json()
    signature = sign(wallet["private_key"], nonce_data["message"])
    
    r = client.post(f"{BASE_URL}/web3/login", json={
        "address": wallet["address"],
        "signature": signature,
        "message": nonce_data["message"],
        "password": "wrong_password",
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 401:
        print(f"    ✅ 正确拒绝: {r.json().get('detail')}")
    else:
        print(f"    ❌ 未能阻止错误密码: {r.text}")

    # 6. 无效签名测试（应该失败）
    print("\n[6] 无效签名（应该失败）...")
    r = client.post(f"{BASE_URL}/web3/nonce", json={"address": wallet["address"]})
    nonce_data = r.json()
    # 故意使用错误的签名
    fake_signature = "0x" + "00" * 65
    
    r = client.post(f"{BASE_URL}/web3/login", json={
        "address": wallet["address"],
        "signature": fake_signature,
        "message": nonce_data["message"],
        "password": password,
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 400:
        print(f"    ✅ 正确拒绝: {r.json().get('detail')}")
    else:
        print(f"    ❌ 未能阻止无效签名: {r.text}")

    # 7. 过期 Nonce 测试（应该失败）
    print("\n[7] 使用已消费的 Nonce（应该失败）...")
    # 复用之前的 nonce（已被消费）
    r = client.post(f"{BASE_URL}/web3/login", json={
        "address": wallet["address"],
        "signature": signature,  # 旧签名
        "message": nonce_data["message"],  # 旧消息
        "password": password,
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 400:
        print(f"    ✅ 正确拒绝: {r.json().get('detail')}")
    else:
        print(f"    ❌ 未能阻止重放攻击: {r.text}")

print("\n" + "=" * 50)
print("测试完成!")
