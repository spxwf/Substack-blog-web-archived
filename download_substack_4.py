"""
版本 4.0 更新日志：
**精准时间修正**
改变获取日期的方式——不再信任 Sitemap 的时间，而是下载 HTML 后，直接读取网页元数据（Meta Data）里最精准的“发布时间”，并将其自动转换为你电脑的本地时区。
"""
import os
import requests
import base64
import time
import random
import re
import json
import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from dateutil import parser as date_util_parser
from dateutil import tz

# --- 配置区域 ---
TARGET_URL = "https://rationaloptimistsociety.substack.com"  #  https://rationaloptimistsociety.substack.com  https://zachglabman.substack.com/ https://barsoom.substack.com/
OUTPUT_DIR = "Downloads"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Connection": "keep-alive",
}

CSS_CACHE = {}
MAX_WORKERS = 8
session = requests.Session()
session.headers.update(HEADERS)

# ----------------

def download_url_content(url, is_binary=True):
    if not is_binary and url in CSS_CACHE:
        return CSS_CACHE[url]

    for i in range(3):
        try:
            resp = session.get(url, timeout=(5, 15))
            if resp.status_code == 200:
                if is_binary: return resp.content
                else:
                    CSS_CACHE[url] = resp.text
                    return resp.text
            elif resp.status_code == 429:
                time.sleep(5)
        except:
            time.sleep(0.5)
    return None

def clean_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title).strip()

def get_articles_with_sitemap_date():
    """
    恢复从 Sitemap 获取基础时间，作为保底。
    """
    print(f"正在分析 Sitemap: {TARGET_URL}/sitemap.xml ...")
    try:
        r = session.get(f"{TARGET_URL}/sitemap.xml", timeout=20)
        soup = BeautifulSoup(r.content, 'xml')
        articles = []
        for url_tag in soup.find_all('url'):
            loc = url_tag.find('loc').text
            if '/p/' in loc:
                # 获取 Sitemap 时间作为备用
                lastmod = url_tag.find('lastmod').text if url_tag.find('lastmod') else ""
                sitemap_dt = None
                if lastmod:
                    try:
                        sitemap_dt = date_util_parser.parse(lastmod)
                    except: pass
                
                articles.append({
                    'url': loc,
                    'sitemap_date': sitemap_dt
                })
        print(f"Sitemap 解析完成，共 {len(articles)} 篇文章。")
        return articles
    except Exception as e:
        print(f"Sitemap 失败: {e}")
        return []

def extract_date_from_html(soup):
    """
    尝试从 HTML 中提取更精准的 datePublished
    """
    dt = None
    
    # 策略 1: JSON-LD (最精准)
    # Substack 通常把数据放在 <script type="application/ld+json"> 里
    try:
        scripts = soup.find_all('script', type='application/ld+json')
        for script in scripts:
            if script.string:
                data = json.loads(script.string)
                # 可能是列表也可能是字典
                if isinstance(data, list):
                    for item in data:
                        if 'datePublished' in item:
                            dt = date_util_parser.parse(item['datePublished'])
                            break
                elif isinstance(data, dict):
                    if 'datePublished' in data:
                        dt = date_util_parser.parse(data['datePublished'])
                
                if dt: break
    except: pass

    # 策略 2: Meta 标签
    if not dt:
        try:
            meta = soup.find('meta', property='article:published_time') or \
                   soup.find('meta', itemprop='datePublished')
            if meta and meta.get('content'):
                dt = date_util_parser.parse(meta['content'])
        except: pass

    # 统一转换为本地时区
    if dt:
        if dt.tzinfo:
            dt = dt.astimezone(tz.tzlocal())
        return dt
    
    return None

def process_single_article(article_data):
    url = article_data['url']
    sitemap_date = article_data['sitemap_date']
    
    try:
        # 1. 下载页面
        resp = session.get(url, timeout=20)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'lxml')
        
        # 2. 标题
        title_tag = soup.find('h1', class_='post-title') or soup.find('title')
        title = title_tag.get_text(strip=True) if title_tag else "Untitled"
        
        # 3. --- 核心时间逻辑修复 ---
        # 优先尝试从 HTML 拿精准发布时间
        final_date = extract_date_from_html(soup)
        
        # 如果 HTML 里没找到 (比如被反爬拦截了主要数据，或者是特殊页面)
        # 就回退使用 Sitemap 的时间
        if not final_date:
            if sitemap_date:
                # print(f"  [提示] 使用 Sitemap 时间作为保底")
                final_date = sitemap_date
            else:
                # 只有当两者都失败时，才无奈使用当前时间 (极少发生)
                final_date = datetime.datetime.now()
        
        # 格式化
        date_str = final_date.strftime('%Y-%m-%d')
        display_time = final_date.strftime('%Y-%m-%d %H:%M')
        
        # 4. 检查文件是否存在
        safe_title = clean_filename(title)
        filename = f"{date_str}_{safe_title}.html"
        filepath = os.path.join(OUTPUT_DIR, filename)

        if os.path.exists(filepath):
            return False 

        print(f"Downloading: [{date_str}] {title}")

        # --- 以下是资源下载部分 (不变) ---
        
        # A. CSS
        for link in soup.find_all('link', rel='stylesheet'):
            href = link.get('href')
            if href:
                full_url = urljoin(url, href)
                css_text = download_url_content(full_url, is_binary=False)
                if css_text:
                    new_style = soup.new_tag("style")
                    new_style.string = css_text
                    link.replace_with(new_style)

        # B. 图片
        imgs = soup.find_all('img')
        valid_imgs = [(img, urljoin(url, img.get('src') or img.get('data-src'))) 
                      for img in imgs if (img.get('src') or img.get('data-src'))]
        
        if valid_imgs:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_map = {executor.submit(download_url_content, u, True): (i, u) for i, u in valid_imgs}
                for future in as_completed(future_map):
                    img_tag, _ = future_map[future]
                    try:
                        data = future.result()
                        if data:
                            b64 = base64.b64encode(data).decode('utf-8')
                            img_tag['src'] = f"data:image/jpeg;base64,{b64}"
                            for attr in ['srcset', 'data-src', 'loading']:
                                if img_tag.has_attr(attr): del img_tag[attr]
                    except: pass

        # C. 清理
        for tag in soup(["script", "noscript", "iframe"]): tag.decompose()
        for c in ['substack-header', 'pencraft-login-form', 'post-footer-cta']:
            for div in soup.find_all(class_=c): div.decompose()

        # D. 注入时间显示
        if title_tag and title_tag.parent:
            meta_div = soup.new_tag("div")
            meta_div['style'] = "color: #666; font-size: 0.9em; margin-bottom: 20px; border-bottom: 1px solid #eee; padding-bottom: 10px;"
            meta_div.string = f"发布时间: {display_time} | 原文链接: {url}"
            title_tag.insert_after(meta_div)

        # E. 保存
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(str(soup))
        
        time.sleep(random.uniform(1.0, 1.5)) 
        return True

    except Exception as e:
        print(f"  [!] 错误: {url} -> {e}")
        return False

def main():
    if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)
    
    # 检查依赖
    try:
        import dateutil
    except ImportError:
        os.system("pip install python-dateutil")
        print("依赖安装完成，请重试。")
        return

    # 1. 先获取带 Sitemap 时间的列表
    articles = get_articles_with_sitemap_date()
    new_count = 0
    
    print("-" * 50)
    for article in articles:
        if process_single_article(article):
            new_count += 1
            
    print("-" * 50)
    print(f"完成。")
    input("按回车退出...")

if __name__ == "__main__":
    main()