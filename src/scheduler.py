"""
定时调度器
"""

from __future__ import annotations

import random
import time
from datetime import datetime

from loguru import logger

from src.api import build_session, refresh_skey
from src.reader import DailyStats, run_once


def _next_interval(interval_min: int, jitter: int) -> int:
    low = max(1, interval_min - jitter)
    high = interval_min + jitter
    return random.randint(low, high) * 60


def _in_active_hours(start_hour: int, end_hour: int) -> bool:
    return start_hour <= datetime.now().hour < end_hour


def _wait_until_active(start_hour: int, end_hour: int):
    while not _in_active_hours(start_hour, end_hour):
        now = datetime.now()
        logger.info(
            f"当前 {now.strftime('%H:%M')} 不在运行时段 "
            f"({start_hour:02d}:00-{end_hour:02d}:00)，等待中..."
        )
        time.sleep(60)


def run_daemon(cookie_dict: dict, config: dict):
    sched = config.get("schedule", {})
    reading = config.get("reading", {})

    start_hour: int = sched.get("start_hour", 8)
    end_hour: int = sched.get("end_hour", 23)
    interval_min: int = sched.get("interval_min", 25)
    jitter: int = sched.get("interval_jitter", 10)
    max_daily_min: int = reading.get("max_daily_minutes", 60)

    stats = DailyStats()
    session = build_session(cookie_dict)

    # 启动时先刷新一次 Cookie
    logger.info("启动，正在刷新 Cookie...")
    new_skey = refresh_skey(session)
    if new_skey:
        cookie_dict["wr_skey"] = new_skey
        logger.info(f"Cookie 刷新成功，skey: {new_skey}")
    else:
        logger.error("Cookie 无效，请运行 python3 main.py --setup 更新后重试")
        return

    last_time = int(time.time()) - 30
    logger.info(
        f"调度器启动 | 时段 {start_hour:02d}:00-{end_hour:02d}:00 "
        f"| 间隔 {interval_min}±{jitter}min | 每日上限 {max_daily_min}min"
    )

    while True:
        stats.reset_if_new_day()

        _wait_until_active(start_hour, end_hour)

        if stats.total_minutes >= max_daily_min:
            logger.info(f"今日已达上限 {max_daily_min} 分钟，等待明日...")
            time.sleep(300)
            continue

        try:
            last_time = run_once(session, cookie_dict, last_time, stats)
        except RuntimeError as e:
            logger.error(str(e))
            return

        logger.info(
            f"今日累计: {stats.total_minutes} 分钟 / 上限 {max_daily_min} 分钟"
        )

        wait_sec = _next_interval(interval_min, jitter)
        next_time = datetime.fromtimestamp(
            datetime.now().timestamp() + wait_sec
        ).strftime("%H:%M")
        logger.info(f"下次上报约 {wait_sec // 60} 分钟后（{next_time}）")
        time.sleep(wait_sec)
