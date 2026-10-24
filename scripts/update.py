"""Track new articles in the top five economics journals.

Sources, in order, all public APIs or static pages (no publisher article pages, so no bot checks):
  1. Crossref works API by ISSN: discovers new articles and gives most AER/QJE/REStud/Econometrica abstracts.
  2. OpenAlex (with OPENALEX_API_KEY): fills a few remaining abstracts.
  3. RePEc/IDEAS article pages, matched by title from each journal's series listing: fills JPE (whose
     publisher deposits no abstracts with Crossref) and the Econometrica gaps. Articles IDEAS has not
     indexed yet stay "pending" and are retried on the next run.
Titles and abstracts are translated with DeepSeek (DEEPSEEK_API_KEY); translations are cached by source text.

  python scripts/update.py                 # update data in src/data
  python scripts/update.py --days 180      # look further back (first run)
  python scripts/update.py --no-translate  # metadata and abstracts only
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "data"
PAPERS = DATA / "papers.json"
META = DATA / "meta.json"
CACHE = DATA / "translation-cache.json"
GLOSSARY = Path(__file__).with_name("translation_glossary.json")

JOURNALS = {
    "AER": {"name": "American Economic Review", "issn": "0002-8282", "ideas": "aea/aecrev"},
    "JPE": {"name": "Journal of Political Economy", "issn": "0022-3808", "ideas": "ucp/jpolec"},
    "QJE": {"name": "Quarterly Journal of Economics", "issn": "0033-5533", "ideas": "oup/qjecon"},
    "RES": {"name": "Review of Economic Studies", "issn": "0034-6527", "ideas": "oup/restud"},
    "ECTA": {"name": "Econometrica", "issn": "0012-9682", "ideas": "ecm/emetrp"},
}

# issue furniture rather than research articles
NON_RESEARCH = re.compile(
    r"front\s*matter|back\s*matter|\brecent referees\b|acknowledg(?:e)?ments? of referees|turnaround times|"
    r"\breport of the\b|^report of independent auditor|annual report|\bnobel lecture\b|fisher.?schultz lecture|\blecture$|^comment on\b|: comment$|"
    r"\ba comment$|^reply\b|: reply$|^correction to\b|\berrat(?:um|a)\b|\bcorrigend|\bretraction\b|^editorial\b|"
    r"^editor'?s? (?:note|introduction)|^index\b|volume information|^announcements?\b|^minutes of\b|^in memoriam\b|"
    r"^obituary|^issue information|^masthead|^fellows of\b|^submission|^the econometric society\b|^contents\b|^notes?$",
    re.I,
)

UA = {"User-Agent": "top5papers (https://fyapeng.com/top5papers/)"}
session = requests.Session()
session.headers.update(UA)


def env(name: str) -> str | None:
    """Environment variable, else the git-ignored .env at the repo root."""
    if os.environ.get(name):
        return os.environ[name]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip()
    return None


def window_start(today: date) -> date:
    """Papers shown: since 1 January, but never less than six months back, so January still shows the autumn.
    SenLab's frontier page uses the same window."""
    return min(date(today.year, 1, 1), today - timedelta(days=183))


def load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def get(url: str, **kw) -> requests.Response:
    for attempt in range(4):
        try:
            r = session.get(url, timeout=60, **kw)
        except requests.RequestException as e:
            if attempt == 3:
                raise
            logging.warning("%s: %s; retrying", url.split("?")[0], type(e).__name__)
        else:
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                return r
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean(s).lower())


def ymd(parts: dict | None) -> str:
    p = ((parts or {}).get("date-parts") or [[None]])[0]
    if not p or p[0] is None:
        return ""
    return "-".join(f"{x:02d}" if i else str(x) for i, x in enumerate((p + [1, 1])[:3]))


# ---------- 1. Crossref ----------

