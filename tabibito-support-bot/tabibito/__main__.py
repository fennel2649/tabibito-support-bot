import argparse
import logging
import os
from pathlib import Path

from .core import Config, Store


def main():
    parser = argparse.ArgumentParser(description="旅人鯖の交流支援 Bot")
    parser.add_argument("--config", default="config.toml", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = Config.load(args.config)
    except (OSError, KeyError, ValueError, TypeError) as error:
        parser.error(f"設定ファイルを確認してください: {error}")
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        parser.error("環境変数 DISCORD_BOT_TOKEN が未設定です")
    from .bot import TabibitoBot

    config.database.parent.mkdir(parents=True, exist_ok=True)
    store = Store(config.database, config.guild_id)
    try:
        TabibitoBot(config, store).run(token, log_handler=None)
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
