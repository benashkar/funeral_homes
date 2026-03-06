"""WSGI entry point for the CR Obituaries dashboard."""

from dashboard.app import create_app

app = create_app()
