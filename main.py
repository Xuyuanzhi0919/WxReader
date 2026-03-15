#!/usr/bin/env python3
"""
微信读书自动刷阅读时长
改进点（相比 findmover/wxread 和 funnyzak/weread-bot）：
  1. 自动拉取书架 & 章节，无需硬编码书籍 ID
  2. 指数退避重试，失败自动恢复而非直接崩溃
  3. 连续性阅读模拟，更接近真人阅读习惯
  4. 随机化阅读间隔，降低被识别风险
  5. 支持 YAML 配置 + 环境变量双通道（GitHub Action 友好）
  6. 轻量：单文件 < 400 行，依赖仅 requests + PyYAML
"""
import re
import os
import sys
import json
import time
import random
import hashlib
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests
import yaml

# ── 常量 ──────────────────────────────────────────────────────────────────────
SIGN_KEY    = "3c5c8717f3daf09iop3423zafeqoi"
APP_ID      = "wb182564874603h266381671"
READ_URL    = "https://weread.qq.com/web/book/read"
RENEW_URL   = "https://weread.qq.com/web/login/renewal"
SHELF_URL   = "https://weread.qq.com/web/shelf/sync"
CHAPTER_URL = "https://weread.qq.com/web/book/chapterInfos"

# 兜底书籍（仅当书架拉取失败时使用）
FALLBACK_BOOKS = [
    {
        "book_id": "3d03298058a9443d052d409",
        "title": "三体（默认）",
        "chapters": [
            {"c": "ecc32f3013eccbc87e4b62e", "ci": 1},
            {"c": "a87322c014a87ff679a21ea", "ci": 2},
            {"c": "e4d32d5015e4da3b7fbb1fa", "ci": 3},
            {"c": "16732dc0161679091c5aeb1", "ci": 4},
        ],
    },
    {
        "book_id": "ce032b305a9bc1ce0b0dd2a",
        "title": "三体II（默认）",
        "chapters": [
            {"c": "7f632b502707f6ffaa6bf2e", "ci": 27},
            {"c": "65132ca01b6512bd43d90e3", "ci": 28},
        ],
    },
]


# ── 签名算法 ───────────────────────────────────────────────────────────────────
def _encode(data: dict) -> str:
    return "&".join(
        f"{k}={urllib.parse.quote(str(data[k]), safe='')}"
        for k in sorted(data)
    )


def _cal_hash(s: str) -> str:
    a = b = 0x15051505
    i = len(s) - 1
    while i > 0:
        a = 0x7FFFFFFF & (a ^ (ord(s[i])     << ((len(s) - i) % 30)))
        b = 0x7FFFFFFF & (b ^ (ord(s[i - 1]) << (i % 30)))
        i -= 2
    return hex(a + b)[2:].lower()


def _sign(data: dict) -> dict:
    d = {k: v for k, v in data.items() if k != "s"}
    ts = int(time.time() * 1000) + random.randint(0, 999)
    rn = random.randint(0, 1000)
    d.update(
        ts=ts,
        rn=rn,
        sg=hashlib.sha256(f"{ts}{rn}{SIGN_KEY}".encode()).hexdigest(),
        ct=ts // 1000,
    )
    d["s"] = _cal_hash(_encode(d))
    return d


# ── 配置 ───────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    cookies: dict        = field(default_factory=dict)
    headers: dict        = field(default_factory=dict)
    # 从 curl 抓包数据中提取的 ps / pc（可选，提高兼容性）
    ps: str              = ""
    pc: str              = ""
    target_minutes: int  = 60
    interval_lo: float   = 28.0
    interval_hi: float   = 35.0
    # 阅读连续性：继续读同一本书/章节的概率
    continuity: float    = 0.75
    # 书架最多加载书籍数（避免初始化太慢）
    max_shelf_books: int = 8
    # 每本书最多取章节数
    max_chapters: int    = 15
    # 单次请求最大重试次数
    max_retry: int       = 3
    # 推送
    push_method: str     = ""
    pushplus_token: str  = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str   = ""


