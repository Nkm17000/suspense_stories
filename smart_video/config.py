"""Configuration for the Suspense Stories video pipeline."""
import os

VIDEO_SIZE = (720, 1280)
FPS = int(os.getenv("VIDEO_FPS", "24"))
MIN_DURATION = float(os.getenv("MIN_SCENE_DURATION", "5"))

LOGO_PATH = os.getenv("LOGO_PATH", "logo.png")
LOGO_SIZE = int(os.getenv("LOGO_SIZE", "125"))
LOGO_MARGIN = int(os.getenv("LOGO_MARGIN", "18"))

CTA_URL = os.getenv("CTA_URL", "https://www.facebook.com/thesmartlearninglab")
END_CARD_DURATION = float(os.getenv("END_CARD_DURATION", "5"))
TITLE_CARD_DURATION = float(os.getenv("TITLE_CARD_DURATION", "3"))
TITLE_TEMPLATE_PATH = os.getenv("TITLE_TEMPLATE_PATH", "assets/title_page_template.png")

TITLE_TEMPLATE_CROP = (0.0432, 0.0262, 0.8811, 0.9692)

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
DATABASE_NAME = os.getenv("MONGODB_DATABASE", "storydb").strip()
COLLECTION_NAME = os.getenv("MONGODB_COLLECTION", "longstory").strip()
STORY_ID = os.getenv("STORY_ID", "").strip() or None
MONGODB_SERVER_TIMEOUT_MS = int(os.getenv("MONGODB_SERVER_TIMEOUT_MS", "10000"))

os.makedirs("images", exist_ok=True)
os.makedirs("audio", exist_ok=True)
os.makedirs("logs", exist_ok=True)

# Pause after each completed image generation.
IMAGE_GENERATION_SLEEP_SECONDS = max(0.0, float(os.getenv("IMAGE_GENERATION_SLEEP_SECONDS", "5")))

# Optional fixed prefix added to every image-generation prompt.
IMAGE_PROMPT_PREFIX = os.getenv(
    "IMAGE_PROMPT_PREFIX",
    "Do not depict real people or photorealistic humans."
).strip()
