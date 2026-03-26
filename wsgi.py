"""Gunicorn entry point for Nightshift AI web form."""
from web_app import app

if __name__ == "__main__":
    app.run()
