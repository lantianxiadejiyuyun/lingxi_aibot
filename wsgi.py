"""生产入口：waitress-serve --port=8000 wsgi:app"""
from app import create_app

app = create_app()
