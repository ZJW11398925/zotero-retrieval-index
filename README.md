# 检索索引（Retrieval Index）— Zotero 插件

给本地 Zotero 文献库建立**带页码的段落索引**，并通过 Zotero 自己的本地 HTTP 服务提供检索接口。目的是让外部程序（尤其是 AI agent）**按需取用相关段落**，而不是把整篇论文塞进上下文。

> 实测：454 篇 / 22,678 段的语料上，取 top-5 段落约 **2,500 token**；直接读 10 篇论文约 **114,600 token**。**约 45–50 倍**的上下文节省。

## 功能

- **逐页提取**：调用 Zotero 自带的 `PDFWorker.getFullText()`，返回文本以 `\f` 分页，因此**每个检索结果都带真实页码**，可以直接翻到那一页
- **混合检索**：BM25（`Intl.Segmenter` 中文分词，无词典依赖）+ 可选稠密向量（本机 Ollama `bge-m3`），RRF 融合
- **零外部依赖**：不打包 PDF 库、不打包分词词典、不联网
- **只读**：不修改 Zotero 数据库，索引单独存放在配置目录
- **本地接口**：在 `127.0.0.1:23119` 上提供 `/retrieval/search`、`/retrieval/stats`、`/retrieval/build`、`/retrieval/ping`、`/retrieval/protocol`（Agent 使用协议）等

## 安装

1. Zotero 7 及以上（已在 Zotero 10.0.6 / Gecko 140 上测试）
2. 打开 Zotero → 工具 → 插件
3. 右上角齿轮 → **Install Plugin From File…**
4. 选择从 [Releases](../../releases) 下载的 `retrieval-index-*.xpi`
5. 重启 Zotero

> **注意**：Zotero 默认要求插件**已签名**。本插件未签名，安装前需把
> `xpinstall.signatures.required` 设为 `false`（设置 → 高级 → 配置编辑器，或写进 profile 的 `user.js`）。

## 使用

| 菜单项 | 作用 |
|---|---|
| 工具 → 建立检索索引 | 遍历本机 PDF 附件，逐页取文、切块、可选向量化、落盘 |
| 工具 → 检索索引状态 | 查看块数、文献数、是否含向量 |
| 工具 → 检索自检（示例查询） | 快速验证是否可检索 |
| 工具 → 取消建索引 | 中途停止（已完成部分不保存） |

建完索引后即可调用接口：

```bash
# 混合检索（默认）
curl "http://127.0.0.1:23119/retrieval/search?q=儿童色彩偏好&k=8"

# 纯关键词（不需要 Ollama）
curl "http://127.0.0.1:23119/retrieval/search?q=色彩偏好&mode=bm25"

# 状态 / 诊断 / 触发建索引（limit=N 只处理前 N 个附件）
curl "http://127.0.0.1:23119/retrieval/stats"
curl "http://127.0.0.1:23119/retrieval/ping"
curl "http://127.0.0.1:23119/retrieval/build?limit=20"
```

`/retrieval/search` 返回对象（加 `?flat=1` 可退回纯数组，兼容旧脚本）：

```json
{
  "evidence": {
    "returned": 5,
    "papers": 4,
    "abstractOnlySegments": 1,
    "cacheSourcedSegments": 1,
    "segmentsWithPage": 4,
    "topSegmentRole": "front",
    "retractedFiltered": 0,
    "hints": ["部分片段无页码（来自 Zotero 全文缓存，原文 PDF 缺失）"]
  },
  "results": [
    {
      "itemKey": "ABC123XY",
      "parentKey": "DEF456UV",
      "title": "基于开放活动行为的深圳市高密度小学开放空间研究",
      "page": 7,
      "pageSource": "pdf",
      "role": "front",
      "retracted": false,
      "text": "……段落正文……"
    }
  ]
}
```

### 关于「证据不足」

`evidence` 只给**可核验的事实**，不给「够不够」的判定。实测三条路线都不成立：

| 信号 | 实测结果 |
|---|---|
| 词面覆盖（查询词在片段中的命中率） | 6 个真实问题全部饱和 1.00，无区分度 |
| 稠密余弦绝对值 | 6 题都在 0.72–0.78，区分不开直接/间接证据 |
| 本地 qwen2.5:7b 判定 | 6 题只对 3 题，且单次 10–25 秒 |

