import httpx
import time
import os

BASE_URL = "http://127.0.0.1:8000"
TEST_USER_EMAIL = "test_lifecycle@example.com"
TEST_USER_PASSWORD = "StrongPassword123!"

# You'll need an admin token to test admin routes.
# If you don't have one hardcoded, we will just test the auth flows (register -> delete)
# and try to simulate the admin flows if possible.
# For this script, we'll login as an admin if we know the credentials.
# Assuming admin@lumivst.com exists based on typical setup.
ADMIN_EMAIL = "admin@lumi.com" # Just guessing a common admin email, will fail gracefully if wrong
ADMIN_PASSWORD = "admin" # Update if you know the local dev admin password

def print_step(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] 🚀 {msg}")

def get_csrf_token(client):
    res = client.get("/api/auth/csrf")
    res.raise_for_status()
    return res.json()["csrf_token"]

def test_lifecycle():
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        csrf_token = get_csrf_token(client)
        headers = {
            "x-csrf-token": csrf_token,
            "origin": "http://localhost:3000",
            "referer": "http://localhost:3000/"
        }
        
        print_step(f"1. Registering new user: {TEST_USER_EMAIL}")
        res = client.post("/api/auth/register", json={
            "email": TEST_USER_EMAIL,
            "password": TEST_USER_PASSWORD,
            "full_name": "Test User"
        }, headers=headers)
        
        if res.status_code == 400 and "مستخدم بالفعل" in res.text:
            print("User already exists, attempting to login and delete it first.")
            # Login to get token and delete it
            # Need to pass CSRF for login (it's double-submit, but usually login is an exception or requires it too in this app)
            res_login = client.post("/api/auth/login", json={"email": TEST_USER_EMAIL, "password": TEST_USER_PASSWORD}, headers=headers)
            if res_login.status_code == 200:
                token = res_login.json()["access_token"]
                client.delete("/api/auth/delete-account", headers={"Authorization": f"Bearer {token}", "x-csrf-token": csrf_token})
                print("Old test user deleted. Re-registering...")
                res = client.post("/api/auth/register", json={
                    "email": TEST_USER_EMAIL,
                    "password": TEST_USER_PASSWORD,
                    "full_name": "Test User"
                }, headers=headers)
        
        print(f"Register Response: {res.status_code} - {res.text}")
        
        print_step("2. Attempting to login (Should fail with Pending status 403)")
        res_login = client.post("/api/auth/login", json={"email": TEST_USER_EMAIL, "password": TEST_USER_PASSWORD}, headers=headers)
        print(f"Login Response: {res_login.status_code} - {res_login.text}")
        assert res_login.status_code == 403, "Expected 403 Forbidden for pending user"
        assert "موافقة الإدارة" in res_login.text or "pending" in res_login.text.lower(), "Expected pending approval message"
        
        print_step("✅ Registration starts as Pending!")
        
        print("\n🎉 Lifecycle Test (Phase 1: Registration is secure) completed successfully!")
        print("To test the full Admin Revoke/Delete, log in as an Admin in the UI and try deleting the 'test_lifecycle@example.com' user.")

if __name__ == "__main__":
    try:
        test_lifecycle()
    except Exception as e:
        print(f"❌ Error during test: {e}")
