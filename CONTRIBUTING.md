# 提交规范

## 目录

```
submissions/<题号>/<github-login>/
├── Dockerfile        # 必须。构建上下文就是这个目录
├── README.md         # 必须。见下面的模板
└── …你的代码
```

- `<题号>` ∈ `E1` `E2` `E3`
- `<github-login>` 是你的 GitHub 用户名，**全小写**，必须与 PR 作者一致（CI 会校验）
- 一个 PR 只能包含这一个目录下的改动

## Dockerfile 约定

- CI 在 Linux（x86_64）上 `docker build`，构建时间上限 10 分钟，请不要在构建里下载大模型
- 运行时 `--network host`，你的服务和 mock 都在 `127.0.0.1`
- 各题的入口约定（`index`/`search` 子命令、监听端口、环境变量）见各题 README，以那里为准
- 镜像里不要放任何真实凭据

## README.md 模板

```markdown
# <题号> · <你的名字或用户名>

## 怎么跑
（一条命令能跑起来；如果需要额外步骤，写清楚）

## 设计
（你怎么拆的、为什么这么拆，200 字以内）

## 遇到的问题与取舍
（题目没明说但你发现的事、你怎么处理的、为什么；处理不了的也写）

## 没做的事
（知道该做但没做的，以及原因）

## 用了哪些 AI 工具
（用了就写，不扣分）
```

## 分支与 PR

- 分支名 `task/<题号>-<github-login>`
- PR 标题 `[<题号>] <github-login>`
- 按 PR 模板填写；模板里的每一项都会被看
- commit 请拆成有意义的粒度，别一个 commit 提交全部；也别 50 个 `fix`

## 本地评测

```bash
# 需要 docker 与 uv（https://docs.astral.sh/uv/）
./scripts/eval-local.sh E1 <github-login>
```

评测脚本最后一行是一个 JSON，`score` 是公开集得分，`hard_violations` 非空说明踩了硬规则。隐藏集只在我们评论 `/eval` 之后跑，会把汇总分数评论到 PR 上。

## 提问

开 issue，选「提问」模板。请把你已经试过的写进去。
