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

# 兜底书籍：所有章节 ID 均经过真实 API 请求验证可用（返回 succ:1）
# 章节 ID 必须是十六进制字符串格式，整数型 chapterUid 的书不被 read API 接受
FALLBACK_BOOKS = [
    {
        "book_id": "ce032b305a9bc1ce0b0dd2a",
        "title": "三体II",
        "chapters": [
            {"c": "7f632b502707f6ffaa6bf2e", "ci": 27},
            {"c": "65132ca01b6512bd43d90e3", "ci": 28},
        ],
    },
    {
        "book_id": "3d03298058a9443d052d409",
        "title": "三体I",
        "chapters": [
            {"c": "ecc32f3013eccbc87e4b62e", "ci": 1},
            {"c": "a87322c014a87ff679a21ea", "ci": 2},
            {"c": "e4d32d5015e4da3b7fbb1fa", "ci": 3},
            {"c": "16732dc0161679091c5aeb1", "ci": 4},
        ],
    },
]

# 已验证可用的 ps/pc 默认值（读请求必须非空，否则 API 返回 {}）
DEFAULT_PS = "4ee326507a65a465g015fae"
DEFAULT_PC = "aab32e207a65a466g010615"


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
    # ps/pc 从 curl body 中提取；未提供时用已验证有效的默认值（空值会导致 API 返回 {}）
    ps: str              = DEFAULT_PS
    pc: str              = DEFAULT_PC
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


def _parse_cookie_str(cookie_str: str) -> dict:
    """将 'k1=v1; k2=v2' 格式的 cookie 字符串解析为字典"""
    cookies: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _parse_curl(curl_str: str) -> tuple[dict, dict, str, str]:
    """
    解析抓包数据，兼容三种格式：
      1. curl bash 命令（curl '...' -H '...' --data-raw '...'）
      2. Chrome/DevTools 原始请求头（每行 "key: value"，cookie 可多行）
      3. 纯 cookie 字符串（'k1=v1; k2=v2; ...'，单行或以 'cookie' 开头）
    返回 (headers, cookies, ps, pc)
    """
    s = curl_str.strip()

    # ── 格式1：curl bash ───────────────────────────────────────────────────────
    if s.startswith("curl "):
        hdrs_raw: dict[str, str] = {}
        for k, v in re.findall(r"-H '([^:]+): ([^']+)'", s):
            hdrs_raw[k] = v

        m = re.search(r"-b '([^']+)'", s)
        cookie_str = m.group(1) if m else next(
            (v for k, v in hdrs_raw.items() if k.lower() == "cookie"), ""
        )
        cookies: dict[str, str] = {}
        for part in cookie_str.split("; "):
            if "=" in part:
                ck, cv = part.split("=", 1)
                cookies[ck.strip()] = cv.strip()

        headers = {k: v for k, v in hdrs_raw.items() if k.lower() != "cookie"}

        ps = pc = ""
        m_body = re.search(r"--data-raw '([^']+)'", s) or re.search(r"-d '([^']+)'", s)
        if m_body:
            try:
                body = json.loads(m_body.group(1))
                ps = body.get("ps", "")
                pc = body.get("pc", "")
            except Exception:
                pass
        return headers, cookies, ps, pc

    # ── 格式3：纯 cookie 字符串（单行，含 wr_skey= 或 wr_vid=，无换行）──────────
    # 去掉可选的 "cookie:" / "Cookie:" 前缀后直接解析
    first_line = s.splitlines()[0].strip()
    cookie_prefix = re.match(r"^[Cc]ookie\s*:\s*", first_line)
    candidate = first_line[cookie_prefix.end():] if cookie_prefix else first_line
    if "\n" not in s and ("wr_skey=" in candidate or "wr_vid=" in candidate):
        return {}, _parse_cookie_str(candidate), "", ""

    # ── 格式2：Chrome 原始请求头（key: value，每行一个，cookie 可多行） ─────────
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    for line in s.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(": ")
        key = key.strip()
        val = val.strip()
        if key.startswith(":"):          # HTTP/2 伪头（:authority / :method …）
            continue
        if key.lower() == "cookie":
            # 每行一个 cookie 键值对
            if "=" in val:
                ck, cv = val.split("=", 1)
                cookies[ck.strip()] = cv.strip()
        else:
            headers[key] = val

    return headers, cookies, "", ""


def _apply_yaml_global(cfg: Config, raw: dict):
    """将 YAML 全局配置写入 Config 对象（reading / notify 节）"""
    r = raw.get("reading", {})
    cfg.target_minutes  = int(r.get("target_minutes", cfg.target_minutes))
    interval = str(r.get("interval", "28-35"))
    lo, _, hi = interval.partition("-")
    cfg.interval_lo     = float(lo)
    cfg.interval_hi     = float(hi or lo)
    cfg.continuity      = float(r.get("continuity", cfg.continuity))
    cfg.max_shelf_books = int(r.get("max_shelf_books", cfg.max_shelf_books))

    n = raw.get("notify", {})
    cfg.push_method        = n.get("method", "")
    cfg.pushplus_token     = n.get("pushplus_token", "")
    cfg.telegram_bot_token = n.get("telegram_bot_token", "")
    cfg.telegram_chat_id   = n.get("telegram_chat_id", "")


