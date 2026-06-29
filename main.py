"""MiniMax 限额查询插件

触发指令: /mmquota
查询 MiniMax 套餐的当前区间与本周剩余限额，并以面向用户的可读文本返回。

可选能力（均默认关闭，可在 WebUI 配置）：
- admin_only：仅 AstrBot 框架管理员可调用 /mmquota
- schedule_enable：按 Cron 表达式定时把限额推送给指定的人或群
"""

from datetime import datetime, timezone, timedelta

import httpx

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    _APS_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时降级，仅禁用定时功能
    _APS_AVAILABLE = False

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


@register(
    "astrbot_plugin_minimax_quota",
    "v_klixie",
    "查询 MiniMax 套餐限额，触发指令 /mmquota，支持管理员限制与定时推送",
    "0.2.0",
)
class MinimaxQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = None
        if self.config.get("schedule_enable"):
            self._setup_schedule()

    # ---------------- 限额查询 ----------------

    async def _fetch_quota(self) -> dict:
        """请求 MiniMax 接口，返回原始 JSON（失败抛异常）。"""
        api_key = (self.config.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("未配置 API Key")

        api_base = (self.config.get("api_base") or "").strip() \
            or "https://www.minimaxi.com/v1/token_plan/remains"
        timeout = self.config.get("timeout", 30) or 30

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            resp = await client.get(api_base, headers=headers)
            resp.raise_for_status()
            return resp.json()

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

    # ---------------- 指令入口 ----------------

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断发送者是否为 AstrBot 框架管理员，兼容不同版本 API。"""
        # 优先使用 role 属性（'admin' / 'member'）
        role = getattr(event, "role", None)
        if role is not None:
            return role == "admin"
        # 回退到 is_admin() 方法
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    @filter.command("mmquota")
    async def quota(self, event: AstrMessageEvent):
        """查询 MiniMax 套餐限额。"""
        # 管理员开关（默认关）
        if self.config.get("admin_only") and not self._is_admin(event):
            yield event.plain_result("该指令已设置为仅管理员可用，你没有权限调用。")
            return

        try:
            data = await self._fetch_quota()
        except ValueError:
            yield event.plain_result(
                "未配置 API Key。请在 AstrBot WebUI 的插件配置中填写 MiniMax API Key 后重试。"
            )
            return
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

    # ---------------- 定时推送 ----------------

    def _build_session(self) -> str | None:
        """根据配置（纯数字号码）构造统一会话标识 unified_msg_origin。"""
        platform = (self.config.get("schedule_platform") or "aiocqhttp").strip()
        target_type = (self.config.get("schedule_target_type") or "group").strip()
        target_id = (self.config.get("schedule_target_id") or "").strip()
        if not target_id:
            return None
        # 统一会话格式：{platform}:{Group|Private}Message:{id}
        msg_type = "GroupMessage" if target_type == "group" else "PrivateMessage"
        return f"{platform}:{msg_type}:{target_id}"

    def _setup_schedule(self):
        """根据配置注册定时任务。"""
        if not _APS_AVAILABLE:
            logger.error("[minimax_quota] 未安装 apscheduler，定时推送功能不可用。")
            return

        cron = (self.config.get("schedule_cron") or "0 9 * * *").strip()
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=TZ)
        except ValueError as e:
            logger.error(f"[minimax_quota] Cron 表达式无效：'{cron}'，错误：{e}")
            return

        self.scheduler = AsyncIOScheduler(timezone=TZ)
        self.scheduler.add_job(self._scheduled_push, trigger, id="minimax_quota_push")
        self.scheduler.start()
        logger.info(f"[minimax_quota] 定时推送已启用，Cron='{cron}'。")

    async def _scheduled_push(self):
        """定时任务回调：查询并主动推送到目标会话。"""
        session = self._build_session()
        if not session:
            logger.warning("[minimax_quota] 未配置定时推送目标号码，跳过本次推送。")
            return
        try:
            data = await self._fetch_quota()
            text = self._build_message(data)
        except Exception as e:
            logger.error(f"[minimax_quota] 定时查询失败：{e}")
            text = "定时查询 MiniMax 限额失败，请检查 API Key 或网络。"

        try:
            from astrbot.api.event import MessageChain

            await self.context.send_message(session, MessageChain().message(text))
            logger.info(f"[minimax_quota] 已定时推送到 {session}。")
        except Exception as e:
            logger.error(f"[minimax_quota] 定时推送发送失败：{e}")

    async def terminate(self):
        """插件卸载/重载时停止调度器，避免任务残留。"""
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self.scheduler = None
