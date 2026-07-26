"""astrbot_plugin_example - 插件一句话描述。"""

import asyncio

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools


class ExamplePlugin(Star):
    """插件主类。整个插件唯一的 Star 子类,必须定义在 main.py。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task] = set()

    async def initialize(self):
        """插件激活时调用。耗时初始化放这里,不放 __init__。"""
        self.data_dir = StarTools.get_data_dir()  # data/plugin_data/<插件名>
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("example 插件初始化完成")

    def _spawn(self, coro) -> asyncio.Task:
        """创建受管理的后台任务,terminate 时统一清理。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @filter.command("example", alias={"示例"})
    async def example_cmd(self, event: AstrMessageEvent):
        """示例指令,回显发送者昵称。"""  # docstring 会展示给用户
        try:
            yield event.plain_result(f"Hello, {event.get_sender_name()}!")
        except Exception:
            logger.exception("example 指令执行失败")
            yield event.plain_result("出了点问题,请稍后再试~")

    async def terminate(self):
        """禁用/卸载/重载/更新时调用。必须直接定义在主类上;禁止定义 __del__。"""
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("example 插件已终止")
