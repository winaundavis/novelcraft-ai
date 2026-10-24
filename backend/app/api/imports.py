"""Import APIs for markdown folders and suggestion documents."""

import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.dao import projects as projects_dao
from app.core.config import settings
from app.dao import memory_fts as memory_fts_dao
from app.db import default_style_config, get_conn, json_dump, row_to_chapter, row_to_project, utc_today
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.models.schemas import ApiResponse, gen_id
from app.services.ai_service import apply_suggestion_to_text
from app.services.import_service import (
    build_analysis_summary,
    parse_suggestion_docx,
    scan_markdown_folder,
)
from app.services.style_engine import transition_service
from app.utils.text_repair import repair_data, repair_text


router = APIRouter(prefix="/import", tags=["import"])

def _upload_dir() -> Path:
    target = Path(settings.upload_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


class ApplySuggestionRequest(BaseModel):
    suggestionId: str
    projectId: str
    action: str
    target: str
    context: str = ""


class ImportCharacterSelection(BaseModel):
    """「人物」页勾选导入候选时的请求体：只提交名字，其余字段由服务端按事实生成。"""

    names: list[str] = []


def _resolve_existing_path(path_str: str, expect: str) -> Path:
    candidate = Path(path_str).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.resolve()
    else:
        candidate = Path(os.path.abspath(str(candidate)))

    if expect == "dir" and not candidate.is_dir():
        raise HTTPException(status_code=400, detail="文件夹路径不存在或不是目录。")
    if expect == "file" and not candidate.is_file():
        raise HTTPException(status_code=400, detail="文件路径不存在或不是文件。")
    return candidate


def _store_uploaded_file(file: UploadFile) -> Path:
    suffix = Path(file.filename or "upload.docx").suffix.lower()
    if suffix not in {".docx", ".txt", ".md"}:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{suffix}")

    allowed_mime_types = {
        ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/octet-stream"},
        ".txt": {"text/plain", "application/octet-stream"},
        ".md": {"text/markdown", "text/plain", "application/octet-stream"},
    }
    content_type = (file.content_type or "").lower()
    if content_type and content_type not in allowed_mime_types[suffix]:
        raise HTTPException(status_code=400, detail="上传文件 MIME 类型与扩展名不匹配。")

    payload = file.file.read()
    if suffix == ".docx" and not payload.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=400, detail="DOCX 文件签名无效。")
    if suffix in {".txt", ".md"} and b"\x00" in payload:
        raise HTTPException(status_code=400, detail="文本文件包含二进制内容。")
    if len(payload) > settings.upload_max_bytes:
        raise HTTPException(status_code=400, detail="上传文件超过大小上限。")

    target = _upload_dir() / f"{uuid.uuid4().hex}{suffix}"
    with target.open("wb") as handle:
        handle.write(payload)
    return target


_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_MARKDOWN_MIME_TYPES = {"text/markdown", "text/plain", "application/octet-stream", ""}


def _safe_relative_path(raw: str, fallback: str) -> Path:
    """把浏览器给的上传相对路径净化成安全的相对路径。

    拒绝绝对路径与 `..` 穿越（`webkitdirectory` 会给带目录的路径，
    普通多选只会给文件名）。落盘位置永远在调用方给的临时根目录下。
    """
    cleaned = (raw or "").replace("\\", "/").strip()
    parts = [
        part for part in cleaned.split("/")
        if part and part not in {".", ".."} and not part.endswith(":")
    ]
    if not parts:
        parts = [Path(fallback or "chapter.md").name or "chapter.md"]
    return Path(*parts)


def _upload_folder_name(relative_paths: list[str], fallback: str = "") -> str:
    """从上传的相对路径里推断一个像样的项目名。

    临时目录名是随机 uuid，直接拿去当项目名会很难看。选整个文件夹时第一个
    相对路径形如「我的小说/第01章.md」，取顶层目录名；多选文件时推不出来，
    退回兜底文案。
    """
    for raw in relative_paths:
        cleaned = (raw or "").replace("\\", "/").strip().strip("/")
        if "/" not in cleaned:
            continue
        head = cleaned.split("/")[0].strip()
        if head and head not in {".", ".."} and not head.endswith(":"):
            return head
    return fallback or "Markdown 导入"


