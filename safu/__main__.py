import uvicorn

from safu.api import create_app
from safu.config import load_settings


def main() -> None:
    settings = load_settings()
    # One process. Render's free instance has 512 MB, and extra workers would duplicate it.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
