# 部署指南

把 Dash 部署到 GitHub Pages 的完整步骤。**首次部署照单顺序走**；后续维护看「日常运维」。

## 一次性准备

### 1. GitHub 仓库设置

- 仓库可见性：**Public**（runner 4 vCPU / 16 GB / 公共 Pages 免费）
- 默认分支：`main`

启用 Pages：

1. **Settings → Pages → Build and deployment**
2. Source 选 **GitHub Actions**（不是 Deploy from a branch）

启用 Actions 写权限：

1. **Settings → Actions → General → Workflow permissions**
2. 勾 **Read and write permissions**（workflow 要 push 到 `data` 分支）

### 2. 配置 secrets 与 vars

**Settings → Secrets and variables → Actions**：

| 类型 | 名称 | 值 | 备注 |
|---|---|---|---|
| Secret | `OPENAI_API_KEY` | DeepSeek API key | 命名沿用 OpenAI-compatible，不是真用 OpenAI |
| Variable | `OPENAI_BASE_URL` | `https://api.deepseek.com` | |
| Variable | `MODEL_NAME` | `deepseek-v4-flash` | 或你想用的其他 DeepSeek 型号 |
| Variable | `LANGUAGE` | `zh-CN` | 摘要输出语言 |
| Variable | `LLM_ENABLED` | `true` | 设 `false` 则跳过 LLM，全部走 fallback |

**可选 Variables**（不写就用 `config.yaml` / 脚本默认值）：

| 名称 | 默认 | 用途 |
|---|---|---|
| `CATEGORIES` | 用 `config.yaml` 的 `arxiv.categories` | 逗号/空格/分号分隔，覆盖运行时的 category 列表 |
| `LLM_TIMEOUT_SECONDS` | `600` | 单次 DeepSeek 请求超时 |
| `LLM_RETRY_TIMES` | `3` | DeepSeek 失败重试次数 |

### 3. 校准 base_url

`config.yaml` 里 `site.base_url` 当前是 `/Dash/`，对应 `https://<user>.github.io/Dash/`。如果 fork 后改了仓库名或者用 user/org page，更新这个字段。

注意：前端 `app.js` 用的是相对路径 `./data/...`，所以 base_url 当前**只用于 metadata**，不影响数据加载。

## 首次部署

> 鸡生蛋问题：`frontend-deploy.yml` 在 push main 时触发 Pages 部署，但首次没有 `data` 分支，前端会渲染空。所以反过来：**先手动跑一次 daily.yml 把数据建好**，再让前端部署生效。

### 步骤

1. **Push 主分支代码到 `main`**（不要等 frontend-deploy 触发）

2. **手动触发 daily workflow**

   - **Actions → Daily arXiv Digest → Run workflow**
   - 可以先用小样本测：
     - `limit` 填 `5`（只处理 5 篇，省 API token）
     - 其它保持默认
   - 跑一次完整流程；预期：
     - `Stage 1` 抓 arXiv 列表
     - `Stage 2` 下载 + 解析 + LLM
     - `Stage 5` 创建 `data` 分支并 push 一个 commit
     - 触发 `Deploy Pages`，Pages 上线

3. **验证**

   - `https://<user>.github.io/Dash/` 能打开，前端能拉到 `./data/index.json`
   - `data` 分支存在，`docs/data/index.json` 在分支上

4. **正式跑一次**：再次 Run workflow，`limit` 留空（0），处理全部论文。这会成为正常的每日基线。

### 首次没成功时怎么排查

- `Stage 1` 失败：检查 `CATEGORIES` 是否拼错、网络是否能到 arxiv.org
- `Stage 2` 失败：看 `OPENAI_API_KEY` 是否设了、`OPENAI_BASE_URL` 是否对、`LLM_ENABLED=true` 是否注入
- `Stage 5` 报「No data changes」：上一次跑过、缓存命中、数据未变；正常情况
- Pages 部署 404：`Settings → Pages` 里 Source 必须是 **GitHub Actions**

## 日常运维

### 自动调度

`daily.yml` 已配 `cron: "20 23 * * *"`（UTC 23:20，等于北京时间 07:20）。**调度时区只通过 cron 控制**，不要去 Python 里加时区逻辑。改时间直接改 cron 表达式。

### 手动重跑

- **某天的数据：** Actions → Run workflow → `date` 填 `YYYY-MM-DD`
- **跳过 LLM 调试：** 勾 `skip_summarize`
- **强制重新解析 PDF：** 勾 `refresh_extract`（默认会用 PDF cache）
- **重做摘要：** 勾 `refresh_ok`
- **只 build 不 deploy：** 勾 `skip_deploy`