def crossref(key: str, since: date) -> list[dict]:
    j = JOURNALS[key]
    out, cursor = [], "*"
    while cursor:
        r = get(f"https://api.crossref.org/journals/{j['issn']}/works", params={
            "filter": f"type:journal-article,from-created-date:{since.isoformat()}",
            "select": "DOI,title,author,abstract,volume,issue,page,published-online,published-print,created",
            "rows": 200, "cursor": cursor,
        })
        r.raise_for_status()
        msg = r.json()["message"]
        items = msg.get("items", [])
        for w in items:
            title = clean((w.get("title") or [""])[0])
            if not title or NON_RESEARCH.search(title):
                continue
            abstract = clean(w.get("abstract", ""))
            abstract = re.sub(r"^(?:abstract|summary)\s*[:.]?\s*", "", abstract, flags=re.I)
            out.append({
                "doi": w["DOI"].lower(), "journal": key, "title": title,
                "authors": [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "") for a in w.get("author", [])],
                "abstract": abstract, "abstract_source": "crossref" if abstract else "",
                "volume": w.get("volume", ""), "issue": w.get("issue", ""), "pages": w.get("page", ""),
                "online": ymd(w.get("published-online")) or ymd(w.get("created")),
                "print": ymd(w.get("published-print")),
            })
        cursor = msg.get("next-cursor") if len(items) == 200 else None
    return out


# ---------- 2. OpenAlex ----------

def openalex_abstracts(dois: list[str]) -> dict[str, str]:
    key = env("OPENALEX_API_KEY")
    found: dict[str, str] = {}
    for i in range(0, len(dois), 40):
        chunk = dois[i:i + 40]
        params = {"filter": "doi:" + "|".join(chunk), "per-page": 50, "select": "doi,abstract_inverted_index"}
        if key:
            params["api_key"] = key
        r = get("https://api.openalex.org/works", params=params)
        if r.status_code != 200:
            logging.warning("OpenAlex HTTP %s; skipping", r.status_code)
            return found
        for w in r.json().get("results", []):
            inv = w.get("abstract_inverted_index")
            if inv:
                words = sorted((p, word) for word, ps in inv.items() for p in ps)
                found[w["doi"].lower().removeprefix("https://doi.org/")] = " ".join(word for _, word in words)
    return found


def working_paper_abstract(title: str) -> tuple[str, str]:
    """Abstract of a working-paper version with the same title (NBER, CEPR, SSRN...), via OpenAlex search.
    Stopgap for ahead-of-print articles; replaced by the journal abstract once IDEAS indexes the issue."""
    params = {"search": title, "per-page": 8, "select": "title,type,primary_location,abstract_inverted_index"}
    if env("OPENALEX_API_KEY"):
        params["api_key"] = env("OPENALEX_API_KEY")
    r = get("https://api.openalex.org/works", params=params)
    if r.status_code != 200:
        return "", ""
    nt = norm_title(title)
    for w in r.json().get("results", []):
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        if (w.get("abstract_inverted_index") and w.get("type") not in ("dataset", "paratext", "peer-review")
                and "dataverse" not in src.lower() and norm_title(w.get("title") or "")[:50] == nt[:50]):
            words = sorted((p, word) for word, ps in w["abstract_inverted_index"].items() for p in ps)
            return " ".join(word for _, word in words), src
    return "", ""


# ---------- 3. RePEc / IDEAS ----------

def ideas_listing(series: str, pages: int = 2) -> dict[str, str]:
    """normalized title -> IDEAS article URL, from the newest pages of a journal's series listing."""
    links: dict[str, str] = {}
    for n in range(1, pages + 1):
        r = get(f"https://ideas.repec.org/s/{series}{'' if n == 1 else n}.html")
        if r.status_code != 200:
            break
        r.encoding = "utf-8"
        for href, t in re.findall(rf'<A HREF="(/a/{series}/[^"]+)">(.*?)</A>', r.text, re.I):
            links.setdefault(norm_title(t), "https://ideas.repec.org" + href)
        time.sleep(1.5)
    return links


