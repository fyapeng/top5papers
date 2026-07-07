# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import os
import json
import argparse
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

# --- 翻译模块 ---
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None

# ==============================================================================
# 0. 全局配置与工具函数 (无修改)
# ==============================================================================
def log_message(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})

NON_RESEARCH_TITLE_PATTERN = re.compile(
    r'front\s*matter|frontmatter|back\s*matter|backmatter|\brecent referees\b|'
    r'acknowledg(?:e)?ment of referees|turnaround times|\breport of the|annual report|'
    r'\bnobel lecture\b|fisher.?schultz lecture|^comment on\b|: comment$|\ba comment$|'
    r'^reply to comments?|^correction to:|\berratum\b',
    flags=re.I,
)

def is_research_article(article):
    return not NON_RESEARCH_TITLE_PATTERN.search(article.get("title", ""))

def get_soup(url, parser='html.parser'):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.content, parser)
    except requests.RequestException as e:
        log_message(f"❌ 请求失败: {url}: {e}")
        return None

def extract_doi(value):
    if not value:
        return ""
    match = re.search(r'(10\.\d{4,9}/[^\s?&#]+)', unquote(value), flags=re.I)
    return match.group(1).rstrip('.').lower() if match else ""

def missing_text(value):
    text = (value or "").strip()
    return (
        not text
        or text == "PENDING_LOCAL_FETCH"
        or "摘要未找到" in text
        or "摘要不可用" in text
        or "not found" in text.lower()
        or "not available" in text.lower()
    )

def clean_crossref_abstract(value):
    if not value:
        return ""
    return ' '.join(BeautifulSoup(value, 'html.parser').get_text(" ").split())

def format_crossref_authors(authors):
    names = []
    for author in authors or []:
        name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
        if not name and author.get("name"):
            name = author["name"]
        if name:
            names.append(name)
    return ", ".join(names)

def fetch_crossref_metadata(doi):
    if not doi:
        return {}
    try:
        response = session.get(
            f"https://api.crossref.org/works/{doi}",
            params={"mailto": "fuyapeng.evan@gmail.com"},
            timeout=30,
        )
        response.raise_for_status()
        message = response.json().get("message", {})
    except (requests.RequestException, ValueError) as e:
        log_message(f"  ⚠️ Crossref 元数据获取失败 {doi}: {e}")
        return {}

    return {
        "authors": format_crossref_authors(message.get("author")),
        "abstract": clean_crossref_abstract(message.get("abstract")),
    }

def enrich_article_metadata(article):
    if article.get("authors") and not missing_text(article.get("abstract")):
        return article

    doi = extract_doi(article.get("url", ""))
    metadata = fetch_crossref_metadata(doi)
    if not metadata:
        return article

    if not article.get("authors") and metadata.get("authors"):
        article["authors"] = metadata["authors"]
    if missing_text(article.get("abstract")) and metadata.get("abstract"):
        article["abstract"] = metadata["abstract"]
    return article

# ==============================================================================
# 1. 各期刊抓取函数 (无修改)
# ==============================================================================
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
        author_elements = soup.select('.attribution .author')
        if not author_elements:
            log_message(f"  > [AER] 跳过非文章条目 (无作者): {title}")
            return None
        
        authors = ", ".join([a.get_text(strip=True) for a in author_elements])
        abstract_tag = soup.find('section', class_='article-information abstract')
        raw_text = abstract_tag.get_text(strip=True) if abstract_tag else ""
        abstract = ' '.join((raw_text[8:] if raw_text.lower().startswith('abstract') else raw_text).split())
        return {'url': url, 'title': title, 'authors': authors, 'abstract': abstract or '摘要未找到'}
    except Exception as e:
        log_message(f"  ❌ [AER] 解析详情页失败 for ID {article_id}: {e}")
        return None

def fetch_from_rss(journal_name, rss_url, item_parser, item_filter=lambda item: True):
    log_message(f"🔍 [{journal_name}] 正在从 RSS Feed 获取文章...")
    soup = get_soup(rss_url, parser='lxml')
    if not soup: return [], None

    items = [item for item in soup.find_all('item') if item_filter(item)]
    articles = [item_parser(item) for item in items]
    
    first_item = items[0] if items else None
    vol, iss = (None, None)
    if first_item:
        if (vol_tag := first_item.find('prism:volume')): vol = vol_tag.text.strip()
        if (iss_tag := first_item.find('prism:number')): iss = iss_tag.text.strip()
    report_header = f"第{vol}卷(Vol. {vol}), 第{iss}期" if vol and iss else None
    
    return articles, report_header

# --- 各RSS期刊的解析器和过滤器 (URL提取已修复) ---
def oup_parser(item):
    desc_html = BeautifulSoup(item.description.text, 'html.parser')
    abstract_div = desc_html.find('div', class_='boxTitle')
    abstract = abstract_div.next_sibling.strip() if abstract_div and abstract_div.next_sibling else "摘要不可用"
    
    url = ""
    if link_tag := item.find('link'): url = link_tag.text.strip()
    if not url and (guid_tag := item.find('guid')): url = guid_tag.text.strip()
        
    return {'url': url or "链接未找到", 'title': item.title.text.strip(), 'authors': "", 'abstract': abstract}

