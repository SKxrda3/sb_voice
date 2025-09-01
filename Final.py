import os
import argparse
import json
import re
import tempfile
import mysql.connector
import speech_recognition as sr
from gtts import gTTS
from playsound import playsound
from typing import List, Dict, Any, Tuple, Optional
from dotenv import load_dotenv
from rapidfuzz import process as fuzz_process
from word2number import w2n
from flask import request


word_to_digit = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20"
}

# ─────────────────────────── CONFIG ───────────────────────────
load_dotenv()
recognizer = sr.Recognizer()

# ─────────────────── GLOBAL DATABASE CONNECTION ───────────────────
_db_connection = None

def get_db_connection():
    global _db_connection
    if _db_connection is None or not _db_connection.is_connected():
        try:
            _db_connection = mysql.connector.connect(
                host=os.getenv("DB_HOST"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                database=os.getenv("DB_NAME"),
                autocommit=True,
                charset="utf8mb4",
                use_pure=True
            ) 
        except mysql.connector.Error as e:
            print(f"Database connection failed: {e}")
            return None
    return _db_connection


# ───────────────────────── TTS / STT ──────────────────────────
def speak(text: str) -> None:
    print(f"\nAssistant: {text}")
    try:
        tts = gTTS(text=text, lang="en", tld="co.in")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            path = fp.name.replace("\\", "/")
        tts.save(path)
        playsound(path)
        os.remove(path)
    except Exception as e:
        print(f"[ERROR] TTS failed: {e}")

#------------------------------
def normalize_choice(choice: str) -> str:
    choice = choice.lower().strip()
    if choice.isdigit() and 0 <= int(choice) <= 9:
        return choice
    return word_to_digit.get(choice, "")

#------------------------------------
def ask_boolean_question(question_text: str, max_retries: int = 2) -> Optional[bool]:
    """
    Asks a yes/no boolean question using voice and returns True/False or None.
    """
    positive_phrases = ["yes", "yeah", "yup", "i want", "sure", "of course", "absolutely", "okay", "add", "include"]
    negative_phrases = ["no", "nope", "don't", "not", "skip", "without", "exclude", "remove"]

    for attempt in range(max_retries):
        speak(question_text + " (yes or no)")
        ans = listen().lower()
        if any(p in ans for p in positive_phrases) or any(n in ans for n in negative_phrases):
            return any(p in ans for p in positive_phrases) and not any(n in ans for n in negative_phrases)
        speak("Sorry, please answer yes or no.")

    # Fallback if no valid response
    fallback = input("Fallback (type yes/no): ").strip().lower()
    if any(p in fallback for p in positive_phrases):
        return True
    elif any(n in fallback for n in negative_phrases):
        return False
    return None

#-----------------------------------------------
def extract_quantity(text: str) -> int:
    
    # Try direct digits
    digit_match = re.search(r"\b\d+\b", text)
    if digit_match:
        return int(digit_match.group())

    # Try word-to-number conversion
    try:
        return w2n.word_to_num(text)
    except:
        return 1  # Default quantity if not found


# ------------------------------------------------
def process_order(user_input: str, item: Dict[str, Any]) -> Dict[str, Any]:
    
    if 'quantity' not in item or item['quantity'] is None:
        quantity = extract_quantity(user_input)
        if quantity is not None:
            item['quantity'] = quantity
    return item
#------------------------------------------------
def listen() -> str:
    with sr.Microphone() as source:
        print("Listening...")
        recognizer.adjust_for_ambient_noise(source)
        try:
            audio = recognizer.listen(source, timeout=2)
            query = recognizer.recognize_google(audio)
            print(f"You: {query}")
            return query
        except (sr.WaitTimeoutError, sr.UnknownValueError, sr.RequestError):
            speak("Sorry, I couldn't understand that.")
            return input("Fallback (type your input): ")


# def listen() -> str:
#     return request.json.get("user_input", "")


#------------------------------------
def ensure_mysql_connection_alive(obj):
    try:
        conn = getattr(obj, "connection", None) or getattr(obj, "_connection", None) or obj
        conn.ping(reconnect=True, attempts=3, delay=2)
    except mysql.connector.Error as e:
        print(f"[MySQL Warning] Lost connection. Attempting to reconnect... ({e})")
        raise

#-------------------------------------


def get_user_name(cur, id: int) -> str:
    cur.execute("SELECT name FROM tbl_user WHERE id = %s AND ustatus = 1;", (id,))
    row = cur.fetchone()
    return row["name"] if row and row["name"] else "Customer"

#-----------------------------------
def confirm_order(item_name: str, qty: int, variations: Optional[Dict[str, Any]] = None):
    item_display = f"{item_name}{'s' if int(qty) > 1 and not item_name.endswith('s') else ''}"
    if variations:
        summary_text = _get_variation_summary(variations)
        print(f"Got it. You want {qty} {item_display} with {summary_text}")
    else:
        print(f"Got it. You want {qty} {item_display}.") 

# ----------------------------
def _get_variation_summary(variations: Dict[str, Any]) -> str:
    summary = []
    if "selected_options" in variations:
        option_summary = [f"{opt['quantity']} {opt['name']}" for opt in variations["selected_options"]]
        summary.append(f"sizes: {', '.join(option_summary)}")
    if "selected_addons" in variations:
        addon_summary = [f"{addon['addon_name']}" for addon in variations["selected_addons"]]
        summary.append(f"{', '.join(addon_summary)}")
    return ", ".join(summary)


# ───────────────────────── MENU FETCH ─────────────────────────
def fetch_store_menu(conn, store_id):
    try:
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            query = """
                SELECT
                    s.title AS store_name,
                    p.id AS item_id,
                    p.title AS item_name,
                    p.description,
                    p.status,
                    p.store_id,
                    p.cat_id AS subcategory_id,
                    sub.sub_name AS subcategory_name,
                    main.id AS category_id,
                    main.title AS category_name,
                    pa.title AS attribute_title
                FROM tbl_product AS p
                LEFT JOIN tbl_mcat_sub AS sub ON sub.sub_id = p.cat_id
                LEFT JOIN tbl_mcat AS main ON main.id = sub.mcat_id
                LEFT JOIN service_details AS s ON s.id = p.store_id
                LEFT JOIN tbl_product_attribute AS pa ON pa.product_id = p.id
                WHERE p.store_id = %s AND p.status = 1
            """
            cursor.execute(query, (store_id,))
            result = cursor.fetchall()
            return result
    except mysql.connector.Error as err:
        print(f"Database error in fetch_store_menu: {err}")
        return []

def fetch_product_details(conn, item_id: int):
    """Fetches options and add-ons for a specific product."""
    details = {"options": [], "addons": [], "normal_price": None}
    try:  
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            # Fetch Options    
            cursor.execute("""
                SELECT option_name, option_values, is_required, max_selections
                FROM tbl_product_options
                WHERE product_id = %s AND status = 1;
            """, (item_id,)) 
            options_data = cursor.fetchall()
            for opt in options_data:
                opt['option_values'] = json.loads(opt['option_values'])
            details["options"] = options_data

            # Fetch Add-ons
            cursor.execute("""
                SELECT addon_name, addon_price, addon_category, is_required
                FROM tbl_product_addons
                WHERE product_id = %s AND status = 1;
            """, (item_id,))
            details["addons"] = cursor.fetchall()
            
            # Fetch normal price
            cursor.execute("""
                SELECT normal_price FROM tbl_product_attribute WHERE product_id = %s;
            """, (item_id,))
            price_data = cursor.fetchone()
            if price_data and 'normal_price' in price_data and price_data['normal_price'] is not None:
                details['normal_price'] = float(price_data['normal_price'])

    except mysql.connector.Error as err:
        print(f"Database error in fetch_product_details: {err}")
    except json.JSONDecodeError as err:
        print(f"JSON decode error for product options: {err}")
    return details

def fetch_product_attributes(conn, item_id: int) -> List[str]:
    """Fetches attributes for a specific product."""
    try:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT title FROM tbl_product_attribute WHERE product_id = %s;
            """, (item_id,))
            return [row['title'] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Database error in fetch_product_attributes: {err}")
        return []


# ───────────────────────── QUESTIONS ──────────────────────────
def fetch_menu_questions(cur, item_id: int) -> List[Dict[str, Any]]:
    
    """Fetches dynamic questions for a specific product from the menu_questions table."""
    try:
        cur.execute("""
            SELECT question_text, question_type, required, sort_order
            FROM menu_questions
            WHERE item_id = %s
            ORDER BY sort_order;
        """, (item_id,))
        return cur.fetchall()
    except mysql.connector.Error as err:
        print(f"Database error in fetch_menu_questions: {err}")
        return []
#-------------------------------------
def _parse_multi_sizes(sentence: str, allowed: List[str]) -> List[Dict[str, Any]]:
    sizes_found = {}
    size_pat = "|".join(map(re.escape, allowed)) if allowed else r"[a-z0-9]+"
    rex = re.compile(rf"(?:\b(\w+)\s*)?(?:x\s*)?\b({size_pat})\b", re.I)

    for qty_word, size in rex.findall(sentence):
        qty = w2n.word_to_num(qty_word) if qty_word and not qty_word.isdigit() else int(qty_word or 1)
        sizes_found[size.lower()] = sizes_found.get(size.lower(), 0) + qty
    
    return [{"name": s.capitalize(), "quantity": q} for s, q in sizes_found.items()]

#------------------------------------------
def _parse_multi_options(sentence: str, allowed: List[str]) -> List[Dict[str, Any]]:
    canon = {o.lower(): o for o in allowed}
    rex = r"(?:(\d+)\s*)?(?:x\s*)?(" + "|".join(re.escape(o.lower()) for o in allowed) + r")"
    found = re.findall(rex, sentence.lower())
    result = []
    for qty_s, opt_lc in found:
        qty = int(qty_s) if qty_s else 1
        result.append({"name": canon[opt_lc], "quantity": qty})
    return result

#---------------------------------
def ask_dynamic_questions(conn, item: Dict[str, Any], prefilled: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    details = fetch_product_details(conn, item["item_id"])
    questions = fetch_menu_questions(conn.cursor(dictionary=True), item["item_id"])
    answers = {"selected_options": [], "selected_addons": []}
    prefilled = prefilled or {}

    # Handle items with options (sizes)
    if details["options"]:
        for options_data in details["options"]:
            option_name = options_data.get('option_name', 'options')
            options_list = {val['name'].lower(): val for val in options_data.get('option_values', [])}
            
            prompt_text = f"Please select your {option_name}.\n"
            prompt_text += "The available options are: "
            prompt_text += ", ".join([f"{v['name']} at ₹{v['price']}" for v in options_data['option_values']])
            speak(prompt_text)

            ans = listen().lower()
            selected_opts = _parse_multi_sizes(ans, list(options_list.keys()))

            if selected_opts:
                for opt in selected_opts:
                    db_option = options_list.get(opt['name'].lower())
                    if db_option:
                        opt['price'] = db_option['price']
                answers["selected_options"].extend(selected_opts)
            elif options_data.get('is_required'):
                speak(f"A selection for {option_name} is required. Please try again.")
    # Handle items without options, but with a normal price
    elif details["normal_price"] is not None:
        speak("What quantity would you like?")
        ans = listen().lower()
        qty = extract_quantity(ans)
        if qty > 0:
            answers["quantity"] = qty
        else:
            speak("Sorry, I didn't get a valid quantity. The quantity has been set to 1.")
            answers["quantity"] = 1
    
    for question in questions:
        question_text = question['question_text']
        question_type = question['question_type']
        
        if question_type == 'boolean':
            addon_data = next((addon for addon in details["addons"] if addon["addon_name"].lower() in question_text.lower()), None)
            if addon_data:
                if ask_boolean_question(question_text):
                    answers["selected_addons"].append(addon_data)
                    
    # The quantity is the sum of quantities from all selected options, or the quantity asked for if no options
    if not answers.get("selected_options"):
        answers["quantity"] = answers.get("quantity", prefilled.get("quantity", 1))
    else:
        answers["quantity"] = sum(o.get("quantity", 0) for o in answers.get("selected_options", [])) or 1
    
    return answers  

# ──────────────── VARIATION CONVERTER ────────────────
def transform_variation(variation: Dict[str, Any]) -> str:
    if not variation:
        return None

    structured = []
    
    # Options (e.g., Sizes)
    if "selected_options" in variation:
        for opt in variation["selected_options"]:
            
            price = opt.get("price", 0) # Assuming the price is part of the option dict now
            structured.append({
                "name": opt["name"],
                "values": {"label": f"Quantity: {opt['quantity']}", "price": price}
            })
    
    # Add-ons
    if "selected_addons" in variation:
        for addon in variation["selected_addons"]:
            structured.append({
                "name": addon["addon_name"],
                "values": {"label": "Add-on", "price": str(addon["addon_price"])}
            })

    return json.dumps(structured)


# ───────────────────────── CART & ORDER ───────────────────────

def add_to_cart(user_id: int,
              store_id: int,
              item: Dict[str, Any],
              total_qty: int,
              price: float,
              variation: Optional[Dict[str, Any]],
              visible: int) -> None:
    conn = get_db_connection()
    if not conn:
        print("Cart insert failed: No database connection.")
        return

    cur = conn.cursor()

    variation_str = transform_variation(variation) if variation else None
    product_id = item.get("item_id")
    product_title = item.get("item_name", "Unnamed Product")
    product_img = item.get("image", "")
    cart_type = "normal"
    subscription_data = None
    

    fetch_attribute_id_query = """
        SELECT id FROM tbl_product_attribute
        WHERE product_id = %s AND store_id = %s
        LIMIT 1 
    """
    cur.execute(fetch_attribute_id_query, (product_id, store_id))
    result = cur.fetchone()

    attribute_id = result[0] if result else 0

    cur.execute("""
        INSERT INTO tbl_cart_data (
            uid, store_id, product_id, attribute_id, quantity, price,
            product_title, product_img, cart_type, variation, visible, subscription_data
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        user_id,
        store_id,
        product_id,
        attribute_id,
        total_qty,
        price,
        product_title,
        product_img,
        cart_type,
        variation_str,
        visible,
        subscription_data
    ))

    conn.commit()
    cur.close()

    # The confirmation message needs to be adjusted slightly to be accurate
    confirm_order(item_name=product_title, qty=total_qty, variations=variation)

