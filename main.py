"""
微信读书自动刷时长工具
入口文件，负责 CLI 解析与流程调度

用法：
  python main.py              # 默认：持续运行调度模式
  python main.py --once       # 单次上报后退出（用于测试）
  python main.py --status     # 查看今日进度和 Cookie 状态
  python main.py --setup      # 重新运行交互式设置向导
  python main.py --cookie "wr_skey=xxx; wr_vid=xxx"  # 快速更新 Cookie
"""

import argparse
import sys
from pathlib import Path

import yaml
from loguru import logger

CONFIG_PATH = Path(__file__).parent / "config.yaml"
LOG_DIR = Path(__file__).parent / "logs"


def _setup_logger():
    LOG_DIR.mkdir(exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {level} | {message}")
    logger.add(
        LOG_DIR / "{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="7 days",
        encoding="utf-8",
    )


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get_cookie(config: dict) -> dict:
    return config.get("cookie", {})


def _cookie_is_empty(cookie_dict: dict) -> bool:
    return not cookie_dict.get("wr_skey") or not cookie_dict.get("wr_vid")


# ─── CLI 处理函数 ────────────────────────────────────────────────────

def cmd_setup():
    """--setup：运行交互式设置向导"""
    from src.setup import run_setup_wizard
    success = run_setup_wizard()
    sys.exit(0 if success else 1)


def cmd_cookie(raw_cookie: str):
    """--cookie：命令行直接更新 Cookie"""
    from src.setup import parse_cookie_string, _save_cookie
    from src.api import build_session, get_user_info

    cookie_dict = parse_cookie_string(raw_cookie)
    missing = [f for f in ["wr_skey", "wr_vid"] if not cookie_dict.get(f)]  # 必要字段检查
    if missing:
        logger.error(f"Cookie 字符串中缺少必要字段: {', '.join(missing)}")
        logger.info("示例格式: --cookie \"wr_skey=abc123; wr_vid=12345678\"")
        sys.exit(1)

    logger.info("正在验证 Cookie...")
    session = build_session(cookie_dict)
    user_info = get_user_info(session)
    if not user_info:
        logger.error("Cookie 无效或已过期，请重新获取")
        sys.exit(1)

    _save_cookie(cookie_dict)
    name = user_info.get("name", user_info.get("userVid", "未知"))
    logger.info(f"✓ Cookie 有效，已更新配置 (用户: {name})")


def cmd_status(cookie_dict: dict, config: dict):
    """--status：打印当前状态"""
    from src.reader import DailyStats, print_status
    stats = DailyStats()
    print_status(cookie_dict, stats, config)


def cmd_once(cookie_dict: dict, config: dict):
    """--once：单次上报后退出"""
    from src.api import build_session, get_bookshelf
    from src.reader import DailyStats, run_once

    reading = config.get("reading", {})
    duration = reading.get("duration_per_report", 60)

    session = build_session(cookie_dict)
    books = get_bookshelf(session)
    if not books:
        logger.error("书架为空或获取失败")
        sys.exit(1)

    logger.info(f"书架加载成功，共 {len(books)} 本书")
    stats = DailyStats()
    success = run_once(cookie_dict, books, duration, stats)
    sys.exit(0 if success else 1)


def cmd_daemon(cookie_dict: dict, config: dict):
    """默认：持续调度模式"""
    from src.scheduler import run_daemon
    try:
        run_daemon(cookie_dict, config)
    except KeyboardInterrupt:
        logger.info("已手动停止。")


# ─── 主入口 ─────────────────────────────────────────────────────────

def main():
    _setup_logger()

    parser = argparse.ArgumentParser(
        prog="wxreader",
        description="微信读书自动刷时长工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                                    # 持续运行
  python main.py --once                             # 单次测试
  python main.py --status                           # 查看状态
  python main.py --setup                            # 重新配置 Cookie
  python main.py --cookie "wr_skey=xxx; wr_vid=xxx"  # 快速更新 Cookie
        """,
    )
    parser.add_argument("--once", action="store_true", help="执行一次上报后退出")
    parser.add_argument("--status", action="store_true", help="查看今日进度和 Cookie 状态")
    parser.add_argument("--setup", action="store_true", help="重新运行交互式设置向导")
    parser.add_argument("--cookie", metavar="COOKIE_STR", help="直接通过命令行更新 Cookie")

    args = parser.parse_args()

    # --setup 优先级最高，不依赖现有配置
    if args.setup:
        cmd_setup()
        return

    # --cookie 更新后继续走正常流程
    if args.cookie:
        cmd_cookie(args.cookie)

    # 加载配置
    config = _load_config()
    cookie_dict = _get_cookie(config)

    # 检查是否需要首次设置
    if _cookie_is_empty(cookie_dict):
        logger.warning("未检测到有效 Cookie，启动首次设置向导...")
        from src.setup import run_setup_wizard
        if not run_setup_wizard():
            sys.exit(1)
        # 重新加载配置
        config = _load_config()
        cookie_dict = _get_cookie(config)

    # 分发命令
    if args.status:
        cmd_status(cookie_dict, config)
    elif args.once:
        cmd_once(cookie_dict, config)
    else:
        cmd_daemon(cookie_dict, config)


if __name__ == "__main__":
    main()
