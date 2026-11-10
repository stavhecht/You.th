"""IMAGE_PROVIDER picks the image client, and every unusable setting means none.

A missing key or an unknown name must cost pictures, never a run or a boot.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.imagery.factory import build_image_client
from app.imagery.huggingface import HuggingFaceImageClient


@pytest.mark.parametrize("name", ["none", "off", "", "NONE"])
def test_imagery_can_be_switched_off(name: str) -> None:
    assert build_image_client(Settings(image_provider=name, image_gen_key="hf_x")) is None


def test_hugging_face_needs_a_key() -> None:
    assert build_image_client(Settings(image_provider="huggingface", image_gen_key="")) is None


def test_hugging_face_with_a_key_builds_the_client() -> None:
    client = build_image_client(
        Settings(image_provider="huggingface", image_gen_key="hf_test_key")
    )
    assert isinstance(client, HuggingFaceImageClient)
    assert client.model_id == "huggingface/black-forest-labs/FLUX.1-schnell"


def test_an_unknown_provider_is_no_client_not_a_crash() -> None:
    assert build_image_client(Settings(image_provider="local", image_gen_key="hf_x")) is None


def test_cover_mode_must_be_crop_or_render() -> None:
    assert Settings().image_cover_mode == "crop"
    with pytest.raises(ValueError, match="image_cover_mode"):
        Settings(image_cover_mode="stretch")
