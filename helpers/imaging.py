"""Pure image-cropping helpers for the guessing game.

Kept in their own light module (numpy + PIL only, no discord/app imports) so they can run
in the process pool: a spawned worker importing this module doesn't drag in the whole bot.
"""

import io
import random

import numpy as np
from PIL import Image


def _crop_chart(data: bytes) -> io.BytesIO:
    arr = np.array(Image.open(io.BytesIO(data)).convert("RGB"))
    height, width, _ = arr.shape
    row = max(3, round((width - 80) / 272))
    rannum = random.randint(2, row - 1)
    start_x = 80 + 272 * (rannum - 1)
    cropped = arr[32 : height - 287, start_x : start_x + 192]
    mid_y = cropped.shape[0] // 2
    img1, img2 = cropped[: mid_y + 20], cropped[mid_y - 20 :]
    final_height = max(img1.shape[0], img2.shape[0])
    final = np.full((final_height, 410, 3), 255, dtype=np.uint8)
    final[: img2.shape[0], 10 : 10 + img2.shape[1]] = img2
    final[: img1.shape[0], 210 : 210 + img1.shape[1]] = img1
    f = io.BytesIO()
    Image.fromarray(final).save(f, "PNG")
    f.seek(0)
    return f


def _crop_square(data: bytes, size: int, bw: bool) -> io.BytesIO:
    arr = np.array(Image.open(io.BytesIO(data)).convert("L" if bw else "RGB"))
    h, w = arr.shape[:2]
    size = min(size, w, h)
    x = random.randint(0, w - size)
    y = random.randint(0, h - size)
    out = Image.fromarray(arr[y : y + size, x : x + size])
    f = io.BytesIO()
    out.save(f, "PNG")
    f.seek(0)
    return f
