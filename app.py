#!/usr/bin/env python3
"""微信读书自动阅读 - Web 界面服务端（多用户 + SQLite 持久化）"""
import sys, os, threading, time, random, logging, sqlite3, base64

from flask import Flask, request, jsonify, render_template

sys.path.insert(0, ".")
from main import (
    Config, _parse_curl, WeReadClient, FALLBACK_BOOKS, _pick,
    DEFAULT_PS, DEFAULT_PC,
)

app     = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wxread.db")

# ── 数据库 ─────────────────────────────────────────────────────────────────────
def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _db()
    # 1. 建表（不含依赖新列的索引）
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        target_minutes INTEGER NOT NULL,
        interval_str   TEXT    NOT NULL,
        status         TEXT    NOT NULL DEFAULT 'running',
        progress_done  INTEGER DEFAULT 0,
        progress_total INTEGER DEFAULT 0,
        created_at     REAL    NOT NULL,
        finished_at    REAL
    );
    CREATE TABLE IF NOT EXISTS session_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        line       TEXT    NOT NULL,
        created_at REAL    NOT NULL
    );
    CREATE TABLE IF NOT EXISTS saved_config (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_logs_sid ON session_logs(session_id);
    """)
    conn.commit()
    # 2. 迁移旧表：补充 client_id 列（已存在则忽略）
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN client_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # 3. 建依赖新列的索引（迁移完成后才安全执行）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_cid ON sessions(client_id)")
    conn.commit()
    conn.close()


def db_create_session(target_minutes: int, interval_str: str, client_id: str = "") -> int:
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO sessions (client_id,target_minutes,interval_str,created_at) VALUES(?,?,?,?)",
            (client_id, target_minutes, interval_str, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def db_add_log(session_id: int, line: str):
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO session_logs (session_id,line,created_at) VALUES(?,?,?)",
            (session_id, line, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def db_update_progress(session_id: int, done: int, total: int):
    conn = _db()
    try:
        conn.execute(
            "UPDATE sessions SET progress_done=?,progress_total=? WHERE id=?",
            (done, total, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def db_finish_session(session_id: int, status: str):
    conn = _db()
    try:
        conn.execute(
            "UPDATE sessions SET status=?,finished_at=? WHERE id=?",
            (status, time.time(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def db_get_session_by_id(session_id: int):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def db_get_logs(session_id: int, offset: int = 0) -> list:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT line FROM session_logs WHERE session_id=? ORDER BY id LIMIT -1 OFFSET ?",
            (session_id, offset),
        ).fetchall()
        return [r["line"] for r in rows]
    finally:
        conn.close()


def db_get_recent_sessions(limit: int = 10, client_id: str = "") -> list:
    """按 client_id 隔离各用户历史；client_id 为空时返回所有（管理用）"""
    conn = _db()
    try:
        if client_id:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE client_id=? ORDER BY id DESC LIMIT ?",
                (client_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def db_set_config(key: str, value: str):
    conn = _db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO saved_config (key,value,updated_at) VALUES(?,?,?)",
            (key, value, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def db_get_config(key: str, default: str = "") -> str:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT value FROM saved_config WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


# ── 多用户会话管理 ──────────────────────────────────────────────────────────────
_lock     = threading.Lock()
_sessions: dict = {}   # session_id(int) -> WebReadSession

CLEANUP_AFTER = 7200   # 完成 2 小时后从内存移除


def _get_session(session_id: int):
    with _lock:
        return _sessions.get(int(session_id))


def _cleanup_old():
    """惰性清理：移除完成超过 2 小时的会话，防止内存无限增长"""
    cutoff = time.time() - CLEANUP_AFTER
    with _lock:
        stale = [sid for sid, s in _sessions.items()
                 if s.finished and (s.finish_time or 0) < cutoff]
        for sid in stale:
            del _sessions[sid]


# ── 会话类 ─────────────────────────────────────────────────────────────────────
class WebReadSession:
    def __init__(self, cfg: Config):
        self.cfg        = cfg
        self.stop_event = threading.Event()
        self._logs: list = []
        self._llock      = threading.Lock()
        self.progress    = {"done": 0, "total": 0}
        self.finished    = False
        self.finish_time = None
        self.session_id  = None

    def _emit(self, msg: str):
        ts   = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._llock:
            self._logs.append(line)
        if self.session_id:
            db_add_log(self.session_id, line)

    def logs_from(self, offset: int) -> list:
        with self._llock:
            return self._logs[offset:]

    def run(self):
        cfg    = self.cfg
        status = "error"
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
            if self.session_id:
                db_update_progress(self.session_id, 0, total)
            self._emit(f"⏱ 目标 {cfg.target_minutes} 分钟，共 {total} 次请求")

            ok_cnt  = 0
            prev_bi = prev_ci = 0
            last_ts = int(time.time()) - 35

            for idx in range(1, total + 1):
                if self.stop_event.is_set():
                    self._emit("⏹ 已手动停止")
                    break

                bi, ci = _pick(books, prev_bi, prev_ci, cfg.continuity)
                b  = books[bi]
                ch = b["chapters"][ci]

                ok, last_ts = client.read_once(b["book_id"], ch, last_ts)
                if ok:
                    ok_cnt += 1
                    prev_bi, prev_ci = bi, ci
                    self.progress["done"] = ok_cnt
                    if self.session_id:
                        db_update_progress(self.session_id, ok_cnt, total)
                    self._emit(
                        f"✅ [{idx}/{total}] 《{b['title']}》第{ch['ci']}章"
                        f"  已累计 {ok_cnt * 0.5:.1f} min"
                    )
                else:
                    self._emit(f"❌ [{idx}/{total}] 请求失败，稍后重试")

                if idx < total and not self.stop_event.is_set():
                    wait = random.uniform(cfg.interval_lo, cfg.interval_hi)
                    end  = time.time() + wait
                    while time.time() < end:
                        if self.stop_event.is_set():
                            break
                        time.sleep(0.5)

            actual = ok_cnt * 0.5
            self._emit(f"🎉 完成！成功 {ok_cnt}/{total} 次，约 {actual:.1f} 分钟")
            status = "stopped" if self.stop_event.is_set() else "done"

        except Exception as e:
            self._emit(f"❌ 运行异常: {e}")
            status = "error"
        finally:
            if self.session_id:
                db_finish_session(self.session_id, status)
            self.finish_time = time.time()
            self.finished    = True

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
    data       = request.get_json() or {}
    cookie_str = (data.get("cookie") or "").strip()
    if not cookie_str:
        return jsonify({"error": "Cookie 不能为空"}), 400

    try:
        hdrs, cookies, ps, pc = _parse_curl(cookie_str)
    except Exception as e:
        return jsonify({"error": f"Cookie 解析失败: {e}"}), 400
    if not cookies:
        return jsonify({"error": "未能解析到 Cookie，请检查格式"}), 400

    target       = max(1, min(360, int(data.get("target_minutes", 60))))
    interval_str = str(data.get("interval", "28-35"))
    lo, _, hi    = interval_str.partition("-")
    try:
        ilo = float(lo.strip());  ihi = float((hi or lo).strip())
    except ValueError:
        ilo, ihi = 28.0, 35.0

    client_id = (data.get("client_id") or "")[:64]

    # 保存非敏感配置（cookie 由前端 localStorage 管理，不落库）
    db_set_config("target_minutes", str(target))
    db_set_config("interval",       interval_str)

    cfg = Config(
        cookies=cookies, headers=hdrs,
        ps=ps or DEFAULT_PS, pc=pc or DEFAULT_PC,
        target_minutes=target, interval_lo=ilo, interval_hi=ihi,
    )

    session_id      = db_create_session(target, interval_str, client_id)
    sess            = WebReadSession(cfg)
    sess.session_id = session_id
    with _lock:
        _sessions[session_id] = sess
    sess.start()
    _cleanup_old()

    return jsonify({"ok": True, "session_id": session_id})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data       = request.get_json() or {}
    session_id = data.get("session_id")
    if session_id:
        s = _get_session(int(session_id))
        if s:
            s.stop()
    return jsonify({"ok": True})


@app.route("/api/poll")
def api_poll():
    session_id = request.args.get("session_id")
    offset     = int(request.args.get("offset", 0))

    if not session_id:
        return jsonify({"lines": [], "offset": 0, "done": False,
                        "running": False, "progress_done": 0, "progress_total": 0})

    s = _get_session(int(session_id))
    if s:
        lines   = s.logs_from(offset)
        running = not s.stop_event.is_set() and not s.finished
        return jsonify({
            "lines":          lines,
            "offset":         offset + len(lines),
            "done":           s.finished,
            "running":        running,
            "progress_done":  s.progress["done"],
            "progress_total": s.progress["total"],
        })

    # 内存中不存在（服务重启）→ 从 DB 补全剩余日志
    row = db_get_session_by_id(int(session_id))
    if not row:
        return jsonify({"lines": [], "offset": offset, "done": True,
                        "running": False, "progress_done": 0, "progress_total": 0})
    lines = db_get_logs(int(session_id), offset)
    return jsonify({
        "lines":          lines,
        "offset":         offset + len(lines),
        "done":           True,
        "running":        False,
        "progress_done":  row["progress_done"],
        "progress_total": row["progress_total"],
    })


@app.route("/api/restore")
def api_restore():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"has_session": False})

    sid = int(session_id)
    s   = _get_session(sid)
    if s:
        logs   = s.logs_from(0)
        status = ("done"    if s.finished
                  else "stopped" if s.stop_event.is_set()
                  else "running")
        return jsonify({
            "has_session":    True,
            "running":        status == "running",
            "status":         status,
            "session_id":     sid,
            "progress_done":  s.progress["done"],
            "progress_total": s.progress["total"],
            "logs":           logs,
            "offset":         len(logs),
        })

    row = db_get_session_by_id(sid)
    if not row:
        return jsonify({"has_session": False})

    if row["status"] == "running":
        db_finish_session(sid, "error")
        row["status"] = "error"

    logs = db_get_logs(sid)
    if row["status"] == "error":
        logs.append("[--:--:--] ⚠️ 检测到服务重启，上次任务已中断")

    return jsonify({
        "has_session":    True,
        "running":        False,
        "status":         row["status"],
        "session_id":     sid,
        "progress_done":  row["progress_done"],
        "progress_total": row["progress_total"],
        "logs":           logs,
        "offset":         len(logs),
    })


@app.route("/api/history")
def api_history():
    client_id = request.args.get("client_id", "")
    sessions  = db_get_recent_sessions(10, client_id)
    result    = []
    for s in sessions:
        result.append({
            "id":             s["id"],
            "target_minutes": s["target_minutes"],
            "status":         s["status"],
            "progress_done":  s["progress_done"],
            "progress_total": s["progress_total"],
            "created_str":    time.strftime("%m-%d %H:%M", time.localtime(s["created_at"])),
            "duration_min":   round(s["progress_done"] * 0.5, 1),
        })
    return jsonify(result)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """只返回非敏感的参数配置；cookie 由前端 localStorage 自行管理"""
    return jsonify({
        "target_minutes": int(db_get_config("target_minutes", "60")),
        "interval":       db_get_config("interval", "28-35"),
    })


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    init_db()
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 微信读书 Web 界面已启动：http://localhost:{port}")
    print(f"   支持多用户同时运行，每个浏览器标签独立会话")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
