# CLAUDE.md

本文件是 Claude Code 在本项目中的上下文文档，记录 CLI 环境配置、故障排查结论与维护操作日志。

---

## 1. 环境概览

| 项目 | 值 |
| --- | --- |
| 安装方式 | npm 全局安装 |
| 安装路径 | `C:\Users\508\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code` |
| CLI 入口 | `C:\Users\508\AppData\Roaming\npm\claude.cmd` / `claude.ps1` |
| 当前版本 | 2.1.300 |
| 模型接入 | DeepSeek 的 Anthropic 兼容端点 |
| 配置文件 | `C:\Users\508\.claude\settings.json` |
| Node / npm | v24.20.0 / 11.19.0 |

> `ANTHROPIC_AUTH_TOKEN` 存放在上述配置文件中，**不要**写入本文件或提交到版本库。

---

## 2. 当前生效配置

`~/.claude/settings.json` 的 `env` 段：

| 变量 | 值 | 作用 |
| --- | --- | --- |
| `ANTHROPIC_BASE_URL` | `https://api.deepseek.com/anthropic` | 指向 DeepSeek 兼容端点 |
| `ANTHROPIC_MODEL` | `deepseek-v4-pro` | 会话默认模型 |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `deepseek-v4-pro` | opus 槽位 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `deepseek-v4-pro` | sonnet 槽位 |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `deepseek-flash` | haiku 槽位 / 后台任务 |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `1000000` | 声明真实上下文长度，消除 200k 误判 |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `786432` | 自动压缩阈值（768K），为最大输出预留余量 |
| `CLAUDE_CODE_SUBAGENT_MODEL` | `deepseek-flash` | 子代理默认模型 |
| `CLAUDE_CODE_EFFORT_LEVEL` | `max` | 思考强度 |

**DeepSeek 侧模型事实**（来源：DeepSeek 官方 Models & Pricing / Claude Code 集成指南）：

- `deepseek-v4-pro` = DeepSeek-V4-Pro-0813，上下文 **1M**，最大输出 384K
- `deepseek-flash` = DeepSeek-V4.1-Flash，上下文 **1M**，最大输出 384K
- `deepseek-v4-flash` 是**已退役的旧名**，官方仍接受，但请求实际由 V4.1-Flash 承接
- 传给 API 的模型名若无法识别，DeepSeek 会**自动映射到 `deepseek-flash`**

---

## 3. 故障排查日志

### 3.1 —— `claude` 命令无法启动

**现象**

命令行执行 `claude` 直接失败：

```
This version of ...\bin\claude.exe is not compatible with the version
of Windows you're running. Check your computer's system information
and then contact the software publisher.
```

**根因**

`bin/claude.exe` 并不是可执行文件，而是一个 **500 字节的 Unix shell 占位脚本**（内容为 `echo "Error: claude native binary not installed."`），因此 Windows 拒绝将其作为程序执行。

之所以停留在占位脚本，是因为 2026-10-09 升级到 2.1.286 时平台原生包安装失败，npm 日志 `2026-10-09T13_45_24_059Z-debug-0.log` 给出了完整链条：

| 日志行 | 含义 |
| --- | --- |
| `attempt 1 failed with ECONNRESET` | 安装期间网络中断 |
| `cache hit` 后紧跟 `no local data for ...win32-x64...` | npm 缓存只有索引、tarball 实体丢失，需重新下载 |
| `reify failed optional dependency ... mark deleted` | 关键的原生包 `@anthropic-ai/claude-code-win32-x64@2.1.286` 被静默丢弃 |
| `postinstall { code: 0 }` | postinstall 退出码为 0，**掩盖了失败** |

最后一条是本次故障最隐蔽之处：`install.cjs` 找不到原生包时走「打印提示后 `return`」分支，退出码仍为 0，npm 因此判定安装成功。

**处置**

1. 将损坏的占位文件备份为 `claude.exe.stub-backup`
2. 从 npm 因 `EPERM` 未能清理干净的退休目录 `.claude-code-O4x1zFQZ` 中，取出仍然完好的 2.1.285 原生二进制（已核验为合法 x64 PE，`machine=0x8664`），恢复到 `bin/claude.exe`，使命令立即可用
3. `npm cache verify` —— 清掉 64 个残缺缓存条目（45 MB）
4. 重装至最新版对齐版本：

   ```powershell
   npm install -g @anthropic-ai/claude-code@latest --allow-scripts=@anthropic-ai/claude-code
   ```

