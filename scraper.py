# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import time
import os
import json
import argparse
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
})

# ==============================================================================
# 1. 数据提取器类 (全新稳定版)
# ==============================================================================

class BaseExtractor:
    def __init__(self, journal_name):
        self.journal_name = journal_name
    
    def _get_soup(self, url, parser='xml'):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return BeautifulSoup(response.content, parser)
        except requests.RequestException as e:
            log_message(f"❌ [{self.journal_name}] 请求失败: {url}: {e}")
            return None

class AerExtractor(BaseExtractor):
    def fetch_articles(self):
        log_message(f"🔍 [{self.journal_name}] 正在抓取官网...")
        url = 'https://www.aeaweb.org/journals/aer/current-issue'
        soup = self._get_soup(url, parser='html.parser')
        if not soup: return [], None

        # 提取卷/期号
        header_tag = soup.find('h1', class_='issue')
        vol, iss = None, None
        if header_tag:
            match = re.search(r'Vol\.\s*(\d+),\s*No\.\s*(\d+)', header_tag.text)
            if match:
                vol, iss = match.groups()
        report_header = f"第{vol}卷(Vol. {vol}), 第{iss}期" if vol and iss else None
        
        # 提取文章详情
        article_tags = soup.find_all('article', class_='journal-article')
        articles = []
        for tag in article_tags:
            # 过滤掉非正式论文条目
            if 'symposia-title' in tag.get('class', []) or not tag.find('h3', class_='title'):
                continue
            articles.append({
                'url': f"https://www.aeaweb.org{tag.find('a', class_='view-article')['href']}",
                'title': tag.find('h3', class_='title').text.strip(),
                'authors': tag.find('p', class_='attribution').text.strip(),
                'abstract': tag.find('div', class_='abstract').text.strip()
            })
        return articles, report_header

class RssExtractor(BaseExtractor):
    def __init__(self, journal_name, rss_url):
        super().__init__(journal_name)
        self.rss_url = rss_url

    def fetch_articles(self):
        log_message(f"🔍 [{self.journal_name}] 正在从 RSS Feed 获取文章...")
        soup = self._get_soup(self.rss_url)
        if not soup: return [], None

        items = self._filter_items(soup.find_all('item'))
        articles = []
        volume, issue = None, None

        for item in items:
            articles.append(self._parse_item(item))
            if not volume and item.find('prism:volume'): volume = item.find('prism:volume').text.strip()
            if not issue and item.find('prism:number'): issue = item.find('prism:number').text.strip()

        report_header = f"第{volume}卷(Vol. {volume}), 第{issue}期" if volume and issue else None
        return articles, report_header

    def _filter_items(self, items):
        return items # 默认不过滤

    def _parse_item(self, item):
        raise NotImplementedError

class OupRssExtractor(RssExtractor):
    def _parse_item(self, item):
        desc_html = BeautifulSoup(item.description.text, 'html.parser')
        abstract_div = desc_html.find('div', class_='boxTitle')
        abstract = abstract_div.next_sibling.strip() if abstract_div and abstract_div.next_sibling else "摘要不可用"
        return {
            'url': item.link.text.strip(),
            'title': item.title.text.strip(),
            'authors': '作者信息未在RSS中提供',
            'abstract': abstract
        }

class EctaRssExtractor(RssExtractor):
    def _filter_items(self, items):
        # 过滤掉 dc:creator 标签内容为空的条目
        return [item for item in items if item.find('dc:creator') and item.find('dc:creator').text.strip()]

    def _parse_item(self, item):
        abstract_html = item.find('content:encoded').text.strip()
        return {
            'url': item.link.text.strip(),
            'title': item.title.text.strip(),
            'authors': item.find('dc:creator').text.strip(),
            'abstract': BeautifulSoup(abstract_html, 'html.parser').get_text().strip()
        }

class JpeRssExtractor(RssExtractor):
    def _filter_items(self, items):
        # 过滤掉包含 "Ahead of Print" 且没有作者的条目
        return [item for item in items if item.find('dc:creator') and "Ahead of Print" not in item.description.text]

    def _parse_item(self, item):
        # JPE的摘要在RSS中不提供，但我们可以从详情页获取，如果失败则留空
        # 为了稳定，我们选择不二次抓取，直接留空
        return {
            'url': item.link.text.strip(),
            'title': item.title.text.strip(),
            'authors': item.find('dc:creator').text.strip(),
            'abstract': '摘要需访问原文链接查看'
        }

# ==============================================================================
# 2. 核心处理逻辑
# ==============================================================================
def translate_with_kimi(text, kimi_client):
    if not text or "not found" in text.lower() or "not available" in text.lower() or "未提供" in text or "需访问" in text:
        return text
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
    full_journal_names = {
        "AER": "American Economic Review", "JPE": "Journal of Political Economy",
        "QJE": "The Quarterly Journal of Economics", "RES": "The Review of Economic Studies",
        "ECTA": "Econometrica",
    }
    
    log_message(f"--- 开始处理: {journal_key} ---")
    
    extractors = {
        "AER": AerExtractor("AER"),
        "JPE": JpeRssExtractor("JPE", "https://www.journals.uchicago.edu/action/showFeed?ui=0&mi=0&ai=t6&jc=jpe&type=etoc&feed=rss"),
        "QJE": OupRssExtractor("QJE", "https://academic.oup.com/rss/site_5504/3365.xml"),
        "RES": OupRssExtractor("RES", "https://academic.oup.com/rss/site_5508/3369.xml"),
        "ECTA": EctaRssExtractor("ECTA", "https://onlinelibrary.wiley.com/feed/14680262/most-recent"),
    }
    
    try:
        extractor = extractors[journal_key]
        raw_articles, report_header = extractor.fetch_articles()
        log_message(f"✅ 找到 {len(raw_articles)} 篇来自 {journal_key} 的有效文章。")
        
        processed_articles = []
        if raw_articles:
            with ThreadPoolExecutor(max_workers=8) as executor:
                # 提交翻译任务
                for article in raw_articles:
                    article['title_cn_future'] = executor.submit(translate_with_kimi, article['title'], kimi_client)
                    article['abstract_cn_future'] = executor.submit(translate_with_kimi, article['abstract'], kimi_client)
                # 获取翻译结果
                for article in raw_articles:
                    article['title_cn'] = article.pop('title_cn_future').result()
                    article['abstract_cn'] = article.pop('abstract_cn_future').result()
                    processed_articles.append(article)
        
        output_data = {
            "journal_key": journal_key,
            "journal_full_name": full_journal_names[journal_key],
            "report_header": report_header or "最新一期",
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            "articles": processed_articles
        }
        
    except Exception as e:
        log_message(f"❌ 处理 {journal_key} 时发生严重错误: {e}")
        output_data = { "error": str(e), "articles": [], **full_journal_names.get(journal_key, {})}

    # 统一写入JSON文件
    with open(f"{journal_key}.json", 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)
    log_message(f"✅ 已将 {journal_key} 的数据写入到 {journal_key}.json")

# ==============================================================================
# 3. 程序入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="通过混合策略抓取经济学期刊最新论文。")
    parser.add_argument("journal", help="要抓取的期刊代码 (e.g., AER, JPE, ALL)。")
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
        log_message("KIMI_API_KEY 环境变量未设置或 openai 库不可用，将不进行翻译。")

    if args.journal.upper() in ["AER", "JPE", "QJE", "RES", "ECTA"]:
        process_journal(args.journal.upper(), kimi_client)
    else:
        log_message(f"错误: 不支持的期刊代码 '{args.journal}'.")

if __name__ == "__main__":
    main()
