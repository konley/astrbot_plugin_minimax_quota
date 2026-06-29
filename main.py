"""MiniMax 限额查询插件

触发指令: /quota
查询 MiniMax 套餐的当前区间与本周剩余限额，并以面向用户的可读文本返回。
"""

from datetime import datetime, timezone, timedelta

import httpx

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# 状态码 -> 用户可读文案
STATUS_MAP = {
    0: "未知",
    1: "受限",
    2: "已用尽",
    3: "充足",
}

# MiniMax 时间戳为东八区毫秒
TZ = timezone(timedelta(hours=8))


def _ts_to_str(ts_ms) -> str:
    """毫秒时间戳转可读时间 (UTC+8)。"""
    if not ts_ms:
        return "未知"
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=TZ).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "未知"


def _ms_to_hours(ms) -> str:
    """毫秒转小时，保留一位小数。"""
    try:
        return f"{int(ms) / 1000 / 3600:.1f}"
    except (ValueError, TypeError):
        return "0.0"


@register(
    "astrbot_plugin_minimax_quota",
    "v_klixie",
    "查询 MiniMax 套餐限额，触发指令 /quota",
    "0.1.0",
)
class MinimaxQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    def _build_message(self, data: dict) -> str:
        """将接口返回的 JSON 渲染为面向用户的可读文本。"""
        base = data.get("base_resp", {})
        if base.get("status_code") != 0:
            return f"查询失败：{base.get('status_msg', '未知错误')}"

        models = data.get("model_remains", [])
        if not models:
            return "未查询到任何套餐限额信息。"

        lines = ["📊 MiniMax 套餐限额", ""]
        for item in models:
            name = item.get("model_name", "未知")
            # 当前区间
            interval_pct = item.get("current_interval_remaining_percent", 0)
            interval_status = STATUS_MAP.get(item.get("current_interval_status", 0), "未知")
            interval_used = item.get("current_interval_usage_count", 0)
            interval_total = item.get("current_interval_total_count", 0)
            interval_reset = _ts_to_str(item.get("end_time"))
            # 本周
            weekly_pct = item.get("current_weekly_remaining_percent", 0)
            weekly_status = STATUS_MAP.get(item.get("current_weekly_status", 0), "未知")
            weekly_used = item.get("current_weekly_usage_count", 0)
            weekly_total = item.get("current_weekly_total_count", 0)
            weekly_reset = _ts_to_str(item.get("weekly_end_time"))

            lines.append(f"【{name} 模型】")
            lines.append(
                f"  当前时段：剩余 {interval_pct}%（{interval_status}），"
                f"已用 {interval_used}/{interval_total}，{interval_reset} 重置"
            )
            lines.append(
                f"  本周：剩余 {weekly_pct}%（{weekly_status}），"
                f"已用 {weekly_used}/{weekly_total}，{weekly_reset} 重置"
            )
            lines.append("")

        lines.append("提示：百分比越高代表可用额度越充足。")
        return "\n".join(lines).strip()

    @filter.command("mmquota")
    async def quota(self, event: AstrMessageEvent):
        """查询 MiniMax 套餐限额。"""
        api_key = (self.config.get("api_key") or "").strip()
        if not api_key:
            yield event.plain_result(
                "未配置 API Key。请在 AstrBot WebUI 的插件配置中填写 MiniMax API Key 后重试。"
            )
            return

        api_base = (self.config.get("api_base") or "").strip() \
            or "https://www.minimaxi.com/v1/token_plan/remains"
        timeout = self.config.get("timeout", 30) or 30

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                resp = await client.get(api_base, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"[minimax_quota] HTTP {e.response.status_code}: {e.response.text}")
            yield event.plain_result(
                f"查询失败：服务器返回 {e.response.status_code}，请检查 API Key 是否正确。"
            )
            return
        except httpx.RequestError as e:
            logger.error(f"[minimax_quota] 请求异常: {e}")
            yield event.plain_result("查询失败：网络请求异常，请稍后重试。")
            return
        except Exception as e:
            logger.error(f"[minimax_quota] 未知错误: {e}")
            yield event.plain_result("查询失败：发生未知错误，请查看日志。")
            return

        yield event.plain_result(self._build_message(data))
