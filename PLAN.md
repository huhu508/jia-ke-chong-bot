# 甲壳虫 —— 架构设计与路线图

本文档描述「甲壳虫」QQ 群运动机器人的整体设计、分层结构、核心取舍与后续计划。面向后续维护者与协作者。

## 1. 项目定位

一个长期在线的 QQ 群运动搭子：把多个运动平台的数据（Garmin / 高驰 COROS / 截图 OCR）汇聚到群里，提供个人查询、三榜排行与定时播报，并用大模型做「说人话」的解读。

核心价值不是「做一个数据面板」，而是**降低群成员记录与展示运动数据的摩擦**：

- 已接入平台的成员：绑定一次，之后「今日 / 周数据 / 总结」随时查；
- 未接入平台的成员：发一张运动截图即可自动记录、进入排行；
- 全部交互用一句命令 / 一张图完成，不用跳转 App。

## 2. 技术选型

| 环节 | 选型 | 理由 |
|---|---|---|
| 机器人框架 | NoneBot2 + OneBot v11 适配器 | 生态成熟、插件化、命令/消息双模式 |
| 协议端 | NapCat（反向 WebSocket） | 扫码登录、无需协议密码，稳定接入 QQ |
| 数据存储 | SQLAlchemy 2.0 + SQLite | 单文件、零运维、够用；`Session` 按需创建 |
| OCR | RapidOCR（rapidocr-onnxruntime） | **本地**识别，截图数据不出本机 |
| 平台 SDK | garminconnect / COROS 官方 MCP | 佳明仅邮箱登录；COROS 走 OAuth 授权码 + PKCE |
| 大模型 | 智谱 GLM（OpenAI 兼容） | 国产、免费档 `glm-4-flash`、接口稳定 |
| 配置 | pydantic-settings | 类型安全读 `.env` |
| 代码质量 | ruff（format + lint） | 单工具覆盖格式化 / 静态检查 / 导入排序 |

## 3. 架构分层

```
┌─────────────────────────────────────────────────────┐
│ 协议层   NoneBot2 + OneBot v11 适配器（NapCat 反向 WS）│
├─────────────────────────────────────────────────────┤
│ 插件层   app/plugins（8 个：命令处理器 + 消息处理器）  │
│          admin / group_tracker / image_ocr /         │
│          interact / lottery / query / ranking /      │
│          summary                                     │
├─────────────────────────────────────────────────────┤
│ 服务层   app/services（业务逻辑，与 NoneBot 解耦）     │
│          sync · ranking · summary · aggregator ·     │
│          retention · ocr · parsers · cheers · llm ·  │
│          member · credentials · providers/           │
├─────────────────────────────────────────────────────┤
│ 数据层   app/models（SQLAlchemy ORM）+ SQLite         │
└─────────────────────────────────────────────────────┘
```

**分层原则**：插件层只做「收发消息 + 编排」，可复用的业务逻辑全部下沉到服务层；服务层不依赖 `Bot`/`Event`，便于单独测试与复用。

## 4. 核心设计原则

### 4.1 大模型「生态位」：只做增强层

LLM 被严格限制在**核心数据链路之外**：

- **确定性链路**（不依赖 LLM）：OCR 识别 → 字段解析 → 数据落库 → 排行聚合 → 平台同步。
- **增强层**（LLM）：总结 / 鼓励 / 建议 / 打卡点评 / @问答，失败一律返回 `None`，调用方降级到模板文案。

这样即使没有 key、断网、模型改版，机器人的查询 / 打卡 / 排行功能始终可用。

### 4.2 隐私最小化

- 发给 LLM 的**只有聚合后的数字**，绝不发送原始消息、截图、账号密码。
- 成员账号凭据按 QQ 号隔离存于 `data/accounts/`，只在私聊中收集。
- OCR 用本地模型，图片不离开本机。

### 4.3 每用户独立绑定

运动平台凭据是「一人一账号」，按 QQ 号作为主键区分。绑定后查询固定返回该成员自己的平台数据，互不串扰。

### 4.4 稳定性优先

针对实测踩过的坑做了显式防护：

- **跨线程 Session**：SQLAlchemy 的 `Session` 非线程安全，`asyncio.to_thread` 里不共享同一 Session；SQLite 写入极快，直接在当前协程同步执行。
- **截图去重**：NapCat 偶发发送超时会诱导用户重发同一张图，用「QQ + 图片 MD5」10 分钟窗口去重，避免重复累计里程。
- **解析兜底**：OCR 与解析各包 try/except，识别不出数据时静默跳过，不拖垮整条 handler。
- **发送超时误报**：`finish()` 抛出的 `ActionFailed` / `FinishedException` 单独 re-raise，不误报为「业务失败」。

## 5. 数据模型

