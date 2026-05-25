# Dash 重构与改进规划

> 这份文档是 Claude 的工作记忆。基于一次架构勘察对话（2026-05-17），对齐到用户的三条实际诉求：
> 1. 部署后 pipeline 跑得快（多线程、并发、流水线）
> 2. summary 质量更高，前端访问更快
> 3. pipeline 拆得清楚，便于理解和 debug
>
> 不追求"代码质量提升"本身，所有改动以上述三条为唯一裁判。

---

## 用户的非可妥协偏好（来自 HANDOFF.md，等同 ADR）

- 不要在应用代码里塞时区逻辑，调度时间靠外部（GitHub Actions）控制。
- `.env.local` 仅本地，绝不能提交。
- 项目事实上是 DeepSeek-only，不要做通用 provider 抽象。
- 前端轻量，`docs/data/*.json` 不能携带重 fulltext。
- Online workflow（`.github/workflows/daily.yml`）跑独立 stage 脚本，不走 `pipeline.py --from-stage`。
- pipeline 显式分 stage，但 stage 内部可以并行。
- 优先用 fulltext 做 summary，abstract-only 是 fallback。
- Snapshot 同日多次重跑要合并到同一文件；前一日去重靠 arXiv id。

## 当前已知关键事实

- `opendataloader_pdf.convert()` 内部已经是 `java -jar` subprocess。当前 `pdf_fulltext.py` 又额外用 `subprocess.run([sys.executable, "-c", "from opendataloader_pdf import convert; ..."])` 套了一层 Python，所以是 "Python → Python → java" 三层启动。A2 的 win 来自砍掉外层 Python，**JVM 启动每篇仍然不可避免**（除非改 `opendataloader-pdf-hybrid` server，但它要求装额外 extras，先不动）。
- `summarize.py` 每篇论文 `with httpx.Client(...)` 一次，chunk 模式下一篇要多次往返，连接每次都重建。
- `extract_fulltext.py` / `summarize.py` 都是每篇 paper 完成就 `persist_progress` 全 snapshot 写一次（pretty JSON）。
- 没有任何单元测试。
- 现行 `daily.yml` 五个 stage 严格串行：fetch → extract → summarize → build → validate。
- 默认 `PDF_EXTRACT_MAX_WORKERS=2`，`SUMMARY_MAX_WORKERS=2`。

---

## 改进清单（按性价比排）

### A 组 — 速度（先做，本次执行目标）

| ID | 改动 | 预期收益 | 涉及文件 |
|---|---|---|---|
| **A1** | 把 `extract_fulltext` + `summarize` 合并成一个 `enrich` stage，per-paper 流水线（一篇 fulltext 好就喂给 summary，不等批） | 总耗时 ≈ max(extract_total, summary_total)，而非 sum；100 篇可省约 30% wall-clock | 新增编排模块 + `daily.yml` |
| **A2** | `pdf_fulltext.run_opendataloader_extract` 直接 `from opendataloader_pdf import convert` 在主进程里调，免一层 Python subprocess | 每篇省 ~300–800ms 启动开销 | `scripts/pdf_fulltext.py` |
| **A3** | 整个 summarize stage 用一个全局 `httpx.Client`（HTTP/2 + keep-alive 连接池），通过 worker 共享 | chunk 模式下尤其明显，每次省一次 TLS 握手 | `scripts/summarize.py` |
| **A4** | 把默认 worker 数提一档，并区分"网络 I/O bound"（summary）和"subprocess CPU bound"（extract） | summary 默认 4，extract 默认按 CPU 数（GH runner 通常 2-4） | env 默认值，README 文档化 |
| **A5** | snapshot 持久化去抖：每 N 篇或每 T 秒才整文件落盘，最后强制 flush；写入加锁 | 100 篇省 ~95% 写入次数；A1 流水线下两个 pool 抢同一文件必须加锁 | `scripts/extract_fulltext.py`, `scripts/summarize.py`, 新增 `snapshot_writer` 工具 |

