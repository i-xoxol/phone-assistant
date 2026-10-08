from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.config import Settings
from app.api.calls import router as calls_router
from app.api.twilio_webhooks import router as twilio_router
from app.calling.call_manager import CallManager
from app.calling.twilio_client import TwilioGateway
from app.logging_config import setup_logging
from app.storage.database import Database
from app.api.live import router as live_router
from app.web_auth import WebAuth
from app.events import WebhookDispatcher


def create_app(settings: Settings | None = None, *, gateway=None) -> FastAPI:
    setup_logging()
    @asynccontextmanager
    async def lifespan(app):
        await app.state.webhook.start()
        if hasattr(app.state.manager, 'mcp_events'):
            await app.state.manager.mcp_events.start()
        try:
            if hasattr(app.state, 'mcp'):
                async with app.state.mcp.session_manager.run():
                    yield
            else:
                yield
        finally:
            if hasattr(app.state.manager, 'mcp_events'):
                await app.state.manager.mcp_events.stop()
            await app.state.webhook.stop()
    app = FastAPI(title='Phone Assistant', lifespan=lifespan)
    app.state.settings = settings or Settings()
    app.state.manager = CallManager(app.state.settings, Database(app.state.settings.database_path),
                                    gateway or TwilioGateway(app.state.settings))
    app.state.web_auth = WebAuth(app.state.manager.db)
    app.state.webhook = WebhookDispatcher(app.state.manager)
    app.include_router(calls_router)
    app.include_router(twilio_router)
    app.include_router(live_router)

    @app.get('/health')
    def health():
        missing = app.state.settings.missing()
        return {'status': 'ok', 'ready': not missing, 'missing_configuration': missing}

    if app.state.settings.mcp_enabled:
        from app.mcp.server import create_mcp
        app.state.mcp, app.state.oauth = create_mcp(app.state.manager)
        from urllib.parse import urlsplit
        from mcp.server.transport_security import TransportSecuritySettings
        app.mount('/', app.state.mcp.streamable_http_app(stateless_http=True, json_response=True,
            transport_security=TransportSecuritySettings(
                allowed_hosts=[urlsplit(app.state.settings.public_base_url).netloc,
                    '127.0.0.1:*', 'localhost:*', 'testserver'],
                allowed_origins=['https://chatgpt.com'])))
    return app


app = create_app()
