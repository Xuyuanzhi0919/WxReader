"""
微信读书 Web API 封装
所有 HTTP 请求集中在此模块
"""

import requests
from dataclasses import dataclass
from loguru import logger

BASE_URL = "https://i.weread.qq.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weread.qq.com/",
    "Origin": "https://weread.qq.com",
}


@dataclass
class Book:
    book_id: str
    title: str
    author: str


def build_session(cookie_dict: dict) -> requests.Session:
    """构建带 Cookie 和请求头的 Session"""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(cookie_dict)
    return session


def get_user_info(session: requests.Session) -> dict | None:
    """
    验证登录状态，获取用户信息
    返回 None 表示 Cookie 无效
    """
    try:
        resp = session.get(f"{BASE_URL}/user/profile", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "userVid" in data:
                return data
        logger.warning(f"user/profile 返回异常: status={resp.status_code}")
        return None
    except Exception as e:
        logger.error(f"获取用户信息失败: {e}")
        return None


def get_bookshelf(session: requests.Session) -> list[Book]:
    """
    获取用户书架书单
    返回 Book 列表，失败返回空列表
    """
    try:
        resp = session.get(
            f"{BASE_URL}/shelf/sync",
            params={"synckey": 0, "teenmode": 0, "album": 1, "onlyBookid": 0},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"shelf/sync 返回异常: status={resp.status_code}")
            return []

        data = resp.json()
        books = []
        for item in data.get("books", []):
            book_info = item.get("book", {})
            book_id = book_info.get("bookId", "")
            if not book_id:
                continue
            books.append(
                Book(
                    book_id=book_id,
                    title=book_info.get("title", "未知书名"),
                    author=book_info.get("author", "未知作者"),
                )
            )
        return books
    except Exception as e:
        logger.error(f"获取书架失败: {e}")
        return []


def report_reading(
    session: requests.Session,
    book: Book,
    duration_seconds: int,
) -> bool:
    """
    上报阅读记录
    duration_seconds: 本次上报的阅读秒数
    返回是否成功
    """
    payload = {
        "bookId": book.book_id,
        "readingTime": duration_seconds,
        "appId": "wb182564874663h25f1a4e6eb",
        "synckey": 0,
        "format": "epub",
        "version": 1,
    }
    try:
        resp = session.post(
            f"{BASE_URL}/readbookrecord",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            result = resp.json()
            # 成功时服务端返回 {"succ": 1} 或包含 synckey
            if result.get("succ") == 1 or "synckey" in result:
                logger.info(
                    f"上报成功 | 《{book.title}》 +{duration_seconds}s"
                )
                return True
        logger.warning(
            f"上报失败 | status={resp.status_code} body={resp.text[:200]}"
        )
        return False
    except Exception as e:
        logger.error(f"上报请求异常: {e}")
        return False
