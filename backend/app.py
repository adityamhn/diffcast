"""Flask application factory."""

import os

from dotenv import load_dotenv
from flask import Flask

# Load .env from backend directory (works regardless of cwd)
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from config import config_by_name
from routes import main_bp, api_bp, webhook_bp, sync_bp, repos_bp


def create_app(config_name=None):
    """Create and configure the Flask app."""
    app = Flask(__name__)

    config_name = config_name or os.environ.get("FLASK_ENV", "default")
    app.config.from_object(config_by_name[config_name])

    # Register blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(repos_bp)

    return app


# For `flask run` and direct execution
app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
