# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import time
import os
import json
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- 翻译模块 ---
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None # 定义一个占位符，防止未安装时报错

# ==============================================================================
# 0. 全局配置与工具函数
# ==============================================================================

def log_message(message):
    """通用日志记录函数"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

# 创建一个共享的、带有通用浏览器头的 requests.Session
# 这个 session 会被所有基于 requests 的提取器使用
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
})

# ==============================================================================
# 1. 数据提取器类
# ==============================================================================

# --- AER 专用提取器 (直接抓取官网) ---
class AERDataExtractor:
    def __init__(self):
        self.base_url = 'https://www.aeaweb.org'
        self.current_issue_url = f'{self.base_url}/journals/aer/current-issue'

    def get_article_ids(self):
        log_message("🔍 [AER] 正在获取期刊主页以提取文章 ID...")
        response = session.get(self.current_issue_url, timeout=30)
        response.raise_for_status()
        if "Checking if the site connection is secure" in response.text:
            raise ConnectionError("[AER] 被机器人验证拦截，无法继续。")
        
        soup = BeautifulSoup(response.content, 'html.parser')
        all_articles = soup.find_all('article', class_='journal-article')
        symposia_title = soup.find('article', class_='journal-article symposia-title')
        target_articles = all_articles
        if symposia_title:
            try:
                symposia_index = all_articles.index(symposia_title)
                target_articles = all_articles[symposia_index + 1:]
            except ValueError:
                pass
        
        article_ids = [a.get('id') for a in target_articles if 'symposia-title' not in a.get('class', []) and a.get('id')]
        log_message(f"✅ [AER] 找到 {len(article_ids)} 篇文章待处理。")
        return article_ids

    def get_single_article_details(self, article_id: str):
        log_message(f"  > [AER] 正在抓取文章详情: {article_id}")
        article_url = f'{self.base_url}/articles?id={article_id}'
        response = session.get(article_url, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        title = soup.find(class_='title').get_text(strip=True) if soup.find(class_='title') else 'Title not found'
        authors = ", ".join([a.get_text(strip=True) for a in soup.select('.attribution .author')]) or 'Authors not found'
        abstract_element = soup.find('section', class_='article-information abstract')
        abstract = 'Abstract not found'
        if abstract_element:
            raw_text = abstract_element.get_text(strip=True)
            abstract = ' '.join((raw_text[8:] if raw_text.lower().startswith('abstract') else raw_text).split())
        
        return {'url': article_url, 'title': title, 'authors': authors, 'abstract': abstract}

# --- 基于 RSS 的提取器 ---
class BaseRssExtractor:
    def __init__(self, journal_name, rss_url):
        self.journal_name = journal_name
        self.rss_url = rss_url

    def _get_soup(self, url, parser='xml'):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return BeautifulSoup(response.content, parser)
        except requests.RequestException as e:
            log_message(f"❌ [{self.journal_name}] 请求失败: {url}: {e}")
            return None

class EctaRssExtractor(BaseRssExtractor):
    def fetch_articles(self):
        log_message(f"🔍 [{self.journal_name}] 正在从 RSS Feed 获取文章...")
        soup = self._get_soup(self.rss_url)
        if not soup: return [], None
        
        articles = []
        volume, issue = None, None
        for item in soup.find_all('item'):
            abstract_html = item.find('content:encoded').text.strip()
            articles.append({
                'url': item.link.text.strip(),
                'title': item.title.text.strip(),
                'authors': item.find('dc:creator').text.strip() if item.find('dc:creator') else "作者未找到",
                'abstract': BeautifulSoup(abstract_html, 'html.parser').get_text().strip()
            })
            if not volume and item.find('prism:volume'): volume = item.find('prism:volume').text.strip()
            if not issue and item.find('prism:number'): issue = item.find('prism:number').text.strip()
        
        report_header = f"{datetime.now().year}年 第{volume}卷(Vol. {volume}) 第{issue}期" if volume and issue else None
        return articles, report_header

class TwoStageRssExtractor(BaseRssExtractor):
    def fetch_articles(self):
        log_message(f"🔍 [{self.journal_name}] 阶段1: 从 RSS 获取链接...")
        rss_soup = self._get_soup(self.rss_url)
        if not rss_soup: return [], None
        
        article_links = self._parse_rss_for_links(rss_soup)
        log_message(f"✅ [{self.journal_name}] 阶段1: 找到 {len(article_links)} 个有效链接。")
        if not article_links: return [], None

        log_message(f"🔍 [{self.journal_name}] 阶段2: 并行抓取详情页...")
        articles = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_url = {executor.submit(self._scrape_detail_page, url): url for url in article_links}
            for future in as_completed(future_to_url):
                if result := future.result():
                    articles.append(result)
        
        report_header = None
        if articles:
            first_article = articles[0]
            vol, iss = first_article.get('volume'), first_article.get('issue')
            if vol and iss: report_header = f"{datetime.now().year}年 第{vol}卷(Vol. {vol}) 第{iss}期"
        
        return articles, report_header

    def _parse_rss_for_links(self, soup): raise NotImplementedError
    def _scrape_detail_page(self, url): raise NotImplementedError

class JpeRssExtractor(TwoStageRssExtractor):
    def _parse_rss_for_links(self, soup):
        return [item.get('rdf:about') for item in soup.find_all('item') if item.find('dc:creator') and item.get('rdf:about')]

    def _scrape_detail_page(self, url):
        soup = self._get_soup(url, parser='html.parser')
        if not soup: return None
        try:
            volume_text = soup.select_one('.issue-info-pub-date a').text.strip() if soup.select_one('.issue-info-pub-date a') else ""
            vol = iss = None
            if "Volume" in volume_text and "Issue" in volume_text:
                parts = volume_text.split(',')
                vol = parts[0].replace("Volume", "").strip()
                iss = parts[1].replace("Issue", "").strip()
            return {
                'url': url,
                'title': soup.find('h1', class_='citation__title').text.strip(),
                'authors': ", ".join([a.text.strip() for a in soup.select('.author-name span')]) or "作者未找到",
                'abstract': soup.find('div', class_='abstractSection').p.text.strip(),
                'volume': vol, 'issue': iss
            }
        except AttributeError as e:
            log_message(f"  ❌ [{self.journal_name}] 解析详情页失败 {url.split('?')[0]}: {e}")
            return None

class OupRssExtractor(TwoStageRssExtractor):
    def _parse_rss_for_links(self, soup):
        return [item.link.text.strip() for item in soup.find_all('item')]

    def _scrape_detail_page(self, url):
        soup = self._get_soup(url, parser='html.parser')
        if not soup: return None
        try:
            issue_info = soup.select_one('.issue-info')
            return {
                'url': url,
                'title': soup.find('h1', class_='wi-article-title').text.strip(),
                'authors': ", ".join([a.text.strip().strip(',') for a in soup.select('.wi-authors a.linked-name')]) or "作者未找到",
                'abstract': soup.find('section', class_='abstract').p.text.strip(),
                'volume': issue_info['data-vol'] if issue_info and 'data-vol' in issue_info.attrs else None,
                'issue': issue_info['data-issue'] if issue_info and 'data-issue' in issue_info.attrs else None
            }
        except AttributeError as e:
            log_message(f"  ❌ [{self.journal_name}] 解析详情页失败 {url.split('?')[0]}: {e}")
            return None

# ==============================================================================
# 2. 核心处理逻辑
# ==============================================================================
def translate_with_kimi(text, kimi_client):
    if not text or "not found" in text.lower(): return "内容缺失"
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
    """主流程：根据期刊代码选择不同策略进行处理"""
    
    full_journal_names = {
        "AER": "American Economic Review", "JPE": "Journal of Political Economy",
        "QJE": "The Quarterly Journal of Economics", "RES": "The Review of Economic Studies",
        "ECTA": "Econometrica",
    }
    
    log_message(f"--- 开始处理: {journal_key} ---")
    
    extractor_map = {
        "JPE": JpeRssExtractor("JPE", "https://www.journals.uchicago.edu/action/showFeed?ui=0&mi=0&ai=t6&jc=jpe&type=etoc&feed=rss"),
        "QJE": OupRssExtractor("QJE", "https://academic.oup.com/rss/site_5504/3365.xml"),
        "RES": OupRssExtractor("RES", "https://academic.oup.com/rss/site_5508/3369.xml"),
        "ECTA": EctaRssExtractor("ECTA", "https://onlinelibrary.wiley.com/feed/14680262/most-recent"),
    }
    
    try:
        # AER 使用特殊处理流程
        if journal_key == "AER":
            aer_extractor = AERDataExtractor()
            article_ids = aer_extractor.get_article_ids()
            raw_articles = []
            if article_ids:
                with ThreadPoolExecutor(max_workers=5) as executor:
                    future_to_id = {executor.submit(aer_extractor.get_single_article_details, aid): aid for aid in article_ids}
                    for future in as_completed(future_to_id):
                        if result := future.result():
                            raw_articles.append(result)
            report_header = None # AER不方便从页面提取卷期，故不生成
        else: # 其他期刊使用RSS流程
            extractor = extractor_map[journal_key]
            raw_articles, report_header = extractor.fetch_articles()

        log_message(f"✅ 找到 {len(raw_articles)} 篇来自 {journal_key} 的有效文章。")
        
        processed_articles = []
        if raw_articles:
            with ThreadPoolExecutor(max_workers=8) as executor: # 增加翻译线程
                for article in raw_articles:
                    article['title_cn_future'] = executor.submit(translate_with_kimi, article['title'], kimi_client)
                    article['abstract_cn_future'] = executor.submit(translate_with_kimi, article['abstract'], kimi_client)

            for article in raw_articles:
                article['title_cn'] = article.pop('title_cn_future').result()
                article['abstract_cn'] = article.pop('abstract_cn_future').result()
                processed_articles.append(article)
        
        output_data = {
            "journal_key": journal_key,
            "journal_full_name": full_journal_names[journal_key],
            "report_header": report_header or f"{datetime.now().year}年 最新一期",
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            "articles": processed_articles
        }
        
    except Exception as e:
        log_message(f"❌ 处理 {journal_key} 时发生严重错误: {e}")
        output_data = {
            "journal_key": journal_key,
            "journal_full_name": full_journal_names.get(journal_key, journal_key),
            "report_header": f"{datetime.now().year}年 数据获取失败",
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            "error": str(e), "articles": []
        }
    
    # 统一写入JSON文件
    output_filename = f"{journal_key}.json"
    with open(output_filename, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)
    log_message(f"✅ 已将 {journal_key} 的数据写入到 {output_filename}")

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
        log_message(f"错误: 不支持的期刊代码 '{args.journal}'. GitHub Actions 会为每个期刊单独运行此脚本。")

if __name__ == "__main__":
    main()
