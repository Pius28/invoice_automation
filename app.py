import os
import re
import sys
import locale
import random
import time
import openai
import json
import pdfplumber
from dotenv import load_dotenv
from flask import Flask, render_template, session, redirect, url_for, request, send_file, flash, jsonify
from chatgpt import (decide_with_chatgpt, get_ai_suggestions, get_ai_errors_from_pdfs,
                     get_fully_auto_result, get_ai_errors_cooperative, fix_invoice_with_chatgpt)

# ---------------------------
# INITIAL SETUP
# ---------------------------
# Load environment variables and set the OpenAI API key
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

# Reconfigure standard input, output, and error to use UTF-8 encoding for proper text handling
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# Set locale for proper formatting (numbers, dates, ...)
locale.setlocale(locale.LC_ALL, "en_US.UTF-8")
if sys.platform.startswith("win"):
    locale.setlocale(locale.LC_ALL, "deu_deu" if sys.version_info.major == 3 else "German_Germany.1252")
else:
    locale.setlocale(locale.LC_ALL, "de_DE.UTF-8")

# Initialize Flask and configure JSON options
app = Flask(__name__)
app.secret_key = "my_random_secret_key_123"
app.config["JSON_AS_ASCII"] = False
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True

# Define folders for invoices, purchase orders, modified invoices, and result storage
INVOICE_FOLDER = "dataset/invoices"
PURCHASE_FOLDER = "dataset/PurchaseOrders"
MODIFIED_INVOICE_FOLDER = "dataset/invoices_modified"
RESULTS_FOLDER = "results"


# ---------------------------
# UTILITY FUNCTIONS
# ---------------------------
def reset_level_specific_data(current_level: str):
    """
    Removes session keys specific to any previous level.
    Persistent keys (e.g. used_invoice_numbers, user_id) remain.
    """
    keys_to_remove = [
        # Manual level keys
        "manual_start_time",
        # Assistive level keys
        "assistive_start_time", "assist_inv_data", "assist_po_data", "assist_errors", "assist_suggest",
        # Cooperative level keys
        "coop_inv_data", "coop_po_data", "coop_errors", "coop_duration", "coop_ai_response",
        "current_first_decision", "current_second_decision", "fix_result",
        # Supervisory Control level keys
        "sc_inv_data", "sc_po_data", "sc_errors", "sc_duration", "sc_decision",
        # Fully Automated level key
        "automated_start_time"
    ]
    for key in keys_to_remove:
        session.pop(key, None)
    session["level"] = current_level


def get_matching_numbers():
    """
    Returns a list of invoice numbers that are present in both the invoices and purchase orders folders.
    """
    invoice_files = os.listdir(INVOICE_FOLDER)
    purchase_files = os.listdir(PURCHASE_FOLDER)
    invoice_nums = set()
    purchase_nums = set()

    for f in invoice_files:
        if f.startswith("invoice_") and f.endswith(".pdf"):
            num = f[len("invoice_"):-4]
            invoice_nums.add(num)

    for f in purchase_files:
        if f.startswith("purchase_orders_") and f.endswith(".pdf"):
            num = f[len("purchase_orders_"):-4]
            purchase_nums.add(num)

    return list(invoice_nums.intersection(purchase_nums))


def get_modified_numbers():
    """
    Searches the "invoices_modified" folder for files named "modified_invoice_XXXX.pdf"
    and the purchase orders folder for corresponding "purchase_orders_XXXX.pdf".
    """
    invoice_files = os.listdir(MODIFIED_INVOICE_FOLDER)
    purchase_files = os.listdir(PURCHASE_FOLDER)
    invoice_nums = set()
    purchase_nums = set()

    for f in invoice_files:
        if f.startswith("modified_invoice_") and f.endswith(".pdf"):
            num = f[len("modified_invoice_"):-4]
            invoice_nums.add(num)

    for f in purchase_files:
        if f.startswith("purchase_orders_") and f.endswith(".pdf"):
            num = f[len("purchase_orders_"):-4]
            purchase_nums.add(num)

    return list(invoice_nums.intersection(purchase_nums))


