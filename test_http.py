
from pathfuzz import fetch_response

url = "http://127.0.0.1:8000/"

result = fetch_response(url)

print("HTTP response test")
print("------------------")

for key, value in result.items():
    print(f"{key}: {value}")
