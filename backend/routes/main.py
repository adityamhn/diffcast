"""Main routes - landing, health, etc."""

from flask import Blueprint, jsonify

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    """Landing page."""
    return jsonify({
        "message": "Welcome to Diffcast",
        "docs": "/api",
        "health": "/health",
    })


@main_bp.route("/health")
def health():
    """Health check endpoint for deployment/monitoring."""
    return jsonify({"status": "ok"})
