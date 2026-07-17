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
        "You are a compassionate, emotionally intelligent financial interpreter for an African user. "
        "The user may speak in English, Nigerian Pidgin, Yoruba, Igbo, Hausa, or a mix. "
        "They could be sharing a simple expense, expressing worry about money, or just making small talk.\n\n"
        "Your task is to extract structured financial information ONLY if the user is clearly recording a money event. "
        "If the message is a greeting, a question, an emotion, or just chat, return a simple question marker. "
        "Do NOT force a transaction where there is none.\n\n"
        "Return ONLY a valid JSON object wrapped in a markdown code block:\n"
        "```json\n"
        "{...}\n"
        "```\n"
        "Do not include any other text.\n\n"
        "FIELDS:\n"
        '- "type": one of "expense", "income", "transfer", "liability", "asset", "intention", "swap", "send_token", "question"\n'
        '  - "question" is for any non‑financial chat: greetings, "how are you", "I\'m tired", "thank you", etc.\n'
        '  - Use "expense" if they spent money on goods or services.\n'
        '  - Use "income" if they received money.\n'
        '  - Use "liability" if they borrowed money.\n'
        '  - Use "asset" if they lent money or sold something they owned.\n'
        '  - "intention" if they plan to save or buy something.\n'
        '  - "swap" / "send_token" only for clear crypto commands.\n'
        '- "amount": number or null\n'
        '- "currency": three‑letter code (e.g., NGN, USD) or null\n'
        '- "category": one of food, transport, housing, utilities, entertainment, health, clothing, education, income, loan, investment, other\n'
        '- "description": a short, caring summary (e.g., "bought rice for the family")\n'
        '- "has_amount": true/false\n\n'
        "EMOTIONAL CUES:\n"
        "If the user sounds stressed (e.g., 'I don\'t know how I will pay school fees'), still extract the type as 'intention' or 'question', but include a note in description about their emotion.\n"
        "If they are just sharing a feeling without a specific money event, use type: 'question'.\n\n"
        "EXAMPLES:\n"
        'User: "I dey happy today, I sell all my goods"\n'
        '→ type: income, amount: null, description: "sold all goods, feeling happy"\n'
        'User: "omo, I tire for this country"\n'
        '→ type: question, description: "expressing frustration"\n'
        'User: "I buy rice 2k for market"\n'
        '→ type: expense, amount: 2000, category: food\n'
        'User: "hello"\n'
        '→ type: question\n\n'
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