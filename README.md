# ReproHub · 生殖医学前沿

> 面向生殖医学、辅助生殖及相关基础研究的 **信息聚合 · 文献解读 · 阅读管理 · 推送** 平台。
> 自己找文献、自己写日报，但不只是周报机器人。

框架思路参考 [AIHOT](https://github.com/KKKKhazix/AIHOT)（一个自己找热点、自己写日报的网站框架），界面风格参考 [Bear Blog](https://bearblog.dev/) 的极简主义。
技术栈：Python 3.11+ · FastAPI · SQLite（FTS5）· Jinja2 · 原生 CSS/JS，零外部服务，`pip install` 即可运行。

---

## 它做什么

| 能力 | 说明 |
|---|---|
| **聚合** | PubMed E-utilities、Europe PMC（含预印本）、bioRxiv/medRxiv、Crossref（核心期刊 ISSN）、期刊/媒体 RSS、通用 JSON API、HTML 列表页、API 推送入库。按 DOI / PMID / URL / 标题指纹去重；同一篇被多源收录只保留一条并记录「另外 N 个来源也收录」。 |
| **领域门槛** | 22 个主题覆盖生殖与辅助生殖全领域（早期胚胎、卵母细胞、精子/男性、ART 临床、PGT、胚胎实验室、内膜与植入、生殖内分泌、生殖遗传、生殖干细胞/IVG、早孕与流产、生育力保存、生殖免疫、微生物组、避孕与生殖健康、生殖毒理、生殖衰老、AI、伦理法规、产业政策、动物模型、多组学方法）。**属于该领域是收录门槛**；横切主题（组学方法 / AI / 动物模型）单独命中不算在领域内。 |
| **重点偏好（只加权）** | 早期胚胎转录组 + 蛋白质组联合分析、胚胎/卵母细胞蛋白质组、胚胎单细胞转录组、多组学整合、母源-合子转换、人类胚胎材料、胚胎表观组、干细胞胚胎模型、可转化到 ART 实验室的发现。命中后加分提升排序与入选概率，**不是收录条件**，未命中的领域内内容照常收录。 |
| **期刊等级与影响因子** | `config/journals.yaml` 注册表（125 种，ISSN / 别名 / 正则匹配）把期刊分为 **CNS / 大子刊 / 综合顶刊 / 领域顶刊 / 普通 / 高发文量 OA 刊**，分别加权 +12 / +8 / +6 / +4 / 0 / −3 进入最终分；每条标注影响因子（近似 JCR 值，明确标注来源，可改、可 CSV 导入覆盖；未收录显示「IF 未收录」且不加减分）。专门的 PubMed（限定 40 余种顶刊 [ta]）与 Crossref（22 个顶刊 ISSN）来源保证 CNS/大子刊的生殖相关文章能被抓到；`/top` 页、`tier=` 筛选、按 IF 排序、`/feed/top.xml`、`top-journal-alert` 推送规则。 |
| **评分与入选** | 模型按新颖性 / 严谨性 / 领域影响 / 领域相关 / 转化价值五维评分 0-100，加偏好与来源等级修正得最终分；入选阈值按来源等级（期刊一手 60 / 学会机构 62 / 媒体 68）与类型微调。可选「评两次取均值并标记分歧」。 |
| **文献解读** | 高分文献自动生成结构化中文解读：一句话结论 / 背景 / 设计与方法 / 主要发现 / 多组学视角 / 意义 / 局限 / 值得跟进 / 关键术语；其余在条目页一键按需生成。 |
| **事件与热度** | 近 14 天相关条目聚为事件（标题/摘要 Jaccard + 模型确认灰区）；热度按**独立来源**计（48 小时内每来源一次，24 小时减半，按来源等级加权），同一媒体多次报道不重复计热。 |
| **日报 / 周报 / 月报** | 日报按本地日历日由规则编纂（当天持续更新，次日 08:00 定稿）；周报（周一）与月报（1 日）从日报汇编，模型只写总述与栏目导读。 |
| **阅读管理** | 未读 / 在读 / 已读、星标、稍后读、五星评分、标签、Markdown 笔记、按课题分集合、只看未读；导出 Markdown / JSON / CSV / BibTeX，JSON 导入。 |
| **推送** | 日报与符合规则的高分条目推送到：Markdown 文件（可指向 Obsidian 仓库）、Server 酱、Bark、ntfy、飞书、企业微信、钉钉、Telegram、SMTP 邮件、通用 Webhook。规则可按最低分 / 偏好分 / 主题 / 类型过滤。 |
| **对外出口** | RSS（精选 / 全部 / 文献 / 资讯 / 偏好 / 按主题 / 版次）、JSON API（`/openapi.json`、`/docs`）、`/llms.txt`、`/agent` 接入说明。 |
| **模型与思考强度** | 首选 **DeepSeek**（官方 `deepseek-flash` / `deepseek-v4-pro`）；Gemini、Grok 等 OpenAI 兼容网关，通义、智谱、Moonshot、OpenAI、Ollama、自定义端点可切换。统一的思考强度词汇 `off / low / medium / high` 按提供商映射（`reasoning_effort`、DeepSeek `thinking.disabled`、通义 `thinking_budget` …）。提供商 / 模型 / 强度可在环境变量、每个流水线步骤、运行时（CLI 标志、`reprohub llm set`、后台表单、API）四个层级设置，`provider/model` 简写一处切换两者。每次调用记录提供商、模型、强度与推理 tokens。 |
| **模拟模式** | 没有 API key 时进入**明确标识的模拟模式**：评分由规则生成、解读只抽取原文句子、周报总述只做统计；所有这类结果在数据库、页面、RSS、API 中都带 `is_mock=true`，页首有横幅，**绝不伪报为真实模型调用**。`LLM_ALLOW_MOCK_FALLBACK=false` 可改为缺 key 直接报错。 |

---

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m reprohub init          # 生成 .env、建库、同步来源
```

编辑 `.env`，填入 `DEEPSEEK_API_KEY=sk-...`（不填也能跑，只是模拟模式）。

```bash
python -m reprohub pipeline      # 采集 → 预筛/评分 → 解读 → 聚类 → 日报 → 推送（首次约 1-3 分钟）
python -m reprohub serve         # http://127.0.0.1:8008
```

没网络、想先看看界面：

```bash
python -m reprohub demo-seed && python -m reprohub pipeline --no-collect
```

长期运行（网站 + 后台调度：每小时采集，08:00 日报、周一 10:00 周报、1 日 10:30 月报）：

```bash
python -m reprohub serve --scheduler
```

或者只跑调度交给 systemd / cron：`python -m reprohub run-forever`，亦可 cron 每小时 `python -m reprohub pipeline`。

安装为命令：`pip install -e .` 后可直接 `reprohub <command>`。

---

## 命令

```
reprohub init                      初始化 .env / 数据库
reprohub status                    状态、LLM 模式
reprohub collect [--source KEY] [--force] [--days N]
reprohub process [--retry-errors]  预筛与评分
reprohub interpret [--item ID] [--force]
reprohub cluster                   事件聚类与热度
reprohub edition daily|weekly|monthly [--key K] [--push] [--print]
reprohub pipeline [--no-collect ...] [--force]
reprohub push-test CHANNEL         测试推送渠道
reprohub serve [--host] [--port] [--reload] [--scheduler]
reprohub run-forever               只跑调度
reprohub llm status                各步骤实际使用的提供商/模型/强度与来源
reprohub llm test [--all] [--provider P] [--model M] [--effort E]   真实调用一次，报告延迟与 tokens
reprohub llm set --provider P --model M --effort E                   保存运行时覆盖
reprohub llm clear                 清除运行时覆盖
reprohub llm cache [--clear] [--task T]   模型输出缓存统计/清理
reprohub upgrade [--limit N]       用真实模型重评仍是模拟评分的条目
reprohub journals stats|unknown|annotate|import <csv>|resolve <name>   期刊等级与 IF
reprohub rescore                   只用已存的模型分+偏好+来源等级+期刊等级重算最终分（不调模型）
reprohub llm models [--provider P] 列出已知模型与端点返回的模型
reprohub --provider x666 --effort high edition weekly                任意命令前加全局标志 = 本次临时覆盖
reprohub demo-seed                 写入离线演示样例
reprohub sources                   来源列表与状态
reprohub export-reading            导出阅读记录 JSON
```

网站后台 `/admin` 提供同样的操作按钮、来源状态、LLM 调用统计与推送记录。

---

## 目录

```
config/
  site.yaml          站点、版次时间、调度、LLM 提供商表
  models.yaml        每个步骤的提供商/模型/温度
  taxonomy.yaml      22 个主题及关键词（领域门槛）
  journals.yaml      期刊等级（CNS/大子刊/顶刊…）与影响因子注册表
  preferences.yaml   重点偏好规则（只加权）
  selection.yaml     阈值：门槛、评分、入选、解读、聚类、清理
  sources.yaml       数据源
  push.yaml          推送渠道与规则
  prompts/           prescreen / score / interpret / cluster / digest 提示词（Jinja2）
reprohub/
  settings.py  db.py  schema.sql  repo.py  textutil.py
  llm/         base.py  openai_compat.py  mock.py  __init__.py（门面、按步骤选模型、调用日志）
  sources/     pubmed  europepmc  biorxiv  crossref  rss  jsonapi  html_list
  pipeline/    classify  collect  score  interpret  cluster  editions  runner
  push/        channels（10 种渠道）  dispatcher（规则）
  web/         app.py  views.py  api.py  feeds.py  templates/  static/
  scheduler.py  cli.py  demo.py
tests/         pytest，离线（58 个用例）
data/          SQLite、推送 outbox（git 忽略）
```

---

## 配置要点

### 提供商 · 模型 · 思考强度

四个层级，高优先级覆盖低优先级：

| 层级 | 怎么设 | 适合 |
|---|---|---|
| 调用点参数 | 代码里 `llm.run(..., provider=, model=, effort=)` | 程序内特殊场景 |
| 运行时覆盖 | `reprohub --provider x666 --effort high <cmd>`（仅本次）；`reprohub llm set …`（持久，存库）；后台 `/admin#llm` 表单；`POST /api/v1/llm/override` | 临时换模型试效果、紧急降级 |
| 步骤配置 | `config/models.yaml`：`score: { model: deepseek/deepseek-flash, effort: low }`、`interpret: { model: x666/grok-4.7, effort: high }` | 便宜模型做预筛，强模型写解读 |
| 全局默认 | `.env`：`LLM_PROVIDER` / `LLM_MODEL` / `LLM_EFFORT` | 日常 |

- `model` 处处支持 `provider/model` 简写，一次切换两者；模型若与选中的提供商不匹配会被忽略，不会张冠李戴。
- 思考强度统一为 `off / low / medium / high`。每个提供商在 `config/site.yaml → llm.providers.<key>.reasoning` 声明映射：`style: openai` 发 `reasoning_effort`（`off` 时可追加 DeepSeek 的 `thinking: {type: disabled}`）；`style: custom` 按等级写请求体（如通义 `enable_thinking` + `thinking_budget`）；`style: none` 忽略。
- 默认分配：`prescreen` / `cluster` 用 `off`（二元判断，越快越好），`score` 用 `low`，`interpret` / `digest` 用 `medium`。
- 思考型模型的 `max_tokens` 通常同时覆盖推理与正文，解读/总述步骤默认给到 8000 / 6000；输出被截断时会报明确错误而不是解析失败。
- **缓存**：真实模型的输出按「任务 + 提示词内容」哈希缓存在 `llm_cache` 表，同一篇文章再次评分/解读不再花 token；条目页「重新生成」与 `interpret --force` 绕过缓存。模拟输出永不缓存。`reprohub llm cache [--clear]` 查看/清理。
- **模拟 → 真实升级**：曾在无 key 时评过的条目带 `is_mock`，一旦有真实提供商，`process`/`pipeline` 会自动按「已入选 › 偏好高 › 新发表」顺序重评（每轮上限 `selection.yaml → scoring.upgrade_mock_limit`，回溯 `upgrade_mock_days` 天），模拟解读随之作废重做；也可手动 `reprohub upgrade [--limit N]` 或后台「升级模拟评分」。
- **限速**：提供商可配 `rate_limit: { requests: 25, per_seconds: 300 }`（x666.me 网关默认如此），滑动窗口、跨线程共享，超出自动等待而不是撞 429。
- **并发**：评分步骤多线程（`site.yaml → llm.concurrency`，默认 4；环境变量 `REPROHUB_LLM_CONCURRENCY` 覆盖），模拟模式自动为 1。
- 新增提供商只需在 `providers` 下加一段（base_url、api_key_env、默认 model、models 列表、reasoning），再到 `.env` 填 key；`reprohub llm test --provider <key>` 立即验证。
- YAML 1.1 会把裸 `off` / `on` 解析成布尔值，配置里的 `"off"` 请保留引号。

### 加来源

`config/sources.yaml` 一条即可。字段：`type`（pubmed / europepmc / biorxiv / crossref / rss / jsonapi / html_list / push）、`tier`（1 一手 / 2 机构 / 3 媒体）、`kind`、`in_scope_default`（来源本身即领域内）、`scope_filter`（综合性来源入库前先过领域门槛）、`interval_minutes`、`params`。
微信公众号等可经 RSSHub 转为 RSS：设置 `RSSHUB_BASE`，在 `params.url` 中用 `${RSSHUB_BASE}/...`。

### 期刊等级与影响因子

- 注册表 [config/journals.yaml](config/journals.yaml)：`tiers` 定义等级与加分，`journals` 列出期刊（`name`、`aliases`（PubMed ISO 缩写 + 全名）、`issn`、`tier`、`if`），`patterns` 给未列出的 Nature/Cell/Lancet 系刊兜底等级。
- 影响因子是人工整理的近似 JCR 2023 值，页面和 API 都注明来源；要用精确/最新 JCR，把 Clarivate 导出的表整理成 `name,issn,if,tier,year` 的 CSV，`reprohub journals import if.csv` 后 `reprohub journals annotate` 回填并重算最终分。
- `reprohub journals unknown` 列出高频未收录期刊，便于补充。
- 等级只影响排序与入选阈值（CNS +12 … OA 刊 −3），不是收录条件；`selection.yaml → scoring.journal_boost_enabled/cap` 可关闭或封顶。

### 调阈值

`config/selection.yaml`：`scope.min_topic_score`（领域门槛）、`scoring.select_threshold`（入选）、`scoring.preference_weight / preference_cap`（偏好权重）、`interpret.auto_min_score`（自动解读）、`cluster.*`（聚类与热度）。

### 推送

`.env` 填渠道密钥 → `config/push.yaml` 把对应渠道 `enabled: true` → 规则里把渠道加入 `channels`。`reprohub push-test <channel>` 验证。
文件渠道默认写到 `data/outbox/*.md`，把 `dir` 指向 Obsidian 仓库目录即可直接在笔记软件里阅读。

### 访问控制

单用户本地工具。未设 `ADMIN_PASSWORD` 时本机访问自动拥有写权限（阅读状态、后台）；对外部署请设置 `ADMIN_PASSWORD`，网页在 `/login` 登录，API 带 `X-Admin-Token` 头。推送入库接口需 `INGEST_TOKEN`。

---

## API 速览

```
GET  /api/v1/status                         LLM 模式、计数、渠道
GET  /api/v1/items?selected=1&days=7&topic=early-embryo&order=score
GET  /api/v1/items/{id}                     含 interpretation.markdown 与 is_mock
GET  /api/v1/search?q=...                   FTS5 trigram，中英文子串
GET  /api/v1/items?tier=top&order=if        顶刊（cns,cns-sub,general-top,field-top），按 IF 排序
GET  /api/v1/journals                       期刊等级统计与注册表信息
GET  /api/v1/stories  /api/v1/editions  /api/v1/topics  /api/v1/sources
POST /api/v1/reading/{id}                   {"status":"read","starred":true,"tags":[],"notes":"","rating":5}
POST /api/v1/items/{id}/interpret
POST /api/v1/collections  /api/v1/collections/{cid}/items/{id}
POST /api/v1/ingest                         Bearer INGEST_TOKEN，批量推送条目入库
POST /api/v1/admin/run                      {"collect":true,"process":true,...}
GET  /api/v1/llm                            当前提供商/模型/强度的解析结果、各步骤、可用提供商
POST /api/v1/llm/override                   {"provider":"x666","model":"grok-4.7","effort":"high"}
DELETE /api/v1/llm/override
POST /api/v1/llm/test                       {"provider":"deepseek","effort":"off"}  真实试调一次
```

RSS：`/feed.xml` `/feed/top.xml` `/feed/all.xml` `/feed/papers.xml` `/feed/news.xml` `/feed/preference.xml` `/feed/topic/<slug>.xml` `/feed/editions.xml`。

---

## 测试

```bash
pytest
```

全部离线运行（HTTP 被屏蔽、LLM 强制模拟、不读 `.env`），覆盖文本工具、分类门槛与偏好、LLM 回退与真实客户端、提供商/模型/强度解析优先级与映射、来源解析、完整流水线、推送规则、Web 页面与 API。

---

## 设计上的几条原则

1. **领域是门槛，偏好是加权。** 分类法决定收不收，偏好只决定排多前；两者在配置里分开，页面上分开标注。
2. **模拟就是模拟。** 任何没有真实模型参与的输出都带 `is_mock`，并用显眼方式告诉读者。宁可空着中文标题，也不用规则伪造「翻译」。
3. **规则能做的不叫模型。** 去重、分类门槛、日报编纂、热度统计全是规则，模型只做它擅长的：判断质量、写中文、写解读与总述。
4. **一切可配置、可替换。** 来源、主题、偏好、阈值、提示词、模型、推送渠道都在 `config/` 和 `.env`，不改代码。

## 许可

MIT
