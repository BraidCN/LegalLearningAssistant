# LawAgent · 法学学习助手（本地版 v1.7.1）

本地法条库（1,778 部、82,651 条，含行政法规/司法解释/已废止文本）＋ 检索 ＋ 规则路由 ＋ 联网补充 ＋ 引用门禁 ＋ 网页界面。
法律依据只依据库内条文；命中不足或热点问题会自动联网检索作为线索（标注“未核验”）。
每条引用都要过“条号存在 + 引文逐字一致”的机器核对。

**在线演示**：[https://law.braidcn.com](https://law.braidcn.com) （联网版，仅供体验；本仓库只包含单机版，不含联网版的排队/隧道/多用户服务）

> 法条数据（`articles.db`、向量索引）体积超过 GitHub 单文件限制，**不在仓库中**，第一次运行前请按 [data/README.md](data/README.md) 构建或下载。

## 快速开始

前提：Windows，已装 Python 3.11+，已装 Ollama（本地生成模型，可选）。

```bat
:: 1) 初始化环境（只做一次）
python setup_env.py

:: 2) 构建数据（只做一次；约几分钟）
构建数据.cmd

:: 3) 启动
启动.cmd
```

浏览器会自动打开 http://127.0.0.1:8787 。若提示端口被占用，说明已有实例在跑，先关掉旧的控制台窗口。

命令行自测（不开网页）：

```bat
.venv\Scripts\python.exe cli.py search "拖欠工资"
.venv\Scripts\python.exe cli.py ask "公司拖欠工资怎么办"
```

单元测试与数据体检：

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe tools\stats.py
```

## 目录

```
law-agent/
├─ configs/               # config.json（模型、参数）、synonyms.json（同义词）
├─ data/                  # raw 快照 / interim 中间产物 / db 索引库（不入 git）
├─ build/                 # fetch.py → parse_articles.py → build_db.py
├─ src/lawagent/          # 检索包（搜索、门禁、流水线、路由、联网、LLM 调用）
├─ serve/                 # 本地服务 + 单页界面
├─ tests/                 # 门禁与中文数字单元测试
├─ tools/stats.py         # 数据体检
├─ cli.py                 # 命令行入口
├─ setup_env.py           # 建虚拟环境
├─ 构建数据.cmd / 启动.cmd
└─ README.md
```

## 当前实现（v0.1）

- 数据：just-laws（MIT，304 部现行法律）＋ **lawtext**（FLK 转制，含行政法规 783、司法解释 521、已废止 163、已修改 350）；合计 1,778 部 / 82,651 条
- 更新：双击 `更新法条库.cmd`（下载 → 按文件哈希比对 → 只重导变化 → 有变更时重建向量）
- 检索：FTS5 + jieba 分词 + 法名识别限定 + 同义词扩展 + LIKE 兜底（向量检索尚未接入）
- 路由与联网：纯规则判断意图（热点/规范/概念/案例/闲聊）与检索置信度，自动选择本地/云端后端并决定是否联网；命中不足或热点问题走 Bing 联网检索（官方域名优先，正文抓取失败时回落到摘要）；`routing.force` 可强制 `local` / `cloud` / `web`
- 生成：Ollama 本地（默认 `qwen3.5:9b`）或云端 DeepSeek（OpenAI 兼容）；按路由选择后端，失败按 local → cloud 回退；实测 30~80 秒/问（9B 模型，首次加载更慢）
- 门禁：条号存在性（库内比对、简称归一）+ 引文 10 字窗口逐字匹配 + 失败自动重写一次；热点问题允许只回答网络信息，但回答里出现的任何条号仍要校验存在性与逐字
- 界面：单页网页（提问、路由徽标、门禁徽标、引用核验卡片、网络来源卡片、命中条文列表）
- 测试：73 个单元测试全过（门禁、中文数字、路由、联网解析）

## 已知限制

1. 效力状态目前统一标“现行有效”（just-laws 只有现行法；status/沿革字段待接入 lawtext）
2. 没有向量检索与重排——口语命中靠分词＋同义词，跨法域语义检索会漏
3. 联网检索依赖 Bing 的 RSS/HTML，可能限流或无结果；网页抓取失败时只用摘要，网络信息未经核验，只能作线索
4. 9B 本地模型速度一般；可换更小模型或使用云端 DeepSeek（见下方配置）
5. 法典分卷合并、修正案识别已处理，但个别“决定”类文件未入库（见上）

## 调参位置

- 换本地模型：`llm.model`（先 `ollama list` 看有哪些）
- 云端配置：`llm.cloud`（`base_url` / `model` / `timeout`）；key 来自环境变量 `DEEPSEEK_API_KEY`，或 `llm.cloud.api_key_file` 指向的 Hermes `.env`（默认 `%LOCALAPPDATA%/hermes/.env`）；用 `llm.cloud_enabled` 开关云端
- 路由：`routing.min_score`（检索置信阈值）、`routing.long_query_chars`、`routing.force`（`local` / `cloud` / `web`，留空自动）
- 联网：`web.enabled`、`web.top_k`、`web.fetch_pages`、`web.max_page_chars`、`web.timeout`
- 检索条数：`retrieval.top_k`；喂给模型的条数：`retrieval.max_context`
- 门禁严格度：`gate.min_quote_len`（默认 10 字）
- 同义词：`configs/synonyms.json`

## 下一步（TODO）

1. 向量检索（bge-m3）+ RRF 融合 + 重排
2. 《技术路线》里的 status/沿革字段接入（lawtext 扩展包）
3. MCP 接口（给 Claude Code / AnythingLLM 调用）
4. 五个学习技能（SKILL.md）
5. 评测集与跑分脚本

## 学习技能与向量检索（v0.3）

- 向量：`bge-m3` 全量索引 82,651 条 × 1024 维；`search()` 做 FTS＋向量**加权 RRF**（向量 1.0／FTS 0.7），并按 (法名, 条号) 去重、优先现行有效。
- 学习技能（HTTP）：`/api/explain`（概念三段式）、`/api/case`（鉴定式案例）、`/api/quiz`（出题）、`/api/grade`（批改）、`/api/wrongbook`（错题本）。
- 学习技能默认走云端（引文格式要求高，本地 9B 易改写引文）；要改回本地：`configs/config.json` → `learn.prefer_cloud = false`。
- 打包骨架：`packaging/portable`（免安装目录）、`packaging/docker`（compose）、`tools/build_portable.py`（构建分发包）、`docs/PACKAGING.md`。
- 测试：136 个单元测试全绿。

## 资料归档 Agent（v0.4）

拖文件进网页「归档」标签 → 写一句提示词 → Agent 自己决定归档去向并执行，最后给一张可追溯的回执。

- 接口：`POST /api/upload`（multipart，字段 file）→ `POST /api/agent/ingest`（`doc_id` + `prompt` + 可选 `target`）→ `POST /api/agent/confirm`（回执为 `need_confirm` 时点选用）。
- 去向：`note` 课程笔记 / `paper` 法学论文（文献页 + 题录 + 引文核验表）/ `doc` 资料索引 / `case` 案例 / `law` 法条核对（**只出报告，绝不改法条库**）。
- 决策链：用户手选 > 归档记忆（`rules` 表，用户确认过一次就记住）> 模型（云端 Flash 优先、本地 9B 兜底）> 关键词规则；置信度低于 `ingest.confirm_below`（默认 0.6）且提示词无明确意图 → 返回 `need_confirm`。
- 索引：所有文件先进资料库 `data/db/docs.db`（docs / chunks / chunks_fts，jieba 预分词），可 `POST /api/docs/search` 或网页「资料库检索」查；索引只作线索，不作证据。
- 引用核验：文件里每个《法名》第X条 都对照 `articles.db`（存在性 + 逐字 + 效力状态），结果写进回执与文献页。
- 支持格式：md / txt / html / docx（纯标准库）；PDF 需 `pip install pypdf`（未装时给出提示并保留原件）；CAJ / 图片暂不支持。
- 原件归档默认 `data/docs/<日期>/`（sha256 去重、同名不覆盖）；笔记与文献写进 wiki 收件箱 `data/wiki_raw/law/<课程>/inbox/`，待 `一键整理.cmd` 编译成 wiki 页。
- **存储位置可改**：网页「归档」页顶部「归档存储位置」三行（归档原件 / 用户资料 / wiki 收件箱）各有一个「选择…」按钮，点开是本机文件夹选择框；保存前会建目录并写测试文件验证可写，失败会明确报错。留空 = 用默认值。设置存 `data/settings.json`（本机，不随包分发），接口为 `GET/POST /api/storage`、`POST /api/pick-folder`。

## 联网与路由（v0.2 实测）

- Bing 在无会话 Cookie 时会返回与查询无关的缓存页（实测：研招网／网易云音乐／美剧），因此 `websearch.py` 先访问 `cn.bing.com` 预热 Cookie；即便预热，长问句仍可能跑偏。
- 兜底：Bing 结果经“二字片段相关性过滤”为空时，自动切 DuckDuckGo（实测稳定）；仍不相关则标注“联网结果与问题不相关”并拒答。
- 热点问题走云端 DeepSeek（`deepseek-flash`）；法条题走本地 `qwen3.5:9b`，不联网。
- 路由可强制：`configs/config.json` → `routing.force` = `local` / `cloud` / `web`。

## 注意

- 本工具仅用于学习检索，不构成法律意见。
- just-laws 数据为 MIT 许可；lawtext 等无许可数据未随包分发。
- 数据快照为 2026-10 的 just-laws 版本，效力状态以国家法律法规数据库为准。

## 许可

- 本项目代码采用 **MIT** 许可，全文见 [LICENSE](LICENSE)。
- 法条数据与第三方项目的来源、许可与署名见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 引用核验的思路上参考了 [legal-research-skill](https://github.com/Golden2002/legal-research-skill)（MIT），代码为独立实现。
