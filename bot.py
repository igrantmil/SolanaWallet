import asyncio
import json
import logging
import signal
from pathlib import Path
from typing import Optional

import betterlogging as bl
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from redis import asyncio as aioredis

from tgbot.config import Config, load_config
from tgbot.handlers import routers_list
from tgbot.middlewares.config import ConfigMiddleware
from tgbot.middlewares.database import DatabaseMiddleware
from tgbot.middlewares.dev import DeveloperMiddleware
from tgbot.services import broadcaster
from tgbot.services.migration import init_db_and_migrations


class TgBot:
    def __init__(self, config: Config):
        self.config = config
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.redis: Optional[aioredis.Redis] = None
        self.pubsub: Optional[aioredis.client.PubSub] = None
        self.app: Optional[web.Application] = None
        self.logger = logging.getLogger(__name__)

    async def setup_redis(self) -> None:
        try:
            self.redis = await aioredis.from_url(self.config.redis.dsn(1))
            self.pubsub = self.redis.pubsub()
            await self.pubsub.subscribe("solana_transactions")
        except Exception as e:
            self.logger.error(f"Failed to setup Redis: {e}")
            raise

    async def setup_bot(self) -> None:
        storage = RedisStorage.from_url(
            self.config.redis.dsn(),
            key_builder=DefaultKeyBuilder(with_bot_id=True, with_destiny=True),
        )

        session = AiohttpSession(
            api=TelegramAPIServer.from_base("http://tracker_nginx:80")
        )
        self.bot = Bot(
            token=self.config.tg_bot.token, parse_mode="HTML", session=session
        )
        self.dp = Dispatcher(storage=storage)

        # Register handlers
        self.dp.include_routers(*routers_list)
        self.register_middlewares()

    def register_middlewares(self) -> None:
        middleware_types = [
            ConfigMiddleware(self.config, self.redis),
            DatabaseMiddleware(),
            DeveloperMiddleware(),
        ]

        for middleware_type in middleware_types:
            self.dp.message.outer_middleware(middleware_type)
            self.dp.callback_query.outer_middleware(middleware_type)

    async def setup_webhook(self) -> None:
        self.app = web.Application()
        webhook_handler = SimpleRequestHandler(dispatcher=self.dp, bot=self.bot)
        webhook_handler.register(self.app, path="/webhook")
        setup_application(self.app, self.dp, bot=self.bot)

    async def on_startup(self) -> None:
        await init_db_and_migrations(
            database_url=f"postgresql+asyncpg://{self.config.postgres.db_user}:{self.config.postgres.db_pass}"
            f"@{self.config.postgres.db_host}:5432/{self.config.postgres.db_name}",
            alembic_cfg_path=str(Path(__file__).parent / "alembic.ini"),
        )
        await broadcaster.broadcast(
            self.bot, self.config.tg_bot.admin_ids, "Бот запущен"
        )
        await self.bot.delete_webhook()
        await self.bot.set_webhook("http://tracker_tg_bot/webhook")

    async def on_shutdown(self) -> None:
        if self.bot:
            await self.bot.session.close()
        if self.redis:
            await self.redis.close()
        if self.pubsub:
            await self.pubsub.unsubscribe()

    def setup_logging(self) -> None:
        bl.basic_colorized_config(level=logging.INFO)
        logging.basicConfig(
            level=logging.INFO,
            format="%(filename)s:%(lineno)d #%(levelname)-8s [%(asctime)s] - %(name)s - %(message)s",
        )
        self.logger.info("Starting bot")

    async def start(self) -> None:
        try:
            self.setup_logging()
            await self.setup_redis()
            await self.setup_bot()
            await self.setup_webhook()
            await self.on_startup()

            asyncio.create_task(self.process_transaction_updates())

            # graceful shutdown
            for sig in (signal.SIGTERM, signal.SIGINT):
                asyncio.get_event_loop().add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self.shutdown(s))
                )

            runner = web.AppRunner(self.app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", 80)
            await site.start()

            await asyncio.Event().wait()

        except Exception as e:
            self.logger.error(f"Error starting bot: {e}")
            raise

    async def shutdown(self, signal: Optional[signal.Signals] = None) -> None:
        self.logger.info(f"Received exit signal {signal.name if signal else 'Unknown'}")
        await self.on_shutdown()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.get_event_loop().stop()

    async def process_transaction_updates(self):
        """Prorcees the transactions coming from pubsub"""
        while True:
            try:
                message = await self.pubsub.get_message(timeout=1)
                if not message or message["type"] != "message":
                    continue

                data = json.loads(message["data"])

                text = f"transaction for the address: {data['address']}"

                for chat_id in data["chat_ids"]:
                    try:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        self.logger.error(f"Error sending notification: {e}")

            except Exception as e:
                self.logger.error(f"Error processing transaction: {e}")

            await asyncio.sleep(0.1)


def main():
    config = load_config(".env")
    bot = TgBot(config)

    try:
        asyncio.run(bot.start())
    except (KeyboardInterrupt, SystemExit):
        logging.error("Bot stopped")


if __name__ == "__main__":
    main()
