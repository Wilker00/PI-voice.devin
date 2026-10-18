"""PI Voice — FastAPI app: static frontend + REST + /ws audio socket."""
from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .model import load_model
from .profiles import ProfileStore
from .session import ClientSession
from .system import get_stats

logging.basicConfig(level=logging.INFO)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    app = FastAPI(title="PI Voice")
    model = load_model()
    profiles = ProfileStore(s.profiles_path)

    @app.get("/api/health")
    def health():
        return {"ok": True, "model": type(model).__name__,
                "profiles": len(profiles.names())}

    @app.get("/api/system")
    def system_stats():
        return get_stats()

    @app.get("/api/profiles")
    def list_profiles():
        return {"profiles": profiles.names()}

    @app.delete("/api/profiles/{name}")
    def delete_profile(name: str):
        if profiles.remove(name):
            return {"ok": True, "profiles": profiles.names()}
        return JSONResponse({"ok": False, "error": "not found"}, 404)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        try:
            await ClientSession(ws, model, profiles, s).run()
        except Exception:
            logging.getLogger("pi_voice").exception("session error")

    app.mount("/", StaticFiles(directory=str(s.web_dir), html=True),
              name="web")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("server.main:app", host=s.host, port=s.port)
