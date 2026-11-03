"""Local stand-in for buyerapi.shopgoodwill.com, in the shapes the old adapter
verified on 2026-08-14 (naive Pacific endTime, listingType, HTML shipping)."""
import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def end(hours):
    return (datetime.now(PT) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


ITEMS = {
    "canon sx110": [
        {"itemId": 101, "sellerId": 43, "sellerName": "Seller 43", "title": "Canon PowerShot SX110 IS 9MP Digital Camera Tested", "currentPrice": 14.0, "numBids": 3, "listingType": 0, "endTime": end(5), "imageURL": "26\\Items\\a.jpg"},
        {"itemId": 102, "sellerId": 43, "sellerName": "Seller 43", "title": "Canon SX110 IS For Parts Not Working", "currentPrice": 5.0, "numBids": 0, "listingType": 0, "endTime": end(30)},
        {"itemId": 103, "sellerId": 43, "sellerName": "Seller 43", "title": "Canon SX110 case only", "currentPrice": 2.0, "numBids": 0, "listingType": 0, "endTime": end(2)},
    ],
    "sony dsc-w7": [
        {"itemId": 201, "sellerId": 999, "sellerName": "Seller 999", "title": "Sony Cyber-shot DSC-W70 7.2MP", "currentPrice": 9.0, "numBids": 1, "listingType": 0, "endTime": end(8)},
        {"itemId": 202, "sellerId": 999, "sellerName": "Seller 999", "title": "Sony DSC-W7 Cybershot w/ battery", "currentPrice": 30.0, "numBids": 6, "listingType": 0, "endTime": end(1)},
    ],
    "kodak zi8": [
        {"itemId": 301, "sellerId": 999, "sellerName": "Seller 999", "title": "Lot of 3 Kodak Zi8 cameras", "currentPrice": 4.0, "numBids": 0, "listingType": 0, "endTime": end(50)},
    ],
    "ipod classic": [
        {"itemId": 401, "sellerId": 43, "sellerName": "Seller 43", "title": "Apple iPod Classic 160GB Black 7th Gen", "currentPrice": 20.0, "numBids": 2, "listingType": 0, "endTime": end(3)},
        {"itemId": 402, "sellerId": 43, "sellerName": "Seller 43", "title": "Apple iPod Nano Classic look case", "currentPrice": 3.0, "numBids": 0, "listingType": 0, "endTime": end(3)},
        {"itemId": 403, "sellerId": 43, "sellerName": "Seller 43", "title": "Apple iPod 5th gen video", "currentPrice": 8.0, "numBids": 0, "listingType": 0, "endTime": end(80)},
        {"itemId": 404, "sellerId": 8, "sellerName": "Seller 8", "title": "iPod Classic 80GB Silver", "currentPrice": 25.0, "numBids": 0, "listingType": 1, "buyNowPrice": 25.0, "endTime": end(100)},
    ],
}
SHIP = {101: (8.50, 3.49), 202: (9.0, 3.49), 401: (7.0, 3.0), 404: (6.0, 3.0)}  # 102, 301: no quote


SELLERS = {999: {"sellerCompanyName": "Goodwill of Testville", "pickupCity": "Dallas", "pickupState": "tx",
                  "pickupZip": "75201", "sellerAllowPickup": True,
                  "sellerItemProfile": "<p>Combined Shipping is Temporarily Unavailable</p>"}}
DETAIL_CALLS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        iid = int(self.path.rsplit("/", 1)[-1])
        sid = next((i.get("sellerId") for items in ITEMS.values() for i in items if i["itemId"] == iid), None)
        out = dict(SELLERS.get(sid, {}), sellerId=sid, itemId=iid)
        b = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/Search/ItemListing"):
            items = ITEMS.get(body["searchText"].lower(), []) if body["page"] == "1" else []
            out = {"searchResults": {"items": items, "itemCount": len(items)}, "categoryListModel": []}
        elif self.path.endswith("/CalculateShipping"):
            q = SHIP.get(body["itemId"])
            out = (f"<p>Shipping: <span id='shipping-span'>${q[0]:.2f} (GROUND_HOME_DELIVERY)</span></p>"
                   f"<p>Handling: ${q[1]:.2f}</p>") if q else "<p>Pickup only</p>"
        else:
            self.send_response(404); self.end_headers(); return
        b = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8099), H).serve_forever()
