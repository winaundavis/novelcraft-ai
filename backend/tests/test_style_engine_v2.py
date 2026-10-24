from app.services.style_engine import profile_service


def test_compile_contract_exposes_identity_mechanics_and_boundaries():
    profile = {
        "id": "p1",
        "status": "completed",
        "styleKind": "reference",
        "version": 2,
        "identity": {"tone": ["冷硬"], "impression": "克制、压迫"},
        "mechanics": {"information_release": "通过行动逐步揭示", "sentence_variation": "长短交替"},
        "boundaries": {"avoid": ["模板化开场"], "must_preserve": ["事实准确"]},
        "dimensions": {},
        "narrativeRules": [], "characterRules": [], "languageRules": [], "rhythmRules": [], "antiAiRules": [],
    }
    contract = profile_service.compile_contract({}, profile, None)
    assert contract["profile"]["styleKind"] == "reference"
    assert contract["profile"]["version"] == 2
    assert contract["profile"]["identity"]["impression"] == "克制、压迫"
    assert contract["profile"]["mechanics"]["information_release"] == "通过行动逐步揭示"
    assert "模板化开场" in contract["profile"]["boundaries"]["avoid"]


def test_genre_profile_is_deterministic_and_has_no_author_reference():
    first = profile_service.build_genre_style_profile("mystery")
    second = profile_service.build_genre_style_profile("mystery")
    assert first == second
    assert first["styleKind"] == "genre"
    assert first["sourceType"] == "genre"
    assert "author" not in str(first).lower()
    assert first["mechanics"]["information_release"]


def test_style_prompt_contains_the_three_v2_layers():
    block = profile_service.contract_to_prompt_block(
        profile_service.compile_contract(
            {},
            {
                "id": "p1", "status": "completed", "styleKind": "genre", "version": 1,
                "identity": {"impression": "轻快"},
                "mechanics": {"scene_progression": "每场改变一个关系"},
                "boundaries": {"avoid": ["空泛总结"]},
                "dimensions": {}, "narrativeRules": [], "characterRules": [],
                "languageRules": [], "rhythmRules": [], "antiAiRules": [],
            },
            None,
        )
    )
    assert "风格身份" in block
    assert "写法机制" in block
    assert "风格边界" in block