def confirm_order_summary(orders: List[Tuple[Dict[str, Any], int, Dict[str, Any]]]) -> Tuple[str, float, List[Tuple[Dict[str, Any], int, Dict[str, Any], float]]]:
    total_price_final = 0.0
    speak("Here is your order summary:")
    
    conn = get_db_connection()
    if not conn:
        print("[Error] No database connection for order summary.")
        return "no", 0.0, []
        
    finalized_orders_with_prices = []

    for item, total_item_quantity, cust_variations in orders:
        item_details = fetch_product_details(conn, item["item_id"])
        
        options_summary = []
        item_base_price_with_options = 0.0
        if "selected_options" in cust_variations:
            for option in cust_variations["selected_options"]:
                options_summary.append(f"{option['quantity']} {option['name']}")
                item_base_price_with_options += float(option['price']) * option['quantity']
        
        addon_summary = []
        addon_price_per_item = 0.0
        if "selected_addons" in cust_variations:
            for addon in cust_variations["selected_addons"]:
                addon_summary.append(f"{addon['addon_name']} (+₹{float(addon['addon_price']):.2f})")
                addon_price_per_item += float(addon['addon_price'])

        item_total_before_discount = 0.0
        if item_base_price_with_options > 0:
            item_total_before_discount = item_base_price_with_options + (addon_price_per_item * total_item_quantity)
        else:
            base_price = item_details.get("normal_price", 0.0)       ######
            item_total_before_discount = (base_price * total_item_quantity) + (addon_price_per_item * total_item_quantity)

        discount_rate = 0.0
        try:
            # Re-establish a new cursor here to avoid the disconnected cursor error
            with conn.cursor(dictionary=True) as cur:
                cur.execute("SELECT discount FROM tbl_product_attribute WHERE product_id = %s", (item["item_id"],))
                result = cur.fetchone()
                if result:
                    discount_rate = float(result.get("discount", 0.0))
        except mysql.connector.Error as e:
            print(f"[DB Error] Could not fetch discount: {e}")

        item_discount_amount = item_total_before_discount * (discount_rate / 100)
        item_total_after_discount = item_total_before_discount - item_discount_amount
        
        total_price_final += item_total_after_discount
        
        line = f"{total_item_quantity} {item['item_name']}"
        if options_summary:
            line += f" | sizes: {', '.join(options_summary)}"
        if addon_summary:
            line += f", {', '.join(addon_summary)}"

        print(line)

        finalized_orders_with_prices.append((item, total_item_quantity, cust_variations, item_total_after_discount))

    final_total_line = f"Total: ₹{total_price_final:.2f}"
    print(final_total_line)
    speak(final_total_line)

    max_attempts = 3
    for attempt in range(max_attempts):
        speak("Would you like to confirm this order?")
        response = listen().lower()
        if any(w in response for w in ["yes", "confirm", "sure", "okay"]):
            return "yes", total_price_final, finalized_orders_with_prices
        elif any(w in response for w in ["no", "cancel", "not now"]):
            return "no", total_price_final, finalized_orders_with_prices
        elif any(w in response for w in ["maybe", "later"]):
            return "maybe", total_price_final, finalized_orders_with_prices
        else:
            speak("Sorry, I didn't get that.")

    fallback = input("Fallback (type yes / no / maybe): ").strip().lower()
    return fallback, total_price_final, finalized_orders_with_prices

