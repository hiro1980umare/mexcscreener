import requests

r = requests.get("https://api.mexc.com/api/v3/ticker/24hr", timeout=20)
print("接続結果:", r.status_code)
data = r.json()
usdt = [d for d in data if d["symbol"].endswith("USDT")]
print("USDT建て銘柄数:", len(usdt))