根因：这类问题不是「词没覆盖」，而是**研究对象错配**（问题是小学儿童，证据是 24 名大学生）——词面完全匹配、语义却不对，只能由阅读者判断。

**建议用法**：调用方检查 `evidence`，遇到以下情况请在答案里标注局限——

- `topSegmentRole` 为 `front` 或 `abstractOnlySegments` 较多 → 仅有摘要级证据
- `segmentsWithPage` < `returned` → 部分片段无页码
- 片段的研究样本/场景与问题对象不一致 → 间接证据

## 给 AI agent 的使用协议（推荐先读）

检索质量已接近这套语料的性价比上限，**瓶颈在使用方式**：拿到 8 条召回后只读第 1 条、把首页综述段当研究结果、把成人/办公研究外推到小学教室。

完整协议见 **[docs/agent-usage-protocol.md](docs/agent-usage-protocol.md)**（提示词模板 + `evidence` 字段判读 + 覆盖映射表 + 三个真实用例）。同证据 A/B（6 题、盲评）：**协议组 6:0 胜，总分 86.5 vs 73.0（满分 90）**，增益集中在「局限与外部效度」（1.17→3.00）；多源与溯源两项已打平——现代 agent 本来就会读多篇，缺的是**证据分级**。详见 [docs/agent-protocol-ab.md](docs/agent-protocol-ab.md)。

最小可用提示词：

```
调用 GET /retrieval/search?q=<问题>&k=8，先读 evidence 与 hints；
对每条结果标 直接/间接/背景/无关（同文献按 parentKey 合并），
排序靠后但更贴题的优先于第 1 条；直接证据 <2 篇必须声明「单一来源」；
每条结论标（文献简写，页 N），无页码写「页未知，来自全文缓存」，禁止编页码；
区分「原文报告/据此推断/证据不足」，并交代被试与场景差异。
```

**自举**：插件 v0.1.23+ 直接提供协议文本，agent 一条命令即可读到完整规则（不用带着文档）：

```bash
curl "http://127.0.0.1:23119/retrieval/protocol"                 # text/markdown
curl "http://127.0.0.1:23119/retrieval/protocol?format=json"     # {"ok":true,"version":"0.1.23","protocol":"…"}
```

协议正文的单一事实源是 `docs/agent-usage-protocol.md`，`build.py` 在打包时自动生成 `content/protocol-text.js`（勿手改产物）。

## 冒烟回归测试（Zotero 升级后 1 分钟判活）

```bash
python tools/smoke_test.py            # ping + stats + 6 条固定查询 + 排序漂移比对，约 1 秒
python tools/smoke_test.py --json     # 机器可读
python tools/smoke_test.py --freeze   # 确认新排序可接受后，更新黄金基准 smoke_expected.json
```

- 每个请求独立超时 + 总预算 60s，**服务端挂起（历史坑）不会卡死**
- FAIL（退出码 1）：接口不可达/超时、结构变化、结果为空、索引未加载、块数骤降、页码提取退化
- WARN（退出码 0）：排序漂移 / top1 变化，只提示不误判
- 已在 8 类故障注入下实测：错误端口、服务挂起、搜索挂起、预算耗尽、非 JSON、空结果、耗时超标、块数不足

## distribution（内部）

把「插件 + 索引快照 + 部署/自检脚本 + 使用说明」打成一个 zip，队友免建索引、免 OCR、免 Ollama：

```bash
python tools/pack_team_kit.py --out-dir D:\workspace\dist
# 纯净版（剔除个人库 lib1：其 itemKey 在他人机器上不存在，会变成噪声）
python tools/pack_team_kit.py --out-dir D:\workspace\dist --exclude-libraries 1
# 产物：retrieval-index-team-kit-v<版本>-<日期>.zip
#       v0.1.48 全量 → 59,528 块 / 791 父条目；纯净版 → 59,526 块 / 790 父条目（zip 83.6 MB，含 490 篇表格 sidecar；快照 2026-10-25；sha256 d45a14fa…）
```

