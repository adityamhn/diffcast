"""Flask application factory."""

import json
import os
import logging

from dotenv import load_dotenv
from flask import Flask, request

# Load .env from backend directory (works regardless of cwd)
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from config import config_by_name
from utils.logging_config import configure_logging
from routes import main_bp, api_bp, webhook_bp, sync_bp, repos_bp, pipeline_bp
class FirestoreJSONEncoder(json.JSONEncoder):
    """Handle Firestore Timestamp and datetime in JSON responses."""

    def default(self, o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        # Firestore Timestamp (has .timestamp() or seconds)
        if hasattr(o, "timestamp"):
            from datetime import datetime
            return datetime.fromtimestamp(o.timestamp()).isoformat()
        if hasattr(o, "seconds"):
            from datetime import datetime
            return datetime.fromtimestamp(getattr(o, "seconds", 0)).isoformat()
        return super().default(o)


def create_app(config_name=None):
    """Create and configure the Flask app."""
    configure_logging()
    app = Flask(__name__)
    app.json_encoder = FirestoreJSONEncoder

    # CORS for frontend
    @app.after_request
    def cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PATCH"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    @app.before_request
    def cors_preflight():
        if request.method == "OPTIONS":
            return "", 204

    config_name = config_name or os.environ.get("FLASK_ENV", "default")
    app.config.from_object(config_by_name[config_name])

    # Register blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(repos_bp)
    app.register_blueprint(pipeline_bp)

    logging.getLogger(__name__).info(
        "Flask app initialized with config=%s debug=%s",
        config_name,
        app.config.get("DEBUG"),
    )

    return app


# For `flask run` and direct execution
app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
