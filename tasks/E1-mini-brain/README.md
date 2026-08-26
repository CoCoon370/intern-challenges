# E1 · 迷你企业大脑

## 背景

我们内部有一个知识入口，给 Agent 和新人共用：问「量房要多久上门」「为什么不做实木」这类问题，它从几十篇内部文档里找出答案并**附上出处（文件路径 + 行号）**。它有两条不能破的契约：

1. **出处必附**——没有出处的答案在我们这里等于没答，因为没人能核对。
2. **实时数字不进索引**——余额、库存、线索数这类每天变的数字，索引里存的永远是重建那一刻的快照，Agent 会用同样自信的语气把过期值报出来，且没有任何机制让它自知。所以这类问题必须**路由到实时源**，而不是从索引里答。

这道题是它的迷你版：语料换成虚构品牌「云栖家居」（全屋定制：衣柜 / 橱柜 / 榻榻米 / 全屋）的约 30 篇内部文档。

## 任务

做一个可以打成 Docker 镜像的 CLI：

- `index`：读 `/corpus` 下的 markdown，把索引写到 `/data`
- `search "<问题>" --k 3`：返回最相关的 ≤ k 段落，每段带路径、行号、片段；遇到「实时数字」类问题时改为路由到实时源

语料在 `corpus/`：

```
products/   产品线、板材、五金
pricing/    报价公式、活动规则
sop/        量房、设计、下单、生产、安装、售后、投诉
faq/        对客与内部常见问题
decisions/  决策记录（为什么不做 X）
ops/        内部系统与工具说明
daily/      每日变化的运营数据
```

## 接口（评测按此调用，字段名不要改）

```bash
docker build -t e1 submissions/E1/<login>
docker run --rm -v "$CORPUS":/corpus:ro -v "$DATA":/data e1 index
docker run --rm -v "$CORPUS":/corpus:ro -v "$DATA":/data e1 search "<query>" --k 3
```

- `index`：读 `/corpus`，把索引写到 `/data`，exit 0
- `search`：**stdout 只输出一个 JSON 对象**（日志、进度等其它输出走 stderr）：

```json
{
  "route": "index",
  "results": [
    {"path": "products/wardrobe.md", "line_start": 12, "line_end": 20, "snippet": "……", "score": 0.83}
  ],
  "note": "可选说明"
}
```

- `route` ∈ `index` | `live`。`live` 表示「这个问题的答案是实时数字，不应从索引答」，此时 `results` 可为空，`note` 写应去查什么实时源
- `path` 相对 `/corpus` 根，正斜杠
- `--k` 默认 3，最多返回 k 条
- 评测环境**不提供任何 LLM key**，方案必须在无外部 key 时可运行（有 key 时可以更好）

## 本地跑评测

```bash
docker build -t e1 submissions/E1/<login>
uv run tasks/E1-mini-brain/eval/run.py --image e1 --mode public
```

stdout 最后一行是结果 JSON（`score` / `passed` / `total` / `hard_violations` / `details`），逐题原因在 stderr。单题 `search` 超时 60 秒。

## 评分

- 公开集 `eval/public_qa.json` 20 条，CI 自动跑；隐藏集 15 条，面试官在 PR 上评论 `/eval` 触发，内容不公开
- 每题判定（见 `eval/run.py`）：
  - `passage` 题：top-k 中任一 result 的 `path` 在期望列表内即命中；若题目给了 `must_contain_any`，命中那条的 `snippet` 须含其中之一
  - `live` 题：`route == "live"` 才算命中
  - **`.archive/` 开头的 path 永远不算命中；top-1 是 `.archive/` 的题直接 0 分**
  - 隐藏集会检查某些**不该出现**的内容：任一 result 的 `snippet` 或顶层 `note` 里出现即记「泄漏」硬违规，整题 0 分并单独标红
- 得分 = 命中 / 总数；硬违规或得分 < 0.5 时评测退出码为 1
- 除自动评分外，我们还会看 PR 描述里的「遇到的问题 / 我的假设 / 没做的事」，以及走读时你能否解释每个决定

## 提示

语料来自真实内部文档的脱敏版，**不保证干净，请按生产环境对待**。

## 提交清单

- [ ] `submissions/E1/<github-login>/Dockerfile`（构建上下文 = 该目录）
- [ ] `submissions/E1/<github-login>/README.md`：怎么跑、设计思路、**遇到的问题与取舍**、**没做的事**
- [ ] 本地 `uv run tasks/E1-mini-brain/eval/run.py --image e1 --mode public` 跑过，README 里贴最后一行 JSON
- [ ] 可选：测试、`NOTES.md`

## 自由度

任何语言、任何检索方式（纯关键词 / BM25 / embedding / 混合）都可以。不要求高分，要求**能解释**：为什么这么切分、为什么这么打分、哪些问题你决定不从索引答。
