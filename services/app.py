import os
import secrets
from flask import Flask, send_from_directory
from dotenv import load_dotenv

from db.database import init_db
from routes import main_routes

# ✅ blueprint OAuth do Bling
from auth_routes import auth_routes

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# pasta downloads dentro do projeto
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")

# garante que existe
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

print("📁 Pasta de downloads:", DOWNLOADS_DIR)

def create_app():
    load_dotenv()

    app = Flask(__name__)

    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(16)

    # 🔥 MIGRAÇÃO AQUI
    with app.app_context():
        print("📦 Validando estrutura do Banco de Dados...")
        init_db()

    app.register_blueprint(main_routes)
    app.register_blueprint(auth_routes)

    print("\n===== ROTAS REGISTRADAS =====")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
        print(f"{rule.methods}  {rule}")
    print("=============================\n")

    return app

if __name__ == "__main__":
    if not os.path.exists("db"):
        os.makedirs("db")

    app = create_app()

    print("\n" + "=" * 40)
    print("🚀 NEXUS WMS: LHV-MED Online!")
    print("📍 URL: http://127.0.0.1:5000")
    print("=" * 40 + "\n")

    app.run(debug=True, host="127.0.0.1", port=5000)
