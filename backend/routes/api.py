"""API routes - versioned under /api."""

from flask import Blueprint, jsonify

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/")
def index():
    """API root - lists available endpoints."""
    return jsonify({
        "version": "v1",
        "endpoints": {
            "status": "/api/status",
        },
    })


@api_bp.route("/status")
def status():
    """API status check."""
    return jsonify({"status": "operational"})
