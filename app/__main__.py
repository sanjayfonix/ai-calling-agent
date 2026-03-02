"""
Application entry point.
Run with: python -m app   OR   uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import uvicorn
from app.config import get_settings


def main():
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.effective_port,
        reload=settings.app_debug,
        log_level=settings.log_level.lower(),
        ws_max_size=16 * 1024 * 1024,  # 16MB for audio
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    main()
