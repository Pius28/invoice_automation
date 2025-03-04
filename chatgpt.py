import openai
import json
import sys
import locale

# Reconfigure stdout and stderr to support UTF-8 encoding
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# Set locale to ensure proper formatting for numbers, dates, etc.
locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')

# Encode the input string to UTF-8, replacing invalid characters, and then decode it back to a UTF-8 string
def handle_encoding(s):
  return s.encode('utf-8', 'replace').decode('utf-8')

# ---------------------------
# AI for Assistive
# ---------------------------
def get_ai_errors_from_pdfs(invoice_pdf_text, purchase_pdf_text):
  """
  Calls the AI to extract invoice and purchase order data from given PDF texts.
  It also detects any errors based on discrepancies between the invoice and purchase order.
  
  Parameters:
    invoice_pdf_text (str): Text content of the invoice PDF.
    purchase_pdf_text (str): Text content of the purchase order PDF.
  
  Returns:
    dict: JSON object containing extracted invoice data, purchase order data, and a list of errors.
  """
  # Define the system prompt that instructs the AI how to process the PDFs.
  system_prompt = """\
You are an AI assistant and receive two PDF texts:
1) invoice_pdf_text (invoice)
2) purchase_pdf_text (purchase order)

### 1. Extract the following fields
- order_id
- order_date
- contact_name (for the invoice) or customer_name (for the order)
- items: { product_id, product_name, quantity, unit_price }
- total_price (only for the invoice)

### 2. Rules for price comparisons
- All numeric values (Quantity, Unit Price, etc.) must be interpreted as floats.
- Values like "9" and "9.0" or "32" and "32.0" are identical and must not be considered errors.
- Ignore trailing zeros after the decimal point (e.g., "44.0" and "44" should be treated as the same value).
- Only consider it an error if the numerical value is truly different.

### 3. Error messages and corrections
- If you find any discrepancies, create a list called "errors" with entries in the following format:
  {
    "error_type": "...",
    "description": "..." (as short as possible),
    "correction": "..." (as short as possible)
  }
- Use only the following error types:
  - "Order ID"
  - "Date"
  - "Contact Name"
  - "Product ID"
  - "Product Name"
  - "Quantity"
  - "Unit Price"
  - "Total Price"
  - "Product is missing"
- **Always use the value from the purchase order** for the correction.
- Always calculate the correct value of "Total Price" and only store the correct result in correction.
- If there are no errors, the errors list will be empty.

### 4. Output format
Only return a JSON object in the following format, without any additional explanations or comments:
{
  "invoice_extracted": {...},
  "purchase_extracted": {...},
  "errors": [...],
}
- Use short, concise field names.
"""
  # Construct the user prompt with the provided PDF texts
  user_prompt = f"""{{
  "invoice_pdf_text": "{invoice_pdf_text}",
  "purchase_pdf_text": "{purchase_pdf_text}"
}}"""

  # Request the AI's completion using the specified model and prompts
  response = openai.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
      {"role": "system", "content": system_prompt},
      {"role": "user", "content": user_prompt}
    ],
    temperature=0.0
  )

  # Extract and clean the AI's response
  raw_content = response.choices[0].message.content.strip()

  try:
    parsed_json = json.loads(raw_content)
    # Verify that the parsed JSON is a dictionary and contains an "errors" key
    if not isinstance(parsed_json, dict):
      raise ValueError("Response is not a JSON object")
    if "errors" not in parsed_json:
      raise KeyError("Missing 'errors' key in response")
  except (json.JSONDecodeError, ValueError, KeyError):
    # Fallback to an empty structure if parsing fails
    parsed_json = {
      "invoice_extracted": {},
      "purchase_extracted": {},
      "errors": []
    }
  return parsed_json

def get_ai_suggestions(inv_data, po_data, errors, corrections):
  """
  Generates concise markdown bullet point suggestions based on error types detected.
  
  Parameters:
    inv_data (dict): Extracted invoice data.
    po_data (dict): Extracted purchase order data.
    errors (list): List of error objects detected.
    corrections (list): List of correction objects with validation flags.
  
  Returns:
    str: Markdown bullet points with error suggestions.
  """
  # Determine valid corrections from the corrections list
  valid_corrections = [c["type"] for c in corrections if c.get("is_valid")]

  # Filter out errors that have already been addressed by valid corrections
  remaining_errors = []
  for e in errors:
    if isinstance(e, dict):
      error_str = e.get("type") or e.get("error_type")
      if error_str:
        if error_str not in valid_corrections:
          remaining_errors.append({"type": error_str})

  # Get unique error types
  unique_fields = list(dict.fromkeys(e["type"] for e in remaining_errors))
  if not unique_fields:
    return ""

  # Create a string list of errors for the user prompt
  error_list_str = "\n".join([f"- {err}" for err in unique_fields])

  # Define a simple system prompt for rewriting error messages
  system_prompt = "You are a concise assistant that rewrites error messages in plain English."
  user_prompt = f"""
You are a helpful assistant specialized in invoice error validation.
For each unique error type listed below, produce exactly one markdown bullet point.
Your output must explicitly refer to the specific field and indicate that it might be incorrect.
Do not output generic messages like "Ensure all required fields are filled."
For example, if the error type is "ProductID", your output should be similar to "Maybe Product ID is wrong."
Values like "32" and "32.0" are **identical** and **not considered errors**.
Each bullet point should be between 5 and 7 words.
Output only markdown bullet points.
Error types:
{error_list_str}
""".strip()

  try:
    response = openai.chat.completions.create(
      model="gpt-4o-mini",
      messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
      ],
      temperature=0.6,
      max_tokens=150
    )
    return response.choices[0].message.content.strip()
  except Exception as e:
    return f"AI Error: {str(e)}"

