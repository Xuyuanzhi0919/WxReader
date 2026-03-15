"""
微信读书 Web API 封装
参考实现: https://github.com/findmover/wxread

关键说明:
- 正确端点: weread.qq.com/web/book/read (非 i.weread.qq.com)
- 每次请求需要双层签名: sg (SHA256) + s (自定义哈希)
- Cookie 过期时调用 /web/login/renewal 自动续期
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.parse

import requests
from loguru import logger

# ─── 端点 ────────────────────────────────────────────────────────────
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"

# 签名密钥（来自微信读书 JS 源码）
_SIGN_KEY = "3c5c8717f3daf09iop3423zafeqoi"

# 请求基础数据模板（书籍/章节取自《三体》，固定值可正常上报）
_BASE_DATA: dict = {
    "appId": "wb182564874603h266381671",
    "b": "ce032b305a9bc1ce0b0dd2a",
    "c": "7f632b502707f6ffaa6bf2e",
    "ci": 27,
    "co": 389,
    "sm": "19聚会《三体》网友的聚会地点是一处僻静",
    "pr": 74,
    "rt": 15,
    "ts": 0,
    "rn": 0,
    "sg": "",
    "ct": 0,
    "ps": "4ee326507a65a465g015fae",
    "pc": "aab32e207a65a466g010615",
    "s": "",
}

# 随机轮换的书籍/章节 ID（来自 findmover/wxread）
_BOOKS = [
    "36d322f07186022636daa5e", "6f932ec05dd9eb6f96f14b9", "43f3229071984b9343f04a4",
    "d7732ea0813ab7d58g0184b8", "3d03298058a9443d052d409", "4fc328a0729350754fc56d4",
    "a743220058a92aa746632c0", "140329d0716ce81f140468e", "1d9321c0718ff5e11d9afe8",
    "ff132750727dc0f6ff1f7b5", "e8532a40719c4eb7e851cbe", "9b13257072562b5c9b1c8d6",
]

_CHAPTERS = [
    "ecc32f3013eccbc87e4b62e", "a87322c014a87ff679a21ea", "e4d32d5015e4da3b7fbb1fa",
    "16732dc0161679091c5aeb1", "8f132430178f14e45fce0f7", "c9f326d018c9f0f895fb5e4",
    "45c322601945c48cce2e120", "d3d322001ad3d9446802347", "65132ca01b6512bd43d90e3",
    "c20321001cc20ad4d76f5ae", "c51323901dc51ce410c121b", "aab325601eaab3238922e53",
    "9bf32f301f9bf31c7ff0a60", "c7432af0210c74d97b01b1c", "70e32fb021170efdf2eca12",
    "6f4322302126f4922f45dec",
]

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "content-type": "application/json;charset=UTF-8",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "origin": "https://weread.qq.com",
    "referer": "https://weread.qq.com/",
}


# ─── 签名算法 ─────────────────────────────────────────────────────────

def _encode_data(data: dict) -> str:
    """将 dict 按 key 排序并 URL 编码，用于计算 s 字段"""
    return "&".join(
        f"{k}={urllib.parse.quote(str(data[k]), safe='')}"
        for k in sorted(data.keys())
    )


def _cal_hash(input_string: str) -> str:
    """微信读书自定义哈希算法，用于计算 s 字段"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    i = length - 1
    while i > 0:
        _7032f5 = 0x7FFFFFFF & (_7032f5 ^ ord(input_string[i]) << (length - i) % 30)
        _cc1055 = 0x7FFFFFFF & (_cc1055 ^ ord(input_string[i - 1]) << i % 30)
        i -= 2
    return hex(_7032f5 + _cc1055)[2:].lower()


def _build_payload(last_time: int) -> dict:
    """
    构建带签名的请求 body
    last_time: 上一次请求的时间戳（秒），用于计算 rt 阅读时长
    """
    data = dict(_BASE_DATA)
    data.pop("s", None)

    now = int(time.time())
    data["b"] = random.choice(_BOOKS)
    data["c"] = random.choice(_CHAPTERS)
    data["ct"] = now
    data["rt"] = now - last_time
    data["ts"] = int(now * 1000) + random.randint(0, 1000)
    data["rn"] = random.randint(0, 1000)
    data["sg"] = hashlib.sha256(
        f"{data['ts']}{data['rn']}{_SIGN_KEY}".encode()
    ).hexdigest()
    data["s"] = _cal_hash(_encode_data(data))
    return data


# ─── API 函数 ─────────────────────────────────────────────────────────

def build_session(cookie_dict: dict) -> requests.Session:
    """构建带 Cookie 和请求头的 Session"""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(cookie_dict)
    return session


def refresh_skey(session: requests.Session) -> str | None:
    """
    调用 /web/login/renewal 刷新 wr_skey
    成功返回新的 8 位 skey 字符串，失败返回 None
    """
    try:
        payload = json.dumps(
            {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
            separators=(",", ":"),
        )
        resp = session.post(RENEW_URL, data=payload, timeout=10)
        for part in resp.headers.get("Set-Cookie", "").split(";"):
            if "wr_skey" in part:
                new_skey = part.split("=")[-1][:8]
                session.cookies.set("wr_skey", new_skey)
                return new_skey
    except Exception as e:
        logger.error(f"刷新 skey 请求异常: {e}")
    return None


def fix_synckey(session: requests.Session):
    """修复 synckey 异常（服务端偶发问题）"""
    try:
        payload = json.dumps(
            {"bookIds": ["3300060341"]}, separators=(",", ":")
        )
        session.post(FIX_SYNCKEY_URL, data=payload, timeout=10)
    except Exception as e:
        logger.warning(f"fix_synckey 请求异常: {e}")


def report_reading(session: requests.Session, last_time: int) -> tuple[bool, int]:
    """
    上报一次阅读记录
    返回 (success, new_last_time)
      - success=True  且 new_last_time>0 表示成功
      - success=False 且 new_last_time=-1 表示 Cookie 过期需刷新
      - success=False 且 new_last_time=0  表示其他错误
    """
    payload = _build_payload(last_time)
    try:
        resp = session.post(
            READ_URL,
            data=json.dumps(payload, separators=(",", ":")),
            timeout=10,
        )
        res = resp.json()
        logger.debug(f"上报响应: {res}")

        if "succ" in res:
            if "synckey" in res:
                logger.info(f"上报成功 | rt={payload['rt']}s | b={payload['b'][:8]}...")
                return True, payload["ct"]
            else:
                logger.warning("响应缺少 synckey，尝试修复...")
                fix_synckey(session)
                return False, 0
        else:
            logger.warning("Cookie 已过期，需要刷新")
            return False, -1

    except Exception as e:
        logger.error(f"上报请求异常: {e}")
        return False, 0
