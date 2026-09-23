# 场内标的监控（ETF 折溢价 + 国债逆回购利率）

监控 A 股场内标的，超出阈值时通过 **Server酱** 推送到微信。
跑在 **GitHub Actions** 上，不需要自己的服务器，公开仓库完全免费。

## 监控标的与告警档位

| 标的 | 类型 | 第一档 ⚠️ | 第二档 🚨 | 代码 | 每日上限 |
|---|---|---|---|---|---|
| 513800 日本东证指数ETF(南方) | 折溢价 | \|折溢价\| ≥ **1%** | \|折溢价\| ≥ **2%** | `sh513800` | 每档 1 条 |
| 159866 日经ETF(工银) | 折溢价 | \|折溢价\| ≥ **3%** | \|折溢价\| ≥ **5%** | `sz159866` | **总计 1 条** |
| GC001 国债逆回购(1天) | 利率 | 利率 ≥ **3%** | 利率 ≥ **4%** | `sh204001` | 总计 2 条 |

两类标的（脚本里的 `kind`）：

- `premium` —— **折溢价监控**：溢价(>0) 和折价(<0) 分方向，档位标识为 `premium_i` / `discount_i`
- `rate` —— **利率监控**：单向，数值越大越「高」，≥ 阈值即触发，档位标识为 `high_i`

规则说明：

- **同一档位每天只推一次**（利率标的按档，折溢价标的按方向+档）
- **回落缓冲**：已在档内时，要跌回「阈值 − 0.5%」才复位；复位后再次突破会重新提醒。
  没有这个缓冲，数值在阈值线上反复蹭就会刷屏
- **标的级每日上限 `daily_cap`**：达到上限后该标的当天不再推送，但**档位仍然置位**，
  避免每次运行都重复评估、反复触发
- **高档优先**：多个档位同时被突破时，先推第二档，再推第一档
- **每日推送总数上限 5 条**（Server酱免费版每天 5 条，正好卡住不超）。
  当前三个标的的理论最大值为 2 + 1 + 2 = 5 条
- 每 5 分钟检查一次，只在 A 股交易时段（9:30–11:30 / 13:00–15:00）生效
- **节假日自动跳过**：用行情数据自带的时间戳判断，不需要维护节假日表

## 文件说明

```
etf_premium_monitor.py          主脚本（纯标准库，无第三方依赖）
state.json                      去抖状态（脚本自动维护，被 workflow 回写）
config.example.json             本地调试用的 SendKey 模板（复制成 config.json）
requirements.txt                无第三方依赖（占位说明）
.github/workflows/monitor.yml   监控工作流（由外部定时器触发）
.github/workflows/keepalive.yml 保活工作流（每月打卡，防止被 GitHub 停用）
```

**数据源**：腾讯行情 `http://qt.gtimg.cn/q=<code>`

| 字段 | 含义 |
|---|---|
| 1 | 名称 |
| 3 | 现价（股票/ETF）／**当前年化利率 %**（逆回购） |
| 4 | 昨收 |
| 30 | 行情时间戳 `YYYYMMDDHHMMSS` |
| 32 | 涨跌幅(%) |
| 77 | 折溢价率(%) —— 仅股票/ETF |
| 78 | IOPV —— 仅股票/ETF |

逆回购没有折溢价/IOPV 概念，脚本对 `kind="rate"` 不读 77/78 字段。

## 部署

**1. 建仓库**
GitHub 右上角 `+` → `New repository` → 名字比如 `etf-premium-monitor` →
可见性选 **Public**（公开仓库 Actions 免费且不限时长；私有仓库每月 2000 分钟）→
不勾选任何初始化选项 → `Create repository`。

**2. 上传文件**
把本目录**全部内容**（含隐藏的 `.github` 目录）传上去。

- 网页：`Add file` → `Upload files` → 拖拽上传。
  ⚠️ macOS 的 Finder 里按 `Cmd + Shift + .` 才能看到 `.github` 这类隐藏目录，
  务必确认 `.github/workflows/` 下的两个 yml 都传上去了。
- 命令行（更可靠）：
  ```bash
  git init && git add -A && git commit -m "init"
  git branch -M main
  git remote add origin https://github.com/<你的用户名>/etf-premium-monitor.git
  git push -u origin main
  ```

**3. 配置 SendKey（密钥不进代码）**
`Settings` → `Secrets and variables` → `Actions` → `New repository secret`
- Name：`SERVERCHAN_SENDKEY`
- Secret：你的 Server酱 SendKey（`SCT` 开头那串）

**4. 打开写权限**（脚本要把 `state.json` 提交回仓库，用于跨运行的档位去抖）
`Settings` → `Actions` → `General` → `Workflow permissions` →
选 **Read and write permissions** → `Save`