# ---------------------------
# AI for Cooperative
# ---------------------------
def get_ai_errors_cooperative(invoice_pdf_text, purchase_pdf_text):
  """
  The AI performs a analysis on invoice and purchase order PDFs.
  Extracts relevant fields, identifies errors, and suggests corrections.
  
  Parameters:
    invoice_pdf_text (str): Invoice PDF content.
    purchase_pdf_text (str): Purchase order PDF content.
  
  Returns:
    dict: JSON object containing extracted data, errors, and corrected invoice data.
  """
  system_prompt = """\
You are an AI assistant and receive two PDF texts:
1) invoice_pdf_text (invoice)
2) purchase_pdf_text (purchase order)

### 1. Extract the following fields
- order_id
- order_date
- contact_name (for the invoice) or customer_name (for the order)
- items: { product_id, product_name, quantity, unit_price }
- total_price (only for the invoice)

### 2. Rules for price comparisons
- All numeric values (Quantity, Unit Price, etc.) must be interpreted as floats.
- Values like "9" and "9.0" or "32" and "32.0" are identical and must not be considered errors.
- Ignore trailing zeros after the decimal point (e.g., "44.0" and "44" should be treated as the same value).
- Only consider it an error if the numerical value is truly different.

### 3. Error messages and corrections
- If you find any discrepancies, create a list called "errors" with entries in the following format:
  {
    "error_type": "...",
    "description": "..." (as short as possible),
    "correction": "..." (as short as possible)
  }
- Use only the following error types:
  - "Order ID"
  - "Date"
  - "Contact Name"
  - "Product ID"
  - "Product Name"
  - "Quantity"
  - "Unit Price"
  - "Total Price"
  - "Product is missing"
- **Always use the value from the purchase order** for the correction.
- Always calculate the correct value of "Total Price" and only store the correct result in correction.
- If there are no errors, the errors list will be empty.

### 4. Output format
Only return a JSON object in the following format, without any additional explanations or comments:
{
  "invoice_extracted": {...},
  "purchase_extracted": {...},
  "errors": [...],
  "invoice_corrected": {...}
}
- If no corrections are necessary, “invoice_corrected” remains empty.
- Use short, concise field names.
"""

  user_prompt = json.dumps({
    "invoice_pdf_text": invoice_pdf_text,
    "purchase_pdf_text": purchase_pdf_text
  }, ensure_ascii=False)

  try:
    response = openai.chat.completions.create(
      model="gpt-4o-mini",
      messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
      ],
      temperature=0.0
    )
    raw_content = response.choices[0].message.content.strip()
    result_json = json.loads(raw_content)
  except Exception as e:
    print("Error in get_ai_errors_cooperative:", e)
    result_json = {
      "invoice_extracted": {},
      "purchase_extracted": {},
      "errors": []
    }
  return result_json

