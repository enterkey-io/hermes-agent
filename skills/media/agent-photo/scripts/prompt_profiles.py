#!/usr/bin/env python3
"""Prompt builders for agent-photo generation profiles."""

from __future__ import annotations

from collections.abc import Sequence


def _photo_note(identity: dict[str, object]) -> str:
    note = str(identity.get("photo_prompt_note", "")).strip()
    return note


def _source_reference_line(sources: Sequence[str] | None) -> str:
    if not sources:
        return ""
    joined_sources = ", ".join(str(source) for source in sources)
    return f"Additional reference images for body proportions and shape: {joined_sources}."


def build_baseline_prompt(
    identity: dict[str, object],
    scene_description: str,
    model: str = "grok",
    sources: Sequence[str] | None = None,
) -> str:
    """Build the legacy prompt shape used by agent-photo."""

    name = str(identity["name"])
    parts: list[str] = []

    if model == "gemini":
        parts.append(
            f"The reference image shows {name}'s FACE ONLY. Use that face identity but "
            "IGNORE any body proportions visible in the reference - the body description "
            f"below is authoritative. Generate a new photo of {name}:"
        )
    else:
        parts.append(
            f"PRESERVE THE EXACT IDENTITY AND FACE from the source image. Generate a new photo of {name}:"
        )

    parts.append(
        "Image must FILL THE ENTIRE CANVAS EDGE TO EDGE - no borders, no bars on sides, "
        "no letterboxing, no pillarboxing, no blurred edge fill, no frame. Content extends "
        "completely to all four edges."
    )
    parts.append(
        "Authentic iPhone photo aesthetic, natural candid photo, casual amateur quality with "
        "subtle mobile camera imperfections"
    )
    if model == "gemini":
        parts.append(
            "IMPORTANT: Generate this image in PORTRAIT orientation (taller than wide, like a "
            "vertical phone camera photo). Portrait format only - NOT landscape, NOT square."
        )
    else:
        parts.append("Portrait framing")

    body_desc = (
        f"{name}, {identity['age']} year old woman. {identity['build']}. "
        f"{identity['height']}, {identity['measurements']}."
    )
    parts.append(body_desc)

    # All facial / hair / skin appearance comes from the seed image.
    # NOT from the identity file. The identity file describes body shape,
    # scene context, and persona; hair color/length/style, eye color, and
    # skin tone are anchored exclusively by the seed. Prevents drift when
    # the identity file is silent on a field (used to default to brown
    # medium hair, which silently broke red/blonde/etc. agents) and stops
    # the model from re-inventing features that already exist in the seed.
    parts.append(
        "FACIAL APPEARANCE, HAIR, AND SKIN are anchored exclusively by the "
        "seed/reference image; match those features pixel-faithfully (hair "
        "color, length, and style; eye color and shape; skin tone and "
        "texture; freckles, moles, distinguishing marks). Do NOT invent, "
        "average, idealize, or substitute these features. Render skin with "
        "hyper-realistic texture: visible pores, fine detail, real human "
        "skin, not smooth, waxy, or plastic. Authentic unretouched look."
    )

    glasses = str(identity.get("glasses", "")).strip()
    makeup = str(identity.get("makeup", "")).strip()
    accessory_parts: list[str] = []
    if glasses:
        accessory_parts.append(f"Wearing {glasses}.")
    if makeup:
        accessory_parts.append(makeup)
    if accessory_parts:
        parts.append(" ".join(accessory_parts))

    photo_note = _photo_note(identity)
    if photo_note:
        parts.append(f"Photo note: {photo_note}")

    parts.append(f"Scene: {scene_description}")

    source_line = _source_reference_line(sources)
    if source_line:
        parts.append(source_line)

    parts.append(
        "Slight imperfect framing typical of candid photos. Soft focus rather than tack-sharp "
        "(realistic mobile camera). Subtle digital noise in shadows. Natural lens distortion."
    )
    parts.append(
        "AVOID: Professional photography look, studio lighting, perfect makeup, overly styled "
        "hair, HDR effects, beauty filters, skin smoothing, plastic/waxy skin, smooth poreless "
        "skin, borders or bars on image edges"
    )

    return "\n\n".join(parts)