def ideas_abstract(url: str) -> str:
    r = get(url)
    if r.status_code != 200:
        return ""
    r.encoding = "utf-8"
    m = re.search(r'id="abstract-body"[^>]*>(.*?)</div>', r.text, re.S)
    text = clean(m.group(1)) if m else ""
    return "" if not text or text.lower().startswith("no abstract is available") else text


# ---------- translation ----------

def build_prompt() -> str:
    g = load(GLOSSARY, {})
    lines = [
        "你是给经济学研究者阅读五大经济学期刊（AER、JPE、QJE、REStud、Econometrica）论文的中文翻译助手。",
        "请使用中国大陆经济学学术写作中常见、准确、克制的译法。只输出译文，不要添加解释、标题、引号或项目符号。",
        "",
        "翻译原则：",
        "1. 优先准确传达经济学含义，不做软件、日常口语或新闻化误译。",
        "2. 论文标题译成简洁的学术标题；摘要译成自然的中文学术段落。",
        "3. 保留作者名、模型名、数据集名、缩写和必要专有名词。",
        "4. 不确定的专有概念宁可保留英文括注，也不要生造术语。",
        "5. 不得省略限定条件、因果方向、比较对象或否定词；数字、公式符号和缩写完整保留。",
        "",
        "术语约束：",
    ]
    for t in g.get("prompt_terms", []):
        if t.get("source") and t.get("target"):
            lines.append(f"- {t['source']} -> {t['target']}" + (f"；{t['note']}" if t.get("note") else ""))
    return "\n".join(lines)


def ok_translation(src: str, out: str, field: str) -> bool:
    zh = len(re.findall(r"[㐀-鿿]", out))
    if not out or out.strip().lower() == src.strip().lower() or zh == 0:
        return False
    return not (field == "abstract" and len(src) >= 240 and (zh < 20 or len(out) < len(src) * 0.12))


