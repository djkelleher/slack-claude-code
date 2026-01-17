"""FastAPI server for web-based code and diff viewer."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.config import config
from src.webviewer.cache import WebViewerCache
from src.webviewer.renderer import render_code, render_diff
from src.webviewer.security import validate_signature

logger = logging.getLogger(__name__)

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# Global cache instance
_cache: WebViewerCache | None = None


def get_cache() -> WebViewerCache:
    """Get the global cache instance.

    Returns
    -------
    WebViewerCache
        The cache instance.

    Raises
    ------
    RuntimeError
        If cache not initialized.
    """
    if _cache is None:
        raise RuntimeError("WebViewerCache not initialized")
    return _cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _cache
    _cache = WebViewerCache(config.DATABASE_PATH)
    logger.info(f"Web viewer server started on port {config.timeouts.webviewer.port}")
    yield
    logger.info("Web viewer server stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns
    -------
    FastAPI
        The configured FastAPI application.
    """
    app = FastAPI(
        title="Claude Code Web Viewer",
        description="Web-based code and diff viewer for Slack Claude Code",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "ok"}

    @app.get("/view/{content_id}", response_class=HTMLResponse)
    async def view_content(
        request: Request,
        content_id: str,
        expires: int = Query(..., description="Expiration timestamp"),
        sig: str = Query(..., description="HMAC signature"),
    ):
        """View code or diff content.

        Parameters
        ----------
        request : Request
            The FastAPI request object.
        content_id : str
            The content ID.
        expires : int
            Expiration timestamp.
        sig : str
            HMAC signature.

        Returns
        -------
        HTMLResponse
            Rendered HTML page.

        Raises
        ------
        HTTPException
            If signature invalid or content not found.
        """
        # Validate signature
        if not validate_signature(content_id, expires, sig):
            raise HTTPException(status_code=403, detail="Invalid or expired link")

        # Get content
        cache = get_cache()
        content = await cache.get(content_id)

        if not content:
            raise HTTPException(status_code=404, detail="Content not found or expired")

        # Render based on content type
        if content.content_type == "diff":
            unified_diff, side_by_side_html, css = render_diff(
                content.content,
                content.new_content or "",
                content.file_path,
            )
            return templates.TemplateResponse(
                "diff_view.html",
                {
                    "request": request,
                    "file_path": content.file_path or "file",
                    "tool_name": content.tool_name or "Edit",
                    "unified_diff": unified_diff,
                    "side_by_side_html": side_by_side_html,
                    "css": css,
                },
            )
        else:
            highlighted_code, css = render_code(content.content, content.file_path)
            return templates.TemplateResponse(
                "code_view.html",
                {
                    "request": request,
                    "file_path": content.file_path or "file",
                    "tool_name": content.tool_name or "Read",
                    "highlighted_code": highlighted_code,
                    "css": css,
                },
            )

    return app


async def run_server():
    """Run the web viewer server."""
    import uvicorn

    app = create_app()
    server_config = uvicorn.Config(
        app,
        host=config.timeouts.webviewer.host,
        port=config.timeouts.webviewer.port,
        log_level="info",
    )
    server = uvicorn.Server(server_config)
    await server.serve()
