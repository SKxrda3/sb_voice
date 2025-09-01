import os
import json
import uuid  
from flask import Flask, request, jsonify, g
import mysql.connector
from dotenv import load_dotenv
from mysql.connector import Error
from Final import (
    _parse_multi_sizes,
    fetch_menu_questions,
    get_user_name,
    fetch_store_menu,
    fetch_product_details,
    extract_quantity,
    transform_variation,
    add_to_cart,
)
from rapidfuzz import process as fuzz_process

load_dotenv()
app = Flask(__name__)

# --- In-Memory Session Cache ---
# In production, replace this with a connection to Redis or Memcached
session_cache = {}

def get_store_name(cur, store_id: int) -> str:
    try:
        cur.execute("SELECT title FROM service_details WHERE id = %s AND status = 1;", (store_id,))
        row = cur.fetchone()
        return row["title"] if row and row.get("title") else "Store"
    except Error as e:
        print(f"Error fetching store name: {e}")
        return "Store"

def get_db():
    if 'db' not in g:
        g.db = mysql.connector.connect(
            host=os.getenv('DB_HOST'), user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'), database=os.getenv('DB_NAME'),
            autocommit=True
        )
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# def parse_boolean_answer(sentence: str) -> bool:
#     positive_phrases = ["yes", "yeah", "yup", "i want", "sure", "of course", "absolutely", "okay", "add", "include"]
#     return any(p in sentence.lower() for p in positive_phrases)

def parse_boolean_answer(sentence: str) -> bool:

    s = sentence.lower()
    positive_phrases = ["yes", "yeah", "yup", "i want", "sure", "of course", "absolutely", "okay", "add", "include", "do", "confirm"]
    negative_phrases = ["no", "nope", "don't", "not", "skip", "without", "exclude", "remove", "cancel"]
    
    # It's positive if a positive word is present AND no negative word is present.
    is_positive = any(p in s for p in positive_phrases)
    is_negative = any(n in s for n in negative_phrases)
    
    return is_positive and not is_negative

def calculate_item_price(conn, item: dict) -> tuple:
    total_price, total_quantity = 0.0, 0
    if item.get("selected_options"):
        for opt in item["selected_options"]:
            total_price += float(opt.get('price', 0)) * int(opt.get('quantity', 1))
            total_quantity += int(opt.get('quantity', 1))
    else:
        price = item.get('price', 0) or fetch_product_details(conn, item['item_id']).get('normal_price', 0)
        quantity = item.get('quantity', 1)
        total_price = float(price) * int(quantity)
        total_quantity = int(quantity)

    addon_price = sum(float(addon.get('addon_price', 0)) for addon in item.get("selected_addons", []))
    total_price += addon_price * total_quantity

    with conn.cursor(dictionary=True) as cur:
        cur.execute("SELECT discount FROM tbl_product_attribute WHERE product_id = %s", (item["item_id"],))
        result = cur.fetchone()
        discount_rate = float(result['discount']) if result and result.get('discount') else 0.0

    final_price = total_price * (1 - discount_rate / 100)
    return final_price, total_quantity

def create_order_summary_for_api(conn, completed_items: list) -> dict:
    """
    Calculates prices and generates a summary object for the API.
    """
    summary_items = []
    total_price_final = 0.0

    for item in completed_items:
        total_quantity = 0
        options_summary = []
        if item.get("selected_options"):
            for option in item["selected_options"]:
                options_summary.append(f"{option.get('quantity', 1)} {option.get('name', '')}")
                total_quantity += int(option.get('quantity', 1))
        else:
            total_quantity = int(item.get('quantity', 1))

        addon_summary = []
        if item.get("selected_addons"):
            for addon in item["selected_addons"]:
                addon_summary.append(addon.get('addon_name', ''))

        final_price, _ = calculate_item_price(conn, item)
        total_price_final += final_price

        # Create a summary line for this specific item
        line = f"{total_quantity} {item['item_name']}"
        if options_summary:
            line += f" ({', '.join(options_summary)})"
        if addon_summary:
            line += f" with {', '.join(addon_summary)}"
        
        summary_items.append({
            "line_item": line,
            "price": f"{final_price:.2f}"
        })

    return {
        "summary_items": summary_items,
        "total_price": total_price_final
    }


