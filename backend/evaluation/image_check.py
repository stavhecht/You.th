"""Render each evaluated article's FLUX picture and check it.

    python -m evaluation.image_check evaluation/reports/<run>

The harness does not run ILLUSTRATE (it writes ``media_objects``), so this
drives the same production parts the stage does, minus the store: the
claim-free ``build_prompt`` over each trace's extraction, the configured
``HuggingFaceImageClient``, and crop mode's ``crop_to_ratio`` for the cover.
It renders even while ``IMAGE_PROVIDER=none``, because asking for this run is
the decision to spend; it needs ``IMAGE_GEN_KEY``. One billed render per case.

Per case it records latency, the dimensions read back against the ones asked
for, bytes against ``media_max_bytes``, the prompt, and any word of the
headline's claim that leaked into it (the prompt must never carry the claim:
``imagery/prompt.py``). Whether the picture *fits* the article is a judgement:
``images/contact_sheet.jpg`` puts every lead beside its headline for a person
to look at.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import get_settings
from app.imagery.base import ImageError, ImageRequest
from app.imagery.encode import crop_to_ratio
from app.imagery.factory import build_image_client
from app.imagery.prompt import build_prompt
from app.services.illustration import seed_for_run

_WORD = re.compile(r"[a-z]{5,}")


def leaked_claim_words(headline: str, product: str, topic: str, prompt: str) -> list[str]:
    """Headline words (5+ letters) in the prompt that the subject does not explain."""
    subject = set(_WORD.findall(f"{product} {topic}".lower()))
    claim = set(_WORD.findall(headline.lower())) - subject
    return sorted(claim & set(_WORD.findall(prompt.lower())))


async def check(report: Path) -> Path:
    settings = get_settings().model_copy(update={"image_provider": "huggingface"})
    client = build_image_client(settings)
    if client is None:
        raise SystemExit("no image client: is IMAGE_GEN_KEY set?")
    lead_size = (settings.image_lead_width, settings.image_lead_height)
    cover_size = (settings.image_cover_width, settings.image_cover_height)
    out = report / "images"
    out.mkdir(exist_ok=True)

    rows = []
    for path in sorted((report / "traces").glob("*.json")):
        trace = json.loads(path.read_text())
        draft, extraction = trace.get("final_draft"), trace.get("extraction")
        if not draft or not extraction:
            continue
        case, topic = trace["case_id"], trace.get("query") or ""
        seed = seed_for_run(case)
        prompt, negative = build_prompt(
            extraction["product"],
            fallback=topic,
            ingredients=" ".join(extraction["ingredients"]),
            seed=seed,
            outcome=" ".join(extraction["target_claims"]),
        )
        row = {"case": case, "headline": draft["headline"], "prompt": prompt, "seed": seed}
        started = time.monotonic()
        try:
            lead = await client.render(ImageRequest(prompt, negative, *lead_size, seed))
            cover = await asyncio.to_thread(crop_to_ratio, lead, cover_size)
        except ImageError as exc:
            rows.append(row | {"error": str(exc)})
            continue
        row |= {
            "latency_s": round(time.monotonic() - started, 1),
            "lead": [lead.width, lead.height],
            "cover": [cover.width, cover.height],
            "sizes_ok": (lead.width, lead.height) == lead_size
            and cover.width * cover_size[1] == cover.height * cover_size[0],
            "bytes": [len(lead.data), len(cover.data)],
            "under_max_bytes": max(len(lead.data), len(cover.data)) <= settings.media_max_bytes,
            "claim_words_in_prompt": leaked_claim_words(
                draft["headline"], extraction["product"], topic, prompt
            ),
        }
        (out / f"{case}_lead.webp").write_bytes(lead.data)
        (out / f"{case}_cover.webp").write_bytes(cover.data)
        rows.append(row)

    (out / "images.json").write_text(json.dumps(rows, indent=2))
    contact_sheet(rows, out)
    return out


def contact_sheet(rows: list[dict], out: Path) -> None:
    drawn = [r for r in rows if "error" not in r]
    if not drawn:
        return
    tile_w, tile_h, caption = 480, 328, 40
    sheet = Image.new(
        "RGB", (tile_w * 2, (tile_h + caption) * ((len(drawn) + 1) // 2)), "white"
    )
    pen = ImageDraw.Draw(sheet)
    for index, row in enumerate(drawn):
        x, y = tile_w * (index % 2), (tile_h + caption) * (index // 2)
        with Image.open(out / f"{row['case']}_lead.webp") as lead:
            sheet.paste(lead.convert("RGB").resize((tile_w, tile_h)), (x, y + caption))
        pen.text((x + 6, y + 4), f"{row['case']}: {row['headline'][:70]}", fill="black")
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=85)
    (out / "contact_sheet.jpg").write_bytes(buffer.getvalue())


if __name__ == "__main__":
    print(asyncio.run(check(Path(sys.argv[1]))))
