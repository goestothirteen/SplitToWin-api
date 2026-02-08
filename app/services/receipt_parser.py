import json
import os
import google.generativeai as genai

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# Use Flash for speed/cost-efficiency on receipts
model = genai.GenerativeModel("gemini-2.5-flash")

def parse_receipt_text(ocr_text: str):
    prompt = f'''Extract receipt items from the text below. If there are any kind of GST or Service Charge, please include them as individual items in the json. 
        Return ONLY valid JSON in this format:
        {{
        "items": [{{ "name": string, "price": number }}],
        "total": number

        }}
        Here is the receipt text:
        {ocr_text}'''
            
    # Use 'response_mime_type' to force JSON output
    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"}
    )

    try:
        # Much safer than eval()
        return json.loads(response.text)
    except json.JSONDecodeError:
        return {"error": "Failed to parse JSON", "raw": response.text}