def _store_markdown_uploads(files: list[UploadFile], relative_paths: list[str]) -> Path:
    """把上传的 Markdown 落到临时目录，目录结构按上传时的相对路径还原。

    章节顺序沿用 `scan_markdown_folder` 的规则（文件名里的章节号优先，
    其次相对路径），所以「多选文件」与「选整个文件夹」的语义完全一致。
    调用方负责在 finally 里删掉返回的临时目录。
    """
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个 Markdown 文件。")

    root = Path(tempfile.mkdtemp(prefix="novelcraft-md-import-"))

    def _abort(detail: str) -> None:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail=detail)

    try:
        for index, file in enumerate(files):
            raw_name = file.filename or f"chapter-{index + 1}.md"
            if Path(raw_name).suffix.lower() not in _MARKDOWN_SUFFIXES:
                _abort(f"只支持 .md / .markdown 文件：{Path(raw_name).name}")

            content_type = (file.content_type or "").lower()
            if content_type not in _MARKDOWN_MIME_TYPES:
                _abort("上传文件 MIME 类型与扩展名不匹配。")

            payload = file.file.read()
            if b"\x00" in payload:
                _abort(f"文件包含二进制内容：{Path(raw_name).name}")
            if len(payload) > settings.upload_max_bytes:
                _abort(f"上传文件超过大小上限：{Path(raw_name).name}")

            relative = relative_paths[index] if index < len(relative_paths) else raw_name
            target = root / _safe_relative_path(relative, raw_name)
            # 不同目录下的同名章节（如「第一卷/第01章.md」「第二卷/第01章.md」）不能互相覆盖
            if target.exists():
                target = target.with_name(f"{target.stem}-{index + 1}{target.suffix}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    return root


def _chapter_outline_node(*, chapter_id: str, title: str, order: int, word_count: int) -> dict:
    """大纲里的一个章节节点（前端 ChapterTree 直接渲染这些字段）。"""
    return {
        "id": chapter_id,
        "parentId": None,
        "type": "chapter",
        "title": title,
        "summary": "",
        "conflict": "",
        "revelation": "",
        "emotion": "",
        "isKeyPoint": False,
        "order": order,
        "targetWordCount": word_count,
        "povCharacterId": None,
    }


def _build_import_outline(chapters: list[dict], summary: str) -> str:
    """由导入的正文生成大纲，让「章节导航」在导入后立刻有内容。

    此前导入只写 chapters，projects.outline 固定写 "null"，导致：
    导入 100 章后章节导航依然一片空白，作者无法从导航进入已导入的章节。
    这里按章节顺序生成一层 chapter 节点（不虚构卷/弧/摘要，只搬运已有事实）：
    前端 ChapterTree 在没有 volume 节点时会把所有 chapter 节点平铺在「大纲章节」下。
    """
    nodes = []
    for chapter in sorted(chapters, key=lambda item: item.get("order", 0)):
        # 节点 id 必须复用扫描阶段生成的章节 id —— 它就是 chapters 行的主键（见下方 INSERT）。
        # 这里曾用 gen_id() 另生成一个，于是导航节点指向**不存在的章节**：
        # 前端点进去 GET /chapters/{node.id} 拿到 404，正文全空白，
        # 而章节行其实完好（实测 f0ee954f3291：114 章全有正文，节点 id 匹配数为 0）。
        nodes.append(
            _chapter_outline_node(
                chapter_id=chapter["id"],
                title=repair_text(chapter.get("title") or ""),
                order=chapter.get("order", 0),
                word_count=chapter.get("wordCount", 0),
            )
        )
    return json_dump(
        {
            "mainSummary": summary,
            # 必须是 schemas.Outline 声明的枚举写法（下划线）。这里历史上写成 "three-act"，
            # 与自身声明的 Literal["three_act", ...] 不符：前端按枚举查中文名会漏掉，
            # 于是把内部值 "three-act" 原样显示给作者。改回规范写法的同时，
            # 前端也做拼写归一，以便兼容此前已写入库中的连字符值。
            "structure": "three_act",
            "nodes": nodes,
            "outlineVersion": 1,
        }
    )


# 导入到**已有项目**时对既有章节的处理方式，由作者在导入面板上选：
#   replace —— 把当前项目的全部章节移入回收站，再写入导入内容（历史行为）
#   merge   —— 只替换**章节序号相同**的章节（按 order 对齐，与章节标题无关），
#              导入文件里没有的序号原样保留
# 两种方式下被替换的旧正文都会先进回收站（含正文与提交历史），因此都可回查。
_IMPORT_MERGE_MODES = ("replace", "merge")


def _merge_import_outline(existing_raw: str | None, chapter_rows) -> str:
    """增量合并后的大纲：按**章节序号**对齐刷新章节节点，结构节点与手工字段全部保留。

    完全替换可以拿导入内容重建大纲，合并不行 —— 合并会留下导入文件里没有的章节，
    重建会让这些章节从导航里消失（正文还在，作者却点不进去）。这里改为按 order 对齐：

    - 卷/弧等结构节点、以及作者手改过的字段（摘要、关键点、POV、归属卷）一律不动；
    - 序号对得上的章节节点只更新 id/title/targetWordCount（正文换人了，节点得跟着换）；
    - 项目里有、大纲里还没有的章节补一个平铺节点。

    既有大纲缺失或损坏时退回与导入同源的平铺结构。
    """
    try:
        parsed = json.loads(existing_raw) if existing_raw else None
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        parsed = None
    nodes = parsed.get("nodes") if parsed else None
    if not isinstance(nodes, list) or not nodes:
        return _build_import_outline(
            [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "order": row["chapter_order"],
                    "wordCount": row["word_count"],
                }
                for row in chapter_rows
            ],
            "",
        )

    node_by_order = {
        int(node.get("order") or 0): node
        for node in nodes
        if isinstance(node, dict) and node.get("type") == "chapter"
    }
    for row in chapter_rows:
        order = int(row["chapter_order"] or 0)
        node = node_by_order.get(order)
        if node is None:
            nodes.append(
                _chapter_outline_node(
                    chapter_id=row["id"],
                    title=repair_text(row["title"] or ""),
                    order=order,
                    word_count=int(row["word_count"] or 0),
                )
            )
            continue
        node["id"] = row["id"]
        node["title"] = repair_text(row["title"] or "")
        node["targetWordCount"] = int(row["word_count"] or 0)

    # 结构未设置时补上导入一贯的写法，避免合并后的项目在「结构」卡片上什么都不显示
    if not parsed.get("structure"):
        parsed["structure"] = "three_act"
    parsed["nodes"] = nodes
    parsed.setdefault("outlineVersion", 1)
    return json_dump(parsed)