### worker 数量调优

默认值在 `daily.yml` 里显式声明，按 ubuntu-latest 4 vCPU / 16 GB 调过：

| 阶段 | env | 默认 | 调整建议 |
|---|---|---|---|
| arXiv list 页抓取 | `ARXIV_LIST_WORKERS` | 5 | category 多就上调，arXiv 列表页是普通 HTML 没强限流 |
| arXiv API chunk | `ARXIV_API_WORKERS` | 4 | 一般不动 |
| PDF 下载 | `PDF_DOWNLOAD_MAX_WORKERS` | 8 | 网络 IO 受限，可加到 12 但收益递减 |
| PDF 解析 | `PDF_EXTRACT_MAX_WORKERS` | 2 | **不要拉高**：每个 JVM ~0.5–1 GB；2 留给 4 vCPU 一半算力够稳。Self-hosted runner 内存大才考虑 3–4 |
| DeepSeek 摘要 | `SUMMARY_MAX_WORKERS` | 4 | 触发 429 就降到 2，DeepSeek 没公开严格 rate limit |

如果某天看到 `summary_fallback` 比例升高，先看日志里的具体 error name（`HTTPStatusError` / `TimeoutException`），再决定是降并发还是涨 `LLM_TIMEOUT_SECONDS`。

### 缓存

PDF 下载/解析结果用 `actions/cache` 按月分桶（key 是 `pdf-YYYY-MM-DD`，restore-keys 回退到 `pdf-YYYY-MM-` 和 `pdf-`）。同月内多次重跑近乎免费；跨月第一次会 cold 一些。

### 数据保留

`config.yaml` 的 `output.keep_days: 90` 控制本地 state JSON 的保留天数。`data` 分支累积所有历史，要修剪从 `data` 分支手动 `git rm`。

### 回滚

- **代码回滚：** `git revert` main 上的提交，frontend-deploy 会自动重新部署
- **数据回滚：** `data` 分支上 `git revert` 对应那次 `data: arxiv digest YYYY-MM-DD` 提交，pages.yml 在下次触发时取新的 data
- **彻底重建：** 删 `data` 分支 + 清 Actions cache，再触发一次 daily workflow

## 增量更新与稳定性保证

> 你以后会在本地改代码、改 prompt、改 config，然后 push main。这一节说明这些改动**为什么不会破坏已经生成的 paper**，以及当真的有 bug 时哪些地方会兜住。

### 数据隔离模型

```
main 分支     →  代码、prompt、frontend、workflow（你 push 这里）
data 分支     →  历史 paper JSON 归档（只由 daily.yml 自动写）
docs/data/   →  在 main 上 gitignore，本地跑 pipeline 的产物不会污染 main
tmp/state/   →  本地 pipeline 工作区，gitignore，跟线上无关
```

push main 不会触发数据重建。**只有 daily.yml 会写 data 分支**，且每次都会先把 data 分支 seed 回工作区，所以历史数据不会丢失。

### enrich.py 的幂等性

`enrich.py` 默认对每篇 paper 检查两个状态：

- `summary_status == "ok"` → 跳过 LLM 调用（除非 `refresh_ok=true`）
- `fulltext_status == "ok"` → 跳过 PDF 重新解析（除非 `refresh_extract=true`）

所以同一天重跑 daily.yml 不会重新烧 token；改了脚本后再跑，已经 ok 的 paper 也不会被重做。

### 三层兜底

1. **`validate_data.py`（Stage 4）：** 任何输出 JSON 为空 / paper_count ≤ 0 都直接 fail，commit 步骤被 `if: success()` 拦下来。
2. **`check_regression.py`（Stage 4b）：** 把新 build 出来的 `docs/data/*.json` 与 `data` 分支上的旧版逐天比对 paper_count，**任何历史日期变少或缺失** 都 fail。这里是 schema 漂移和误删的最后一道防线。
3. **`if: success()`（Stage 5）：** 上面任何一步失败都不 commit 到 data 分支，旧数据原封不动留在线上。

### 改动 schema 时怎么做

如果你打算给 paper 加新字段（比如 `relevance_score`），记住：

