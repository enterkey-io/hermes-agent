#!/usr/bin/env python3
"""
Agent photo generation with proper prompt structure.
Teaches agents how to build detailed, realistic prompts.
"""

import os
import sys
import argparse
import subprocess
import shutil
from contextvars import ContextVar
from pathlib import Path
from datetime import datetime
import base64
from io import BytesIO

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent))

import requests
from identity_parser import load_workspace_agent, resolve_profile_dir
from prompt_profiles import build_prompt

try:
    from PIL import Image
except ImportError:
    Image = None


# API keys come from the Hermes service environment or 1Password CLI. This
# package deliberately does not read NanoClaw or OpenClaw secret files.

_OP_ITEMS = {
    "xai": "op://Assistant/Grok API key for Openclaw/notesPlain",
    "gemini": "op://Assistant/Gemini API Credentials/credential",
    "novita": "op://Assistant/Novita API Credentials/credential",
}

_NO_OP_FALLBACK = ContextVar("agent_photo_no_op_fallback", default=False)
WRAPPER_CONTRACT = "hermes-agent-photo/no-op-fallback/v1"


def _get_api_key(provider: str, *, allow_op_fallback: bool | None = None) -> str:
    """Get API key from env var or 1Password CLI."""
    env_map = {"xai": "XAI_API_KEY", "gemini": "GEMINI_API_KEY", "novita": "NOVITA_API_KEY"}
    env_name = env_map.get(provider, "")
    key = os.environ.get(env_name, "")
    if key:
        return key
    if allow_op_fallback is None:
        allow_op_fallback = not _NO_OP_FALLBACK.get()
    if not allow_op_fallback:
        raise RuntimeError(
            f"authorized credential for {provider} was not injected into the photo child."
        )
    op_ref = _OP_ITEMS.get(provider)
    if op_ref:
        commands = (
            ["op", "read", op_ref],
            ["/bin/bash", "-ic", 'op read "$1"', "agent-photo", op_ref],
        )
        for command in commands:
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
    raise RuntimeError(f"No API key for {provider}. Set {env_name} or check 1Password.")


SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def validate_sources(
    sources: list[str] | None,
    profile_dir: Path | None = None,
) -> list[Path]:
    """Resolve explicit image references and reject missing or non-image files."""
    validated: list[Path] = []
    for source in sources or []:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Source image does not exist or is not a file: {path}")
        if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            raise ValueError(f"Source must be a supported image file: {path}")
        if profile_dir is not None:
            try:
                path.relative_to(profile_dir.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"Source image must stay inside the active Hermes profile: {path}"
                ) from exc
        validated.append(path)
    return validated


def provider_sequence(selected: str, allow_fallback: bool) -> list[str]:
    """Return providers in call order, with paid fallback disabled by default."""
    if not allow_fallback:
        return [selected]
    orders = {
        "gemini": ["gemini", "grok", "seedream"],
        "grok": ["grok", "gemini", "seedream"],
        "seedream": ["seedream", "gemini", "grok"],
    }
    return orders[selected]


def mirror_to_media(paths: list[Path], profile_dir: Path) -> list[Path]:
    """Copy generated files into the channel-visible profile media directory."""
    media_dir = profile_dir.resolve() / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    mirrored: list[Path] = []
    reserved: set[Path] = set()
    for source in paths:
        source = Path(source).resolve()
        target = media_dir / source.name
        suffix_index = 2
        while target in reserved or (target.exists() and source != target):
            target = media_dir / f"{source.stem}-{suffix_index}{source.suffix}"
            suffix_index += 1
        if source != target:
            shutil.copy2(source, target)
        mirrored.append(target)
        reserved.add(target)
    return mirrored