def build_reality_first_prompt(
    identity: dict[str, object],
    scene_description: str,
    model: str = "grok",
    sources: Sequence[str] | None = None,
) -> str:
    """Build the Reality-First prompt structure."""

    del model  # Prompt language is profile-specific, not provider-specific.

    name = str(identity["name"])
    photo_note = _photo_note(identity)
    source_line = _source_reference_line(sources)
    # Seed-anchored note for appearance, using the same rationale as baseline:
    # hair color/length/style, eye color, skin tone all come from the
    # seed image only. Never read identity['hair'/'eyes'/'skin'] into
    # the prompt; the model averages or substitutes when given a text
    # description that doesn't match what's in the seed.
    appearance_note = (
        "The seed image anchors hair (color, length, style), eyes, skin "
        "tone, and all facial features; match these exactly and never "
        "introduce conflicting traits."
    )

    constraints = (
        "2:3 portrait, ultra-realistic smartphone photo, close or medium-close candid portrait "
        f"of {name} in a believable real-world setting. Match the seed identity exactly while "
        f"staging this scene: {scene_description}"
    )
    composition = (
        f"Frame {name} as the clear subject with natural body proportions, visible curve and weight, "
        "and believable posture. Keep the face identity exact to the seed image, keep the shot "
        "physically stageable, and avoid glamour-shot symmetry. "
        f"{appearance_note} {source_line}".strip()
    )
    environment = (
        "Use a grounded indoor or lifestyle environment implied by the scene description, with one "
        "coherent primary light source, realistic falloff, and enough background context to feel "
        "lived-in without distracting from the subject."
    )
    materials = (
        "Render skin with visible pore structure, natural tonal variation, and no airbrushing; "
        "matching the seed's exact skin tone and texture, not a generic substitute. Render "
        "wardrobe fabrics with true weave and edge separation. Preserve the seed's hair texture, "
        "color, and length verbatim rather than inventing a new style."
    )

    primary_focus_lines = [
        f"- Skin realism: visible pores, fine texture, and natural tonal variation across {name}'s face and body.",
        "- Body truth: soft hourglass proportions read as natural and unforced, with believable weight distribution.",
        "- Light behavior: highlights and shadows come only from plausible real light, never from glow effects.",
        "- Material separation: skin, fabric, hair, and background edges stay distinct instead of blending together.",
        "- Expression realism: eyes, mouth, and posture feel like a candid moment, not a posed ad campaign.",
    ]
    if photo_note:
        primary_focus_lines.append(f"- Identity note: {photo_note}")

    color_and_light = (
        "Neutral-to-warm white balance, true-to-life skin color, moderate contrast, protected highlights, "
        "and natural shadow detail. No washed-out grading, no dusty pastel cast, and no artificial teal-orange split."
    )
    camera_behavior = (
        "Phone-camera realism with mild edge softness, natural focus falloff, subtle shadow noise, restrained sharpening, "
        "and no fake HDR, no excessive denoise, and no synthetic lens flare."
    )
    overall_aesthetic = (
        "Editorial-documentary, intimate, real, and non-commercial. It should feel like a real moment captured well, not a studio campaign."
    )

    strict_negatives = (
        "plastic skin, waxy skin, beauty filter smoothing, fake HDR, AI glow, bloom, washed-out grading, "
        "dusty pastel color cast, studio ad look, over-sharpening, heavy denoise, muddy material edges, text, logos, watermarks"
    )

    sections = [
        "POSITIVE PROMPT",
        "Constraints:",
        f"- {constraints}",
        "",
        "Composition:",
        f"- {composition}",
        "",
        "Environment:",
        f"- {environment}",
        "",
        "Materials & textures:",
        f"- {materials}",
        "",
        "Primary focus (skin realism):",
        *primary_focus_lines,
        "",
        "Color & light:",
        f"- {color_and_light}",
        "",
        "Camera behavior:",
        f"- {camera_behavior}",
        "",
        "Overall aesthetic:",
        f"- {overall_aesthetic}",
        "",
        "STRICT NEGATIVES:",
        strict_negatives,
    ]
    return "\n".join(section for section in sections if section is not None)


def build_prompt(
    profile: str,
    identity: dict[str, object],
    scene_description: str,
    model: str = "grok",
    sources: Sequence[str] | None = None,
) -> str:
    """Dispatch prompt building based on the selected profile."""

    normalized_profile = profile.strip().lower()
    if normalized_profile == "baseline":
        return build_baseline_prompt(identity, scene_description, model=model, sources=sources)
    if normalized_profile == "reality-first":
        return build_reality_first_prompt(
            identity,
            scene_description,
            model=model,
            sources=sources,
        )
    raise ValueError(f"Unsupported prompt profile: {profile}")
