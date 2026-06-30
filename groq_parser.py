# groq_parser.py
import os
import json
import re
import requests
from datetime import datetime

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

def parse_intent_groq(text):
    if not GROQ_API_KEY:
        print("Groq API key not set.")
        return None

    prompt = (
        "You are a personal CFO assistant for a Nigerian user. The user may speak Nigerian Pidgin, slang, or code‑switched English.\n"
        "Extract financial information from the user message. Return ONLY a valid JSON object wrapped in a markdown code block:\n"
        "```json\n"
        "{...}\n"
        "```\n"
        "Do not include any other text.\n\n"
        "Fields:\n"
        '- "type": one of "expense", "income", "transfer", "liability", "asset", "intention", "swap", "send_token"\n'
        '  (rules:\n'
        '   - "expense": money spent on goods/services (food, transport, bills).\n'
        '   - "income": money earned (salary, side hustle, profit).\n'
        '   - "transfer": moving money between own accounts or investing.\n'
        '   - "liability": taking a loan or borrowing money.\n'
        '   - "asset": selling a physical/financial asset you owned or lending money.\n'
        '   - "intention": savings goal, plan to buy something.\n'
        '   - "swap": exchanging one cryptocurrency for another via DEX. Extract token_in, token_out, amount, and optionally wallet name.\n'
        '   - "send_token": sending a specific token from a wallet to an address. Extract token, amount, to_address, and optionally wallet name.\n'
        '  )\n'
        '- "amount": number or null\n'
        '- "currency": three-letter code (e.g., NGN, USD) or null\n'
        '- "category": one of food, transport, housing, utilities, entertainment, health, clothing, education, income, loan, investment, other\n'
        '- "date": YYYY-MM-DD or null\n'
        '- "description": short summary\n'
        '- "has_amount": true/false\n'
        '- "wallet": optional, name of the wallet (e.g., metamask, trust wallet, bsc wallet)\n'
        '- "token_in": for swaps, the token you are selling\n'
        '- "token_out": for swaps, the token you are buying\n'
        '- "token": for send_token, the token to send\n'
        '- "to_address": for send_token, the destination address\n\n'
        "Pidgin / Slang Examples:\n"
        'User: "i drop 5k for data"\n'
        'Response: type: expense, category: entertainment, amount: 5000, currency: NGN\n\n'
        'User: "i buy rice 2k for market"\n'
        'Response: type: expense, category: food, amount: 2000, currency: NGN\n\n'
        'User: "omo i borrow money 10k from my friend"\n'
        'Response: type: liability, category: loan, amount: 10000, currency: NGN\n\n'
        'User: "i sell my old phone 50k"\n'
        'Response: type: asset, category: other, amount: 50000, currency: NGN\n\n'
        'User: "i wan save 20k for christmas"\n'
        'Response: type: intention, category: savings, amount: 20000, currency: NGN\n\n'
        'User: "i send my guy 2k for transport"\n'
        'Response: type: transfer, category: transport, amount: 2000, currency: NGN\n\n'
        "Rules for 'give/lend a loan':\n"
        '   - "i gave someone a loan of 7000" → type: asset, category: loan, amount: 7000\n'
        '   - "i lent John 5000" → type: asset, category: loan, amount: 5000\n'
        "Rules for 'invested':\n"
        '   - "i invested 50000 in dangote cement" → type: asset, category: other, amount: 50000\n'
        '   - "i invested 20k in mutual funds" → type: asset, category: investment, amount: 20000\n'
        "Crypto swap & send examples:\n"
        'User: "swap 50 USDT for BNB on metamask"\n'
        'Response: type: swap, token_in: USDT, token_out: BNB, amount: 50, wallet: metamask\n'
        'User: "send 100 USDC to 0xABC123 from my bsc wallet"\n'
        'Response: type: send_token, token: USDC, amount: 100, to_address: 0xABC123, wallet: bsc wallet\n'
        'User: "swap 1 ETH for USDC using trust wallet"\n'
        'Response: type: swap, token_in: ETH, token_out: USDC, amount: 1, wallet: trust wallet\n'
        "Important: Do NOT treat questions, greetings, or budget queries as financial transactions.\n"
        "If the user is asking a question, greeting, or saying goodbye, return ONLY:\n"
        "```json\n"
        '{"type": "question"}\n'
        "```\n\n"
        f'User message: "{text}"\n'
        "JSON:"
    )

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "qwen-3.6-27b",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0
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
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0
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