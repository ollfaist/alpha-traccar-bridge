from flask import Flask, Response
import requests

app = Flask(__name__)

LANTMATERIET_URL = (
    "https://maps.lantmateriet.se/open/topowebb-ccby/v1/wmts/1.0.0"
    "/topowebb/default/3857/{z}/{y}/{x}.png"
)

@app.route("/<int:z>/<int:x>/<int:y>.png")
def tile(z, x, y):
    url = LANTMATERIET_URL.format(z=z, x=x, y=y)
    r = requests.get(url, timeout=10)
    return Response(r.content, content_type="image/png", status=r.status_code)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8085)