- 约定：**internal file share/群文件**流转（索引含文献正文，禁止公开上传）；**每月重打一次全量快照**，队友关闭 Zotero 后重跑 `deploy_index.ps1 -Force` 覆盖即可（自动备份、可回滚）
- 队友三步：设 `xpinstall.signatures.required=false` → 装 xpi 重启 → 关 Zotero 跑 `deploy_index.ps1 -Force`，再跑 `verify_install.ps1`（17 项自检）
- kit 源文件在 `tools/team-kit/`（部署脚本、校验脚本、使用说明），打包脚本会注入快照日期/版本/块数/表格篇数并生成 SHA256 清单
- **依赖安装（v0.1.48 包）**：`tools/team-kit/install_ollama.ps1` 一键检测/安装 Ollama + 拉取 bge-m3 + 验证 1024 维；`安装Ollama与bge-m3.md` 含手动/winget/代理/离线模型方案（已进团队包）
- **分发包隐私（v0.1.49）**：xpi 不再打包开发 README；表格 sidecar 去除本机 `source` 绝对路径（改 `sourceFile` 文件名）；脚本/文档示例路径改为环境变量/占位符；审计脚本 `tools/audit 逻辑见 opt_eval/t14/audit_final.py` 复核：脚本/文档/xpi/元数据 **0 命中**，索引正文中 **0 个 Windows 绝对路径**（仅文献原文出现的 user/URL 等词）
- **团队交付验收（v0.1.48）**：安装说明已重写（前置条件含 Ollama+bge-m3、代理例外、autoIncremental 首装建议）；`verify_install.ps1` 升级为 18 项（manifest/xpi sha/接口/协议表格补充段/检索/OCR+表格旁路/Ollama 可选），PASS 18 / WARN 2 / FAIL 0；`deploy_index.ps1` 部署后打印块数/OCR/表格统计；已在模拟 profile 实测部署成功

## 线上调参（v0.1.28+，无需重装）

检索参数已做成 pref，**改完立即生效**，并可通过受控端点设置（白名单 + 范围校验）：

```bash
# 查看当前生效值/范围
curl --noproxy '*' "http://127.0.0.1:23119/retrieval/admin/tune"

# 设置（写操作需 confirm=TUNE）：aggW2 / rrfK / wBm25 / wDense
curl --noproxy '*' "http://127.0.0.1:23119/retrieval/admin/tune?key=aggW2&value=0&confirm=TUNE"

# 恢复默认
curl --noproxy '*' "http://127.0.0.1:23119/retrieval/admin/tune?key=aggW2&reset=1&confirm=TUNE"
```

| 参数 | 含义 | 范围 | 默认 |
|---|---|---|---|
| `aggW2` | 篇级聚合第二块权重 | 0–2 | **0**（线上实测最优；离线结论 0.4 在 Firefox-ICU 下反而拖累 R@5）|
| `rrfK` | RRF 平滑常数 | 1–500 | **30**（T14 段落级验证：30 优于 60，篇级不掉）|
| `wBm25` / `wDense` | 两路权重 | 0–5 | 1 / 1 |
| `abstractPrior` | 摘要页先验（首条替换为含摘要标记的首页块） | 0–1 | **0**（T14 段落级实测：开=负收益，P@1 41.5% → 关=53.7%）|
| `capPerPaper` | 同一文献最多返回几段 | 1–5 | 2（cap=2 段落级最优）|
| `rerank` | 启用 GPU 重排（需本地服务） | 0–1 | 0（本机已手动开为 1）|
| `rerankN` / `rerankW` | 重排候选数 / 重排权重 | 1–50 / 0–1 | **10** / 0.6 |

自动部署脚本：`tools/deploy_xpi.ps1 <xpi路径>`（关 Zotero → 备份旧 xpi → 替换 → 重启 → 等插件响应），改代码后一条命令上线。

## 段落级评估（v0.1.32+）

篇级 R@5 会掩盖"论文对了、段落错了"。repo 的 gold 自带 `gt_cid`（查询来源段落），
`D:\workspace\eval\chunks.jsonl` 存有原文 → **47/75 条可免费映射成段落级金标**（high 41 / wide 47）。

