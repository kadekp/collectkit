"""
Health check HTTP endpoint for Railway deployment monitoring.

Provides a /health endpoint that returns:
- Worker status (healthy/degraded)
- Background thread status
- Metrics summary
- Uptime information

Usage:
    from .health import start_health_server, set_background_thread_status
    
    server = start_health_server(port=8080)
    set_background_thread_status(True)  # Called from background thread
"""

import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from typing import Optional

from .metrics import metrics
from .logging_config import get_logger

logger = get_logger(__name__)

# Optional token for protecting sensitive endpoints
HEALTH_CHECK_TOKEN = os.getenv("HEALTH_CHECK_TOKEN")

# Global state for background thread status
_background_thread_alive = True
_background_thread_lock = threading.Lock()


def set_background_thread_status(alive: bool) -> None:
    """Update the background thread status."""
    global _background_thread_alive
    with _background_thread_lock:
        _background_thread_alive = alive


def get_background_thread_status() -> bool:
    """Get current background thread status."""
    with _background_thread_lock:
        return _background_thread_alive


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health checks."""

    def _check_auth(self) -> bool:
        """Check if request has valid auth token (if configured)."""
        if not HEALTH_CHECK_TOKEN:
            return True  # No token configured, allow all (backwards compat)

        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {HEALTH_CHECK_TOKEN}":
            return True

        # Also allow query param for simple health checks
        if f"?token={HEALTH_CHECK_TOKEN}" in self.path:
            return True

        return False

    def _send_unauthorized(self):
        """Send 401 Unauthorized response."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def do_GET(self):
        """Handle GET requests."""
        # /live is always public (for Railway/K8s liveness probes)
        if self.path == "/live":
            self._handle_live()
            return

        # All other endpoints require auth if token is configured
        if not self._check_auth():
            self._send_unauthorized()
            return

        # Strip query params for path matching
        path = self.path.split("?")[0]

        if path == "/health" or path == "/":
            self._handle_health()
        elif path == "/metrics":
            self._handle_metrics()
        elif path == "/ready":
            self._handle_ready()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_health(self):
        """Return comprehensive health status."""
        bg_alive = get_background_thread_status()
        status = "healthy" if bg_alive else "degraded"

        response = {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": {
                "background_thread": "alive" if bg_alive else "dead",
            },
            "metrics": metrics.get_all(),
        }

        self.send_response(200 if bg_alive else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response, indent=2).encode())

    def _handle_metrics(self):
        """Return metrics only."""
        response = metrics.get_all()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response, indent=2).encode())

    def _handle_ready(self):
        """Kubernetes-style readiness probe."""
        bg_alive = get_background_thread_status()
        if bg_alive:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"NOT READY")

    def _handle_live(self):
        """Kubernetes-style liveness probe."""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        """Suppress HTTP access logs to avoid noise."""
        pass


class HealthServer:
    """Health check server wrapper."""

    def __init__(self, port: int = 8080):
        self.port = port
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the health server in a background thread."""
        try:
            self.server = HTTPServer(("0.0.0.0", self.port), HealthHandler)
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                name="HealthServer",
                daemon=True,
            )
            self.thread.start()
            logger.info("Health server started", extra={
                "event": "health_server_started",
                "port": self.port,
            })
        except OSError as e:
            logger.warning(f"Failed to start health server on port {self.port}: {e}", extra={
                "event": "health_server_failed",
                "port": self.port,
                "error": str(e),
            })

    def stop(self) -> None:
        """Stop the health server."""
        if self.server:
            self.server.shutdown()
            logger.info("Health server stopped", extra={"event": "health_server_stopped"})


def start_health_server(port: int = 8080) -> HealthServer:
    """
    Start the health check server.

    Args:
        port: Port to listen on (default: 8080)

    Returns:
        HealthServer instance
    """
    server = HealthServer(port=port)
    server.start()
    return server
