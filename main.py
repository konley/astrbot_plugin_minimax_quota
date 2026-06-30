"""MiniMax 限额查询插件

触发指令: /mmquota
查询 MiniMax 套餐当前时段的剩余额度，并以信号灯样式的可读文本返回/播报。

可选能力（均默认关闭，可在 WebUI 配置）：
- admin_only：仅 AstrBot 框架管理员可调用 /mmquota
- schedule_enable：按 Cron 表达式定时把完整限额播报推送给指定的人或群
- monitor_enable：后台轮询，余量跌破阶梯档位时自动告警（防重复打扰）
"""

from datetime import datetime, timezone, timedelta

import httpx

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    _APS_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时降级，仅禁用定时/监控功能
    _APS_AVAILABLE = False

# MiniMax 时间戳为东八区毫秒
TZ = timezone(timedelta(hours=8))

DEFAULT_API_BASE = "https://www.minimaxi.com/v1/token_plan/remains"
DEFAULT_TITLE = "MiniMax 限额播报"


def _reset_hint(ts_ms) -> str:
    """根据重置时间戳生成 '相对时间（绝对时间）' 文案。

    例：'3.4 小时后重置（20:00）'，跨天则绝对时间带日期 '（06-30 00:00）'。
    """
    if not ts_ms:
        return "重置时间未知"
    try:
        reset_dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=TZ)
    except (ValueError, TypeError, OSError):
        return "重置时间未知"

    now = datetime.now(tz=TZ)
    secs = (reset_dt - now).total_seconds()
    # 绝对时间：同一天只显示 HH:MM，跨天带日期
    abs_str = reset_dt.strftime("%H:%M") if reset_dt.date() == now.date() \
        else reset_dt.strftime("%m-%d %H:%M")

    if secs <= 0:
        return f"即将重置（{abs_str}）"
    if secs < 3600:
        rel = f"{int(secs // 60)} 分钟后重置"
    elif secs < 86400:
        rel = f"{secs / 3600:.1f} 小时后重置"
    else:
        rel = f"{secs / 86400:.1f} 天后重置"
    return f"{rel}（{abs_str}）"


def _light(percent) -> str:
    """根据剩余百分比返回信号灯 emoji：🟢满 / 🟡偏紧 / 🔴告急。"""
    try:
        p = float(percent)
    except (ValueError, TypeError):
        p = 0
    if p <= 20:
        return "🔴"
    if p < 100:
        return "🟡"
    return "🟢"


