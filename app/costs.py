"""Cost ledger: every paid call records a line item; totals are reported in INR."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from . import config


@dataclass
class LineItem:
    kind: str          # text | image | veo | tts
    detail: str
    quantity: float    # tokens / images / seconds / chars
    usd: float
    cached: bool = False


@dataclass
class CostLedger:
    items: list[LineItem] = field(default_factory=list)

    def text(self, detail: str, in_tokens: int, out_tokens: int, cached: bool = False) -> None:
        usd = 0.0 if cached else (
            in_tokens / 1e6 * config.PRICES["text_input_per_1m"]
            + out_tokens / 1e6 * config.PRICES["text_output_per_1m"]
        )
        self.items.append(LineItem("text", detail, in_tokens + out_tokens, usd, cached))

    def image(self, detail: str, count: int = 1, cached: bool = False) -> None:
        usd = 0.0 if cached else count * config.PRICES["image_per_unit"]
        self.items.append(LineItem("image", detail, count, usd, cached))

    def veo(self, detail: str, seconds: float, cached: bool = False) -> None:
        usd = 0.0 if cached else seconds * config.PRICES["veo_per_second"]
        self.items.append(LineItem("veo", detail, seconds, usd, cached))

    def tts(self, detail: str, chars: int, cached: bool = False) -> None:
        usd = 0.0 if cached else chars / 1e6 * config.PRICES["tts_per_1m_chars"]
        self.items.append(LineItem("tts", detail, chars, usd, cached))

    def sung(self, detail: str, minutes: float, cached: bool = False) -> None:
        usd = 0.0 if cached else minutes * config.PRICES["sung_per_minute"]
        self.items.append(LineItem("sung", detail, minutes, usd, cached))

    @property
    def total_usd(self) -> float:
        return sum(i.usd for i in self.items)

    @property
    def total_inr(self) -> float:
        return round(self.total_usd * config.USD_TO_INR, 2)

    def summary(self) -> dict:
        by_kind: dict[str, dict] = {}
        for i in self.items:
            b = by_kind.setdefault(i.kind, {"usd": 0.0, "inr": 0.0, "quantity": 0.0, "cached_hits": 0})
            b["usd"] += i.usd
            b["inr"] = round(b["usd"] * config.USD_TO_INR, 2)
            b["quantity"] += i.quantity
            b["cached_hits"] += int(i.cached)
        return {
            "total_usd": round(self.total_usd, 4),
            "total_inr": self.total_inr,
            "usd_to_inr": config.USD_TO_INR,
            "by_kind": by_kind,
            "items": [asdict(i) for i in self.items],
        }


def estimate(num_scenes: int, engine: str, languages: list[str], chars_per_lang: int = 1200,
             vocals: str = "tts", seconds: int = 120, clip_seconds: float = 0) -> dict:
    """Rough pre-flight estimate shown before the user clicks Generate."""
    led = CostLedger()
    led.text("script", 2500, 2500)
    if engine == "clips":    # no pictures to draw: Gemini watches the footage (≈ 300 tokens per second) and writes lyrics
        led.text("watch your clips", int(clip_seconds * 300), 600)
        for lang in languages:
            led.text(f"rhyming lyrics {lang}", 1500, 1200)
    elif engine == "cutout":   # ≈ 6 backgrounds + 3 props (+ a one-time cut-out per new character)
        led.image("backgrounds + props", min(num_scenes, 9))
    else:
        led.image("keyframes", num_scenes + 1)
    if engine == "veo":
        led.veo("clips", num_scenes * config.VEO_CLIP_SECONDS)
    for lang in languages:
        if vocals == "sung":
            led.sung(lang, seconds / 60)
        else:
            led.tts(lang, chars_per_lang)
    return led.summary()