#### A 组实施约束

- A1 的"流水线 stage"必须保留对 HANDOFF "online 跑独立脚本" 偏好的兼容：要么 `enrich.py` 是个新 stage 脚本，把 daily.yml 里的两个 step 合一；要么内部仍然分两 stage，外面用一个 driver 串起来。倾向前者。
- A2 必须保留 hard timeout 能力（当前是 `subprocess.run(timeout=)`）。进程内 `convert` 没有 timeout 参数；要用 `concurrent.futures` + 单独 worker thread + 超时 join 的方案，但 thread 杀不掉 subprocess。最稳妥：仍用 subprocess 但只调 `java -jar` 一层，跳过 Python wrapper（它只是参数包装器）。**确认改造路径：复用 `opendataloader_pdf.runner.run_jar` 但加 timeout**，或自己 fork 一份 `runner.run_jar` 加 `subprocess.run(..., timeout=...)`。
- A3 `trust_env=False` 必须保留（绕本地 proxy）。
- A5 的去抖窗口 N=5 / T=10s 是初值，后续按观察调。崩溃时丢失最近未持久化的 paper 是可接受的——PDF 缓存和 LLM 响应都还在原位（A2 / 未来的 C1），重跑成本是装回 snapshot 而不是重新付 token。

### B 组 — 质量（A 组之后）

| ID | 改动 | 收益 |
|---|---|---|
| **B1** | chunk prompt 与 final prompt 解耦：chunk 只产事实条目（kind + content），不强填 5 节；reduce 阶段才合成最终 5 节 | 长 paper 不再被"每 chunk 凑齐 motivation"摊平 |
| **B2** | `pdf_fulltext.detect_section_kind` 删除 substring fallback（"Background-aware Method" 不应被识别为 method） | 喂给 LLM 的结构化摘要更准 |
| **B3** | summary 失败时自动降级重试一次（abstract-only + 短 max_tokens），不是直接进 fallback | fallback 数量下降 |
| **B4** | `docs/data/*.json` 强制 minified，把冗余字段（重复的 summary_zh / 不必要的 categories）剥光 | 单日 payload 体积 ↓30–50%，前端首屏更快 |

### C 组 — 拆分 & 调试（穿插进行）

| ID | 改动 | 收益 |
|---|---|---|
| **C1** | LLM 响应缓存：key=hash(model+messages+max_tokens)，落 `tmp/llm_cache/`，B 组 prompt 实验依赖此 | 改 prompt 不烧 token；调试秒级 |
| **C2** | 每篇 paper 一个 `tmp/trace/<date>/<paper_id>.json` 落地结构化 trace（取代 stdout dict prints） | 出问题不用翻屏 |
| **C3** | extract / summarize 加 `--paper-id` 单篇模式 | 单篇定位调试 |
| **C4** | 每个 stage 提供 `run(args)` 函数，main 只 argparse；A1 流水线编排器可直接 import 调用 | notebook / 在线编排可复用 |

---

## 推荐执行顺序

1. **A 组（A1+A2+A3+A4+A5）一起做**：都触在 pipeline 编排层，分批改容易冲突。本次目标。
2. **C1 + C2**：B 组要反复跑 prompt 实验，没有缓存会烧钱、慢调试。
3. **B 组**：在缓存 + trace 的支持下做 prompt 调优。
4. **B4**：5 分钟搞掉，跟谁都不冲突，可以随时插。
5. **C3 + C4**：边做边补。

---

## 不在范围内（明确放弃）

- 抽象的 `Snapshot` 模块、`SummaryClient`/`SummaryPolicy` ports & adapters：单元测试受益巨大但与用户三条诉求关系弱。
- `FulltextExcerpt` 纯函数模块：B2 收紧 heuristic 时如果发现复杂可以考虑顺手切，但不主动重构。
- 前端 `UiPreferences` 模块化：personal-use，最小化即可。
