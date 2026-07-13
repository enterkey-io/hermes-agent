#!/usr/bin/env python3
"""
Parse workspace agent IDENTITY.md files for photo generation.
Extracts physical details for building realistic prompts.
"""

import re
import os
from pathlib import Path

def parse_hair(hair_text: str) -> dict:
    """Parse hair description into components.

    Returns empty strings for any component not explicitly present in the
    identity file. Previously defaulted to brown/medium when the file was
    silent. That fallback caused images to drift away from the seed when
    the identity file deliberately omitted hair info. NEVER guess hair
    fields; the seed image is the source of truth for hair appearance.
    """
    parts = [p.strip() for p in hair_text.split(',')]

    # All fields empty by default. Only filled in from what's actually in
    # the file, never inferred or padded.
    result = {"color": "", "length": "", "style": ""}

    length_patterns = ['shoulder-length', 'chin-length', 'waist-length', 'short', 'long']

    if len(parts) >= 1:
        first_part = parts[0]

        found_length = None
        for pattern in length_patterns:
            if pattern in first_part.lower():
                found_length = pattern
                words = first_part.split()
                for word in words:
                    if pattern in word.lower():
                        found_length = word
                        break
                break

        if found_length:
            # Color is whatever remains after stripping the length word,
            # empty if the file had only a length descriptor.
            color = first_part.replace(found_length, "").strip()
            result["color"] = color
            result["length"] = found_length
        else:
            result["color"] = first_part

            if len(parts) >= 2:
                second_part = parts[1]
                for pattern in length_patterns:
                    if pattern in second_part.lower():
                        result["length"] = second_part
                        if len(parts) > 2:
                            result["style"] = ", ".join(parts[2:])
                        return result

    if len(parts) > 1:
        start_idx = 1
        if result["length"]:
            for i, part in enumerate(parts[1:], 1):
                if result["length"] in part:
                    start_idx = i + 1
                    break

        if start_idx < len(parts):
            result["style"] = ", ".join(parts[start_idx:])

    return result

def parse_identity_md(identity_path: Path) -> dict:
    """
    Parse an agent's IDENTITY.md file and extract physical details.

    Args:
        identity_path: Path to IDENTITY.md file

    Returns:
        dict with physical characteristics
    """
    with open(identity_path) as f:
        content = f.read()

    # Extract key fields using regex patterns
    def extract(pattern, default=""):
        match = re.search(pattern, content, re.IGNORECASE)
        return match.group(1).strip() if match else default

    # Extract basic info
    name = extract(r'\*\*Name:\*\*\s*(.+)')
    age_str = extract(r'\*\*Age:\*\*\s*(\d+)')
    age = int(age_str) if age_str else 30

    # Extract physical details
    height = extract(r'\*\*Height:\*\*\s*(.+)')
    build = extract(r'\*\*Build:\*\*\s*(.+)')
    measurements = extract(r'\*\*Measurements:\*\*\s*(.+)')
    hair_text = extract(r'\*\*Hair:\*\*\s*(.+)')
    eyes = extract(r'\*\*Eyes:\*\*\s*(.+)')
    skin = extract(r'\*\*Skin:\*\*\s*(.+)')
    style = extract(r'\*\*Style:\*\*\s*(.+)')
    distinguishing = extract(r'\*\*Distinguishing features:\*\*\s*(.+)')
    photo_prompt_note = extract(r'\*\*Photo Note:\*\*\s*(.+)')

    # Parse hair into components. When the identity file has no Hair
    # line, return empty fields rather than guessing. The prompt builder
    # is required to skip empty appearance fields so the seed image
    # remains the sole source of truth for hair / eyes / skin.
    hair = parse_hair(hair_text) if hair_text else {"color": "", "length": "", "style": ""}

    # Check for dedicated glasses field first
    glasses = extract(r'\*\*Glasses:\*\*\s*(.+)')

    # If no dedicated field, check in eyes or content
    if not glasses and re.search(r'glasses|eyewear|spectacles', content, re.IGNORECASE):
        # Try to extract glasses description from eyes or distinguishing features
        eyes_lower = eyes.lower()
        if 'glasses' in eyes_lower:
            # Extract glasses description from eyes field
            glasses_match = re.search(r'(black|silver|gold|metal|plastic|rimmed|rectangular|round|oval).*?(glasses|frames)', eyes_lower)
            if glasses_match:
                glasses = glasses_match.group(0)
            else:
                glasses = "glasses"

    # Check for makeup mention
    makeup = ""
    if re.search(r'makeup|mascara|eyeliner|lipstick', content, re.IGNORECASE):
        makeup_match = re.search(r'\*\*Makeup:\*\*\s*(.+)', content, re.IGNORECASE)
        if makeup_match:
            makeup = makeup_match.group(1).strip()
        else:
            # Default minimal if mentioned but not detailed
            makeup = "Minimal natural makeup"

    return {
        "name": name,
        "age": age,
        "height": height,
        "build": build,
        "measurements": measurements,
        "hair": hair,  # Now a dict with color, length, style
        "eyes": eyes,
        "skin": skin,
        "style": style,
        "distinguishing": distinguishing,
        "glasses": glasses,
        "makeup": makeup,
        "photo_prompt_note": photo_prompt_note,
    }

