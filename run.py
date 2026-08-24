"""GemmaSim entry point. Run with: python run.py"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    host = app.config.get("HOST", "127.0.0.1")
    port = int(app.config.get("PORT", 5000))
    debug = bool(app.config.get("DEBUG", False))
    app.run(host=host, port=port, debug=debug, threaded=True)
