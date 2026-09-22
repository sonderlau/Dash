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
| Secret | `OPENAI_API_KEY` | 小米按量付费 API key（`sk-`） | 命名沿用 OpenAI-compatible，不是真用 OpenAI。Batch 不扣 Token Plan |
| Variable | `OPENAI_BASE_URL` | `https://api.xiaomimimo.com/v1` | 结构化输出文档里的对话地址 |
| Variable | `MODEL_NAME` | `mimo-v2.6-flash` | 必须小写 |
| Variable | `LANGUAGE` | `zh-CN` | 摘要输出语言 |
| Variable | `LLM_ENABLED` | `true` | 设 `false` 则跳过 LLM，全部走 fallback |

**可选 Variables**（不写就用 `config.yaml` / 脚本默认值）：

| 名称 | 默认 | 用途 |
|---|---|---|
| `CATEGORIES` | 用 `config.yaml` 的 `arxiv.categories` | 逗号/空格/分号分隔，覆盖运行时的 category 列表 |
| `LLM_TIMEOUT_SECONDS` | `600` | 上传 batch 文件和查询状态的 HTTP 超时 |
| `LLM_RETRY_TIMES` | `3` | 保留的配置项。Batch 任务内部不能重试，失败行直接记 fallback |

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
     - `Stage 2` 用流式接口给当天论文写摘要
     - `Stage 2b` 把 `state/` 推到 `data` 分支
     - 摘要写完后，同一轮构建公开 JSON 并部署 Pages

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

### 调度状态

当前 `daily.yml` 同时启用 `workflow_dispatch` 手动触发和自动 `schedule`：

```yaml
schedule:
  - cron: "10 10 * * *"
```

这对应 UTC 10:10。摘要和页面更新都在这一轮完成。arXiv 通常在美东 20:00 开始公告，夏令时约 UTC 00:00，冬令时约 UTC 01:00；10:10 仍给 list 页和 metadata 同步留出充足缓冲。**调度时区只通过 cron 控制**，不要去 Python 里加时区逻辑。改时间直接改 cron 表达式。

### 手动重跑

- **某天的数据：** Actions → Run workflow → `date` 填 `YYYY-MM-DD`
- **跳过 LLM 调试：** 勾 `skip_summarize`
- **重做摘要：** 勾 `refresh_ok`
- **只 build 不 deploy：** 勾 `skip_deploy`

### worker 数量调优

默认值在 `daily.yml` 里显式声明，按 ubuntu-latest 4 vCPU / 16 GB 调过：

| 阶段 | env | 默认 | 调整建议 |
|---|---|---|---|
| arXiv list 页抓取 | `ARXIV_LIST_WORKERS` | 5 | 主数据源；如果这里失败，workflow 必须失败，因为无法确认今日更新 |
| arXiv API chunk | `ARXIV_API_WORKERS` | 1 | 只补 abstract、DOI、comment 等字段；共享 CI IP 容易被限流，失败后会降级继续 |
| arXiv API chunk 间隔 | `ARXIV_API_REQUEST_DELAY_SECONDS` | 10 | 只影响可选补充；不要低于 arXiv 建议的 3 秒 |
| MiMo 摘要 | 无 worker 数 | 一天一个 batch | 失败行记 fallback，不在同一个任务里重试 |

如果某天看到 `summary_fallback` 比例升高，先看 collect 日志里的 `batch_error` / `JsonOutputError` / `batch_missing`。Batch 不会自动重跑失败行。

如果某天看到 `fetch_status.api_backfill_status = "degraded"`，说明 list 页已经成功确认今日 paper，但 export API 补充 metadata 被 429/503/timeout 限制了。当日 snapshot 仍会产出，只是 `abstract_en`、DOI、journal ref、comment 等补充字段可能为空。

如果 `run_daily.py` 打印 `run_status = "no_new_papers"`，说明 list 页可用，但本次抓到的 paper 全部已经存在于最近一次 snapshot，常见原因是跑得太早、arXiv 当天公告列表还没翻新，或者节假日/周末无公告。workflow 会成功结束并跳过摘要、validate 当前空日期、commit 和 deploy；旧数据继续在线。

### relevance score

`keywords.yaml` 控制个人阅读优先级评分。`keywords: []` 时不计算 `relevance_score`；填入关键词后，后续新摘要会输出 0–100 的字符串分数。要给已生成的 paper 补分，需要手动重跑并勾 `refresh_ok=true`。

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

`enrich.py` 默认对每篇 paper 检查摘要状态：