def parse_private_identity_md(identity_path: Path) -> dict:
    """
    Parse an IDENTITY.md file for the ## NSFW Physical Details section.

    Previously read from .private/IDENTITY.md; as of 2026-03-01 intimate
    details are merged into the main IDENTITY.md, so callers now pass the
    main identity file.

    Args:
        identity_path: Path to an IDENTITY.md file containing an optional
                       "## NSFW Physical Details" section.

    Returns:
        dict with NSFW details (empty dict if section missing)
    """
    with open(identity_path) as f:
        content = f.read()

    # Find NSFW Physical Details section
    nsfw_match = re.search(
        r'## NSFW Physical Details\s+(.*?)(?=\n##|\Z)',
        content,
        re.DOTALL
    )

    if not nsfw_match:
        return {}

    nsfw_section = nsfw_match.group(1).strip()

    # Parse **Label:** description patterns
    details = {}
    pattern = r'\*\*([^*]+):\*\*\s*([^\n]+(?:\n(?!\*\*)[^\n]+)*)'

    for match in re.finditer(pattern, nsfw_section):
        label = match.group(1).strip().lower()  # Normalize: "Nipples" -> "nipples"
        description = match.group(2).strip()
        details[label] = description

    return details


def load_private_identity_if_available(workspace_dir: Path) -> dict | None:
    """
    Load private identity if available and mode permits.

    Args:
        workspace_dir: Path to workspace directory

    Returns:
        dict with private details, or None if unavailable/not permitted
    """
    # Private details are now merged into main IDENTITY.md (as of 2026-03-01)
    # Read from main IDENTITY.md instead of the deleted .private/IDENTITY.md
    identity_path = workspace_dir / 'identity.md'
    if not identity_path.exists():
        identity_path = workspace_dir / 'IDENTITY.md'
    if not identity_path.exists():
        return None  # No identity file - not an error

    # Parse the main identity file for NSFW section
    try:
        private_details = parse_private_identity_md(identity_path)
        return private_details if private_details else None
    except Exception as e:
        print(f"Error parsing {identity_path} for private details: {e}")
        return None  # Parse error - return None to continue with public identity


def find_seed_image(workspace_dir: Path) -> Path:
    """
    Find the lifelike-seed image in workspace directory.

    Args:
        workspace_dir: Path to workspace directory

    Returns:
        Path to seed image
    """
    # Prefer the curated assets seed over any root-level compatibility copy.
    # Elliott expects the lifelike seed to live in assets/; root copies can lag
    # or be accidental, so assets wins whenever present.
    for search_dir in [workspace_dir / "assets", workspace_dir]:
        for ext in ['.png', '.jpg', '.jpeg']:
            seed_path = search_dir / f"lifelike-seed{ext}"
            if seed_path.exists():
                return seed_path

    raise FileNotFoundError(f"No lifelike-seed image found in {workspace_dir / 'assets'} or {workspace_dir}")


