import json
import os
import sqlite3
from contextlib import closing

import click
import stripe
from flask import Flask, abort, current_app, flash, redirect, render_template, request, session, url_for

FREE_SHIPPING_OVER = 40
SHIPPING = 6


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    slug TEXT PRIMARY KEY, name TEXT NOT NULL, roast TEXT NOT NULL, price INTEGER NOT NULL,
    color TEXT NOT NULL, notes TEXT NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stock (slug TEXT PRIMARY KEY, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS processed_sessions (id TEXT PRIMARY KEY);
"""


def db():
    return sqlite3.connect(current_app.config["DB_PATH"])


def catalog():
    with closing(db()) as conn:
        conn.row_factory = sqlite3.Row
        return {r["slug"]: dict(r) for r in conn.execute("SELECT * FROM products ORDER BY rowid")}


def stock_levels():
    with closing(db()) as conn:
        return dict(conn.execute("SELECT slug, qty FROM stock"))


def available(slug, levels):
    return max(0, levels.get(slug, 0))  # no stock row = Sold Out


def cap_message(slug, avail, products):
    name = products[slug]["name"]
    return f"{name} is sold out." if not avail else f"Only {avail} bags of {name} available."


def cart_summary():
    items, products = [], catalog()
    for slug, qty in session.get("cart", {}).items():
        if slug in products:
            p = products[slug]
            items.append({"slug": slug, "qty": qty, "total": p["price"] * qty, **p})
    subtotal = sum(i["total"] for i in items)
    shipping = 0 if not items or subtotal >= FREE_SHIPPING_OVER else SHIPPING
    return {"items": items, "subtotal": subtotal, "shipping": shipping, "total": subtotal + shipping}


def create_app(db_path=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev")
    app.config["DB_PATH"] = db_path or os.environ.get("DB_PATH", "shop.db")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    with app.app_context(), closing(db()) as conn:
        conn.executescript(SCHEMA)

    @app.context_processor
    def inject_globals():
        return {"cart_count": sum(session.get("cart", {}).values()), "stock": stock_levels()}

    @app.route("/")
    def index():
        return render_template("index.html", products=list(catalog().items())[:4])

    @app.route("/products")
    def products():
        return render_template("products.html", products=catalog().items())

    @app.route("/products/<slug>")
    def product_detail(slug):
        product = catalog().get(slug) or abort(404)
        return render_template("product_detail.html", slug=slug, product=product)

    @app.route("/cart")
    def cart():
        return render_template("cart.html", **cart_summary())

    @app.post("/cart/<slug>")
    def cart_update(slug):
        products = catalog()
        if slug not in products:
            abort(404)
        cart = session.get("cart", {})
        qty = max(0, min(99, request.form.get("qty", 1, type=int)))
        if request.form.get("mode") == "add":
            qty += cart.get(slug, 0)
        avail = available(slug, stock_levels())
        if qty > avail:
            qty = avail
            flash(cap_message(slug, avail, products))
        if qty:
            cart[slug] = qty
        else:
            cart.pop(slug, None)
        session["cart"] = cart
        return redirect(url_for("cart"))

    @app.post("/checkout")
    def checkout():
        summary = cart_summary()
        if not summary["items"]:
            return redirect(url_for("cart"))
        # Stock may have dropped while the Cart sat there: cap and send back to the Cart.
        # ponytail: oversell possible between this check and payment, reserve on session create if it happens
        levels, cart, capped = stock_levels(), session["cart"], False
        for slug, qty in list(cart.items()):
            avail = available(slug, levels)
            if qty > avail:
                capped = True
                flash(cap_message(slug, avail, catalog()))
                if avail:
                    cart[slug] = avail
                else:
                    del cart[slug]
        if capped:
            session["cart"] = cart
            return redirect(url_for("cart"))

        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"quantity": i["qty"], "price_data": {
                "currency": "usd", "unit_amount": i["price"] * 100, "product_data": {"name": i["name"]}}}
                for i in summary["items"]],
            shipping_address_collection={"allowed_countries": ["US"]},
            shipping_options=[{"shipping_rate_data": {
                "type": "fixed_amount", "display_name": "Free shipping" if not summary["shipping"] else "Standard",
                "fixed_amount": {"amount": summary["shipping"] * 100, "currency": "usd"}}}],
            metadata={i["slug"]: i["qty"] for i in summary["items"]},  # read back by the webhook
            success_url=url_for("success", _external=True),
            cancel_url=url_for("cart", _external=True),
        )
        return redirect(checkout_session.url, code=303)

    @app.route("/success")
    def success():
        session.pop("cart", None)
        return render_template("success.html")

    @app.post("/webhook")
    def webhook():
        payload = request.get_data(as_text=True)
        try:
            stripe.WebhookSignature.verify_header(
                payload, request.headers.get("Stripe-Signature", ""), os.environ["STRIPE_WEBHOOK_SECRET"])
        except stripe.SignatureVerificationError:
            abort(400)
        event = json.loads(payload)
        obj = event["data"]["object"]
        if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded") \
                and obj["payment_status"] == "paid":
            with closing(db()) as conn, conn:
                # Stripe delivers at least once: the primary key makes a retry a no-op.
                if conn.execute("INSERT OR IGNORE INTO processed_sessions VALUES (?)", (obj["id"],)).rowcount:
                    conn.executemany("UPDATE stock SET qty = qty - ? WHERE slug = ?",
                                     [(int(q), slug) for slug, q in obj["metadata"].items()])
        return "", 200

    @app.cli.command("restock")
    @click.argument("slug")
    @click.argument("qty", type=click.IntRange(min=1))
    def restock(slug, qty):
        """Add QTY arrived bags to SLUG's Stock."""
        if slug not in catalog():
            raise click.BadParameter(f"unknown product {slug!r}", param_hint="SLUG")
        with closing(db()) as conn, conn:
            conn.execute("INSERT INTO stock VALUES (?, ?) ON CONFLICT(slug) DO UPDATE SET qty = qty + excluded.qty",
                         (slug, qty))
            click.echo(f"{slug}: {conn.execute('SELECT qty FROM stock WHERE slug = ?', (slug,)).fetchone()[0]}")

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
