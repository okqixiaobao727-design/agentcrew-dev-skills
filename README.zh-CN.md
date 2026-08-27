# AgentCrew

[English](README.md) · **简体中文**

**两个技能，把一份 spec 变成可以直接 review 的分支：`/route` 对每张 ticket 分类切分，分派到你手上的
Claude 与 Codex 订阅；`/crew` 把它们作为无人值守的 tmux 子 agent 波次跑完。**

```text
mattpocock-skills   grilling → to-spec → to-tickets → implement    一次一张 ticket
AgentCrew           grilling → to-spec →   route    →   crew       整条 frontier 并行
```

AgentCrew 是 [mattpocock-skills](https://github.com/mattpocock/skills) 的聚合式增强：`grilling` 与
`to-spec` 原样保留，被替换的是流水线的后半段。`/route` 以路由 overlay 的方式调用
`/mattpocock-skills:to-tickets`，让 ticket 粒度和路由粒度在同一次确认里定下来；`/crew` 负责运行这些
已路由的 ticket。**Matt-first 原则**统辖所有集成问题——overlay 只做加法；当 AgentCrew 的需要与 Matt 的
技能行为冲突时，以 Matt 的经验为准。

![AgentCrew 跑通一个真实 feature：/route 的路由表、wave 表、tmux 里的 Claude 与 Codex 子 agent，
以及收尾的耗时表](docs/media/agentcrew-demo.gif)

> **一次真实运行，剪成 30 秒。** `/route` 给出五张 ticket 的切分方案并接受一次修订；wave 表里每个厂商
> 各跑一个子 agent、彼此交叉 review；wave 2 运行时两个 monitor 同时待命；最后是运行报告——五张 ticket
> 全部完成在 `crew/textkit` 上，三个波次共 30 分 17 秒。AgentCrew 由 AgentCrew 自己跑出来：这次运行就是
> 本次发布的验收。

晚上把 spec 交给它，早上拿到一条待 review 的集成分支和一份决策日志。除了销毁数据这一条红线，其余的事
它都会自己推进下去。

## 两个技能

| 技能 | 输入 | 产出 |
| --- | --- | --- |
| `/route <feature-dir>` | 一份 spec，有没有 ticket 都可以 | 每张 ticket 带上 `## Routing` 段落——workflow、executor、model、effort，以及需要 review 的 workflow 上的 review lane |
| `/crew <feature-dir>` | 已路由的 ticket | 集成分支 `crew/<slug>`、带逐 ticket 耗时表的 `report.md`，以及记录整场运行的 machine log |

`/route` 有两种模式，由 feature 目录里已有的东西决定。没有 ticket 的 spec 会在一趟里切分并路由；已经
有 ticket 的 feature 只做路由、不切分，所以进行中的工作也能接入 AgentCrew。路由是**建议**：一张表、
一个确认点，在你批准之前什么都不写。

`/crew` 给每张 ticket 一个独立的 git worktree 和一个独立的 tmux window，所以无人值守不等于看不见——你
随时可以切进任何一个子 agent 接手。子 agent 通过跨会话消息通道上报，完成时给出完整 40 位 commit sha，
协调者会独立校验后才合并。整个运行过程中 base 分支不被触碰，最后一次合并由你来做。

## 环境要求

- **Claude Code**，并已安装 [mattpocock-skills](https://github.com/mattpocock/skills) 插件——`/route`
  会调用 `/to-tickets`；当你的仓库还没有 issue tracker 约定文档时，安装向导会让你先去跑
  `/setup-matt-pocock-skills`。
- **tmux**——子 agent 以协调者所在会话的 window 形式运行。
- **Python 3.11+**——配置校验脚本与 Codex bridge 需要。
- 若要跑 Codex ticket：**Codex CLI**，以及为 Claude Code 所用的 Python 解释器安装的 `aiohttp` 包。
- 若要跑带 review 的 ticket——即所有 `tdd` 与 `refactor` ticket：
  **[Review-Switch](https://github.com/okqixiaobao727-design/review-switch)**，且要装到它的
  `review-bridge` 命令在你的 `PATH` 上。AgentCrew 自己不带任何 review 实现，而是跨进程调用这条命令
  （`docs/adr/0020-review-switch-owns-the-review-agentcrew-owns-the-reviewer.md`），所以 wave 表里
  只要有 review lane，命令没装好这一趟 run 就会停在 preflight。

每个使用 AgentCrew 的仓库还需要 `docs/agents/issue-tracker.md`：两个技能都从它读取 ticket 存放在哪里、
状态写回到哪里，二者都没有兜底默认值。

项目还可以把 base gate（集成基线门禁，也就是开工前必须通过的整套检查）配置成 argv 参数列表，例如
`[preflight] gate = ["python3", "scripts/test.py"]`。AgentCrew 不经过 shell，切换并快进更新 base 后、创建
集成分支前，从仓库根目录运行它。未配置时 run 会继续，machine log 与最终报告都会明确写
`base gate: none configured`；跳过门禁绝不会被写成通过。

## 安装

```text
/plugin marketplace add okqixiaobao727-design/agentcrew-dev-skills
/plugin install agentcrew-dev-skills@agentcrew-dev-skills
```

想从本地检出运行——为了阅读或修改技能——克隆仓库后把克隆目录当作 marketplace 添加：

```bash
git clone https://github.com/okqixiaobao727-design/agentcrew-dev-skills.git
```

```text
/plugin marketplace add ./agentcrew-dev-skills
/plugin install agentcrew-dev-skills@agentcrew-dev-skills
```

## 第一次运行

1. **在你的项目里跑 `/route <feature-dir>`。** 仓库根目录没有 `agentcrew.toml` 时，会先进入安装向导：
   它先落实仓库的 issue tracker 约定文档，再把随插件发布的默认配置连同注释一起复制成仓库根目录的
   `agentcrew.toml`。任何时候都可以再叫一次向导来重新配置。
2. **读 `/route` 打印的那张表。** 一行一张 ticket——workflow、executor、model、effort、review lane，
   以及决定每一项的判据；表头写明这些取值来自哪个配置文件。改到对为止，批准之后它才落笔。
3. **跑 `/crew <feature-dir>`。** 它按 ticket 当前携带的路由重建波次表，确认一次，然后开跑：每张
   ticket 一个 worktree 和一个 tmux window，波次从依赖 frontier 切出，本波落地的分支全部合入
   `crew/<slug>` 之后才切下一波。
4. **review 集成分支与 `report.md`**，然后由你自己合并。

手工改过配置文件后可以这样校验：

```bash
python3 scripts/validate_plugin_tree.py --config agentcrew.toml
```

它每发现一个问题打印一行，并以非零码退出。文件里没写到的 case 不算问题：随插件发布的默认值回答了每一个
case，所以项目配置只需要携带它要覆盖的那些格子。

## 配置参考

可配置面位于仓库根目录的 `agentcrew.toml`。分类逻辑——六种 workflow、core 与 non-core、complex 与
routine——是固定的产品判断：你配置的是结论，不是决策过程。随插件发布的带注释默认值在
[`config/agentcrew.default.toml`](config/agentcrew.default.toml)。

每个格子都由同样的三个字段构成：

| 字段 | 取值 |
| --- | --- |
| `executor` | `claude` 或 `codex`——运行这张 ticket 的厂商 |
| `model` | 原样传给该厂商的启动命令，所以 Codex 模型要写完整 slug |
| `effort` | 同样原样传递——该厂商启动时的推理强度 |

### implementer 表——谁来写这张 ticket 的代码

| 格子 | 对应的 case | 默认值 |
| --- | --- | --- |
| `implementer.tdd-refactor.core-complex` | 下游耦合其设计决策，且跨模块或由执行者自行决定实现路线 | `claude` / `claude-opus-5` / `medium` |
| `implementer.tdd-refactor.core-routine` | 同样的耦合，但范围收敛、实现路线已定 | `claude` / `claude-opus-5` / `medium` |
| `implementer.tdd-refactor.non-core-complex` | 没有下游耦合其设计决策，但活儿本身复杂 | `claude` / `claude-opus-5` / `medium` |
| `implementer.tdd-refactor.non-core-routine` | 范围收敛、路线已定，且下游不依赖它怎么实现 | `codex` / `gpt-5.6-luna` / `max` |
| `implementer.direct.any` | 文档、技能文案、配置——不分难度 | `claude` / `claude-opus-5` / `medium` |
| `implementer.spike.directed-collection` | 问题能事先枚举成填空、每个空都指明验证方式、交付物不含推荐结论 | `codex` / `gpt-5.6-luna` / `max` |
| `implementer.spike.open-exploration` | 上述三条缺任意一条 | `claude` / `claude-opus-5` / `medium` |
| `implementer.ops.mechanical` | 对环境执行动作并记录结果 | `codex` / `gpt-5.6-luna` / `max` |
| `implementer.ops.acceptance-judgement` | 同样是执行，但以对结果的判断收尾 | `claude` / `claude-opus-5` / `medium` |
| `implementer.acceptance.any` | 收尾需要人，agent 的活儿是准备好并交接 | `claude` / `claude-opus-5` / `medium` |

三个默认值相同的 `tdd-refactor` 格子仍然分开列出，这样其中任何一个都能被单独改掉。

### reviewer 表——谁来 review

只有 `tdd` 和 `refactor` 的 diff 里 review 才可能抓到东西，所以只有它们带 reviewer。选出 implementer
的那个象限同时选出 reviewer，并且 review 的厂商永远是没有参与实现的那一家。

| 格子 | 默认值 |
| --- | --- |
| `reviewer.core-complex` | `codex` / `gpt-5.6-sol` / `medium` |
| `reviewer.core-routine` | `codex` / `gpt-5.6-luna` / `max` |
| `reviewer.non-core-complex` | `codex` / `gpt-5.6-luna` / `max` |
| `reviewer.non-core-routine` | `claude` / `claude-opus-5` / `medium` |

### `[hooks.on-child-launch]`——唯一的扩展点

两个字段默认都为空；两者都为空意味着子 agent 的启动方式与完全没有 hook 时一模一样。

| 字段 | 取值 |
| --- | --- |
| `command` | 每个子 agent 启动时在其工作目录里执行一次的 shell 命令——把它接到你已经在跑的通知或会话追踪系统上 |
| `env` | 一张字符串环境变量表，加到每个子 agent 以及 hook 命令的环境里 |

该命令还会收到两个标识本次启动子 agent 的变量：

| 变量 | 取值 |
| --- | --- |
| `AGENTCREW_CHILD_CWD` | 子 agent 的工作目录 |
| `AGENTCREW_CHILD_TMUX_TARGET` | 子 agent 所在的 tmux window 或 pane；没有的话为空 |

这个命令是对你既有工具的顺手照顾：它失败或卡住都不影响启动本身，运行过程会记录它打印了什么。

```toml
[hooks.on-child-launch]
command = "notify-send 'AgentCrew child launched' \"$AGENTCREW_CHILD_CWD\""

[hooks.on-child-launch.env]
MY_PROJECT_MODE = "unattended"
```

### `[dashboard]`——一次 run 把自己画在哪个面上

| `surface` | 这次 run 的行为 |
| --- | --- |
| `window` | 和一直以来一样，给这次 run 开它自己的 tmux window——默认值 |
| `pin` | 不开那个 window，把同一帧画进 coordinator 的 Claude Code statusline，run 结束后没有任何东西要关 |
| `both` | 两个都跑，通过这次 run 唯一的 toast state 去重 |

```toml
[dashboard]
surface = "window"
```

pin 是什么、怎么接进 Claude Code，见
[`docs/monitor-dashboard.md`](docs/monitor-dashboard.md)。

### `[preflight]`——可选的集成基线门禁

`gate` 是非空 argv 参数列表，不是一整段 shell 命令字符串。Driver 先完成便宜的只读 preflight 检查，
再切换并快进更新 base，然后从仓库根目录运行门禁，紧接着才创建集成分支。退出码 0 表示通过；其他
退出码会让新 run 以 `preflight-failed` 停止，恢复操作员开始时所在的分支或 detached commit（游离
提交，即当前不在任何分支上），且不创建集成分支或子 worktree。preflight notice 会显示命令、退出码和
输出末尾。AgentCrew 不另设超时，也不解析测试工具的输出；这些规则都由所配置的命令自己负责。

和 run section（运行配置快照）里的其他值一样，gate argv 只在 start 时从项目配置读取一次；快进更新
base 后不会重新读取任何 run 配置。machine log 会记录实际执行的那份 argv。

```toml
[preflight]
gate = ["python3", "scripts/test.py"]
```

这个 key 可省略，发布的默认配置不会主动开启它。没有配置的新 run 会记录
`base gate: none configured`；接管已有 run 时不会再次执行门禁。

## Tracker 支持

两个技能都读仓库的 `docs/agents/issue-tracker.md`，不写死任何 tracker。

| Tracker | 状态 |
| --- | --- |
| GitHub Issues | 支持。ticket 即 issue；`/crew` 关闭每张完成的 issue，并清掉标记它可被领取的 label |
| 本地 markdown 文件 | 支持。`/route` 只写 ticket 文件，`/crew` 翻转每张 ticket 的 `Status:` 行而不是关闭 issue——不需要远端 |
| GitLab、Jira、Linear 及其他 | **未经测试。** 两个技能会照你的约定文档去做，但本版本从未真正对它们跑过。支持声明到此为止 |

## 检查插件树

`python3 scripts/validate_plugin_tree.py` 会校验一棵插件树——manifest、技能位、配置默认值、
自引用路径——并对其做 residue lint，挡住不该出现在公开仓库里的残留：私有 bridge 路径、
私有环境变量、花费金额，以及这个技能已废弃的旧名字。

这条 lint 也会拒绝个人标识符，而它猜不出这些是什么：你的机器昵称、你的账号名。把你自己的
标识符逐行写进插件树根目录的 `.agentcrew-local-identifiers`——该文件已被 gitignore，所以你的
列表只属于你——或者把 `AGENTCREW_LOCAL_IDENTIFIERS` 设成逗号分隔的列表，一次覆盖所有 checkout，
并且优先级高于文件。一个都不配置时，只有这一条规则失效，其余四条照旧运行。

## 路线图

- **Codex bridge**——`codex_bridge.py` 目前是原样内置的，重写已在计划中。
- **mattpocock-skills 依赖**——现在是必需的，之后会变成可选。

## 文档

- [`docs/design.md`](docs/design.md)——架构、红线，以及被否决的备选方案。
- [`docs/glossary.md`](docs/glossary.md)——两个技能所讲的术语。
- [`docs/dogfooding-run.md`](docs/dogfooding-run.md)——上面那段演示对应的真实运行：做了什么、发现了
  什么、还留下了什么。
- [`docs/cost-baseline.md`](docs/cost-baseline.md)——ADR-0001 所依据的那次实测运行：协调者的钱到底
  花在哪，以及以后的运行拿什么来对照。
- [`config/agentcrew.default.toml`](config/agentcrew.default.toml)——随插件发布的带注释默认配置。

## 许可证

MIT，见 [LICENSE](LICENSE)。
