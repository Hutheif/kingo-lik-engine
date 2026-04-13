# check_models.py
from dotenv import load_dotenv
import os
load_dotenv()

from google import genai
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

print("Available models that support generateContent:\n")
for model in client.models.list():
    if "generateContent" in (model.supported_actions or []):
        print(f"  {model.name}")