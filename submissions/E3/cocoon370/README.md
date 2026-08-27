# E3 · cocoon370

## 结论

本方案使用 Playwright 操作仿微信页面，通过离线规则、最近会话上下文和轻量文本相似度完成自动回复。核心路径不需要外网、LLM key 或第三方账号。

当前本机没有 Docker Desktop。验证情况如下：

- 20 项核心分类、上下文、客户交接与安全单元测试；
- 压缩 Demo 剧本的无 Docker 端到端测试；
- 官方 `scenarios/public.json` 完整时序的无 Docker 端到端测试；
- 无 Docker 回归均使用官方 `eval/rules.py` 判定，R1-R6 得分为 1.0；
- GitHub CI 已在 Linux x86_64 上成功构建 Docker 镜像，正式公开评测为 1.00、6/6 通过、无硬违规。

本机未执行本地 Docker 评测；容器构建、Linux 网络和公开集运行已经由本 PR 的 GitHub CI 验证。

## 怎么跑

```bash
docker build -t sub:cocoon370 submissions/E3/cocoon370
docker run --rm --network host \
  -e WECHAT_URL=http://127.0.0.1:8765 \
  -v "$(pwd)/out:/out" \
  sub:cocoon370
```

容器会持续运行直到被 kill。它只读取评测器传入的 `WECHAT_URL`，不会写死本地或 CI 地址。

## 设计

`bot.py` 只负责用 Playwright 扫描会话、关闭弹窗、读取和发送消息；`core.py` 只负责意图、情绪、上下文和回复决策；`customer_notes.py` 只维护客户需求摘要。模块通过 `IntentResult` 协作，发送成功后才记录消息，并把每次决策写入 `/out`，便于独立测试和替换。

## 遇到的问题与取舍

### 对客人设与语气

对客表达按“入职半年、熟悉业务、ESFJ式热情细心的门店同事”设计，并统一使用四步口径：回应客户当前问题；服务失误或售后问题时先道歉；说明能确认的事实与不能越权承诺的边界；最后给出一个明确的下一步。普通询价不会无缘无故道歉，售后故障、久等、无人回复、投诉和明显不满必须道歉。程序不会在客户可见回复中出现“AI、机器人、自动回复、大模型”等身份暴露词，也不会虚构亲身经历或越权承诺。

当前离线模板是必选主路径。未来如增加大模型，只能作为有 key 时的可选润色层，并且输出仍须经过不报价、转人工停机、内部信息和身份词安全检查；密钥只从环境变量读取，不写入代码或提交记录。

### 为什么不只使用关键词

“这周六上午可以不”和“我发照片了”本身信息不完整，必须结合上一轮“预约量房”或“门板划痕”才能判断。本方案先使用高精度短语，再参考最近客户消息，最后才使用字符 n-gram 相似度兜底。

### 为什么没有使用完整向量数据库

语料规模很小，而且评测环境不保证外网。完整嵌入模型会增加镜像体积、构建时间和失败点。当前使用字符二元/三元向量，能够离线识别相近说法；把握不足时使用安全澄清，不强行猜测。

### 为什么E1知识库没有整库复制

只提炼了 `faq/customer.md`、板材、全屋范围、量房和售后的对客口径。明确排除了价格数字、`daily/` 实时快照、`ops/` 配置、内部账号、补偿权限和疑似凭证。完整复制会增加泄密、过期数字和错误承诺风险。

### 客户意向备注

每次成功回复后，程序会把客户的累计意向、当前情绪、处理状态和优先级更新到 `customer_notes.json`。例如客户先询价、后来预约量房，备注会保留为“询价、预约量房”，不会被最后一句“好的”覆盖。售后、量房和转人工会标记为需要人工继续处理。

询价场景会先明确说明线上不能直接报价，然后引导客户提供区域、想了解的产品或空间、装修阶段、方便量房的时间或联系电话，不预设客户一定询问柜类。桌子、沙发等成品家具或软装会单独分类，先登记再确认门店是否提供，不错误承诺或强行引导柜类量房。这些信息只用于更新交接表和推进后续跟进，不进入自动报价逻辑。客户继续补充资料时，程序不会再次暗示可以报价；收到手机号后也不会在对客回复里复述号码。运行输出可能包含客户联系方式，生产环境需要限制访问和设置保留期限。

题目提供的仿微信页面没有“修改客户备注”的按钮，也没有把备注写回页面的稳定接口。为遵守“只能操作页面稳定契约”和“PR 只改自己目录”的规则，本方案没有篡改 mock 页面或伪造备注成功。真实微信版本应增加一个客户端备注适配模块：页面存在备注入口时由 Playwright 点击并填写；当前 Demo 先用 `customer_notes.json` 作为人工交接表。

### 情绪转人工

