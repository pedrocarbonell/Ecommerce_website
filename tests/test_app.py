import json
import sqlite3
from types import SimpleNamespace

import stripe

from app import create_app


def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    app = create_app(db_path=str(tmp_path / "shop.db"))
    with sqlite3.connect(app.config["DB_PATH"]) as conn:
        conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)", [
            ("daybreak", "Daybreak", "Light roast · Ethiopia", 18, "#f9ac1c", "Blueberry, jasmine", "Bright."),
            ("bunker", "Bunker", "Decaf · Mexico", 17, "#e06d2d", "Milk chocolate", "Decaf.")])
    app.test_cli_runner().invoke(args=["restock", "daybreak", "5"])
    return app


def stock(app, slug):
    return sqlite3.connect(app.config["DB_PATH"]).execute("SELECT qty FROM stock WHERE slug = ?", (slug,)).fetchone()


def test_pages_and_cart_flow(tmp_path, monkeypatch):
    client = make_app(tmp_path, monkeypatch).test_client()
    for url in ["/", "/products", "/products/daybreak", "/cart"]:
        assert client.get(url).status_code == 200
    assert client.post("/checkout").status_code == 302  # empty cart

    client.post("/cart/daybreak", data={"qty": 2, "mode": "add"})
    client.post("/cart/daybreak", data={"qty": 1, "mode": "add"})
    page = client.get("/cart").get_data(as_text=True)
    assert "$54" in page and "Free" in page  # 3 × $18, free shipping over $40

    client.post("/cart/daybreak", data={"qty": 0})
    assert "Your cart is empty" in client.get("/cart").get_data(as_text=True)


def test_stock_caps_cart_and_sold_out(tmp_path, monkeypatch):
    client = make_app(tmp_path, monkeypatch).test_client()
    client.post("/cart/daybreak", data={"qty": 9, "mode": "add"})
    page = client.get("/cart").get_data(as_text=True)
    assert "Only 5 bags of Daybreak available." in page and 'value="5"' in page

    client.post("/cart/bunker", data={"qty": 1, "mode": "add"})  # no stock row = Sold Out
    assert "Bunker is sold out." in client.get("/cart").get_data(as_text=True)
    assert "Sold out" in client.get("/products/bunker").get_data(as_text=True)


def test_checkout_recaps_stale_cart_then_creates_session(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client = app.test_client()
    client.post("/cart/daybreak", data={"qty": 5, "mode": "add"})
    with sqlite3.connect(app.config["DB_PATH"]) as conn:
        conn.execute("UPDATE stock SET qty = 2")  # stock dropped while the cart sat there
    resp = client.post("/checkout")
    assert resp.location.endswith("/cart") and "Only 2 bags" in client.get("/cart").get_data(as_text=True)

    created = {}
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda **kw: created.update(kw) or SimpleNamespace(url="https://stripe.test/pay"))
    resp = client.post("/checkout")
    assert resp.status_code == 303 and resp.location == "https://stripe.test/pay"
    assert created["line_items"][0]["quantity"] == 2 and created["line_items"][0]["price_data"]["unit_amount"] == 1800
    assert created["shipping_options"][0]["shipping_rate_data"]["fixed_amount"]["amount"] == 600  # $36 < $40
    assert created["metadata"] == {"daybreak": 2}

    client.get("/success")
    assert "Your cart is empty" in client.get("/cart").get_data(as_text=True)


def test_webhook_decrements_once(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client = app.test_client()
    assert client.post("/webhook", data="{}", headers={"Stripe-Signature": "bad"}).status_code == 400

    monkeypatch.setattr(stripe.WebhookSignature, "verify_header", lambda *a: True)
    event = json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "payment_status": "paid", "metadata": {"daybreak": "3"}}}})
    for _ in range(2):  # Stripe retry
        assert client.post("/webhook", data=event).status_code == 200
    assert stock(app, "daybreak") == (2,)

    app.test_cli_runner().invoke(args=["restock", "daybreak", "10"])
    assert stock(app, "daybreak") == (12,)  # restock adds
    assert app.test_cli_runner().invoke(args=["restock", "nope", "1"]).exit_code != 0
