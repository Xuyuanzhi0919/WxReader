#!/usr/bin/env python3
"""微信读书自动阅读 - Web 界面服务端"""
import sys
import threading
import time
import random
import os
import json
import logging

from flask import Flask, request, jsonify, render_template, Response, stream_with_context

sys.path.insert(0, ".")
from main import (
    Config, _parse_curl, WeReadClient, FALLBACK_BOOKS, _pick,
    DEFAULT_PS, DEFAULT_PC,
)

app = Flask(__name__)

_lock = threading.Lock()
_session = None   # type: WebReadSession | None


# ── 会话类 ─────────────────────────────────────────────────────────────────────
class WebReadSession:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self._logs: list[str] = []
        self._llock = threading.Lock()
        self.progress = {"done": 0, "total": 0}
        self.finished = False

    def _emit(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        with self._llock:
            self._logs.append(f"[{ts}] {msg}")

    def logs_from(self, offset: int) -> list[str]:
        with self._llock:
            return self._logs[offset:]

    def run(self):
        cfg = self.cfg
        try:
            self._emit("🔐 正在验证登录状态...")
            client = WeReadClient(cfg)
            client.refresh_cookie()
            self._emit("✅ 登录验证完成")

            self._emit("📚 正在加载书架...")
            shelf = client.fetch_shelf()[: cfg.max_shelf_books]
            for b in shelf:
                if self.stop_event.is_set():
                    break
                chs = client.fetch_chapters(b["book_id"])
                if chs:
                    b["chapters"] = chs[: cfg.max_chapters]
                    self._emit(f"  《{b['title']}》载入 {len(b['chapters'])} 章")

            shelf = [b for b in shelf if b.get("chapters")]
            books = shelf + FALLBACK_BOOKS
            self._emit(f"📖 共 {len(books)} 本书可用，开始阅读...")

            total = max(1, int(cfg.target_minutes * 60 / 30))
            self.progress["total"] = total
            self._emit(f"⏱ 目标 {cfg.target_minutes} 分钟，共 {total} 次请求")

            ok_cnt = 0
            prev_bi = prev_ci = 0
            last_ts = int(time.time()) - 35

            for idx in range(1, total + 1):
                if self.stop_event.is_set():
                    self._emit("⏹ 已手动停止")
                    break

                bi, ci = _pick(books, prev_bi, prev_ci, cfg.continuity)
                b = books[bi]
                ch = b["chapters"][ci]

                ok, last_ts = client.read_once(b["book_id"], ch, last_ts)

                if ok:
                    ok_cnt += 1
                    prev_bi, prev_ci = bi, ci
                    self.progress["done"] = ok_cnt
                    self._emit(
                        f"✅ [{idx}/{total}] 《{b['title']}》第{ch['ci']}章"
                        f"  累计 {ok_cnt * 0.5:.1f} 分钟"
                    )
                else:
                    self._emit(f"❌ [{idx}/{total}] 请求失败，稍后重试")

                if idx < total and not self.stop_event.is_set():
                    wait = random.uniform(cfg.interval_lo, cfg.interval_hi)
                    end = time.time() + wait
                    while time.time() < end:
                        if self.stop_event.is_set():
                            break
                        time.sleep(0.5)

            actual = ok_cnt * 0.5
            self._emit(f"🎉 完成！成功 {ok_cnt}/{total} 次，约 {actual:.1f} 分钟")

        except Exception as e:
            self._emit(f"❌ 运行异常: {e}")
        finally:
            self.finished = True

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.stop_event.set()


# ── 路由 ───────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    global _session
    with _lock:
        if _session and not _session.finished and not _session.stop_event.is_set():
            return jsonify({"error": "已有任务运行中，请先停止"}), 400

    data = request.get_json() or {}
    cookie_str = (data.get("cookie") or "").strip()
    if not cookie_str:
        return jsonify({"error": "Cookie 不能为空"}), 400

    try:
        hdrs, cookies, ps, pc = _parse_curl(cookie_str)
    except Exception as e:
        return jsonify({"error": f"Cookie 解析失败: {e}"}), 400

    if not cookies:
        return jsonify({"error": "未能解析到 Cookie，请检查格式"}), 400

    target = max(1, min(360, int(data.get("target_minutes", 60))))
    interval_str = str(data.get("interval", "28-35"))
    lo, _, hi = interval_str.partition("-")
    try:
        ilo = float(lo.strip())
        ihi = float((hi or lo).strip())
    except ValueError:
        ilo, ihi = 28.0, 35.0

    cfg = Config(
        cookies=cookies,
        headers=hdrs,
        ps=ps or DEFAULT_PS,
        pc=pc or DEFAULT_PC,
        target_minutes=target,
        interval_lo=ilo,
        interval_hi=ihi,
    )

    sess = WebReadSession(cfg)
    with _lock:
        _session = sess
    sess.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _lock:
        s = _session
    if s:
        s.stop()
    return jsonify({"ok": True})


@app.route("/api/poll")
def api_poll():
    """轮询接口：返回新日志行 + 当前进度状态"""
    offset = int(request.args.get("offset", 0))
    with _lock:
        s = _session

    if not s:
        return jsonify({
            "lines": [], "offset": 0, "done": False,
            "running": False, "progress_done": 0, "progress_total": 0,
        })

    lines = s.logs_from(offset)
    running = not s.stop_event.is_set() and not s.finished
    return jsonify({
        "lines": lines,
        "offset": offset + len(lines),
        "done": s.finished,
        "running": running,
        "progress_done": s.progress["done"],
        "progress_total": s.progress["total"],
    })


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 微信读书 Web 界面已启动：http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