def pick_random_pair():
    """
    Selects a random invoice-purchase pair excluding already used invoices.
    Updates session variables for the current invoice and purchase order.
    """
    if "used_invoice_numbers" not in session:
        session["used_invoice_numbers"] = []

    # Use modified invoices 2/3 of the time
    use_modified = (random.random() < (2.0 / 3.0))
    if use_modified:
        modified_nums = get_modified_numbers()
        available_nums = [num for num in modified_nums if num not in session["used_invoice_numbers"]]
    else:
        original_nums = get_matching_numbers()
        available_nums = [num for num in original_nums if num not in session["used_invoice_numbers"]]

    if not available_nums:
        return None

    chosen_num = random.choice(available_nums)
    session["used_invoice_numbers"].append(chosen_num)

    invoice_filename = f"modified_invoice_{chosen_num}.pdf" if use_modified else f"invoice_{chosen_num}.pdf"
    purchase_filename = f"purchase_orders_{chosen_num}.pdf"

    session["current_invoice"] = invoice_filename
    session["current_purchase"] = purchase_filename
    session["errors"] = []
    return True


def pdf_to_text_plumber(pdf_path: str) -> str:
    """
    Extracts the text of a PDF using pdfplumber.
    """
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text.append(text)
    return "\n".join(full_text)

@app.before_request
def require_id():
    """
    Ensure that the user has provided an ID before accessing any route.
    """
    allowed_routes = ["enter_id", "static"]
    if "user_id" not in session and request.endpoint not in allowed_routes:
        return redirect(url_for("enter_id"))

# ---------------------------
# MAIN / EXPLANATION / LOGOUT
# ---------------------------
@app.route("/enter_id", methods=["GET", "POST"])
def enter_id():
    """
    Route to enter the user ID.
    If POST, store the user ID in the session and redirect to the manual explanation.
    """
    if request.method == "POST":
        user_id = request.form.get("user_id")
        if not user_id:
            return "Please enter an ID."
        session["user_id"] = user_id
        return redirect(url_for("manual_explain"))
    else:
        return render_template("main.html")


@app.route("/logout")
def logout():
    """
    Clears the session and redirects to the ID entry page.
    """
    session.clear()
    return redirect(url_for("enter_id"))


# ---------------------------
# SHOW PDF
# ---------------------------
@app.route("/show_invoice")
def show_invoice():
    """
    Displays the current invoice.
    """
    inv = session.get("current_invoice")
    if not inv:
        return "No invoice selected."
    if inv.startswith("modified_invoice_"):
        path = os.path.join(MODIFIED_INVOICE_FOLDER, inv)
    else:
        path = os.path.join(INVOICE_FOLDER, inv)
    return send_file(path, as_attachment=False)


@app.route("/show_purchase")
def show_purchase():
    """
    Displays the current purchase.
    """
    pu = session.get("current_purchase")
    if not pu:
        return "No purchase order selected."
    path = os.path.join(PURCHASE_FOLDER, pu)
    return send_file(path, as_attachment=False)


# ---------------------------
# REPORT ISSUE
# ---------------------------
@app.route("/add_error", methods=["GET", "POST"])
def add_error():
    """
    Allows the user to report an error.
    """
    if request.method == "POST":
        error_type = request.form.get("error_type")
        correction = request.form.get("correction")
        free_text = request.form.get("free_text")
        if "errors" not in session:
            session["errors"] = []
        session["errors"].append({
            "error_type": error_type,
            "correction": correction,
            "free_text": free_text
        })
        level = session.get("level", "manual")
        return redirect(url_for(level))
    else:
        return render_template("add_error.html")

# ---------------------------
# MANUAL LEVEL
# ---------------------------
@app.route("/manual_explain")
def manual_explain():
    if "user_id" not in session:
        return redirect(url_for("enter_id"))
    return render_template("manual_explain.html")