def fix_invoice_with_chatgpt(inv_data, purchase_data, instructions):
  """
  Adjusts the invoice data based on user instructions.
  
  Parameters:
    inv_data (dict): Extracted invoice data.
    purchase_data (dict): Extracted purchase order data.
    instructions (str): User instructions for corrections.
  
  Returns:
    dict: JSON object containing the updated invoice data, errors, and a short AI response.
  """
  system_prompt = """\
You are an AI assistant and receive the following input data:
1) invoice_extracted (already extracted invoice data, JSON)
2) purchase_extracted (PO data, JSON),
3) instructions (user instructions)

### 1. Prioritize user instructions and update "invoice_extracted"
- Example: “Change the product name to "Otto"”. Set the product name to "Otto", even if the PO data contradicts this.
- If the user instruction is in the format "change <field> to <new_value>" (e.g. "change contact name to otto"), update the corresponding field's correction to <new_value>.
- Do not require the text "suggested fix" to be present.
- Carefully check if the error type, name, or number mentioned in the instruction matches an existing error.
- If the instruction is unclear, only modify the specific field listed in the error list, not all fields.
- If the user only writes "ok" or other words/sentences without clear instructions, inform them that you did not make any changes because the instructions were unclear.
- If the user only instructs you to change the "Unit Price," do not remove or alter any other fields (like "Product ID," "Product Name," etc.). Keep them exactly as they were unless the user explicitly says to change them.
- If the user provides a partial instruction such as "total price 123", that does NOT mean you should ignore or remove other discrepancies. Keep all previously identified errors (like a date mismatch) unless the user explicitly instructs you to fix or remove them.

### 2. Rules for price comparisons
- All numeric values (Quantity, Unit Price, etc.) must be interpreted as floats.
- Values like "9" and "9.0" or "32" and "32.0" are identical and must not be considered errors.
- Ignore trailing zeros after the decimal point (e.g., "44.0" and "44" should be treated as the same value).
- Only consider it an error if the numerical value is truly different.

### 3. Error messages and corrections
- If you find any discrepancies, create a list named “errors” with entries in the following format:
  {
    "error_type": "...",
    "description": "..." (as short as possible),
    "correction": "..." (as short as possible)
  }
- Use only the following error types:
  - "Order ID"
  - "Date"
  - "Contact Name"
  - "Product ID"
  - "Product Name"
  - "Quantity"
  - "Unit Price"
  - "Total Price"
  - "Product is missing"
- **Always use the value from the purchase order** for the correction.
- Always calculate the correct value of "Total Price" and only store the correct result in correction.
- If there are no errors, the errors list remains empty.
- **ALWAYS** list ALL ERRORS you found. If you found an error an the user says something like "total price is also wrong" and if he is right then list BOTH errors.

### 4. Output format
Return only a JSON object in the following format, without any additional explanations or comments:
{
  "invoice_extracted": {...},
  "purchase_extracted": {...},
  "errors": [...],
  "ai_answer": "...(answer the question in a few words.)..."
}
- If no corrections are needed, "invoice_corrected" remains empty.
- Use short, concise field names.
"""
  user_prompt = {
    "invoice_extracted": inv_data,
    "purchase_extracted": purchase_data,
    "instructions": instructions
  }

  try:
    response = openai.chat.completions.create(
      model="gpt-4o-mini",
      messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)}
      ],
      temperature=0.0
    )
    raw_content = response.choices[0].message.content.strip()
    result_json = json.loads(raw_content)

    result_json["ai_response"] = raw_content
    result_json["user_message"] = instructions

    return result_json
  except Exception as e:
    return {
      "invoice_extracted": inv_data,
      "purchase_extracted": purchase_data,
      "errors": [],
      "ai_response": "",
      "user_message": instructions
    }

# ---------------------------
# AI for Supervisory
# ---------------------------
def decide_with_chatgpt(invoice_pdf_text, purchase_pdf_text):
  """
  Uses the AI to decide whether an invoice should be processed automatically or escalated.
  
  Parameters:
    invoice_pdf_text (str): Invoice PDF content.
    purchase_pdf_text (str): Purchase order PDF content.
  
  Returns:
    dict: JSON object containing extracted data, errors, decision ("auto" or "escalate"),
          and booking status ("book" or "decline").
  """
  system_prompt = """\
You are an AI assistant and receive two PDF texts:
1) invoice_pdf_text (invoice)
2) purchase_pdf_text (purchase order)

### 1. Extract the following fields
- order_id
- order_date
- contact_name (for the invoice) or customer_name (for the order)
- items: { product_id, product_name, quantity, unit_price }
- total_price (only for the invoice)

### 2. Rules for price comparisons
- All numeric values (Quantity, Unit Price, etc.) must be interpreted as floats.
- Values like "9" and "9.0" or "32" and "32.0" are identical and must not be considered errors.
- Ignore trailing zeros after the decimal point (e.g., "44.0" and "44" should be treated as the same value).
- Only consider it an error if the numerical value is truly different.

### 3. Error messages and corrections
- If you find any discrepancies, create a list named “errors” with entries in the following format:
  {
    "error_type": "...",
    "description": "..." (as short as possible),
    "correction": "..." (as short as possible)
  }
- Use only the following error types:
  - "Order ID"
  - "Date"
  - "Contact Name"
  - "Product ID"
  - "Product Name"
  - "Quantity"
  - "Unit Price"
  - "Total Price"
  - "Product is missing"
- **Always use the value from the purchase order** for the correction.
- Always calculate the correct value of "Total Price" and only store the correct result in correction.
- If there are no errors, the errors list remains empty.

### 4. Decision
- Set "decision" to "auto" if there are at most 2 minor errors (e.g. rounding errors, typos).
- Set "decision" to "escalate" if there are more than 2 errors or any major discrepancies (e.g. differences in quantity or unit price that conflict with the purchase order, missing contact/customer name, Order ID, Order Date).

### 5. Booking criteria
- If there are only minor errors (e.g. a single extra or missing letter in a name, or a digit transposition) and no serious discrepancies, set "booking" to "book".
- If a product is missing or there are more than two minor errors, or any serious error that undermines trust, set "booking" to "decline".

### 6. Output format
Only return a JSON object in the following format, without additional explanations or comments:
{
  "invoice_extracted": {...},
  "purchase_extracted": {...},
  "errors": [...],
  "decision": "auto" or "escalate",
  "booking": "book" or "decline"
}
- Use short, concise field names.
"""

  user_prompt = f"""{{
  "invoice_pdf_text": "{invoice_pdf_text}",
  "purchase_pdf_text": "{purchase_pdf_text}"
}}"""

  try:
    response = openai.chat.completions.create(
      model="gpt-4o-mini",
      messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
      ],
      temperature=0.0
    )
    raw_content = response.choices[0].message.content.strip()

    parsed = {}
    try:
      parsed = json.loads(raw_content)
    except json.JSONDecodeError:

      parsed = {
        "invoice_extracted": {},
        "purchase_extracted": {},
        "errors": [],
        "decision": "auto",
        "booking": "decline"
      }
    return parsed

  except Exception as e:

    return {
      "invoice_extracted": {},
      "purchase_extracted": {},
      "errors": [],
      "decision": "auto",
      "booking": "decline"
    }

