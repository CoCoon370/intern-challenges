# E2 工作流自动化：下单 → 渲染 → 通知

## 背景

我们有一条真实在跑的自动化链路：客户下单后系统发一个 webhook，工作流收到后把订单提交给一个
GPU 渲染服务（按需拉起的实例，**冷启动要几十秒**，偶尔会返回 5xx），然后轮询渲染进度，渲染完
把结果地址通知给下游（真实场景是存对象存储 + 发飞书卡片）。

这条链路上线后踩过的坑，每一个都是真实事故：批量下单时整批超时、一单被渲染了两次、任务挂死
了没人知道。这道题是这条链路的缩小版——渲染服务和通知端换成了仓里自带的 mock，其余约束和
线上一样。

## 任务

写一个服务（容器化），做到：

1. 接收下单 webhook `POST /webhook/order`
2. 把订单提交到渲染任务 API（mock）
3. 轮询直到出结果
4. 把结果**通知**到 `NOTIFY_URL`

技术栈不限：Python / Node / Go / n8n 都可以（n8n 见下面「n8n 方案指引」）。评测只看接口行为。

## 接口规范

### 你的服务（容器）

容器启动时会拿到这些环境变量：

| env | 值（CI） | 说明 |
|---|---|---|
| `PORT` | `8700` | 你的 HTTP 服务监听端口（请绑定 `0.0.0.0`） |
| `RENDER_API_URL` | `http://127.0.0.1:8600` | 渲染任务 API 根地址 |
| `NOTIFY_URL` | `http://127.0.0.1:8600/notify` | 通知端点 |
| `JOB_TIMEOUT_SEC` | `60` | 一个订单从提交起最多等多久 |

CI 用 Linux runner + `docker run --network host`，所以 mock 就在 `127.0.0.1`。在 macOS 上本地跑
评测时 driver 会自动改用 `-p 8700:8700` + `host.docker.internal`（见「本地怎么跑」）——**不要
把地址写死，一律读 env**。

必须实现的端点：

| 端点 | 要求 |
|---|---|
| `GET /healthz` | 返回 200。评测等它 200 才开始发 webhook（最多等 60s），所以 200 时 webhook 必须已经可用 |
| `POST /webhook/order` | body `{"order_id": "ord-1001", "scene": "living-room", "image_urls": ["…"]}`，与 `POST /jobs` 相同。**必须 2 秒内返回 2xx**（调用方 3 秒就断） |

之后异步处理，处理完成后每个 `order_id` **恰好一条**通知 `POST` 到 `NOTIFY_URL`（JSON）：

```json
{"order_id": "ord-1001", "status": "succeeded", "result_url": "https://…"}
{"order_id": "ord-1005", "status": "failed", "error": "render engine crashed", "attempts": 1}
```

- `succeeded` 时 `result_url` 非空（就是渲染 API 返回的那个，不需要真的下载）
- `failed` 时 `error` 非空；`attempts` 写你向渲染 API 提交了几次
- `JOB_TIMEOUT_SEC` 内没拿到结果 → 发 `failed` 通知，`error` 里要含 `timeout`

### 渲染任务 API（mock，`mock/mock.py`）

| 端点 | 行为 |
|---|---|
| `POST /jobs` body `{"order_id","scene","image_urls":[...]}` | `202 {"job_id": "job-…"}`。**同一 order_id 重复提交会创建新 job**——幂等是调用方的责任 |
| `GET /jobs/{job_id}` | `{"job_id","status":"queued\|running\|succeeded\|failed","result_url"?,"error"?}`；不存在的 job → 404 |
| `POST /notify` | 记录 body，返回 `204` |
| `GET /_admin/state` | `{"jobs":[...],"notifications":[...],"requests":[...],"meta":{...}}`——调试用，看 mock 到底收到了什么 |
| `POST /_admin/reset` | 清空状态 |
| `GET /healthz` | 200 |

mock 对每个 `order_id` 的行为由 scenario 文件指定（`scenarios/public.json`）。**渲染服务可能这样坏**：

| behavior | 含义 |
|---|---|
| `normal` | queued 1s → running 1s → succeeded |
| `cold_start` | queued 20s → running 3s → succeeded |
| `transient_500` | 前 N 次 `GET /jobs/{id}` 返回 500，之后正常（N 由 scenario 指定，默认 2） |
| `submit_503_once` | 第一次 `POST /jobs` 返回 503，第二次起正常 |
| `permanent_fail` | running 2s → failed，`error: "render engine crashed"` |
| `hang` | 永远 running |

公开 scenario 里 6 个订单覆盖了其中 5 种。**隐藏 scenario 会换 order_id、换 behavior 组合、改
N 值、改到达时序**，你的服务不能依赖任何具体的 order_id 或顺序。不在 scenario 里的 order_id
按 `normal` 处理。

## 本地怎么跑

