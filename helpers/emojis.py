import json

from discord.ext import commands

EXPORT_PATH = "data/emojis.json"

FIELDS = (
    "append_ap",
    "append_fc",
    "append_clear",
    "append_none",
    "ap",
    "fc",
    "clear",
    "none",
    "mikuleak",
)


class Emojis:
    def __init__(self) -> None:
        self.append_ap = "append_ap"
        self.append_fc = "append_fc"
        self.append_clear = "append_clear"
        self.append_none = "append_fail"

        self.ap = "normal_ap"
        self.fc = "normal_fc"
        self.clear = "normal_clear"
        self.none = "normal_fail"

        self.mikuleak = "mikuleak"  # resolves to the mention once uploaded

        self.difficulty_colors = {
            "easy": "easy_color",
            "normal": "normal_color",
            "hard": "hard_color",
            "expert": "expert_color",
            "master": "master_color",
            "append": "append_color",
        }

        self.attributes = {
            "cool": "icon_attribute_cool",
            "cute": "icon_attribute_cute",
            "happy": "icon_attribute_happy",
            "mysterious": "icon_attribute_mysterious",
            "pure": "icon_attribute_pure",
        }

        self.rarities = {
            "trained": "rarity_star_afterTraining",
            "untrained": "rarity_star_normal",
            "birthday": "rarity_birthday",
        }

    def _needed_names(self) -> list[str]:
        return [
            *self.attributes.values(),
            *self.rarities.values(),
            *self.difficulty_colors.values(),
            *(getattr(self, field) for field in FIELDS),
        ]

    async def load(self, bot: commands.Bot) -> None:
        exported: dict[str, str] = {}
        try:
            with open(EXPORT_PATH, "r", encoding="utf-8") as f:
                exported = {
                    name: info["mention"] for name, info in json.load(f).items()
                }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            exported = {}

        fetched: dict[str, str] = {}
        if any(name not in exported for name in self._needed_names()):
            for emoji in await bot.fetch_application_emojis():
                fetched[emoji.name] = (
                    f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>"
                )

        def resolve(emoji_name: str) -> str | None:
            return exported.get(emoji_name) or fetched.get(emoji_name)

        for mapping in (self.attributes, self.rarities, self.difficulty_colors):
            for key, emoji_name in mapping.copy().items():
                resolved = resolve(emoji_name)
                if resolved:
                    mapping[key] = resolved

        for field in FIELDS:
            resolved = resolve(getattr(self, field))
            if resolved:
                setattr(self, field, resolved)


emojis = Emojis()
