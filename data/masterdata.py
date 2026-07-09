from typing import Any, Callable

from data.models import (
    Card,
    Character,
    CheerfulCarnivalTeam,
    Event,
    Gacha,
    GachaRate,
    WorldBloom,
)

EVENT_FILES = ("events", "eventDeckBonuses", "worldBlooms", "gameCharacterUnits")
CHARACTER_FILES = ("gameCharacters", "characterProfiles", "cheerfulCarnivalTeams")

TRAINABLE_RARITIES = ("rarity_3", "rarity_4")

AssetUrl = Callable[[str], str]


def build_events(
    events: list[dict[str, Any]],
    deck_bonuses: list[dict[str, Any]],
    world_blooms: list[dict[str, Any]],
    character_units: list[dict[str, Any]],
    asset_url: AssetUrl,
) -> list[Event]:
    unit_to_char: dict[int, int] = {
        u["id"]: u["gameCharacterId"] for u in character_units
    }

    # Attr-only bonus rows carry the event's bonus attribute; character-only
    # rows carry the bonus characters (rows with both are the overlap boost).
    bonus_attrs: dict[int, str] = {}
    bonus_chars: dict[int, list[int]] = {}
    for b in deck_bonuses:
        eid = b["eventId"]
        has_char = "gameCharacterUnitId" in b
        if "cardAttr" in b and not has_char:
            bonus_attrs.setdefault(eid, b["cardAttr"])
        elif has_char and "cardAttr" not in b:
            char_id = unit_to_char.get(b["gameCharacterUnitId"])
            chars = bonus_chars.setdefault(eid, [])
            if char_id is not None and char_id not in chars:
                chars.append(char_id)

    blooms: dict[int, list[WorldBloom]] = {}
    for w in world_blooms:
        blooms.setdefault(w["eventId"], []).append(
            WorldBloom(
                chapter_no=w["chapterNo"],
                game_character_id=w.get("gameCharacterId"),
                start_at=w["chapterStartAt"],
                aggregate_at=w["aggregateAt"],
            )
        )

    out: list[Event] = []
    for e in events:
        abn = e["assetbundleName"]
        unit = e.get("unit")
        out.append(
            Event(
                id=e["id"],
                name=e["name"],
                event_type=e["eventType"],
                unit=None if unit in (None, "none") else unit,
                asset_bundle_name=abn,
                start_at=e.get("startAt"),
                aggregate_at=e.get("aggregateAt"),
                ranking_announce_at=e.get("rankingAnnounceAt"),
                distribution_start_at=e.get("distributionStartAt"),
                closed_at=e.get("closedAt"),
                bonus_attribute=bonus_attrs.get(e["id"]),
                bonus_character_ids=bonus_chars.get(e["id"], []),
                logo_url=asset_url(f"event/{abn}/logo/logo"),
                background_url=asset_url(f"event/{abn}/screen/bg"),
                character_url=asset_url(f"event/{abn}/screen/character"),
                world_blooms=sorted(
                    blooms.get(e["id"], []), key=lambda w: w.chapter_no
                ),
            )
        )
    return out


def build_characters(
    game_characters: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
) -> list[Character]:
    profile_by_id: dict[int, dict[str, Any]] = {p["characterId"]: p for p in profiles}
    out: list[Character] = []
    for gc in game_characters:
        profile = profile_by_id.get(gc["id"], {})
        support = gc.get("supportUnitType")
        height = profile.get("height") or gc.get("height")
        out.append(
            Character(
                id=gc["id"],
                given_name=gc.get("givenName", ""),
                first_name=gc.get("firstName", ""),
                given_name_pronunciation=gc.get("givenNameRuby"),
                first_name_pronunciation=gc.get("firstNameRuby"),
                unit=gc.get("unit"),
                support_unit=None if support in (None, "none") else support,
                gender=gc.get("gender"),
                height=str(height) if height is not None else None,
                birthday=profile.get("birthday"),
                school=profile.get("school"),
                school_year=profile.get("schoolYear"),
                hobby=profile.get("hobby"),
                special_skill=profile.get("specialSkill"),
                favorite_food=profile.get("favoriteFood"),
                hated_food=profile.get("hatedFood"),
                weak_point=profile.get("weak"),
                introduction=profile.get("introduction"),
                voice_actor=profile.get("characterVoice"),
            )
        )
    return out


def build_teams(teams: list[dict[str, Any]]) -> list[CheerfulCarnivalTeam]:
    return [
        CheerfulCarnivalTeam(
            id=t["id"],
            event_id=t["eventId"],
            team_name=t["teamName"],
            asset_bundle_name=t.get("assetbundleName"),
        )
        for t in teams
    ]


def build_cards(cards: list[dict[str, Any]], asset_url: AssetUrl) -> list[Card]:
    out: list[Card] = []
    for c in cards:
        abn = c["assetbundleName"]
        trainable = c["cardRarityType"] in TRAINABLE_RARITIES
        support = c.get("supportUnit")
        phrase = c.get("gachaPhrase")
        out.append(
            Card(
                id=c["id"],
                character_id=c["characterId"],
                card_rarity_type=c["cardRarityType"],
                attr=c.get("attr"),
                support_unit=None if support in (None, "none") else support,
                prefix=c.get("prefix", ""),
                gacha_phrase=None if phrase in (None, "-") else phrase,
                release_at=c.get("releaseAt"),
                archive_published_at=c.get("archivePublishedAt"),
                card_url_normal=asset_url(f"character/member/{abn}/card_normal"),
                card_url_trained=(
                    asset_url(f"character/member/{abn}/card_after_training")
                    if trainable
                    else None
                ),
                # member_cutout, not member_cutout_trm: the trimmed variant only ships
                # for ~700 of the 1405 cards, so newer ones 404.
                cutout_url_normal=asset_url(f"character/member_cutout/{abn}/normal"),
                cutout_url_trained=(
                    asset_url(f"character/member_cutout/{abn}/after_training")
                    if trainable
                    else None
                ),
                thumbnail_url_normal=asset_url(f"thumbnail/chara/{abn}_normal"),
                thumbnail_url_trained=(
                    asset_url(f"thumbnail/chara/{abn}_after_training")
                    if trainable
                    else None
                ),
                frame_rarity=c["cardRarityType"],
            )
        )
    return out


def build_gachas(gachas: list[dict[str, Any]], asset_url: AssetUrl) -> list[Gacha]:
    return [
        Gacha(
            id=g["id"],
            name=g["name"],
            gacha_type=g.get("gachaType"),
            start_at=g.get("startAt"),
            end_at=g.get("endAt"),
            asset_bundle_name=g.get("assetbundleName"),
            banner_url=asset_url(
                f"home/banner/banner_gacha{g['id']}/banner_gacha{g['id']}"
            ),
            rarity_rates=[
                GachaRate(card_rarity_type=r["cardRarityType"], rate=r["rate"])
                for r in g.get("gachaCardRarityRates", [])
                if r.get("lotteryType") == "normal"
            ],
            pickup_card_ids=[p["cardId"] for p in g.get("gachaPickups", [])],
            pool_card_ids=[d["cardId"] for d in g.get("gachaDetails", [])],
        )
        for g in gachas
    ]