class Translator:
    def __init__(self, enabled: bool):
        self.cache = load(CACHE, {})
        self.client = None
        key = env("DEEPSEEK_API_KEY")
        if enabled and key:
            from openai import OpenAI
            self.client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
            self.model = env("DEEPSEEK_MODEL") or "deepseek-chat"
            self.prompt = build_prompt()
        elif enabled:
            logging.warning("DEEPSEEK_API_KEY not set; skipping translation")

    def __call__(self, field: str, text: str) -> str:
        if not text:
            return ""
        k = hashlib.sha256(f"{field}\n{text}".encode()).hexdigest()[:20]
        if k in self.cache:
            return self.cache[k]
        if not self.client:
            return ""
        for attempt in range(3):
            try:
                r = self.client.chat.completions.create(model=self.model, temperature=0.1, messages=[
                    {"role": "system", "content": self.prompt},
                    {"role": "user", "content": f"请翻译以下论文{'标题' if field == 'title' else '摘要'}：\n\n{text}"},
                ])
                out = re.sub(r"^(?:译文|中文翻译|翻译)\s*[：:]\s*", "", (r.choices[0].message.content or "").strip())
                if ok_translation(text, out, field):
                    self.cache[k] = out
                    return out
                logging.warning("rejected %s translation (attempt %d)", field, attempt + 1)
            except Exception as e:  # noqa: BLE001 - the client raises several exception families
                logging.warning("DeepSeek error: %s", e)
                time.sleep(5 * (attempt + 1))
        return ""


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="look back this many days of new DOIs (default 45, or the whole window on first run)")
    ap.add_argument("--no-translate", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    DATA.mkdir(parents=True, exist_ok=True)
    # re-apply the filter so stored items it now excludes drop out
    papers = {p["doi"]: p for p in load(PAPERS, []) if not NON_RESEARCH.search(p["title"])}
    start = window_start(date.today())
    # routine runs only look for recently registered DOIs; a first run fills the whole window
    since = date.today() - timedelta(days=args.days) if args.days else (date.today() - timedelta(days=45) if papers else start)
    today = date.today().isoformat()

    # 1. discover / refresh
    for key in JOURNALS:
        found = crossref(key, since)
        new = 0
        for w in found:
            old = papers.get(w["doi"])
            if old is None:
                papers[w["doi"]] = {**w, "first_seen": today, "title_cn": "", "abstract_cn": ""}
                new += 1
            else:  # issue assignment and abstracts can arrive later; keep fills from other sources
                for f in ("title", "authors", "volume", "issue", "pages", "online", "print"):
                    if w[f]:
                        old[f] = w[f]
                if w["abstract"] and old.get("abstract_source") in ("", "crossref"):
                    old["abstract"], old["abstract_source"] = w["abstract"], "crossref"
        logging.info("%s: %d articles since %s, %d new", key, len(found), since, new)
        time.sleep(1)

    # 2-3. fill missing abstracts
    missing = [p for p in papers.values() if not p.get("abstract")]
    if missing:
        oa = openalex_abstracts([p["doi"] for p in missing])
        for p in missing:
            if oa.get(p["doi"]):
                p["abstract"], p["abstract_source"] = oa[p["doi"]], "openalex"
        logging.info("OpenAlex filled %d of %d", len(oa), len(missing))
    # IDEAS also upgrades working-paper abstracts to the journal's own once the issue is indexed
    for key, j in JOURNALS.items():
        todo = [p for p in papers.values() if p["journal"] == key and p.get("abstract_source") in ("", "working-paper")]
        if not todo:
            continue
        listing = ideas_listing(j["ideas"])
        filled = 0
        for p in todo:
            nt = norm_title(p["title"])
            url = listing.get(nt) or next((u for t, u in listing.items() if t[:60] == nt[:60] and len(nt) > 30), None)
            if url:
                text = ideas_abstract(url)
                time.sleep(1.5)
                if text:
                    p["abstract"], p["abstract_source"], p["ideas"] = text, "repec", url
                    p.pop("wp_source", None)
                    filled += 1
        logging.info("%s: IDEAS filled %d of %d", key, filled, len(todo))

    still = [p for p in papers.values() if not p.get("abstract")]
    for p in still:
        text, src = working_paper_abstract(p["title"])
        if text:
            p["abstract"], p["abstract_source"], p["wp_source"] = text, "working-paper", src
        time.sleep(0.5)
    logging.info("working-paper versions filled %d of %d", sum(bool(p.get("abstract")) for p in still), len(still))

    # translation
    tr = Translator(not args.no_translate)

    def translate(p: dict) -> None:
        p["title_cn"] = p.get("title_cn") or tr("title", p["title"])
        if p.get("abstract") and not p.get("abstract_cn_src") == p["abstract"][:80]:
            cn = tr("abstract", p["abstract"])
            if cn:
                p["abstract_cn"], p["abstract_cn_src"] = cn, p["abstract"][:80]

    # papers are independent, so translate several at a time
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(translate, sorted(papers.values(), key=lambda x: x["online"], reverse=True)))
    save(CACHE, tr.cache)

    for p in papers.values():  # some sources drop the space between sentences ("results.We show")
        p["abstract"] = re.sub(r"(?<=[a-z0-9][.?!])(?=[A-Z][a-z])", " ", p.get("abstract", ""))
    rows = sorted((p for p in papers.values() if p["online"] >= start.isoformat()), key=lambda x: (x["online"], x["doi"]), reverse=True)
    save(PAPERS, rows)
    save(META, {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "since": start.isoformat(),
        "journals": {k: {"name": j["name"], "count": sum(p["journal"] == k for p in rows)} for k, j in JOURNALS.items()},
        "pending_abstracts": sum(not p.get("abstract") for p in rows),
        "untranslated": sum(bool(p.get("abstract")) and not p.get("abstract_cn") for p in rows),
    })
    logging.info("%d papers; %d without abstract", len(rows), sum(not p.get("abstract") for p in rows))


if __name__ == "__main__":
    main()
