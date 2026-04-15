import json
from datetime import datetime

def show_on_this_day():
    # 1. 获取今天的日期
    today = datetime.now()
    target_month = today.month
    target_day = today.day
    
    print(f"--- 📅 那年今日 ({target_month}月{target_day}日) ---")
    
    try:
        # 2. 读取你的数据
        with open("weibo_data.json", "r", encoding="utf-8") as f:
            weibos = json.load(f)
            
        found = False
        # 3. 筛选数据
        # 倒序排列，让最新的年份显示在前面
        weibos.sort(key=lambda x: x['year'], reverse=True)
        
        for item in weibos:
            if item["month"] == target_month and item["day"] == target_day:
                print(f"\n[年份: {item['year']}]")
                print(f"内容: {item['content']}")
                print("-" * 30)
                found = True
        
        if not found:
            print("今天没有发现历史日记记录哦~")
            
    except FileNotFoundError:
        print("错误：找不到 weibo_data.json 文件，请先运行爬虫！")

if __name__ == "__main__":
    show_on_this_day()
