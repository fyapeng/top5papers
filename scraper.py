# -*- coding: utf-8 -*-
import requests
from bs4 import BeautifulSoup
import threading
import time
import os
import json
import argparse
import queue
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Selenium and Anti-Detection Imports ---
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    
# --- Translation ---
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ==============================================================================
# 0. 全局配置 (Global Configuration)
# ==============================================================================
# 在 GitHub Actions 中，这个超时时间会被动态增加
WAIT_TIMEOUT = 120 

def log_message(message):
    """简单的日志记录函数，替代GUI日志"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

# ==============================================================================
# 1. 数据提取器基类和实现 (Data Extractor Base & Implementations)
# ==============================================================================

class BaseExtractor:
    """所有提取器的基类"""
    def __init__(self, log_callback, session=None):
        self.log = log_callback
        self.session = session or requests.Session()
        self.driver = None
        self.wait = None

    def fetch_data(self):
        """
        这个方法应该是一个生成器 (generator), 
        使用 `yield` 返回每一篇文章的信息字典。
        """
        raise NotImplementedError("每个子类必须实现 fetch_data 方法")

    def cleanup(self):
        """清理资源，例如关闭WebDriver"""
        if self.driver:
            try:
                self.log("正在关闭 WebDriver...")
                self.driver.quit()
                self.log("✅ WebDriver 已关闭。")
            except Exception as e:
                self.log(f"关闭 WebDriver 时出错: {e}")

    def _setup_driver(self):
        """[内部方法] 初始化 undetected_chromedriver，已为无头环境优化。"""
        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium 或 undetected-chromedriver 未安装，无法抓取此期刊。")
        self.log("🚀 正在设置 undetected-chromedriver for Headless Environment...")
        options = uc.ChromeOptions()
        
        # --- 针对 GitHub Actions 的关键修改 ---
        options.add_argument("--headless=new")
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        try:
            chrome_version = 137 
            self.log(f"   - 强制使用 Chrome v{chrome_version} 对应的驱动...")
            self.driver = uc.Chrome(options=options, use_subprocess=True, version_main=chrome_version)
            
            # 在CI环境中增加等待时间
            global WAIT_TIMEOUT
            WAIT_TIMEOUT = 180
            self.wait = WebDriverWait(self.driver, WAIT_TIMEOUT)
            self.log("✅ undetected-chromedriver 初始化成功。")
            return True
        except WebDriverException as e:
            self.log(f"❌ WebDriver 错误: {e}")
            self.log("   - 自动化环境中的浏览器初始化失败。检查Chrome版本和驱动设置。")
            raise ConnectionError(f"无法初始化浏览器驱动: {e}")
        except Exception as e:
            self.log(f"❌ 初始化 undetected-chromedriver 失败: {e}")
            raise ConnectionError(f"初始化浏览器驱动时发生未知错误: {e}")

    def _save_debug_info(self):
        """在抓取失败时保存截图和HTML源码"""
        if not self.driver:
            return
        
        debug_dir = "debug_output"
        os.makedirs(debug_dir, exist_ok=True)
        
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        
        # 保存截图
        screenshot_path = os.path.join(debug_dir, f"error_screenshot_{timestamp}.png")
        try:
            self.driver.save_screenshot(screenshot_path)
            self.log(f"  ℹ️  截图已保存到: {screenshot_path}")
        except Exception as e:
            self.log(f"  ❌ 保存截图失败: {e}")

        # 保存页面HTML
        html_path = os.path.join(debug_dir, f"error_page_{timestamp}.html")
        try:
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(self.driver.page_source)
            self.log(f"  ℹ️  页面HTML已保存到: {html_path}")
        except Exception as e:
            self.log(f"  ❌ 保存HTML失败: {e}")

class AERDataExtractor(BaseExtractor):
    """从 American Economic Review (AER) 网站提取论文信息。"""
    def __init__(self, log_callback, session=None):
        super().__init__(log_callback)
        self.base_url = 'https://www.aeaweb.org'
        self.current_issue_url = f'{self.base_url}/journals/aer/current-issue'

        # 创建一个带有更逼真请求头的 session
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })
        self.log("AERDataExtractor 已使用强化请求头进行初始化。")

    def fetch_data(self):
        self.log("🔍 [AER] 正在获取期刊主页以提取文章ID...")
        try:
            self.log(f"   - 正在访问: {self.current_issue_url}")
            response = self.session.get(self.current_issue_url, timeout=45)
            response.raise_for_status()
            self.log("   - 主页访问成功，状态码: " + str(response.status_code))
        except requests.exceptions.RequestException as e:
            self.log(f"❌ 访问AER期刊主页失败: {e}")
            if 'response' in locals(): self.log(f"   - 响应内容预览: {response.text[:500]}")
            raise ConnectionError(f"无法访问AER期刊主页: {e}") from e

        if "Checking if the site connection is secure" in response.text or "human" in response.text.lower():
            self.log("❌ [AER] 检测到机器人验证页面，抓取失败。")
            return

        soup = BeautifulSoup(response.content, 'html.parser')
        all_articles = soup.find_all('article', class_='journal-article')
        
        symposia_title = soup.find('article', class_='journal-article symposia-title')
        target_articles = all_articles
        if symposia_title:
            try:
                symposia_index = all_articles.index(symposia_title)
                target_articles = all_articles[symposia_index + 1:]
            except ValueError:
                self.log("⚠️ [AER] 未找到 symposia-title 的确切位置，将尝试抓取所有文章。")

        article_ids = [a.get('id') for a in target_articles if a.get('id') and 'symposia-title' not in a.get('class', [])]
        self.log(f"✅ [AER] 找到 {len(article_ids)} 篇待处理文章。")

        if not article_ids:
            return

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_id = {executor.submit(self._get_single_article_details, aid): aid for aid in article_ids}
            for future in as_completed(future_to_id):
                result = future.result()
                if result:
                    yield result

    def _get_single_article_details(self, article_id: str):
        article_url = f'{self.base_url}/articles?id={article_id}'
        try:
            response = self.session.get(article_url, timeout=45)
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
        except requests.exceptions.RequestException as e:
            self.log(f"  ❌ 抓取AER文章 {article_id} 失败: {e}")
            return None

class JPEDataExtractor(BaseExtractor):
    """从 Journal of Political Economy (JPE) 网站提取论文信息。"""
    def fetch_data(self):
        self._setup_driver()
        base_url = "https://www.journals.uchicago.edu"
        current_issue_url = f"{base_url}/toc/jpe/current"
        self.log(f"正在导航至JPE主页: {current_issue_url}")
        self.driver.get(current_issue_url)

        try:
            self.log(f"⏳ 正在等待JPE文章列表加载...")
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.form-action-item")))
            list_items = self.driver.find_elements(By.CSS_SELECTOR, "li.form-action-item")
            urls = []
            for item in list_items:
                try:
                    item.find_element(By.CSS_SELECTOR, ".issue-item__loa")
                    link_element = item.find_element(By.CSS_SELECTOR, ".issue-item__title a")
                    href = link_element.get_attribute('href')
                    urls.append(f"{base_url}{href}" if href.startswith('/') else href)
                except NoSuchElementException:
                    continue
            
            self.log(f"✅ 找到 {len(urls)} 篇JPE文章，开始逐一抓取详情...")
            for url in urls:
                details = self._get_single_article_details(url)
                if details:
                    yield details
        
        except TimeoutException:
            self.log(f"❌ 错误: 在 {WAIT_TIMEOUT} 秒内未能加载JPE文章列表。")
            self._save_debug_info() # 保存调试信息
        finally:
            self.cleanup()

    def _get_single_article_details(self, article_url):
        self.log(f"  > 正在访问JPE文章: {article_url.split('/')[-1]}")
        try:
            self.driver.get(article_url)
            title = self.wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "h1.citation__title"))).text.strip()
            author_container = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".accordion-tabbed.loa-accordion")))
            authors = ", ".join([span.text.strip() for span in author_container.find_elements(By.CSS_SELECTOR, "a.author-name span")]) or "作者未找到"
            abstract = self.wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".abstractSection.abstractInFull p"))).text.strip()
            return {'url': article_url, 'title': title, 'authors': authors, 'abstract': abstract}
        except Exception as e:
            self.log(f"  ❌ 抓取JPE文章 {article_url} 详情失败: {e}")
            self._save_debug_info() # 保存调试信息
            return None

class OUPBaseExtractor(BaseExtractor):
    """为OUP平台(QJE, RES)设计的基类提取器"""
    journal_code = "" # e.g., "qje", "restud"
    
    def fetch_data(self):
        self._setup_driver()
        base_url = "https://academic.oup.com"
        current_issue_url = f"{base_url}/{self.journal_code}/issue"
        self.log(f"正在导航至 {self.journal_code.upper()} 主页: {current_issue_url}")
        self.driver.get(current_issue_url)

        try:
            self.log(f"⏳ 正在等待 {self.journal_code.upper()} 文章列表加载...")
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".al-article-item-wrap.al-normal")))
            list_items = self.driver.find_elements(By.CSS_SELECTOR, ".al-article-item-wrap.al-normal")
            urls = []
            for item in list_items:
                try:
                    item.find_element(By.CSS_SELECTOR, ".al-authors-list")
                    link_element = item.find_element(By.CSS_SELECTOR, "h5.item-title a.at-articleLink")
                    href = link_element.get_attribute('href')
                    urls.append(href if href.startswith('http') else f"{base_url}{href}")
                except NoSuchElementException:
                    continue
            
            self.log(f"✅ 找到 {len(urls)} 篇 {self.journal_code.upper()} 文章，开始逐一抓取详情...")
            for url in urls:
                details = self._get_single_article_details(url)
                if details:
                    yield details
        
        except TimeoutException:
            self.log(f"❌ 错误: 在 {WAIT_TIMEOUT} 秒内未能加载 {self.journal_code.upper()} 文章列表。")
            self._save_debug_info()
        finally:
            self.cleanup()

    def _get_single_article_details(self, article_url):
        self.log(f"  > 正在访问 {self.journal_code.upper()} 文章: {article_url.split('/')[-1]}")
        try:
            self.driver.get(article_url)
            title = self.wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "h1.wi-article-title"))).text.strip()
            authors_container = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".al-authors-list")))
            author_elements = authors_container.find_elements(By.CSS_SELECTOR, ".al-author-name-role > a")
            if not author_elements: author_elements = authors_container.find_elements(By.TAG_NAME, "a")
            authors_list = [elem.text.strip().strip(',') for elem in author_elements]
            authors = ", ".join([name for name in authors_list if name]) or "作者未找到"
            abstract_container = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "section.abstract")))
            abstract = abstract_container.find_element(By.CSS_SELECTOR, "p.chapter-para").text.strip()
            return {'url': article_url, 'title': title, 'authors': authors, 'abstract': abstract}
        except Exception as e:
            self.log(f"  ❌ 抓取 {self.journal_code.upper()} 文章 {article_url} 详情失败: {e}")
            self._save_debug_info()
            return None

class QJEDataExtractor(OUPBaseExtractor):
    journal_code = "qje"

class RESDataExtractor(OUPBaseExtractor):
    journal_code = "restud"

class ECTADataExtractor(BaseExtractor):
    """从 Econometrica (ECTA) 网站提取论文信息。"""
    def fetch_data(self):
        self._setup_driver()
        base_url = "https://onlinelibrary.wiley.com"
        current_issue_url = f"{base_url}/toc/14680262/current"
        self.log(f"正在导航至ECTA主页: {current_issue_url}")
        self.driver.get(current_issue_url)

        try:
            self.log("⏳ 正在等待ECTA文章列表加载...")
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.issue-item")))
            list_items = self.driver.find_elements(By.CSS_SELECTOR, "div.issue-item")
            urls = []
            for item in list_items:
                try:
                    link_element = item.find_element(By.CSS_SELECTOR, "a.issue-item__title.visitable")
                    href = link_element.get_attribute('href')
                    doi_part = href.split('/')[-1]
                    if doi_part and doi_part[-1].isdigit():
                        urls.append(f"{base_url}{href}" if href.startswith('/') else href)
                except NoSuchElementException:
                    continue
            
            self.log(f"✅ 找到 {len(urls)} 篇ECTA文章，开始逐一抓取详情...")
            for url in urls:
                details = self._get_single_article_details(url)
                if details:
                    yield details

        except TimeoutException:
            self.log(f"❌ 错误: 在 {WAIT_TIMEOUT} 秒内未能加载ECTA文章列表。")
            self._save_debug_info()
        finally:
            self.cleanup()

    def _get_single_article_details(self, article_url):
        self.log(f"  > 正在访问ECTA文章: {article_url.split('/')[-1]}")
        try:
            self.driver.get(article_url)
            title = self.wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "h1.citation__title"))).text.strip()
            author_container = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.accordion-tabbed")))
            authors = ", ".join([span.text.strip() for span in author_container.find_elements(By.CSS_SELECTOR, "a.author-name > span")]) or "作者未找到"
            abstract_container = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.article-section__content.en.main")))
            abstract = "\n\n".join([p.text.strip() for p in abstract_container.find_elements(By.TAG_NAME, "p")]) or "摘要未找到"
            return {'url': article_url, 'title': title, 'authors': authors, 'abstract': abstract}
        except Exception as e:
            self.log(f"  ❌ 抓取ECTA文章 {article_url} 详情失败: {e}")
            self._save_debug_info()
            return None

# ==============================================================================
# 2. 翻译与主逻辑 (Translation & Main Logic)
# ==============================================================================

def translate_with_kimi(text, kimi_client):
    if not text or "not found" in text.lower():
        return "内容缺失"
    if not kimi_client:
        log_message("警告: Kimi 客户端未初始化，跳过翻译。")
        return "(未翻译)"
    try:
        response = kimi_client.chat.completions.create(
            model="moonshot-v1-8k",
            messages=[
                {"role": "system", "content": "你是一个专业的经济学领域翻译助手。请将用户提供的英文文本准确、流畅地翻译成中文。请直接输出翻译结果，不要包含任何额外说明或客套话。"},
                {"role": "user", "content": text}
            ],
            temperature=0.3, max_tokens=2000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log_message(f"Kimi翻译失败: {str(e)[:100]}...")
        return f"翻译失败: {e}"

def process_journal(journal_key, kimi_client):
    """处理单个期刊的抓取和翻译"""
    extractors = {
        "AER": AERDataExtractor, "JPE": JPEDataExtractor,
        "QJE": QJEDataExtractor, "RES": RESDataExtractor,
        "ECTA": ECTADataExtractor,
    }
    
    full_journal_names = {
        "AER": "American Economic Review", "JPE": "Journal of Political Economy",
        "QJE": "The Quarterly Journal of Economics", "RES": "The Review of Economic Studies",
        "ECTA": "Econometrica",
    }

    if journal_key not in extractors:
        log_message(f"错误: 未知的期刊代码 '{journal_key}'")
        return

    log_message(f"--- 开始处理: {journal_key} ---")
    extractor_class = extractors[journal_key]
    extractor = extractor_class(log_callback=log_message)
    
    processed_articles = []
    
    try:
        raw_articles = list(extractor.fetch_data())
        log_message(f"找到 {len(raw_articles)} 篇来自 {journal_key} 的文章。")

        if not raw_articles:
            log_message(f"在 {journal_key} 未找到文章，处理结束。")
        else:
            with ThreadPoolExecutor(max_workers=5) as executor:
                # 提交所有翻译任务
                future_to_article = {
                    executor.submit(translate_with_kimi, article['abstract'], kimi_client): (article, 'abstract_cn')
                    for article in raw_articles
                }
                future_to_article.update({
                    executor.submit(translate_with_kimi, article['title'], kimi_client): (article, 'title_cn')
                    for article in raw_articles
                })

                # 获取翻译结果并更新文章字典
                for future in as_completed(future_to_article):
                    article, key = future_to_article[future]
                    try:
                        translation = future.result()
                        article[key] = translation
                    except Exception as exc:
                        log_message(f"翻译任务失败: {exc}")
                        article[key] = "翻译出错"
            
            # 整理结果
            processed_articles = raw_articles
        
        # 准备最终的JSON输出
        output_data = {
            "journal_key": journal_key,
            "journal_full_name": full_journal_names.get(journal_key, journal_key),
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            "articles": processed_articles
        }
        
    except Exception as e:
        log_message(f"❌ 处理 {journal_key} 时发生严重错误: {e}")
        output_data = {
            "journal_key": journal_key,
            "journal_full_name": full_journal_names.get(journal_key, journal_key),
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            "error": str(e),
            "articles": []
        }
    finally:
        extractor.cleanup()

    # 将结果写入JSON文件
    output_filename = f"{journal_key}.json"
    with open(output_filename, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=4)
    log_message(f"✅ 已将 {journal_key} 的数据写入到 {output_filename}")


# ==============================================================================
# 3. 程序入口 (Application Entry Point)
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="抓取经济学期刊最新论文。")
    parser.add_argument("journal", help="要抓取的期刊代码 (e.g., AER, JPE, ALL)。")
    args = parser.parse_args()

    # 从环境变量中获取API密钥
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

    journals_to_process = ["AER", "JPE", "QJE", "RES", "ECTA"]
    
    if args.journal.upper() == 'ALL':
        # 在CI环境中，我们通过矩阵策略并行运行，所以不需要这个分支
        # 但本地测试时仍然有用
        for journal in journals_to_process:
            process_journal(journal, kimi_client)
    elif args.journal.upper() in journals_to_process:
        process_journal(args.journal.upper(), kimi_client)
    else:
        log_message(f"错误: 不支持的期刊代码 '{args.journal}'. 请使用 'ALL' 或以下之一: {', '.join(journals_to_process)}")

if __name__ == "__main__":
    main()