- 明确出现“投诉、人工、经理、负责人、真人”等公开触发词：回复一次后永久停止该会话。
- 明显辱骂、黑店、骗子、曝光、报警等：业务上主动转人工并停止。
- 单独的“？？？”不会转人工；只有连续强烈标点叠加当前或近期不满时才升级。
- 普通催促标记为 `frustrated`，仍给出与当前售后问题相关的回复，不机械询问装修阶段。

情绪转人工属于主动补充的真实业务问题，不是公开R4的唯一判定方式。高精度触发减少误停机风险。

## 防重复与弹窗竞态

- 每条客户消息按 `data-mid` 去重。
- 发送前后都重新关闭弹窗并确认当前会话。
- 点击发送后必须在页面中看到新的我方消息，才把客户消息标记为已处理。
- 确认阶段失败时先检查页面是否其实已经发送成功，再决定是否重试。
- 浏览器重连后，根据页面已有我方回复恢复进度，避免重启后回复全部历史消息。

## 本地测试（Windows，无 Docker）

### 首次准备

在仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install fastapi uvicorn websockets playwright==1.62.0
```

本地测试使用电脑已安装的 Microsoft Edge，不需要下载额外 Chromium。

### 双击可视化 Demo

快速自动测试可双击本目录的 `start-demo.cmd`。`scenario-review.json` 是为了覆盖专业咨询、图片售后和情绪升级而补充的扩展测试语料，不是官方消息原文。如果需要逐条阅读扩展语料的回复，运行：

```powershell
.\.venv\Scripts\python.exe submissions/E3/cocoon370/run_local_eval.py `
  --headed `
  --scenario submissions/E3/cocoon370/scenario-review.json `
  --reply-pause 5 `
  --grace 8
```

慢速模式约两分钟，每次回复后会停留五秒。正式容器不会设置这个等待参数，因此不影响回复时效。程序会：

1. 启动官方 mock；
2. 打开一个可见的 Edge；
3. 在约半分钟内推送报价、量房、图片售后、情绪升级和转人工消息；
4. 制造两个弹窗；
5. 用官方 R1-R6 规则输出评分；
6. 自动关闭本次测试进程。

结果在：

- `local-out/result.json`：R1-R6 评分；
- `local-out/decisions.jsonl`：每条消息的意图、主题、情绪、原因和回复；
- `local-out/customer_notes.json`：按客户汇总的意向标签、情绪、状态与人工跟进标记；
- `local-out/events.json`：官方 mock 的完整事件流；
- `local-out/bot.log`：机器人运行日志。

### 命令行运行

```powershell
.\.venv\Scripts\python.exe submissions/E3/cocoon370/run_local_eval.py --headed
```

运行官方公开剧本：

```powershell
.\.venv\Scripts\python.exe submissions/E3/cocoon370/run_local_eval.py `
  --scenario tasks/E3-wechat-autoreply/scenarios/public.json `
  --grace 35 `
  --out submissions/E3/cocoon370/public-out
```

### 单元测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
  -s submissions/E3/cocoon370 `
  -p "test_*.py" `
  -v
```

## Docker运行（GitHub CI 已验证）

```bash
docker build -t my-e3 submissions/E3/cocoon370
uv run tasks/E3-wechat-autoreply/eval/run.py --image my-e3 --mode public
```

Dockerfile 使用与 Python 包完全一致的 Playwright `1.62.0` 版本，避免运行时重新下载浏览器。GitHub CI 已完成构建并跑通公开评测；本机因未安装 Docker Desktop，没有重复执行该步骤。

## 决策日志

每行格式示例：

```json
{"ts":"...","conv_id":"c03","msg_id":"m030","intent":"aftersales","topic":"aftersales","emotion":"frustrated","confidence":0.86,"reason":"命中短语并发现催促","action":"reply","reply":"..."}
```

转人工后收到的新消息记录为 `suppressed_after_handoff`，但不会再次发送内容。

## 没做的事

- 没有接入真实微信客户端；题目只提供仿微信页面。
- 没有调用WebSocket业务协议，全部操作均经过页面UI。
- 没有OCR或图片理解；收到 `[图片]` 时只确认收到图片消息，不编造图片内容。
- 没有接外部LLM；如未来增加，只能作为可选增强，不能替代离线路径。
- 没有接入真实的人工客服工作台或通知系统；当前 `handoff` 会写入 `decisions.jsonl` 并停止该会话的自动回复。生产环境还需要下游系统读取这条记录、通知并分配给真人客服。
- 没有直接修改仿微信页面中的客户备注，因为题目页面没有提供该功能；当前生成独立客户交接表。
- 没有自动承诺报价、折扣、具体换板结果和无法确认的门店地址。
- 没有在本机重复执行 Docker 评测；容器验证依据本 PR 已通过的 GitHub CI。

## 用了哪些 AI 工具

使用 Codex / ChatGPT 协助阅读题目、梳理真实客服风险、实现代码、补充测试和整理文档。关键业务判断由本人逐轮验收，包括不报价、上下文承接、情绪转人工、量房引导和客户交接；面试时可以解释每项取舍与主要代码。
