# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import os
import json
import argparse
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed # <--- 修复: 补上 as_completed 的导入

# --- 翻译模块 ---
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None

# ==============================================================================
# 0. 全局配置与工具函数
# ==============================================================================
def log_message(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})

def get_soup(url, parser='html.parser'):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.content, parser)
    except requests.RequestException as e:
        log_message(f"❌ 请求失败: {url}: {e}")
        return None

# ==============================================================================
# 1. 各期刊抓取函数 (不再使用类，直接用函数，更简洁)
# ==============================================================================

# --- AER 抓取函数 ---
def fetch_aer():
    log_message("🔍 [AER] 正在抓取官网...")
    url = 'https://www.aeaweb.org/journals/aer/current-issue'
    soup = get_soup(url)
    if not soup: return [], None

    header_tag = soup.find('h1', class_='issue')
    vol, iss = (match.groups() if (match := re.search(r'Vol\.\s*(\d+),\s*No\.\s*(\d+)', header_tag.text)) else (None, None)) if header_tag else (None, None)
    report_header = f"第{vol}卷(Vol. {vol}), 第{iss}期" if vol and iss else None
    
    article_ids = [a.get('id') for a in soup.find_all('article', class_='journal-article') if a.get('id') and 'symposia-title' not in a.get('class', [])]
    log_message(f"✅ [AER] 找到 {len(article_ids)} 个文章ID。")

    articles = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_id = {executor.submit(fetch_aer_detail, aid): aid for aid in article_ids}
        for future in as_completed(future_to_id):
            if result := future.result():
                articles.append(result)
    return articles, report_header

def fetch_aer_detail(article_id):
    url = f'https://www.aeaweb.org/articles?id={article_id}'
    soup = get_soup(url)
    if not soup: return None
    try:
        title = soup.find(class_='title').get_text(strip=True)
        authors = ", ".join([a.get_text(strip=True) for a in soup.select('.attribution .author')])
        abstract_tag = soup.find('section', class_='article-information abstract')
        raw_text = abstract_tag.get_text(strip=True) if abstract_tag else ""
        abstract = ' '.join((raw_text[8:] if raw_text.lower().startswith('abstract') else raw_text).split())
        return {'url': url, 'title': title, 'authors': authors or '作者未找到', 'abstract': abstract or '摘要未找到'}
    except Exception as e:
        log_message(f"  ❌ [AER] 解析详情页失败 for ID {article_id}: {e}")
        return None

# --- RSS 抓取通用函数 ---
def fetch_from_rss(journal_name, rss_url, item_parser, item_filter=lambda item: True):
    log_message(f"🔍 [{journal_name}] 正在从 RSS Feed 获取文章...")
    soup = get_soup(rss_url, parser='lxml') # 使用 lxml 解析器
    if not soup: return [], None

    items = [item for item in soup.find_all('item') if item_filter(item)]
    articles = [item_parser(item) for item in items]
    
    first_item = items[0] if items else None
    vol = iss = None
    if first_item:
        if (vol_tag := first_item.find('prism:volume')): vol = vol_tag.text.strip()
        if (iss_tag := first_item.find('prism:number')): iss = iss_tag.text.strip()
    report_header = f"第{vol}卷(Vol. {vol}), 第{iss}期" if vol and iss else None
    
    return articles, report_header

# --- 各RSS期刊的解析器和过滤器 ---
def oup_parser(item):
    desc_html = BeautifulSoup(item.description.text, 'html.parser')
    abstract_div = desc_html.find('div', class_='boxTitle')
    abstract = abstract_div.next_sibling.strip() if abstract_div and abstract_div.next_sibling else "摘要不可用"
    return {'url': item.link.text.strip(), 'title': item.title.text.strip(), 'authors': '作者信息未在RSS中提供', 'abstract': abstract}

def ecta_parser(item):
    abstract_html = item.find('content:encoded').text.strip()
    return {'url': item.link.text.strip(), 'title': item.title.text.strip(), 'authors': item.find('dc:creator').text.strip(), 'abstract': BeautifulSoup(abstract_html, 'html.parser').get_text().strip()}

def ecta_filter(item):
    return item.find('dc:creator') and item.find('dc:creator').text.strip()

def jpe_parser(item):
    return {'url': item.link.text.strip(), 'title': item.title.text.strip(), 'authors': item.find('dc:creator').text.strip(), 'abstract': '摘要需访问原文链接查看'}

def jpe_filter(item):
    return item.find('dc:creator') and "Ahead of Print" not in item.description.text

