import csv
import time
from io import StringIO

import aiohttp

from data.pjsk import PJSKData

PRIMARY_CSV = "https://docs.google.com/spreadsheets/d/1B8tX9VL2PcSJKyuHFVd2UT_8kYlY4ZdwHwg9MfWOPug/export?format=csv&gid=1855810409"
SECONDARY_CSV = "https://docs.google.com/spreadsheets/d/1Yv3GXnCIgEIbHL72EuZ-d5q_l-auPgddWi4Efa14jq0/export?format=csv&gid=182216"

DIFFICULTIES = ("easy", "normal", "hard", "expert", "master", "append")
REFRESH_SECONDS = 3600

ConstantKey = tuple[int, str]


class Constants:
    def __init__(self, pjsk: PJSKData) -> None:
        self.pjsk = pjsk
        self.constants: dict[ConstantKey, float] = {}
        self.constants_override: dict[ConstantKey, float] = {}
        self.updated: float = 0

    async def update(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(PRIMARY_CSV) as response:
                if response.status == 200:
                    self._parse_csv(await response.read())
                    self.updated = time.time()
            async with session.get(SECONDARY_CSV) as response:
                if response.status == 200:
                    self._parse_csv(await response.read(), secondary=True)
                    self.updated = time.time()

    def _parse_csv(self, csv_data: bytes, secondary: bool = False) -> None:
        reader = csv.DictReader(StringIO(csv_data.decode("utf-8")))
        for row in reader:
            try:
                music_id = int(row["Song ID"])
                difficulty = row["Difficulty"].lower()
                constant = round(float(row["Constant"]), 1)
                assert difficulty in DIFFICULTIES
            except (ValueError, KeyError, AssertionError):
                continue
            target = self.constants_override if secondary else self.constants
            target[(music_id, difficulty)] = constant

    async def get(
        self,
        music_id: int,
        difficulty: str,
        ap: bool,
        error_on_not_found: bool = False,
        include_source: bool = False,
        force_39s: bool = False,
    ) -> float | None | tuple[float | None, str]:
        if self.updated + REFRESH_SECONDS < time.time():
            await self.update()
        return self.get_sync(
            music_id, difficulty, ap, error_on_not_found, include_source, force_39s
        )

    def get_sync(
        self,
        music_id: int,
        difficulty: str,
        ap: bool,
        error_on_not_found: bool = False,
        include_source: bool = False,
        force_39s: bool = False,
    ) -> float | None | tuple[float | None, str]:
        key = (music_id, difficulty)
        diff: float | None = self.constants_override.get(key)
        source = "Not 39s"
        if force_39s or not diff:
            diff = self.constants.get(key)
            source = "39s Constants"
        if not diff:
            if error_on_not_found:
                raise IndexError()
            diff = self.pjsk.get_play_level(music_id, difficulty)
            source = "Not Rated! (wait for rating if expert/master/append)"
        value = diff - 1 if diff and not ap else diff
        if include_source:
            return value, source
        return value
