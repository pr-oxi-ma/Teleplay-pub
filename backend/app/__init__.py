"""
TelePlay Backend Application package.

Keep package import side-effect free. Uvicorn should load the ASGI app from
``app.main:app``; importing ``app`` or any submodule must not eagerly start the
FastAPI app and Telegram/router dependencies.
"""

__all__: list[str] = []