- `summary_status == "ok"` → 跳过 LLM 调用（除非 `refresh_ok=true`）
- 如果 `keywords.yaml` 非空且旧摘要没有 `summary_sections.relevance_score`，则不会跳过，会补一次 relevance score

所以同一天重跑 daily.yml 不会重新烧 token；改了脚本后再跑，已经 ok 的 paper 也不会被重做。

### 三层兜底

1. **`run_daily.py`（Stage 1）：** list 页抓取失败直接 fail；如果 list 页成功但去重后没有新 paper，输出 `run_status = "no_new_papers"`，workflow 跳过摘要和发布。
2. **`validate_data.py`（Stage 4）：** 正常发布路径里，任何输出 JSON 为空 / paper_count ≤ 0 都直接 fail，commit 步骤被 `if: success()` 拦下来。
3. **`check_regression.py`（Stage 4b）：** 把新 build 出来的 `docs/data/*.json` 与 `data` 分支上的旧版逐天比对 paper_count，**任何历史日期变少或缺失** 都 fail。这里是 schema 漂移和误删的最后一道防线。
4. **`if: success()`（Stage 5）：** 上面任何一步失败都不 commit 到 data 分支，旧数据原封不动留在线上。

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
| 用 `--refresh-ok` 全量刷 | 重新烧 token | 谨慎使用，通常只给新 prompt / relevance keywords 补数据 |

### 杂项注意

- **MiMo Batch API 费用是唯一变量成本。** 当前每篇只用 metadata / abstract，打进当天的一个 JSONL；`LLM_ENABLED=false` 是紧急关阀。Batch 按成功请求计费，价格是实时接口的一半。需要按量付费余额，不能用 Token Plan。
- **arXiv 列表抓取并发了 5 个 category。** 如果哪天看到 503/429，把 `ARXIV_LIST_WORKERS` 降到 1 临时回退到顺序。
- **不要把 `.env.local` commit 到 main。** secrets 走 GitHub Actions secrets，本地走 `.env.local`，两条路完全分开。
- **`docs/data/` 在 main 分支被 gitignore。** 数据只活在 `data` 分支，pages.yml 把两边合并到 `_site` 后部署。
- **前端字体引用 SJTU 镜像。** `docs/index.html` 里 Google Fonts 走 `google-fonts.mirrors.sjtug.sjtu.edu.cn`。国外用户访问可能慢；如需更稳，换回 `fonts.googleapis.com` 或自托管 woff2。字体加载失败有 system fallback，不会渲染崩。

## MiMo 结构化输出

摘要走小米对话接口的流式输出，模型 `mimo-v2.6-flash`。`response_format` 是 `json_object`。客户端把 `delta.content` 拼完再 `json.loads`，并检查六个字符串字段。

- 请求体用 `max_completion_tokens`（默认 1800）、`thinking.type=disabled`、`response_format.type=json_object`。思考模式默认开着，不关掉会忽略 `temperature`，并把思考 token 算进输出。
- 每个日期同时只挂一个 batch。`custom_id` 是 arXiv id。Batch 不能在任务里重试；坏 JSON、缺行和单行错误记 `fallback:`，不另外再提交一轮。
- 创建参数 `completion_window` 用文档示例里的 `24h`。
- 用量日志同时认 `prompt_cache_hit_tokens` 和 `prompt_tokens_details.cached_tokens`。

## 首次部署 checklist

提交 main 分支前请确认：

- [ ] `OPENAI_API_KEY` 已设为 secret，**没有**写进任何 commit 文件
- [ ] `OPENAI_BASE_URL` 是 batch 地址，`MODEL_NAME=mimo-v2.6-flash`，`LANGUAGE` / `LLM_ENABLED` 已设为 vars
- [ ] Settings → Pages → Source 选 **GitHub Actions**
- [ ] Settings → Actions → Workflow permissions 是 **Read and write**
- [ ] `config.yaml` 的 `arxiv.categories` 是你想抓的列表（或者用 `CATEGORIES` var 覆盖）
- [ ] `config.yaml` 的 `site.base_url` 与仓库名一致（默认 `/Dash/`）
- [ ] `.gitignore` 包含 `.env.local`（已包含）和 `docs/data/`（已包含）

提交后：

- [ ] 手动触发 daily.yml，先用 `limit=5` 跑一次小样本
- [ ] 确认 Actions 跑通、`data` 分支被创建、Pages 站点能打开
- [ ] 再 Run workflow 一次（`limit` 留空）跑全量
