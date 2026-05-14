"""Content bridge — fetches content from SEO AI Agent for SAU uploads.

Standalone module with zero dependencies beyond stdlib.
Importable without triggering the heavy patchright/playwright dependency chain.
"""

import json
import urllib.request
import urllib.error


def fetch_content_from_url(url: str) -> dict:
    """Fetch content from seo-ai-agent export endpoint.

    Returns dict with: title, description, tags, platform_hint.
    Raises RuntimeError on failure.
    """
    try:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if "title" not in data:
                raise RuntimeError(
                    f"Unexpected response from {url}: {json.dumps(data)[:200]}"
                )
            return data
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Cannot reach content URL '{url}': {e.reason}"
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}") from e


# Quick self-test
if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/content/export/latest?format=sau"
    try:
        data = fetch_content_from_url(url)
        print(f"Title: {data.get('title', 'N/A')[:80]}")
        print(f"Tags: {data.get('tags', [])[:5]}")
        print(f"Platform hint: {data.get('platform_hint', 'unknown')}")
        print(f"Description length: {len(data.get('description', ''))} chars")
        print("✓ Bridge working")
    except RuntimeError as e:
        print(f"✗ {e}")
        sys.exit(1)
