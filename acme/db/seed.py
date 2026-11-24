"""
Seed the Acme Store database with realistic, reproducible synthetic data.

Usage:
    python acme/db/seed.py

Re-running is safe: it wipes all tables and regenerates the same data.
Dates are relative to "now", so refund-window logic always works when you demo it.
"""

import os
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
from dotenv import load_dotenv
from faker import Faker

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://acme:acme@localhost:5432/acme")

SEED = 42
N_CUSTOMERS = 300
N_ORDERS = 1000
N_TICKETS = 150

fake = Faker()
Faker.seed(SEED)
random.seed(SEED)
NOW = datetime.now(timezone.utc)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


# ---------------------------------------------------------------------------
# Product catalog
# ---------------------------------------------------------------------------
CATALOG = {
    "Electronics": [
        ("Wireless Earbuds", 49.99), ("Bluetooth Speaker", 79.99), ("USB-C Charger 65W", 34.99),
        ("Mechanical Keyboard", 119.00), ("Wireless Mouse", 29.99), ("Smart Watch", 199.00),
        ("Noise-Cancelling Headphones", 249.00), ("Portable SSD 1TB", 109.00),
        ("Webcam 1080p", 59.99), ("Power Bank 20000mAh", 44.99),
    ],
    "Home & Kitchen": [
        ("Stainless Steel Water Bottle", 24.99), ("Ceramic Coffee Mug Set", 32.00),
        ("Non-Stick Frying Pan", 39.99), ("Electric Kettle", 54.99), ("Chef's Knife", 69.00),
        ("Bamboo Cutting Board", 27.50), ("Air Fryer", 129.00), ("French Press", 35.00),
    ],
    "Clothing": [
        ("Cotton T-Shirt", 19.99), ("Denim Jacket", 89.00), ("Running Shoes", 119.99),
        ("Wool Sweater", 74.00), ("Rain Jacket", 99.00), ("Baseball Cap", 22.00),
        ("Leather Belt", 39.00), ("Hiking Socks (3-pack)", 18.99),
    ],
    "Books": [
        ("Intro to Machine Learning", 45.00), ("The Pragmatic Engineer", 38.00),
        ("Cooking for Busy People", 29.99), ("World History Atlas", 55.00),
        ("Mindful Productivity", 24.00), ("Python in Practice", 49.00),
    ],
    "Sports & Outdoors": [
        ("Yoga Mat", 34.99), ("Adjustable Dumbbells", 179.00), ("Camping Tent (2-person)", 149.00),
        ("Resistance Bands Set", 27.99), ("Cycling Helmet", 64.99), ("Insulated Lunch Bag", 29.00),
    ],
}

