import time, urllib.request, datetime

url = "https://gridwise-api-8b23.onrender.com/health"
print(f"[{datetime.datetime.now()}] Starting keep-alive daemon for {url}")

while True:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GridWise-KeepAlive/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[{datetime.datetime.now()}] Ping status: {resp.status}")
    except Exception as e:
        print(f"[{datetime.datetime.now()}] Ping error: {e}")
    time.sleep(240)  # Ping every 4 minutes (Render sleeps at 15 mins)