@app.route('/api/v1/start-conversation', methods=['POST'])
def start_conversation():
    """
    Starts a new conversation, creates a session, and returns the session ID.
    """
    data = request.get_json()
    user_id = data.get('user_id')
    store_id = data.get('store_id')

    if not user_id or not store_id:
        return jsonify({"error": "user_id and store_id are required."}), 400

    conn = get_db()
    cur = conn.cursor(dictionary=True)

    # Generate a unique session ID
    session_id = str(uuid.uuid4())

    # Create the initial state and store it in the cache
    initial_state = {
        "user_id": user_id,
        "store_id": store_id,
        "status": "started",
        "item_in_progress": None,
        "completed_items": []
    }
    session_cache[session_id] = initial_state

    user_name = get_user_name(cur, user_id)
    store_name = get_store_name(cur, store_id)
    cur.close()

    message = f"Hello {user_name}! You're chatting with {store_name}'s assistant. What would you like to eat today?"
    return jsonify({
        "status": "success",
        "session_id": session_id,
        "assistant_response": message
    })
    

# @app.route('/api/v1/chat', methods=['POST'])
# def chat_step():
#     """
#     Handles a single turn, now with ambiguity resolution.
#     """
#     data = request.get_json()
#     session_id = data.get('session_id')
#     user_input = data.get('user_input')

#     if not session_id or user_input is None:
#         return jsonify({"error": "session_id and user_input are required."}), 400

#     state = session_cache.get(session_id)
#     if not state:
#         return jsonify({"error": "Invalid or expired session_id."}), 404

#     conn = get_db()

#     # ---- LOGIC FOR DIFFERENT CONVERSATION STATES ----

#     # A. If the API is waiting for the user to clarify an ambiguous item
#     if state.get('status') == 'clarification_needed':
#         clarification_options = state.get('clarification_options', [])
#         chosen_item = None

#         # Try to match by number (e.g., "1", "2")
#         if user_input.strip().isdigit():
#             choice_index = int(user_input.strip()) - 1
#             if 0 <= choice_index < len(clarification_options):
#                 chosen_item = clarification_options[choice_index]
        
#         # If not a valid number, try to match by name again from the small list
#         if not chosen_item:
#             item_names = [item['item_name'] for item in clarification_options]
#             match = fuzz_process.extractOne(user_input, item_names, score_cutoff=80)
#             if match:
#                 # Find the full item object that corresponds to the matched name
#                 chosen_item = next((item for item in clarification_options if item['item_name'] == match[0]), None)

#         if chosen_item:
#             # Item has been chosen, now proceed to ask questions for it
#             state['status'] = 'item_selected'
#             state['item_in_progress'] = chosen_item
#             state.pop('clarification_options', None) # Clean up
#             # Fall through to the 'else' block to start asking questions
#         else:
#             # Could not understand the choice, ask again
#             options_text = "\n".join([f"{i+1}. {item['item_name']}" for i, item in enumerate(clarification_options)])
#             return jsonify({
#                 "status": "clarification_needed",
#                 "assistant_response": f"Sorry, I didn't get that. Please choose a number or name from the list:\n{options_text}",
#                 "options": clarification_options,
#                 "session_id": session_id
#             })

#     # B. If the API is waiting for final order confirmation
#     if state.get('status') == 'pending_confirmation':
#         # ... (This logic remains the same)
#         if parse_boolean_answer(user_input):
#             try:
#                 for item in state['completed_items']:
#                     final_price, total_quantity = calculate_item_price(conn, item)
#                     add_to_cart(user_id=state['user_id'], store_id=state['store_id'], item=item, total_qty=total_quantity, price=final_price, variation=item, visible=1)
#                 del session_cache[session_id]
#                 return jsonify({"status": "order_confirmed", "assistant_response": "Thank you! Your order has been placed in your cart."})
#             except Error as err:
#                 return jsonify({"status": "error", "message": f"Database error: {err}"}), 500
#         else:
#             del session_cache[session_id]
#             return jsonify({"status": "order_cancelled", "assistant_response": "Okay, I've cancelled your order."})

#     item_in_progress = state.get('item_in_progress')

#     # C. If we are asking questions for an item
#     if item_in_progress and state.get('pending_questions'):
#         # ... (This logic remains the same)
#         current_question = state['pending_questions'].pop(0)
#         question_type = current_question['type']
        
#         if question_type == 'options':
#             allowed = current_question.get('data', {}).get('option_values', [])
#             parsed = _parse_multi_sizes(user_input, [opt.get('name') for opt in allowed])
#             for p in parsed:
#                 for a in allowed:
#                     if p['name'].lower() == a['name'].lower():
#                         p['price'] = a['price']; break
#             item_in_progress.setdefault('selected_options', []).extend(parsed)
#         elif question_type == 'boolean':
#              if parse_boolean_answer(user_input):
#                 item_in_progress.setdefault('selected_addons', []).append(current_question.get('data', {}))