- 老的 `2026-05-XX.json` 不会有这个字段。前端 `app.js` **必须用 `paper.relevance_score ?? null` 这种安全访问**，不能假设字段存在。
- 如果新字段是"重字段"（不希望进 public payload），加到 `build_site_data.py:HEAVY_PAPER_FIELDS`。
- 想给历史日期补字段：在 main 上跑 `python scripts/enrich.py --date YYYY-MM-DD --refresh-ok` 本地刷，然后提示 daily.yml 重建，但**通常不值得** —— 让旧数据保留旧字段集即可。

### 改动 prompt 时怎么做

prompt 在 `src/prompts/*.txt`。改完 push main 不会自动重做摘要，因为已经 ok 的 paper 会被跳过。

要重做：Run workflow → 勾 `refresh_ok=true` →（可选）填具体 `date`。这会**烧 token**，谨慎使用。

### 想"重做今天"

正确：在 Actions 里 Run workflow，`date` 留空或填今天，`refresh_ok=true`。
危险（不要做）：在本地 `git push --force` data 分支、手动删 docs/data/。

### 不会破坏数据的常见操作

| 操作 | 影响 |
|---|---|
| 改 frontend (`docs/app.js` / `style.css` / `index.html`) | 只触发 frontend-deploy.yml，不动 data 分支 |
| 改 prompt | push 后下次 daily.yml 才会用新 prompt，且只用于新 paper |
| 改 worker 数量 / `daily.yml` env | 下次 daily.yml 生效，旧数据不动 |
| 本地 `pipeline.py --date 2026-05-XX` | 只写本地 `tmp/state/`，gitignore，不会上传 |
| 改 `config.yaml` 里的 categories | 下次 daily.yml 抓的 category 变了；旧日期的 paper 不会重新分类 |

### 真要触发数据破坏的场景（避免）

| 危险操作 | 后果 | 替代 |
|---|---|---|
| 手动 `git push origin :data` 删 data 分支 | 历史归档丢失 | 永远不要做；要"清空"用 Run workflow 重建 |
| 在 data 分支手动 commit | 跟自动 commit 冲突，下次 daily.yml 可能 push 失败 | 不要直接动 data 分支 |
| 改 `tmp/state/YYYY-MM-DD.json` schema 同时不改前端 | 前端字段 missing | schema 改动配前端 `?? defaultValue` |
| 用 `--refresh-extract` 全量刷 | 烧时间，不烧 token | 谨慎使用，通常没必要 |

### 杂项注意

- **DeepSeek API 费用是唯一变量成本。** 100 篇 chunk_reduce 模式平均 ~10–15K tokens / 篇，按 `deepseek-v4-flash` 价格估算每天每天 < $1（自己核对）。`LLM_ENABLED=false` 是紧急关阀。
- **Java 在 ubuntu-latest 自带 OpenJDK 21。** 不需要额外 setup。
- **arXiv 列表抓取并发了 5 个 category。** 如果哪天看到 503/429，把 `ARXIV_LIST_WORKERS` 降到 1 临时回退到顺序。
- **不要把 `.env.local` commit 到 main。** secrets 走 GitHub Actions secrets，本地走 `.env.local`，两条路完全分开。
- **`docs/data/` 在 main 分支被 gitignore。** 数据只活在 `data` 分支，pages.yml 把两边合并到 `_site` 后部署。
- **前端字体引用 SJTU 镜像。** `docs/index.html` 里 Google Fonts 走 `google-fonts.mirrors.sjtug.sjtu.edu.cn`。国外用户访问可能慢；如需更稳，换回 `fonts.googleapis.com` 或自托管 woff2。字体加载失败有 system fallback，不会渲染崩。

## 首次部署 checklist

提交 main 分支前请确认：

- [ ] `OPENAI_API_KEY` 已设为 secret，**没有**写进任何 commit 文件
- [ ] `OPENAI_BASE_URL` / `MODEL_NAME` / `LANGUAGE` / `LLM_ENABLED` 已设为 vars
- [ ] Settings → Pages → Source 选 **GitHub Actions**
- [ ] Settings → Actions → Workflow permissions 是 **Read and write**
- [ ] `config.yaml` 的 `arxiv.categories` 是你想抓的列表（或者用 `CATEGORIES` var 覆盖）
- [ ] `config.yaml` 的 `site.base_url` 与仓库名一致（默认 `/Dash/`）
- [ ] `.gitignore` 包含 `.env.local`（已包含）和 `docs/data/`（已包含）

提交后：

- [ ] 手动触发 daily.yml，先用 `limit=5` 跑一次小样本
- [ ] 确认 Actions 跑通、`data` 分支被创建、Pages 站点能打开
- [ ] 再 Run workflow 一次（`limit` 留空）跑全量
