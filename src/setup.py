"""
首次设置向导
自动引导用户完成 Cookie 配置
"""

import re
import sys
from pathlib import Path

import yaml
from loguru import logger

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_COOKIE_FIELDS = ["wr_skey", "wr_vid"]


def parse_cookie_string(raw: str) -> dict:
    """
    解析浏览器复制的原始 Cookie 字符串
    支持格式: "wr_skey=abc; wr_vid=123; wr_name=xxx"
    返回提取出的字段字典，至少包含 wr_skey 和 wr_vid
    """
    result = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key in _COOKIE_FIELDS:
            result[key] = value
    return result


def _print_guide():
    print("\n" + "=" * 55)
    print("  微信读书自动刷时长工具 - 首次设置向导")
    print("=" * 55)
    print()
    print("请按以下步骤获取 Cookie：")
    print()
    print("  1. 用浏览器打开 https://weread.qq.com 并登录")
    print("  2. 按 F12 打开开发者工具")
    print("  3. 切换到「Network（网络）」标签")
    print("  4. 刷新页面，点击任意请求")
    print("  5. 在「Request Headers」中找到「Cookie」字段")
    print("  6. 复制完整的 Cookie 字符串")
    print()
    print("  提示：Cookie 字符串形如：")
    print("  wr_skey=xxxxxxxx; wr_vid=12345678; wr_name=...; ...")
    print()


def run_setup_wizard(session_factory=None) -> bool:
    """
    运行交互式首次设置向导
    session_factory: 可注入的 build_session 函数（便于测试），默认使用 api 模块
    返回 True 表示设置成功
    """
    # 延迟导入避免循环
    if session_factory is None:
        from src.api import build_session, get_user_info as _get_user_info
    else:
        build_session = session_factory
        _get_user_info = None

    _print_guide()

    for attempt in range(3):
        print(f"请粘贴 Cookie 字符串（第 {attempt + 1}/3 次）：")
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消设置。")
            return False

        if not raw:
            print("输入为空，请重试。\n")
            continue

        cookie_dict = parse_cookie_string(raw)
        missing = [f for f in _COOKIE_FIELDS if not cookie_dict.get(f)]
        if missing:
            print(f"未找到必要字段: {', '.join(missing)}，请确认复制了完整 Cookie。\n")
            continue

        # 验证有效性
        print("正在验证 Cookie 有效性...")
        session = build_session(cookie_dict)

        if session_factory is not None:
            # 测试模式，跳过实际验证
            user_info = {"userVid": cookie_dict.get("wr_vid"), "name": "测试用户"}
        else:
            from src.api import get_user_info as _get_user_info
            user_info = _get_user_info(session)

        if not user_info:
            print("Cookie 无效或已过期，请重新获取后再试。\n")
            continue

        # 保存到 config.yaml
        _save_cookie(cookie_dict)
        name = user_info.get("name", user_info.get("userVid", "未知"))
        print(f"\n✓ 验证成功！已登录为：{name}")
        print(f"✓ 配置已保存到 config.yaml\n")
        return True

    print("设置失败次数过多，请检查 Cookie 后重新运行 python main.py --setup")
    return False


def _save_cookie(cookie_dict: dict):
    """将 Cookie 写入 config.yaml，保留其他配置不变"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    if "cookie" not in config:
        config["cookie"] = {}
    config["cookie"]["wr_skey"] = cookie_dict.get("wr_skey", "")
    config["cookie"]["wr_vid"] = cookie_dict.get("wr_vid", "")

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