def _parse_curl(curl_str: str) -> tuple[dict, dict, str, str]:
    """从 curl bash 命令提取 headers、cookies、ps、pc"""
    hdrs_raw: dict[str, str] = {}
    for k, v in re.findall(r"-H '([^:]+): ([^']+)'", curl_str):
        hdrs_raw[k] = v

    # cookie 字符串（-b 优先，否则取 -H Cookie）
    m = re.search(r"-b '([^']+)'", curl_str)
    cookie_str = m.group(1) if m else next(
        (v for k, v in hdrs_raw.items() if k.lower() == "cookie"), ""
    )
    cookies: dict[str, str] = {}
    for part in cookie_str.split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    headers = {k: v for k, v in hdrs_raw.items() if k.lower() != "cookie"}

    # 从请求体（data-raw）提取 ps / pc
    ps = pc = ""
    m_body = re.search(r"--data-raw '([^']+)'", curl_str) or \
             re.search(r"-d '([^']+)'", curl_str)
    if m_body:
        try:
            body = json.loads(m_body.group(1))
            ps = body.get("ps", "")
            pc = body.get("pc", "")
        except Exception:
            pass

    return headers, cookies, ps, pc


def load_config() -> Config:
    cfg = Config()

    # 1. YAML 配置文件（优先读 config.yaml）
    for path in ("config.yaml", "config.yml"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            r = raw.get("reading", {})
            cfg.target_minutes = int(r.get("target_minutes", cfg.target_minutes))
            interval = str(r.get("interval", "28-35"))
            lo, _, hi = interval.partition("-")
            cfg.interval_lo = float(lo)
            cfg.interval_hi = float(hi or lo)
            cfg.continuity = float(r.get("continuity", cfg.continuity))
            cfg.max_shelf_books = int(r.get("max_shelf_books", cfg.max_shelf_books))

            n = raw.get("notify", {})
            cfg.push_method       = n.get("method", "")
            cfg.pushplus_token    = n.get("pushplus_token", "")
            cfg.telegram_bot_token = n.get("telegram_bot_token", "")
            cfg.telegram_chat_id  = n.get("telegram_chat_id", "")
            break

    # 2. 环境变量（覆盖 YAML）
    curl_str = os.getenv("WXREAD_CURL_BASH", "")
    if curl_str:
        cfg.headers, cfg.cookies, cfg.ps, cfg.pc = _parse_curl(curl_str)

    if os.getenv("TARGET_MINUTES"):
        cfg.target_minutes = int(os.environ["TARGET_MINUTES"])
    if os.getenv("PUSH_METHOD"):
        cfg.push_method = os.environ["PUSH_METHOD"]
    for attr, env in [
        ("pushplus_token",     "PUSHPLUS_TOKEN"),
        ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
        ("telegram_chat_id",   "TELEGRAM_CHAT_ID"),
    ]:
        if os.getenv(env):
            setattr(cfg, attr, os.environ[env])

    return cfg


# ── HTTP 客户端 ────────────────────────────────────────────────────────────────
class WeReadClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sess = requests.Session()
        self.sess.headers.update({
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            **cfg.headers,
        })
        for k, v in cfg.cookies.items():
            self.sess.cookies.set(k, v)

    # ── Cookie 刷新 ───────────────────────────────────────────────────────────
    def refresh_cookie(self) -> bool:
        payload = {"rq": "%2Fweb%2Fbook%2Fread", "ql": True}
        try:
            resp = self.sess.post(RENEW_URL, json=payload, timeout=15)
            set_cookie = resp.headers.get("Set-Cookie", "")
            for part in set_cookie.split(";"):
                if "wr_skey" in part:
                    new_key = part.split("=")[-1].strip()[:8]
                    self.cfg.cookies["wr_skey"] = new_key
                    self.sess.cookies.set("wr_skey", new_key)
                    log.info("Cookie 已刷新: wr_skey=%s", new_key)
                    return True
        except Exception as e:
            log.warning("Cookie 刷新异常: %s", e)
        log.warning("Cookie 刷新失败（可能已完全过期）")
        return False

    # ── 拉取书架 ──────────────────────────────────────────────────────────────
    def fetch_shelf(self) -> list[dict]:
        """从书架 API 获取书籍列表，返回 [{book_id, title, chapters:[]}]"""
        try:
            resp = self.sess.get(
                SHELF_URL,
                params={"synckey": 0, "teenmode": 0, "album": 1, "onlyBookid": 0},
                timeout=15,
            )
            data = resp.json()
            books_raw = data.get("books") or []
            result = []
            for item in books_raw:
                # 兼容两种响应格式
                book_info = item.get("book") or item
                bid   = book_info.get("bookId", "")
                title = book_info.get("title", bid)
                if bid:
                    result.append({"book_id": bid, "title": title, "chapters": []})
            log.info("书架获取成功：共 %d 本书", len(result))
            return result
        except Exception as e:
            log.warning("书架获取失败: %s", e)
            return []

    # ── 拉取章节 ──────────────────────────────────────────────────────────────
    def fetch_chapters(self, book_id: str) -> list[dict]:
        """获取指定书籍的章节列表，返回 [{c, ci}]"""
        try:
            resp = self.sess.post(
                CHAPTER_URL,
                json={"bookIds": [book_id]},
                timeout=15,
            )
            data = resp.json()
            chapters = []
            for entry in data.get("data", []):
                if entry.get("bookId") == book_id:
                    for ch in entry.get("updated", []):
                        cid = ch.get("chapterUid", "")
                        ci  = int(ch.get("chapterIdx", 1))
                        if cid:
                            chapters.append({"c": cid, "ci": ci})
            return chapters
        except Exception as e:
            log.warning("获取书籍 %s 章节失败: %s", book_id, e)
            return []

    def _fix_synckey(self, book_id: str):
        """请求 chapterInfos 修复 synckey 缺失问题"""
        try:
            self.sess.post(CHAPTER_URL, json={"bookIds": [book_id]}, timeout=10)
        except Exception:
            pass

    # ── 发送单次阅读请求 ──────────────────────────────────────────────────────
    def read_once(
        self, book_id: str, chapter: dict, last_ts: int
    ) -> tuple[bool, int]:
        """
        发送一次 /web/book/read 请求。
        返回 (是否成功, 新的 last_ts)
        """
        now = int(time.time())
        payload = {
            "appId": APP_ID,
            "b":  book_id,
            "c":  chapter["c"],
            "ci": chapter["ci"],
            "co": random.randint(100, 900),
            "sm": "",
            "pr": random.randint(10, 95),
            "rt": max(15, now - last_ts),
            "ps": self.cfg.ps,
            "pc": self.cfg.pc,
        }
        payload = _sign(payload)

        cookie_refreshed = False
        for attempt in range(self.cfg.max_retry):
            try:
                resp = self.sess.post(READ_URL, json=payload, timeout=15)
                res  = resp.json()
                log.debug("read resp: %s", res)

                if "succ" in res:
                    if "synckey" not in res:
                        # 修复 synckey 缺失（偶发情况）
                        self._fix_synckey(book_id)
                    return True, now

                # succ 不存在 → Cookie 过期
                if not cookie_refreshed:
                    log.warning("succ 字段缺失，尝试刷新 Cookie（第 %d 次）", attempt + 1)
                    if self.refresh_cookie():
                        cookie_refreshed = True
                        # 刷新后重新签名（ct/ts 需要更新）
                        payload = _sign(payload)
                        continue
                    else:
                        log.error("Cookie 无法刷新，终止本次请求")
                        return False, last_ts

            except requests.exceptions.RequestException as e:
                log.warning("网络异常 (attempt %d/%d): %s", attempt + 1, self.cfg.max_retry, e)

            # 指数退避等待
            if attempt < self.cfg.max_retry - 1:
                wait = (2 ** attempt) * 3 + random.uniform(0, 2)
                log.info("等待 %.1fs 后重试...", wait)
                time.sleep(wait)

        return False, last_ts


# ── 推送通知 ───────────────────────────────────────────────────────────────────
def push_notify(cfg: Config, msg: str):
    if not cfg.push_method:
        return
    try:
        if cfg.push_method == "pushplus":
            requests.post(
                "https://www.pushplus.plus/send",
                json={"token": cfg.pushplus_token, "title": "微信读书", "content": msg},
                timeout=10,
            )
            log.info("PushPlus 推送完成")
        elif cfg.push_method == "telegram":
            url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": cfg.telegram_chat_id, "text": msg},
                timeout=15,
            )
            log.info("Telegram 推送完成")
    except Exception as e:
        log.warning("推送失败: %s", e)