需要 [uv](https://docs.astral.sh/uv/) 和 Docker（macOS 上 OrbStack / Docker Desktop 均可）。

```bash
cd tasks/E2-workflow-automation

# 1. 起 mock（另开一个终端）
uv run mock/mock.py --port 8600 --scenario scenarios/public.json

# 2. 手动试一下 mock
curl -s -X POST localhost:8600/jobs -H 'content-type: application/json' \
     -d '{"order_id":"ord-1004","scene":"x","image_urls":[]}'
curl -s localhost:8600/jobs/<job_id>        # 多敲几次，看它怎么变
curl -s localhost:8600/_admin/state | jq

# 3. 本地跑你的服务（不进容器也行）
PORT=8700 RENDER_API_URL=http://127.0.0.1:8600 NOTIFY_URL=http://127.0.0.1:8600/notify JOB_TIMEOUT_SEC=60 <你的启动命令>
curl -s -X POST localhost:8700/webhook/order -H 'content-type: application/json' \
     -d '{"order_id":"ord-1001","scene":"x","image_urls":[]}'

# 4. 完整评测（driver 自己起 mock，别再手动起一个占着 8600）
docker build -t e2-mine submissions/E2/<你的 github login>
uv run eval/run.py --image e2-mine --mode public
```

评测输出：stderr 是过程日志（每次 webhook 的耗时、每单判定、容器日志尾部 30 行）；**stdout
最后一行是结果 JSON**。`--mock-port` / `--app-port` / `--net-mode host|bridge` 可改。

## 评分

driver 流程：起 mock → 起你的容器 → 等 `/healthz` ≤60s → 按 scenario 发 webhook → 等到
`deadline_sec`（公开集 120s）→ 读 `/_admin/state` → **逐单**做五项检查，全过才算这一单通过：

| 检查 | 通过条件 |
|---|---|
| 响应及时 | 该单每次 webhook 响应 ≤2s 且 2xx |
| 通知唯一 | 该 `order_id` 的通知条数 == 1 |
| 结果正确 | `normal`/`cold_start`/`transient_500`/`submit_503_once` → `succeeded` 且 `result_url` 非空；`permanent_fail` → `failed`；`hang` → `failed` 且 `error` 含 `timeout` |
| 幂等 | 该 `order_id` 在 mock 的 `jobs` 里只有 1 个 job |
| 不滥重试 | 该 order 的 job 数 ≤3；对同一 job 的 `GET` 轮询间隔 ≥0.5s（容差 0.05s，防忙轮询） |

得分 = 通过的订单数 / 总订单数。**硬违规**（任一出现即整题不通过、退出码 1）：

- `webhook_timeout`：任何一次 webhook 超过 2s 才返回
- `duplicate_notification`：任何一个 order_id 收到 ≥2 条通知
- `container_not_ready`：60s 内 `/healthz` 没 200

结果 JSON 形如：

```json
{"task":"E2","mode":"public","score":0.83,"passed":5,"total":6,"hard_violations":["duplicate_notification"],"details":[{"id":"ord-1002","ok":false,"why":"2 notifications","behavior":"normal","checks_failed":["duplicate_notification"],"jobs":1,"notifications":2}]}
```

退出码：硬违规或 score < 0.5 → 1；否则 0。CI 跑公开集；面试官用 `/eval` 跑隐藏集（隐藏集结果
只给分数和 order_id）。

## n8n 方案指引

可以用 n8n 做，但同样受上面接口约束，而且**要能在 CI 的容器里无人值守地跑起来**：

- Dockerfile 基于 `n8nio/n8n`，把导出的工作流 JSON `COPY` 进镜像
- 启动脚本里先 `n8n import:workflow --input=<file>` 导入并**激活**，再启动 n8n（不同版本激活方式
  有差异，务必在容器里 `curl` 一遍 webhook 确认真的通了，别只看 UI）
- Webhook 节点路径设为 `order`、方法 `POST`，n8n 的生产 webhook 地址就是 `/webhook/order`
- `GET /healthz` 可以直接用 n8n 自带的 `/healthz`；监听端口用 `N8N_PORT=$PORT`
- 评测环境没有任何账号或 key，n8n 的所有 HTTP Request 节点都只访问 mock

## 提示

- webhook 调用方 **3 秒就断**，它不关心你的渲染做没做完
- 冷启动 20s 是正常现象，不是故障；`JOB_TIMEOUT_SEC` 才是「等太久」的界线
- 渲染 API 会偶发 5xx，也会永久失败；两种情况你的处理应该不一样
- mock 的 `GET /_admin/state` 把它收到的每个请求都记着（时间戳、状态码），评测靠它判定；你自己
  调试也用它，比猜有用
- 允许并鼓励用 AI 工具；但走读时要能解释每个决定

## 提交清单

- [ ] `submissions/E2/<github-login>/Dockerfile`（构建上下文 = 该目录，`--network host` 运行）
- [ ] `submissions/E2/<github-login>/README.md`：怎么跑、设计思路、**遇到的问题与取舍**、**没做的事**
- [ ] 本地 `uv run eval/run.py --image <img> --mode public` 通过，把最后一行 JSON 贴进 PR 描述
- [ ] （可选）测试、`NOTES.md`、n8n 导出的 workflow JSON
- [ ] 分支名 `task/E2-<github-login>`，PR 只改自己的目录