# 增量合并按**文件名里的章节号**对齐（作者指定：第几章，不看标题）。
#
# 为什么不直接复用 `import_service._extract_chapter_number`：那是给**排序**用的，
# 取的是文件名里第一个数字 —— 对「第2卷 第10章.md」会拿到卷号 2，拿它当身份键
# 会把章节对错（整卷的章都会被算成同一章）。所以这里单独一套规则，并按此写测试。
_CHAPTER_NUMBER_PATTERNS = (
    re.compile(r"第\s*(\d+)\s*[章节回话]"),  # 第10章 / 第 3 节 / 第5话（优先，能跳过卷号）
    re.compile(r"^\s*(\d+)"),  # 010-雨夜.md / 10.雨夜.md
)


def _chapter_number(chapter: dict) -> int | None:
    """从文件名里取章节号；取不到返回 None（调用方退回按导入顺序对齐）。"""
    stem = Path(str(chapter.get("fileName") or "")).stem
    for pattern in _CHAPTER_NUMBER_PATTERNS:
        match = pattern.search(stem)
        if match:
            return int(match.group(1))
    # 兜底：文件名里最后一段数字（雨夜-002.md、ch1-02.md）
    tail = re.findall(r"\d+", stem)
    return int(tail[-1]) if tail else None


def _merge_target_orders(chapters: list[dict]) -> tuple[list[int], str]:
    """合并时每一章对齐到哪个章节序号，以及依据（number / index）。

    只有**每一章都能取到互不重复的章节号**时才按章节号对齐；否则整体退回按导入顺序
    对齐 —— 一半按号一半按位置的混合对齐会产生谁也无法解释的结果。依据会回传给前端。
    """
    fallback = [int(chapter.get("order") or 0) for chapter in chapters]
    numbers = [_chapter_number(chapter) for chapter in chapters]
    if any(number is None for number in numbers) or len(set(numbers)) != len(numbers):
        return fallback, "index"
    return [int(number) for number in numbers], "number"


