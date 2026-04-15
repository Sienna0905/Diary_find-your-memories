# 微博日记本 Render 部署说明

本项目使用 `Streamlit + SQLite`，已支持多用户独立账号。

## 1. 部署前准备

- 把项目代码放到 GitHub 仓库
- 确保仓库里包含以下文件：
  - `app.py`
  - `requirements.txt`
  - `render.yaml`

## 2. 在 Render 创建服务

1. 登录 [Render](https://render.com/)
2. 点击 **New +** -> **Blueprint**
3. 选择你的 GitHub 仓库
4. Render 会自动识别 `render.yaml` 并创建 Web Service + 持久磁盘

## 3. 核心配置（已在 render.yaml 内）

- 启动命令：
  - `streamlit run app.py --server.address 0.0.0.0 --server.port $PORT`
- 持久化数据库路径：
  - `DB_PATH=/var/data/diary_app.db`
- 持久磁盘挂载：
  - `mountPath=/var/data`

这一步非常关键，数据库必须在持久磁盘目录下，否则重启后会丢数据。

## 4. 首次上线后操作

- 打开 Render 提供的网址
- 注册你的管理员账号（当前版本无管理员角色，普通注册即可）
- 让朋友通过同一个网址注册并使用（账号数据彼此隔离）

## 5. 常见问题

- **Q: 重启后数据没了？**
  - A: 通常是 `DB_PATH` 没指向持久磁盘目录，检查是否为 `/var/data/diary_app.db`。

- **Q: 手机上打不开？**
  - A: 确认你打开的是 Render 公网 URL，不是本地 `localhost` 地址。

- **Q: PDF 下载按钮提示不可用？**
  - A: 需要 `reportlab` 安装成功。`requirements.txt` 已包含该依赖，重新部署后应恢复。

## 6. 后续建议（可选）

- 密码哈希从 `sha256` 升级到 `bcrypt`
- 增加“忘记密码/邮箱验证”
- 数据库从 SQLite 升级到 PostgreSQL（多人并发更稳）
