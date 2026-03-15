"""
阅读模拟核心逻辑
负责 Cookie 验证、书籍选取、单次上报执行
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date

from loguru import logger

from src.api import Book, build_session, get_bookshelf, get_user_info, report_reading


@dataclass
class DailyStats:
    """当日上报统计"""
    date: date = field(default_factory=date.today)
    total_seconds: int = 0
    success_count: int = 0
    fail_count: int = 0

    def reset_if_new_day(self):
        today = date.today()
        if self.date != today:
            self.date = today
            self.total_seconds = 0
            self.success_count = 0
            self.fail_count = 0

    @property
    def total_minutes(self) -> int:
        return self.total_seconds // 60


def validate_cookie(cookie_dict: dict) -> tuple[bool, dict | None]:
    """
    验证 Cookie 是否有效
    返回 (is_valid, user_info)
    """
    if not cookie_dict.get("wr_skey") or not cookie_dict.get("wr_vid"):
        logger.error("Cookie 为空，请先运行 python main.py --setup 完成配置")
        return False, None

    session = build_session(cookie_dict)
    user_info = get_user_info(session)
    if not user_info:
        logger.error("Cookie 已失效，请运行 python main.py --setup 或 --cookie 更新")
        return False, None

    return True, user_info


def pick_book(books: list[Book]) -> Book:
    """随机选取书架中一本书，避免单一书籍引发异常"""
    return random.choice(books)


def run_once(
    cookie_dict: dict,
    books: list[Book],
    duration_seconds: int,
    stats: DailyStats,
) -> bool:
    """
    执行一次阅读上报
    返回是否成功
    """
    session = build_session(cookie_dict)
    book = pick_book(books)
    success = report_reading(session, book, duration_seconds)

    stats.reset_if_new_day()
    if success:
        stats.total_seconds += duration_seconds
        stats.success_count += 1
    else:
        stats.fail_count += 1

    return success


def print_status(cookie_dict: dict, stats: DailyStats, config: dict):
    """打印当前状态（--status 命令使用）"""
    max_daily = config.get("reading", {}).get("max_daily_minutes", 60)
    interval = config.get("schedule", {}).get("interval_min", 25)
    jitter = config.get("schedule", {}).get("interval_jitter", 10)

    # 验证 Cookie
    valid, user_info = validate_cookie(cookie_dict)
    if valid:
        name = user_info.get("name", user_info.get("userVid", "未知"))
        status_str = f"✓ 有效 (用户: {name})"
    else:
        status_str = "✗ 无效或未配置"

    stats.reset_if_new_day()

    print()
    print("=" * 45)
    print("  WxReader 运行状态")
    print("=" * 45)
    print(f"  Cookie 状态 : {status_str}")
    print(f"  今日进度   : {stats.total_minutes} 分钟 / 上限 {max_daily} 分钟")
    print(f"  今日成功   : {stats.success_count} 次")
    print(f"  今日失败   : {stats.fail_count} 次")
    print(f"  上报间隔   : {interval} ± {jitter} 分钟")
    print("=" * 45)
    print()
