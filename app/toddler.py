"""Toddler-psychology presets: colours, shapes, pacing, voice and music.

Toddlers (1-3 yrs) respond to: high-contrast saturated primaries, large rounded
shapes, big friendly eyes, slow smooth motion (no fast cuts), repetition,
a warm slightly higher-pitched voice spoken slowly, and simple major-key
melodies at a walking tempo.  Everything here feeds the prompts and the renderer.
"""

PALETTE = {
    "sunshine_yellow": "#FFD93D",
    "sky_blue": "#6BCBFF",
    "apple_red": "#FF6B6B",
    "grass_green": "#8BE78B",
    "candy_pink": "#FFA3D7",
    "orange_pop": "#FFA94D",
    "soft_purple": "#C3A6FF",
    "cream_white": "#FFF8E7",
}

STYLE_PRESETS = {
    "2d": (
        "flat 2D cartoon illustration for toddlers, thick clean outlines, bold saturated "
        "primary colours ({palette}), big rounded shapes, oversized friendly eyes, soft "
        "gradient sky, simple uncluttered background, no text, no letters, centred composition, "
        "storybook children's TV style, consistent character design"
    ),
    "3d": (
        "cute 3D rendered cartoon for toddlers, soft rounded plush-like shapes, bright "
        "saturated colours ({palette}), smooth matte materials, big sparkling friendly eyes, "
        "soft global illumination, shallow depth of field, Pixar-like preschool show, no text, "
        "simple clean background, consistent character design"
    ),
}

NEGATIVE = (
    "scary, dark, horror, realistic human faces, blood, weapons, text, letters, watermark, "
    "logo, blurry, deformed, extra limbs, cluttered background, fast motion"
)

VOICE = {
    # Slower and a touch higher than adult narration; toddlers track it better.
    "speaking_rate": 0.88,
    "pitch_semitones": 2.5,
    "pause_between_lines_ms": 450,
}

MUSIC = {
    "bpm": 92,
    "key_root_hz": 261.63,  # C4
    "scale": [0, 2, 4, 7, 9],  # major pentatonic: no dissonance
    "duck_db": -16,  # music level under the voice
}


def style_prompt(mode: str) -> str:
    palette = ", ".join(PALETTE.keys()).replace("_", " ")
    return STYLE_PRESETS.get(mode, STYLE_PRESETS["2d"]).format(palette=palette)
