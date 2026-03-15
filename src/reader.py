"""
阅读模拟核心逻辑
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from loguru import logger

from src.api import build_session, refresh_skey, report_reading


@dataclass
class DailyStats:
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


def validate_and_refresh(session, cookie_dict: dict) -> bool:
    """
    尝试刷新 skey，验证 Cookie 是否可用
    返回 True 表示可继续使用
    """
    if not cookie_dict.get("wr_skey") or not cookie_dict.get("wr_vid"):
        logger.error("Cookie 为空，请先运行 python3 main.py --setup")
        return False

    logger.info("正在刷新 Cookie 密钥...")
    new_skey = refresh_skey(session)
    if new_skey:
        cookie_dict["wr_skey"] = new_skey
        logger.info(f"Cookie 刷新成功，新 skey: {new_skey}")
        return True
    else:
        logger.error("Cookie 已失效，请运行 python3 main.py --setup 更新")
        return False


def run_once(session, cookie_dict: dict, last_time: int, stats: DailyStats) -> int:
    """
    执行一次阅读上报，处理 Cookie 过期自动刷新
    返回新的 last_time（失败返回原 last_time）
    """
    stats.reset_if_new_day()
    success, new_last_time = report_reading(session, last_time)

    if success:
        elapsed = new_last_time - last_time
        stats.total_seconds += elapsed
        stats.success_count += 1
        return new_last_time
    elif new_last_time == -1:
        # Cookie 过期，尝试刷新
        refreshed = validate_and_refresh(session, cookie_dict)
        if not refreshed:
            raise RuntimeError("Cookie 已失效且无法刷新，请手动更新")
        # 刷新后重试一次
        success2, new_last_time2 = report_reading(session, last_time)
        if success2:
            elapsed = new_last_time2 - last_time
            stats.total_seconds += elapsed
            stats.success_count += 1
            return new_last_time2
        stats.fail_count += 1
    else:
        stats.fail_count += 1

    return last_time


def print_status(cookie_dict: dict, stats: DailyStats, config: dict):
    """打印当前状态（--status 命令）"""
    from src.api import refresh_skey, build_session

    max_daily = config.get("reading", {}).get("max_daily_minutes", 60)
    interval = config.get("schedule", {}).get("interval_min", 25)
    jitter = config.get("schedule", {}).get("interval_jitter", 10)

    session = build_session(cookie_dict)
    new_skey = refresh_skey(session) if cookie_dict.get("wr_skey") else None
    if new_skey:
        status_str = f"✓ 有效 (skey: {new_skey})"
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
