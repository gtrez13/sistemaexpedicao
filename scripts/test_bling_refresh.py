from services.bling_service import BlingService
import json, os, time

b = BlingService()

t = b._load_tokens() or {}
old_access = (t.get("access_token") or "")[:25]
old_refresh_tail = (t.get("refresh_token") or "")[-8:]
old_exp = float(t.get("expires_at") or 0)

print("OLD access prefix:", old_access)
print("OLD refresh tail:", old_refresh_tail)
print("OLD expires_at:", old_exp, "in", int(old_exp - time.time()), "s")

print("\n--- force refresh ---")
refresh = t.get("refresh_token")
if not refresh:
    raise Exception("Sem refresh_token no bling_tokens.json")

b._refresh_access_token(refresh)

t2 = b._load_tokens() or {}
new_access = (t2.get("access_token") or "")[:25]
new_refresh_tail = (t2.get("refresh_token") or "")[-8:]
new_exp = float(t2.get("expires_at") or 0)

print("NEW access prefix:", new_access)
print("NEW refresh tail:", new_refresh_tail)
print("NEW expires_at:", new_exp, "in", int(new_exp - time.time()), "s")

print("\n--- smoke call ---")
r = b._get("/situacoes/modulos", timeout=(10, 30))
print("STATUS:", r.status_code)
print("BODY:", (r.text or "")[:300])