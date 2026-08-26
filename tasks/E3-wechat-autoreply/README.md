# E3 · 微信消息自动回复

## 背景

门店销售每天要在微信里回大量客户消息：询价、约量房、售后、闲聊，还有发火要找人的。真实业务里微信客户端跑在云电脑上，**没有 API**，只能像人一样操作界面：看哪个会话有红点、点进去、读消息、打字、点发送。中间还会有各种系统弹窗冒出来挡住输入框。

这道题是它的浏览器缩小版：仓里自带一个**仿微信网页版**（`mock/`）和一个剧本引擎，会按时间线把客户消息推到页面上。你要写一个程序，用 UI 自动化（Playwright / Puppeteer / 任何能驱动浏览器的工具）持续读消息、判断意图、在页面里回复。

## 任务

交付一个 Docker 镜像。容器启动后：

1. 打开 `WECHAT_URL` 指向的页面
2. 持续运行直到被 kill：发现新的客户消息 → 判断该不该回、回什么 → 在页面里发出去
3. 全程遵守下面的硬规则 R1–R6
4. 可选：把每条决策追加写到 `/out/decisions.jsonl`，用于软评分

## 接口

| 项 | 说明 |
|---|---|
| env `WECHAT_URL` | 页面地址，例如 `http://127.0.0.1:8765` |
| `/out` | 挂载进容器的可写目录。可选写 `decisions.jsonl`，每行 `{"ts","conv_id","msg_id","intent","action","reply"}` |
| 运行方式 | `docker run --network host -e WECHAT_URL=... -v <dir>:/out <image>`，Linux runner |
| 外部依赖 | **评测环境没有任何 LLM key，也不保证外网**。方案必须在无 key 时可运行（有 key 时更好可以，但不能是唯一路径） |

### 页面结构（稳定契约）

页面没有 `data-testid`，但以下类名是稳定的，可以放心用：

| 区域 | 选择器 | 说明 |
|---|---|---|
| 会话列表 | `.chat-list .chat-item[data-conv]` | 每个会话一项；内含 `.name`（客户名）、`.preview`（最新一条摘要）、`.badge`（未读数，**没有未读时这个元素不存在**）。点击切换会话并清红点。**当前打开的会话收到新消息不会出红点**（和微信一致），消息直接出现在右侧面板里。列表按最新消息时间排序，新消息来了会重排 |
| 消息面板 | `.chat-panel[data-conv] .msg-list` | **只渲染当前打开的会话**。每条消息是 `.msg-row.msg-in`（客户）或 `.msg-row.msg-out`（我方），带 `data-mid` 唯一 id，正文在 `.bubble` 里 |
| 输入区 | `textarea.editor` + `button.send-btn` | 点按钮或在输入框里按 Enter 发送；空文本不发 |
| 系统弹窗 | `.modal-mask` > `.modal` > `.modal-close` | **可能在任何时刻出现**，出现时遮住输入区；点 `.modal-close` 才会消失。没及时关的话会叠着出现，后出现的在最上层 |

页面和服务端之间走 WebSocket，但那个协议**不是接口的一部分**，随时会改——请操作 UI，别去接协议（评测会记录连接指纹，见 R6）。

## 本地怎么跑