# ---------------------------
# AI for Fully Automated
# ---------------------------
def get_fully_auto_result(invoice_pdf_text, purchase_pdf_text):
  """
  Processes invoice and purchase order PDFs to extract data, detect errors, and
  decide on corrections and booking status in a fully automated manner.
  
  Parameters:
    invoice_pdf_text (str): Invoice PDF content.
    purchase_pdf_text (str): Purchase order PDF content.
  
  Returns:
    dict: JSON object containing extracted data, errors, corrected invoice data, and booking status.
  """
  system_prompt = """\
You are an AI assistant and receive two PDF texts:
1) invoice_pdf_text (invoice)
2) purchase_pdf_text (purchase order)

### 1. Extract the following fields
- order_id
- order_date
- contact_name (for the invoice) or customer_name (for the order)
- items: { product_id, product_name, quantity, unit_price }
- total_price (only for the invoice)

### 2. Rules for price comparisons
- All numeric values (Quantity, Unit Price, etc.) must be interpreted as floats.
- Values like "9" and "9.0" or "32" and "32.0" are identical and must not be considered errors.
- Ignore trailing zeros after the decimal point (e.g., "44.0" and "44" should be treated as the same value).
- Only consider it an error if the numerical value is truly different.

### 3. Error messages and corrections
- If you find any discrepancies, create a list called “errors” with entries in the following format:
  {
    "error_type": "...",
    "description": "..." (as short as possible),
    "correction": "..." (as short as possible)
  }
- Use only the following error types:
  - "Order ID"
  - "Date"
  - "Contact Name"
  - "Product ID"
  - "Product Name"
  - "Quantity"
  - "Unit Price"
  - "Total Price"
  - "Product is missing"
**Always use the value from the purchase order** for the correction.
- Always calculate the correct value of "Total Price" and only store the correct result in correction.
- If there are no errors, the errors list will be empty.

### 4. Output format
Only return a JSON object in the following format, without any additional explanations or comments:
{
  "invoice_extracted": {...},
  "purchase_extracted": {...},
  "errors": [...],
  "invoice_corrected": {...},
  "booking": "book" or "decline"
}
- If no corrections are needed, "invoice_corrected" will be empty.
- Use short, concise field names.

### 5. Booking criteria
- If there are at most 2 minor errors (e.g. rounding errors, typos) and no serious discrepancies, set "booking" to "book".
- If a product is missing or there are more than two minor errors, or any serious error (e.g. missing contact/customer name, Order ID or Order Date), set "booking" to "decline".
"""

  user_prompt = f"""{{
  "invoice_pdf_text": "{invoice_pdf_text}",
  "purchase_pdf_text": "{purchase_pdf_text}"
}}"""

  response = openai.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
      {"role": "system", "content": system_prompt},
      {"role": "user", "content": user_prompt}
    ],
    temperature=0.0
  )

  raw_content = response.choices[0].message.content.strip()
  try:
    parsed = json.loads(raw_content)
  except json.JSONDecodeError:

    parsed = {
      "invoice_extracted": {},
      "purchase_extracted": {},
      "errors": [],
      "invoice_corrected": {},
      "booking": "decline"
    }
  return parsed
