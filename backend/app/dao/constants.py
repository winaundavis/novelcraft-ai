"""数据库表名与列名常量，消除 SQL 字符串中的魔法值。"""

# ── 表名 ──────────────────────────────────────────────
TABLE_PROJECTS = "projects"
TABLE_CHAPTERS = "chapters"
TABLE_LLM_CONFIGS = "llm_configs"
TABLE_NOVEL_JOBS = "novel_jobs"
TABLE_NOVEL_FACTS = "novel_facts"
TABLE_VOLUMES = "volumes"
TABLE_CHAPTER_HOOKS = "chapter_hooks"  # M3 钩子账本（短程跨章张力）
TABLE_FORESHADOWS = "foreshadows"  # P2  伏笔账本（长程跨卷张力）
TABLE_REFERENCE_WORKS = "reference_works"  # P2-3 参考书档案（别人的作品，个人素材资产；与 style_profiles 分表）
TABLE_CHAPTER_FTS = "chapter_fts"  # M1 增强：SQLite FTS5 章节全文检索虚拟表
TABLE_MEMORY_ENTITIES = "memory_entities"  # M1 增强：结构化实体账本（人物/地点/物品/阵营/规则/时间线/伏笔）
TABLE_MEMORY_RELATIONS = "memory_relations"  # M1 增强：实体关系账本
TABLE_MEMORY_RECALL_LOG = "memory_recall_log"  # M1 增强：记忆召回日志（验收 M1/M8 测量）
TABLE_CHAPTER_REWRITES = "chapter_rewrites"  # 文笔质量固化：局部重写审计痕迹（验收 3.2 重写次数证据）
TABLE_STORY_STATES = "story_states"  # P0-B：项目当前剧情状态（事实账本之外）
TABLE_CHAPTER_CONTRACTS = "chapter_contracts"  # P0-B：章节生成合同版本
TABLE_STORY_STATE_TRANSITIONS = "story_state_transitions"  # P0-E：章节状态转移审计

# ── projects 表全部列（与 init_db() 中的建表顺序一致） ──
PROJECTS_COLUMNS = [
    "id", "name", "subtitle", "genre", "target_word_count",
    "current_word_count", "status", "created_at", "updated_at",
    "style_config", "outline", "tags", "is_starred", "premise",
    "target_chapters", "characters", "locations", "world_rules",
    "import_analysis", "inspiration_fragments",
]

# ── chapters 表全部列 ────────────────────────────────
CHAPTERS_COLUMNS = [
    "id", "project_id", "volume_id", "title", "chapter_order",
    "content", "word_count", "status", "summary", "notes",
    "created_at", "updated_at",
]

# ── llm_configs 表全部列（建议4 模型档位路由：role_overrides 角色级模型覆盖） ──
LLM_CONFIGS_COLUMNS = [
    "id", "provider", "api_key", "api_base", "model",
    "temperature", "max_tokens", "role_overrides",
    "is_active", "created_at", "updated_at",
]

# ── novel_jobs 表全部列 ───────────────────────────────
NOVEL_JOBS_COLUMNS = [
    "id", "project_id", "mode", "requested_chapter_id", "status",
    "message", "target_words", "total_chapters", "completed_chapters",
    "failed_chapters", "fallback_chapters", "generated_words",
    "current_chapter_id", "current_chapter_title",
    "last_completed_chapter_id", "last_completed_chapter_title",
    "llm_provider", "llm_model", "last_generation_source",
    "started_at", "updated_at", "finished_at", "error",
    "cancel_requested", "max_retries", "retry_count", "created_at",
    # M2 断点续传字段
    "checkpoint_type", "resume_target_json",
    "pending_manual_recovery", "heartbeat_at",
    # M3 自检闭环统计字段
    "fact_check_count", "rewrite_count",
    "hook_miss_count", "quality_debt_count",
    # 第 3 档动态路由：置信度升级链复查次数
    "escalation_count",
    # P2 伏笔账本统计字段
    "foreshadow_alert_count", "foreshadow_updated_count",
]

# ── novel_facts 表全部列（M1 事实账本；M3 加 is_conflict 写作冲突标记） ──
# 记忆升级 v1：扩展结构化字段（status/version/valid_from/valid_to/confidence/
# entities_json/relations_json/source_chapter_id/updated_at），与 is_conflict 兼容。
NOVEL_FACTS_COLUMNS = [
    "id", "project_id", "fact_type", "content", "chapter_id", "created_at",
    "is_conflict", "status", "version", "valid_from", "valid_to",
    "confidence", "entities_json", "relations_json", "source_chapter_id",
    "updated_at",
]

# ── chapter_hooks 表全部列（M3 钩子账本） ─────────────────
CHAPTER_HOOKS_COLUMNS = [
    "id", "project_id", "chapter_id", "hook_type",
    "status", "target_chapter", "description",
    "created_at", "updated_at",
]

# ── foreshadows 表全部列（P2 伏笔账本） ─────────────────
FORESHADOWS_COLUMNS = [
    "id", "project_id", "name", "description",
    "plant_chapter_id", "target_chapter_id", "status",
    "resolution", "events_json", "created_at", "updated_at",
]

# ── volumes 表全部列（M1 卷级摘要；order 为 SQLite 关键字，查询时需加引号） ──
VOLUMES_COLUMNS = [
    "id", "project_id", "start_chapter_id", "end_chapter_id",
    "title", "summary", "order", "created_at", "updated_at", "summary_hash",
]

# ── chapters 表列（记忆升级 v1：加 generation_source 记录降级来源） ──
CHAPTERS_GENERATION_SOURCE = "generation_source"

# ── memory_entities 表全部列（M1 增强：结构化实体账本） ──
MEMORY_ENTITIES_COLUMNS = [
    "id", "project_id", "entity_type", "name", "aliases_json",
    "description", "status", "confidence", "source_chapter_id",
    "first_seen_chapter_id", "version", "valid_from", "valid_to",
    "meta_json", "created_at", "updated_at",
]

# ── memory_relations 表全部列（M1 增强：实体关系账本） ──
MEMORY_RELATIONS_COLUMNS = [
    "id", "project_id", "entity_id", "relation_type", "target_entity_id",
    "status", "confidence", "source_chapter_id", "version",
    "valid_from", "valid_to", "note", "created_at", "updated_at",
]

# ── memory_recall_log 表全部列（M1 增强：记忆召回日志） ──
MEMORY_RECALL_LOG_COLUMNS = [
    "id", "project_id", "chapter_id", "recall_type", "block_key",
    "source_ids", "chars", "created_at",
]

# ── 常用列组合 ───────────────────────────────────────
PROJECTS_INSERT_COLUMNS = [
    "id", "name", "subtitle", "genre", "target_word_count",
    "current_word_count", "status", "created_at", "updated_at",
    "style_config", "outline", "tags", "is_starred", "premise",
    "target_chapters", "characters", "locations", "world_rules",
    "import_analysis", "inspiration_fragments",
]

CHAPTERS_INSERT_COLUMNS = [
    "id", "project_id", "volume_id", "title", "chapter_order",
    "content", "word_count", "status", "summary", "notes",
    "created_at", "updated_at",
]

LLM_CONFIGS_INSERT_COLUMNS = [
    "id", "provider", "api_key", "api_base", "model",
    "temperature", "max_tokens", "is_active", "created_at", "updated_at",
]

NOVEL_JOBS_INSERT_COLUMNS = NOVEL_JOBS_COLUMNS[:]
