import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from functools import wraps

import bcrypt
from flask import Flask, request, jsonify, send_from_directory, session
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

# ── 配置 ──────────────────────────────────────────────────────────
TZ_CN = timezone(timedelta(hours=8))

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    NORMALIZED_DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    NORMALIZED_DATABASE_URL = f"sqlite:///diary_app.db"

ENGINE = create_engine(NORMALIZED_DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.getenv("SECRET_KEY", "find-your-memory-secret-2024")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False  # 本地开发用 False，线上改 True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)


# ── 数据库初始化 ──────────────────────────────────────────────────
def sqlite_pk():
    if "sqlite" in NORMALIZED_DATABASE_URL:
        return "INTEGER PRIMARY KEY AUTOINCREMENT"
    return "SERIAL PRIMARY KEY"


def init_db():
    with ENGINE.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS users (
                id {sqlite_pk()},
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                avatar TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS diaries (
                id {sqlite_pk()},
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT DEFAULT '日记',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """))
    # 列迁移（独立事务）
    for sql in [
        "ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT NULL",
        "ALTER TABLE diaries ADD COLUMN source TEXT DEFAULT '日记'",
        "ALTER TABLE diaries ADD COLUMN images TEXT DEFAULT NULL",
    ]:
        try:
            with ENGINE.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass


# ── 认证工具 ──────────────────────────────────────────────────────
def password_hash(raw: str) -> str:
    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()


def verify_password(raw: str, stored: str) -> bool:
    try:
        if stored.startswith("$2"):
            return bcrypt.checkpw(raw.encode(), stored.encode())
        if len(stored) == 64:
            return hashlib.sha256(raw.encode()).hexdigest() == stored
    except Exception:
        pass
    return False


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "未登录"}), 401
        return f(*args, **kwargs)
    return decorated


def current_user_id():
    return session.get("user_id")


# ── API：认证 ─────────────────────────────────────────────────────
@app.route("/api/register", methods=["POST"])
def register():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少6位"}), 400
    try:
        with ENGINE.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (username, password_hash) VALUES (:u, :p)"
            ), {"u": username, "p": password_hash(password)})
        return jsonify({"ok": True})
    except IntegrityError:
        return jsonify({"error": "用户名已存在"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    with ENGINE.begin() as conn:
        row = conn.execute(text(
            "SELECT id, username, password_hash, avatar FROM users WHERE username=:u"
        ), {"u": username}).mappings().fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return jsonify({"error": "用户名或密码错误"}), 401
    session.permanent = True
    session["user_id"] = row["id"]
    session["username"] = row["username"]
    return jsonify({
        "ok": True,
        "username": row["username"],
        "avatar": row["avatar"],
    })


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    if "user_id" not in session:
        return jsonify({"loggedIn": False})
    with ENGINE.begin() as conn:
        row = conn.execute(text(
            "SELECT username, avatar FROM users WHERE id=:uid"
        ), {"uid": session["user_id"]}).mappings().fetchone()
    if not row:
        session.clear()
        return jsonify({"loggedIn": False})
    return jsonify({
        "loggedIn": True,
        "username": row["username"],
        "avatar": row["avatar"],
    })


@app.route("/api/avatar", methods=["POST"])
@login_required
def update_avatar():
    data = request.json or {}
    avatar_b64 = data.get("avatar", "")
    with ENGINE.begin() as conn:
        conn.execute(text(
            "UPDATE users SET avatar=:a WHERE id=:uid"
        ), {"a": avatar_b64, "uid": current_user_id()})
    return jsonify({"ok": True})


# ── API：日记 ─────────────────────────────────────────────────────
@app.route("/api/diaries/today")
@login_required
def today_memories():
    now = datetime.now(TZ_CN)
    month, day = now.month, now.day
    with ENGINE.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, date, time, content, images
            FROM diaries
            WHERE user_id=:uid
              AND date LIKE :pattern
            ORDER BY date DESC, time DESC
        """), {"uid": current_user_id(), "pattern": f"%-{month:02d}-{day:02d}"}).mappings().all()
    return jsonify([dict(r) for r in rows])


@app.route("/api/diaries/search")
@login_required
def search_diaries():
    keyword = (request.args.get("q") or "").strip()
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    uid = current_user_id()

    sql = "SELECT id, date, time, content, images FROM diaries WHERE user_id=:uid"
    params = {"uid": uid}

    if keyword:
        sql += " AND content LIKE :kw"
        params["kw"] = f"%{keyword}%"
    if date_from:
        sql += " AND date >= :from"
        params["from"] = date_from
    if date_to:
        sql += " AND date <= :to"
        params["to"] = date_to

    sql += " ORDER BY date DESC, time DESC LIMIT 100"

    with ENGINE.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return jsonify([dict(r) for r in rows])


@app.route("/api/diaries/date/<date_str>")
@login_required
def diaries_by_date(date_str):
    with ENGINE.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, date, time, content, images FROM diaries
            WHERE user_id=:uid AND date=:d
            ORDER BY time ASC
        """), {"uid": current_user_id(), "d": date_str}).mappings().all()
    return jsonify([dict(r) for r in rows])


@app.route("/api/diaries", methods=["POST"])
@login_required
def create_diary():
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "内容不能为空"}), 400
    now_cn = datetime.now(TZ_CN)
    d = data.get("date") or now_cn.strftime("%Y-%m-%d")
    t = data.get("time") or now_cn.strftime("%H:%M:%S")
    images = data.get("images") or None  # JSON array string
    with ENGINE.begin() as conn:
        conn.execute(text("""
            INSERT INTO diaries (user_id, date, time, content, source, images)
            VALUES (:uid, :d, :t, :c, :s, :img)
        """), {"uid": current_user_id(), "d": d, "t": t, "c": content, "s": "日记", "img": images})
    return jsonify({"ok": True})


@app.route("/api/diaries/<int:diary_id>", methods=["PUT"])
@login_required
def update_diary(diary_id):
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "内容不能为空"}), 400
    images = data.get("images")  # None = don't change; string = update
    with ENGINE.begin() as conn:
        if images is not None:
            result = conn.execute(text("""
                UPDATE diaries SET content=:c, images=:img WHERE id=:id AND user_id=:uid
            """), {"c": content, "id": diary_id, "uid": current_user_id(), "img": images})
        else:
            result = conn.execute(text("""
                UPDATE diaries SET content=:c WHERE id=:id AND user_id=:uid
            """), {"c": content, "id": diary_id, "uid": current_user_id()})
    if result.rowcount == 0:
        return jsonify({"error": "未找到"}), 404
    return jsonify({"ok": True})


@app.route("/api/diaries/<int:diary_id>", methods=["DELETE"])
@login_required
def delete_diary(diary_id):
    with ENGINE.begin() as conn:
        result = conn.execute(text(
            "DELETE FROM diaries WHERE id=:id AND user_id=:uid"
        ), {"id": diary_id, "uid": current_user_id()})
    if result.rowcount == 0:
        return jsonify({"error": "未找到"}), 404
    return jsonify({"ok": True})


@app.route("/api/diaries/import", methods=["POST"])
@login_required
def import_diaries():
    records = request.json or []
    if not isinstance(records, list):
        return jsonify({"error": "格式错误"}), 400
    uid = current_user_id()
    inserted = 0
    with ENGINE.begin() as conn:
        existing = conn.execute(text(
            "SELECT date, time, content FROM diaries WHERE user_id=:uid"
        ), {"uid": uid}).mappings().all()
        existing_keys = {(r["date"], r["time"], r["content"]) for r in existing}
        for item in records:
            d = str(item.get("date", "")).strip()
            t = str(item.get("time", "")).strip() or "00:00:00"
            c = str(item.get("content", "")).strip()
            if not d or not c:
                continue
            if (d, t, c) in existing_keys:
                continue
            existing_keys.add((d, t, c))
            conn.execute(text("""
                INSERT INTO diaries (user_id, date, time, content, source)
                VALUES (:uid, :d, :t, :c, :s)
            """), {"uid": uid, "d": d, "t": t, "c": c, "s": item.get("source", "历史导入")})
            inserted += 1
    return jsonify({"ok": True, "inserted": inserted})


@app.route("/api/stats/monthly")
@login_required
def monthly_stats():
    month = request.args.get("month", "")
    if not month:
        month = datetime.now(TZ_CN).strftime("%Y-%m")
    with ENGINE.begin() as conn:
        rows = conn.execute(text("""
            SELECT date, COUNT(*) as count
            FROM diaries
            WHERE user_id=:uid AND date LIKE :m
            GROUP BY date ORDER BY date
        """), {"uid": current_user_id(), "m": f"{month}%"}).mappings().all()
    return jsonify([dict(r) for r in rows])


# ── 前端入口 ──────────────────────────────────────────────────────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path and (Path(app.static_folder) / path).exists():
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
