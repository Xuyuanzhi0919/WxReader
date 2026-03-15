"""
定时调度器
控制运行时段、随机间隔、每日上限
"""

import random
import time
from datetime import datetime

from loguru import logger

from src.api import build_session, get_bookshelf
from src.reader import DailyStats, run_once


def _next_interval(interval_min: int, jitter: int) -> int:
    """计算下次间隔秒数（带随机抖动）"""
    low = max(1, interval_min - jitter)
    high = interval_min + jitter
    return random.randint(low, high) * 60


def _in_active_hours(start_hour: int, end_hour: int) -> bool:
    return start_hour <= datetime.now().hour < end_hour


def _wait_until_active(start_hour: int, end_hour: int):
    """非运行时段时挂起等待，每分钟检查一次"""
    while not _in_active_hours(start_hour, end_hour):
        now = datetime.now()
        logger.info(
            f"当前 {now.strftime('%H:%M')} 不在运行时段 "
            f"({start_hour:02d}:00 - {end_hour:02d}:00)，等待中..."
        )
        time.sleep(60)


def run_daemon(cookie_dict: dict, config: dict):
    """
    持续运行的主调度循环
    - 仅在设定时段内运行
    - 每次上报后随机 sleep
    - 达到每日上限后当日挂起
    """
    sched = config.get("schedule", {})
    reading = config.get("reading", {})

    start_hour: int = sched.get("start_hour", 8)
    end_hour: int = sched.get("end_hour", 23)
    interval_min: int = sched.get("interval_min", 25)
    jitter: int = sched.get("interval_jitter", 10)
    duration: int = reading.get("duration_per_report", 60)
    max_daily_min: int = reading.get("max_daily_minutes", 60)

    stats = DailyStats()

    logger.info(
        f"调度器启动 | 时段 {start_hour:02d}:00-{end_hour:02d}:00 "
        f"| 间隔 {interval_min}±{jitter}min | 每日上限 {max_daily_min}min"
    )

    # 预先拉取书架
    session = build_session(cookie_dict)
    books = get_bookshelf(session)
    if not books:
        logger.error("书架为空或获取失败，请检查 Cookie 后重试")
        return

    logger.info(f"书架加载成功，共 {len(books)} 本书")

    while True:
        stats.reset_if_new_day()

        # 等待进入活跃时段
        _wait_until_active(start_hour, end_hour)

        # 检查每日上限
        if stats.total_minutes >= max_daily_min:
            logger.info(
                f"今日已达上限 {max_daily_min} 分钟，等待明日..."
            )
            time.sleep(300)  # 每 5 分钟检查一次（等跨天）
            continue

        # 执行一次上报
        run_once(cookie_dict, books, duration, stats)
        logger.info(
            f"今日累计: {stats.total_minutes} 分钟 / 上限 {max_daily_min} 分钟"
        )

        # 随机间隔 sleep
        wait_sec = _next_interval(interval_min, jitter)
        next_time = datetime.fromtimestamp(
            datetime.now().timestamp() + wait_sec
        ).strftime("%H:%M")
        logger.info(f"下次上报约 {wait_sec // 60} 分钟后（{next_time}）")
        time.sleep(wait_sec)