**5. 配外部定时器触发**（关键，不要依赖 GitHub 自带的 schedule）

> ⚠️ GitHub Actions 的 `schedule`(cron) 自 2026-08-26 起存在平台级故障：
> 高负载时排队的 job 会被**直接丢弃而非延迟**，且不留失败记录。
> 实测配置「每 5 分钟、覆盖 9:00–15:59」（理论 84 次/日）的 workflow，
> 连续 7 个交易日**每天实际只执行 2 次，其中落在盘中的只有 1 次**，上午盘零覆盖。
> 因此改由外部定时器调用 `workflow_dispatch` 接口来触发。

1. 建一个只授权本仓库、权限为 **Actions: Read and write** 的 fine-grained token
   （GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens）
2. 在 [cron-job.org](https://cron-job.org)（免费，邮箱注册即可）建任务：

   | 字段 | 值 |
   |---|---|
   | URL | `https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/monitor.yml/dispatches` |
   | Request method | **POST** |
   | Request body | `{"ref":"main"}` |
   | Headers | `Authorization: Bearer <token>`<br>`Accept: application/vnd.github+json`<br>`Content-Type: application/json` |
   | Schedule | Custom → 分钟 `0,5,…,55`／小时 `9-14`／周 `Mon-Fri`，时区 `Asia/Shanghai` |
   | 通知 | 打开「因过多失败被禁用时通知」 |

   最终 crontab 应为 `*/5 9-14 * * 1-5`。
   只到 14 点：脚本的收盘判断是 `t <= 15:00:00`（精确到秒），15:00 那次触发只要晚一秒
   就会被判为非交易时段，留 15 点反而白跑 11 次。
3. 保存后点 **Test run**：期望返回码 `204`，且仓库 Actions 页面出现 `workflow_dispatch` 运行。

**6. 试跑**
`Actions` 标签页 → 左侧选「ETF 折溢价监控」→ 如有 `Enable workflow` 就点一下 →
右侧 `Run workflow` 手动跑 → 点进这次运行看日志。
（非交易时段运行会直接跳过，属于正常行为。）

## 本地运行

```bash
cp config.example.json config.json   # 填入 SendKey（config.json 已被 .gitignore 忽略）

python3 etf_premium_monitor.py                       # 正常模式
python3 etf_premium_monitor.py --force               # 忽略交易时段/休市判断
python3 etf_premium_monitor.py --dry-run             # 不发推送，只打印
python3 etf_premium_monitor.py --force --dry-run --state /tmp/s.json
```

也可以用环境变量：`SERVERCHAN_SENDKEY=xxx python3 etf_premium_monitor.py --force`

## 调整标的与阈值

改脚本顶部的 `TARGETS`：

```python
TARGETS = (
    {"code": "sh513800", "label": "513800 日本东证指数ETF(南方)",
     "kind": "premium", "levels": (1.0, 2.0)},
    {"code": "sz159866", "label": "159866 日经ETF(工银)",
     "kind": "premium", "levels": (3.0, 5.0), "daily_cap": 1},
    {"code": "sh204001", "label": "GC001 国债逆回购(1天)",
     "kind": "rate",    "levels": (3.0, 4.0), "daily_cap": 2},
)

HYSTERESIS = 0.5        # 回落缓冲(百分点)
MAX_PUSH_PER_DAY = 5    # 每日推送总条数上限
```

- `levels` 可给任意档位（如 `(1.0, 2.0, 3.0)` 三档），档位标识自动生成
- `daily_cap` 可选，限制该标的每天最多推几条
- 加标的只需往 `TARGETS` 里追加一条：折溢价用 `kind="premium"`，利率类用 `kind="rate"`；
  沪市用 `sh`、深市用 `sz` 前缀
- 注意 `MAX_PUSH_PER_DAY` 要 ≥ 各标的 `daily_cap` 之和，否则会互相挤占

## 已知限制

- **不要依赖 GitHub 自带的 schedule**：见上文部署第 5 步，已改用外部定时器触发。
  外部定时器（cron-job.org 免费版）每 job 每小时最多 60 次、无每日总次数上限；
  连续失败 25 次会自动禁用任务，可在任务设置里打开邮件通知。
- **60 天限制**：GitHub 会停用连续 60 天没有仓库活动的定时任务。
  仓库里的 `keepalive.yml` 每月 1 号自动打卡一次，正常情况下不会触发。
- **推送额度是共享的**：三个标的一共只有 5 条/天，极端行情下可能被先用完，
  被跳过的档位当天不再补推（第二天重新计算）。
- **公开仓库**：代码和 `state.json` 会公开（只有档位状态，无敏感信息）；
  SendKey 存在仓库 Secrets 里，不会泄露。