#         state['item_in_progress'] = item_in_progress
#         if state['pending_questions']:
#             next_question = state['pending_questions'][0]
#             session_cache[session_id] = state
#             return jsonify({"status": "question", "assistant_response": next_question['question_text'], "session_id": session_id})
#         else:
#             state['completed_items'].append(item_in_progress)
#             state['item_in_progress'] = None
#             session_cache[session_id] = state
#             return jsonify({"status": "item_complete", "assistant_response": "Got it. Anything else?", "session_id": session_id})

#     # D. If the user wants to end the order
#     elif user_input.lower() in ["no", "that's all", "thats all"]:
#         # ... (This logic remains the same)
#         if not state['completed_items']:
#             return jsonify({"status": "complete", "assistant_response": "Your cart is empty. What would you like to order?"})
#         summary = create_order_summary_for_api(conn, state['completed_items'])
#         state['status'] = 'pending_confirmation'
#         session_cache[session_id] = state
#         summary_lines = [item['line_item'] for item in summary['summary_items']]
#         response_text = "Here is your order summary:\n- " + "\n- ".join(summary_lines)
#         response_text += f"\n\nYour total is ₹{summary['total_price']:.2f}. Should I confirm this order?"
#         return jsonify({"status": "pending_confirmation", "assistant_response": response_text, "summary": summary, "session_id": session_id})

#     # E. If we are waiting for a new item from the user
#     else:
#         # <<< MODIFIED LOGIC START >>>
#         # Handle empty input by showing the menu
#         if not user_input.strip():
#             # ... (This logic remains the same)
#             menu = fetch_store_menu(conn, state['store_id'])
#             if not menu: return jsonify({"status": "error", "assistant_response": "Sorry, the menu is currently unavailable."})
#             unique_item_names = sorted(list(set(item['item_name'] for item in menu)))
#             menu_text = "\n".join(f"{i}. {name}" for i, name in enumerate(unique_item_names, 1))
#             response_message = "I didn't catch that. Here is what we have on the menu:\n" + menu_text
#             return jsonify({"status": "awaiting_item_selection", "assistant_response": response_message, "menu_items": unique_item_names, "session_id": session_id})

#         # Find items using fuzzy search
#         menu = fetch_store_menu(conn, state['store_id'])
#         all_items_map = {item['item_name']: item for item in menu} # Assumes unique names
#         all_item_names = list(all_items_map.keys())
        
#         # Use extract to get multiple matches, not just one
#         matches = fuzz_process.extract(user_input, all_item_names, score_cutoff=75, limit=5)

#         if not matches:
#             return jsonify({"status": "not_found", "assistant_response": f"Sorry, I couldn't find anything like '{user_input}'."})

#         # Check if the top matches are too similar in score (ambiguous)
#         best_score = matches[0][1]
#         ambiguous_matches = [m for m in matches if m[1] >= best_score - 5] # Get all matches with scores close to the best one

#         if len(ambiguous_matches) > 1:
#             # AMBIGUITY DETECTED
#             clarification_options = [all_items_map[name] for name, score, idx in ambiguous_matches]
#             state['status'] = 'clarification_needed'
#             state['clarification_options'] = clarification_options
#             session_cache[session_id] = state

#             options_text = "\n".join([f"{i+1}. {item['item_name']}" for i, item in enumerate(clarification_options)])
#             return jsonify({
#                 "status": "clarification_needed",
#                 "assistant_response": f"I found a few options, which one did you mean?\n{options_text}",
#                 "options": clarification_options, # Send structured data to the client
#                 "session_id": session_id
#             })
        
#         # NO AMBIGUITY, PROCEED WITH THE BEST MATCH
#         best_match_name = matches[0][0]
#         matched_item = all_items_map[best_match_name]
#         state['item_in_progress'] = matched_item
        
#         # This logic is now inside the main endpoint
#         details = fetch_product_details(get_db(), matched_item['item_id'])
#         questions = []
#         if details.get("options"):
#             for opt_group in details["options"]:
#                 choices = ", ".join([f"{val['name']} (₹{val['price']})" for val in opt_group['option_values']])
#                 questions.append({"type": "options", "question_text": f"Please select your {opt_group['option_name']}. Options are: {choices}", "data": opt_group})
#         if details.get("addons"):
#             for addon in details["addons"]:
#                 questions.append({"type": "boolean", "question_text": f"Would you like to add {addon['addon_name']} (₹{addon['addon_price']})?", "data": addon})

