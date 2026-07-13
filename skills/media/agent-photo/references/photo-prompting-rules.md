# Photo Prompting Rules

Use these rules when composing a scene for `agent-photo`.

## Describe The Frame

Describe what the viewer sees, not how the image was captured. Terms such as "selfie," "holding a phone," or "camera pointing at" often produce visible phones, extra arms, or a picture of someone taking a picture.

Prefer:

- "close portrait, looking directly into the lens"
- "portrait from slightly above, looking up toward the viewer"
- "arm continuing past the edge of the frame"

The viewer is the camera. Describe the subject's relationship to the viewer.

## Keep The Scene Coherent

A useful scene contains:

1. framing and viewer angle;
2. one physically possible pose;
3. expression described through eyes, mouth, and posture;
4. outfit or visible body state;
5. location and background;
6. one lighting direction and quality.

Do not stack several limb positions or body orientations. "Leaning on both elbows" is more reliable than a sequence of instructions for shoulders, hands, waist, and legs.

## Let The Seed Own Identity

The seed is authoritative for facial structure, hair, eyes, skin, makeup, piercings, and stable jewelry. Repeating those details in the scene can overwrite identity or duplicate facial hardware.

If an identity field should be omitted from prompt text, write "Carried by seed image" in `identity.md`. Do not put negative instructions such as "do not describe eyes" in identity fields; prompt construction may inject the negative wording into the generated prompt.

Extreme angles weaken identity lock. Prefer a moderate angle or ask before rerolling. Do not compensate by restating every facial detail.

## Provider Notes

Gemini can exaggerate words such as "flushed," "rosy," or "red cheeks." Use "faint natural warmth across the cheekbones" only when the detail matters. Gemini also has the narrowest content acceptance, so choose another provider before generation when the requested scene is predictably outside its range.

Grok handles multiple outputs and multiple source images. With several sources, treat the lifelike seed as identity and keep each additional image's role explicit and narrow.

Seedream accepts multiple reference images and is useful when a scene requires body baselines. Keep the total sources limited to the few references that materially affect the result.

Provider failure does not authorize a second paid call. Use `--allow-fallback` only after the user authorizes a retry or alternate provider.

## Final Prompt Check

- Does the prompt describe the resulting image rather than the capture mechanism?
- Is there one clear pose and one viewer angle?
- Did the scene avoid repeating seed-owned identity traits?
- Are every source image and its purpose necessary?
- Is the selected provider appropriate before the paid call?
- Is the requested output count exact?
