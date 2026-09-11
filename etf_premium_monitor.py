#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日股 ETF 折溢价监控 —— 多标的、多档位版

监控标的（每只独立配置档位）:
  513800 日本东证指数ETF(南方)  档位 1.0% / 2.0%
  159866 日经ETF(工银)          档位 3.0% / 5.0%

数据源 : 腾讯行情 http://qt.gtimg.cn/q=<code>   （沪市 sh，深市 sz）
         字段 77 = 折溢价率(%), 78 = IOPV, 30 = 行情时间戳

告警逻辑:
  * 溢价(>0) / 折价(<0) 分方向，同一标的同一方向同一档位每天只推一次
  * 回落有 0.5 个百分点的缓冲(滞后区间)，避免在阈值附近反复刷屏
  * 多个档位同时被突破时，优先推高档位
  * 每日推送总数有硬上限（Server酱免费版每天 5 条），先到先得、高档优先

推送渠道 : Server酱（SendKey 走环境变量 SERVERCHAN_SENDKEY）

设计要点:
  * 纯标准库，无第三方依赖
  * 无状态环境(GitHub Actions)下靠 state.json 保存当日档位状态与推送计数
  * state.json 仅在内容真正变化时写盘，避免无意义的提交
  * 用行情数据自带的时间戳判断是否休市，无需维护节假日表

用法:
  python etf_premium_monitor.py                 # 正常模式
  python etf_premium_monitor.py --force         # 忽略时段/休市判断
  python etf_premium_monitor.py --dry-run       # 不推送，仅打印
  python etf_premium_monitor.py --state /tmp/s.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- 配置区 ---

TZ = ZoneInfo("Asia/Shanghai")

TARGETS = (
    {
        "code": "sh513800",
        "label": "513800 日本东证指数ETF(南方)",
        "levels": (1.0, 2.0),          # 第一档 1%，第二档 2%
    },
    {
        "code": "sz159866",
        "label": "159866 日经ETF(工银)",
        "levels": (3.0, 5.0),          # 第一档 3%，第二档 5%
    },
)

HYSTERESIS = 0.5        # 回落缓冲(百分点)：已在档内时需跌回「阈值-缓冲」才复位
MAX_PUSH_PER_DAY = 4    # 每日推送总条数上限（Server酱免费版每天 5 条，留 1 条余量）

MORNING = (dtime(9, 30), dtime(11, 30))
AFTERNOON = (dtime(13, 0), dtime(15, 0))

# ------------------------------------------------------------------ 工具 ---


def log(msg: str) -> None:
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def level_keys(levels) -> tuple:
    """某标的的全部档位标识，如 ('premium_1','premium_2','discount_1','discount_2')。"""
    return tuple(
        f"{side}_{idx}"
        for idx in range(1, len(levels) + 1)
        for side in ("premium", "discount")
    )


def in_trading_hours(now: datetime) -> bool:
    """是否处于 A 股交易时段（北京时间，周一至周五）。"""
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (MORNING[0] <= t <= MORNING[1]) or (AFTERNOON[0] <= t <= AFTERNOON[1])


def fetch_quote(code: str, retries: int = 3, timeout: int = 10) -> dict:
    """抓取腾讯行情并解析。code 形如 sh513800 / sz159866。"""
    url = f"http://qt.gtimg.cn/q={code}"
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode(
                "gbk", errors="ignore"
            )
            if '="' not in raw:
                raise ValueError(f"响应格式异常: {raw[:80]!r}")
            parts = raw.split('="', 1)[1].split("~")
            if len(parts) < 80:
                raise ValueError(f"字段数不足({len(parts)})，代码可能有误")
            quote = {
                "name": parts[1],
                "code": parts[2],
                "price": float(parts[3]),
                "prev_close": float(parts[4]),
                "change_pct": float(parts[32]),
                "quote_time": parts[30],
                "premium": float(parts[77]),
                "iopv": float(parts[78]),
            }
            if quote["price"] <= 0:
                raise ValueError("现价为 0，行情未有效刷新")
            return quote
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                time.sleep(3)
    raise RuntimeError(f"获取行情失败(retries={retries}): {last_err}")