**验证**

| 检查项 | 结果 |
| --- | --- |
| 包装包版本 | 2.1.300 |
| `node_modules\@anthropic-ai\claude-code-win32-x64` | 已就位 |
| `bin/claude.exe` | 270,863,520 字节，合法 x64 PE |
| `claude --version`（cmd） | `2.1.300 (Claude Code)`，exit 0 |
| `claude --version`（PowerShell shim） | `2.1.300 (Claude Code)`，exit 0 |

---

### 3.2 —— 模型未识别告警：`Model "deepseek-v4-pro" isn't recognized`

**现象**

会话中提示：

```
Claude Code doesn't recognize the model "deepseek-v4-pro", so it assumes
a context window of 200k tokens. Update Claude Code, or see
https://code.claude.com/docs/en/model-config#correct-the-window-for-a-gateway-or-custom-model-id
```

**根因**

`settings.json` 用**原生 DeepSeek 模型名**覆盖了模型槽位。Claude Code 内置模型注册表只认识 Anthropic 的模型 ID 与别名（`opus`、`sonnet`、`claude-opus-5-5`，以及 `[1m]` 这类标记）；`deepseek-v4-pro` 不以 `claude-` 开头，被判定为 provider/custom spelling，查不到上下文长度，于是回退到 200k。

**这不是致命错误**——请求可正常完成，但代价真实：DeepSeek 两个模型实际上下文均为 **1M**，按 200k 估算会导致自动压缩提前约 5 倍触发，最多浪费 80% 可用上下文，并因反复压缩重发上下文而增加输入 token 消耗。

**处置**

按官方文档「Correct the window for a gateway or custom model ID」的建议，改用 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 声明窗口，并同步 DeepSeek 官方集成指南的推荐项。

**为何不用 `[1m]` 后缀**

官方集成指南使用 `deepseek-flash[1m]` 写法，实测也能消除告警。但该写法依赖 Claude Code 在发请求前剥离 `[1m]` 后缀，而这一点**未能验证**（`modelUsage` 只是 Claude Code 自身的记账键，DeepSeek 响应又会原样回显请求中的模型名，两端都无法证明实际转发内容）。若后缀被原样发出，按 DeepSeek 规则会被映射到 `deepseek-flash`，造成**静默降级**。`CLAUDE_CODE_MAX_CONTEXT_TOKENS` 不改变发给 API 的模型名，无此风险，因此采用后者。

**验证**

| 试验 | 模型写法 | 用户可见告警 |
| --- | --- | --- |
| A | `deepseek-v4-pro` | 出现完整告警 |
| B | `deepseek-v4-pro[1m]` | 消失 |
| C | `deepseek-v4-pro` + `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` | 消失 |
| D | C 再加 `CLAUDE_CODE_AUTO_COMPACT_WINDOW=786432` | 消失 |

改动后复测：stderr 仅剩机器可读的遥测标记 `[claude-code:unrecognized_model]`（无害），用户可见告警消失；`modelUsage` 显示实际服务模型仍为 `deepseek-v4-pro`，**未被降级**。

---

## 4. 维护注意事项

1. **重装必须带 `--allow-scripts`**：npm 11.19 会对未显式授权的安装脚本告警，而 Claude Code 完全依赖 `postinstall` 铺放原生二进制。不带此参数会重现 3.1 的故障。

   ```powershell
   npm install -g @anthropic-ai/claude-code@latest --allow-scripts=@anthropic-ai/claude-code
   ```

2. **不要轻信 npm 的退出码**：`install.cjs` 在失败分支返回 0。安装后务必实际执行 `claude --version` 确认，而不是只看 npm 报告成功。

3. **善用 `claude doctor`**：可快速查看安装方式、版本、更新渠道与端点状态。

4. **模型名优先用当前名**：使用 `deepseek-flash`，不要再用退役名 `deepseek-v4-flash`。

5. **成本观察（未执行，供决策）**：目前 `ANTHROPIC_MODEL` / OPUS / SONNET 三个槽位都指向 `deepseek-v4-pro`，即所有请求均走最贵档（cache-miss 输入 $0.66/M、输出 $1.98/M）。DeepSeek 官方指南对这三个槽位推荐的是 `deepseek-flash`。如按量敏感，可把 SONNET 槽位降为 `deepseek-flash`。

