import logging

import uvicorn

from . import config
from .web.app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The rate-limited Douban / TMDB calls are noisy at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
