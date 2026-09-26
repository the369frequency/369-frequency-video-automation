"""
video_voiceover.py

Step 1 of The 369 Frequency's video automation pipeline.

What it does:
  1. Connects to Supabase (same project used by the rest of the platform).
  2. Finds facts in `truth_vault` that have an approved short_script but no
     voiceover yet (audio_url is empty).
  3. Sends each script's text to ElevenLabs to generate a narrated voiceover.
  4. Uploads the resulting MP3 to Supabase Storage (bucket: "audio").
  5. Saves the public audio URL back onto the fact's row in `truth_vault`.

Runs once and exits — deployed on Railway the same way as rss_parser.py,
with a Cron Schedule to run periodically (e.g. every few hours) so newly
approved scripts get turned into voiceovers automatically.

REQUIRED SETUP BEFORE THIS WILL WORK (see setup notes at the bottom):
  1. Add two columns to truth_vault: audio_url (text), audio_generated_at (timestamptz)
  2. Create a public Storage bucket in Supabase called "audio"
  3. Set these environment variables in Railway:
     - SUPABASE_URL
     - SUPABASE_KEY   (the legacy anon JWT key — same one rss_parser.py uses)
     - ELEVENLABS_API_KEY
     - ELEVENLABS_VOICE_ID  (optional — defaults to a standard ElevenLabs voice)
"""

import os
import sys
import requests
from datetime import datetime, timezone
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
# Default voice: "Rachel", one of ElevenLabs' standard premade voices.
# Override with your own ELEVENLABS_VOICE_ID env var to use a different voice.
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

ELEVENLABS_TTS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
AUDIO_BUCKET = "audio"


def require_env():
    missing = [
        name
        for name, value in [
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_KEY", SUPABASE_KEY),
            ("ELEVENLABS_API_KEY", ELEVENLABS_API_KEY),
        ]
        if not value
    ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def clean_script_for_narration(raw_script: str) -> str:
    """
    short_script is stored with section labels like **[HOOK]**, **[BODY]**,
    **[STAKES]**, **[CTA]**, and a trailing "Sources:" block with URLs.
    Strip all of that so ElevenLabs only narrates the actual spoken words.
    """
    lines = raw_script.split("\n")
    spoken_lines = []
    in_sources = False

    for line in lines:
        stripped = line.strip()

        if stripped.lower().startswith("**sources") or stripped.lower().startswith("sources:"):
            in_sources = True
            continue
        if in_sources:
            continue

        if stripped.startswith("# "):
            continue

        # Remove section labels like **[HOOK]** but keep any text after them
        cleaned = stripped
        if cleaned.startswith("**[") and "]**" in cleaned:
            cleaned = cleaned.split("]**", 1)[1].strip()
        elif cleaned.startswith("[") and cleaned.endswith("]") and len(cleaned) < 20:
            # A lone section label on its own line, e.g. "[HOOK]"
            cleaned = ""

        if cleaned:
            spoken_lines.append(cleaned)

    return " ".join(spoken_lines).strip()


def generate_voiceover(text: str) -> bytes:
    response = requests.post(
        ELEVENLABS_TTS_URL,
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def main():
    require_env()
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("Checking for approved scripts without a voiceover yet...")

    result = (
        supabase.table("truth_vault")
        .select("id, fact_title, short_script")
        .not_.is_("short_script", "null")
        .is_("audio_url", "null")
        .eq("script_approved", True)
        .execute()
    )

    facts = result.data or []

    if not facts:
        print("Nothing to do — no approved scripts are waiting for a voiceover.")
        return

    print(f"Found {len(facts)} fact(s) needing a voiceover.")

    for fact in facts:
        fact_id = fact["id"]
        title = fact.get("fact_title", f"fact {fact_id}")
        raw_script = fact.get("short_script") or ""

        narration_text = clean_script_for_narration(raw_script)

        if not narration_text:
            print(f"  Skipping '{title}' (id={fact_id}) — no narratable text found.")
            continue

        print(f"  Generating voiceover for '{title}' (id={fact_id})...")

        try:
            audio_bytes = generate_voiceover(narration_text)
        except requests.exceptions.RequestException as e:
            print(f"    ElevenLabs request failed for id={fact_id}: {e}")
            continue

        file_path = f"{fact_id}.mp3"

        try:
            supabase.storage.from_(AUDIO_BUCKET).upload(
                file_path,
                audio_bytes,
                file_options={"content-type": "audio/mpeg", "upsert": "true"},
            )
        except Exception as e:
            print(f"    Failed to upload audio for id={fact_id}: {e}")
            continue

        public_url = supabase.storage.from_(AUDIO_BUCKET).get_public_url(file_path)

        try:
            supabase.table("truth_vault").update(
                {
                    "audio_url": public_url,
                    "audio_generated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", fact_id).execute()
        except Exception as e:
            print(f"    Failed to save audio_url for id={fact_id}: {e}")
            continue

        print(f"    Done — voiceover saved: {public_url}")

    print("Voiceover run complete.")


if __name__ == "__main__":
    main()