| 指标（v0.1.35，插件真实接口） | high（n=41） | wide（n=47） |
|---|---|---|
| 段落 P@1（首条含来源段） | **53.7%** | 51.1% |
| 段落 P@5 / P@20 | 73.2% / 80.5% | 70.2% / 76.6% |
| 篇级 top-5 | 95.1% | 95.7% |
| **篇✓段✗（论文对、段落错）** | **21.9%** | **25.5%** |

工具在 `opt_eval/t14/`：`build_passage_gold.py`（重建后可重映射）、`passage_eval.py`（段落级 harness）、
`phase_c_metrics.py`（参数 A/B）、`paper_quick.py`、`cross_table.py`，报告 `T14_REPORT.md`。
参数结论：**rrfK=30**、**abstractPrior=0**（摘要页先验实测为负收益）、**capPerPaper=2**；k 建议见协议文档（默认 k=8）。

## 团队实时更新索引（v0.1.51）

方案 A（推荐）：中心机跑检索服务 + 内网反向代理 `http://192.0.2.10:23120`，队友零安装、agent 改 URL 即用；中心机自动增量 90s/30min，索引随群组同步自动更新。
方案 B：队友本地开 `autoIncremental`（需 Ollama + 本地 PDF），各自索引随库更新；方案 C：月度快照兜底。
见 `docs/团队实时更新.md`（包内《可选-团队实时更新.md》）。代理 `tools/retrieval_proxy.py` 已开机自启（RetrievalProxy.lnk）+ watchdog（每 5 分钟）+ 防火墙仅放行 192.0.2.0/24。

## 文献库增长后的索引更新（v0.1.50，2026-10-25）

群组新增文献后：自动增量会把「已就绪的 PDF」并入（本次 59,528→69,496 块，+72 篇）；
但同步中/当时未下载完的 PDF 会以 no_text 失败，且失败不会被指纹变化的增量自动重试。
补齐办法：`opt_eval/t14/backfill_text_sidecars.py` 给「有文本层但未入索引」的文献写文本旁路（本次 63 篇），
纯扫描件用 `tools/ocr_helper.py --keys ...` OCR（本次 8 篇因韩/日文 OCR 质量不达标未纳入），再触发一次增量。
本轮结果：69,496 块 / 915 篇 / 表格 492 篇(2,870 张) / OCR 130 篇。
旧金标指标：R@5 90.67%（副本别名后 92.00%）、H@1 76.00%、MRR 0.8130；抽查 18 条 H@1 变化样本，top1 多数仍高度相关（金标是旧快照的单目标，新增文献会改变排序）。

## 无 Ollama 时的质量代价（实测 2026-10-24）

同索引、75 条金标：混合+重排 R@5 **92.00%** / H@1 **82.67%** / MRR 0.8596；
纯 BM25（无 Ollama）**74.67% / 64.00% / 0.7073**（R@5 −17.3pp）；BM25+rerank 76.00% / 65.33% / 0.7162。
段落级首条命中 61.0% → 41.5%（P@5 73.2% → 61.0%，加 rerank 可回到 70.7%）。
结论：Ollama + bge-m3 是质量关键项（1.2GB，CPU 也可跑）；无向量时 reranker 只能小幅补偿。

## 可选重排：本地 GPU reranker（v0.1.36+）

段落级金标复测（T14）显示 cross-encoder 在**段落级**有明显价值（此前 T7 只按篇级判"不达标"）：
融合 N=20 / w=0.6 时，段落 P@1 53.7%→63.4%、P@5 73.2%→80.5%，篇级 H@1 +1.33pp、MRR +0.0147。