# ── 书籍选择（连续性模拟） ─────────────────────────────────────────────────────
def _pick(books: list[dict], prev_bi: int, prev_ci: int, continuity: float):
    """
    按连续性概率选书/章节：
    - continuity 概率：继续读当前书的下一章
    - 1-continuity 概率：随机跳到其他书的随机章节
    """
    if books and random.random() < continuity and 0 <= prev_bi < len(books):
        bi = prev_bi
        chapters = books[bi]["chapters"]
        if chapters:
            # 偏向顺序阅读，但偶尔跳章节
            if random.random() < continuity and prev_ci + 1 < len(chapters):
                ci = prev_ci + 1
            else:
                ci = random.randrange(len(chapters))
            return bi, ci

    bi = random.randrange(len(books))
    chapters = books[bi]["chapters"]
    ci = random.randrange(len(chapters)) if chapters else 0
    return bi, ci


# ── 日志初始化 ─────────────────────────────────────────────────────────────────
log = logging.getLogger("wxread")


def _setup_logging():
    os.makedirs("logs", exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-7s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                f"logs/{time.strftime('%Y-%m-%d')}.log", encoding="utf-8"
            ),
        ],
    )


# ── 主流程 ─────────────────────────────────────────────────────────────────────
def main():
    _setup_logging()
    cfg = load_config()

    if not cfg.cookies:
        log.error(
            "未检测到 Cookie！\n"
            "  本地运行：在 config.yaml 中配置，或\n"
            "  GitHub Action：设置 WXREAD_CURL_BASH secret"
        )
        sys.exit(1)

    client = WeReadClient(cfg)

    # 1. 先刷新一次 Cookie，保证有效性
    client.refresh_cookie()

    # 2. 从书架获取真实书籍
    books = client.fetch_shelf()

    # 3. 限制数量并拉取每本书的章节
    books = books[: cfg.max_shelf_books]
    for book in books:
        chapters = client.fetch_chapters(book["book_id"])
        if chapters:
            book["chapters"] = chapters[: cfg.max_chapters]
            log.info("  《%s》 载入 %d 章", book["title"], len(book["chapters"]))

    # 过滤没有章节的书
    books = [b for b in books if b["chapters"]]

    if not books:
        log.warning("书架为空或章节拉取失败，使用内置兜底书籍")
        books = FALLBACK_BOOKS

    log.info("共使用 %d 本书进行模拟阅读", len(books))

    # 4. 计算需要请求的次数（每 30s 一次）
    total = max(1, int(cfg.target_minutes * 60 / 30))
    log.info("目标 %d 分钟，共 %d 次请求", cfg.target_minutes, total)

    success   = 0
    prev_bi   = 0
    prev_ci   = 0
    last_ts   = int(time.time()) - 35  # 初始偏移，避免第一次 rt 过小

    for idx in range(1, total + 1):
        bi, ci = _pick(books, prev_bi, prev_ci, cfg.continuity)
        book   = books[bi]
        ch     = book["chapters"][ci]

        ok, last_ts = client.read_once(book["book_id"], ch, last_ts)

        if ok:
            success += 1
            prev_bi, prev_ci = bi, ci
            elapsed = success * 0.5
            log.info(
                "[%d/%d] ✅  《%s》第%d章  累计 %.1f min",
                idx, total, book["title"], ch["ci"], elapsed,
            )
        else:
            log.warning("[%d/%d] ❌  请求失败，跳过本次", idx, total)

        # 最后一次无需等待
        if idx < total:
            sleep_sec = random.uniform(cfg.interval_lo, cfg.interval_hi)
            log.debug("等待 %.1fs", sleep_sec)
            time.sleep(sleep_sec)

    # 5. 收尾
    actual_min = success * 0.5
    msg = (
        f"微信读书完成！\n"
        f"成功次数：{success}/{total}\n"
        f"约计时长：{actual_min:.1f} 分钟"
    )
    log.info("🎉 %s", msg.replace("\n", " | "))
    push_notify(cfg, msg)


if __name__ == "__main__":
    main()