#         if not questions: # No questions, item is complete
#             state['completed_items'].append(matched_item)
#             state['item_in_progress'] = None
#             session_cache[session_id] = state
#             return jsonify({"status": "item_complete", "assistant_response": f"Added {matched_item['item_name']}. Anything else?"})

#         # Ask the first question
#         state['pending_questions'] = questions
#         session_cache[session_id] = state
#         return jsonify({"status": "question", "assistant_response": questions[0]['question_text'], "session_id": session_id})
#     # <<< MODIFIED LOGIC END >>>



@app.route('/api/v1/chat', methods=['POST'])
def chat_step():

    data = request.get_json()
    session_id = data.get('session_id')
    user_input = data.get('user_input')

    if not session_id or user_input is None:
        return jsonify({"error": "session_id and user_input are required."}), 400

    state = session_cache.get(session_id)
    if not state:
        return jsonify({"error": "Invalid or expired session_id."}), 404

    conn = get_db()
  
    if state.get('status') == 'clarification_needed':
        clarification_options = state.get('clarification_options', [])
        chosen_item = None

        if user_input.strip().isdigit():
            choice_index = int(user_input.strip()) - 1
            if 0 <= choice_index < len(clarification_options):
                chosen_item = clarification_options[choice_index]
        
        if not chosen_item:
            item_names = [item['item_name'] for item in clarification_options]
            match = fuzz_process.extractOne(user_input, item_names, score_cutoff=80)
            if match:
                chosen_item = next((item for item in clarification_options if item['item_name'] == match[0]), None)

        if chosen_item:
            state['status'] = 'item_selected'
            state['item_in_progress'] = chosen_item
            state.pop('clarification_options', None)
        else:
            options_text = "\n".join([f"{i+1}. {item['item_name']}" for i, item in enumerate(clarification_options)])
            
            formatted_options = [{
                "item_id": item.get("item_id"),
                "item_name": item.get("item_name", "").strip()
            } for item in clarification_options]
            
            return jsonify({
                "status": "clarification_needed",
                "assistant_response": f"Sorry, I didn't get that. Please choose a number or name from the list:\n{options_text}",
                "options": formatted_options,
                "session_id": session_id
            })

    # B. If the API is waiting for final order confirmation
    if state.get('status') == 'pending_confirmation':
        if parse_boolean_answer(user_input):
            try:
                for item in state['completed_items']:
                    final_price, total_quantity = calculate_item_price(conn, item)
                    add_to_cart(user_id=state['user_id'], store_id=state['store_id'], item=item, total_qty=total_quantity, price=final_price, variation=item, visible=1)
                del session_cache[session_id]
                return jsonify({"status": "order_confirmed", "assistant_response": "Thank you! Your order has been placed in your cart."})
            except Error as err:
                return jsonify({"status": "error", "message": f"Database error: {err}"}), 500
        else:
            del session_cache[session_id]
            return jsonify({"status": "order_cancelled", "assistant_response": "Okay, I've cancelled your order."})

    item_in_progress = state.get('item_in_progress')

    # C. If we are asking questions for an item
    if item_in_progress and state.get('pending_questions'):
        current_question = state['pending_questions'].pop(0)
        question_type = current_question['type']
        
        if question_type == 'options':
            allowed = current_question.get('data', {}).get('option_values', [])
            parsed = _parse_multi_sizes(user_input, [opt.get('name') for opt in allowed])
            for p in parsed:
                for a in allowed:
                    if p['name'].lower() == a['name'].lower():
                        p['price'] = a['price']; break
            item_in_progress.setdefault('selected_options', []).extend(parsed)
        elif question_type == 'boolean':
             if parse_boolean_answer(user_input):
                item_in_progress.setdefault('selected_addons', []).append(current_question.get('data', {}))

        state['item_in_progress'] = item_in_progress
        if state['pending_questions']:
            next_question = state['pending_questions'][0]
            session_cache[session_id] = state
            return jsonify({"status": "question", "assistant_response": next_question['question_text'], "session_id": session_id})
        # else:
        #     state['completed_items'].append(item_in_progress)
        #     state['item_in_progress'] = None
        #     session_cache[session_id] = state
        #     return jsonify({"status": "item_complete", "assistant_response": "Got it. Anything else?", "session_id": session_id})

        
        else:
            # All questions for this item are done, add it to the list
            state['completed_items'].append(item_in_progress)
            state['item_in_progress'] = None
            
            # --- NEW LOGIC: Immediately show the summary ---
            summary = create_order_summary_for_api(conn, state['completed_items'])
            state['status'] = 'pending_confirmation'
            session_cache[session_id] = state
            
            summary_lines = [item['line_item'] for item in summary['summary_items']]
            response_text = "Here is your order summary:\n- " + "\n- ".join(summary_lines)
            response_text += f"\n\nYour total is ₹{summary['total_price']:.2f}. Should I confirm this order?"
            
            return jsonify({
                "status": "pending_confirmation",
                "assistant_response": response_text,
                "summary": summary,
                "session_id": session_id
            })
        

        
    # D. If the user wants to end the order
    elif user_input.lower() in ["no", "that's all", "thats all"]:
        if not state['completed_items']:
            return jsonify({"status": "complete", "assistant_response": "Your cart is empty. What would you like to order?"})
        summary = create_order_summary_for_api(conn, state['completed_items'])
        state['status'] = 'pending_confirmation'
        session_cache[session_id] = state
        summary_lines = [item['line_item'] for item in summary['summary_items']]
        response_text = "Here is your order summary:\n- " + "\n- ".join(summary_lines)
        response_text += f"\n\nYour total is ₹{summary['total_price']:.2f}. Should I confirm this order?"
        return jsonify({"status": "pending_confirmation", "assistant_response": response_text, "summary": summary, "session_id": session_id})

    # E. If we are waiting for a new item from the user
    else:
        if not user_input.strip():
            menu = fetch_store_menu(conn, state['store_id'])
            if not menu: return jsonify({"status": "error", "assistant_response": "Sorry, the menu is currently unavailable."})
            unique_item_names = sorted(list(set(item['item_name'] for item in menu)))
            menu_text = "\n".join(f"{i}. {name}" for i, name in enumerate(unique_item_names, 1))
            response_message = "I didn't catch that. Here is what we have on the menu:\n" + menu_text
            return jsonify({"status": "awaiting_item_selection", "assistant_response": response_message, "menu_items": unique_item_names, "session_id": session_id})

        # Find items using fuzzy search
        menu = fetch_store_menu(conn, state['store_id'])
        # A small change here to handle potential duplicate item names in the menu list
        unique_item_objects = {item['item_name'].strip(): item for item in menu}.values()
        all_items_map = {item['item_name'].strip(): item for item in unique_item_objects}
        all_item_names = list(all_items_map.keys())
        
        matches = fuzz_process.extract(user_input, all_item_names, score_cutoff=75, limit=5)

        if not matches:
            return jsonify({"status": "not_found", "assistant_response": f"Sorry, I couldn't find anything like '{user_input}'."})

        best_score = matches[0][1]
        ambiguous_matches = [m for m in matches if m[1] >= best_score - 5]

        if len(ambiguous_matches) > 1:
            # AMBIGUITY DETECTED
            clarification_options = [all_items_map[name] for name, score, idx in ambiguous_matches]
            state['status'] = 'clarification_needed'
            # Store the full objects in the session for our internal use
            state['clarification_options'] = clarification_options
            session_cache[session_id] = state

            # Create a clean, formatted list to send to the client
            formatted_options = []
            for item in clarification_options:
                formatted_options.append({
                    "item_id": item.get("item_id"),
                    "item_name": item.get("item_name", "").strip(),
                    
                })

            options_text = "\n".join([f"{i+1}. {item['item_name']}" for i, item in enumerate(formatted_options)])
            return jsonify({
                "status": "clarification_needed",
                "assistant_response": f"I found a few options, which one did you mean?\n{options_text}",
                "options": formatted_options, 
                "session_id": session_id
            })
        
        best_match_name = matches[0][0]
        matched_item = all_items_map[best_match_name]
        state['item_in_progress'] = matched_item
        
        details = fetch_product_details(get_db(), matched_item['item_id'])
        questions = []
        if details.get("options"):
            for opt_group in details["options"]:
                choices = ", ".join([f"{val['name']} (₹{val['price']})" for val in opt_group['option_values']])
                questions.append({"type": "options", "question_text": f"Please select your {opt_group['option_name']}. Options are: {choices}", "data": opt_group})
        if details.get("addons"):
            for addon in details["addons"]:
                questions.append({"type": "boolean", "question_text": f"Would you like to add {addon['addon_name']} (₹{addon['addon_price']})?", "data": addon})

        if not questions:
            state['completed_items'].append(matched_item)
            state['item_in_progress'] = None
            session_cache[session_id] = state
            return jsonify({"status": "item_complete", "assistant_response": f"Added {matched_item['item_name']}. Anything else?"})

        state['pending_questions'] = questions
        session_cache[session_id] = state
        return jsonify({"status": "question", "assistant_response": questions[0]['question_text'], "session_id": session_id})


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)