| 项 | 说明 |
|---|---|
| 模型 | `bge-reranker-base`（PyTorch fp16，约 1.1GB，放 `D:\workspace\opt_eval\t14\rr_model`，**不随团队包分发**）|
| 服务 | `python tools/rerank_service.py`（127.0.0.1:8898）或 `powershell -File tools/start_rerank_service.ps1`；**已配置开机自启**（启动文件夹 `RerankService.lnk`）+ **watchdog 计划任务**（每 5 分钟健康检查，失败自动拉起，脚本 `tools/rerank_watchdog.ps1`）|
| 插件开关 | `curl --noproxy '*' "http://127.0.0.1:23119/retrieval/admin/tune?key=rerank&value=1&confirm=TUNE"`（关：`reset=1`）|
| 兼容性 | 服务不可用时自动跳过并回落（1.2s 超时 + 60s 内不再尝试），不影响检索；默认关闭 |
| 开销 | GPU 打分 34–66ms（N=10/20），实测端到端 **+69ms/查询**（k=8）|
| 线上实测 | 篇级 H@1 81.33→82.67%、MRR 0.8529→0.8596；段落 P@1(high) 58.5→61.0%（N=10）、wide +4.2pp；**判定口径首条充分率约 72.8%** |

## 冒烟基准的参数固定（v0.1.38+）

`python tools/smoke_test.py --freeze` 会把当前 `/retrieval/admin/tune` 的 effective 参数写入 `smoke_expected.json`；
常规冒烟会自动按该参数固定配置、结束后恢复。因此调参（如 rerankN）不会让冒烟误报漂移；重排服务不可用时插件自动回落，冒烟会如实反映配置差异。

## 表格补充段（v0.1.42+）

列表/指标类答案常在表格里，而正文文本按列乱序。做法：**表格不进索引排序**，只在检索结果末尾补附表内容。

```bash
# 1) 从 Zotero PDF 抽取表格（生成 retrieval-index/tables/<key>.json + _manifest.json）
python tools/extract_tables.py --all --workers 4
# 全量实测：454 篇 / 2,748 张表 / 0 失败（3.7 MB sidecar）
```

- 检索返回的 `results` 末尾可能带 `supplement: true, role: 'table'` 的条目（默认最多 2 条，来自已召回论文的表格）；
- `evidence.tableSupplements` 给出条数，`hints` 会提示"末尾为表格补充段"；
- 参数：`tableSupplement`(0/1)、`tableSupplementMax`(0–5)，`/retrieval/admin/tune` 可调；表格按内容指纹去重；
- v0.1.47 起**表格 sidecar 随团队包分发**（455 篇，+1.3 MB），队友部署索引后即生效；
- 反例记录：把表格并入索引会稀释篇级排序（R@5 92→89.33），故默认 `tableChunksBuild=0`（实验开关）；
- v0.1.48：`python tools/fix_table_headers.py` 做**表头多行合并**（双层表头并回一行；417 张表/140 篇），再跑 `merge_tables.py` 做**跨页合并**（续页/空题注/同表号；34 篇/70 次，表数 2760→2690），合并表带 `pages` 页范围，补充段上限 2200 字符；

## 维护接口：重复附件清理（v0.1.24+）

库里同一 PDF 挂了两份（或同一附件被重复导入）时，可用管理接口把**多余附件**移入回收站（可恢复）：

```bash
# 1) 先 dry-run：只回报会做什么，不改任何东西（默认就是 dry-run）
curl -X POST http://127.0.0.1:23119/retrieval/admin/dedup \
  -H "Content-Type: application/json" \
  -d '{"items":[{"libraryID":3,"key":"S8JHY7MS"}]}'

# 2) 确认清单无误后 apply：必须带 confirm 口令
curl -X POST http://127.0.0.1:23119/retrieval/admin/dedup \
  -H "Content-Type: application/json" \
  -d '{"dryRun":false,"confirm":"DELETE-DUPLICATES","items":[{"libraryID":3,"key":"S8JHY7MS"}]}'
```

安全护栏：默认 dry-run；apply 需 `confirm:"DELETE-DUPLICATES"`；**只允许删附件**（拒绝父条目）；**拒删当前索引内的条目**；单次 ≤200 条；执行前自动把条目 JSON 备份到 `<索引目录>/admin-dedup-backup-<时间戳>.json`，删除进回收站可恢复。

> 判据务必用 **PDF 文件哈希**（标题、正文前 1200 字都会漏判）。跨库重复无法用 Zotero 合并，删低优先级库副本会丢该库的标签/分类，建议保留。

## 向量检索（可选）

