from app.services import entity_catalog_service


def test_catalog_resolves_display_name_and_alias_to_one_canonical_id():
    catalog = entity_catalog_service.build_character_catalog(
        [
            {
                "id": "lin-qiao",
                "name": "Lin Qiao",
                "display_name": "林乔",
                "aliases": ["林乔", "Lin Qiao"],
            }
        ]
    )

    assert catalog == [
        {
            "id": "character_lin_qiao",
            "display_name": "林乔",
            "aliases": ["林乔", "Lin Qiao"],
        }
    ]
    assert entity_catalog_service.match_character_mentions("林乔推开门。", catalog) == ["character_lin_qiao"]
    assert entity_catalog_service.match_character_mentions("Lin Qiao pushed the door.", catalog) == ["character_lin_qiao"]


def test_presence_requires_explicit_scene_participation_evidence():
    catalog = entity_catalog_service.build_character_catalog(
        [{"id": "lin-qiao", "display_name": "林乔", "aliases": ["Lin Qiao"]}]
    )

    assert entity_catalog_service.match_character_mentions("林乔的名字写在名单上。", catalog) == ["character_lin_qiao"]
    assert entity_catalog_service.match_scene_participants("场景参与：林乔", catalog) == ["character_lin_qiao"]
    assert entity_catalog_service.match_scene_participants("林乔的名字写在名单上。", catalog) == []
