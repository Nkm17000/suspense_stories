def build_prompt(topic: str) -> str:
    return f"""
You are an expert Hindi suspense storyteller creating short cinematic social-media stories.

Generate ONE complete suspense story for this topic: "{topic}"

OUTPUT ONLY VALID JSON. No markdown and no explanation.

EXACT JSON STRUCTURE:
{{
  "story_id": "कहानी_001",
  "title": "हिंदी शीर्षक",
  "status": "PENDING",
  "category": "रहस्य और सस्पेंस",
  "language": "hi",
  "style": "cinematic dark suspense realistic Indian atmosphere",
  "characters": {{
    "MAIN": {{
      "id": "arjun_32",
      "name": "अर्जुन",
      "description": "32 वर्षीय भारतीय पुरुष, सांवला चेहरा, छोटे काले बाल, नीली शर्ट"
    }},
    "SUPPORT": {{
      "id": "rohan_35",
      "name": "रोहन",
      "description": "35 वर्षीय भारतीय पुरुष, गेहुँआ चेहरा, छोटे काले बाल, भूरे रंग की जैकेट"
    }}
  }},
  "parts": [
    {{
      "part_no": 1,
      "part_title": "हिंदी भाग शीर्षक",
      "scenes": [
        {{
          "scene_number": 1,
          "text": "पूरा हिंदी संवाद और वर्णन...",
          "sub_image_prompts": [
            {{
              "text": "हिंदी में इस दृश्य का छोटा वर्णन",
              "scene_prompt": "English only cinematic image prompt..."
            }},
            {{
              "text": "हिंदी में दूसरा दृश्य वर्णन",
              "scene_prompt": "English only cinematic image prompt..."
            }}
          ]
        }}
      ]
    }}
  ]
}}

STRICT STORY RULES:
1. `text`, `title`, `category`, `part_title`, and sub-image `text` must be entirely in Hindi.
2. The story must have exactly TWO recurring characters: MAIN and SUPPORT. Both must be boys/men.
3. Both characters must communicate with each other through natural Hindi dialogue in the story.
4. Keep the same character names, ages, clothing, facial features, and roles in every scene.
5. Create a strong hook in the first scene, escalating mystery, clues, a believable twist, and a satisfying ending.
6. Avoid supernatural gore. Keep suspense suitable for a broad Indian social-media audience.
7. Each part should contain exactly 10 scenes.
8. Each scene should have 1–2 `sub_image_prompts`. If the scene text is visually dense, use 2.
9. Each `scene_prompt` MUST be English only. Do not use any Hindi, Devanagari, Hindi names, or Hindi characters in `scene_prompt`.
10. Because image requests are sent one at a time, EVERY `scene_prompt` must repeat the complete MAIN and SUPPORT visual descriptions needed for that image. Never rely on a previous prompt.
11. Every `scene_prompt` must describe the actual moment from the matching `text`, the location, action, emotion, lighting, camera framing, and cinematic suspense mood.
12. Do not put dialogue text, captions, logos, watermarks, or written words inside the generated image.
13. Keep `scene_prompt` concise enough for an image model but detailed enough to preserve character consistency.
14. Do not create new characters unless absolutely necessary; if a background person is unavoidable, keep them visually indistinct.
15. Use natural Indian environments: homes, streets, shops, railway areas, offices, villages, or city neighborhoods as appropriate.
16. Output valid JSON only.

Create the story now.
"""