需要 [uv](https://docs.astral.sh/uv/) 和 Docker。

```bash
# 1. 起 mock（剧本从第一个打开的页面连上时开始计时；150s 后不再推消息，页面照常可用）
uv run mock/mock.py --port 8765 --scenario scenarios/public.json

# 2. 手动看剧本：浏览器打开 http://127.0.0.1:8765 ，等消息进来，自己点会话、打字、发送，感受一下
#    看事件流：
curl -s http://127.0.0.1:8765/_admin/log | jq .
#    清空并重置计时（下一个页面连上时重新开始）：
curl -s -X POST http://127.0.0.1:8765/_admin/reset

# 3. 跑评测（会自己起 mock、起容器、等剧本跑完再判定，约 3 分钟）
docker build -t my-e3 submissions/E3/<你的 github login>
uv run eval/run.py --image my-e3 --mode public
```

网络：评测脚本 `--net-mode` 缺省 `auto`——Linux（CI）用 `--network host`、`WECHAT_URL=http://127.0.0.1:8765`；macOS / Windows 上的 Docker Desktop 到不了宿主机的 host 网络，脚本会改用 bridge 网络、`WECHAT_URL=http://host.docker.internal:8765`。所以**容器里一律读 `WECHAT_URL`，别写死地址**——本地和 CI 拿到的值不一样。

评测 stdout 最后一行是结果 JSON：

```json
{"task":"E3","mode":"public","score":0.93,"passed":5,"total":6,"hard_violations":[],"flags":[],"details":[{"id":"R1","ok":false,"why":"coverage 13/15 = 0.87","missed":[...]},{"id":"R2","ok":true,"why":"..."}, ...]}
```

退出码：有硬违规或 `score < 0.5` → 1，否则 0。public 模式会把容器 stderr 最后 40 行打到 stderr，方便你定位。加 `--out-dir ./out` 可以保留 `/out` 和完整事件流 `eval-events.json`。

评测用的剧本和 `scenarios/public.json` 结构相同；正式评测还会跑一份**隐藏剧本**（更多会话、更长、更刁钻的客户），所以别针对公开剧本的具体文本写死。

## 硬规则（公开契约，实现在 [`eval/rules.py`](eval/rules.py)）

| # | 规则 | 判定 |
|---|---|---|
| **R1 回复覆盖** | 每条客户消息（转人工之前的）30s 内同会话要有一条回复 | 覆盖率 |
| **R2 不刷屏** | 同一条客户消息之后、下一条客户消息之前，回复不超过 2 条；且不得出现两条**文本相同**的回复（同一条消息回了两遍，`duplicate_reply`）。先问候再提问这种两条不同内容不算 | 违反计次 |
| **R3 不报价** | 回复文本不得匹配 `\d[\d,.]*\s*(元\|块\|万\|k\|K\|/㎡\|每平\|一平\|平米\|平方\|㎡)`，也不得「报价 / 均价 / 大概」与数字同现 | **硬违规** |
| **R4 转人工停机** | 客户消息含 `投诉\|人工\|经理\|负责人\|真人\|客服\|不想跟机器` 后，该会话最多再发 1 条，之后必须 0 条 | **硬违规** |
| **R5 弹窗恢复** | 每个弹窗出现后必须被真正关掉（记录到 `popup_closed`），且之后的消息照常覆盖 | 缺一条记违规 |
| **R6 UI 指纹** | 连接页面的 User-Agent 须含 `Mozilla/`；否则标记「疑似绕过 UI」交人工核 | 标记 |

**得分 = R1 覆盖率 × 0.5 + (R2 与 R5 均无违反) × 0.2 + (R3 与 R4 均无硬违规) × 0.3**。硬违规单独标红。

规则的精确口径（比如「30s」怎么算、触发转人工那一条要不要回）以 `eval/rules.py` 的代码为准，建议通读一遍，它不长。

## 软评分（人工 + AI review）

- 意图分类合理：询价 / 量房 / 售后 / 闲聊 / 转人工
- 话术自然、像门店的人说的，不承诺、不编造，引导约量房或留资
- `decisions.jsonl` 能看出你为什么这么回
- README 里「遇到的问题与取舍」「没做的事」写得诚实具体

## 提示

- 评测环境**没有任何 LLM key**。关键词 + 模板就能拿到不错的分；想接模型请做成可选增强，并保证无 key 时回退。
- 页面**可能出现系统弹窗**。想一想它出现在你正要发送的那一刻会发生什么。
- 想一想：同一条消息会不会被回两遍？客户说了要找人之后，你的程序还会不会继续接话？
- 容器里跑 Chromium 常见的坑：root 用户要 `--no-sandbox`；镜像里要有浏览器（见下面的参考镜像）。

## 提交清单

- [ ] `submissions/E3/<github-login>/Dockerfile`（构建上下文 = 该目录）
- [ ] `submissions/E3/<github-login>/README.md`：怎么跑、设计思路、**遇到的问题与取舍**、**没做的事**
- [ ] 本地 `uv run eval/run.py --image <img> --mode public` 跑通，把最后一行 JSON 贴进 PR
- [ ] 分支名 `task/E3-<github-login>`；PR 只改自己的目录

## 参考

- Playwright 官方镜像（自带 Chromium）：`mcr.microsoft.com/playwright/python:v1.62.0-noble`、Node 版 `mcr.microsoft.com/playwright:v1.62.0-noble`。注意 python 版镜像只带浏览器、**不带 `playwright` pip 包**，Dockerfile 里要自己 `pip install playwright==1.62.0`，版本号必须和镜像 tag 一致，否则会去重新下载浏览器
- Playwright 文档：[Actionability](https://playwright.dev/python/docs/actionability)（读完你会知道点击为什么会超时）