def media_lines(paths: list[Path]) -> list[str]:
    """Build one Hermes-native attachment line per unique output file."""
    seen: set[Path] = set()
    lines: list[str] = []
    for path in paths:
        resolved = Path(path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        lines.append(f"MEDIA: {resolved}")
    return lines


def image_bytes_and_mime(path: Path, max_size_mb: float) -> tuple[bytes, str]:
    """Prepare an image and report the MIME type matching the returned bytes."""
    was_compressed = path.stat().st_size > max_size_mb * 1024 * 1024
    raw = compress_image_if_needed(path, max_size_mb=max_size_mb)
    if was_compressed:
        mime = "image/jpeg"
    else:
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[path.suffix.lower()]
    return raw, mime


def image_data_url(path: Path, max_size_mb: float) -> str:
    """Encode an image as a provider-ready data URL."""
    raw, mime = image_bytes_and_mime(path, max_size_mb)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

def compress_image_if_needed(image_path: Path, max_size_mb: float = 3.0) -> bytes:
    """
    Compress image if it's too large for xAI API.

    xAI /v1/images/edits has a per-request body-size ceiling around ~2.3MB
    post-base64 when routed through the OneCLI MITM proxy. For single-image
    calls the default 3MB cap is fine. For multi-image calls the caller
    should pass a smaller max_size_mb (e.g. 0.35) so the total JSON body
    stays under the proxy ceiling; see generate_photo() for budget logic.

    Args:
        image_path: Path to image file
        max_size_mb: Maximum size in MB before compression

    Returns:
        Image bytes (compressed if necessary)
    """
    if Image is None:
        raise RuntimeError("Pillow is required for photo generation")

    # Read original image
    img = Image.open(image_path)

    # Convert RGBA to RGB if needed
    if img.mode == 'RGBA':
        # Create white background
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])  # 3 is the alpha channel
        img = background

    # Check file size
    original_size = image_path.stat().st_size
    original_size_mb = original_size / (1024 * 1024)

    if original_size_mb <= max_size_mb:
        # Small enough, return as-is
        print(f"Seed image: {original_size_mb:.1f}MB (no compression needed)")
        with open(image_path, 'rb') as f:
            return f.read()

    # Need to compress
    print(f"Image {image_path.name}: {original_size_mb:.2f}MB (compressing to fit {max_size_mb}MB limit)")

    # Scale target dimensions with the size budget. Tiny budgets (<0.5MB)
    # need smaller dimensions because a high-quality 1200x1800 JPEG will not fit under
    # 400KB. Pick dimensions that give the encoder room to hit the target
    # without dropping to unusable quality.
    if max_size_mb < 0.5:
        target_width, target_height = 768, 1152
    elif max_size_mb < 1.0:
        target_width, target_height = 960, 1440
    else:
        target_width, target_height = 1200, 1800

    img.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)

    # Then compress at descending quality until the budget is met.
    for quality in [95, 90, 85, 80, 75, 70, 65, 60]:
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=quality, optimize=True)
        compressed_size = len(buffer.getvalue())
        compressed_size_mb = compressed_size / (1024 * 1024)

        if compressed_size_mb <= max_size_mb:
            print(f"  -> {img.size[0]}x{img.size[1]} @ q{quality} = {compressed_size_mb:.2f}MB")
            return buffer.getvalue()

    # Last resort - smaller resize
    print("  Still too large, reducing dimensions further...")
    img.thumbnail((640, 960), Image.Resampling.LANCZOS)

    for quality in [80, 70, 60, 50]:
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=quality, optimize=True)
        final_size_mb = len(buffer.getvalue()) / (1024 * 1024)
        if final_size_mb <= max_size_mb:
            print(f"  -> {img.size[0]}x{img.size[1]} @ q{quality} = {final_size_mb:.2f}MB")
            return buffer.getvalue()

    # Give up and return the smallest version we have
    print(f"  Warning: could not hit budget, returning {final_size_mb:.2f}MB")
    return buffer.getvalue()

