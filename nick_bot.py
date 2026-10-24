"""
Telegram 昵称实时时间 + 天气 userbot

- 只更新姓氏框(last_name)：HH:MM（双线空心数字）+ 天气 emoji + 温度（双线空心数字）°𝙲，名字框不改动
- 时间每分钟刷新一次；天气与温度每 10 分钟对齐 :00/:10/:20/:30/:40/:50 拉取（和风天气 QWeather）
- 用自己的 Telegram 账号登录（MTProto / Telethon）
"""
import asyncio
import os
import time
from datetime import datetime, timedelta
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
WEATHER_REFRESH_SECONDS = 600  # 天气每 10 分钟刷新一次（:00/:10/:20/:30/:40/:50）
LAT = float(os.environ.get("LAT", "22.27"))    # 珠海
LON = float(os.environ.get("LON", "113.58"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Shanghai"))
PROXY_URL = os.environ.get("PROXY", "").strip()
QWEATHER_KEY = os.environ.get("QWEATHER_KEY", "").strip()
QWEATHER_HOST = os.environ.get("QWEATHER_HOST", "devapi.qweather.com").strip()

# ---------- 特殊字体映射 ----------
# 粗体大写字母（不斜体）: 𝐀 = U+1D400
BOLD_UPPER = {chr(ord("A") + i): chr(0x1D400 + i) for i in range(26)}
# 粗体小写字母: 𝐚 = U+1D41A
BOLD_LOWER = {chr(ord("a") + i): chr(0x1D41A + i) for i in range(26)}
# 双线空心数字: 𝟘 = U+1D7D8
DOUBLE_STRUCK_DIGIT = {str(i): chr(0x1D7D8 + i) for i in range(10)}
# 粗体数字: 𝟎 = U+1D7CE
BOLD_DIGIT = {str(i): chr(0x1D7CE + i) for i in range(10)}


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


def stylize_bold_digit(text: str) -> str:
    out = []
    for ch in text:
        out.append(BOLD_DIGIT.get(ch) or ch)
    return "".join(out)


# ---------- 天气（和风天气 QWeather，免费 key） ----------
# icon 代码 → 表情；夜间晴类 icon 直接给月亮
WEATHER_EMOJI = {
    # 晴
    "100": "☀️", "150": "🌙",
    # 多云 / 少云 / 晴间多云
    "101": "🌤️", "102": "⛅", "103": "🌤️",
    "151": "🌙", "152": "🌙", "153": "🌙",
    # 阴
    "104": "☁️",
    # 雨
    "300": "🌦️", "301": "🌧️", "302": "⛈️", "303": "⛈️", "304": "⛈️",
    "305": "🌦️", "306": "🌧️", "307": "🌧️", "308": "🌧️", "309": "🌦️",
    "310": "🌧️", "311": "🌧️", "312": "🌧️", "313": "🌧️",
    "314": "🌦️", "315": "🌧️", "316": "🌧️", "317": "🌧️", "318": "🌧️",
    "350": "🌦️", "351": "🌧️", "399": "🌧️",
    # 雪
    "400": "🌨️", "401": "🌨️", "402": "🌨️", "403": "🌨️",
    "404": "🌧️", "405": "🌨️", "406": "🌧️", "407": "🌨️",
    "408": "🌨️", "409": "🌨️", "410": "🌨️",
    "456": "🌧️", "457": "🌨️", "499": "🌨️",
    # 雾 / 霾
    "500": "🌫️", "501": "🌫️", "502": "🌫️",
    "509": "🌫️", "510": "🌫️", "511": "🌫️",
    "512": "🌫️", "513": "🌫️", "514": "🌫️", "515": "🌫️",
    # 沙尘 / 扬沙 / 沙尘暴
    "503": "🌪️", "504": "🌪️", "507": "🌪️", "508": "🌪️",
    # 热 / 冷 → 统一阴
    "900": "☁️", "901": "☁️",
}


async def fetch_weather():
    url = f"https://{QWEATHER_HOST}/v7/weather/now"
    params = {
        "location": f"{LON},{LAT}",
        "key": QWEATHER_KEY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            data = await r.json()
    now = data["now"]
    temp = round(float(now["temp"]))
    return WEATHER_EMOJI.get(now["icon"], "☁️"), temp


# ---------- 天文边界（民用蒙影：civilDawn ≤ 现在 < civilDusk 为白天） ----------
# 启动/跨天拉取当天边界并按本地日期缓存；失败先在短窗口内重试，连续失败 6 次后转整点节流
SHORT_WINDOW_MINUTES = 5   # 首次失败后的短窗口长度（分钟）
MAX_SHORT_ATTEMPTS = 6     # 短窗口内最多尝试次数（首轮 + 5 分钟内 5 次）
astro_cache = {
    "date": None, "dawn": None, "dusk": None,
    "fail_count": 0, "first_fail_at": None, "next_retry": None,
}


def _next_hour(now: datetime) -> datetime:
    """返回下一整点时刻（本地 aware），作为整点节流的下次允许重试时间"""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _reset_failure_state() -> None:
    """清空失败计数与退避时刻，保留日期与边界由调用方处理"""
    astro_cache["fail_count"] = 0
    astro_cache["first_fail_at"] = None
    astro_cache["next_retry"] = None


def _parse_astro_time(ts: str) -> datetime:
    """v1 astro 为 UTC ISO（Z 结尾），解析后转本地时区，返回 aware datetime"""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)


async def fetch_civil_twilight():
    """拉取当天 civilDawn（民用晨光开始）/ civilDusk（民用昏影结束），返回本地 aware (dawn, dusk)"""
    url = f"https://{QWEATHER_HOST}/weather/v1/daily/{LAT}/{LON}"
    params = {"days": "1", "key": QWEATHER_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            data = await r.json()
    astro = data["days"][0]["astro"]
    dawn = _parse_astro_time(astro["civilDawn"])
    dusk = _parse_astro_time(astro["civilDusk"])
    return dawn, dusk


async def ensure_astro(now: datetime) -> None:
    """当天缓存命中直接返回；启动/跨天拉取边界；失败先短窗口重试，6 次失败后整点节流"""
    # 当天已有缓存：不发请求
    if astro_cache["date"] == now.date():
        return
    # 跨天：清理昨天遗留的失败计数与节流时刻，新一天重新进入短窗口
    if astro_cache["first_fail_at"] and astro_cache["first_fail_at"].date() != now.date():
        _reset_failure_state()
    # 整点节流期：未到下次允许时刻直接跳过
    if astro_cache["fail_count"] >= MAX_SHORT_ATTEMPTS:
        if astro_cache["next_retry"] and now < astro_cache["next_retry"]:
            return
    # 短窗口已过但计数未满 6（循环延迟兜底）：直接转入整点节流
    elif astro_cache["fail_count"] > 0:
        if now - astro_cache["first_fail_at"] > timedelta(minutes=SHORT_WINDOW_MINUTES):
            astro_cache["fail_count"] = MAX_SHORT_ATTEMPTS
            astro_cache["next_retry"] = _next_hour(now)
            return

    try:
        dawn, dusk = await fetch_civil_twilight()
        astro_cache.update(date=now.date(), dawn=dawn, dusk=dusk)
        _reset_failure_state()
        print(f"[天文边界] 今日 {dawn.strftime('%H:%M')} – {dusk.strftime('%H:%M')}")
    except Exception as e:
        # 边界清空：day_or_night_emoji 回退固定时段 06:00–18:59
        astro_cache["date"] = None
        astro_cache["dawn"] = None
        astro_cache["dusk"] = None
        was_throttled = astro_cache["fail_count"] >= MAX_SHORT_ATTEMPTS
        if astro_cache["fail_count"] == 0:
            astro_cache["first_fail_at"] = now
        astro_cache["fail_count"] += 1
        astro_cache["next_retry"] = _next_hour(now)
        if was_throttled:
            print(
                f"[天文边界整点重试失败，下次 "
                f"{astro_cache['next_retry'].strftime('%H:00')}] {e}"
            )
        elif astro_cache["fail_count"] >= MAX_SHORT_ATTEMPTS:
            print(
                f"[天文边界连续失败 {MAX_SHORT_ATTEMPTS} 次，转整点节流，"
                f"下次重试 {astro_cache['next_retry'].strftime('%H:00')}] {e}"
            )
        else:
            print(
                f"[天文边界拉取失败，第 {astro_cache['fail_count']}/"
                f"{MAX_SHORT_ATTEMPTS} 次，短窗口内重试] {e}"
            )


def build_client() -> TelegramClient:
    kwargs = {}
    if PROXY_URL:
        # 形如 socks5://127.0.0.1:1080 或 http://127.0.0.1:7890
        u = urlparse(PROXY_URL)
        scheme = "socks5" if u.scheme.startswith("socks") else "http"
        kwargs["proxy"] = (scheme, u.hostname, u.port)
    # Railway 部署用环境变量 SESSION_STRING；本地用 my_account.session 文件
    session_str = os.environ.get("SESSION_STRING", "").strip()
    if session_str:
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(session_str), API_ID, API_HASH, **kwargs)
    return TelegramClient("my_account", API_ID, API_HASH, **kwargs)


def day_or_night_emoji(emoji: str) -> str:
    """夜间：晴换月亮，多云/少云转阴云；白天表情原样"""
    now = datetime.now(TZ)
    if astro_cache["dawn"] and astro_cache["dusk"]:
        is_day = astro_cache["dawn"] <= now < astro_cache["dusk"]
    else:
        # 天文边界缺失/拉取失败：回退固定时段 06:00–18:59
        is_day = 6 <= now.hour < 19
    if is_day:
        return emoji
    return {
        "☀️": "🌙",
        "🌤️": "☁️",
        "⛅": "☁️",
    }.get(emoji, emoji)


async def main():
    client = build_client()
    await client.start(phone=PHONE)

    # 健康检查：存在 PORT 环境变量时起一个 HTTP 保活服务
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
    await ensure_astro(datetime.now(TZ))

    while True:
        now = datetime.now(TZ)
        await ensure_astro(now)
        # 对齐到分钟边界，只显示 HH:MM
        aligned = now.replace(second=0, microsecond=0)
        time_str = stylize(aligned.strftime("%H:%M"))

        # 天气在每小时 :00/:10/:20/:30/:40/:50 对齐刷新（每 10 分钟；60 秒防同分钟重复）
        if now.minute % 10 == 0 and time.time() - last_weather >= 60:
            try:
                emoji, temp = await fetch_weather()
                last_weather = time.time()
                print(f"[天气更新] {emoji} {temp}°C")
            except Exception as e:
                print(f"[天气拉取失败，沿用缓存] {e}")

        nick_emoji = day_or_night_emoji(emoji)
        nick = f"{time_str} {nick_emoji}{stylize(str(temp))}°𝙲"
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

        # 对齐到下一个分钟边界再睡
        await asyncio.sleep(60 - (time.time() % 60))


if __name__ == "__main__":
    asyncio.run(main())