装了 [Ollama](https://ollama.com) 并拉取模型后会自动启用；检测不到则退化为纯 BM25，其余功能不受影响。

```bash
ollama pull bge-m3
```

## OCR（处理无文本层的扫描件）

插件**不内置 OCR 引擎**（WASM 方案体积大、中文模型数十 MB），改为「外部脚本生成旁路文本 + 插件读取」：

```bash
# 扫候选（约 2 分钟；也可以直接用插件 /retrieval/ocr-candidates，读上次建索引的失败/空白页清单）
python tools/ocr_helper.py --scan --dry-run

# 推荐：带「质量闸 + 跳过清单 + 分批」，每篇写完立即更新 manifest（中断可续跑）
python tools/ocr_helper.py --budget-pages 700 \
  --skip-file D:\workspace\opt_eval\t6\ocr_skip.json \
  --min-bigram 0.45 --min-common 0.90

# 只补空白页（文献已有部分文本层）
python tools/ocr_helper.py --scan --only-blank

# 处理完并入索引（增量即可，插件会识别新增 OCR 并重抽取这些条目）
curl --noproxy '*' "http://127.0.0.1:23119/retrieval/build?incremental=1"
```

产物写入 `<索引目录>/ocr/`：`_manifest.json` 与 `<itemKey>.json`（`{"pages":[...], "ocrPages":[...]}`，保留页分隔，**仍有真实页码**）。

### 引擎与语言（含质量闸）

使用 **Windows 内置 OCR**（`Windows.Media.Ocr`），零外部依赖。需要对应的 OCR 语言包：

```powershell
Get-WindowsCapability -Online -Name 'Language.OCR*'   # 查看已装语言
# 安装示例（联网；本机实测日语/英语包各约 7–8 分钟，Windows Update 慢时可能更久）
Add-WindowsCapability -Online -Name 'Language.OCR~~~ko-KR~0.0.1.0'
Add-WindowsCapability -Online -Name 'Language.OCR~~~ja-JP~0.0.1.0'
Add-WindowsCapability -Online -Name 'Language.OCR~~~en-US~0.0.1.0'
```

**多语言支持（v0.1.24 后的 ocr_helper）**：`--lang zh-CN | ko-KR | ja-JP | en-US`，每个语言独立跑一遍、写同一份 manifest。

| 语言 | 质量闸（低于即丢弃并记入跳过清单） | 本机实测 |
|---|---|---|
| 中文 zh-CN | 中文 bigram 命中率 ≥0.45 **且** 常用字覆盖率 ≥0.90 | 43 篇 / ~8,500 页 |
| 韩语 ko-KR | Hangul 占比 ≥0.10 | 12 篇 / 825 页，实测 0.30–0.86 |
| 日语 ja-JP | 假名占比 ≥0.03 | 2 篇 / 9 页，实测 0.28–0.48 |
| 英语 en-US | 拉丁字母占比 ≥0.50 | ~10 篇（进行中） |

> **为什么必须有质量闸**：zh-CN 引擎读韩文会把 Hangul 误识别成「看起来像汉字、其实全是罕见字」的乱码；旧 `--probe-lang`（只看汉字占比）会把韩文**误判为中文**写进索引。现在改为按语言校验字形占比，判据数据来自语料本身（`tools/cjk_common_chars.txt`、`tools/cjk_bigrams.txt`）。

### 规模参考（本机实测）

| 类别 | 规模 | 状态 |
|---|---|---|
| 完全无文本层 | 70 篇 / 8,896 页 | 已 OCR **67 篇 / 9,912 页**（中 43 + 韩 12 + 日 2 + 英 10，含历史批次）；剩余 20 篇**缺 PDF**，无法 OCR |
| 部分空白页 | 149 篇 / 2,124 页 | 已并入前 80 页内的 612 页缺口（超出 `maxPagesPerItem` 的页本就不索引）|
| 索引内 OCR 块 | **8,447 块 / 65 篇** | 中文 7,509 + 韩日 789 + 英 149（`XFN55VKS`/`HII23RCN` 与同内容副本去重后各少 1 篇）|

Windows OCR 实测 **2.0–2.3 秒/页**（200 dpi）；7,000 页约 4 小时，建议分批（每批 700 页，约 26 分钟）。

## 配置

设置 → 高级 → 配置编辑器：

| 键 | 默认值 | 说明 |
|---|---|---|
| `extensions.zotero.retrieval.ollamaURL` | `http://127.0.0.1:11434` | Ollama 地址 |
| `extensions.zotero.retrieval.model` | `bge-m3` | 嵌入模型 |
| `extensions.zotero.retrieval.maxPagesPerItem` | `80` | 单篇最多取页数，超出截断 |
| `extensions.zotero.retrieval.chunkChars` | `520` | 每块目标字符数 |

## 构建

```bash
python build.py        # 产出 retrieval-index-<version>.xpi
```

`build.py` 读取 `manifest.json` 的版本号并打包以下文件：

```
manifest.json  bootstrap.js  prefs.js  content/  chrome/
```

## 技术说明

- **分页文本**来自 `Zotero.PDFWorker.getFullText(itemID, maxPages)`，返回 `{text, extractedPages, totalPages}`，`text` 中用 `\f` 分隔页
- **分块**按页进行、不跨页，因此页码如实（PDF 物理页序，非印刷页码）
- **中文分词**使用 `Intl.Segmenter('zh', {granularity:'word'})`（ICU），避免自造切分
- **BM25** 参数 `k1=1.5, b=0.75`
- **向量**以 int8 量化后 base64 存入 `vectors.b64`，避免依赖二进制 IO
- 索引位置：`<Zotero 配置目录>/retrieval-index/`

### 检索质量（454 篇中文为主语料，bge-m3 + BM25）

| 查询类型 | 方法 | 篇级 Recall@5 |
|---|---|---|
| 自然语言（35 条） | BM25 | 94.3% |
| | 稠密向量 | 82.9% |
| | **混合 RRF** | **94.3%** |
| 关键词（40 条） | BM25 | 100% |
| | 稠密向量 | 92.5% |
| | **混合 RRF** | **100%** |

结论：**稠密向量单独用会掉 11–20 个百分点，必须混合**。

> 2026-10-23 在**当前 52,298 块 / 739 篇**索引上复跑金标：n=73 可定位（natural 34 + keyword 39），当前默认配置（等权 RRF k=60、无标题加成、同父条目 cap2）**篇级 R@5 = 91.8%、Hit@1 = 78.1%、MRR@10 = 0.849**。其中 n=73 是**删失口径**（另有 2 条金标的内容仍在索引里，但对应 itemKey 被 v0.1.20 去重的库内 tie-break 顶掉，属评估映射伪失败）：按 titleKey 修复映射后 **n=75，R@5 = 90.67%、Hit@1 = 77.33%、MRR@10 = 0.8397**，这是建议对外报告的口径。同一批 75 条在**去重前**的 sit-only 索引（54,778 块）上指标完全相同（90.67% / 77.33% / 0.8397）→ **去重对篇级 R@5 零效应**。历史「95.0%」是「40 条正规论文查询 + 剔除孤立 PDF 噪块语料」的口径（38/40），同口径在当前索引为 92.50%（37/40）；差异来自语料是否含孤立 PDF 噪块，不是索引退化。详见 `D:\workspace\opt_eval\audit\T1_audit.md`。
>
> **2026-10-24 OCR 并入后**（7,020 页 → 58,590 块 / 768 篇）复跑同一金标：**n=75、R@5 90.67%、Hit@1 78.67%（+1.33pp）、MRR@10 0.8417**——R@5 持平、Top-1 精度小幅提升。
>
> 之后又并入韩/日/英 OCR（+939 页 → **59,528 块 / 791 篇**）：同一金标指标**不变**（n=75、R@5 90.67%、Hit@1 78.67%、MRR 0.8417），多语言 OCR 内容未稀释中文检索。


## 实测（本机 Zotero 10.0.6，个人库 + 两个群组库）

| 项 | 结果 |
|---|---|
| 扫描附件 | 1,108 个（the-group 群组库 1,041 / the-group 65 / 个人库 2）|
| 实际索引文献 | **482 篇**（537 个 PDF 附件，55 个抽取失败）|
| 片段 | **37,173 块**（全部带页码）|
| 向量 | 1,024 维，int8 量化，embedErrors=0 |
| 建索引耗时 | **15.5 分钟**（含 bge-m3 向量化）|
| 索引体积 | chunks.json 39.9 MB + vectors.b64 48.4 MB |
| 检索延迟 | 混合检索 **88 ms**，纯 BM25 **4 ms** |

### 实测：与“读全文”对比（6 个真实研究问题，第三方盲评）

| 指标 | 无插件（读全文） | 插件检索 |
|---|---|---|
| 字符消耗 | 485,974 | **34,817（−92.8%，约 1/14）** |
| 请求次数 | 55 | 9 |
| 读全文文件 | 11 个 | **0 个** |
| 耗时 | 467.5 秒 | **1.1 秒** |
| 给出页码 | 0 / 6 | **6 / 6** |
| 引文可核验 | 6 / 6 | 6 / 6 |
| 质量总分（满分 90） | 58 | **75** |

结论：插件适合**检索定位**，不宜替代全文精读；推荐“插件定位 → 按页码翻原文精读”。

检索示例（q=低年级学生为何更倾向于在同一层进行活动？）：

`
1. [第66页] 小学建筑交往空间设计研究
2. [第33页] 城市小学校交往空间构成及设计方法
3. [第 2页] 基于儿童心理学的小学建筑空间设计研究
`

## 已知限制

- 页码是 **PDF 物理页序**，不是印刷页码（封面、罗马数字页会造成偏移）
- 超过 `maxPagesPerItem`（默认 80 页）的文献会被截断
- 单个超大 PDF 可能长时间占用 Zotero，建议分批建索引
- **孤立 PDF**（无父条目）默认纳入索引，其标题来自文件名（已做清洗），信息量低的标题会在第一轮被跳过；对精度敏感时可用 `?orphans=0`
- **库内重复条目**（同一文献在多个库/多个条目）检索时已去重（父条目 + 同标题 cap2 + 跨库正文签名去重），但 Zotero 库本身仍是脏数据，未做合并
- OCR 为**旁路**方案（Windows 内置 OCR，保留真实页码）：中文效果良好；**韩/日文需装语言包**，未装时用 bigram/常用字质量闸拦截（见 OCR 章节）
- OCR 已覆盖 **中/韩/日/英** 四语（67 篇 / 9,912 页，含历史批次）；剩余 20 篇无法 OCR（storage 缺 PDF）；韩/日/英需对应 OCR 语言包（本机已装 zh-CN / ko-KR / ja-JP / en-US）
- **v0.1.25 修复**：`titlePoor()` 的两轮循环曾使 **60/835 篇低信息标题文献（4,023 块，含 AIC2016–2023 翻译 8 篇）在任何查询下都不返回**（插件真实 R@5 82.67%）；改为单轮输出后 **R@5 82.67%→88.00%**、Hit@1 70.67%→73.33%，AIC2017 等恢复 rank 1，详见 `docs/t7-reranker-validation.md`
- **v0.1.26 新增**：篇级聚合重排（论文前两块 RRF 分数按 `s1 + 0.4·s2` 排序）——单块勉强命中的文献不再压过多块互相印证的文献；插件真实接口 **R@5 88.00%→90.67%、Hit@1 73.33%→78.67%**（=官方模拟同口径），冒烟重冻 PASS 16
- **v0.1.27 新增**：上下文增强 embedding——建索引时把「论文标题 + 块原文」作为 embedding 输入（截断 900 字符）。离线官方评估 R@5 90.67%→92.00%；**线上实测（v0.1.31 调参后）R@5 92.00%、Hit@1 81.33%、MRR 0.8522**。注意：离线评估用 Node ICU 分词、插件用 Firefox ICU，个别查询排序不同，上线前须用插件真实回放复核（见 `opt_eval/t11/T13_REPORT.md`）
- 部分 PDF 文本层本身是 OCR 结果，质量参差

> 已实现（v0.1.19+）：**增量更新**——启动 90 秒后自动跑增量，无变更约 3 秒短路；手动 `GET /retrieval/build?incremental=1`。

## 许可

MIT，见 [LICENSE](LICENSE)。