@register(
    "astrbot_plugin_minimax_quota",
    "v_klixie",
    "查询 MiniMax 套餐限额，触发指令 /mmquota，支持管理员限制、定时播报与阶梯告警",
    "0.3.0",
)
class MinimaxQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = None
        # 记录每个模型上一次已触发的最低告警档位，用于防重复打扰
        # 结构：{model_name: 已触发的档位值(int)}；None 表示尚未触发任何档位
        self._alerted_level: dict[str, int | None] = {}

        if self.config.get("schedule_enable") or self.config.get("monitor_enable"):
            self._setup_scheduler()

    # ---------------- 限额查询 ----------------

    async def _fetch_quota(self) -> dict:
        """请求 MiniMax 接口，返回原始 JSON（失败抛异常）。"""
        api_key = (self.config.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("未配置 API Key")

        api_base = (self.config.get("api_base") or "").strip() or DEFAULT_API_BASE
        timeout = self.config.get("timeout", 30) or 30

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            resp = await client.get(api_base, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _title(self) -> str:
        return (self.config.get("report_title") or "").strip() or DEFAULT_TITLE

    def _build_message(self, data: dict) -> str:
        """信号灯样式播报：标题 + 每模型一行（仅本时段）。"""
        base = data.get("base_resp", {})
        if base.get("status_code") != 0:
            return f"查询失败：{base.get('status_msg', '未知错误')}"

        models = data.get("model_remains", [])
        if not models:
            return "未查询到任何套餐限额信息。"

        lines = [f"📊 {self._title()}"]
        # 模型名对齐：取最长名称右侧补空格
        names = [str(m.get("model_name", "未知")) for m in models]
        width = max((len(n) for n in names), default=0)
        for item in models:
            name = str(item.get("model_name", "未知"))
            pct = item.get("current_interval_remaining_percent", 0)
            reset = _reset_hint(item.get("end_time"))
            lines.append(f"{_light(pct)} {name.ljust(width)}｜本时段 {pct}% · {reset}")
        return "\n".join(lines)

    # ---------------- 指令入口 ----------------

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断发送者是否为 AstrBot 框架管理员，兼容不同版本 API。"""
        role = getattr(event, "role", None)
        if role is not None:
            return role == "admin"
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

    # ---------------- 主动推送基础设施 ----------------

    def _resolve_platform_id(self) -> str | None:
        """解析推送用的平台实例 ID。

        优先用用户在 WebUI 配置的 schedule_platform 值匹配平台实例 ID；
        若匹配不到，自动查找第一个 aiocqhttp 类型平台实例。
        """
        configured = (self.config.get("schedule_platform") or "").strip()

        pm = getattr(self.context, "platform_manager", None)
        # AstrBot 的 platform_insts 是一个列表，每个元素有 .meta().id
        insts = getattr(pm, "platform_insts", None)
        if not insts:
            # 兼容旧版本：platforms 字典
            insts_dict = getattr(pm, "platforms", None)
            if isinstance(insts_dict, dict) and insts_dict:
                if configured and configured in insts_dict:
                    return configured
                return next(iter(insts_dict.keys()), None)
            return configured or None

        # 精确匹配实例 ID
        if configured:
            for inst in insts:
                try:
                    if inst.meta().id == configured:
                        return configured
                except Exception:
                    pass

        # 模糊匹配：配置值是类型名（如 aiocqhttp），匹配 platform 类型
        if configured:
            for inst in insts:
                try:
                    ptype = type(inst).__name__
                    if configured.lower() in ptype.lower():
                        return inst.meta().id
                except Exception:
                    pass

        # 回退：取第一个 aiocqhttp 类型的实例
        for inst in insts:
            try:
                ptype = type(inst).__name__
                if "aiocqhttp" in ptype.lower():
                    return inst.meta().id
            except Exception:
                pass

        # 再回退：取第一个任意实例
        for inst in insts:
            try:
                return inst.meta().id
            except Exception:
                pass
        return configured or None

    def _build_session(self) -> str | None:
        """根据配置（纯数字号码）构造统一会话标识 unified_msg_origin。"""
        target_type = (self.config.get("schedule_target_type") or "group").strip()
        target_id = (self.config.get("schedule_target_id") or "").strip()
        if not target_id:
            return None
        platform = self._resolve_platform_id() or "aiocqhttp"
        msg_type = "GroupMessage" if target_type == "group" else "PrivateMessage"
        return f"{platform}:{msg_type}:{target_id}"

    async def _send(self, text: str):
        """主动发送一条文本到配置的目标会话。"""
        session = self._build_session()
        if not session:
            logger.warning("[minimax_quota] 未配置推送目标号码，跳过发送。")
            return
        try:
            from astrbot.api.event import MessageChain

            await self.context.send_message(session, MessageChain().message(text))
            logger.info(f"[minimax_quota] 已推送到 {session}。")
        except Exception as e:
            logger.error(f"[minimax_quota] 推送发送失败：{e}")

    # ---------------- 调度器 ----------------

    def _setup_scheduler(self):
        """根据配置注册定时播报与监控告警任务。"""
        if not _APS_AVAILABLE:
            logger.error("[minimax_quota] 未安装 apscheduler，定时/监控功能不可用。")
            return

        self.scheduler = AsyncIOScheduler(timezone=TZ)

        # 定时全量播报
        if self.config.get("schedule_enable"):
            cron = (self.config.get("schedule_cron") or "0 9 * * *").strip()
            try:
                trigger = CronTrigger.from_crontab(cron, timezone=TZ)
                self.scheduler.add_job(self._scheduled_push, trigger, id="mmquota_push")
                logger.info(f"[minimax_quota] 定时播报已启用，Cron='{cron}'。")
            except ValueError as e:
                logger.error(f"[minimax_quota] Cron 表达式无效：'{cron}'，错误：{e}")

        # 阶梯告警监控
        if self.config.get("monitor_enable"):
            interval = self.config.get("monitor_interval", 30) or 30
            try:
                interval = max(1, int(interval))
            except (ValueError, TypeError):
                interval = 30
            self.scheduler.add_job(
                self._monitor_check,
                IntervalTrigger(minutes=interval, timezone=TZ),
                id="mmquota_monitor",
            )
            logger.info(f"[minimax_quota] 阶梯告警监控已启用，间隔 {interval} 分钟。")

        if self.scheduler.get_jobs():
            self.scheduler.start()

    async def _scheduled_push(self):
        """定时任务回调：查询并主动播报完整限额。"""
        try:
            data = await self._fetch_quota()
            text = self._build_message(data)
        except Exception as e:
            logger.error(f"[minimax_quota] 定时查询失败：{e}")
            text = "定时查询 MiniMax 限额失败，请检查 API Key 或网络。"
        await self._send(text)

    # ---------------- 阶梯告警核心逻辑 ----------------

    def _parse_levels(self) -> list[int]:
        """解析告警档位配置，返回从高到低排序、去重的整数列表。"""
        raw = (self.config.get("alert_levels") or "20,10,0").strip()
        levels = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                levels.append(int(float(part)))
            except ValueError:
                continue
        return sorted(set(levels), reverse=True)

    def _parse_monitor_models(self) -> set[str]:
        """解析需监控的模型集合，空表示监控全部。"""
        raw = (self.config.get("monitor_models") or "").strip()
        return {p.strip() for p in raw.split(",") if p.strip()}

    @staticmethod
    def _crossed_level(percent: float, levels: list[int]) -> int | None:
        """返回当前余量已跌破的最低档位（即应触发的档位），未跌破任何档位返回 None。

        levels 已从高到低排序。例如 [20,10,0]，percent=8 -> 命中 0? 否（8>0）实际命中 10。
        """
        hit = None
        for lv in levels:
            if percent <= lv:
                hit = lv  # 继续找更低的命中档位
        return hit

    async def _monitor_check(self):
        """后台轮询：检查各模型余量，跨过新档位时告警一次。"""
        levels = self._parse_levels()
        if not levels:
            return
        try:
            data = await self._fetch_quota()
        except Exception as e:
            logger.error(f"[minimax_quota] 监控查询失败：{e}")
            return

        if data.get("base_resp", {}).get("status_code") != 0:
            return

        watch = self._parse_monitor_models()
        for item in data.get("model_remains", []):
            name = str(item.get("model_name", "未知"))
            if watch and name not in watch:
                continue
            try:
                pct = float(item.get("current_interval_remaining_percent", 100))
            except (ValueError, TypeError):
                continue

            hit = self._crossed_level(pct, levels)  # 当前应处于的告警档位
            last = self._alerted_level.get(name)  # 上次已告警的档位

            if hit is None:
                # 余量已回升到所有档位之上，重置该模型告警状态
                self._alerted_level[name] = None
                continue

            # 仅当跌破了"更低的新档位"时才再次告警（hit 比上次更低，或上次未告警）
            if last is None or hit < last:
                self._alerted_level[name] = hit
                reset = _reset_hint(item.get("end_time"))
                text = (
                    f"⚠️ {self._title()} - 额度告警\n"
                    f"🔴 {name} 本时段仅剩 {int(pct)}%（已跌破 {hit}% 档）· {reset}\n"
                    f"请留意用量。"
                )
                await self._send(text)
            elif hit > last:
                # 余量有所回升但仍在告警区间，更新档位记录但不重复打扰
                self._alerted_level[name] = hit

    async def terminate(self):
        """插件卸载/重载时停止调度器，避免任务残留。"""
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self.scheduler = None