@app.route("/manual")
def manual():
    """
    Displays the manual level.
    Resets data, selects a random pair and checks if the user has completed 3 invoices.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))

    reset_level_specific_data("manual")
    user_folder = os.path.join(RESULTS_FOLDER, str(user_id))
    json_path = os.path.join(user_folder, "manual.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if len(data) >= 3:
            return redirect(url_for("manual_done"))
    if "manual_count" not in session:
        session["manual_count"] = 0
    if session["manual_count"] >= 3:
        return redirect(url_for("manual_done"))

    session["manual_start_time"] = time.time()
    if not pick_random_pair():
        return "No matching pairs found."
    return render_template("manual.html")


@app.route("/manual_submit", methods=["POST"])
def manual_submit():
    """
    Processes the manual review submission:
    - Parses errors from the form.
    - Calculates duration.
    - Saves the review data as JSON.
    - Increments the manual count and redirects accordingly.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    all_errors = []
    for key in request.form:
        if key.startswith("error_type_"):
            idx = key.split("_")[2]
            entry = {
                "error_type": request.form.get(key),
                "correction": request.form.get(f"correction_{idx}"),
                "free_text": request.form.get(f"free_text_{idx}")
            }
            all_errors.append(entry)

    invoice_file = session.get("current_invoice")
    purchase_file = session.get("current_purchase")
    duration = round(time.time() - session["manual_start_time"], 2)
    booking_decision = request.form.get("booking_decision", "decline")

    data_entry = {
        "invoice_file": invoice_file,
        "purchase_file": purchase_file,
        "duration_seconds": duration,
        "errors": all_errors,
        "booking": booking_decision
    }
    # Save the manual level data as JSON
    user_folder = os.path.join(RESULTS_FOLDER, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    json_path = os.path.join(user_folder, "manual.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    else:
        old_data = []
    old_data.append(data_entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(old_data, f, indent=2, ensure_ascii=False)

    session["manual_count"] = session.get("manual_count", 0) + 1
    if session["manual_count"] < 3:
        return redirect(url_for("manual"))
    else:
        return redirect(url_for("manual_done"))


@app.route("/manual_done")
def manual_done():
    """
    Renders the done page for the manual level.
    """
    return render_template("manual_done.html")


# ---------------------------
# ASSISTIVE LEVEL
# ---------------------------
# This level uses AI suggestions to assist the user.
@app.route("/assistive_explain")
def assistive_explain():
    if "user_id" not in session:
        return redirect(url_for("enter_id"))
    return render_template("assistive_explain.html")


@app.route("/assistive")
def assistive():
    """
    Displays the assistive level waiting page.
    Resets data, selects a random pair and checks if the user has completed 3 invoices.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    reset_level_specific_data("assistive")
    if "assistive_count" not in session:
        session["assistive_count"] = 0
    if session["assistive_count"] >= 3:
        return redirect(url_for("assistive_done"))
    session["assistive_start_time"] = time.time()
    if not pick_random_pair():
        return "No matching pairs found."
    return render_template("assistive_waiting.html")


@app.route("/assistive_submit", methods=["POST"])
def assistive_submit():
    """
    Processes the assistive level submission.
    - Parses errors from the form.
    - Calculates duration.
    - Saves the review data as JSON.
    - Increments the manual count and redirects accordingly.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    all_errors = []
    for key in request.form:
        if key.startswith("error_type_"):
            idx = key.split("_")[2]
            entry = {
                "error_type": request.form.get(key),
                "correction": request.form.get(f"correction_{idx}"),
                "free_text": request.form.get(f"free_text_{idx}")
            }
            all_errors.append(entry)
    invoice_file = session.get("current_invoice")
    purchase_file = session.get("current_purchase")
    duration = round(time.time() - session["assistive_start_time"], 2)
    inv_data = session.get("assist_inv_data", {})
    po_data = session.get("assist_po_data", {})
    booking_decision = request.form.get("booking_decision", "decline")

    data_entry = {
        "invoice_file": invoice_file,
        "purchase_file": purchase_file,
        "duration_seconds": duration,
        "invoice_extracted": inv_data,
        "purchase_extracted": po_data,
        "errors": all_errors,
        "booking": booking_decision
    }
    user_folder = os.path.join(RESULTS_FOLDER, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    json_path = os.path.join(user_folder, "assistive.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    else:
        old_data = []
    old_data.append(data_entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(old_data, f, indent=2, ensure_ascii=False)

    session["assistive_count"] = session.get("assistive_count", 0) + 1
    if session["assistive_count"] < 3:
        return redirect(url_for("assistive"))
    else:
        return redirect(url_for("assistive_done"))


@app.route("/assistive_done")
def assistive_done():
    """
    Renders the done page for the assistive level.
    """
    return render_template("assistive_done.html")


@app.route("/assistive_process", methods=["POST"])
def assistive_process():
    """
    Processes the PDF extraction for the assistive level.
    - Extracts text from the selected invoice and purchase order.
    - Calls the AI to extract errors and suggestions.
    - Stores the AI results in session.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "No user_id"}), 400

    invoice_file = session.get("current_invoice")
    purchase_file = session.get("current_purchase")
    if invoice_file.startswith("modified_invoice_"):
        invoice_path = os.path.join(MODIFIED_INVOICE_FOLDER, invoice_file)
    else:
        invoice_path = os.path.join(INVOICE_FOLDER, invoice_file)
    purchase_path = os.path.join(PURCHASE_FOLDER, purchase_file)

    invoice_text = pdf_to_text_plumber(invoice_path)
    purchase_text = pdf_to_text_plumber(purchase_path)

    # Call AI to extract invoice errors
    ai_result = get_ai_errors_from_pdfs(invoice_text, purchase_text)
    inv_data = ai_result.get("invoice_extracted", {})
    po_data = ai_result.get("purchase_extracted", {})
    errors = ai_result.get("errors", [])

    session["assist_inv_data"] = inv_data
    session["assist_po_data"] = po_data
    session["assist_errors"] = errors

    corrections = []
    suggestions = get_ai_suggestions(inv_data, po_data, errors, corrections)
    session["assist_suggest"] = suggestions

    if session.get("assistive_count", 0) >= 3:
        next_url = url_for("assistive_done")
    else:
        next_url = url_for("assistive_display")
    return jsonify({"redirect_url": next_url})


@app.route("/assistive_display")
def assistive_display():
    """
    Renders the assistive level display page, showing extracted data, errors, and AI suggestions.
    """
    inv_data = session.get("assist_inv_data")
    po_data = session.get("assist_po_data")
    errors = session.get("assist_errors")
    suggestions = session.get("assist_suggest")
    return render_template("assistive.html",
                           inv_data=inv_data,
                           po_data=po_data,
                           errors=errors,
                           suggestions=suggestions)


@app.route("/get_dynamic_suggestions", methods=["POST"])
def get_dynamic_suggestions():
    """
    Provides dynamic AI suggestions based on user corrections.
    - Processes manual corrections from the frontend.
    - Compares them with expected values.
    - Calls the AI for updated suggestions.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid JSON"}), 400

        inv_data = session.get("assist_inv_data", {})
        po_data = session.get("assist_po_data", {})
        auto_errors = session.get("assist_errors", [])

        # Capture manual errors from frontend
        manual_errors = []
        for e in data.get("errors", []):
            if isinstance(e, dict) and "type" in e:
                manual_errors.append({"type": e["type"]})

        # Define expected values for corrections
        correct_values = {
            "Contact Name": str(po_data.get("customer_name", "")).strip().lower(),
            "Total Price": str(inv_data.get("total_price", "")).strip().lower(),
            "Order ID": str(po_data.get("order_id", "")).strip().lower(),
            "Unit Price": str(po_data.get("unit_price", "")).strip().lower(),
            "Product Name": str(po_data.get("product_name", "")).strip().lower(),
            "Product is missing": str(po_data.get("product_missing", "")).strip().lower(),
            "Quantity": str(po_data.get("quantity", "")).strip().lower(),
            "Order Date": str(po_data.get("order_date", "")).strip().lower(),
            "Date": str(po_data.get("date", "")).strip().lower(),
        }

        if "items" in po_data and isinstance(po_data["items"], list):
            for item in po_data["items"]:
                pid = item.get("product_id", "").strip().lower()
                key_name = f"Product Name:{pid}"
                correct_values[key_name] = item.get("product_name", "").strip().lower()
                key_qty = f"Quantity:{pid}"
                correct_values[key_qty] = str(item.get("quantity", "")).strip().lower()
                key_unit = f"Unit Price:{pid}"
                correct_values[key_unit] = str(item.get("unit_price", "")).strip().lower()

        validated_corrections = []
        for cor in data.get("corrections", []):
            if not isinstance(cor, dict):
                continue
            error_type = cor.get("type")
            user_value = cor.get("correction", "").strip().lower()
            if error_type == "Product Name" and "product_id" in cor:
                key = f"Product Name:{cor.get('product_id').strip().lower()}"
            elif error_type == "Quantity" and "product_id" in cor:
                key = f"Quantity:{cor.get('product_id').strip().lower()}"
            elif error_type == "Unit Price" and "product_id" in cor:
                key = f"Unit Price:{cor.get('product_id').strip().lower()}"
            else:
                key = error_type
            expected_value = correct_values.get(key, "").strip().lower()
            if error_type in ["Date", "Quantity", "Unit Price", "Total Price", "Order ID"]:
                is_valid = (user_value == expected_value)
            else:
                is_valid = (expected_value in user_value) or (user_value in expected_value)
            validated_corrections.append({
                "type": error_type,
                "is_valid": is_valid,
                "expected": expected_value,
                "entered": user_value
            })

        combined_errors = auto_errors + manual_errors
        suggestions = get_ai_suggestions(inv_data, po_data, combined_errors, validated_corrections)
        return jsonify({
            "suggestions": suggestions,
            "validatedCorrections": validated_corrections
        })

    except Exception as e:
        print(f"Server Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ---------------------------
# COOPERATIVE LEVEL
# ---------------------------
# This level uses AI for the invoice verification process and the user can decide to accept, decline or request an AI fix.
@app.route("/cooperative_explain")
def cooperative_explain():
    if "user_id" not in session:
        return redirect(url_for("enter_id"))
    return render_template("cooperative_explain.html")


@app.route("/cooperative")
def cooperative():
    """
    Displays the cooperative level waiting page.
    Resets data, selects a random pair and checks if the user has completed 3 invoices.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    reset_level_specific_data("cooperative")
    if "cooperative_count" not in session:
        session["cooperative_count"] = 0
    if session["cooperative_count"] >= 3:
        return redirect(url_for("cooperative_done"))
    session["cooperative_start_time"] = time.time()
    if not pick_random_pair():
        return "No matching pairs found."
    return render_template("cooperative_waiting.html")


@app.route("/cooperative_process", methods=["GET", "POST"])
def cooperative_process():
    """
    Processes the cooperative level and saving results in the session.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "No user_id"}), 400
    invoice_file = session.get("current_invoice")
    purchase_file = session.get("current_purchase")
    if invoice_file.startswith("modified_invoice_"):
        invoice_path = os.path.join(MODIFIED_INVOICE_FOLDER, invoice_file)
    else:
        invoice_path = os.path.join(INVOICE_FOLDER, invoice_file)
    purchase_path = os.path.join(PURCHASE_FOLDER, purchase_file)
    invoice_text = pdf_to_text_plumber(invoice_path)
    purchase_text = pdf_to_text_plumber(purchase_path)
    ai_analysis = get_ai_errors_cooperative(invoice_text, purchase_text)
    session["coop_inv_data"] = ai_analysis.get("invoice_extracted", {})
    session["coop_po_data"] = ai_analysis.get("purchase_extracted", {})
    session["coop_errors"] = ai_analysis.get("errors", [])
    session["coop_duration"] = round(time.time() - session["cooperative_start_time"], 2)
    session["coop_original_errors"] = session["coop_errors"][:]
    session.pop("current_first_decision", None)
    session.pop("current_second_decision", None)
    session.pop("coop_ai_response", None)
    session.pop("fix_result", None)
    if session.get("cooperative_count", 0) >= 3:
        next_url = url_for("cooperative_done")
    else:
        next_url = url_for("cooperative_display")
    return jsonify({"redirect_url": next_url})


@app.route("/cooperative_display")
def cooperative_display():
    """
    Renders the cooperative level display page with AI analysis and options for further decisions.
    """
    return render_template("cooperative.html",
                           errors=session.get("coop_errors", []),
                           inv_data=session.get("coop_inv_data"),
                           po_data=session.get("coop_po_data"),
                           fix_result=session.get("fix_result", False),
                           show_second_decision=session.get("show_second_decision", False),
                           show_ai_instructions=session.get("show_ai_instructions", False),
                           ai_response=session.get("coop_ai_response", ""))


@app.route("/cooperative_decision", methods=["POST"])
def cooperative_decision():
    """
    Processes the user decision in the cooperative level.
    Depending on the first and second decisions and any AI instructions, it either finalizes the decision,
    triggers an AI fix, or re-displays the page.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    first_decision = request.form.get("first_decision")
    second_decision = request.form.get("second_decision")
    ai_instructions = request.form.get("ai_instructions", "").strip()
    booking_decision = request.form.get("booking_decision")
    if booking_decision:
        session["coop_booking"] = booking_decision
    else:
        session["coop_booking"] = session.get("coop_booking", "unknown")
    if first_decision:
        session["current_first_decision"] = first_decision
    if second_decision:
        session["current_second_decision"] = second_decision
    session.modified = True
    if first_decision == "ok":
        session["coop_booking"] = "book"
        return finalize_cooperative()
    elif first_decision == "not_ok":
        if second_decision:
            if second_decision == "ai_fix":
                if not ai_instructions:
                    session["show_second_decision"] = False
                    session["show_ai_instructions"] = True
                    return render_template("cooperative.html",
                                           errors=session.get("coop_errors", []),
                                           inv_data=session.get("coop_inv_data"),
                                           po_data=session.get("coop_po_data"),
                                           fix_result=False,
                                           show_second_decision=False,
                                           show_ai_instructions=True,
                                           ai_response=session.get("coop_ai_response", ""))
                else:
                    return do_ai_fix(ai_instructions)
            elif second_decision == "no_fix":
                session["coop_booking"] = "decline"
                return finalize_cooperative()
            else:
                session["show_second_decision"] = True
                session["show_ai_instructions"] = False
                return render_template("cooperative.html",
                                       errors=session.get("coop_errors", []),
                                       inv_data=session.get("coop_inv_data"),
                                       po_data=session.get("coop_po_data"),
                                       fix_result=False,
                                       show_second_decision=True,
                                       show_ai_instructions=False,
                                       ai_response=session.get("coop_ai_response", ""))
        else:
            session["show_second_decision"] = True
            session["show_ai_instructions"] = False
            return render_template("cooperative.html",
                                   errors=session.get("coop_errors", []),
                                   inv_data=session.get("coop_inv_data"),
                                   po_data=session.get("coop_po_data"),
                                   fix_result=False,
                                   show_second_decision=True,
                                   show_ai_instructions=False,
                                   ai_response=session.get("coop_ai_response", ""))
    else:
        flash(f"Unknown first decision: {first_decision}")
        return render_template("cooperative.html",
                               errors=session.get("coop_errors", []),
                               inv_data=session.get("coop_inv_data"),
                               po_data=session.get("coop_po_data"),
                               fix_result=False,
                               show_second_decision=False,
                               show_ai_instructions=False,
                               ai_response=session.get("coop_ai_response", ""))


def do_ai_fix(instructions):
    """
    Executes the AI fix based on the provided instructions.
    Updates the session data with the AI-corrected invoice data.
    """
    user_id = session.get("user_id")
    inv_data = session.get("coop_inv_data")
    po_data = session.get("coop_po_data")
    result_json = fix_invoice_with_chatgpt(inv_data, po_data, instructions)
    updated_inv_data = result_json.get("invoice_extracted", {})
    updated_po_data = result_json.get("purchase_extracted", {})
    updated_errors = result_json.get("errors", [])
    ai_answer = result_json.get("ai_answer", "")
    session["coop_inv_data"] = updated_inv_data
    session["coop_po_data"] = updated_po_data
    session["coop_errors"] = updated_errors
    session["coop_ai_response"] = ai_answer
    session["fix_result"] = True
    return render_template("cooperative.html",
                           errors=updated_errors,
                           inv_data=updated_inv_data,
                           po_data=updated_po_data,
                           fix_result=True,
                           ai_response=ai_answer,
                           show_second_decision=False,
                           show_ai_instructions=False)


@app.route("/cooperative_next_decision", methods=["POST"])
def cooperative_next_decision():
    """
    Handles the decision after an AI fix.
    Depending on the next decision from the user, it either finalizes the cooperative level or
    redirects back to reprocess the current invoice.
    """
    next_decision = request.form.get("next_decision")
    if next_decision == "ok_now":
        session["coop_booking"] = "book"
        return finalize_cooperative()
    elif next_decision == "still_error":
        session["fix_result"] = False
        session["show_ai_instructions"] = False
        session["show_second_decision"] = True
        return redirect(url_for("cooperative_process"))
    else:
        flash("Invalid decision")
        return redirect(url_for("cooperative_process"))


def finalize_cooperative():
    """
    Finalizes the cooperative level by saving it as a JSON, 
    resetting level-specific session keys
    and redirecting to the next invoice or the done page.
    """
    user_id = session.get("user_id")
    duration = round(time.time() - session["cooperative_start_time"], 2)
    data_entry = {
        "invoice_file": session.get("current_invoice"),
        "purchase_file": session.get("current_purchase"),
        "duration_seconds": duration,
        "invoice_extracted": session.get("coop_inv_data"),
        "purchase_extracted": session.get("coop_po_data"),
        "errors_found": session.get("coop_errors", []),
        "booking": session.get("coop_booking", "unknown"),
    }
    user_folder = os.path.join(RESULTS_FOLDER, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    json_path = os.path.join(user_folder, "cooperative.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    else:
        old_data = []
    old_data.append(data_entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(old_data, f, indent=2, ensure_ascii=False)
    session["cooperative_count"] = session.get("cooperative_count", 0) + 1
    for key in ["coop_errors", "coop_inv_data", "coop_po_data", "current_invoice", "current_purchase",
                "coop_ai_response", "fix_result", "coop_booking", "show_second_decision",
                "show_ai_instructions", "current_first_decision", "current_second_decision"]:
        session.pop(key, None)
    if session["cooperative_count"] < 3:
        if not pick_random_pair():
            return "No more invoices available."
        return redirect(url_for("cooperative"))
    else:
        return redirect(url_for("cooperative_done"))


@app.route("/reset_fix_state", methods=["POST"])
def reset_fix_state():
    """
    Resets the AI fix state, clearing fix result and instruction flags.
    """
    session.pop("show_ai_instructions", None)
    session.pop("fix_result", None)
    return "", 204


@app.route("/cooperative_done")
def cooperative_done():
    """
    Renders the done page for the cooperative level.
    """
    return render_template("cooperative_done.html")


# ---------------------------
# SUPERVISORY CONTROL LEVEL
# ---------------------------
# This level uses AI for invoice verification and only passes it on to the user in exceptional cases.
@app.route("/supervisory_control_explain")
def supervisory_control_explain():
    if "user_id" not in session:
        return redirect(url_for("enter_id"))
    return render_template("supervisory_control_explain.html")


@app.route("/supervisory_control")
def supervisory_control():
    """
    Displays the assistive level loading page.
    Resets data, selects a random pair and checks if the user has completed 3 invoices.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    reset_level_specific_data("supervisory")
    if "supervisory_count" not in session:
        session["supervisory_count"] = 0
    if session["supervisory_count"] >= 3:
        return redirect(url_for("supervisory_control_done"))
    session["supervisory_start_time"] = time.time()
    if not pick_random_pair():
        return "No matching pairs found."
    return render_template("supervisory_control_waiting.html")


@app.route("/supervisory_control_process", methods=["POST"])
def supervisory_control_process():
    """
    Processes the supervisory control level.
    It calls the AI to decide on processing and saves the results.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "No user_id"}), 400
    invoice_file = session.get("current_invoice")
    purchase_file = session.get("current_purchase")
    if invoice_file.startswith("modified_invoice_"):
        invoice_path = os.path.join(MODIFIED_INVOICE_FOLDER, invoice_file)
    else:
        invoice_path = os.path.join(INVOICE_FOLDER, invoice_file)
    purchase_path = os.path.join(PURCHASE_FOLDER, purchase_file)
    invoice_text = pdf_to_text_plumber(invoice_path)
    purchase_text = pdf_to_text_plumber(purchase_path)
    ai_result = decide_with_chatgpt(invoice_text, purchase_text)
    duration = round(time.time() - session["supervisory_start_time"], 2)
    decision = ai_result.get("decision", "auto")
    if decision == "auto":
        booking = ai_result.get("booking", "book")
    else:
        booking = ai_result.get("booking", "decline")
    session["sc_inv_data"] = ai_result.get("invoice_extracted", {})
    session["sc_po_data"] = ai_result.get("purchase_extracted", {})
    session["sc_errors"] = ai_result.get("errors", [])
    session["sc_duration"] = duration
    session["sc_decision"] = decision
    session["sc_booking"] = booking
    data_entry = {
        "invoice_file": invoice_file,
        "purchase_file": purchase_file,
        "duration_seconds": duration,
        "invoice_extracted": session["sc_inv_data"],
        "purchase_extracted": session["sc_po_data"],
        "errors": session["sc_errors"],
        "decision": decision,
        "booking": booking
    }
    user_folder = os.path.join(RESULTS_FOLDER, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    json_path = os.path.join(user_folder, "supervisory_control.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    else:
        old_data = []
    old_data.append(data_entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(old_data, f, indent=2, ensure_ascii=False)
    if decision == "escalate":
        next_url = url_for("supervisory_control_manual")
    else:
        session["supervisory_count"] += 1
        next_url = url_for("supervisory_control") if session["supervisory_count"] < 3 else url_for("supervisory_control_done")
    return jsonify({"redirect_url": next_url})


@app.route("/supervisory_control_manual")
def supervisory_control_manual():
    """
    Renders the supervisory control manual intervention page,
    allowing the supervisor to review AI errors and data.
    """
    return render_template("supervisory_control.html",
                           errors=session.get("sc_errors", []),
                           inv_data=session.get("sc_inv_data", {}),
                           po_data=session.get("sc_po_data", {}))


@app.route("/supervisory_control_done")
def supervisory_control_done():
    """
    Renders the done page for the supervisory control level.
    """
    return render_template("supervisory_control_done.html")


@app.route("/supervisor_note", methods=["POST"])
def supervisor_note():
    """
    Processes the supervisor's note and booking decision.
    Saves the data for the supervisory control level.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    supervisor_note = request.form.get("supervisor_note", "").strip()
    booking_decision = request.form.get("booking_decision", "decline")
    if not supervisor_note:
        return "Error: Supervisor Note is required.", 400
    duration = round(time.time() - session["supervisory_start_time"], 2)
    data_entry = {
        "invoice_file": session.get("current_invoice"),
        "purchase_file": session.get("current_purchase"),
        "duration_seconds": duration,
        "invoice_extracted": session.get("sc_inv_data"),
        "purchase_extracted": session.get("sc_po_data"),
        "errors": session.get("sc_errors", []),
        "supervisor_note": supervisor_note,
        "decision": session.get("sc_decision", "auto"),
        "booking": booking_decision
    }
    user_folder = os.path.join(RESULTS_FOLDER, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    json_path = os.path.join(user_folder, "supervisory_control.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    else:
        old_data = []
    old_data.append(data_entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(old_data, f, indent=2, ensure_ascii=False)
    session["supervisory_count"] = session.get("supervisory_count", 0) + 1
    if session["supervisory_count"] < 3:
        if not pick_random_pair():
            return "No more invoices available."
        return redirect(url_for("supervisory_control"))
    else:
        return redirect(url_for("supervisory_control_done"))


# ---------------------------
# FULLY AUTOMATED LEVEL
# ---------------------------
# In this level the entire process is automated with AI
@app.route("/fully_automated_explain")
def fully_automated_explain():
    if "user_id" not in session:
        return redirect(url_for("enter_id"))
    return render_template("fully_automated_explain.html")


@app.route("/fully_automated")
def fully_automated():
    """
    Displays the fully automated level.
    Resets data, selects a random pair and checks if the user has completed 3 invoices.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("enter_id"))
    reset_level_specific_data("fully_automated")
    if "auto_count" not in session:
        session["auto_count"] = 0
    if session["auto_count"] >= 3:
        return redirect(url_for("fully_automated_done"))
    session["automated_start_time"] = time.time()
    if not pick_random_pair():
        return "No matching pairs found."
    return render_template("fully_automated.html",
                           invoice_file=session.get("current_invoice"),
                           purchase_file=session.get("current_purchase"))


@app.route("/fully_automated_process", methods=["POST"])
def fully_automated_process():
    """
    Processes the fully automated level:
    - Calls the AI to analyze and decide on the invoice
    - Saves the result as JSON
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "No user_id"}), 400
    invoice_file = session.get("current_invoice")
    purchase_file = session.get("current_purchase")
    if invoice_file.startswith("modified_invoice_"):
        invoice_path = os.path.join(MODIFIED_INVOICE_FOLDER, invoice_file)
    else:
        invoice_path = os.path.join(INVOICE_FOLDER, invoice_file)
    purchase_path = os.path.join(PURCHASE_FOLDER, purchase_file)
    invoice_text = pdf_to_text_plumber(invoice_path)
    purchase_text = pdf_to_text_plumber(purchase_path)
    ai_result = get_fully_auto_result(invoice_text, purchase_text)
    inv_data = ai_result.get("invoice_extracted", {})
    po_data = ai_result.get("purchase_extracted", {})
    errors = ai_result.get("errors", [])
    corrected_invoice = ai_result.get("invoice_corrected", {})
    booking = ai_result.get("booking", "decline")
    duration = round(time.time() - session["automated_start_time"], 2)
    data_entry = {
        "invoice_file": invoice_file,
        "purchase_file": purchase_file,
        "duration_seconds": duration,
        "invoice_extracted": inv_data,
        "purchase_extracted": po_data,
        "errors": errors,
        "booking": booking
    }
    if corrected_invoice:
        data_entry["invoice_corrected"] = corrected_invoice
    user_folder = os.path.join(RESULTS_FOLDER, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    json_path = os.path.join(user_folder, "fully_automated.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    else:
        old_data = []
    old_data.append(data_entry)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(old_data, f, indent=2, ensure_ascii=False)
    session["auto_count"] += 1
    next_url = url_for("fully_automated") if session["auto_count"] < 3 else url_for("fully_automated_done")
    return jsonify({"redirect_url": next_url})


@app.route("/fully_automated_done")
def fully_automated_done():
    """
    Renders the done page for the fully automated level.
    """
    return render_template("fully_automated_done.html")


if __name__ == "__main__":
    app.run(debug=True)
