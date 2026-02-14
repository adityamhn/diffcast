"""Route blueprints for the application."""

from .main import main_bp
from .api import api_bp
from .webhook import webhook_bp
from .sync import sync_bp
from .repos import repos_bp

__all__ = ["main_bp", "api_bp", "webhook_bp", "sync_bp", "repos_bp"]