def _apply_env(cfg: Config):
    """将环境变量覆盖写入 Config 对象"""
    curl_str = os.getenv("WXREAD_CURL_BASH", "") or os.getenv("WXREAD_COOKIE", "")
    if curl_str:
        cfg.headers, cfg.cookies, ps, pc = _parse_curl(curl_str)
        if ps:
            cfg.ps = ps
        if pc:
            cfg.pc = pc
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


def load_users() -> list[tuple[str, "Config"]]:
    """
    加载用户列表，返回 [(name, Config), ...]。

    优先级：
      1. YAML config.yaml 中的 users 列表（多用户）
      2. 环境变量 WXREAD_COOKIE / WXREAD_CURL_BASH（单用户）

    YAML 多用户格式示例：
      users:
        - name: "Alice"
          cookie: "wr_skey=xxx; wr_vid=111"
          target_minutes: 60
        - name: "Bob"
          cookie: "wr_skey=yyy; wr_vid=222"
          target_minutes: 30
    """
    raw = {}
    for path in ("config.yaml", "config.yml"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            break

    users_yaml = raw.get("users", [])

    # ── 多用户模式：YAML users 列表 ──────────────────────────────────────────
    if users_yaml:
        result = []
        for u in users_yaml:
            name   = u.get("name", "user")
            cookie = u.get("cookie", "")
            if not cookie:
                log.warning("用户 %s 未配置 cookie，跳过", name)
                continue
            cfg = Config()
            _apply_yaml_global(cfg, raw)          # 全局默认值
            # 用户级覆盖（target_minutes / interval / continuity）
            if "target_minutes" in u:
                cfg.target_minutes = int(u["target_minutes"])
            if "interval" in u:
                lo2, _, hi2 = str(u["interval"]).partition("-")
                cfg.interval_lo, cfg.interval_hi = float(lo2), float(hi2 or lo2)
            _, cookies_u, ps, pc = _parse_curl(cookie)
            cfg.cookies = cookies_u
            if ps: cfg.ps = ps
            if pc: cfg.pc = pc
            result.append((name, cfg))
        if result:
            return result
        log.warning("YAML users 列表为空或全部无效，回退到环境变量")

    # ── 单用户模式：环境变量 ─────────────────────────────────────────────────
    cfg = Config()
    _apply_yaml_global(cfg, raw)
    _apply_env(cfg)
    name = os.getenv("WXREAD_USER_NAME", "default")
    return [(name, cfg)]


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
        self._reset_cookies()

    def _reset_cookies(self):
        """清空 Session 中所有 cookie 再按 cfg.cookies 重新写入，避免 renewal 后出现重名 cookie"""
        self.sess.cookies.clear()
        for k, v in self.cfg.cookies.items():
            # .weread.qq.com（含前导点）才能匹配 renewal 返回的 Set-Cookie，避免重名 cookie
            self.sess.cookies.set(k, v, domain=".weread.qq.com", path="/")

    # ── Cookie 刷新 ───────────────────────────────────────────────────────────
    def refresh_cookie(self) -> bool:
        payload = {"rq": "%2Fweb%2Fbook%2Fread", "ql": True}
        try:
            resp = self.sess.post(RENEW_URL, json=payload, timeout=15)
            data = resp.json() if resp.text.strip().startswith("{") else {}
            # 服务端返回错误（如 -2013 鉴权失败）时会同时通过 Set-Cookie 清空认证 cookie
            # 必须立即 _reset_cookies() 恢复，否则后续阅读请求全部失败
            if data.get("errCode", 0) != 0:
                self._reset_cookies()
                log.warning("Cookie 刷新失败（errCode=%s %s）", data.get("errCode"), data.get("errMsg", ""))
                return False
            new_key = None
            # 逐段解析 Set-Cookie，找到 wr_skey
            for segment in resp.headers.get("Set-Cookie", "").split(","):
                for part in segment.split(";"):
                    part = part.strip()
                    if part.startswith("wr_skey="):
                        new_key = part.split("=", 1)[1].strip()[:8]
                        break
                if new_key:
                    break
            if new_key:
                self.cfg.cookies["wr_skey"] = new_key
                self._reset_cookies()          # 清掉重名 cookie，重新写入
                log.info("Cookie 已刷新: wr_skey=%s", new_key)
                return True
        except Exception as e:
            self._reset_cookies()              # 异常时同样恢复，防止半途清空
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
        """
        获取指定书籍的章节列表，返回 [{c, ci}]。
        注意：chapterUid 为整数的商业书籍不被 /web/book/read 接受，跳过此类书。
        """
        try:
            resp = self.sess.post(
                CHAPTER_URL,
                json={"bookIds": [book_id]},
                timeout=15,
            )
            data = resp.json()
            chapters = []
            for entry in data.get("data", []):
                if entry.get("soldOut"):
                    return []  # 已下架，跳过
                for ch in entry.get("updated", []):
                    cid = ch.get("chapterUid", "")
                    ci  = int(ch.get("chapterIdx", 1))
                    # 只接受十六进制字符串格式的 chapterUid（整数型书籍 read API 无效）
                    if isinstance(cid, str) and len(cid) > 8:
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
    - continuity 概率：继续读当前书的下一章（顺序推进）
    - 1-continuity 概率：随机跳到其他书的随机章节
    - 强制避免连续两次选同一 (book, chapter)，防触发服务端反重复检测
    """
    for _ in range(10):   # 最多尝试 10 次，确保不重复
        if books and random.random() < continuity and 0 <= prev_bi < len(books):
            bi = prev_bi
            chapters = books[bi]["chapters"]
            if chapters:
                if random.random() < continuity and prev_ci + 1 < len(chapters):
                    ci = prev_ci + 1
                else:
                    ci = random.randrange(len(chapters))
            else:
                bi = random.randrange(len(books))
                ci = 0
        else:
            bi = random.randrange(len(books))
            chapters = books[bi]["chapters"]
            ci = random.randrange(len(chapters)) if chapters else 0

        # 避免与上一次完全相同
        if (bi, ci) != (prev_bi, prev_ci):
            return bi, ci

    # 兜底：直接取下一个位置
    bi = prev_bi
    ci = (prev_ci + 1) % len(books[bi]["chapters"])
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


# ── 单用户阅读流程 ─────────────────────────────────────────────────────────────
def run_one_user(name: str, cfg: "Config") -> str:
    """
    为单个用户执行完整阅读流程。
    返回结果摘要字符串（供多用户汇总推送）。
    """
    prefix = f"[{name}] " if name and name != "default" else ""
    log.info("%s── 开始 ──────────────────────────", prefix)

    client = WeReadClient(cfg)

    # 1. 刷新 Cookie
    client.refresh_cookie()

    # 2. 拉取书架 & 章节
    shelf_books = client.fetch_shelf()
    shelf_books = shelf_books[: cfg.max_shelf_books]
    for book in shelf_books:
        chapters = client.fetch_chapters(book["book_id"])
        if chapters:
            book["chapters"] = chapters[: cfg.max_chapters]
            log.info("%s  《%s》 载入 %d 章", prefix, book["title"], len(book["chapters"]))

    shelf_books = [b for b in shelf_books if b.get("chapters")]
    if shelf_books:
        log.info("%s书架可用 %d 本", prefix, len(shelf_books))
    else:
        log.info("%s书架无可用书籍，仅使用兜底书籍", prefix)

    # 3. 书池 = 书架可用书 + 兜底书籍
    books = shelf_books + FALLBACK_BOOKS
    log.info("%s共 %d 本书", prefix, len(books))

    # 4. 阅读循环
    total   = max(1, int(cfg.target_minutes * 60 / 30))
    log.info("%s目标 %d 分钟，共 %d 次请求", prefix, cfg.target_minutes, total)

    success = 0
    prev_bi = prev_ci = 0
    last_ts = int(time.time()) - 35

    for idx in range(1, total + 1):
        bi, ci = _pick(books, prev_bi, prev_ci, cfg.continuity)
        book   = books[bi]
        ch     = book["chapters"][ci]

        ok, last_ts = client.read_once(book["book_id"], ch, last_ts)

        if ok:
            success += 1
            prev_bi, prev_ci = bi, ci
            log.info("%s[%d/%d] ✅  《%s》第%d章  累计 %.1f min",
                     prefix, idx, total, book["title"], ch["ci"], success * 0.5)
        else:
            log.warning("%s[%d/%d] ❌  请求失败", prefix, idx, total)

        if idx < total:
            time.sleep(random.uniform(cfg.interval_lo, cfg.interval_hi))

    # 5. 结果
    actual_min = success * 0.5
    summary = f"{prefix}完成 {success}/{total} 次，约 {actual_min:.1f} 分钟"
    log.info("%s🎉 %s", prefix, summary)
    return summary


# ── 主流程 ─────────────────────────────────────────────────────────────────────
def main():
    _setup_logging()

    users = load_users()

    if not users or not any(cfg.cookies for _, cfg in users):
        log.error(
            "未检测到 Cookie！\n"
            "  本地：在 config.yaml 中配置 users 列表，或\n"
            "  GitHub Action：设置 WXREAD_COOKIE secret"
        )
        sys.exit(1)

    all_results = []
    push_cfg = None  # 取第一个有推送配置的用户做汇总推送

    for name, cfg in users:
        if not cfg.cookies:
            log.warning("[%s] 无 cookie，跳过", name)
            continue
        result = run_one_user(name, cfg)
        all_results.append(result)
        if not push_cfg and cfg.push_method:
            push_cfg = cfg

    # 汇总推送（多用户时合并为一条消息）
    if all_results and push_cfg:
        msg = "微信读书汇总\n" + "\n".join(all_results)
        push_notify(push_cfg, msg)


if __name__ == "__main__":
    main()