#---------------------------------------------
def fuzzy_match_item(requested: str, menu_items: List[Dict[str, Any]], threshold: int = 50) -> Optional[Dict[str, Any]]:
    
    if not menu_items:
        return None

    names = [m["item_name"] for m in menu_items]
    match, score, idx = fuzz_process.extractOne(requested, names)
    if score >= threshold:
        return menu_items[idx]
    return None  

# -------------------------------------
def handle_store_assistant(user_id: int, store_id: int) -> None:
    conn = get_db_connection()
    if not conn:
        speak("I am unable to connect to the menu at this time. Please try again later.")
        return 

    cur = conn.cursor(dictionary=True)
    menu = fetch_store_menu(conn, store_id)
    if not menu or "store_name" not in menu[0]:
        speak("This store currently has no food items.")
        return

    store_name = menu[0].get("store_name", "our store")
    user_name = get_user_name(cur, user_id)

    speak(f"Hello {user_name}! You're chatting with {store_name}'s assistant. What would you like to eat today?")

    options_map = {}
    for item in menu:
        details = fetch_product_details(conn, item["item_id"])
        options_map[item["item_name"].lower()] = {
            "options": [
                {
                    "name": opt["option_name"],
                    "values": [val["name"] for val in opt["option_values"]]
                } for opt in details["options"]
            ],
            "addons": [addon["addon_name"] for addon in details["addons"]]
        }
    
    user_input = listen()

    if not user_input.strip():
        speak("I didn't catch that. Here is what we have on the menu.")
        _display_full_menu(menu)
        user_input = listen()

    parsed_orders = _parse_free_form_order(user_input, options_map)
    final_orders: List[Tuple[Dict[str, Any], int, Dict[str, Any]]] = []

    for order in parsed_orders:
        names = [m["item_name"] for m in menu]
        results = fuzz_process.extract(order["item_name"], names, limit=20)
        best_score = results[0][1] if results else 0
        matches = [menu[idx] for _, score, idx in results if score >= best_score - 20]

        if not matches:
            speak(f"Sorry, I couldn’t find anything similar to '{order['item_name']}'.")
            continue

        item = _resolve_ambiguity(matches, order["item_name"])

        prefilled = {
            "quantity": order.get("quantity"),
        }
        for opt_def in options_map.get(item["item_name"].lower(), {}).get("options", []):
            if opt_def["name"].lower() in order:
                prefilled[opt_def["name"].lower()] = order[opt_def["name"].lower()]
        for addon_name in options_map.get(item["item_name"].lower(), {}).get("addons", []):
            if addon_name.lower() in order:
                prefilled[addon_name.lower()] = order[addon_name.lower()]
        
        conn = get_db_connection()
        if not conn:
            speak("I am unable to process your order due to a connection error.")
            return
        custom = ask_dynamic_questions(conn, item, prefilled)

        qty_final = custom.get("quantity", 1)
        if custom.get("selected_options"):
            qty_final = sum(o["quantity"] for o in custom.get("selected_options", [])) or 1
        final_orders.append((item, qty_final, custom))

    if not final_orders:
        speak("No valid items were added.")
        return

    confirmation, total_price, finalized_orders_with_prices = confirm_order_summary(final_orders)
    visibility = 1 if "yes" in confirmation else (2 if "maybe" in confirmation else 0)

    if visibility in (1, 2):
        for item, qty_base, cust, final_price in finalized_orders_with_prices:
            conn = get_db_connection()
            if not conn:
                speak("Could not process your order due to a connection error.")
                return
            
            try:
                add_to_cart(
                    user_id=user_id,
                    store_id=store_id,
                    item=item,
                    total_qty=qty_base,
                    price=final_price,
                    variation=cust,
                    visible=visibility,
                )
            except mysql.connector.Error as e:
                print(f"[Cart Insert Error] Failed to add item: {e}")

    for item, qty, _ in final_orders:
        food_item_name = item["item_name"]
        if visibility == 1:
            speak(f"Thank you {user_name}! Your order has been added to cart successfully.")
        elif visibility == 2:
            speak(f" {user_name}! Your order has been saved as a draft.")
        else:
            speak(f"{qty} {food_item_name} added invisibly to the cart.")  


