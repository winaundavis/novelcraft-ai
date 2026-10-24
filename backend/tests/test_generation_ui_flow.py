from __future__ import annotations

from app.models.schemas import GenerateOutlineRequest
from app.services import ai_service
from app.services.novel_generation_service import NovelGenerationJob


def test_novel_job_payload_includes_live_preview_fields():
    job = NovelGenerationJob(
        id="job-1",
        project_id="project-1",
        mode="single",
        requested_chapter_id="chapter-1",
        status="running",
        message="正在生成",
        target_words=1000,
        total_chapters=1,
        current_chapter_id="chapter-1",
    )
    job.preview_chapter_id = "chapter-1"
    job.preview_content = "第一段实时正文"
    payload = job.to_payload()
    assert payload["preview_chapter_id"] == "chapter-1"
    assert payload["preview_content"] == "第一段实时正文"


def test_outline_volume_summaries_are_not_copy_of_single_reply_line():
    req = GenerateOutlineRequest(
        projectId="project-1",
        premise="祈悦在家庭压迫下死亡，父母悔恨并面对失去",
        genre="现实情感",
        targetWordCount=30000,
        targetChapters=6,
        structure="three_act",
    )
    nodes = ai_service._generate_outline_nodes(req, "围绕祈悦死亡后的家庭悔恨展开主线。")
    volumes = [node for node in nodes if node["type"] == "volume"]
    assert len(volumes) >= 2
    assert len({volume["summary"] for volume in volumes}) == len(volumes)
    assert all(volume["summary"] != req.premise for volume in volumes)


def test_outline_uses_distinct_structured_volume_plans_from_model_reply():
    req = GenerateOutlineRequest(
        projectId="project-1",
        premise="祈悦死后，父母在悔恨中重新理解女儿",
        genre="现实情感",
        targetWordCount=90000,
        targetChapters=18,
        structure="three_act",
    )
    reply = """```json
    {
      "mainSummary": "一个家庭在无法挽回的失去之后面对责任与迟来的爱。",
      "volumes": [
        {"title": "惩罚之后", "summary": "祈悦在长期压迫中死去，父母第一次直面惩罚造成的后果。"},
        {"title": "迟来的理解", "summary": "遗物和同学的证言逐层揭开祈悦生前被忽视的求救。"},
        {"title": "无法补偿的爱", "summary": "父母停止自我辩护，以承担责任完成迟来的告别。"}
      ]
    }
    ```"""

    outline = ai_service._parse_outline_reply(reply, req)
    volumes = [node for node in outline["nodes"] if node["type"] == "volume"]

    assert [volume["title"] for volume in volumes] == ["惩罚之后", "迟来的理解", "无法补偿的爱"]
    assert [volume["summary"] for volume in volumes] == [
        "祈悦在长期压迫中死去，父母第一次直面惩罚造成的后果。",
        "遗物和同学的证言逐层揭开祈悦生前被忽视的求救。",
        "父母停止自我辩护，以承担责任完成迟来的告别。",
    ]


def test_structured_outline_never_uses_other_volume_json_as_chapter_summary():
    req = GenerateOutlineRequest(
        projectId="project-1",
        premise="梧渡发现世界规则正在崩坏",
        targetWordCount=90000,
        targetChapters=18,
        structure="three_act",
    )
    reply = """{
      "mainSummary": "梧渡追查逐渐失常的世界。",
      "volumes": [
        {"title": "第一卷：裂痕初现", "summary": "梧渡记录异常并找到研究所。", "beats": ["白菜枕头引发怀疑", "血雨迫使梧渡调查"]},
        {"title": "第二卷：迷雾追踪", "summary": "梧渡潜入研究所并发现锚点计划。", "beats": ["潜入研究所", "发现实验记录"]},
        {"title": "第三卷：最终抉择", "summary": "梧渡面对污染源并作出最终选择。", "beats": ["确认污染源", "作出牺牲选择"]}
      ]
    }"""

    outline = ai_service._parse_outline_reply(reply, req)
    first_volume = next(node for node in outline["nodes"] if node["type"] == "volume" and node["order"] == 1)
    first_chapters = [
        node for node in outline["nodes"]
        if node["type"] == "chapter" and node["parentId"] == first_volume["id"]
    ]

    assert all("第二卷" not in chapter["summary"] for chapter in first_chapters)
    assert all(not any(mark in chapter["summary"] for mark in ('"volumes"', '"title"', "{", "}")) for chapter in first_chapters)
    arcs = [node for node in outline["nodes"] if node["type"] == "arc"]
    assert len(arcs) == 6
    arc_by_id = {arc["id"]: arc for arc in arcs}
    assert all(chapter.get("arcId") in arc_by_id for chapter in outline["nodes"] if chapter["type"] == "chapter")
    assert all(
        arc_by_id[chapter["arcId"]]["parentId"] == chapter["parentId"]
        for chapter in outline["nodes"]
        if chapter["type"] == "chapter"
    )
    first_arcs = [arc for arc in arcs if arc["parentId"] == first_volume["id"]]
    assert [arc["summary"] for arc in first_arcs] == ["白菜枕头引发怀疑", "血雨迫使梧渡调查"]


def test_structured_outline_uses_complete_model_authored_arcs_with_variable_chapter_counts():
    req = GenerateOutlineRequest(
        projectId="project-1",
        premise="祈悦死后，父母在悔恨中重新理解女儿",
        genre="现实情感",
        targetWordCount=30000,
        targetChapters=6,
        structure="three_act",
    )
    reply = """{
      "mainSummary": "一个家庭在无法挽回的失去之后面对责任与迟来的爱。",
      "volumes": [
        {
          "title": "惩罚之后",
          "summary": "祈悦死亡让父母的教育信念第一次彻底崩塌。",
          "arcs": [
            {"title": "门外的最后一夜", "summary": "祈悦在饥饿与羞耻中等待父母松口，却在昏倒后永远失去回应；父母以为只是一次寻常惩罚，直到救护车离开才意识到家已无法回到原样。", "chapterCount": 2},
            {"title": "迟到的证词", "summary": "医院和同学带来的细节击穿父母的辩解，他们争执着谁该负责，最终在祈悦未发出的消息前第一次承认女儿早已求救。", "chapterCount": 1}
          ]
        },
        {
          "title": "无法补偿的爱",
          "summary": "父母停止自我辩护，以承担责任完成迟来的告别。",
          "arcs": [
            {"title": "遗物里的沉默", "summary": "父母整理祈悦遗物时发现她为了不让家人失望而反复删改的心愿；他们决定面对亲友与学校，而不是把悲剧藏成一场意外。", "chapterCount": 3}
          ]
        }
      ]
    }"""

    outline = ai_service._parse_outline_reply(reply, req)
    arcs = [node for node in outline["nodes"] if node["type"] == "arc"]
    chapters = [node for node in outline["nodes"] if node["type"] == "chapter"]

    assert [arc["title"] for arc in arcs] == ["门外的最后一夜", "迟到的证词", "遗物里的沉默"]
    assert arcs[0]["summary"].startswith("祈悦在饥饿与羞耻中等待父母松口")
    assert [chapter["arcId"] for chapter in chapters] == [arcs[0]["id"], arcs[0]["id"], arcs[1]["id"], arcs[2]["id"], arcs[2]["id"], arcs[2]["id"]]
