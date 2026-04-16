import hashlib
import html
import io
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

import bcrypt
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

DB_PATH = Path(os.getenv("DB_PATH", "diary_app.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    # Supabase 常见前缀是 postgres://，SQLAlchemy 需要 postgresql://
    NORMALIZED_DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    NORMALIZED_DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"
ENGINE: Engine = create_engine(NORMALIZED_DATABASE_URL, pool_pre_ping=True)

st.set_page_config(page_title="微博日记本", page_icon="📔", layout="centered")


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: #FDF6E3; }
        .block-container { max-width: 980px; padding-top: 1.2rem; padding-bottom: 2rem; }
        .main-title { font-size: 1.9rem; font-weight: 700; color: #5B4636; margin-bottom: .2rem; }
        .sub-title { color: #8B6F5C; margin-bottom: 1rem; }
        .section-title { font-size: 1.2rem; font-weight: 700; color: #5B4636; margin: .3rem 0 .7rem 0; }
        .diary-card {
            background: #FFF9EF; border: 1px solid #F1E3C8; border-radius: 15px; padding: 14px 16px;
            margin-bottom: 12px; box-shadow: 0 4px 12px rgba(181, 137, 85, .08);
        }
        .meta-line { color: #9C7F63; font-size: .88rem; margin-bottom: 8px; }
        .content-line { color: #4A3A2A; line-height: 1.7; font-size: .98rem; word-wrap: break-word; }
        .hint-box {
            border-radius: 15px; background: #FFF3DD; padding: 11px 13px; color: #7E6048;
            border: 1px dashed #E9D0A7; margin-bottom: 10px;
        }
        @media (max-width: 768px) {
            .main-title { font-size: 1.55rem; }
            .section-title { font-size: 1.05rem; }
            .diary-card { padding: 12px 14px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def db_now_expression() -> str:
    return "CURRENT_TIMESTAMP"


def sqlite_autoincrement_type() -> str:
    if NORMALIZED_DATABASE_URL.startswith("sqlite:///"):
        return "INTEGER PRIMARY KEY AUTOINCREMENT"
    return "SERIAL PRIMARY KEY"


def init_db() -> None:
    with ENGINE.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    id {sqlite_autoincrement_type()},
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT DEFAULT {db_now_expression()}
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS diaries (
                    id {sqlite_autoincrement_type()},
                    user_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT DEFAULT 'Streamlit日记',
                    likes INTEGER DEFAULT 0,
                    comments INTEGER DEFAULT 0,
                    reposts INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT {db_now_expression()},
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
        )


def is_legacy_sha256(hash_value: str) -> bool:
    if len(hash_value) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in hash_value.lower())


def password_hash(raw_password: str) -> str:
    return bcrypt.hashpw(raw_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw_password: str, stored_hash: str) -> bool:
    try:
        # 新版 bcrypt
        if stored_hash.startswith("$2"):
            return bcrypt.checkpw(raw_password.encode("utf-8"), stored_hash.encode("utf-8"))
        # 兼容旧版 sha256
        if is_legacy_sha256(stored_hash):
            legacy_hash = hashlib.sha256(raw_password.encode("utf-8")).hexdigest()
            return legacy_hash == stored_hash
        return False
    except Exception:
        return False


def register_user(username: str, password: str) -> tuple[bool, str]:
    if not username.strip() or not password.strip():
        return False, "用户名和密码不能为空。"
    if len(password) < 6:
        return False, "密码至少 6 位。"
    try:
        with ENGINE.begin() as conn:
            conn.execute(
                text("INSERT INTO users (username, password_hash) VALUES (:username, :password_hash)"),
                {"username": username.strip(), "password_hash": password_hash(password)},
            )
        return True, "注册成功，请登录。"
    except IntegrityError:
        return False, "用户名已存在，请更换。"
    except Exception as e:
        return False, f"注册失败：{e}"


def login_user(username: str, password: str) -> Optional[dict]:
    with ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT id, username, password_hash FROM users WHERE username = :username"),
            {"username": username.strip()},
        ).mappings().fetchone()
        if not row:
            return None
        stored_hash = row["password_hash"]
        if not verify_password(password, stored_hash):
            return None

        # 老账号平滑升级：sha256 登录成功后自动改为 bcrypt
        if is_legacy_sha256(stored_hash):
            conn.execute(
                text("UPDATE users SET password_hash = :password_hash WHERE id = :user_id"),
                {"password_hash": password_hash(password), "user_id": row["id"]},
            )

    return {"id": row["id"], "username": row["username"]}


@st.cache_data(ttl=10)
def load_user_diaries(user_id: int) -> pd.DataFrame:
    with ENGINE.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, date, time, content, source, likes, comments, reposts
                FROM diaries
                WHERE user_id = :user_id
                ORDER BY date DESC, time DESC, id DESC
                """
            ),
            {"user_id": user_id},
        ).mappings().all()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        # 新账号没有数据时，也返回完整字段，避免页面访问 month/day 等列时报 KeyError。
        return pd.DataFrame(
            columns=[
                "id",
                "date",
                "time",
                "content",
                "source",
                "likes",
                "comments",
                "reposts",
                "date_only",
                "year",
                "month",
                "day",
            ]
        )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).copy()
    df["date_only"] = df["date"].dt.date
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    df["day"] = df["date"].dt.day
    return df


def import_json_for_user(user_id: int, source_file: Path) -> tuple[bool, str]:
    if not source_file.exists():
        return False, f"文件不存在：{source_file.name}"
    try:
        with source_file.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            return False, "JSON 格式应为列表。"

        inserted = 0
        with ENGINE.begin() as conn:
            for item in raw:
                d = str(item.get("date", "")).strip()
                t = str(item.get("time", "")).strip() or "00:00:00"
                content = str(item.get("content", "")).strip()
                if not d or not content:
                    continue
                exists = conn.execute(
                    text(
                        """
                        SELECT 1 FROM diaries
                        WHERE user_id=:user_id AND date=:date_v AND time=:time_v AND content=:content_v
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id, "date_v": d, "time_v": t, "content_v": content},
                ).first()
                if exists:
                    continue
                conn.execute(
                    text(
                        """
                        INSERT INTO diaries (user_id, date, time, content, source, likes, comments, reposts)
                        VALUES (:user_id, :date_v, :time_v, :content_v, :source_v, :likes_v, :comments_v, :reposts_v)
                        """
                    ),
                    {
                        "user_id": user_id,
                        "date_v": d,
                        "time_v": t,
                        "content_v": content,
                        "source_v": str(item.get("source", "微博导入")),
                        "likes_v": int(item.get("likes", 0) or 0),
                        "comments_v": int(item.get("comments", 0) or 0),
                        "reposts_v": int(item.get("reposts", 0) or 0),
                    },
                )
                inserted += 1
        load_user_diaries.clear()
        return True, f"导入完成，新增 {inserted} 条。"
    except Exception as e:
        return False, f"导入失败：{e}"


def import_json_records_for_user(user_id: int, records: list[dict]) -> tuple[bool, str]:
    """把前端上传的 JSON 记录导入当前用户。"""
    try:
        inserted = 0
        with ENGINE.begin() as conn:
            for item in records:
                d = str(item.get("date", "")).strip()
                t = str(item.get("time", "")).strip() or "00:00:00"
                content = str(item.get("content", "")).strip()
                if not d or not content:
                    continue
                exists = conn.execute(
                    text(
                        """
                        SELECT 1 FROM diaries
                        WHERE user_id=:user_id AND date=:date_v AND time=:time_v AND content=:content_v
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id, "date_v": d, "time_v": t, "content_v": content},
                ).first()
                if exists:
                    continue
                conn.execute(
                    text(
                        """
                        INSERT INTO diaries (user_id, date, time, content, source, likes, comments, reposts)
                        VALUES (:user_id, :date_v, :time_v, :content_v, :source_v, :likes_v, :comments_v, :reposts_v)
                        """
                    ),
                    {
                        "user_id": user_id,
                        "date_v": d,
                        "time_v": t,
                        "content_v": content,
                        "source_v": str(item.get("source", "微博导入")),
                        "likes_v": int(item.get("likes", 0) or 0),
                        "comments_v": int(item.get("comments", 0) or 0),
                        "reposts_v": int(item.get("reposts", 0) or 0),
                    },
                )
                inserted += 1
        load_user_diaries.clear()
        return True, f"导入完成，新增 {inserted} 条。"
    except Exception as e:
        return False, f"导入失败：{e}"


def create_diary(user_id: int, d: date, t: str, content: str, source: str) -> tuple[bool, str]:
    try:
        with ENGINE.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO diaries (user_id, date, time, content, source)
                    VALUES (:user_id, :date_v, :time_v, :content_v, :source_v)
                    """
                ),
                {
                    "user_id": user_id,
                    "date_v": d.strftime("%Y-%m-%d"),
                    "time_v": t or "00:00:00",
                    "content_v": content.strip(),
                    "source_v": source.strip() or "Streamlit日记",
                },
            )
        load_user_diaries.clear()
        return True, "保存成功。"
    except Exception as e:
        return False, f"保存失败：{e}"


def update_diary(user_id: int, diary_id: int, new_content: str) -> tuple[bool, str]:
    try:
        with ENGINE.begin() as conn:
            result = conn.execute(
                text("UPDATE diaries SET content=:content_v WHERE id=:diary_id AND user_id=:user_id"),
                {"content_v": new_content.strip(), "diary_id": diary_id, "user_id": user_id},
            )
        load_user_diaries.clear()
        if result.rowcount == 0:
            return False, "未找到该记录。"
        return True, "修改成功。"
    except Exception as e:
        return False, f"修改失败：{e}"


def delete_diary(user_id: int, diary_id: int) -> tuple[bool, str]:
    try:
        with ENGINE.begin() as conn:
            result = conn.execute(
                text("DELETE FROM diaries WHERE id=:diary_id AND user_id=:user_id"),
                {"diary_id": diary_id, "user_id": user_id},
            )
        load_user_diaries.clear()
        if result.rowcount == 0:
            return False, "未找到该记录。"
        return True, "删除成功。"
    except Exception as e:
        return False, f"删除失败：{e}"


def build_month_markdown(month_df: pd.DataFrame, selected_month: str) -> str:
    lines = [f"# 日记导出 {selected_month}", ""]
    sorted_df = month_df.sort_values(["date", "time"], ascending=[True, True])
    for _, row in sorted_df.iterrows():
        day_text = row["date"].strftime("%Y-%m-%d")
        time_text = str(row.get("time", "")).strip() or "未知时间"
        lines.append(f"## {day_text} {time_text}")
        lines.append("")
        lines.append(str(row.get("content", "")).strip() or "(空内容)")
        lines.append("")
    return "\n".join(lines)


def try_build_month_pdf(markdown_text: str) -> tuple[bytes | None, str | None]:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import simpleSplit
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
    except Exception:
        return None, "当前环境未安装 reportlab，已提供 Markdown 导出。"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        width, height = A4
        x, y = 40, height - 50
        c.setFont("STSong-Light", 16)
        c.drawString(x, y, "微博日记导出")
        y -= 28
        c.setFont("STSong-Light", 12)
        for line in markdown_text.splitlines():
            wrapped = simpleSplit(line if line else " ", "STSong-Light", 12, width - 80) or [" "]
            for seg in wrapped:
                if y < 50:
                    c.showPage()
                    c.setFont("STSong-Light", 12)
                    y = height - 50
                c.drawString(x, y, seg)
                y -= 18
        c.save()
        return buf.getvalue(), None
    except Exception as e:
        return None, f"PDF 生成失败：{e}"


def highlight_content(text: str, keywords: list[str] | None = None) -> str:
    rendered = html.escape(text)
    if not keywords:
        return rendered.replace("\n", "<br>")
    for word in keywords:
        if not word:
            continue
        pattern = re.compile(re.escape(html.escape(word)), flags=re.IGNORECASE)
        rendered = pattern.sub(
            lambda m: f"<mark style='background:#FFE6A7; padding:0 2px; border-radius:4px;'>{m.group(0)}</mark>",
            rendered,
        )
    return rendered.replace("\n", "<br>")


def render_diary_card(row: pd.Series, keywords: list[str] | None = None) -> None:
    content = highlight_content(str(row.get("content", "")), keywords=keywords)
    day_text = row["date"].strftime("%Y-%m-%d")
    time_text = str(row.get("time", "")).strip() or "未知时间"
    meta = (
        f"{day_text} {time_text} · ❤️ {int(row.get('likes', 0) or 0)} "
        f"· 💬 {int(row.get('comments', 0) or 0)} · 🔁 {int(row.get('reposts', 0) or 0)}"
    )
    st.markdown(
        f"""
        <div class="diary-card">
            <div class="meta-line">{meta}</div>
            <div class="content-line">{content}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_auth_panel() -> None:
    st.markdown('<div class="main-title">📔 微博日记本</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-title">支持多账号隔离，手机也可访问</div>', unsafe_allow_html=True)
    login_tab, register_tab = st.tabs(["登录", "注册"])
    with login_tab:
        with st.form("login_form"):
            username = st.text_input("用户名")
            password = st.text_input("密码", type="password")
            ok = st.form_submit_button("登录")
        if ok:
            user = login_user(username, password)
            if user:
                st.session_state["user"] = user
                st.success("登录成功")
                st.rerun()
            else:
                st.error("用户名或密码错误。")
    with register_tab:
        with st.form("register_form"):
            username = st.text_input("新用户名")
            password = st.text_input("新密码（至少6位）", type="password")
            ok = st.form_submit_button("创建账户")
        if ok:
            success, msg = register_user(username, password)
            if success:
                st.success(msg)
            else:
                st.error(msg)


def render_today_memory(df: pd.DataFrame) -> None:
    st.markdown('<div class="section-title">🌟 那年今日</div>', unsafe_allow_html=True)
    today = date.today()
    same_day = df[(df["month"] == today.month) & (df["day"] == today.day)].copy()
    if same_day.empty:
        st.markdown('<div class="hint-box">今天暂无历史记录。</div>', unsafe_allow_html=True)
        return
    st.markdown(f'<div class="hint-box">找到 <b>{len(same_day)}</b> 条</div>', unsafe_allow_html=True)
    for _, row in same_day.iterrows():
        render_diary_card(row)


def render_date_search(df: pd.DataFrame) -> None:
    st.markdown('<div class="section-title">📅 日历搜索</div>', unsafe_allow_html=True)
    selected_day = st.date_input("选择日期", value=date.today(), format="YYYY-MM-DD")
    result = df[df["date_only"] == selected_day].copy()
    if result.empty:
        st.markdown(f'<div class="hint-box">{selected_day} 暂无记录。</div>', unsafe_allow_html=True)
        return
    st.markdown(f'<div class="hint-box">共 {len(result)} 条</div>', unsafe_allow_html=True)
    for _, row in result.iterrows():
        render_diary_card(row)


def render_keyword_search(df: pd.DataFrame) -> None:
    st.markdown('<div class="section-title">🔎 关键词搜索</div>', unsafe_allow_html=True)
    keyword = st.text_input("输入关键词（可空格分隔）", placeholder="例如：学习 海边")
    if not keyword.strip():
        st.markdown('<div class="hint-box">输入关键词后开始搜索。</div>', unsafe_allow_html=True)
        return
    tokens = [k.strip() for k in keyword.split() if k.strip()]
    mask = pd.Series(True, index=df.index)
    for token in tokens:
        mask = mask & df["content"].str.contains(token, case=False, na=False)
    result = df[mask].copy()
    if result.empty:
        st.markdown('<div class="hint-box">没有匹配结果。</div>', unsafe_allow_html=True)
        return
    st.markdown(f'<div class="hint-box">找到 {len(result)} 条</div>', unsafe_allow_html=True)
    for _, row in result.iterrows():
        render_diary_card(row, keywords=tokens)


def render_write_diary(user_id: int) -> None:
    st.markdown('<div class="section-title">✍️ 写日记</div>', unsafe_allow_html=True)
    with st.form("write_diary_form", clear_on_submit=True):
        selected_day = st.date_input("日记日期", value=date.today(), format="YYYY-MM-DD")
        selected_time = st.text_input("时间（HH:MM:SS）", value="21:00:00")
        source_hint = st.text_input("来源", value="Streamlit日记")
        content = st.text_area("内容", height=180)
        ok = st.form_submit_button("保存")
    if ok:
        if not content.strip():
            st.warning("内容不能为空。")
            return
        success, msg = create_diary(user_id, selected_day, selected_time.strip(), content, source_hint)
        if success:
            st.success(msg)
            st.rerun()
        else:
            st.error(msg)


def render_manage_diary(df: pd.DataFrame, user_id: int) -> None:
    st.markdown('<div class="section-title">🛠️ 编辑 / 删除</div>', unsafe_allow_html=True)
    if df.empty:
        st.info("暂无可管理的记录。")
        return
    temp = df.copy()
    temp["label"] = temp.apply(
        lambda r: f"{r['date'].strftime('%Y-%m-%d')} {str(r.get('time','')).strip() or '未知时间'} | {str(r.get('content',''))[:24].replace(chr(10),' ')}",
        axis=1,
    )
    selected_label = st.selectbox("选择记录", temp["label"].tolist(), index=0)
    row = temp[temp["label"] == selected_label].iloc[0]
    diary_id = int(row["id"])
    new_content = st.text_area("编辑内容", value=str(row.get("content", "")), height=180, key=f"edit_{diary_id}")
    c1, c2 = st.columns(2)
    if c1.button("保存修改", use_container_width=True, type="primary"):
        success, msg = update_diary(user_id, diary_id, new_content)
        if success:
            st.success(msg)
            st.rerun()
        else:
            st.error(msg)

    if c2.button("删除这条日记", use_container_width=True):
        st.session_state["pending_delete_id"] = diary_id
        st.session_state["pending_delete_label"] = selected_label
    if st.session_state.get("pending_delete_id") == diary_id:
        st.warning(f"确认删除：{st.session_state.get('pending_delete_label', '')}")
        d1, d2 = st.columns(2)
        if d1.button("确认删除", use_container_width=True, type="primary"):
            success, msg = delete_diary(user_id, diary_id)
            st.session_state.pop("pending_delete_id", None)
            st.session_state.pop("pending_delete_label", None)
            if success:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)
        if d2.button("取消", use_container_width=True):
            st.session_state.pop("pending_delete_id", None)
            st.session_state.pop("pending_delete_label", None)


def render_monthly_stats(df: pd.DataFrame) -> None:
    st.markdown('<div class="section-title">📊 月度统计</div>', unsafe_allow_html=True)
    if df.empty:
        st.info("暂无可统计数据。")
        return
    month_options = df["date"].dt.strftime("%Y-%m").drop_duplicates().sort_values(ascending=False).tolist()
    selected_month = st.selectbox("选择月份", month_options, index=0)
    month_df = df[df["date"].dt.strftime("%Y-%m") == selected_month].copy()
    month_df["day_num"] = month_df["date"].dt.day
    day_counts = month_df.groupby("day_num").size()
    c1, c2, c3 = st.columns(3)
    c1.metric("当月条数", int(len(month_df)))
    c2.metric("活跃天数", int(day_counts.index.nunique()))
    c3.metric("单日最高", int(day_counts.max()) if not day_counts.empty else 0)
    st.caption("每日记录数")
    st.bar_chart(day_counts.reindex(range(1, 32), fill_value=0).rename("count").to_frame())

    month_df["week"] = month_df["date"].dt.isocalendar().week.astype(int)
    month_df["weekday"] = month_df["date"].dt.weekday
    heat = (
        month_df.groupby(["week", "weekday"])
        .size()
        .reset_index(name="count")
        .pivot(index="week", columns="weekday", values="count")
        .fillna(0)
        .astype(int)
        .rename(columns={0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"})
    )
    st.caption("日历热力图（周 x 星期）")
    st.dataframe(heat.style.background_gradient(cmap="YlOrBr"), use_container_width=True)

    md_text = build_month_markdown(month_df, selected_month)
    st.download_button(
        "下载 Markdown",
        data=md_text.encode("utf-8"),
        file_name=f"weibo_diary_{selected_month}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    pdf_bytes, pdf_error = try_build_month_pdf(md_text)
    if pdf_bytes:
        st.download_button(
            "下载 PDF",
            data=pdf_bytes,
            file_name=f"weibo_diary_{selected_month}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
    else:
        st.info(pdf_error or "PDF 不可用")


def render_import_panel(user_id: int) -> None:
    st.markdown('<div class="section-title">📥 导入历史 JSON</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="hint-box">仅导入到当前登录账号，不会影响其他账号。线上推荐使用“上传 JSON 导入”。</div>',
        unsafe_allow_html=True,
    )

    uploaded_file = st.file_uploader("上传你的 weibo_diary.json / weibo_data.json", type=["json"])
    if uploaded_file is not None:
        if st.button("导入上传文件", use_container_width=True, type="primary"):
            try:
                records = json.load(uploaded_file)
                if not isinstance(records, list):
                    st.error("上传文件格式错误：JSON 顶层应为列表。")
                    return
                success, msg = import_json_records_for_user(user_id, records)
                if success:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
            except Exception as e:
                st.error(f"上传文件解析失败：{e}")

    st.caption("（可选）使用服务器本地文件导入")
    c1, c2 = st.columns(2)
    if c1.button("导入 weibo_diary.json", use_container_width=True):
        success, msg = import_json_for_user(user_id, Path("weibo_diary.json"))
        if success:
            st.success(msg)
        else:
            st.error(msg)
        if success:
            st.rerun()
    if c2.button("导入 weibo_data.json", use_container_width=True):
        success, msg = import_json_for_user(user_id, Path("weibo_data.json"))
        if success:
            st.success(msg)
        else:
            st.error(msg)
        if success:
            st.rerun()


def render_sidebar(user: dict) -> None:
    st.sidebar.markdown(f"### 你好，`{user['username']}`")
    st.sidebar.caption("每个账号的数据独立隔离")
    st.sidebar.info("手机访问：部署到公网后，手机浏览器打开网址即可。")
    if st.sidebar.button("退出登录", use_container_width=True):
        st.session_state.pop("user", None)
        st.rerun()


def main() -> None:
    init_db()
    inject_styles()

    user = st.session_state.get("user")
    if not user:
        render_auth_panel()
        return

    render_sidebar(user)
    st.markdown('<div class="main-title">📔 微博日记本</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="sub-title">当前账号：{user["username"]}</div>', unsafe_allow_html=True)

    df = load_user_diaries(user["id"])
    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
        ["🌟 那年今日", "📅 日历查询", "🔎 搜索", "✍️ 写日记", "🛠️ 管理日记", "📊 月度统计", "📥 导入"]
    )
    with tab1:
        render_today_memory(df)
    with tab2:
        render_date_search(df)
    with tab3:
        render_keyword_search(df)
    with tab4:
        render_write_diary(user["id"])
    with tab5:
        render_manage_diary(df, user["id"])
    with tab6:
        render_monthly_stats(df)
    with tab7:
        render_import_panel(user["id"])


if __name__ == "__main__":
    main()