def compute_levels(premium: float, prev_active: dict, levels) -> set:
    """算出当前应处于激活状态的档位集合。

    进入档位用阈值本身，退出档位用 阈值-缓冲，构成滞后区间，防止阈值附近抖动刷屏。
    """
    magnitude = abs(premium)
    side = "premium" if premium >= 0 else "discount"
    active: set = set()

    for idx, threshold in enumerate(levels, start=1):
        key = f"{side}_{idx}"
        was_on = bool(prev_active.get(key, False))
        if was_on:
            if magnitude >= threshold - HYSTERESIS:
                active.add(key)
        elif magnitude >= threshold:
            active.add(key)
    return active


def fmt_time(stamp: str) -> str:
    """20260911135925 -> 2026-09-11 13:59:25"""
    if len(stamp) < 14:
        return stamp
    return (
        f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} "
        f"{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}"
    )


# ------------------------------------------------------------ 状态读写 ---


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("状态文件不是对象")
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log(f"状态文件无法解析({exc})，按空状态处理")
        return {}


def save_state(path: Path, state: dict) -> bool:
    """仅在内容变化时写盘，返回是否发生了写入。"""
    new_text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        old_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_text = ""
    if old_text == new_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


# -------------------------------------------------------------- 推送 ---


def read_sendkey() -> str:
    """优先环境变量，其次同目录 config.json（本地调试用）。"""
    key = (os.environ.get("SERVERCHAN_SENDKEY") or "").strip()
    if key:
        return key
    cfg = Path(__file__).with_name("config.json")
    if cfg.exists():
        try:
            return (json.loads(cfg.read_text(encoding="utf-8")).get("sendkey") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


def push(sendkey: str, title: str, desp: str) -> bool:
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    body = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        data = json.loads(resp)
    except Exception as exc:  # noqa: BLE001
        log(f"推送请求异常: {exc}")
        return False
    ok = data.get("code") == 0
    if not ok:
        log(f"推送被拒绝: {data}")
    return ok


def alert_text(target: dict, key: str, quote: dict) -> tuple:
    side, idx = key.split("_")
    idx = int(idx)
    threshold = target["levels"][idx - 1]
    word = "溢价" if side == "premium" else "折价"
    mark = "🚨 第二档" if idx >= 2 else "⚠️ 第一档"
    sign = "+" if quote["premium"] >= 0 else ""

    title = f"{mark}｜{target['label']} {word} {threshold:g}%"
    desp = (
        f"**{target['label']}**（{quote['name']}）\n\n"
        f"- 折溢价率：**{sign}{quote['premium']:.2f}%**  → 触发{word} {threshold:.0f}% 档\n"
        f"- 现价：{quote['price']:.3f}\n"
        f"- IOPV：{quote['iopv']:.4f}\n"
        f"- 当日涨跌：{quote['change_pct']:+.2f}%\n"
        f"- 行情时间：{fmt_time(quote['quote_time'])}\n\n"
        f"> 告警规则：溢价/折价分方向、分档位，同一方向同一档位每天只推一次；"
        f"回落 {HYSTERESIS} 个百分点后复位，再次突破会重新提醒。"
    )
    return title, desp


def write_summary(lines: list) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 主流程 ---


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="日股 ETF 折溢价监控（多标的多档位）")
    parser.add_argument("--force", action="store_true", help="忽略交易时段/休市判断")
    parser.add_argument("--dry-run", action="store_true", help="不推送，仅打印")
    parser.add_argument(
        "--state",
        default=str(Path(__file__).with_name("state.json")),
        help="状态文件路径",
    )
    args = parser.parse_args(argv)

    state_path = Path(args.state)
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    today_stamp = now.strftime("%Y%m%d")

    summary = [f"### 折溢价检查 — {now:%Y-%m-%d %H:%M:%S} (UTC+8)", ""]

    # 1. 交易时段
    if not args.force and not in_trading_hours(now):
        log("非 A 股交易时段，跳过")
        summary.append("非交易时段，已跳过。")
        write_summary(summary)
        return 0

    # 2. 状态：跨日重置
    state = load_state(state_path)
    if state.get("date") != today:
        log(f"新交易日 {today}，重置全部档位状态")
        state = {"date": today, "push_count": 0, "targets": {}}
    saved_targets = dict(state.get("targets") or {})

    # 3. 逐标的取行情、算档位
    pending = []       # 待推送项
    final_active = {}  # 标的 -> 本次结束后应处于激活状态的档位集合
    ok_count = 0

    for target in TARGETS:
        code, label = target["code"], target["label"]
        keys = level_keys(target["levels"])

        try:
            quote = fetch_quote(code)
        except Exception as exc:  # noqa: BLE001
            log(f"{label} 行情获取失败：{exc}")
            summary.append(f"- ❌ `{label}` 行情获取失败：`{exc}`")
            continue

        if not args.force and quote["quote_time"][:8] != today_stamp:
            log(f"{label} 行情时间戳 {quote['quote_time'][:8]} 非今日，判定休市，跳过")
            summary.append(f"- `{label}` 行情时间戳非今日，判定休市，跳过")
            continue

        ok_count += 1
        premium = quote["premium"]
        prev = dict(saved_targets.get(code) or {})
        prev_on = {k for k in keys if prev.get(k)}
        current = compute_levels(premium, prev, target["levels"])
        final_active[code] = set(current)
        newly = sorted(current - prev_on)

        log(
            f"{label} 现价{quote['price']:.3f} IOPV{quote['iopv']:.4f} "
            f"折溢价{premium:+.2f}% 涨跌{quote['change_pct']:+.2f}% "
            f"｜激活{sorted(current) or '无'}｜新触发{newly or '无'}"
        )
        summary.append(
            f"- `{label}`：现价 `{quote['price']:.3f}`｜IOPV `{quote['iopv']:.4f}`｜"
            f"**折溢价 `{premium:+.2f}%`**｜激活 `{', '.join(sorted(current)) or '无'}`"
        )

        for key in newly:
            pending.append(
                {
                    "target": target,
                    "quote": quote,
                    "key": key,
                    "level": int(key.split("_")[1]),
                }
            )
        time.sleep(0.5)  # 多标的时稍微错开请求

    if ok_count == 0:
        log("所有标的均未取到有效行情")
        write_summary(summary)
        return 0

    # 4. 推送：高档位优先，受每日总上限约束
    pending.sort(key=lambda item: -item["level"])
    sendkey = read_sendkey()
    push_count = int(state.get("push_count") or 0)
    pushed = []

    for pos, item in enumerate(pending):
        label, key = item["target"]["label"], item["key"]
        if push_count >= MAX_PUSH_PER_DAY:
            log(f"已达每日推送上限 {MAX_PUSH_PER_DAY} 条，跳过 {label} {key}")
            continue
        title, desp = alert_text(item["target"], key, item["quote"])
        if args.dry_run:
            log(f"[DRY-RUN] 拟推送: {title}")
            pushed.append(f"{label} {key}")
            push_count += 1
            continue
        if not sendkey:
            log("缺少 SendKey（环境变量 SERVERCHAN_SENDKEY 或 config.json），无法推送")
            break
        if push(sendkey, title, desp):
            log(f"已推送: {title}")
            pushed.append(f"{label} {key}")
            push_count += 1
            time.sleep(1)
        else:
            # 推送失败不标记为已激活，下次继续尝试
            log(f"推送失败，{label} {key} 保留待下次重试")
            final_active[item["target"]["code"]].discard(key)

    # 5. 落盘
    new_targets = dict(saved_targets)
    for target in TARGETS:
        code, keys = target["code"], level_keys(target["levels"])
        if code in final_active:
            new_targets[code] = {k: (k in final_active[code]) for k in keys}
        else:
            new_targets.setdefault(code, {k: False for k in keys})
    state["date"] = today
    state["targets"] = new_targets
    state["push_count"] = push_count
    state["updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
    changed = save_state(state_path, state)

    summary.append("")
    summary.append(
        f"- 本次推送：`{'; '.join(pushed) or '无'}`｜今日累计 `{push_count}/{MAX_PUSH_PER_DAY}` 条"
    )
    summary.append(f"- 状态文件{'已更新' if changed else '无变化'}")
    if args.dry_run:
        summary.append("- （dry-run 模式，未真实发送）")
    write_summary(summary)

    log("检查完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
