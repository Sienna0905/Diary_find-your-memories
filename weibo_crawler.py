import requests
import sqlite3
import json
import time
import random
import os
import re
import html as html_lib
import pandas as pd
from datetime import datetime

# ==============================
# 🔧 在这里填入你的信息
# ==============================
MY_UID = "3987025018"          # 例如 "1234567890"
MY_COOKIE = "SCF=Ahim6DzXC4JLHuoEm4X2iR4khmknOTwLhXeTsuQZjXiY3nC3_4ivzesUcXV1eq9R1T628emp3Gd2lKLrSXCb_6c.; XSRF-TOKEN=qFbvU7ZwiEP78X0jKxxYWNGs; PC_TOKEN=cb24f2287b; SUB=_2A25E2zcaDeRhGeVH41UR8ivMyjSIHXVnmTbSrDV8PUNbmtANLRDykW9NTzEDSEyNG4k9ZJKRkqVRejmpmPpXTMf9; SUBP=0033WrSXqPxfM725Ws9jqgMF55529P9D9WhLqnXn1piB2ZyJsTrZiUBC5JpX5KzhUgL.Foe41hM7eo-7eKn2dJLoIp7LxKnLBK-LB.qLxKML1hnLBo2LxKqL1KqLB-q_; ALF=02_1778832458; WBPSESS=pCnfi4EeoIkPvjAjZchhRjthXHYFUGuQzBU1e5geHLXQHW6iz_lqUrum8sM9wHedMwrimiOgyzgJx44RdFdqFEUMVVUl_D_tWFCHBBOIb3s4TUatzhafa2flAWyGagh9UKJLiWfkUuDiF6Z_6az6QQ=="    # 从浏览器复制的那一大串

# ==============================
# 请求头配置
# ==============================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cookie": MY_COOKIE,
    "Referer": "https://weibo.com",
}

LAST_PAGE_FILE = "last_page.txt"
CHECKPOINT_FILE = "weibo_data.json"