| 模型 | 主键 / 约束 | 用途 |
|---|---|---|
| `Member` | `qq`（PK） | 成员：绑定平台、平台账号、QQ 昵称 |
| `DailyRecord` | `(member_qq, record_date, platform)` 唯一 | 每日运动明细（供排行 / 汇总） |
| `WeeklyStat` | `(member_qq, week_start, platform)` 唯一 | 周汇总（幂等写入，历史产物） |
| `ManualDistance` | `member_qq`（PK） | 截图成员的累计里程 |
| `Group` | `group_id`（PK） | 机器人出现过的群（播报定位目标） |

- 原始明细 `daily_record` 保留 **45 天**（`retention.RETENTION_DAYS`），覆盖最长 31 天的月榜 + 缓冲。
- 周榜 / 月榜**直接由明细聚合**（`compute_range_rankings`），不再依赖 `weekly_stat`。

## 6. 关键数据流

### 6.1 绑定（Garmin / COROS）

```
成员发「绑定 garmin」→ 私聊填邮箱密码 → 校验登录 → 存 credentials → 后台回填 31 天历史
成员发「绑定 coros」→ 私聊收授权链接 → 浏览器 OAuth → 「绑定确认」→ 存 token → 后台回填历史
```

### 6.2 查询「今日」

```
已绑定：sync_daily(member, today) → provider.fetch_daily → upsert daily_record → 格式化
未绑定：读当日截图明细 + ManualDistance 累计里程 → 格式化（含引导文案）
```

### 6.3 截图打卡（图片 → 数据存储）

```
下载图片 → 尺寸过滤（表情包拦截）→ MD5 去重 → 保存原始图 →
RapidOCR 识别 → parse_activity_from_boxes（坐标关联）→ parse_activity（文本兜底）→
写入 daily_record + ManualDistance → LLM 打卡点评（失败降级）
```

### 6.4 排行与播报

```
compute_range_rankings(start, end) → 按距离 / 爬升 / 单次距离聚合三榜 →
每日 23:00 播今日榜；周日追加周榜；月末追加月榜（分段独立发送）
```

### 6.5 调度

两个常驻 asyncio 循环（`scheduler.py`）：

- `_weekly_loop`：每周一 00:10 幂等聚合 + 清理 45 天前明细；
- `_daily_broadcast_loop`：每天 23:00 计算并播报排行。

## 7. 安全与隐私约束

> 以下为项目硬性约束，任何改动都必须遵守：

- 不得读取 / 暴露 `.env`（文档只以 `.env.example` 为模板）。
- 不得用移动 App 的 API 密码方式登录 COROS（一律走官方 OAuth 授权码 + PKCE）。
- 成员账号密码绝不出现在群里：Garmin 必须私聊绑定，COROS 必须私聊授权链接。
- 不打印 / 暴露 `passkey.json` 与 NapCat WebUI token。
- `.env`、`data/`、缓存目录已加入 `.gitignore`，不入库。

## 8. 当前状态（已完成）

- ✅ Garmin / COROS 双平台接入，每用户独立绑定（华为 / Suunto 已彻底移除）。
- ✅ 截图 OCR 打卡（含去重、尺寸过滤、坐标解析、文本兜底）。
- ✅ 今日 / 周数据 / 月数据 / 总结 / 鼓励我 / 建议 查询。
- ✅ 今日榜 / 周榜 / 月榜 + 定时播报（周日 / 月末自动追加）。
- ✅ 智谱 GLM 增强层（总结 / 鼓励 / 建议 / 问答 / 打卡点评，失败降级模板）。
- ✅ 抽奖 / 骰子 / 随机数 互动玩法。
- ✅ 代码规范化：ruff 格式化 + 静态检查全过，`pyproject.toml` 依赖补全。

## 9. 路线图（规划中）

| 优先级 | 事项 | 说明 |
|---|---|---|
| 高 | Docker 化 | 补 `Dockerfile` + `docker-compose.yml`，降低部署门槛 |
| 高 | 单元测试 | `parsers` / `ranking` / `retention` / `summary` 等纯函数先覆盖 |
| 中 | CI 流水线 | GitHub Actions：`ruff check` + `compileall` + 测试 |
| 中 | 训练负荷进阶 | 基于 `training_load` 做趋势 / 恢复建议（注意不编造医学结论） |
| 低 | 更多平台 | 有官方开放接口时再评估（如 Keep、悦跑圈等） |
| 低 | LICENSE 选择 | 开源前确定许可协议 |

## 10. 历史决策记录

- **华为 / Suunto 移除**：无稳定开放接口、维护成本高，改为「截图 OCR」统一兜底。
- **截图成员也进排行**：未绑定成员的发图记录写入 `daily_record`（`platform="manual"`），与平台成员同榜竞技。
- **`weekly_stat` 保留但不再用于排行**：改为「明细保留 45 天 + 直接聚合」，避免周清理误删月榜数据；`aggregate_week` 幂等写入保留，无害。
- **OCR 保持默认模型**：PP-OCRv5 中文精确匹配率反而低于默认的 PP-OCRv4 mobile，不做无收益替换；优化重心放在「按坐标关联标签与数值」。
