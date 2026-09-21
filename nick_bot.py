"""
Telegram 昵称实时时间 + 天气 userbot

昵称格式: ZIDDDD. 13:00:00 ☀️30℃  （字母自动转粗斜体、数字自动转粗体无衬线）
- 时间每 30 秒刷新一次昵称
- 天气与温度每 2 小时刷新一次（Open-Meteo，免费无需 key）
- 用自己的 Telegram 账号登录（MTProto / Telethon）
"""
import asyncio
import os
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest, UpdateStatusRequest

load_dotenv()

# ---------- 配置（读 .env） ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]
PREFIX = os.environ.get("PREFIX", "ZIDDDD.")
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "30"))
WEATHER_REFRESH_SECONDS = int(os.environ.get("WEATHER_REFRESH_HOURS", "2")) * 3600
LAT = float(os.environ.get("LAT", "22.27"))    # 珠海
LON = float(os.environ.get("LON", "113.58"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Shanghai"))
PROXY_URL = os.environ.get("PROXY", "").strip()

# ---------- 特殊字体映射 ----------
# 粗体大写字母（不斜体）: 𝐀 = U+1D400
BOLD_UPPER = {chr(ord("A") + i): chr(0x1D400 + i) for i in range(26)}
# 粗体小写字母: 𝐚 = U+1D41A
BOLD_LOWER = {chr(ord("a") + i): chr(0x1D41A + i) for i in range(26)}
# 双线空心数字: 𝟘 = U+1D7D8
DOUBLE_STRUCK_DIGIT = {str(i): chr(0x1D7D8 + i) for i in range(10)}


def stylize(text: str) -> str:
    out = []
    for ch in text:
        out.append(
            BOLD_UPPER.get(ch)
            or BOLD_LOWER.get(ch)
            or DOUBLE_STRUCK_DIGIT.get(ch)
            or ch
        )
    return "".join(out)


# ---------- 天气（Open-Meteo，免费无需 key） ----------
WEATHER_EMOJI = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌦️", 56: "🌧️", 57: "🌧️",
    61: "🌧️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "🌨️", 77: "🌨️",
    80: "🌧️", 81: "🌧️", 82: "🌧️", 85: "🌨️", 86: "🌨️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}


async def fetch_weather():
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "current": "temperature_2m,weather_code",
        "timezone": "Asia/Shanghai",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            data = await r.json()
    temp = round(data["current"]["temperature_2m"])
    code = data["current"]["weather_code"]
    return WEATHER_EMOJI.get(code, "🌡️"), temp


def build_client() -> TelegramClient:
    kwargs = {}
    if PROXY_URL:
        # 形如 socks5://127.0.0.1:1080 或 http://127.0.0.1:7890
        u = urlparse(PROXY_URL)
        scheme = "socks5" if u.scheme.startswith("socks") else "http"
        kwargs["proxy"] = (scheme, u.hostname, u.port)
    # Zeabur 部署用环境变量 SESSION_STRING；本地用 my_account.session 文件
    session_str = os.environ.get("SESSION_STRING", "").strip()
    if session_str:
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(session_str), API_ID, API_HASH, **kwargs)
    return TelegramClient("my_account", API_ID, API_HASH, **kwargs)


def day_or_night_emoji(emoji: str) -> str:
    """晚上（19:00-06:00）晴天类换成月亮"""
    hour = datetime.now(TZ).hour
    if hour >= 6 and hour < 19:
        return emoji
    return {
        "☀️": "🌙",
        "🌤️": "🌜",
        "⛅": "🌛",
    }.get(emoji, emoji)


async def main():
    client = build_client()
    await client.start(phone=PHONE)

    # Zeabur 保活用：存在 PORT 环境变量时起一个健康检查 HTTP 服务
    port_env = os.environ.get("PORT")
    if port_env:
        from aiohttp import web
        app = web.Application()

        async def health(_):
            return web.Response(text="ok")

        app.router.add_get("/", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(port_env))
        await site.start()
        print(f"[健康检查] 监听端口 {port_env}")

    prefix = stylize(PREFIX)
    emoji, temp = await fetch_weather()
    last_weather = time.time()
    print(f"[启动] 当前天气: {emoji} {temp}°C")

    while True:
        now = datetime.now(TZ)
        # 秒数对齐到 30 秒边界：只显示 :00 或 :30
        aligned = now.replace(second=(0 if now.second < 30 else 30), microsecond=0)
        time_str = stylize(aligned.strftime("%H:%M:%S"))

        # 每 2 小时刷新天气（失败则沿用缓存）
        if time.time() - last_weather > WEATHER_REFRESH_SECONDS:
            try:
                emoji, temp = await fetch_weather()
                last_weather = time.time()
                print(f"[天气更新] {emoji} {temp}°C")
            except Exception as e:
                print(f"[天气拉取失败，沿用缓存] {e}")

        nick_emoji = day_or_night_emoji(emoji)
        nick = f"{time_str} {nick_emoji}{stylize(str(temp))}°"
        try:
            if not client.is_connected():
                print("[重连] 连接断开，正在重连...")
                await client.connect()
                await asyncio.sleep(2)
            await client(UpdateProfileRequest(last_name=nick))
            await client(UpdateStatusRequest(offline=True))
            print(f"[更新] {nick}")
        except FloodWaitError as e:
            print(f"[Telegram 限流] 等待 {e.seconds} 秒")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"[更新失败] {e}")
            try:
                await client.disconnect()
                await asyncio.sleep(5)
                await client.connect()
            except Exception:
                pass

        # 对齐到下一个 30 秒边界再睡
        await asyncio.sleep(30 - (time.time() % 30))


if __name__ == "__main__":
    asyncio.run(main())
