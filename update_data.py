import requests
import json
from datetime import datetime, timezone

url = "https://data.sec.gov/submissions/CIK0001824920.json"

headers = {
    "User-Agent": "DzikiRadar research kontakt@example.com",
    "Accept-Encoding": "gzip, deflate",
}

response = requests.get(url, headers=headers, timeout=30)
response.raise_for_status()

data = {
    "spolka": "IONQ",
    "nazwa_sec": response.json().get("name"),
    "aktualizacja": datetime.now(timezone.utc).isoformat(),
    "status": "SEC odpowiada",
}

with open("radar_data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(data)