def _residue_orders(
    *,
    previous: list[tuple[int, str]],
    incoming: list[tuple[int, str]],
) -> list[int]:
    """认出「上一次按顺序编号」留下的旧编号残影，返回它们的既有 order。

    背景：作者指定合并按**文件名章节号**对齐，但此前导入是按**顺序**编号的。
    文件夹里有缺号时同一个文件会落在别的 order 上（1、2、4 曾被编成 1、2、3），
    按号合并后 order 3 上的那一章就成了重复章节。

    只有在能**证明**是同一章时才返回，四条都要满足：
      1. 既有 order 恰好是连续的 1..M —— 旧版按顺序编号的痕迹；
      2. 章数与导入相同（M == N）—— 说明是同一个文件夹重导，而不是换了一批文件；
      3. 按名次配对后（旧编号被压缩过，所以名次配对成立）旧 order i 这一章
         现在应该叫 n_i 号，且 n_i 不在旧 order 集合里（否则它会被正常替换）；
      4. **标题完全一致** —— 标题改过就不算，宁可留着让人自己判断。
    """
    previous_orders = sorted(order for order, _ in previous)
    if previous_orders != list(range(1, len(previous_orders) + 1)):
        return []
    numbers = sorted(number for number, _ in incoming)
    if len(numbers) != len(previous_orders):
        return []

    previous_titles = {order: title for order, title in previous}
    incoming_titles = {number: title for number, title in incoming}
    numbers_set = set(numbers)
    residues = []
    for rank, order in enumerate(previous_orders, start=1):
        number = numbers[rank - 1]
        if order == number or order in numbers_set:
            continue
        title = previous_titles.get(order) or ""
        if title and title == (incoming_titles.get(number) or ""):
            residues.append(order)
    return residues


# 导入扫描的人物最多 20 位（见 `import_service._extract_characters`）。这里沿用同一个上限，
# 避免将来上游放宽时把「人物」页一次灌爆。
_IMPORTED_CHARACTER_LIMIT = 20