def resolve_profile_dir() -> Path:
    """Resolve the active Hermes profile without guessing another agent's home."""
    configured = os.environ.get("HERMES_HOME") or os.environ.get("HERMES_PROFILE_DIR")
    if configured:
        profile_dir = Path(configured).expanduser().resolve()
        if not profile_dir.is_dir():
            raise FileNotFoundError(f"Hermes profile directory does not exist: {profile_dir}")
        return profile_dir

    cwd = Path.cwd().resolve()
    for candidate in (cwd, cwd.parent):
        if (candidate / "identity.md").is_file() or (candidate / "IDENTITY.md").is_file():
            return candidate

    raise FileNotFoundError(
        "Cannot resolve the active Hermes profile. Set HERMES_HOME or run from a profile directory."
    )

def load_workspace_agent() -> tuple[dict, Path, str]:
    """
    Load identity and seed for the agent in current workspace.

    Returns:
        Tuple of (identity dict, seed image path, agent name)
    """
    workspace_dir = resolve_profile_dir()

    # Support both lowercase and uppercase filename
    identity_file = workspace_dir / "identity.md"
    if not identity_file.exists():
        identity_file = workspace_dir / "IDENTITY.md"
    identity = parse_identity_md(identity_file)
    seed = find_seed_image(workspace_dir)
    identity_name = str(identity.get("name") or workspace_dir.name)
    agent_name = identity_name.replace("\u2014", "-").split("-")[0].strip().lower().replace(" ", "-")

    return identity, seed, agent_name

# Test function
if __name__ == "__main__":
    import json
    import sys

    try:
        identity, seed, agent_name = load_workspace_agent()

        print(f"\n{'='*60}")
        print(f"AGENT: {identity['name']}")
        print(f"{'='*60}\n")
        print(f"Workspace: {agent_name}")
        print(f"Age: {identity['age']}")
        print(f"Height: {identity['height']}")
        print(f"Build: {identity['build']}")
        print(f"Measurements: {identity['measurements']}")
        print(f"Hair: {identity['hair']['color']} {identity['hair']['length']} ({identity['hair']['style']})")
        print(f"Eyes: {identity['eyes']}")
        print(f"Skin: {identity['skin']}")
        print(f"Style: {identity['style']}")
        if identity['glasses']:
            print(f"Glasses: {identity['glasses']}")
        if identity['makeup']:
            print(f"Makeup: {identity['makeup']}")
        if identity['distinguishing']:
            print(f"Features: {identity['distinguishing']}")
        print(f"\nSeed image: {seed}")
        print(f"Exists: {seed.exists()}")

        # Show private details if available (agents can load them separately if needed)
        workspace_dir = Path.cwd()
        if not (workspace_dir / "IDENTITY.md").exists():
            workspace_dir = workspace_dir.parent
        private_details = load_private_identity_if_available(workspace_dir)

        if private_details:
            print(f"\n{'='*60}")
            print("PRIVATE DETAILS AVAILABLE:")
            print(f"{'='*60}\n")
            for label, description in private_details.items():
                print(f"{label.title()}: {description}")
        else:
            print("\nNo private details available (or safe mode)")

        # Show full JSON for debugging
        if "--json" in sys.argv:
            print(f"\n{'='*60}")
            print("FULL IDENTITY JSON:")
            print(f"{'='*60}\n")
            print(json.dumps(identity, indent=2))
            if private_details:
                print(f"\n{'='*60}")
                print("PRIVATE DETAILS JSON:")
                print(f"{'='*60}\n")
                print(json.dumps(private_details, indent=2))

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
