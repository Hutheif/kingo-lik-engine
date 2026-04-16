"""
update_ngrok.py
Run this AFTER starting ngrok to update your .env automatically.

Usage:
    1. Start ngrok:  ngrok http 5000
    2. Run this:     python update_ngrok.py
    3. Start Flask:  python app.py

Or if you know the URL:
    python update_ngrok.py https://abc123.ngrok-free.app
"""

import sys, re, os

def get_ngrok_url():
    """Reads the ngrok URL from the ngrok local API."""
    if len(sys.argv) > 1:
        url = sys.argv[1].strip().rstrip("/")
        if url.startswith("https://"):
            return url
        print(f"ERROR: URL must start with https://. Got: {url}")
        return None
    try:
        import urllib.request, json
        data = json.loads(urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3).read())
        for tunnel in data.get("tunnels", []):
            if tunnel.get("proto") == "https":
                return tunnel["public_url"].rstrip("/")
        print("No https tunnel found in ngrok. Start ngrok first: ngrok http 5000")
        return None
    except Exception as e:
        print(f"Cannot reach ngrok API: {e}")
        print("Make sure ngrok is running: ngrok http 5000")
        print("Or pass URL directly: python update_ngrok.py https://your-url.ngrok-free.app")
        return None


def update_env(url):
    env_path = ".env"
    if not os.path.exists(env_path):
        print(f"ERROR: .env not found in {os.getcwd()}")
        return False
    
    content = open(env_path).read()
    
    # Replace BASE_URL line
    new_content = re.sub(
        r'^BASE_URL=.*$',
        f'BASE_URL={url}',
        content,
        flags=re.MULTILINE
    )
    
    if new_content == content:
        print("NOTE: BASE_URL line not found in .env — adding it")
        new_content = content.rstrip() + f"\nBASE_URL={url}\n"
    
    open(env_path, 'w').write(new_content)
    return True


def main():
    url = get_ngrok_url()
    if not url:
        return
    
    print(f"\nngrok URL detected: {url}")
    
    # Update .env
    if update_env(url):
        print(f"✅ .env updated: BASE_URL={url}")
    
    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NOW update Africa's Talking dashboard:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Go to: africastalking.com → Voice → +254711082547 → Edit
   Voice callback URL: {url}/voice/answer

2. Go to: AT → USSD → *789*1990# → Edit
   Callback URL: {url}/ussd

3. Go to: AT → SMS → Incoming → Settings
   Callback URL: {url}/sms

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Test call: {url}/test/call/+254714137554
Check config: {url}/test/at-config
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Then start Flask: python app.py
""")


if __name__ == "__main__":
    main()