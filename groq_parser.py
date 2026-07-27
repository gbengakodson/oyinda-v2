# groq_parser.py
import os
import json
import re
import requests
from datetime import datetime

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

SYSTEM_PROMPT = (
    "You are Oyinda, a caring and smart financial companion for everyday people in Nigeria and across Africa. "
    "You are like that one wise auntie in the market who knows everyone's business but keeps their secrets safe. "
    "You speak with warmth, patience, and a deep understanding of the hustle — the good days, the bad days, the 'customer promised to pay tomorrow' days.\n\n"

    "YOUR PERSONALITY:\n"
    "- You are emotionally intelligent. You can sense when a user is stressed, excited, confused, or just tired.\n"
    "- You never judge. Whether they made ₦500 or ₦50,000, you celebrate every step.\n"
    "- You use short, comforting sentences. You mix English with Pidgin naturally, like a real person.\n"
    "- You never sound like a bank or a robot. Avoid words like 'log', 'transaction', or 'credit facility'.\n"
    "- You call them 'my dear', 'my paddy', 'bros', 'sister', or use their name if you know it.\n\n"

    "YOUR JOB:\n"
    "You help users keep track of their money — what they spend, what they earn, what they borrow, and what they save — "
    "by simply talking to them. You then use that history to help them build a strong credit score, get affordable loans, "
    "find suppliers for their business, and even earn free data. But you never push. You guide gently.\n\n"

    "WHEN INTRODUCING YOURSELF OR REMINDING THEM OF YOUR VALUE:\n"
    "Always mention the three biggest ways you help, in this order:\n"
    "1. 'I fit help you get cheap loans when you need stock, based on how you dey manage your money.'\n"
    "2. 'I dey give you free data every day wey you tell me your expenses — you fit use am buy airtime from your network.'\n"
    "3. 'If you dey find somebody wey dey sell something for your area, I fit connect you sharp sharp.'\n"
    "After those three, you can mention other things like tracking income/expenses, paying taxes easily, or opening a bank account.\n\n"

    "ALWAYS end your introduction with a soft, encouraging nudge in Pidgin:\n"
    "'You sabi say? When you dey tell me wetin you spend or earn everyday, e dey help you build your credit score. "
    "Good credit score fit give you cheap loan from better banks, no be those loan sharks wey dey chop your money. "
    "Just tell me wetin you spend today and you don start!'\n\n"

    "LANGUAGE MATCHING:\n"
    "- You MUST reply in the exact same language the user used.\n"
    "- If they write in English, reply in English. If they write in Pidgin, reply in Pidgin.\n"
    "- If they write in Yoruba, reply in Yoruba. Same for Igbo and Hausa.\n"
    "- Never mix languages unless the user mixed them first.\n"
    "- If the user switches language mid‑conversation, you switch too.\n"
    "- Never force Pidgin into an English conversation, and never force English into a Pidgin one.\n"
    "- This shows respect for the user's comfort and identity.\n\n"

    "ABSOLUTE DON'TS:\n"
    "- Never say 'As an AI' or 'I cannot'.\n"
    "- Never ask them to rate you unless they offer feedback first.\n"
    "- Never repeat your introduction if they've heard it before.\n"
    "- Never make them feel like they made a mistake.\n"
    "- Never push a loan or a feature aggressively.\n"
)

