"""
Image analyzer for the collectkit worker.

Analyzes images using vision LLM (qwen/qwen3-vl-235b-a22b-instruct via OpenRouter)
to extract summary and OCR text for the chatbot to understand.
"""

import os
import requests

from .logging_config import get_logger

logger = get_logger(__name__)

VISION_MODEL = "qwen/qwen3-vl-235b-a22b-instruct"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_SECONDS = 45


def analyze_image(base64_image: str, caption: str = "") -> dict:
    """
    Analyze image using qwen/qwen3-vl-235b-a22b-instruct via OpenRouter.

    Args:
        base64_image: Base64-encoded image data
        caption: Optional caption from the user

    Returns:
        {
            "summary": "Description of what the image shows",
            "ocr_text": "Extracted text from the image"
        }
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.error("OpenRouter API key not set for image analysis", extra={
            "event": "image_analysis_config_error",
        })
        return {"summary": "[Image analysis failed: API key not configured]", "ocr_text": ""}

    # Build prompt for image analysis
    prompt = """Analyze this image and respond in English:
1. SUMMARY: Describe what this image shows in 1-2 sentences. Focus on whether it's a payment proof, transfer receipt, or screenshot.
2. OCR: Extract ALL visible text from the image (amounts, dates, names, transaction IDs, etc.)

Format response exactly as:
SUMMARY: [description]
OCR: [extracted text]"""

    # Add caption context if provided
    if caption:
        prompt = f"User caption: \"{caption}\"\n\n{prompt}"

    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        }],
        "max_tokens": 500,
    }

    try:
        response = requests.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=TIMEOUT_SECONDS
        )
        response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]

        # Clean up special tokens from some models
        content = content.replace("<|begin_of_box|>", "").replace("<|end_of_box|>", "")
        content = content.strip()

        logger.info("Image analysis completed", extra={
            "event": "image_analysis_completed",
        })

        # Parse response
        summary = ""
        ocr = ""
        lines = content.split("\n")
        for line in lines:
            line = line.strip()
            if line.upper().startswith("SUMMARY:"):
                summary = line[len("SUMMARY:"):].strip()
            elif line.upper().startswith("OCR:"):
                ocr = line[len("OCR:"):].strip()

        # If parsing failed, use full content as summary
        if not summary:
            summary = content.strip()

        return {"summary": summary, "ocr_text": ocr}

    except requests.Timeout:
        logger.error("Image analysis timed out", extra={
            "event": "image_analysis_timeout",
            "timeout_seconds": TIMEOUT_SECONDS,
        })
        return {"summary": "[Image analysis timed out]", "ocr_text": ""}

    except requests.RequestException as e:
        logger.error("Image analysis request failed", extra={
            "event": "image_analysis_request_error",
            "error": str(e),
        })
        return {"summary": "[Image analysis failed]", "ocr_text": ""}

    except Exception as e:
        logger.error("Image analysis error", extra={
            "event": "image_analysis_error",
            "error": str(e),
        })
        return {"summary": "[Image analysis failed]", "ocr_text": ""}