# -------------------------
            
def _display_full_menu(menu: List[Dict[str, Any]]):
    speak("Here are the available items on the menu:")
    
    # Use a set to store unique item names to avoid duplicates
    unique_items = set()
    for item in menu:
        unique_items.add(item['item_name'])
        
    # Sort the unique item names alphabetically for a clean display
    sorted_items = sorted(list(unique_items))
    
    # Print the numbered list of unique items
    for i, item_name in enumerate(sorted_items, 1):
        print(f" {i}. {item_name}")

# -------------------
def _parse_free_form_order(
    text: str,
    options_map: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    
    orders = []
    parts = re.split(r"\s+(?:and|&|with|,)\s+", text.lower())

    for part in parts:
        order_data = {
            "item_name": part.strip(),
            "quantity": extract_quantity(part),
        }
        
        best_match, score, _ = fuzz_process.extractOne(part, options_map.keys())
        if score < 70:
            continue
            
        item_name_key = best_match
        order_data["item_name"] = item_name_key

        item_details = options_map.get(item_name_key, {})
        
        for opt_def in item_details.get("options", []):
            opt_name = opt_def["name"].lower()
            opt_values = [v.lower() for v in opt_def["values"]]
            
            for value in opt_values:
                if value in part:
                    order_data[opt_name] = value
                    break
        
        for addon_name in item_details.get("addons", []):
            if addon_name.lower() in part:
                order_data[addon_name.lower()] = True

        orders.append(order_data)

    return orders

# ----------------------
def _resolve_ambiguity(matches: List[Dict[str, Any]], original_text: Optional[str] = None) -> Dict[str, Any]:
    if len(matches) == 1:
        return matches[0]

    speak("I found these options:")
    for idx, m in enumerate(matches, start=1):
        attribute_title = 'N/A'
        db_title = m.get('attribute_title')

        if db_title:
            try:
                titles = json.loads(db_title)
                if titles and isinstance(titles, list):
                    attribute_title = '(["{}"])'.format('", "'.join(titles))
                else:
                    attribute_title = f"({db_title})"
            except (json.JSONDecodeError, TypeError):
                attribute_title = f"({db_title})"
        
        label = f"{idx}. {m['item_name']} - {attribute_title}"
        print(label)

    speak("Please say the number you want.")
    while True:
        ans = listen()
        normalized = normalize_choice(ans)
        if normalized.isdigit():
            idx = int(normalized)
            if 1 <= idx <= len(matches):
                return matches[idx - 1]
        speak("Invalid number, please try again.")

# -----------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user_id", required=True, type=int)
    parser.add_argument("--store_id", required=True, type=int)
    args = parser.parse_args()

    try:
        handle_store_assistant(args.user_id, args.store_id)
    finally:
        conn = get_db_connection()
        if conn and conn.is_connected():
            conn.close()