---

## 5. 回滚方式

Claude Code 配置文件备份：

```
C:\Users\508\.claude\settings.json.bak-20261015-103932
```

原生二进制备份（2.1.285，243,751,072 字节）：

```
C:\Users\508\.claude-code-backup\claude-2.1.285.exe
```

如需回滚模型配置，用备份覆盖 `settings.json` 后重启 Claude Code 即可。

---

## 6. 操作日志（2026-10-15）

| # | 操作 | 结果 |
| --- | --- | --- |
| 1 | 定位 `claude` 命令失效原因，发现 `bin/claude.exe` 为 500 字节占位脚本 | 确认根因 |
| 2 | 从退休目录恢复 2.1.285 原生二进制，备份原占位文件 | `claude --version` 恢复可用 |
| 3 | 校验 npm 缓存 `npm cache verify` | 清除 64 个残缺条目（45 MB） |
| 4 | 重装最新版 `@anthropic-ai/claude-code@latest`（带 `--allow-scripts`） | 升级至 2.1.300，平台原生包就位 |
| 5 | 核验二进制与新旧 shell 启动 | cmd / PowerShell 均 exit 0 |
| 6 | 排查模型未识别告警，定位到 `settings.json` 的模型名覆盖 | 确认根因 |
| 7 | 查阅 DeepSeek 官方文档与 Claude Code 官方 model-config 文档 | 取得权威依据 |
| 8 | 四组对照试验（A/B/C/D）确定改法 | 选定 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 路线 |
| 9 | 备份 `settings.json` 并应用 5 项改动 | 配置生效，JSON 校验通过 |
| 10 | 复测告警与模型归属 | 告警消失，模型仍为 `deepseek-v4-pro` |
| 11 | 核验配置文件 UTF-8 编码未被破坏 | 与备份字节一致，无 BOM |
| 12 | 清理测试残留文件 `$null` | 已删除 |
| 13 | 写入本操作日志 | 完成 |

---

## 7. 甲壳虫项目运行环境（环境对齐）

> 目的：本机与 NUC 的 Python / SQLAlchemy 版本必须落在**同一可用区间**，避免「本机跑不了测试、只有 NUC 能跑」。

### 7.1 版本要求（硬约束）

| 依赖 | 约束 | 原因 |
| --- | --- | --- |
| Python | `>=3.10` | `pyproject.toml` 声明 |
| **SQLAlchemy** | **`>=2.1`** | 2.0.39 及更早的 2.0.x 在 **Python 3.13** 上会让 `Mapped[date]` 抛 `TypeError: Boolean value of this clause is not defined`，`import app.models` 即崩。已在 `pyproject.toml` 收紧为 `>=2.1` |

### 7.2 本机（开发机）实测

| 环境 | Python | SQLAlchemy | 能否跑全量测试 |
| --- | --- | --- | --- |
| **项目 `.venv`（应用这个）** | 3.13.5 | **2.1.4** | ✅ `114 passed` |
| Anaconda 系统 Python（不要用） | 3.13.5 | 2.0.39 | ❌ `import app.models` 直接崩 |

`.venv` 关键版本：`nonebot2 2.5.0` / `nonebot-adapter-onebot 2.4.6` / `pydantic 2.13.5` /
`pydantic-settings 2.15.0` / `httpx 0.28.1` / `pillow 12.3.0` / `cryptography 50.0.1` /
`garminconnect 0.3.15` / `rapidocr-onnxruntime 1.2.3` / `pytest 9.1.1` / `ruff 0.16.8`。

**正确跑测试的方式**（务必用 `.venv`，不要用 `python` 裸命令）：

```powershell
cd "C:\Users\508\Desktop\甲壳虫"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check . --exclude .claude
.venv\Scripts\ruff.exe format --check app tests bot.py
```

### 7.3 NUC（生产机）实测

| 项 | 值 |
| --- | --- |
| 主机 | `192.168.31.223`（Windows） |
| Python | **待确认** |
| SQLAlchemy | **待确认** |

> ⚠️ 待补：SSH 到 NUC 后执行下面命令，把结果回填本表，确保与 7.1 的 `>=2.1` 一致：
>
> ```powershell
> .venv\Scripts\python.exe -c "import sys, sqlalchemy; print(sys.version.split()[0], sqlalchemy.__version__)"
> ```

