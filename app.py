import os
import sqlite3
import hashlib
import json
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

DB_PATH = "database.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            subscribed INTEGER DEFAULT 0,
            ls_customer_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            property_address TEXT,
            property_type TEXT,
            bedrooms INTEGER,
            bathrooms INTEGER,
            square_feet INTEGER,
            price REAL,
            description TEXT,
            listing_text TEXT,
            instagram_caption TEXT,
            email_blast TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

init_db()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = hashlib.sha256(request.form["password"].encode()).hexdigest()
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ? AND password = ?", (email, password)).fetchone()
        conn.close()
        if user:
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["subscribed"] = user["subscribed"]
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid email or password")
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = hashlib.sha256(request.form["password"].encode()).hexdigest()
        conn = get_db()
        try:
            conn.execute("INSERT INTO users (name, email, password) VALUES (?, ?, ?)", (name, email, password))
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["subscribed"] = 0
            conn.close()
            return redirect(url_for("pricing"))
        except sqlite3.IntegrityError:
            conn.close()
            return render_template("login.html", error="Email already registered")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/pricing")
@login_required
def pricing():
    return render_template("pricing.html", ls_api_key=os.getenv("LEMON_SQUEEZY_API_KEY", ""))

@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    gens = conn.execute("SELECT * FROM generations WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (session["user_id"],)).fetchall()
    conn.close()
    return render_template("dashboard.html", generations=gens, subscribed=session.get("subscribed", 0))

@app.route("/create-checkout", methods=["POST"])
@login_required
def create_checkout():
    api_key = os.getenv("LEMON_SQUEEZY_API_KEY")
    store_id = os.getenv("LEMON_SQUEEZY_STORE_ID")
    variant_id = os.getenv("LEMON_SQUEEZY_VARIANT_ID")

    if not api_key or not store_id or not variant_id:
        return jsonify({"error": "Payment not configured. Contact admin."}), 500

    import urllib.request

    data = json.dumps({
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "custom": {"user_id": str(session["user_id"])},
                    "email": session.get("user_email", ""),
                }
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": store_id}},
                "variant": {"data": {"type": "variants", "id": variant_id}},
            }
        }
    }).encode()

    req = urllib.request.Request(
        "https://api.lemonsqueezy.com/v1/checkouts",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read())
        url = result["data"]["attributes"]["url"]
        return jsonify({"url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ls-webhook", methods=["POST"])
def ls_webhook():
    api_key = os.getenv("LEMON_SQUEEZY_API_KEY")
    body = request.get_data(as_text=True)
    sig = request.headers.get("X-Signature", "")

    import hmac
    expected = hmac.new(os.getenv("LEMON_SQUEEZY_WEBHOOK_SECRET", "").encode(), body.encode(), hashlib.sha256).hexdigest()
    if sig != expected:
        return "", 401

    event = json.loads(body)
    if event.get("meta", {}).get("event_name") == "order_created":
        custom = event["data"]["attributes"]["first_order"]["custom"]
        user_id = custom.get("user_id")
        if user_id:
            conn = get_db()
            conn.execute("UPDATE users SET subscribed = 1 WHERE id = ?", (user_id,))
            conn.commit()
            conn.close()
    return "", 200

@app.route("/generate", methods=["POST"])
@login_required
def generate():
    if not session.get("subscribed"):
        return jsonify({"error": "Please subscribe to generate content"}), 403

    data = request.json
    prompt = f"""Write real estate marketing content for a property:

Address: {data.get('address', 'N/A')}
Type: {data.get('property_type', 'N/A')}
Bedrooms: {data.get('beds', 'N/A')}
Bathrooms: {data.get('baths', 'N/A')}
Square Feet: {data.get('sqft', 'N/A')}
Price: ${data.get('price', 'N/A')}

Generate:
1. A professional MLS listing description (3 paragraphs)
2. An Instagram caption with emojis and hashtags
3. A short email blast for potential buyers

Format your response as JSON with keys: listing, instagram, email"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        result = response.choices[0].message.content
        parsed = json.loads(result)

        conn = get_db()
        conn.execute("""INSERT INTO generations
            (user_id, property_address, property_type, bedrooms, bathrooms, square_feet, price, listing_text, instagram_caption, email_blast)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session["user_id"], data.get("address"), data.get("property_type"),
             data.get("beds"), data.get("baths"), data.get("sqft"), data.get("price"),
             parsed.get("listing", ""), parsed.get("instagram", ""), parsed.get("email", "")))
        conn.commit()
        conn.close()
        return jsonify(parsed)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
