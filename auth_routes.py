import os
import json
import base64
import secrets
import urllib.parse
import requests

from flask import Blueprint, session, redirect, request, jsonify
from dotenv import load_dotenv

load_dotenv()

auth_routes = Blueprint("auth_routes", __name__)

AUTH_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"
TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


@auth_routes.get("/auth/login")
def bling_login():
    client_id = (os.getenv("BLING_CLIENT_ID") or "").strip()
    redirect_uri = (os.getenv("BLING_REDIRECT_URI") or "").strip()

    if not client_id or not redirect_uri:
        return jsonify({
            "success": False,
            "error": "missing_env",
            "detail": "BLING_CLIENT_ID/BLING_REDIRECT_URI ausentes"
        }), 400

    # ✅ state obrigatório
    state = secrets.token_hex(16)
    session["bling_oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }

    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    return redirect(url)


from flask import request
import base64, requests, json

@auth_routes.get("/auth/callback")
def bling_callback():
    if request.args.get("error"):
        return jsonify({
            "success": False,
            "error": request.args.get("error"),
            "error_description": request.args.get("error_description"),
        }), 400

    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()

    expected = session.get("bling_oauth_state")
    if not expected or not state or state != expected:
        return jsonify({"success": False, "error": "state inválido/ausente"}), 400

    client_id = (os.getenv("BLING_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("BLING_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.getenv("BLING_REDIRECT_URI") or "").strip()

    headers = {
        "Authorization": f"Basic {_basic_auth(client_id, client_secret)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=(10, 60))
    if r.status_code not in (200, 201):
        return jsonify({"success": False, "error": "token_exchange_failed", "detail": r.text}), 400

    tokens = r.json() or {}

    out_file = os.path.join(os.path.dirname(__file__), "bling_tokens.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)

    return jsonify({"success": True, "saved_to": out_file})