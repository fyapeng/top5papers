# -*- coding: utf-8 -*-
# (保留所有顶部的 import，除了 tkinter 相关的)
import requests, threading, time, os, json, webbrowser, queue, argparse
from bs4 import BeautifulSoup
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ... (保留 Selenium, OpenAI, Pyperclip 的 try-except import) ...
# ... (保留 BaseExtractor 和所有子类: AERDataExtractor, JPEDataExtractor, etc.) ...
# ... (这些类的代码完全不需要修改，设计得很好！) ...

# 全局配置
CONFIG_FILE = "top5_journals_config.json" # 我们仍然可以用它来本地测试
CHROME_DRIVER_VERSION = 120 # 在GitHub Actions中，版本会被动态管理，但本地测试需要

def log_message(message):
    """简单的日志记录函数，替代GUI日志"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

def translate_with_kimi(text, kimi_client):
    if not text or "not found" in text.lower():
        return "内容缺失"
    if not kimi_client:
        log_message("警告: Kimi 客户端未初始化，跳过翻译。")
        return "翻译服务未配置"
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
    # 注意：我们这里没有传递共享的 session，因为每个进程是独立的
    extractor = extractor_class(log_callback=log_message)
    
    all_articles_data = []
    
    try:
        raw_articles = list(extractor.fetch_data())
        log_message(f"找到 {len(raw_articles)} 篇来自 {journal_key} 的文章。")

        if not raw_articles:
            log_message(f"在 {journal_key} 未找到文章，处理结束。")
        else:
            # 使用线程池处理翻译
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_article = {}
                for article in raw_articles:
                    # 提交翻译任务
                    future_title = executor.submit(translate_with_kimi, article['title'], kimi_client)
                    future_abstract = executor.submit(translate_with_kimi, article['abstract'], kimi_client)
                    future_to_article[future_title] = (article, 'title_cn')
                    future_to_article[future_abstract] = (article, 'abstract_cn')

                for future in as_completed(future_to_article):
                    article, key = future_to_article[future]
                    try:
                        translation = future.result()
                        article[key] = translation
                    except Exception as exc:
                        log_message(f"翻译生成任务失败: {exc}")
                        article[key] = "翻译出错"
            
            # 确保所有文章都已处理
            for article in raw_articles:
                if 'title_cn' not in article: article['title_cn'] = "(未翻译)"
                if 'abstract_cn' not in article: article['abstract_cn'] = "(未翻译)"
                all_articles_data.append(article)
        
        # 准备最终的JSON输出
        output_data = {
            "journal_key": journal_key,
            "journal_full_name": full_journal_names.get(journal_key, journal_key),
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            "articles": all_articles_data
        }
        
        # 将结果写入JSON文件
        output_filename = f"{journal_key}.json"
        with open(output_filename, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, ensure_ascii=False, indent=4)
        
        log_message(f"✅ 成功将 {journal_key} 的数据写入到 {output_filename}")

    except Exception as e:
        log_message(f"❌ 处理 {journal_key} 时发生严重错误: {e}")
        # 即使出错，也创建一个空的json文件，以防网页端读取失败
        with open(f"{journal_key}.json", 'w', encoding='utf-8') as f:
            json.dump({"journal_key": journal_key, "error": str(e), "articles": []}, f, ensure_ascii=False, indent=4)
            
    finally:
        # 确保浏览器被关闭
        extractor.cleanup()


def main():
    parser = argparse.ArgumentParser(description="抓取经济学期刊最新论文。")
    parser.add_argument("journal", help="要抓取的期刊代码 (e.g., AER, JPE, ALL)。")
    args = parser.parse_args()

    # 从环境变量中获取API密钥
    kimi_api_key = os.getenv('KIMI_API_KEY')
    kimi_client = None
    
    if OPENAI_AVAILABLE and kimi_api_key:
        try:
            from openai import OpenAI
            kimi_client = OpenAI(
                api_key=kimi_api_key,
                base_url="https://api.moonshot.cn/v1"
            )
            log_message("Kimi API 客户端初始化成功。")
        except Exception as e:
            log_message(f"初始化Kimi客户端失败: {e}")
    else:
        log_message("KIMI_API_KEY 环境变量未设置或 openai 库不可用，将不进行翻译。")

    journals_to_process = ["AER", "JPE", "QJE", "RES", "ECTA"]
    
    if args.journal.upper() == 'ALL':
        for journal in journals_to_process:
            process_journal(journal, kimi_client)
    elif args.journal.upper() in journals_to_process:
        process_journal(args.journal.upper(), kimi_client)
    else:
        log_message(f"错误: 不支持的期刊代码 '{args.journal}'. 请使用 'ALL' 或以下之一: {', '.join(journals_to_process)}")

if __name__ == "__main__":
    main()