def generate_photo(
    prompt: str,
    seed_image: Path,
    output_path: Path,
    sources: list = None,
    size: str = "2048x3072",
    n: int = 1,
    aspect_ratio: str = "2:3",
) -> list:
    """
    Generate photos via xAI Grok with identity seed image.
    Uses the REST /v1/images/edits endpoint.

    Routing (verified 2026-04-11 via live field-shape probes):
    - 1-2 images use `grok-imagine-image-pro` at 2k. Pro accepts up to 2
      input images via the `image` (singular) field as an array of data-URL
      strings. 3+ elements in that array triggers HTTP 400
      "Failed to parse the request body as JSON: image: trailing characters".
    - 3-5 images use `grok-imagine-image` (base). Base uses a different
      contract: `images` (plural) field as an array of
      `{type: "image_url", url: "data:..."}` objects. No `resolution` param.

    Multi-image prompts must reference seeds as `<IMAGE_0>`, `<IMAGE_1>`, ...
    (NOT "Figure 1/2/3"). The caller is responsible for building the prompt
    with that syntax when multi-image is used.

    `aspect_ratio` must be one of Grok's supported ratios (e.g. "2:3", "3:2",
    "1:1", "16:9", "9:16"). Defaults to "2:3" for portrait agent photos.
    When n > 1, saves each with a numeric suffix.
    Returns a list of saved Paths (empty list on failure).
    """
    n = max(1, min(int(n or 1), 10))

    print(f"Loading seed image: {seed_image}")
    image_paths = [seed_image]
    for sp in validate_sources(sources):
        image_paths.append(sp)
        print(f"Extra source: {sp.name} ({sp.stat().st_size / 1024:.1f} KB)")

    if len(image_paths) > 5:
        print(f"Note: trimming {len(image_paths)} images to first 5 (xAI limit)")
        image_paths = image_paths[:5]

    # Budget the per-image size so the total JSON body stays under ~2MB
    # post-base64. The OneCLI MITM proxy has a body-size ceiling around
    # 2.3MB beyond which xAI rejects with Content-Length mismatch errors.
    total_budget_mb = 1.6
    per_image_mb = max(0.35, total_budget_mb / len(image_paths))

    # Route by image count. Pro is best quality but capped at 2 images.
    if len(image_paths) <= 2:
        grok_model = "grok-imagine-image-pro"
    else:
        grok_model = "grok-imagine-image"
    print(f"Generating with Grok REST API ({grok_model}, n={n}, aspect={aspect_ratio}, images={len(image_paths)}, budget={per_image_mb:.2f}MB/img)...")

    encoded_images = [image_data_url(p, per_image_mb) for p in image_paths]

    print("\n" + "="*60)
    print("PROMPT BEING SENT TO GROK:")
    print("="*60)
    print(prompt)
    print("="*60 + "\n")
    print(f"Total images sent: {len(encoded_images)}")

    if grok_model == "grok-imagine-image-pro":
        # Pro contract: `image` is a tuple struct ImageUrl(String, String).
        # it requires EXACTLY 2 data-URL strings. Empirically verified
        # 2026-04-11 via a live provider probe:
        #   1 element: HTTP 422 "invalid length 1, expected 2 elements"
        #   2 elements: HTTP 200
        # Struct wrappers ({type, url}) return HTTP 422 "expected a string".
        # When we only have one seed image, duplicate it so Pro accepts the
        # payload; the model treats both slots as references to the same
        # identity.
        data_urls = list(encoded_images)
        if len(data_urls) == 1:
            data_urls = [data_urls[0], data_urls[0]]
        payload = {
            "model": grok_model,
            "prompt": prompt,
            "image": data_urls,
            "aspect_ratio": aspect_ratio,
            "response_format": "url",
            "n": n,
            "resolution": "2k",
        }
    else:
        # Base contract: `images` plural, array of {type, url} objects.
        # No `resolution` parameter (base model is 1k-class).
        payload = {
            "model": grok_model,
            "prompt": prompt,
            "images": [
                {"type": "image_url", "url": data_url}
                for data_url in encoded_images
            ],
            "aspect_ratio": aspect_ratio,
            "response_format": "url",
            "n": n,
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    saved_paths: list = []
    variation_idx = 0

    try:
        xai_key = _get_api_key("xai")
        resp = requests.post(
            "https://api.x.ai/v1/images/edits",
            json=payload,
            headers={"Authorization": f"Bearer {xai_key}"},
            timeout=240,
        )
        if resp.status_code >= 400:
            print(f"Grok HTTP {resp.status_code}")
            print(f"Response body: {resp.text[:2000]}")
            return saved_paths
        data = resp.json()
    except Exception as e:
        print(f"Error calling Grok REST API: {e}")
        return saved_paths

    items = data.get("data") or []
    if not items:
        print(f"Error: no images in response: {data}")
        return saved_paths

    for item in items:
        src = item.get("url") or item.get("b64_json")
        if not src:
            print(f"  Skip item with no url/b64_json")
            continue

        if src.startswith("http"):
            try:
                img_resp = requests.get(src, timeout=60)
                img_resp.raise_for_status()
                img_bytes = img_resp.content
            except Exception as e:
                print(f"  Download failed: {e}")
                continue
        else:
            try:
                img_bytes = base64.b64decode(src)
            except Exception as e:
                print(f"  base64 decode failed: {e}")
                continue

        variation_idx += 1
        # When only one image is requested, keep the original filename.
        # With n > 1, suffix each filename: ...-slug-1.png, -2.png, ...
        if n == 1:
            this_path = output_path
        else:
            this_path = output_path.with_stem(f"{output_path.stem}-{variation_idx}")
        this_path.write_bytes(img_bytes)
        print(f"\nPhoto {variation_idx}/{n} saved: {this_path}")
        print(f"  Size: {len(img_bytes) / 1024:.1f} KB")
        saved_paths.append(this_path)

    return saved_paths


def generate_photo_gemini(prompt: str, seed_image: Path, output_path: Path, sources: list = None, size: str = "2048x3072", n: int = 1, aspect_ratio: str = "2:3") -> list:
    """
    Generate photo via Gemini REST API with identity seed image.
    Model: gemini-3-pro-image-preview
    Auth injected automatically by OneCLI proxy.
    Note: sources ignored (Gemini only supports single input image).
    Note: `n` and `aspect_ratio` are accepted for signature compatibility;
          Gemini steers aspect via pixel dimensions in `size` and returns 1 image.
    Returns a list of saved Paths (empty list on failure).
    """
    print("Generating with Gemini REST API (gemini-3-pro-image-preview)...")

    identity_prefix = (
        "Identity lock to the woman in the photo. "
        "IDENTITY REFERENCE: The input image shows the exact person to render. "
        "Preserve their facial features, bone structure, eye shape, nose, lips exactly as shown. "
        "Same person, same face. "
    )
    identity_suffix = " The face must match the reference image exactly. Do not alter facial features."
    # Parse size to add aspect ratio guidance
    w, h = size.split("x")
    if w != h:
        aspect_hint = f" Generate a {w}x{h} portrait-orientation image." if int(h) > int(w) else f" Generate a {w}x{h} landscape-orientation image."
    else:
        aspect_hint = f" Generate a {w}x{h} square image."
    gemini_prompt = identity_prefix + prompt + identity_suffix + aspect_hint

    print("\n" + "="*60)
    print("PROMPT BEING SENT TO GEMINI:")
    print("="*60)
    print(gemini_prompt)
    print("="*60 + "\n")

    print(f"Loading seed image: {seed_image}")
    image_bytes, mime_type = image_bytes_and_mime(seed_image, max_size_mb=3.0)
    image_data = base64.b64encode(image_bytes).decode("utf-8")

    try:
        payload = {
            "contents": [{
                "parts": [
                    {"text": gemini_prompt},
                    {"inline_data": {"mime_type": mime_type, "data": image_data}},
                ]
            }],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        gemini_key = _get_api_key("gemini")
        resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3-pro-image-preview:generateContent",
            headers={"x-goog-api-key": gemini_key},
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        img_bytes = None
        for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
            if "inlineData" in part:
                img_bytes = base64.b64decode(part["inlineData"]["data"])
                break

        if not img_bytes:
            print(f"Error: no image in Gemini response: {str(data)[:300]}")
            return []

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(img_bytes)
        print(f"\nPhoto saved: {output_path}")
        print(f"  Size: {len(img_bytes) / 1024:.1f} KB")
        return [output_path]

    except Exception as e:
        print(f"Error calling Gemini REST API: {e}")
        return []


def generate_photo_seedream(
    prompt: str,
    seed_image: Path,
    output_path: Path,
    sources: list | None = None,
    size: str = "2048x3072",
    n: int = 1,
    aspect_ratio: str = "2:3",
) -> list[Path]:
    """Generate one identity-locked image with Novita Seedream 4.5."""
    del n, aspect_ratio
    image_paths = [seed_image, *validate_sources(sources)]
    image_paths = image_paths[:5]
    per_image_mb = max(0.5, 3.0 / len(image_paths))
    encoded_sources = []
    for path in image_paths:
        encoded_sources.append(image_data_url(path, per_image_mb))

    payload = {
        "prompt": prompt,
        "image": encoded_sources,
        "size": size,
        "watermark": False,
    }
    print(f"Generating with Novita/Seedream 4.5 (images={len(encoded_sources)})...")
    try:
        response = requests.post(
            "https://api.novita.ai/v3/seedream-4.5",
            headers={
                "Authorization": f"Bearer {_get_api_key('novita')}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=180,
        )
        response.raise_for_status()
        images = response.json().get("images") or []
        if not images:
            print("Seedream returned no images")
            return []
        first = images[0]
        image_url = first if isinstance(first, str) else first.get("image_url") or first.get("url")
        if not image_url:
            print("Seedream response did not include an image URL")
            return []
        image_response = requests.get(image_url, timeout=60)
        image_response.raise_for_status()
    except Exception as exc:
        print(f"Error calling Seedream: {exc}")
        return []

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_response.content)
    print(f"Photo saved: {output_path}")
    return [output_path]


DEFAULT_PROVIDERS = {
    "gemini": generate_photo_gemini,
    "grok": generate_photo,
    "seedream": generate_photo_seedream,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Generate agent photos with proper prompt structure',
        epilog='''
Examples:
  %(prog)s --preview-prompt "at a desk, natural window light"
  %(prog)s --approved --model gemini "at a desk, natural window light"

The script builds a complete 15-part prompt including:
  - Your physical description from IDENTITY.md
  - Skin texture requirements
  - Technical camera specs
  - Identity preservation

You just provide the scene (pose, outfit, location, lighting, expression).
        '''
    )
    parser.add_argument('scene', help='Scene description: pose, outfit, location, lighting, expression')
    parser.add_argument('--output', help='Output filename (default: auto-generated)')
    parser.add_argument('--model', choices=['gemini', 'grok', 'seedream'], default='gemini',
                        help='Paid image provider (default: gemini)')
    parser.add_argument('--source', action='append', dest='sources', help='Source image for reference (repeat for multiple: --source a.jpg --source b.jpg)')
    parser.add_argument('--size', default='2731x4096',
                        help='Output size WxH (default: 2731x4096, 2:3 portrait at 4K). Examples: 4096x4096 (square 4K), 4096x2731 (landscape 4K).')
    parser.add_argument('-n', '--num-images', type=int, default=1,
                        help='Number of images to generate (Grok only, default: 1, max: 10). Grok batches all variations in a single API call. Gemini and Seedream always return 1.')
    parser.add_argument('--aspect-ratio', default='2:3',
                        choices=['1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9'],
                        help='Aspect ratio for Grok (default: 2:3 portrait). Grok ignores `--size` and uses this instead. Gemini and Seedream ignore this and use pixel dimensions from --size.')
    parser.add_argument(
        '--prompt-profile',
        choices=['baseline', 'reality-first'],
        default='baseline',
        help='Prompt profile to use (default: baseline)',
    )
    parser.add_argument(
        '--preview-prompt',
        action='store_true',
        help='Build and print the prompt, then exit without calling a provider',
    )
    parser.add_argument(
        '--save-prompt',
        help='Optional path to write the built prompt for side-by-side evaluation',
    )
    parser.add_argument(
        '--approved',
        action='store_true',
        help='Confirm the current user explicitly requested this paid generation',
    )
    parser.add_argument(
        '--allow-fallback',
        action='store_true',
        help='Allow paid calls to alternate providers after a failure; requires separate user authorization',
    )
    parser.add_argument(
        '--no-op-fallback',
        action='store_true',
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--wrapper-contract',
        action='store_true',
        help=argparse.SUPPRESS,
    )
    return parser


def _profile_local_path(profile_dir: Path, requested: str, purpose: str) -> Path:
    path = Path(requested).expanduser().resolve()
    try:
        path.relative_to(profile_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"{purpose} path must stay inside the active Hermes profile") from exc
    return path


def _output_path(profile_dir: Path, agent_name: str, scene: str, requested: str | None) -> Path:
    if requested:
        return _profile_local_path(profile_dir, requested, "Output")
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    slug = "-".join(scene.lower().split())[:30].strip("-") or "photo"
    photos_dir = profile_dir / "media" / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    return photos_dir / f"{agent_name}-{timestamp}-{slug}.png"


def main(argv: list[str] | None = None, providers: dict | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.wrapper_contract:
        print(WRAPPER_CONTRACT)
        return 0

    if not args.preview_prompt and not args.approved:
        print(
            "Paid generation refused: use --approved only for an explicit current user photo request.",
            file=sys.stderr,
        )
        return 2

    try:
        profile_dir = resolve_profile_dir()
        sources = validate_sources(args.sources, profile_dir=profile_dir)
        print("Loading agent identity from identity.md...")
        identity, seed, agent_name = load_workspace_agent()
        print(f"Agent: {identity['name']}")
        print(f"Seed: {seed}")
        print()

        prompt = build_prompt(
            profile=args.prompt_profile,
            identity=identity,
            scene_description=args.scene,
            model=args.model,
            sources=sources,
        )

        if args.save_prompt:
            prompt_path = _profile_local_path(profile_dir, args.save_prompt, "Saved prompt")
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt + "\n", encoding='utf-8')
            print(f"Prompt saved to: {prompt_path}")

        if args.preview_prompt:
            print("\n" + "="*60)
            print(f"PROMPT PREVIEW ({args.prompt_profile}):")
            print("="*60)
            print(prompt)
            print("="*60)
            return 0

        output_path = _output_path(profile_dir, agent_name, args.scene, args.output)
        provider_registry = providers if providers is not None else DEFAULT_PROVIDERS
        sequence = provider_sequence(args.model, args.allow_fallback)

        print(f"Model: {args.model}")
        print(f"Prompt profile: {args.prompt_profile}")
        saved_paths: list = []
        no_op_fallback_token = _NO_OP_FALLBACK.set(args.no_op_fallback)
        try:
            for index, provider_name in enumerate(sequence):
                provider = provider_registry.get(provider_name)
                if provider is None:
                    print(f"Provider is unavailable: {provider_name}")
                    continue
                print(f"Trying {provider_name}...")
                try:
                    saved_paths = provider(
                        prompt=prompt,
                        seed_image=seed,
                        output_path=output_path,
                        sources=sources,
                        size=args.size,
                        n=args.num_images,
                        aspect_ratio=args.aspect_ratio,
                    ) or []
                except Exception as e:
                    print(f"{provider_name} failed with exception: {e}")
                    saved_paths = []
                if saved_paths:
                    break
                if index < len(sequence) - 1:
                    print("Authorized fallback to the next provider...")
        finally:
            _NO_OP_FALLBACK.reset(no_op_fallback_token)

        if saved_paths:
            media_paths = mirror_to_media([Path(path) for path in saved_paths], profile_dir)
            print(f"\nGenerated {len(media_paths)} image(s). Include each line once in the reply:")
            for line in media_lines(media_paths):
                print(line)

        return 0 if saved_paths else 1

    except Exception as e:
        print(f"Error: {e}")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