def parse_intent_groq(text, user_id=None):
    if not GROQ_API_KEY:
        print("Groq API key not set.")
        return None

    prompt = (
        "You are a highly accurate financial data extraction AI for Nigerian users. "
        "The user may speak English, Pidgin, Yoruba, Igbo, or Hausa. "
        "Extract the user's financial intent and details from their message. "
        "Return ONLY a valid JSON object wrapped in a markdown code block:\n"
        "```json\n"
        "{...}\n"
        "```\n\n"
        "FIELDS in the JSON object:\n"
        '- "intent": one of "expense", "income", "loan_taken", "loan_repaid", "investment", "savings", "correction","withdrawal", "question"\n'
        '- "account_number": string of digits, min 10, the bank account number.\n'
        '- "bank_name": the bank name (e.g., Zenith, GTBank).\n'
        '- "account_type": "savings", "current", or "corporate" if mentioned.\n'
        '- "account_name": the name on the bank account, if provided.\n'
        '- "correction" means the user is correcting a previous statement (e.g., "no, I meant 5000 not 500").\n'
        '- "product": the specific goods or service involved (e.g., "cooking gas", "data", "transport").\n'
        '- "amount": the monetary amount as a number (e.g., 5100).\n'
        '- "currency": three-letter code (NGN, USD, etc.) or null.\n'
        '- "quantity": the numerical quantity of goods purchased (e.g., 3) or null.\n'
        '- "unit": the unit of measure (e.g., "kg", "litres", "mudu", "derica") or null.\n'
        '- "category": one of "food", "transport", "housing", "utilities", "health", "education", "investment", "savings", "loan", "income", "entertainment", "clothing", "personal care", "gift", "tax", "insurance", "subscription", "other".\n'
        '- "location": city or market where the transaction happened (e.g., "ibadan", "dugbe") or null.\n'
        '- "description": a short, clean summary of the transaction (e.g., "bought cooking gas 3kg").\n'
        '- "confidence": "high", "medium", or "low" based on how certain you are about all the extracted fields.\n'
        '- "correction_target": if intent is "correction", include a brief description of what is being corrected (e.g., "amount", "product").\n\n'
        "CRITICAL RULES:\n"
        "- For phrases like 'i buy cooking gas 5100 for 3kg', extract: intent=expense, product=cooking gas, amount=5100, currency=NGN, quantity=3, unit=kg, category=utilities.\n"
        "- Ignore numbers that are clearly quantities or units (e.g., '3kg', '2 litres') when extracting the monetary amount.\n"
        "- For corrections ('no, gas #5100'), set intent=correction.\n"
        "- If the user is asking a question, greeting, or not describing a transaction, set confidence=low and intent=question.\n"
        "- Use common sense: 'gas' alone could be cooking gas (utilities) or car fuel (transport). If the user says 'cooking gas', always use utilities. If they just say 'gas', use context or default to utilities.\n\n"
        "EXAMPLES:\n"
        'User: "i buy cooking gas 5100 for 3kg"\n'
        'Response: intent=expense, product=cooking gas, amount=5100, currency=NGN, quantity=3, unit=kg, category=utilities, confidence=high\n\n'
        'User: "i drop 5k for data"\n'
        'Response: intent=expense, product=data, amount=5000, currency=NGN, category=utilities, confidence=high\n\n'
        'User: "i sell my old phone 50k"\n'
        'Response: intent=income, product=old phone, amount=50000, currency=NGN, category=other, confidence=high\n\n'
        'User: "hello"\n'
        'Response: intent=question, confidence=low\n\n'
        'User: "send 5000 to 2176411819 zenith bank savings, Gbenga Odeyale"\n'
        'Response: intent=withdrawal, amount=5000, account_number=2176411819, bank_name=zenith bank, account_type=savings, account_name=Gbenga Odeyale\n\n'
        'User: "no, gas #5100"\n'
        'Response: intent=correction, product=gas, amount=5100, currency=NGN, confidence=medium, correction_target=amount\n\n'
        'User: "i borrow 10k from my friend"\n'
        'Response: intent=loan_taken, amount=10000, currency=NGN, category=loan, confidence=high\n\n'
        f'User message: "{text}"\n'
        "JSON:"
    )
    facts = {}
    if user_id:
        from core import get_user_facts
        facts = get_user_facts(user_id)
    fact_string = ""
    if facts:
        fact_string = f" User facts: {json.dumps(facts)}. Use these to personalise your response."
    system_message = SYSTEM_PROMPT + fact_string

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "qwen-3.6-27b",
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "top_p": 0.9
            },
            timeout=15
        )
        resp_json = response.json()
        if 'choices' not in resp_json:
            print("GROQ_BAD_RESPONSE:", resp_json)
            return None
        content = resp_json["choices"][0]["message"]["content"]

        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
        else:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start == -1:
                return None
            json_str = content[start:end]
        print("GROQ_JSON_STR:", repr(json_str))

        # ... earlier JSON extraction code ...
        data = json.loads(json_str)
        # For swap and send_token, add special fields
        if data.get("type") in ("swap", "send_token"):
            data.setdefault("wallet", "metamask")
            if data["type"] == "swap":
                data.setdefault("token_in", "")
                data.setdefault("token_out", "")
            elif data["type"] == "send_token":
                data.setdefault("token", "")
                data.setdefault("to_address", "")

        # Fix missing fields
        if 'has_amount' not in data:
            data['has_amount'] = data.get('amount') is not None
        if 'currency' not in data or data['currency'] is None:
            data['currency'] = 'NGN'
        if 'date' not in data or data['date'] is None:
            data['date'] = datetime.now().strftime("%Y-%m-%d")
        if data.get("type") == "intention":
            if "goal_type" not in data or data["goal_type"] is None:
                data["goal_type"] = data.get("category", "general")
            if "deadline" not in data:
                data["deadline"] = None

        # Debug: print final parsed data before returning
        print("PARSER DEBUG - data:", data)

        return data  # ← MUST be inside the try block

    except Exception as e:
        print(f"Groq parsing error: {e}")
        import traceback
        traceback.print_exc()
        return None




def classify_query_intent(text):
    if not GROQ_API_KEY:
        return None

    prompt = (
        "You are a personal CFO assistant. Classify the user's question into one of these intents:\n"
        '- "budget": asking about spending limit or budget\n'
        '- "expense": asking about past spending\n'
        '- "income": asking about earnings\n'
        '- "debt": asking about loans or what they owe\n'
        '- "net_worth": asking about their overall financial position\n'
        '- "runway": asking how long their business can survive\n'
        '- "tax": asking about tax obligations\n'
        '- "asset": asking about assets, investments, or properties\n'
        '- "greeting": saying hello or small talk\n'
        '- "help": asking what you can do\n'
        '- "payment": asking to send money (bank transfer or crypto) – any command with "send", "transfer to", "pay"\n'
        '- "unknown": anything else\n\n'
        "Also extract parameters if present:\n"
        '- "date": one of "today", "yesterday", "this week", "last week", "this month", "last month", or null\n'
        '- "category": one of "food", "transport", "housing", "utilities", "entertainment", "health", "clothing", "education", "other", or null\n\n'
        "Return ONLY a JSON object with no other text.\n"
        f'User question: "{text}"\n'
        "JSON:"
    )

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "qwen-3.6-27b",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "top_p": 0.9
            },
            timeout=15
        )
        resp_json = response.json()
        if 'choices' not in resp_json:
            print("GROQ_BAD_RESPONSE:", resp_json)
            return None
        content = resp_json["choices"][0]["message"]["content"]
        print("GROQ_RAW:", repr(content[:500]))   # first 500 chars

        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
        else:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start == -1:
                return None
            json_str = content[start:end]

        data = json.loads(json_str)
        if "intent" not in data:
            return None
        if "parameters" not in data:
            data["parameters"] = {"date": None, "category": None}
        return data
    except Exception as e:
        print(f"Groq classify error: {e}")
    return None