def _merge_analysis_characters(existing_raw: str | None, analysis: dict) -> tuple[str, int]:
    """把导入扫描出的人物并入 `projects.characters`，返回 (JSON 文本, 新增人数)。

    背景：「人物」面板读的是 `projects.characters`，而导入结果此前只进 `import_analysis`，
    于是导入完一整本书，人物页里仍是空的（作者报告）。

    **调用方**：导入本身**不再**调用它（识别有误判，不该自动建档），改由
    `POST /import/projects/{id}/characters/from-analysis` 在作者勾选后调用，
    离线批量回填走 `scripts/backfill_import_characters.py`。规则只写在这一处。

    **只增不改**：作者手写的角色档案（身份/外貌/性格/背景…）一律不动，同名只算已存在。
    识别本身是词频/词性启发式、必然有误判（形容词、地名都可能混进来），所以：
      - 作者在导入预览里叉掉的条目已经从 analysis 里移除，这里自然不会再补；
      - 新增条目只搬**事实**（出现次数）+ 一句「启发式识别、请人工确认」，
        不编造性别/外貌/性格 —— 那些留给作者或「AI 档案分析」。
    """
    try:
        parsed = json.loads(existing_raw) if existing_raw else []
    except (TypeError, ValueError):
        parsed = []
    merged = [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    known = {str(item.get("name") or "").strip() for item in merged}

    added = 0
    for item in (analysis.get("characters") or [])[:_IMPORTED_CHARACTER_LIMIT]:
        if not isinstance(item, dict):
            continue
        name = repair_text(str(item.get("name") or "")).strip()
        if not name or name in known:
            continue
        frequency = item.get("frequency")
        appeared = (
            f"导入扫描时在正文中出现 {frequency} 次。"
            if isinstance(frequency, int)
            else "导入扫描时识别到该姓名。"
        )
        merged.append(
            {
                "id": gen_id(),
                "name": name,
                "role": "supporting",
                # 具体字段留空由作者填写（age 不写 0：0 岁是个假事实）
                "basicInfo": {"identity": "", "gender": "", "appearance": "", "occupation": ""},
                "personality": {"description": ""},
                "background": f"{appeared}姓名由词频/词性启发式识别，可能有误判，请人工确认。",
            }
        )
        known.add(name)
        added += 1
    return json_dump(merged), added


def _upsert_import_project(
    *,
    project_id: str | None,
    project_name: str | None,
    owner_id: str,
    scan_result: dict,
    merge_mode: str = "replace",
) -> dict:
    if merge_mode not in _IMPORT_MERGE_MODES:
        raise HTTPException(
            status_code=400,
            detail="导入方式只能是 replace（完全替换）或 merge（增量合并）。",
        )

    today = utc_today()
    chapters = scan_result["chapters"]
    analysis = scan_result.get("analysis") or {}
    total_words = scan_result.get("totalWords", 0)
    folder_name = (scan_result.get("folderName") or "").strip() or Path(scan_result["folderPath"]).name
    resolved_name = repair_text((project_name or folder_name or "Imported Novel").strip())
    subtitle = repair_text(analysis.get("summary", ""))
    analysis = repair_data(analysis)
    # 章节导航直接渲染 outline.nodes（ChapterTree.outlineToTree），每章一个节点。
    imported_outline_value = _build_import_outline(chapters, subtitle)

    # 写进项目时用的章节序号：完全替换沿用扫描顺序；增量合并按文件名章节号对齐
    orders = [int(chapter.get("order") or 0) for chapter in chapters]
    merge_key = "index"
    if merge_mode == "merge":
        orders, merge_key = _merge_target_orders(chapters)

    added = len(chapters)
    updated = 0
    kept = 0
    recycled = 0
    cleaned = 0
    cleaned_details: list[dict] = []
    # 人物**不在导入时写入**。识别是词频/词性启发式，直接落库会把误判（「毛巾」「安静」
    # 这类）变成一条条角色档案，作者还得先接受「这是识别结果」再去删。候选留在
    # `import_analysis` 里，由「人物」页的勾选面板决定谁进档 —— 落库走
    # `POST /import/projects/{id}/characters/from-analysis`，用同一个 `_merge_analysis_characters`
    # （只增不改）保证「不编造字段」这条规则只有一份实现。
    characters_detected = len(
        [
            item
            for item in (analysis.get("characters") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
    )

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = None
        resolved_project_id = project_id
        if resolved_project_id:
            existing = conn.execute(
                "SELECT * FROM projects WHERE id = ?",
                (resolved_project_id,),
            ).fetchone()

        if existing:
            resolved_name = repair_text((project_name or existing["name"] or folder_name or "Imported Novel").strip())

        previous_outline_raw: str | None = None
        if existing:
            from app.services.chapter_trash_service import recycle_on_conn, require_idle_on_conn
            require_idle_on_conn(conn, resolved_project_id)
            previous_outline_raw = existing["outline"]
            previous_chapters = conn.execute(
                "SELECT id, chapter_order, title FROM chapters WHERE project_id = ?",
                (resolved_project_id,),
            ).fetchall()
            imported_orders = set(orders)
            replace_all = merge_mode != "merge"

            # 旧编号残影（上一次按顺序编号留下的重复章节）—— 按名次与标题验证后才回收
            residues: set[int] = set()
            if not replace_all and merge_key == "number":
                residues = set(
                    _residue_orders(
                        previous=[(int(row["chapter_order"] or 0), (row["title"] or "").strip()) for row in previous_chapters],
                        incoming=[(order, repair_text(chapter.get("title") or "").strip()) for chapter, order in zip(chapters, orders)],
                    )
                )

            for previous in previous_chapters:
                previous_order = int(previous["chapter_order"] or 0)
                if replace_all or previous_order in imported_orders:
                    recycled += 1
                    if not replace_all:
                        updated += 1
                elif previous_order in residues:
                    cleaned += 1
                    cleaned_details.append({"order": previous_order, "title": previous["title"]})
                else:
                    kept += 1
                    continue
                recycle_on_conn(
                    conn,
                    resolved_project_id,
                    previous["id"],
                    reason="import_replaced" if replace_all else "import_merged",
                )
            if not replace_all:
                # 章节号对不上任何既有章节的那些章就是新增（导入内部的章节号互不重复）
                added = max(len(chapters) - updated, 0)
        else:
            resolved_project_id = resolved_project_id or gen_id()
            conn.execute(
                """
                INSERT INTO projects (
                    id, name, subtitle, genre, target_word_count, current_word_count,
                    status, created_at, updated_at, style_config, outline, tags, owner_id,
                    is_starred, premise, target_chapters, characters, locations,
                    world_rules, import_analysis
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_project_id,
                    resolved_name,
                    subtitle,
                    "xuanhuan",
                    max(total_words, 30000),
                    total_words,
                    "drafting",
                    today,
                    today,
                    json_dump(default_style_config("epic")),
                    imported_outline_value,
                    json_dump(["导入"]),
                    owner_id,
                    0,
                    "",
                    max(len(chapters), 1),
                    "[]",
                    json_dump(analysis.get("locations", [])),
                    "[]",
                    json_dump(analysis),
                ),
            )

        for chapter, order in zip(chapters, orders):
            conn.execute(
                """
                INSERT INTO chapters (
                    id, project_id, volume_id, title, chapter_order, content,
                    word_count, status, summary, notes, created_at, updated_at
                ) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    chapter["id"],
                    resolved_project_id,
                    repair_text(chapter["title"]),
                    order,
                    repair_text(chapter["content"]),
                    chapter["wordCount"],
                    "completed" if chapter["wordCount"] else "draft",
                    "",
                    today,
                    today,
                ),
            )
        transition_service.exclude_chapters_on_conn(
            conn, resolved_project_id, [str(chapter.get("id") or "") for chapter in chapters]
        )

        chapter_rows = conn.execute(
            """
            SELECT * FROM chapters
            WHERE project_id = ?
            ORDER BY chapter_order ASC, created_at ASC
            """,
            (resolved_project_id,),
        ).fetchall()

        if existing:
            # 合并后项目里可能既有新导入的章节、也有被保留下来的旧章节，
            # 所以大纲与计数都必须按**合并后的全部章节**算，而不是只看这次导入的内容。
            outline_value = (
                imported_outline_value
                if merge_mode != "merge"
                else _merge_import_outline(previous_outline_raw, chapter_rows)
            )
            conn.execute(
                """
                UPDATE projects
                SET name = ?, subtitle = ?, current_word_count = ?, updated_at = ?, import_analysis = ?, outline = ?, target_chapters = ?
                WHERE id = ?
                """,
                (
                    resolved_name,
                    subtitle,
                    # 与 projects._update_project_word_count_on_conn 同一个不变量：
                    # 项目字数恒等于其章节字数之和（完全替换下与扫描的 total_words 相等）
                    sum(int(row["word_count"] or 0) for row in chapter_rows),
                    today,
                    json_dump(analysis),
                    outline_value,
                    max(len(chapter_rows), 1),
                    # 刻意不碰 `characters`：导入只更新候选（在 import_analysis 里），
                    # 作者在「人物」页勾选后才写档案。
                    resolved_project_id,
                ),
            )

        project_row = conn.execute(
            "SELECT * FROM projects WHERE id = ?",
            (resolved_project_id,),
        ).fetchone()

    # 记忆升级：导入落库后批量同步 FTS 索引（含正文/摘要，保证可检索）。
    # 失败降级（FTS 不可用 / 表缺失时静默跳过），绝不阻塞导入。
    try:
        memory_fts_dao.rebuild_project_fts(resolved_project_id)
    except Exception:  # noqa: BLE001
        pass

    return {
        "project": row_to_project(project_row),
        "chapters": [row_to_chapter(row) for row in chapter_rows],
        "importedChapters": len(chapters),
        "totalWords": total_words,
        "analysis": analysis,
        "mergeMode": merge_mode,
        # 合并按什么对齐：number（文件名里的章节号）/ index（退回导入顺序）
        "mergeKey": merge_key,
        "addedChapters": added,
        "updatedChapters": updated,
        "keptChapters": kept,
        "recycledChapters": recycled,
        # 顺带回收的「旧编号残留」（上一次按顺序编号留下的重复章节）
        "cleanedChapters": cleaned,
        "cleanedDetails": cleaned_details[:10],
        # 本次识别到的候选人物数。**这些人没有写进项目**：候选留给「人物」页勾选，
        # 作者选完才走 `POST /import/projects/{id}/characters/from-analysis` 落库。
        "charactersDetected": characters_detected,
    }


# 各分类里作为「条目身份」的字段：人物/地点用 name，时间线/伏笔用 text。
# 按值（而不是下标）过滤：入库时后端会重跑一次分析，按下标对齐过于脆弱。
_EXCLUSION_VALUE_KEYS = {
    "characters": "name",
    "locations": "name",
    "timeline": "text",
    "foreshadowing": "text",
}


def _apply_analysis_exclusions(scan_result: dict, raw: str) -> None:
    """按作者含弃的名单过滤自动识别的分析结果（人物/地点/时间线/伏笔）。

    四类识别全部基于词频/词性启发式，都会有误判（天气、地名、道具都会被当成人名/地点）。
    作者在导入前可以叉掉不像的条目；这里**只删不补**：把名单里的项移除，
    并把摘要里的计数重算一遍，绍不编造任何新条目。

    名单解析失败一律忽略（导入不能因为一个脏参数就失败）。
    """
    raw = (raw or "").strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return

    analysis = scan_result.get("analysis")
    if not isinstance(analysis, dict):
        return

    applied: dict[str, list[str]] = {}
    for key, value_key in _EXCLUSION_VALUE_KEYS.items():
        raw_names = payload.get(key)
        if not isinstance(raw_names, list):
            continue
        # 只接受字符串条目：数字/布尔不是条目名，不该被当成名单，也不该留下任何痕迹
        excluded = {
            item.strip() for item in raw_names if isinstance(item, str) and item.strip()
        }
        if not excluded:
            continue
        entries = analysis.get(key)
        if not isinstance(entries, list):
            continue
        analysis[key] = [
            entry
            for entry in entries
            if not (isinstance(entry, dict) and str(entry.get(value_key) or "").strip() in excluded)
        ]
        applied[key] = sorted(excluded)

    if not applied:
        return

    scan_result["excludedAnalysis"] = applied
    analysis["summary"] = build_analysis_summary(scan_result.get("chapters") or [], analysis)


@router.post("/markdown-folder")
async def api_scan_markdown_folder(request: Request, folderPath: str = Form(...)):
    from app.core.auth import require_authenticated_user
    require_authenticated_user(request)
    if not settings.debug:
        raise HTTPException(status_code=403, detail="生产环境仅支持上传文件。")
    folder = _resolve_existing_path(folderPath, "dir")
    result = await scan_markdown_folder(str(folder))
    return ApiResponse(data=result)


@router.post("/markdown-folder/ingest")
async def api_ingest_markdown_folder(
    request: Request,
    folderPath: str = Form(...),
    projectId: str = Form(""),
    projectName: str = Form(""),
    mergeMode: str = Form("replace"),
):
    if not settings.debug:
        raise HTTPException(status_code=403, detail="生产环境仅支持上传文件。")
    from app.core.auth import current_user_id, require_project_owner
    owner_id = current_user_id(request)
    if projectId.strip():
        require_project_owner(request, projectId.strip())
    folder = _resolve_existing_path(folderPath, "dir")
    scan_result = await scan_markdown_folder(str(folder))
    if not scan_result.get("success"):
        raise HTTPException(status_code=400, detail=scan_result.get("error", "Markdown 导入失败。"))

    data = _upsert_import_project(
        project_id=projectId.strip() or None,
        project_name=projectName.strip() or None,
        owner_id=owner_id,
        scan_result=scan_result,
        merge_mode=(mergeMode or "").strip().lower() or "replace",
    )
    return ApiResponse(data=data)


@router.post("/markdown-files")
async def api_scan_markdown_files(
    request: Request,
    files: list[UploadFile] = File(...),
    relativePaths: list[str] = Form([]),
):
    """上传 Markdown 章节文件并扫描（**生产环境唯一可用的 Markdown 导入入口**）。

    与 `/markdown-folder` 的区别只在于章节来源：这里由浏览器上传，服务端不读任意路径，
    因此不受 `DEBUG` 限制。扫描与摘要逻辑完全复用 `scan_markdown_folder`。
    """
    from app.core.auth import require_authenticated_user
    require_authenticated_user(request)

    root = _store_markdown_uploads(files, relativePaths)
    try:
        result = await scan_markdown_folder(str(root))
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Markdown 导入失败。"))
        # 临时目录名是随机 uuid，不能泄漏给用户当项目名
        result["folderName"] = _upload_folder_name(relativePaths)
        return ApiResponse(data=result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@router.post("/markdown-files/ingest")
async def api_ingest_markdown_files(
    request: Request,
    files: list[UploadFile] = File(...),
    relativePaths: list[str] = Form([]),
    projectId: str = Form(""),
    projectName: str = Form(""),
    excludedAnalysis: str = Form(""),
    mergeMode: str = Form("replace"),
):
    """上传 Markdown 章节文件并写入项目（`projectId` 为空时新建项目）。

    `excludedAnalysis`：作者在扫描结果里叉掉的误识别条目，按分类分组
    （JSON 对象：`{"characters": ["雨夜"], "locations": [...], ...}`）。
    `mergeMode`：写进**已有项目**时怎么处理既有章节 —— `replace`（全部移入回收站）
    或 `merge`（只替换章节序号相同的章节，其余保留）；新建项目时无意义。
    """
    from app.core.auth import current_user_id, require_project_owner
    owner_id = current_user_id(request)
    if projectId.strip():
        require_project_owner(request, projectId.strip())

    root = _store_markdown_uploads(files, relativePaths)
    try:
        scan_result = await scan_markdown_folder(str(root))
        if not scan_result.get("success"):
            raise HTTPException(status_code=400, detail=scan_result.get("error", "Markdown 导入失败。"))
        scan_result["folderName"] = projectName.strip() or _upload_folder_name(relativePaths)
        _apply_analysis_exclusions(scan_result, excludedAnalysis)
        data = _upsert_import_project(
            project_id=projectId.strip() or None,
            project_name=projectName.strip() or None,
            owner_id=owner_id,
            scan_result=scan_result,
            merge_mode=(mergeMode or "").strip().lower() or "replace",
        )
        return ApiResponse(data=data)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@router.post("/suggestion-docx")
async def api_import_suggestion_docx(
    request: Request,
    file: UploadFile | None = File(None),
    filePath: str = Form(""),
):
    from app.core.auth import require_authenticated_user
    require_authenticated_user(request)
    if file is not None:
        target = _store_uploaded_file(file)
    elif filePath.strip():
        if not settings.debug:
            raise HTTPException(status_code=403, detail="生产环境仅支持上传文件。")
        target = _resolve_existing_path(filePath.strip(), "file")
    else:
        raise HTTPException(status_code=400, detail="请上传文件或提供文件路径。")

    result = await parse_suggestion_docx(str(target))
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "建议书解析失败。"))
    return ApiResponse(data=result)


@router.post("/apply-suggestion")
async def api_apply_suggestion(payload: ApplySuggestionRequest, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, payload.projectId)
    try:
        result = await apply_suggestion_to_text(
            payload.suggestionId,
            payload.action,
            payload.target,
            payload.context,
        )
        return ApiResponse(data=result)
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/projects/{project_id}/characters/from-analysis")
async def api_add_analysis_characters(
    project_id: str,
    payload: ImportCharacterSelection,
    request: Request,
):
    """把导入扫描里**勾选**的候选人物加入项目档案（只增不改）。

    导入刻意不写 `projects.characters`：识别是词频/词性启发式，20 个候选里出现
    「毛巾」「安静」这类误判是常态，自动建档等于把垃圾数据变成作者要清理的负担。
    候选留在 `import_analysis` 里等作者挑，这个接口负责把挑选结果落库。

    - 只认 `import_analysis.characters` 里真实存在的名字（不在候选里的名字直接忽略；
      一个都没命中就 400，不做静默成功）；
    - 同名角色保留作者已有档案，只补项目里还没有的名字（复用 `_merge_analysis_characters`，
      因此「不编造性别/外貌/性格」这条规则只有一份实现）；
    - 返回更新后的整个项目，前端据此刷新人物列表与候选（已加入的不再出现在候选里）。
    """
    from app.core.auth import require_project_owner

    require_project_owner(request, project_id)
    wanted = {str(name).strip() for name in payload.names if str(name).strip()}
    if not wanted:
        raise HTTPException(status_code=400, detail="没有选中任何候选人物。")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT characters, import_analysis FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="项目未找到。")

        try:
            analysis = json.loads(row["import_analysis"] or "null") or {}
        except (TypeError, ValueError):
            analysis = {}
        if not isinstance(analysis, dict):
            analysis = {}

        candidates = [
            item
            for item in (analysis.get("characters") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip() in wanted
        ]
        if not candidates:
            raise HTTPException(status_code=400, detail="选中的名字不在这个项目的导入候选里。")

        characters_value, added = _merge_analysis_characters(
            row["characters"], {**analysis, "characters": candidates}
        )
        conn.execute(
            "UPDATE projects SET characters = ?, updated_at = ? WHERE id = ?",
            (characters_value, utc_today(), project_id),
        )
        project_row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()

    return ApiResponse(data={"project": row_to_project(project_row), "added": added})