def ecta_parser(item):
    abstract_html = item.find('content:encoded').text.strip()
    
    url = ""
    if link_tag := item.find('link'): url = link_tag.text.strip()
    if not url and (guid_tag := item.find('prism:url')): url = guid_tag.text.strip()

    return {'url': url or "链接未找到", 'title': item.title.text.strip(), 'authors': item.find('dc:creator').text.strip(), 'abstract': BeautifulSoup(abstract_html, 'html.parser').get_text().strip()}

def ecta_filter(item):
    return item.find('dc:creator') and item.find('dc:creator').text.strip()

def jpe_parser(item):
    url = item.get('rdf:about') or (item.find('link').text.strip() if item.find('link') else "链接未找到")
    return {'url': url, 'title': item.title.text.strip(), 'authors': item.find('dc:creator').text.strip(), 'abstract': 'PENDING_LOCAL_FETCH'}

def jpe_filter(item):
    return item.find('dc:creator') and "Ahead of Print" not in item.description.text

def qje_filter(item):
    title_tag = item.find('title')
    return title_tag and title_tag.text.strip().endswith('*')

# ==============================================================================
# 2. 核心处理逻辑 (有修改)
# ==============================================================================
def translate_with_kimi(text, kimi_client):
    if not text or "not found" in text.lower() or "not available" in text.lower() or "未提供" in text or "需访问" in text or "PENDING_LOCAL_FETCH" in text: return text
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
        "QJE": lambda: fetch_from_rss("QJE", "https://academic.oup.com/rss/site_5504/3365.xml", oup_parser, qje_filter),
        "RES": lambda: fetch_from_rss("RES", "https://academic.oup.com/rss/site_5508/3369.xml", oup_parser),
        "ECTA": lambda: fetch_from_rss("ECTA", "https://onlinelibrary.wiley.com/feed/14680262/most-recent", ecta_parser, ecta_filter),
    }

    output_data = {}
    try:
        raw_articles, report_header = fetch_map[journal_key]()
        
        # 如果抓取失败或没有文章，提前退出
        if not raw_articles and not report_header:
            log_message(f"⚠️ [{journal_key}] 未能抓取到任何文章或报告头，处理中止。")
            return

        if raw_articles:
            original_count = len(raw_articles)
            raw_articles = [article for article in raw_articles if is_research_article(article)]
            skipped_count = original_count - len(raw_articles)
            if skipped_count:
                log_message(f"  > [{journal_key}] 跳过 {skipped_count} 个非研究条目。")

        log_message(f"✅ 找到 {len(raw_articles)} 篇来自 {journal_key} 的有效文章。")

        if raw_articles:
            with ThreadPoolExecutor(max_workers=6) as executor:
                raw_articles = list(executor.map(enrich_article_metadata, raw_articles))
        
        if raw_articles:
            with ThreadPoolExecutor(max_workers=8) as executor:
                for article in raw_articles:
                    article['title_cn_future'] = executor.submit(translate_with_kimi, article['title'], kimi_client)
                    article['abstract_cn_future'] = executor.submit(translate_with_kimi, article['abstract'], kimi_client)

        processed_articles = []
        for article in raw_articles:
            title_cn = article.pop('title_cn_future').result()
            abstract_cn = article.pop('abstract_cn_future').result()
            final_article = {**article, 'title_cn': title_cn, 'abstract_cn': abstract_cn}
            processed_articles.append(final_article)
        
        output_data = {"journal_key": journal_key, "journal_full_name": full_journal_names[journal_key], "report_header": report_header or "最新一期", "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'), "articles": processed_articles}
        
    except Exception as e:
        log_message(f"❌ 处理 {journal_key} 时发生严重错误: {e}")
        output_data = {"journal_key": journal_key, "journal_full_name": full_journal_names.get(journal_key, "Unknown"), "error": str(e), "articles": []}
    
    # --- 新增：JPE文件更新检查逻辑 ---
    filename = f"{journal_key}.json"
    if journal_key == 'JPE' and os.path.exists(filename):
        log_message(f"[{journal_key}] 发现已存在文件: {filename}。开始检查是否需要更新...")
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
            
            existing_header = existing_data.get("report_header")
            new_header = output_data.get("report_header")

            log_message(f"  > 已有文件的 Header: {existing_header}")
            log_message(f"  > 新抓取数据的 Header: {new_header}")

            if existing_header and new_header and existing_header == new_header:
                log_message(f"✅ [{journal_key}] Header 一致。跳过文件写入，以保留手动编辑的内容。")
                return  # 关键步骤：直接退出函数，不执行后续的写入操作
            else:
                log_message(f"🔄 [{journal_key}] Header 不一致。将使用新数据覆盖文件。")

        except (json.JSONDecodeError, IOError, KeyError) as e:
            # 如果旧文件无法读取或解析，或者缺少关键字段，则直接覆盖
            log_message(f"⚠️ [{journal_key}] 无法读取或解析旧文件 {filename} (错误: {e})。将直接覆盖。")
    # --- 检查逻辑结束 ---

    # 只有在需要时（非JPE，或JPE需要更新）才会执行到这里
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)
    log_message(f"✅ 已将 {journal_key} 的数据写入到 {filename}")

# ==============================================================================
# 3. 程序入口 (无修改)
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