def load_last_page():
    """读取上次成功爬取到的页码。"""
    if not os.path.exists(LAST_PAGE_FILE):
        return 0
    try:
        with open(LAST_PAGE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return int(content) if content else 0
    except Exception as e:
        print(f"读取 {LAST_PAGE_FILE} 失败，默认从第1页开始: {e}")
        return 0


def save_last_page(page):
    """保存当前成功爬取到的页码。"""
    try:
        with open(LAST_PAGE_FILE, "w", encoding="utf-8") as f:
            f.write(str(page))
    except Exception as e:
        print(f"写入 {LAST_PAGE_FILE} 失败: {e}")


def load_checkpoint(filename=CHECKPOINT_FILE):
    """读取历史 checkpoint，避免断点续跑后丢失此前数据。"""
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                print(f"📦 检测到历史 checkpoint：{filename}，已有 {len(data)} 条")
                return data
    except Exception as e:
        print(f"读取 checkpoint 失败，将从空数据开始: {e}")
    return []


def get_weibo_list(uid, page=1, max_retries=5):
    """获取某页微博列表，带重试与风控暂停逻辑。"""
    url = "https://weibo.com/ajax/statuses/mymblog"
    params = {
        "uid": uid,
        "page": page,
        "feature": 0,  # 0=全部, 1=原创, 2=图片, 3=视频
    }
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
            if resp.status_code in (403, 418):
                pause_seconds = random.uniform(60, 120)
                print(
                    f"⚠️ 第{page}页触发风控（HTTP {resp.status_code}），"
                    f"第{attempt}/{max_retries}次重试前暂停 {pause_seconds:.1f} 秒..."
                )
                time.sleep(pause_seconds)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("list", [])
        except requests.exceptions.RequestException as e:
            if attempt == max_retries:
                print(f"❌ 第{page}页请求多次失败，已达到重试上限: {e}")
                return None
            pause_seconds = random.uniform(8, 20)
            print(
                f"⚠️ 第{page}页请求异常（第{attempt}/{max_retries}次）: {e}，"
                f"{pause_seconds:.1f} 秒后重试..."
            )
            time.sleep(pause_seconds)
        except Exception as e:
            print(f"❌ 第{page}页解析失败: {e}")
            return None
    return None


def clean_text_content(raw_text):
    """把微博接口里的 HTML 文本转成可读纯文本。"""
    if not raw_text:
        return ""
    text = str(raw_text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    return text.strip()


def get_long_text(weibo_id, max_retries=3):
    """长微博补抓：遇到“全文”时请求完整正文。"""
    if not weibo_id:
        return ""

    url = "https://weibo.com/ajax/statuses/longtext"
    params = {"id": weibo_id}

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
            if resp.status_code in (403, 418):
                pause_seconds = random.uniform(15, 40)
                print(
                    f"⚠️ 长微博 {weibo_id} 触发风控（HTTP {resp.status_code}），"
                    f"第{attempt}/{max_retries}次重试前暂停 {pause_seconds:.1f} 秒..."
                )
                time.sleep(pause_seconds)
                continue

            resp.raise_for_status()
            data = resp.json().get("data", {})
            long_text = data.get("longTextContent", "") or data.get("longText", "")
            return clean_text_content(long_text)
        except requests.exceptions.RequestException as e:
            if attempt == max_retries:
                print(f"⚠️ 长微博 {weibo_id} 获取失败，已放弃: {e}")
                return ""
            time.sleep(random.uniform(2, 6))
        except Exception as e:
            print(f"⚠️ 长微博 {weibo_id} 解析失败: {e}")
            return ""

    return ""

def parse_weibo(item):
    """解析单条微博，提取有用字段"""
    # 先用列表接口文本
    text = item.get("text_raw", "") or item.get("text", "")

    # 如果是长微博（有“全文”），补抓完整内容
    if item.get("isLongText"):
        inline_long = ""
        long_text_obj = item.get("longText", {})
        if isinstance(long_text_obj, dict):
            inline_long = long_text_obj.get("longTextContent", "") or long_text_obj.get("longText", "")
        fetched_long = inline_long or get_long_text(item.get("id", ""))
        if fetched_long:
            text = fetched_long

    text = clean_text_content(text)
    
    # 去掉转发内容（//开头的部分）
    if "//@" in text:
        text = text.split("//@")[0].strip()
    
    created_at = item.get("created_at", "")
    
    # 转换时间格式
    try:
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
        year = dt.year
        month = dt.month
        day = dt.day
    except:
        date_str = created_at
        time_str = ""
        year = month = day = 0
    
    return {
        "id": item.get("id", ""),
        "date": date_str,
        "time": time_str,
        "year": year,
        "month": month,
        "day": day,
        "content": text,
        "pics_count": len(item.get("pic_ids", [])),
        "source": item.get("source", ""),
        "reposts": item.get("reposts_count", 0),
        "comments": item.get("comments_count", 0),
        "likes": item.get("attitudes_count", 0),
    }

def save_checkpoint(weibos, filename=CHECKPOINT_FILE):
    """保存中间 checkpoint"""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(weibos, f, ensure_ascii=False, indent=2)
        print(f"💾 已保存 checkpoint 到 {filename}，当前累计微博数：{len(weibos)}")
    except Exception as e:
        print(f"保存 checkpoint 时出错: {e}")

def crawl_all_weibos(uid, checkpoint_every=5):
    """爬取所有微博，直到没有更多数据为止，支持断点续爬与随机sleep。"""
    all_weibos = load_checkpoint(CHECKPOINT_FILE)
    existing_ids = {str(w.get("id", "")) for w in all_weibos if w.get("id")}

    last_page = load_last_page()
    page = last_page + 1 if last_page > 0 else 1
    if last_page > 0:
        print(f"🧭 从断点续爬：上次完成第 {last_page} 页，本次从第 {page} 页开始")

    while True:
        print(f"正在爬取第 {page} 页...")
        items = get_weibo_list(uid, page)

        if items is None:
            print("🚫 当前页持续失败，程序暂停。下次运行会从最近进度继续。")
            break

        if not items:
            print(f"第 {page} 页没有数据，爬取完成！")
            # 抓取完成后清理进度记录，避免下次误从末页继续
            if os.path.exists(LAST_PAGE_FILE):
                os.remove(LAST_PAGE_FILE)
            break
        
        page_new = 0
        for item in items:
            parsed = parse_weibo(item)
            # 过滤掉太短的内容（可能不是日记）
            if len(parsed["content"]) > 5 and str(parsed["id"]) not in existing_ids:
                all_weibos.append(parsed)
                existing_ids.add(str(parsed["id"]))
                page_new += 1
        
        print(f"  ✅ 本页获取 {page_new} 条，累计 {len(all_weibos)} 条")
        save_last_page(page)
        
        # 每 checkpoint_every 页保存一次
        if page % checkpoint_every == 0:
            save_checkpoint(all_weibos, filename=CHECKPOINT_FILE)
        
        # 更自然的随机 sleep
        sleep_time = random.uniform(3, 8)
        print(f"  🌙 等待 {sleep_time:.1f} 秒，模拟真实用户操作...")
        time.sleep(sleep_time)
        page += 1
    
    # 最后爬取结束再多存一遍完全数据
    save_checkpoint(all_weibos, filename=CHECKPOINT_FILE)
    return all_weibos

def save_results(weibos):
    """保存结果"""
    if not weibos:
        print("没有爬取到任何数据！")
        return
    
    df = pd.DataFrame(weibos)
    df = df.sort_values("date", ascending=False)
    
    # 保存为 Excel（方便查看）
    df.to_excel("weibo_diary.xlsx", index=False)
    
    # 保存为 JSON（后续导入 App 用）
    with open("weibo_diary.json", "w", encoding="utf-8") as f:
        json.dump(weibos, f, ensure_ascii=False, indent=2)
    
    print(f"\n🎉 完成！共爬取 {len(weibos)} 条微博")
    print("📄 已保存为 weibo_diary.xlsx 和 weibo_diary.json")

# ==============================
# 运行
# ==============================
if __name__ == "__main__":
    print("🚀 开始爬取微博数据...")
    print(f"   UID: {MY_UID}")
    print("   翻页策略: 自动翻页直到无数据\n")
    
    weibos = crawl_all_weibos(MY_UID)
    save_results(weibos)
