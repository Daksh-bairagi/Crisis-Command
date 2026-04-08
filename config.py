from dotenv import load_dotenv
import os

load_dotenv()


GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_GENAI_USE_VERTEXAI= False
# Fail loudly at startup if critical config is missing
# This is called "fail-fast" — better to crash on startup than fail mysteriously later
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY is not set in .env")

