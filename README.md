# 甲壳虫 —— QQ 群运动数据机器人

一个长期在线的 QQ 群运动搭子，接入 Garmin / 高驰 COROS 运动平台，支持群内查询、排行播报、截图打卡、抽奖玩法，并用智谱 GLM 大模型做数据解读与运动问答。

## 功能特性

- **多平台接入（每用户独立绑定）**：Garmin（佳明）按成员私聊填账号密码；高驰 COROS 走官方 OAuth 私密授权（不顶掉手机 App）；其余无开放接口的 App 靠**截图 OCR** 自动记录。
- **数据查询**：今日 / 周数据 / 月数据，含距离、配速、爬升、时长、消耗、心率、负荷、单次最长等。
- **三榜排行**：今日榜 / 周榜 / 月榜，每晚 23:00 自动播报（周日追加周榜、月末追加月榜）。
- **AI 增强层（智谱 GLM）**：总结 / 鼓励我 / 建议 / @我运动问答 / 打卡点评。**LLM 只做锦上添花**，未配 key 或调用失败一律降级到确定性模板文案，机器人照常可用。
- **群互动玩法**：抽奖、骰子、随机数、帮助菜单。

## 技术栈

NoneBot2 + OneBot v11（NapCat）· SQLAlchemy + SQLite · RapidOCR（本地识别）· 智谱 GLM · Python 3.10+

```
甲壳虫/
├── bot.py                  # 入口：初始化 NoneBot、注册 OneBot v11、加载插件、启动调度
├── pyproject.toml          # 项目元数据 + 依赖 + ruff 配置
├── .env.example            # 配置模板（复制为 .env 后填写）
├── .gitignore
├── 启动机器人.bat           # Windows 一键启动（切 UTF-8 后跑 bot.py）
├── README.md
├── PLAN.md                 # 架构设计与路线图
└── app/
    ├── config.py           # pydantic-settings 配置（读 .env）
    ├── db.py               # SQLAlchemy 引擎 / 会话 / init_db
    ├── scheduler.py        # 周聚合 + 每日排行播报调度
    ├── models/             # ORM：Base / Member / DailyRecord / WeeklyStat / ManualDistance / Group
    ├── services/
    │   ├── providers/      # 平台适配层：base / garmin / coros
    │   ├── sync.py         # 平台拉取 + 截图写入 + 历史回填
    │   ├── ranking.py      # 日/周/月三榜聚合与格式化
    │   ├── summary.py      # 个人周期汇总
    │   ├── aggregator.py   # 日明细 → 周汇总（幂等）
    │   ├── retention.py    # 原始明细保留 45 天后清理
    │   ├── ocr.py          # RapidOCR 本地识别（数据不出本机）
    │   ├── parsers.py      # 坐标关联解析 + 文本兜底
    │   ├── cheers.py       # 鼓励语库 + 配速格式化
    │   ├── llm.py          # 智谱 GLM 增强层（总结/鼓励/建议/问答/点评）
    │   ├── member.py       # get_or_create_member
    │   └── credentials.py  # 每用户凭据 / token 存储
    └── plugins/            # NoneBot 插件：admin / group_tracker / image_ocr /
                            #   interact / lottery / query / ranking / summary
```

## 快速开始（本地）

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e .

# 2. 准备配置
copy .env.example .env          # Linux/macOS: cp .env.example .env
# 编辑 .env，至少把你的 QQ 号填到 SUPERUSERS（运动平台为成员自行绑定，无需填账号密码）

# 3. 启动 NapCat（见下），然后运行机器人
python bot.py                   # 或双击 启动机器人.bat
```

> 首次调用 OCR 时会初始化 RapidOCR 引擎（加载模型），稍慢属正常。

## NapCat 配置（协议端）

NapCat 负责登录 QQ 并通过反向 WebSocket 把消息推给机器人：

1. 下载 [NapCat](https://github.com/NapNeko/NapCatQQ)（Windows 或 Linux 版）。
2. 启动并扫码登录机器人 QQ 号。
3. 配置「网络设置」新增**反向 WebSocket**，地址填：`ws://127.0.0.1:8080/onebot/v11/ws`。
4. 保存后 NapCat 会主动连到机器人，`python bot.py` 收到连接即打通。

## 运动平台接入

> 每个成员**独立绑定自己的账号**（按 QQ 号区分），凭据按 QQ 存于 `data/accounts/`，不在群里暴露。

### Garmin 佳明

