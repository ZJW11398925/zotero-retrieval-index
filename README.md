# 检索索引（Retrieval Index）— Zotero 插件

给本地 Zotero 文献库建立**带页码的段落索引**，并通过 Zotero 自己的本地 HTTP 服务提供检索接口。目的是让外部程序（尤其是 AI agent）**按需取用相关段落**，而不是把整篇论文塞进上下文。

> 实测：454 篇 / 22,678 段的语料上，取 top-5 段落约 **2,500 token**；直接读 10 篇论文约 **114,600 token**。**约 45–50 倍**的上下文节省。

## 功能

- **逐页提取**：调用 Zotero 自带的 `PDFWorker.getFullText()`，返回文本以 `\f` 分页，因此**每个检索结果都带真实页码**，可以直接翻到那一页
- **混合检索**：BM25（`Intl.Segmenter` 中文分词，无词典依赖）+ 可选稠密向量（本机 Ollama `bge-m3`），RRF 融合
- **零外部依赖**：不打包 PDF 库、不打包分词词典、不联网
- **只读**：不修改 Zotero 数据库，索引单独存放在配置目录
- **本地接口**：在 `127.0.0.1:23119` 上提供 `/retrieval/search`、`/retrieval/stats`、`/retrieval/build`、`/retrieval/ping`

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

## 向量检索（可选）

装了 [Ollama](https://ollama.com) 并拉取模型后会自动启用；检测不到则退化为纯 BM25，其余功能不受影响。

```bash
ollama pull bge-m3
```

## OCR（处理无文本层的扫描件）

插件**不内置 OCR 引擎**（WASM 方案体积大、中文模型数十 MB），改为「外部脚本生成旁路文本 + 插件读取」：

```bash
# 首次：扫库找候选（约 2 分钟）
python tools/ocr_helper.py --scan --dry-run

# 只处理中文文档，最多 1000 页（推荐：先跑一小批验证）
python tools/ocr_helper.py --scan --probe-lang --budget-pages 1000

# 只补空白页（文献已有部分文本层）
python tools/ocr_helper.py --scan --only-blank

# 处理完 → Zotero 工具菜单「增量更新索引」
```

产物写入 `<索引目录>/ocr/`：`_manifest.json` 与 `<itemKey>.json`（`{"pages":[...], "ocrPages":[...]}`，保留页分隔，**仍有真实页码**）。

### 引擎与语言

使用 **Windows 内置 OCR**（`Windows.Media.Ocr`），零外部依赖。需要对应的 OCR 语言包：

```powershell
Get-WindowsCapability -Online -Name 'Language.OCR*'   # 查看已装语言
```

- **中文（zh-CN）效果良好**：实测扫描版期刊每页 2000–3000 字，可读，个别错字（"趼究"→"研究"）不影响检索
- **韩文/日文目前不支持**：本机只装了 zh-CN，`--probe-lang` 会先 OCR 首页探测文字系统，自动跳过韩/日文档（否则会写入乱码污染索引）
- 需要韩/日文时，安装对应语言包（`Add-WindowsCapability -Online -Name 'Language.OCR~~~ko-KR~0.0.1.0'`，需联网）

### 规模参考（本机 852 个 PDF / 30,470 页）

| 类别 | 篇数 | 页数 | 说明 |
|---|---|---|---|
| 完全无文本层 | 70 | 8,896 | 需整篇 OCR；其中相当比例是韩/日文扫描件 |
| 部分空白页 | 149 | 2,124 | 可只补空白页 |
| 空白页占比 | — | 36.1% | 含大量封面/插图页，按需处理 |

Windows OCR 约 **2.3 秒/页**（200 dpi），1,000 页约 38 分钟。

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
- 超过 `maxPagesPerItem` 的文献会被截断
- 尚未实现增量更新：每次建索引是全新的
- 未做 OCR：纯扫描件（无可抽取文本层）无法索引
- 单个超大 PDF 可能长时间占用 Zotero，建议分批建索引
- **同一篇文献若在库中有多个 PDF 副本，检索结果会出现重复条目**（尚未按父条目去重）
- 部分 PDF 文本层为 OCR 结果，质量参差

## 许可

MIT，见 [LICENSE](LICENSE)。
