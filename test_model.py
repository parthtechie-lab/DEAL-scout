import asyncio, os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

for m_name in ["gemini-flash-latest", "gemini-3.1-flash-lite", "gemini-2.5-flash"]:
    print(f"\n--- Testing {m_name} ---")
    model = genai.GenerativeModel(m_name)
    try:
        resp = model.generate_content("Is this a deal? 'Free phone'")
        print(f"✅ Success! Response: {resp.text.strip()}")
    except Exception as e:
        print(f"❌ Error: {e}")
