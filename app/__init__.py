from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv
from flask_cors import CORS


def create_app():
    load_dotenv()

    app = Flask(__name__)
    CORS(app)

    from .routes import api
    app.register_blueprint(api)

    return app