- 无第三方 OAuth，只能邮箱密码登录：成员发「绑定 garmin」后，**私聊**机器人发「garmin绑定 邮箱 密码」。
- 中国区账号在 `.env` 设 `GARMIN_IS_CN=true`（非中国区 `false`）。
- 底层用 [`garminconnect`](https://pypi.org/project/garminconnect/) 库。

### 高驰 COROS

- 无需账号密码：成员发「绑定 coros」→ **私聊收到授权链接** → 浏览器完成 OAuth 授权（不顶掉手机 App）→ 回群里发「绑定确认」。
- 通过 COROS 官方 MCP 的 `queryDailyHealthData` 读取全天指标；`.env` 设 `COROS_REGION=cn` 或 `us`。

### 其他平台（小米运动 / Zepp Life 等）

- 无官方个人数据接口，**不做云端接入**：直接发运动截图，机器人 OCR 识别并记入今日数据、累计里程与排行。

## AI 能力（智谱 GLM，可选增强层）

在 `.env` 填 `ZHIPU_API_KEY`（免费模型 `glm-4-flash`，低价 `glm-4-air`）即可启用。设计上 LLM **只进增强层、不进核心数据链路**：

- OCR 识别、数据解析、排行聚合、平台同步全部走确定性代码；
- 只把「聚合后的数字」发给模型，不发送原始消息 / 截图 / 账号密码；
- 未配 key 或调用失败一律降级到模板文案，机器人照常可用。

## 命令列表

| 命令 | 说明 |
|---|---|
| `今日`（`步数` / `今日运动` / `运动`） | 查询今日运动数据（含累计里程） |
| `周数据` / `月数据` | 查看个人周 / 月汇总（次数、跑量、爬升、负荷等） |
| `总结`（可加 `月`） | AI 把本周 / 本月数据说成人话 |
| `鼓励我` | AI 按你的数据来一句鼓励 |
| `建议`（可加 `月`） | AI 按你的周 / 月数据给训练建议 |
| `排行` / `周榜` / `月榜` | 查看今日 / 本周 / 本月运动三榜 |
| `绑定 garmin` / `绑定 coros` | 绑定运动平台（每人独立账号） |
| `garmin绑定 邮箱 密码` | **私聊**中绑定佳明（密码不出现在群里） |
| `绑定确认` | 完成 COROS 授权后确认绑定 |
| `我的绑定` / `解绑` | 查看 / 解除绑定 |
| `机器状态` | 查看各平台接入人数 |
| `同步数据` / `周聚合` | 管理员：同步历史 / 手动周聚合 + 清理 |
| `帮助`（`菜单` / `help`） | 查看命令菜单 |
| `@我 + 问题` | 运动知识问答（配速 / 跑量 / 恢复…） |
| `抽奖 [N] 候选…` / `骰子` / `随机数 [a] [b]` | 抽奖与随机玩法 |
| 📷 发运动截图 | 截图打卡，记录后 AI 自动补一句点评 |

> 命令默认**无需 `/` 前缀**（`COMMAND_START=["", "/"]`，两者兼容）。所有命令在「帮助」菜单里都能查到。

## 定时任务

| 任务 | 时间 | 说明 |
|---|---|---|
| 每日排行播报 | 每天 23:00 | 播今日榜；周日追加周榜、月末追加月榜，分段独立发送 |
| 周聚合 | 每周一 00:10 | 幂等聚合 + 清理 45 天前的原始明细 |

时间均可在 `.env` 用 `BROADCAST_HOUR/BROADCAST_MINUTE`、`WEEKLY_HOUR/WEEKLY_MINUTE` 调整。

## 配置说明（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8080` | 反向 WebSocket 监听地址 |
| `SUPERUSERS` | `["10001"]` | 管理员 QQ 号（JSON 数组） |
| `COMMAND_START` | `["", "/"]` | 命令前缀 |
| `DB_URL` | `sqlite:///data/bot.db` | 数据库连接 |
| `BROADCAST_HOUR/MINUTE` | `23` / `0` | 每日排行播报时间 |
| `BROADCAST_GROUPS` | `[]` | 显式播报群号；留空自动播报到所有出现过的群 |
| `WEEKLY_HOUR/MINUTE` | `0` / `10` | 周聚合触发时间（每周一） |
| `GARMIN_IS_CN` | `true` | 佳明中国区开关 |
| `COROS_REGION` | `cn` | COROS 区域（cn / us） |
| `ZHIPU_API_KEY` | （空） | 智谱 GLM key，留空自动禁用 AI 增强 |
| `ZHIPU_MODEL` | `glm-4-flash` | 智谱模型名 |

## 数据与隐私

- 全部数据存于本机 `data/`（SQLite 数据库、成员凭据、收到的截图），不经过任何第三方服务器。
- 成员账号凭据按 QQ 号隔离存于 `data/accounts/`，且只在私聊中收集，绝不出现在群里。
- OCR 用本地 RapidOCR，图片不离开本机；发给 LLM 的只有聚合后的数字。
- `.env`、`data/` 已加入 `.gitignore`，不会随代码提交到仓库。

## 部署到 Intel 微型电脑

小主机（NUC 类）低功耗适合 7×24 常开。

### 方案 A：Windows（已装 Windows 直接跑）

1. 装 Python 3.10+。
2. 同上「快速开始」装依赖、配 `.env`。
3. NapCat 用 Windows 版。
4. 用 [NSSM](https://nssm.cc/) 把 `python bot.py` 注册为 Windows 服务开机自启：
   ```
   nssm install JkcBot "C:\...\python.exe" "C:\...\bot.py"
   nssm start JkcBot
   ```

### Docker 化（规划中）

当前仓库尚未提供 `Dockerfile`，容器化部署见 [PLAN.md](./PLAN.md) 路线图。Linux 下可直接 `python bot.py` + NapCat Linux 版运行。

## 常见问题

- **查询报「尚未绑定 / 授权」**：该成员还没绑定，发「绑定 garmin」或「绑定 coros」走流程；其他平台直接发截图。
- **绑定后查不到数据**：确认运动 App 已把当天数据同步到云端；Garmin 中国区账号需 `GARMIN_IS_CN=true`。
- **发截图没反应**：表情包 / 缩略图（最长边 < 500px）会被自动跳过；确认发的是运动记录截图。
- **`今日` 报错**：看终端日志，多半是平台 API 返回结构变化，需微调 `app/services/providers/` 下的字段映射。
- **AI 功能没生效**：检查 `.env` 的 `ZHIPU_API_KEY` 是否填写；未填时自动降级为模板文案，属正常行为。