# ==============================================================================
# 2. 核心处理逻辑
# ==============================================================================
def translate_with_kimi(text, kimi_client):
    if not text or "not found" in text.lower() or "not available" in text.lower() or "未提供" in text or "需访问" in text: return text
    if not kimi_client: return "(未翻译)"
    try:
        response = kimi_client.chat.completions.create(
            model="moonshot-v1-8k",
            messages=[{"role": "system", "content": "你是一个专业的经济学领域翻译助手。请将用户提供的英文文本准确、流畅地翻译成中文。请直接输出翻译结果，不要包含任何额外说明或客套话。"},
                      {"role": "user", "content": text}],
            temperature=0.3, max_tokens=2000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log_message(f"Kimi翻译失败: {str(e)[:100]}...")
        return f"翻译失败: {e}"

def process_journal(journal_key, kimi_client):
    log_message(f"--- 开始处理: {journal_key} ---")
    
    full_journal_names = {"AER": "American Economic Review", "JPE": "Journal of Political Economy", "QJE": "The Quarterly Journal of Economics", "RES": "The Review of Economic Studies", "ECTA": "Econometrica"}
    
    fetch_map = {
        "AER": fetch_aer,
        "JPE": lambda: fetch_from_rss("JPE", "https://www.journals.uchicago.edu/action/showFeed?ui=0&mi=0&ai=t6&jc=jpe&type=etoc&feed=rss", jpe_parser, jpe_filter),
        "QJE": lambda: fetch_from_rss("QJE", "https://academic.oup.com/rss/site_5504/3365.xml", oup_parser),
        "RES": lambda: fetch_from_rss("RES", "https://academic.oup.com/rss/site_5508/3369.xml", oup_parser),
        "ECTA": lambda: fetch_from_rss("ECTA", "https://onlinelibrary.wiley.com/feed/14680262/most-recent", ecta_parser, ecta_filter),
    }

    output_data = {} # 先初始化
    try:
        raw_articles, report_header = fetch_map[journal_key]()
        log_message(f"✅ 找到 {len(raw_articles)} 篇来自 {journal_key} 的有效文章。")
        
        # --- !! 关键修复：采用更清晰、更安全的方式处理 Future 对象 !! ---
        
        # 1. 提交所有翻译任务
        if raw_articles:
            with ThreadPoolExecutor(max_workers=8) as executor:
                for article in raw_articles:
                    article['title_cn_future'] = executor.submit(translate_with_kimi, article['title'], kimi_client)
                    article['abstract_cn_future'] = executor.submit(translate_with_kimi, article['abstract'], kimi_client)

        # 2. 创建一个新的列表来存储最终结果，并逐个获取 Future 的结果
        processed_articles = []
        for article in raw_articles:
            # 获取翻译结果
            title_cn = article.pop('title_cn_future').result()
            abstract_cn = article.pop('abstract_cn_future').result()
            
            # 将 article 字典的剩余部分与新获取的结果合并
            # 注意：我们在这里创建了一个新的字典，而不是修改原始字典
            final_article = {
                **article,
                'title_cn': title_cn,
                'abstract_cn': abstract_cn
            }
            processed_articles.append(final_article)
        # --- !! 修复结束 !! ---
        
        output_data = {
            "journal_key": journal_key, 
            "journal_full_name": full_journal_names[journal_key], 
            "report_header": report_header or "最新一期", 
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'), 
            "articles": processed_articles
        }
        
    except Exception as e:
        log_message(f"❌ 处理 {journal_key} 时发生严重错误: {e}")
        output_data = {
            "journal_key": journal_key, 
            "journal_full_name": full_journal_names.get(journal_key, "Unknown"), 
            "error": str(e), 
            "articles": []
        }
    
    with open(f"{journal_key}.json", 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)
    log_message(f"✅ 已将 {journal_key} 的数据写入到 {journal_key}.json")

# ==============================================================================
# 3. 程序入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="通过混合策略抓取经济学期刊最新论文。")
    parser.add_argument("journal", help="要抓取的期刊代码 (e.g., AER, JPE)。")
    args = parser.parse_args()

    kimi_api_key = os.getenv('KIMI_API_KEY')
    kimi_client = None
    if OPENAI_AVAILABLE and kimi_api_key:
        try:
            kimi_client = OpenAI(api_key=kimi_api_key, base_url="https://api.moonshot.cn/v1")
            log_message("Kimi API 客户端初始化成功。")
        except Exception as e:
            log_message(f"初始化Kimi客户端失败: {e}")
    else:
        log_message("KIMI_API_KEY 环境变量未设置，将不进行翻译。")

    if args.journal.upper() in ["AER", "JPE", "QJE", "RES", "ECTA"]:
        process_journal(args.journal.upper(), kimi_client)
    else:
        log_message(f"错误: 不支持的期刊代码 '{args.journal}'.")

if __name__ == "__main__":
    main()
