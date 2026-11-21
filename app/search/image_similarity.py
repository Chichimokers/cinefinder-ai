"""Similitud visual de imágenes mediante dHash (64 bits)."""

from __future__ import annotations

import io

from PIL import Image


def dhash(image_bytes: bytes, hash_size: int = 8) -> int:
    """Hash perceptual: compara cada píxel con su vecino derecho."""
    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    image = image.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    value = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def similarity(a: int, b: int) -> float:
    """Porcentaje 0-100 de similitud entre dos dHash."""
    return round((1 - hamming(a, b) / 64.0) * 100.0, 1)


def compare_images(original: bytes, candidate: bytes) -> float:
    return similarity(dhash(original), dhash(candidate))
