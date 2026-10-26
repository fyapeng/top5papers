# top5papers

五大经济学期刊（AER、JPE、QJE、REStud、Econometrica）最新论文的中文导读，发布在 <https://fyapeng.com/top5papers/>。与 NBER Weekly（`fyapeng/nber`）相互独立。

## 数据来源

全部来自公开 API 或静态页面，不访问出版社的文章网页，因此不会触发人机验证。

| 步骤 | 来源 | 作用 |
|---|---|---|
| 1 | Crossref works API（按 ISSN，按记录更新日期） | 发现新文章，以及刚分到期号的旧文章；AER、QJE、REStud、Econometrica 的摘要大多在这里 |
| 1b | Crossref（按 DOI） | 每次运行回查仍处于提前在线或缺摘要的文章，补上期号、页码和出版日期 |
| 2 | OpenAlex（`OPENALEX_API_KEY`） | 补少量缺失摘要 |
| 3 | RePEc/IDEAS 文章页（按标题匹配期刊的系列目录） | 补 JPE（芝加哥大学出版社不向 Crossref 提交摘要）和 Econometrica 的缺口；也把下面第 4 步的工作论文摘要升级为期刊版 |
| 4 | OpenAlex 标题检索同名工作论文（NBER、CESifo 等） | JPE 提前在线文章在 IDEAS 收录前的临时摘要，页面上注明来源 |

仍然找不到摘要的文章标为“待补”，每次运行都会重试。

展示窗口：首次在线或正式刊出在当年 1 月 1 日以来（至少回看 6 个月，1 月时仍能看到去年秋天的文章），所以今年各期的目录是完整的；窗口外的文章在下次运行时移出。同一篇文章从提前在线到正式刊出 DOI 不变，始终是一条记录，排序按首次在线日期，不会因分到期号而跳动。SenLab 前沿页用同一窗口。标题和摘要用 DeepSeek 翻译，译文按原文缓存在 `src/data/translation-cache.json`。

## 运行

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\update.py              # 默认回看 45 天内新建或更新的记录；首次运行补齐整个窗口
.venv\Scripts\python scripts\update.py --no-translate
npm install
npm run dev                                         # http://localhost:4321/top5papers/
```

密钥放在不进 git 的 `.env`（格式见 `.env.example`），或设为环境变量：`OPENALEX_API_KEY`、`DEEPSEEK_API_KEY`、可选 `DEEPSEEK_MODEL`（默认 deepseek-chat）。

## 自动更新

`.github/workflows/site.yml`：每周一北京时间 6:00 运行（与 NBER 同为每周一次）（DeepSeek 空闲时段，价格为高峰的一半；高峰为工作日 9:00–12:00、14:00–18:00） `scripts/update.py`，提交 `src/data` 的变化，再构建并发布到 GitHub Pages；推送代码时只重新构建。需要仓库 Secrets：`DEEPSEEK_API_KEY`、`OPENALEX_API_KEY`。可选仓库变量 `DEEPSEEK_MODEL`（Settings → Secrets and variables → Actions → Variables），留空时自动选用接口列出的模型，目前是 `deepseek-flash`。

## 数据

- `src/data/papers.json`：每篇一条，含 DOI、期刊、卷期、在线日期、作者、英文与中文标题和摘要、`abstract_source`（crossref / openalex / repec / working-paper）、`first_seen`。
- `src/data/meta.json`：更新时间、各刊篇数、待补摘要数。

SenLab 的前沿页会读取 `papers.json`，与本库文献比对相近程度。