TICKET_SUBJECTS = [
    ("Where is my order?", "medium"),
    ("Item arrived damaged", "high"),
    ("Wrong item received", "high"),
    ("Request for refund", "medium"),
    ("How do I return an item?", "low"),
    ("Payment charged twice", "high"),
    ("Change delivery address", "medium"),
    ("Product not working", "medium"),
    ("Question about warranty", "low"),
    ("Cancel my order", "medium"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def insert_customer(cur, name, email, tier="standard", created=None) -> int:
    cur.execute(
        """INSERT INTO customers (name, email, phone, city, country, tier, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (name, email, fake.phone_number(), fake.city(), fake.country(), tier,
         created or days_ago(random.randint(200, 900))),
    )
    return cur.fetchone()[0]


def insert_order(cur, customer_id, product_ids, status, created, shipped=None, delivered=None) -> int:
    cur.execute(
        """INSERT INTO orders (customer_id, status, created_at, shipped_at, delivered_at)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (customer_id, status, created, shipped, delivered),
    )
    order_id = cur.fetchone()[0]
    total = Decimal("0")
    for pid in product_ids:
        cur.execute("SELECT price FROM products WHERE id = %s", (pid,))
        price = cur.fetchone()[0]
        qty = random.choice([1, 1, 1, 2])
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (%s, %s, %s, %s)",
            (order_id, pid, qty, price),
        )
        total += price * qty
    cur.execute("UPDATE orders SET total = %s WHERE id = %s", (total, order_id))
    if status == "refunded":
        cur.execute(
            "INSERT INTO refunds (order_id, amount, reason, status, created_at) VALUES (%s, %s, %s, 'completed', %s)",
            (order_id, total, "Customer requested refund", (delivered or created) + timedelta(days=3)),
        )
    return order_id


def insert_ticket(cur, customer_id, order_id, subject, priority, status, created):
    cur.execute(
        """INSERT INTO tickets (customer_id, order_id, subject, description, status, priority, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (customer_id, order_id, subject, fake.sentence(nb_words=14), status, priority,
         created, min(created + timedelta(hours=random.randint(1, 72)), NOW)),
    )


def lifecycle(age_days: float):
    """Pick a realistic status and timestamps for an order of a given age."""
    created = days_ago(age_days)
    roll = random.random()
    if roll < 0.05:
        return "cancelled", created, None, None
    if age_days < 2:
        return random.choice(["pending", "paid"]), created, None, None
    shipped = created + timedelta(days=random.uniform(0.5, 2))
    if age_days < 6:
        return "shipped", created, shipped, None
    delivered = shipped + timedelta(days=random.uniform(2, 6))
    if roll < 0.09:
        return "refunded", created, shipped, delivered
    return "delivered", created, shipped, delivered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        print("Clearing existing data...")
        cur.execute("TRUNCATE tool_calls, audit_log, tickets, refunds, order_items, orders, products, customers RESTART IDENTITY CASCADE")

        # Products
        print("Creating products...")
        product_ids = []
        for category, items in CATALOG.items():
            prefix = category[:3].upper()
            for i, (name, price) in enumerate(items, start=1):
                cur.execute(
                    "INSERT INTO products (sku, name, category, price, stock) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (f"{prefix}-{i:03d}", name, category, Decimal(str(price)), random.randint(0, 250)),
                )
                product_ids.append(cur.fetchone()[0])

        # --- Planted demo customers: known edge cases for demos and evals ---
        print("Creating demo customers with known edge cases...")
        sarah = insert_customer(cur, "Sarah Khan", "sarah.khan@example.com", "standard")
        ali = insert_customer(cur, "Ali Raza", "ali.raza@example.com", "premium")
        john = insert_customer(cur, "John Miller", "john.miller@example.com", "standard")

        # Sarah: one order inside the 30-day refund window, one outside it
        d = days_ago(10)
        insert_order(cur, sarah, [product_ids[0]], "delivered", d - timedelta(days=5), d - timedelta(days=4), d)
        d = days_ago(45)
        insert_order(cur, sarah, [product_ids[6]], "delivered", d - timedelta(days=5), d - timedelta(days=4), d)

        # Ali (premium): delivered 50 days ago -> still refundable under the 60-day premium window
        d = days_ago(50)
        ali_order = insert_order(cur, ali, [product_ids[5], product_ids[2]], "delivered",
                                 d - timedelta(days=4), d - timedelta(days=3), d)
        for subj, prio in [("Product not working", "medium"), ("Request for refund", "medium"),
                           ("Question about warranty", "low")]:
            insert_ticket(cur, ali, ali_order, subj, prio, "open", days_ago(random.randint(1, 5)))

        # John: shipped 12 days ago, still not delivered (late), plus an already-refunded order
        insert_order(cur, john, [product_ids[13]], "shipped", days_ago(13), days_ago(12))
        d = days_ago(30)
        insert_order(cur, john, [product_ids[20]], "refunded", d - timedelta(days=5), d - timedelta(days=4), d)

        # Random customers
        print(f"Creating {N_CUSTOMERS} customers...")
        customer_ids = [sarah, ali, john]
        for _ in range(N_CUSTOMERS):
            tier = "premium" if random.random() < 0.15 else "standard"
            customer_ids.append(insert_customer(cur, fake.name(), fake.unique.safe_email(), tier))

        # Random orders
        print(f"Creating {N_ORDERS} orders...")
        orders_by_customer: dict[int, list[int]] = {}
        for _ in range(N_ORDERS):
            cid = random.choice(customer_ids[3:])
            status, created, shipped, delivered = lifecycle(random.uniform(0, 180))
            items = random.sample(product_ids, k=random.randint(1, 4))
            oid = insert_order(cur, cid, items, status, created, shipped, delivered)
            orders_by_customer.setdefault(cid, []).append(oid)

        # Random tickets
        print(f"Creating {N_TICKETS} support tickets...")
        for _ in range(N_TICKETS):
            cid = random.choice(list(orders_by_customer))
            oid = random.choice(orders_by_customer[cid]) if random.random() < 0.7 else None
            subject, priority = random.choice(TICKET_SUBJECTS)
            status = random.choices(["open", "in_progress", "resolved"], weights=[3, 2, 5])[0]
            insert_ticket(cur, cid, oid, subject, priority, status, days_ago(random.uniform(0, 90)))

        conn.commit()

        # Summary
        for table in ["customers", "products", "orders", "order_items", "refunds", "tickets"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  {table:<12} {cur.fetchone()[0]:>5}")
    print("Done. Acme Store is open for business.")


if __name__ == "__main__":
    main()
