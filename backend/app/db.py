import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models.schemas import gen_id
from app.core.config import resolve_data_dir, resolve_database_path, settings
from app.utils.text_repair import repair_data, repair_json_blob, repair_text


DATA_DIR = resolve_data_dir(settings.data_dir)
DB_PATH = resolve_database_path(DATA_DIR, settings.novelcraft_db_path or os.getenv("NOVELCRAFT_DB_PATH", ""))


def utc_today() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d')


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return repair_data(json.loads(value))
    except json.JSONDecodeError:
        return default


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chapter_trash (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_chapter_id TEXT NOT NULL,
                title TEXT NOT NULL, original_location TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL, removed_at TEXT NOT NULL,
                word_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL,
                chapter_json TEXT NOT NULL, commit_json TEXT NOT NULL DEFAULT '{}',
                derived_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_chapter_trash_project
                ON chapter_trash(project_id, removed_at);
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_project ON chat_conversations(project_id, archived, updated_at);
            CREATE TABLE IF NOT EXISTS chat_messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages ON chat_messages(conversation_id, sequence);
            CREATE TABLE IF NOT EXISTS chat_requests (
                conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, assistant_id TEXT NOT NULL,
                status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(conversation_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'local-dev',
                name TEXT NOT NULL,
                subtitle TEXT NOT NULL DEFAULT '',
                genre TEXT NOT NULL DEFAULT 'xuanhuan',
                target_word_count INTEGER NOT NULL DEFAULT 300000,
                current_word_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'planning',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                style_config TEXT NOT NULL,
                outline TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                is_starred INTEGER NOT NULL DEFAULT 0,
                premise TEXT NOT NULL DEFAULT '',
                target_chapters INTEGER NOT NULL DEFAULT 30,
                characters TEXT NOT NULL DEFAULT '[]',
                locations TEXT NOT NULL DEFAULT '[]',
                world_rules TEXT NOT NULL DEFAULT '[]',
                import_analysis TEXT,
                inspiration_fragments TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS chapters (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                volume_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                chapter_order INTEGER NOT NULL DEFAULT 1,
                content TEXT NOT NULL DEFAULT '',
                word_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                summary TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS llm_configs (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                api_key TEXT NOT NULL,
                api_base TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                temperature REAL NOT NULL DEFAULT 0.8,
                max_tokens INTEGER NOT NULL DEFAULT 4096,
                -- 建议4 模型档位路由：角色级模型覆盖 JSON（如 {"check":{"model":"deepseek-flash"}}）
                role_overrides TEXT NOT NULL DEFAULT '{}',
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS novel_jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                requested_chapter_id TEXT,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                target_words INTEGER NOT NULL DEFAULT 0,
                total_chapters INTEGER NOT NULL DEFAULT 0,
                completed_chapters INTEGER NOT NULL DEFAULT 0,
                failed_chapters INTEGER NOT NULL DEFAULT 0,
                fallback_chapters INTEGER NOT NULL DEFAULT 0,
                generated_words INTEGER NOT NULL DEFAULT 0,
                current_chapter_id TEXT,
                current_chapter_title TEXT NOT NULL DEFAULT '',
                last_completed_chapter_id TEXT,
                last_completed_chapter_title TEXT NOT NULL DEFAULT '',
                llm_provider TEXT NOT NULL DEFAULT '',
                llm_model TEXT NOT NULL DEFAULT '',
                last_generation_source TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 1,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                -- M2 断点续传：任务 checkpoint / 恢复目标 / 失败章待人工处理 / 心跳
                checkpoint_type TEXT NOT NULL DEFAULT '',
                resume_target_json TEXT NOT NULL DEFAULT '',
                pending_manual_recovery INTEGER NOT NULL DEFAULT 0,
                heartbeat_at TEXT NOT NULL DEFAULT '',
                -- M3 自检闭环统计（随 checkpoint 持久化，前端任务中心展示质量指标）
                fact_check_count INTEGER NOT NULL DEFAULT 0,
                rewrite_count INTEGER NOT NULL DEFAULT 0,
                hook_miss_count INTEGER NOT NULL DEFAULT 0,
                quality_debt_count INTEGER NOT NULL DEFAULT 0,
                -- 第 3 档动态路由：置信度升级链复查次数（低置信度 → 强模型复查）
                escalation_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M1 记忆底座 · 事实账本：只记"已发生的不可逆事实"，project_id + content 唯一实现幂等
            -- M3 自检：is_conflict 标记"写作冲突"（事实核查多次失败且不可调和，供人工决策），
            --       该列记录不进入账本注入（build_completed_milestones 排除），与正常事实隔离
            CREATE TABLE IF NOT EXISTS novel_facts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                fact_type TEXT NOT NULL DEFAULT 'completed',
                content TEXT NOT NULL,
                chapter_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                is_conflict INTEGER NOT NULL DEFAULT 0,
                UNIQUE(project_id, content),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M3 自检闭环 · 钩子账本：短程跨章张力管理（与长程伏笔 P2 职责分离）
            -- hook_type：suspense(悬念)/twist(反转)/new_info(新信息)/crisis(危机升级)/emotional(情感悬置)/scene_switch(场景切换预告)
            -- status：planted(已埋设) → carried(已承接未解决) → resolved(已回收)；project_id + chapter_id 唯一（每章至多一个钩子）
            CREATE TABLE IF NOT EXISTS chapter_hooks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL DEFAULT '',
                hook_type TEXT NOT NULL DEFAULT 'suspense',
                status TEXT NOT NULL DEFAULT 'planted',
                target_chapter TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, chapter_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- P2 伏笔账本 · 长程跨卷张力管理（与短程钩子 chapter_hooks 职责分离）
            -- 状态机：planted(已埋设) → progressing(推进中) → resolved(已回收) / abandoned(已放弃)
            -- plant_chapter_id / target_chapter_id：埋设章 / 预计回收章（大纲章节 id）
            -- resolution：回收方式；events_json：每次推进事件记录（[{chapter_id, chapter_order, status, note, resolution, at}]）
            -- 职责：伏笔管"跨卷承诺/待兑现"，钩子管"短程跨章张力"，二者注入点分离不合并
            CREATE TABLE IF NOT EXISTS foreshadows (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                plant_chapter_id TEXT NOT NULL DEFAULT '',
                target_chapter_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planted',
                resolution TEXT NOT NULL DEFAULT '',
                events_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M1 记忆底座 · 卷级摘要：已完结卷懒压缩为 300-600 字摘要
            -- P1-1：id 不再全局唯一，改为 (project_id, id) 复合唯一，杜绝跨项目卷记录相互覆盖
            CREATE TABLE IF NOT EXISTS volumes (
                id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                start_chapter_id TEXT NOT NULL DEFAULT '',
                end_chapter_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                "order" INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M4 文风画像引擎 · 画像表：导入稿子 → 采样 → 结构化抽取四组规则（叙述/角色/语言/节奏
            -- + 反 AI 规则目录），JSON 落库；LLM 失败降级为纯规则统计（status='degraded'）
            CREATE TABLE IF NOT EXISTS style_profiles (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'import',
                status TEXT NOT NULL DEFAULT 'completed',
                narrative_rules TEXT NOT NULL DEFAULT '[]',
                character_rules TEXT NOT NULL DEFAULT '[]',
                language_rules TEXT NOT NULL DEFAULT '[]',
                rhythm_rules TEXT NOT NULL DEFAULT '[]',
                anti_ai_rules TEXT NOT NULL DEFAULT '[]',
                metrics TEXT NOT NULL DEFAULT '{}',
                dimensions TEXT NOT NULL DEFAULT '{}',
                strengths TEXT NOT NULL DEFAULT '[]',
                weaknesses TEXT NOT NULL DEFAULT '[]',
                transition_plan TEXT NOT NULL DEFAULT '{}',
                report TEXT NOT NULL DEFAULT '',
                sample_count INTEGER NOT NULL DEFAULT 0,
                sample_chars INTEGER NOT NULL DEFAULT 0,
                llm_used INTEGER NOT NULL DEFAULT 0,
                style_kind TEXT NOT NULL DEFAULT 'reference',
                identity TEXT NOT NULL DEFAULT '{}',
                mechanics TEXT NOT NULL DEFAULT '{}',
                boundaries TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 1,
                parent_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M4 文风画像引擎 · 画像-项目绑定：一个项目当前生效的画像（优先级/权重）
            CREATE TABLE IF NOT EXISTS style_bindings (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                profile_id TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'project',
                priority INTEGER NOT NULL DEFAULT 1,
                weight REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M4 文风画像引擎 · 渐进过渡进度：从现状画像 → 预设目标，快/中/慢三档，
            -- 进度随项目持久化，整书任务中断后从当前档位恢复（interval_chapters 每 N 章注入一次微调）
            CREATE TABLE IF NOT EXISTS style_transitions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                profile_id TEXT NOT NULL DEFAULT '',
                pace TEXT NOT NULL DEFAULT 'medium',
                progress REAL NOT NULL DEFAULT 0,
                interval_chapters INTEGER NOT NULL DEFAULT 6,
                total_steps INTEGER NOT NULL DEFAULT 10,
                plan TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- P2-3 参考书档案：作者收藏的「别人的作品」的写法档案（**用户级个人资产**，跳项目共享）。
            -- **刻意与 style_profiles 分表**：参考档案永不进 style_bindings、不参与本书文风
            -- 判定与过渡；两套东西共用同一套确定性抽取代码，但不共用表。
            -- project_id 只是「来源项目」标注，不是归属：删项目不应删素材（见下方 owner_id 迁移注释）。
            -- raw_text 存原文（供回看与重新拆解），列表查询会刻意排除该列。
            CREATE TABLE IF NOT EXISTS reference_works (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                owner_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                source_note TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL DEFAULT '',
                char_count INTEGER NOT NULL DEFAULT 0,
                stored_chars INTEGER NOT NULL DEFAULT 0,
                truncated INTEGER NOT NULL DEFAULT 0,
                sample_chars INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'completed',
                status_reason TEXT NOT NULL DEFAULT '',
                metrics TEXT NOT NULL DEFAULT '{}',
                dimensions TEXT NOT NULL DEFAULT '{}',
                narrative_rules TEXT NOT NULL DEFAULT '[]',
                character_rules TEXT NOT NULL DEFAULT '[]',
                language_rules TEXT NOT NULL DEFAULT '[]',
                rhythm_rules TEXT NOT NULL DEFAULT '[]',
                anti_ai_rules TEXT NOT NULL DEFAULT '[]',
                strengths TEXT NOT NULL DEFAULT '[]',
                weaknesses TEXT NOT NULL DEFAULT '[]',
                report TEXT NOT NULL DEFAULT '',
                llm_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- P2-3b 参考书档案 · 「正在参考哪些」绑定（一个项目可启用**多份**并带强度权重）。
            -- **刻意与 style_bindings 分开**：参考档案只影响表达参考，不参与本书文风判定与过渡。
            -- weight：优先级（1-10，越大越优先；预算紧张时先保留高权重的）。
            CREATE TABLE IF NOT EXISTS reference_bindings (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                reference_id TEXT NOT NULL DEFAULT '',
                weight INTEGER NOT NULL DEFAULT 6,
                scope TEXT NOT NULL DEFAULT 'project',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, reference_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 状态机：active(生效) → superseded(已被新版本取代) / conflict(冲突待决) / uncertain(低置信待核)
            -- 与 novel_facts.status 一致；version 每次变更 +1；valid_from/valid_to 支撑过期状态判定（验收 M3）
            CREATE TABLE IF NOT EXISTS memory_entities (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'character',
                name TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                confidence REAL NOT NULL DEFAULT 0.8,
                source_chapter_id TEXT NOT NULL DEFAULT '',
                first_seen_chapter_id TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, entity_type, name),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M1 记忆增强 · 实体关系账本：实体间的定向关系（亲缘/敌友/师承/拥有/位于…）
            -- 同样带 status/confidence/version/valid_from/valid_to，可追溯来源章节（验收 M5）
            CREATE TABLE IF NOT EXISTS memory_relations (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                entity_id TEXT NOT NULL DEFAULT '',
                relation_type TEXT NOT NULL DEFAULT 'related',
                target_entity_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                confidence REAL NOT NULL DEFAULT 0.8,
                source_chapter_id TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, entity_id, relation_type, target_entity_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- M1 记忆增强 · 记忆召回日志：每次生成实际注入了哪些记忆块（块名/来源 ID/字符数）
            -- 支撑验收 M1（召回率测量）与 M8（上下文长度实测）的自动化统计
            CREATE TABLE IF NOT EXISTS memory_recall_log (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL DEFAULT '',
                recall_type TEXT NOT NULL DEFAULT 'generation',
                block_key TEXT NOT NULL DEFAULT '',
                source_ids TEXT NOT NULL DEFAULT '[]',
                chars INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- P0-B：剧情状态与事实账本分离。一个项目只有一个当前状态快照；
            -- 列表字段使用 JSON，便于作者编辑且无需引入新基础设施。
            CREATE TABLE IF NOT EXISTS story_states (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL UNIQUE,
                current_volume_goal TEXT NOT NULL DEFAULT '',
                current_phase_goal TEXT NOT NULL DEFAULT '',
                protagonist_goal TEXT NOT NULL DEFAULT '',
                protagonist_obstacle TEXT NOT NULL DEFAULT '',
                main_conflict TEXT NOT NULL DEFAULT '',
                sub_conflicts_json TEXT NOT NULL DEFAULT '[]',
                relationship_changes_json TEXT NOT NULL DEFAULT '[]',
                current_mysteries_json TEXT NOT NULL DEFAULT '[]',
                active_foreshadows_json TEXT NOT NULL DEFAULT '[]',
                unresolved_questions_json TEXT NOT NULL DEFAULT '[]',
                promised_payoffs_json TEXT NOT NULL DEFAULT '[]',
                last_major_state_change TEXT NOT NULL DEFAULT '',
                next_stage_directions_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'seeded',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- P1-B：人物声线与人物事实分离，支持逐角色人工修订。
            CREATE TABLE IF NOT EXISTS character_voice_profiles (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                character_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                worldview_json TEXT NOT NULL DEFAULT '""',
                speech_length_json TEXT NOT NULL DEFAULT '""',
                sentence_pattern_json TEXT NOT NULL DEFAULT '""',
                favorite_expressions_json TEXT NOT NULL DEFAULT '[]',
                avoided_expressions_json TEXT NOT NULL DEFAULT '[]',
                emotional_expression_json TEXT NOT NULL DEFAULT '""',
                information_strategy_json TEXT NOT NULL DEFAULT '""',
                behavior_signature_json TEXT NOT NULL DEFAULT '""',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, character_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_character_voice_project ON character_voice_profiles(project_id);

            -- P1-C：写作经验必须经过重复证据与人工接受才进入生成提示词。
            CREATE TABLE IF NOT EXISTS writing_experiences (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                experience_type TEXT NOT NULL,
                observation TEXT NOT NULL,
                preferred_fix TEXT NOT NULL DEFAULT '',
                evidence_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'candidate',
                source_chapters_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, experience_type, observation),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_writing_experiences_project ON writing_experiences(project_id, status);

            -- P0-B：合同按项目/章节保存版本；正文生成只消费最新合同。
            CREATE TABLE IF NOT EXISTS chapter_contracts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'compiled',
                goal TEXT NOT NULL DEFAULT '',
                conflict TEXT NOT NULL DEFAULT '',
                state_change TEXT NOT NULL DEFAULT '',
                characters_json TEXT NOT NULL DEFAULT '[]',
                key_events_json TEXT NOT NULL DEFAULT '[]',
                must_payoff_json TEXT NOT NULL DEFAULT '[]',
                must_avoid_json TEXT NOT NULL DEFAULT '[]',
                ending_hook TEXT NOT NULL DEFAULT '',
                pacing TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, chapter_id, version),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_chapter_contracts_latest
                ON chapter_contracts(project_id, chapter_id, version DESC);

            -- P0-E：每章完成后的确定性状态转移；同一项目/章节只应用一次。
            CREATE TABLE IF NOT EXISTS story_state_transitions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                state_version_before INTEGER NOT NULL DEFAULT 1,
                state_version_after INTEGER NOT NULL DEFAULT 1,
                state_change TEXT NOT NULL DEFAULT '',
                knowledge_gained_json TEXT NOT NULL DEFAULT '[]',
                losses_json TEXT NOT NULL DEFAULT '[]',
                relationship_changes_json TEXT NOT NULL DEFAULT '[]',
                conflict_escalations_json TEXT NOT NULL DEFAULT '[]',
                foreshadow_updates_json TEXT NOT NULL DEFAULT '[]',
                unresolved_questions_json TEXT NOT NULL DEFAULT '[]',
                next_directions_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(project_id, chapter_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 文笔质量固化（Codex 方案第 8 步）· 局部重写审计痕迹
            -- 每次局部重写（开头/对话/行动/情绪/结尾）落一条审计记录：
            -- original（原文）/ rewritten（重写结果）/ reason（重写原因）/
            -- score_before / score_after（综合评分前后）。
            -- status：proposed（待人工确认，默认）/ applied（已应用）/ reverted（自动回退）
            -- 数据安全：重写服务不改 chapters.content，只在此表保存 rewritten 供人工确认，
            --          并提供「应用重写」接口（rewrite_service.apply_rewrite）。
            CREATE TABLE IF NOT EXISTS chapter_rewrites (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL DEFAULT '',
                rewrite_type TEXT NOT NULL DEFAULT 'opening',
                original TEXT NOT NULL DEFAULT '',
                rewritten TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                score_before REAL NOT NULL DEFAULT 0,
                score_after REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 项目大纲修改候选：对话智能体只能提出候选，用户显式确认后才应用。
            CREATE TABLE IF NOT EXISTS outline_candidates (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                base_outline_hash TEXT NOT NULL,
                base_outline_version INTEGER NOT NULL DEFAULT 1,
                candidate_outline_json TEXT NOT NULL,
                previous_outline_json TEXT,
                diff_json TEXT NOT NULL DEFAULT '{}',
                instruction TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                applied_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_outline_candidates_scope
                ON outline_candidates(project_id, user_id, status, created_at DESC);

            -- 阶段E（借鉴 Webnovel Writer「章节提交链」思想，自研实现，非复制 GPL 代码）：
            -- 章节提交记录：正文哈希 + 上下文哈希 + audit_id + 模型来源 + 完成时间。
            -- 用于「生成时上下文」与「落库正文」的可追溯审计（防记忆注入与正文不一致）。
            -- P1（Codex 复测）：不可变提交链——每次落库新增一条 version 递增的 commit
            -- 记录（不覆盖），保留完整版本历史与 parent 链接；正文保存与 commit 记录
            -- 在同一事务内（_persist_generated_chapter），commit 失败即回滚正文。
            CREATE TABLE IF NOT EXISTS chapter_commit_history (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                parent_commit_id TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                context_hash TEXT NOT NULL DEFAULT '',
                audit_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(project_id, chapter_id, version),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 章节提交最新快照（只读视图，兼容早期 API/测试：读 history 最新一行）
            CREATE TABLE IF NOT EXISTS chapter_commits (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '',
                context_hash TEXT NOT NULL DEFAULT '',
                audit_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(project_id, chapter_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 阶段E：记忆投影状态与单项重放。
            -- 每章的记忆同步拆分为 5 路投影：chapter_summary / facts / entities /
            -- volume / fts，各自记录 done / failed / skipped；
            -- 某项失败只重跑该投影（replay_memory_projection），避免全量重跑。
            CREATE TABLE IF NOT EXISTS memory_projections (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                projection_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, chapter_id, projection_type),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- Phase 6: deterministic, evidence-backed chapter digest.  This is
            -- intentionally separate from prose summaries and never calls an LLM.
            CREATE TABLE IF NOT EXISTS chapter_digests (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                commit_id TEXT NOT NULL DEFAULT '',
                digest_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, chapter_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 账号体系 · 邀请码（Q1=C + 留痕）：码即登录凭据，所以库里只存 sha256(码)。
            -- 明文只在生成时返回一次；code_hint 存后 4 位供人肉核对是哪一把钥匙。
            -- 状态机（PRD 3.1）：pending(可注册一次) → bound(只能当登录凭据) / disabled(彻底失效)。
            -- 一码最多绑一个账号：由 bind 时的条件更新保证（见 dao/invites.py）。
            CREATE TABLE IF NOT EXISTS invites (
                id TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL UNIQUE,
                code_hint TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                bound_user_id TEXT NOT NULL DEFAULT '',
                bound_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                max_uses INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_invites_status ON invites(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_invites_bound_user ON invites(bound_user_id);
            '''
        )
        # 账号体系 · 用户名唯一性**不区分大小写**（PRD 3.3）：追加函数式唯一索引，
        # 不改既有建表语句（users.username 的原 UNIQUE 仍按大小写敏感保留）。
        # 用 SQLite 的 lower() 而非 Python 的，保证索引与 dao/users.py 的查询口径一致。
        #
        # 老库升级保险：原 UNIQUE 是**大小写敏感**的，所以历史库可能同时存在
        # `test` 与 `TEST`。此时建索引会抛 IntegrityError 让 init_db() 直接失败
        # ——后端起不来、且没有任何可读提示。这里先体检并给出可操作的指引。
        duplicates = conn.execute(
            "SELECT lower(username) AS folded, GROUP_CONCAT(username, ' / ') AS names"
            " FROM users GROUP BY lower(username) HAVING COUNT(*) > 1"
        ).fetchall()
        if duplicates:
            detail = "；".join(f"{row['names']}" for row in duplicates)
            raise RuntimeError(
                "用户名唯一性升级失败：users 表里存在仅大小写不同的用户名（"
                + detail
                + "）。新规则要求用户名不区分大小写，请先人工处理（改名/删除其中一个），"
                "然后重启后端。"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower"
            " ON users(lower(username))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chapter_commits_project "
            "ON chapter_commits(project_id, chapter_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_projections_project "
            "ON memory_projections(project_id, chapter_id)"
        )
        # 文笔质量固化：chapter_rewrites 查询索引（幂等，重复执行无副作用）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chapter_rewrites_project "
            "ON chapter_rewrites(project_id, chapter_id)"
        )
        _create_fts_table(conn)
        _migrate_volumes_schema(conn)
        _ensure_column(
            conn, table='projects', column='owner_id', definition="TEXT NOT NULL DEFAULT 'local-dev'"
        )
        # 记录最近一次明确接受的协议版本和时间，不为旧账号伪造同意记录。
        # 默认空字符串；旧账号须在登录时明确同意当前版本后才补写。
        _ensure_column(
            conn, table='users', column='terms_version', definition="TEXT NOT NULL DEFAULT ''"
        )
        _ensure_column(
            conn, table='users', column='terms_accepted_at', definition="TEXT NOT NULL DEFAULT ''"
        )
        conn.execute("UPDATE projects SET owner_id = 'local-dev' WHERE owner_id IS NULL OR owner_id = ''")
        # P2-3b 参考书档案是**用户级个人资产**（跳项目共享）：补 owner_id 并对老行回填。
        # project_id 是保留的来源标识；个人素材不依赖来源项目继续存在。
        _ensure_column(
            conn, table='reference_works', column='owner_id', definition="TEXT NOT NULL DEFAULT ''"
        )
        # 素材库打标签（P3b）：逗号无关，存 JSON 数组
        _ensure_column(
            conn, table='reference_works', column='tags', definition="TEXT NOT NULL DEFAULT '[]'"
        )
        _migrate_reference_bindings_multi(conn)
        conn.execute(
            """
            UPDATE reference_works
            SET owner_id = COALESCE(
                (SELECT owner_id FROM projects WHERE projects.id = reference_works.project_id),
                'local-dev'
            )
            WHERE owner_id IS NULL OR owner_id = ''
            """
        )
        _migrate_reference_works_source_metadata(conn)
        _ensure_column(
            conn,
            table='projects',
            column='inspiration_fragments',
            definition="TEXT NOT NULL DEFAULT '[]'",
        )
        # 老项目兼容：确保 chapters.summary 列存在（旧库缺失时补列，不崩）
        _ensure_column(
            conn,
            table='chapters',
            column='summary',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        # 建议4 模型档位路由：老库 llm_configs 补 role_overrides 列（幂等，已存在则跳过）
        _ensure_column(
            conn,
            table='llm_configs',
            column='role_overrides',
            definition="TEXT NOT NULL DEFAULT '{}'",
        )
        # M2 断点续传：老库 novel_jobs 补 4 个 checkpoint 字段（幂等，已存在则跳过）
        _ensure_column(
            conn,
            table='novel_jobs',
            column='checkpoint_type',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            table='novel_jobs',
            column='resume_target_json',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            table='novel_jobs',
            column='pending_manual_recovery',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        # Beta invite lifecycle: old one-time invites remain valid with the
        # safe defaults (no expiry, max_uses=1, used_count=0).
        _ensure_column(
            conn,
            table='invites',
            column='expires_at',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            table='invites',
            column='max_uses',
            definition="INTEGER NOT NULL DEFAULT 1",
        )
        _ensure_column(
            conn,
            table='invites',
            column='used_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        conn.execute("UPDATE invites SET max_uses = 1 WHERE max_uses IS NULL OR max_uses < 1")
        conn.execute("UPDATE invites SET used_count = 0 WHERE used_count IS NULL OR used_count < 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invites_expiry ON invites(expires_at, status)")
        _ensure_column(
            conn,
            table='novel_jobs',
            column='heartbeat_at',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        # M3 自检闭环：老库 novel_facts 补 is_conflict 冲突标记列（幂等，已存在则跳过）
        _ensure_column(
            conn,
            table='novel_facts',
            column='is_conflict',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        # M3 自检闭环：novel_jobs 补自检统计列（事实核查次数/重写次数/钩子未命中/质量债）
        _ensure_column(
            conn,
            table='novel_jobs',
            column='fact_check_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn,
            table='novel_jobs',
            column='rewrite_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn,
            table='novel_jobs',
            column='hook_miss_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn,
            table='novel_jobs',
            column='quality_debt_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        # 第 3 档动态路由：novel_jobs 补置信度升级链复查次数列（低置信度 → 强模型复查）
        _ensure_column(
            conn,
            table='novel_jobs',
            column='escalation_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        # P2 伏笔账本：novel_jobs 补伏笔统计列（超期告警次数/成功追踪更新次数）
        _ensure_column(
            conn,
            table='novel_jobs',
            column='foreshadow_alert_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn,
            table='novel_jobs',
            column='foreshadow_updated_count',
            definition="INTEGER NOT NULL DEFAULT 0",
        )
        # ── M1 记忆升级 v1：novel_facts 结构化字段（幂等，重复运行 init_db 无错） ──
        # 冲突状态机：status 与 is_conflict 兼容（is_conflict=1 的历史记录 status 默认 conflict）
        _ensure_column(
            conn,
            table='novel_facts',
            column='status',
            definition="TEXT NOT NULL DEFAULT 'active'",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='version',
            definition="INTEGER NOT NULL DEFAULT 1",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='valid_from',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='valid_to',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='confidence',
            definition="REAL NOT NULL DEFAULT 1.0",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='entities_json',
            definition="TEXT NOT NULL DEFAULT '[]'",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='relations_json',
            definition="TEXT NOT NULL DEFAULT '[]'",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='source_chapter_id',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            conn,
            table='novel_facts',
            column='updated_at',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        # ── M1 记忆升级 v1：volumes 补 summary_hash（摘要对账漂移检测） ──
        _ensure_column(
            conn,
            table='volumes',
            column='summary_hash',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        # ── M1 记忆升级 v1：chapters 补 generation_source（降级章节记忆排除依据） ──
        _ensure_column(
            conn,
            table='chapters',
            column='generation_source',
            definition="TEXT NOT NULL DEFAULT ''",
        )
        # P0-D 文风引擎 2.0：类型化画像 + 身份/机制/边界三层（幂等）
        _ensure_column(conn, table='style_profiles', column='style_kind', definition="TEXT NOT NULL DEFAULT 'reference'")
        _ensure_column(conn, table='style_profiles', column='identity', definition="TEXT NOT NULL DEFAULT '{}' ")
        _ensure_column(conn, table='style_profiles', column='mechanics', definition="TEXT NOT NULL DEFAULT '{}' ")
        _ensure_column(conn, table='style_profiles', column='boundaries', definition="TEXT NOT NULL DEFAULT '{}' ")
        _ensure_column(conn, table='style_profiles', column='version', definition="INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, table='style_profiles', column='parent_id', definition="TEXT NOT NULL DEFAULT ''")
        # P1-A：章节合同保存确定性的章节模式与三层节奏上下文。
        _ensure_column(conn, table='chapter_contracts', column='chapter_mode', definition="TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, table='chapter_contracts', column='arc_context_json', definition="TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, table='chapter_contracts', column='story_state_version', definition="INTEGER NOT NULL DEFAULT 0")
        # Phase B：planned/observed/final 状态证据链与人工冲突确认。
        _ensure_column(conn, table='story_state_transitions', column='status', definition="TEXT NOT NULL DEFAULT 'applied'")
        _ensure_column(conn, table='story_state_transitions', column='planned_state_json', definition="TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(conn, table='story_state_transitions', column='observed_state_json', definition="TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(conn, table='story_state_transitions', column='final_state_json', definition="TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(conn, table='story_state_transitions', column='before_state_json', definition="TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(conn, table='story_state_transitions', column='conflicts_json', definition="TEXT NOT NULL DEFAULT '[]'" )
        # Phase 5: immutable accepted-chapter evidence.  These optional columns
        # extend the existing append-only history instead of introducing a second
        # commit store, so existing projects remain readable.
        _ensure_column(conn, table='chapter_commit_history', column='contract_version', definition="INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, table='chapter_commit_history', column='style_profile_version', definition="INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, table='chapter_commit_history', column='story_state_version_before', definition="INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, table='chapter_commit_history', column='content_snapshot', definition="TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, table='chapter_commit_history', column='observed_facts_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='state_changes_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='relationship_changes_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='knowledge_changes_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='conflict_changes_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='foreshadow_changes_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='payoffs_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='unresolved_questions_json', definition="TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, table='chapter_commit_history', column='next_direction', definition="TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, table='chapter_commit_history', column='created_by', definition="TEXT NOT NULL DEFAULT 'system'")
        _ensure_column(conn, table='chapter_commit_history', column='status', definition="TEXT NOT NULL DEFAULT 'accepted'")
        _ensure_column(conn, table='memory_projections', column='commit_id', definition="TEXT NOT NULL DEFAULT ''")
    cleanup_legacy_demo_data()
    repair_persisted_text()


def _create_fts_table(conn: sqlite3.Connection) -> None:
    """创建 chapter_fts FTS5 虚拟表（M1 增强：关键词检索）。

    - 内容 = 章节标题 + 正文 + 摘要 + 事实内容；project_id/chapter_id 标 UNINDEXED 供过滤。
    - 分词器 trigram（SQLite ≥3.34）：支持中文 3 字以上子串匹配，零外部依赖（不引入 jieba）。
    - 失败（SQLite 未编译 FTS5 / 版本过低）时静默跳过并打印告警，绝不阻塞 init_db ——
      检索功能降级为 LIKE 兜底（memory_fts.search_relevant 内处理）。
    """
    try:
        conn.execute(
            '''
            CREATE VIRTUAL TABLE IF NOT EXISTS chapter_fts USING fts5(
                project_id UNINDEXED,
                chapter_id UNINDEXED,
                title,
                content,
                summary,
                facts,
                tokenize = 'trigram'
            );
            '''
        )
    except Exception as exc:  # noqa: BLE001  FTS5 不可用时降级，不阻塞数据库初始化
        print(f"[警告] FTS5 不可用，关键词检索降级为 LIKE 兜底：{exc}")


def _migrate_volumes_schema(conn: sqlite3.Connection) -> None:
    """迁移 volumes 表：`id` 全局唯一 → `(project_id, id)` 复合唯一（P1-1 跨项目污染修复）。

    旧库 volumes.id 是 PRIMARY KEY（全局唯一），项目 B 对同 id 卷的 UPSERT 会整体覆盖
    项目 A 的记录。这里检测到旧结构时重建表：数据原样搬移（id 全局唯一 ⇒ (project_id, id)
    必然也唯一，可直接复制），并换用复合唯一约束。
    """
    indexes = conn.execute("PRAGMA index_list(volumes)").fetchall()
    has_composite_unique = False
    for index in indexes:
        if not index["unique"]:
            continue
        columns = [
            row["name"]
            for row in conn.execute(f"PRAGMA index_info({index['name']!r})").fetchall()
        ]
        if sorted(columns) == ["id", "project_id"]:
            has_composite_unique = True
            break
    if has_composite_unique:
        return

    conn.execute("ALTER TABLE volumes RENAME TO volumes_legacy")
    conn.execute(
        '''
        CREATE TABLE volumes (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            start_chapter_id TEXT NOT NULL DEFAULT '',
            end_chapter_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            "order" INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        '''
    )
    conn.execute(
        '''
        INSERT INTO volumes
            (id, project_id, start_chapter_id, end_chapter_id, title, summary, "order", created_at, updated_at)
        SELECT id, project_id, start_chapter_id, end_chapter_id, title, summary, "order", created_at, updated_at
        FROM volumes_legacy
        '''
    )
    conn.execute("DROP TABLE volumes_legacy")


def _ensure_column(conn: sqlite3.Connection, *, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    if any(row['name'] == column for row in rows):
        return
    conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def _migrate_reference_works_source_metadata(conn: sqlite3.Connection) -> None:
    """Remove the legacy source-project FK without changing archive data."""
    foreign_keys = conn.execute("PRAGMA foreign_key_list(reference_works)").fetchall()
    if not any(row["table"] == "projects" and row["from"] == "project_id" and row["to"] == "id" for row in foreign_keys):
        return
    original = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reference_works'").fetchone()[0]
    rebuilt, removed = re.subn(
        r',\s*FOREIGN\s+KEY\s*\(\s*(?:"project_id"|project_id)\s*\)\s*'
        r'REFERENCES\s+(?:"projects"|projects)\s*\(\s*(?:"id"|id)\s*\)\s*ON\s+DELETE\s+CASCADE',
        "", original, flags=re.IGNORECASE,
    )
    if removed != 1:
        raise RuntimeError("Unsupported legacy reference source constraint; no archive data changed")
    rebuilt = 'CREATE TABLE "reference_works_source_migration" (' + rebuilt.split("(", 1)[1]
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(reference_works)")]
    quoted_columns = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    objects = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='reference_works' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    conn.execute("SAVEPOINT reference_source_metadata")
    try:
        conn.execute(rebuilt)
        conn.execute(f'INSERT INTO "reference_works_source_migration" ({quoted_columns}) SELECT {quoted_columns} FROM reference_works')
        conn.execute("DROP TABLE reference_works")
        conn.execute('ALTER TABLE "reference_works_source_migration" RENAME TO reference_works')
        for row in objects:
            conn.execute(row["sql"])
        conn.execute("RELEASE SAVEPOINT reference_source_metadata")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT reference_source_metadata")
        conn.execute("RELEASE SAVEPOINT reference_source_metadata")
        raise


def _migrate_reference_bindings_multi(conn: sqlite3.Connection) -> None:
    """把「一个项目只能启用 1 份参考」的老绑定表升级为「可多份 + 强度」。

    SQLite 改不了 UNIQUE 约束，只能重建表：老行原样搬过去（强度给默认 6）。
    仅当老形状（UNIQUE(project_id) 且没有 weight 列）存在时执行；幂等。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reference_bindings'"
    ).fetchone()
    if not row or not row['sql']:
        return
    sql = str(row['sql'])
    if 'weight' in sql:
        return  # 已是新形状
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reference_bindings_multi (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            reference_id TEXT NOT NULL DEFAULT '',
            weight INTEGER NOT NULL DEFAULT 6,
            scope TEXT NOT NULL DEFAULT 'project',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, reference_id)
        );
        INSERT OR IGNORE INTO reference_bindings_multi
            (id, project_id, reference_id, weight, scope, created_at, updated_at)
        SELECT id, project_id, reference_id, 6, scope, created_at, updated_at
        FROM reference_bindings;
        DROP TABLE reference_bindings;
        ALTER TABLE reference_bindings_multi RENAME TO reference_bindings;
        """
    )


def cleanup_legacy_demo_data() -> None:
    demo_project_ids = ('demo-1', 'demo-2', 'demo-3')
    demo_chapter_ids = ('demo1-ch1', 'demo1-ch2')

    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM chapters WHERE id IN ({','.join('?' for _ in demo_chapter_ids)})",
            demo_chapter_ids,
        )
        conn.execute(
            f"DELETE FROM projects WHERE id IN ({','.join('?' for _ in demo_project_ids)})",
            demo_project_ids,
        )


def row_to_project(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'id': row['id'],
        'name': _sanitize_label(repair_text(row['name']), '未命名项目'),
        'subtitle': _sanitize_text(repair_text(row['subtitle']), ''),
        'genre': row['genre'],
        'targetWordCount': row['target_word_count'],
        'currentWordCount': row['current_word_count'],
        'status': row['status'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
        'volumes': [],
        'characters': _json_load(row['characters'], []),
        'locations': _json_load(row['locations'], []),
        'worldRules': _json_load(row['world_rules'], []),
        'inspirationFragments': _json_load(row['inspiration_fragments'], []),
        'styleConfig': _json_load(row['style_config'], {}),
        'outline': _json_load(row['outline'], None),
        'tags': _json_load(row['tags'], []),
        'isStarred': bool(row['is_starred']),
        'premise': _sanitize_text(repair_text(row['premise']), ''),
        'targetChapters': row['target_chapters'],
        'importAnalysis': _json_load(row['import_analysis'], None),
    }


def _strip_generated_chapter_scaffold(content: str, title: str) -> str:
    normalized = content.replace('\r\n', '\n').replace('\r', '\n')
    if not normalized.lstrip().startswith('#'):
        return content

    lines = normalized.split('\n')
    cursor = 0
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1

    if cursor >= len(lines):
        return content

    heading_line = lines[cursor].strip()
    if not heading_line.startswith('#'):
        return content

    heading_text = heading_line.lstrip('#').strip()
    if not heading_text or (title.strip() and heading_text != title.strip()):
        return content

    cursor += 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1

    if cursor >= len(lines):
        return content

    summary_line = lines[cursor].strip()
    if not summary_line.startswith('>'):
        return content

    cursor += 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1

    narrative = '\n'.join(lines[cursor:]).strip()
    if len(narrative) < 300:
        return content

    return narrative


def row_to_chapter(row: sqlite3.Row) -> dict[str, Any]:
    title = _sanitize_label(repair_text(row['title']), f"第{row['chapter_order'] or 1}章")
    content = _sanitize_text(repair_text(row['content']), '')
    return {
        'id': row['id'],
        'projectId': row['project_id'],
        'volumeId': row['volume_id'],
        'title': title,
        'order': row['chapter_order'],
        'content': _strip_generated_chapter_scaffold(content, title),
        'wordCount': row['word_count'],
        'status': row['status'],
        'summary': _sanitize_text(repair_text(row['summary']), ''),
        'notes': _sanitize_text(repair_text(row['notes']), ''),
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
    }


def row_to_llm_config(row: sqlite3.Row) -> dict[str, Any]:
    # 老库容错：role_overrides 列可能尚未迁移（init_db 的 _ensure_column 幂等补列）
    role_overrides = {}
    if 'role_overrides' in row.keys():
        role_overrides = _json_load(row['role_overrides'], {})
    from app.core.secret_box import decrypt_secret, is_protected

    stored_key = row['api_key']
    stored_overrides = role_overrides if isinstance(role_overrides, dict) else {}
    resolved_overrides: dict[str, Any] = {}
    for role, cfg in stored_overrides.items():
        if not isinstance(cfg, dict):
            resolved_overrides[role] = cfg
            continue
        item = dict(cfg)
        if item.get('api_key'):
            item['api_key'] = decrypt_secret(item['api_key'])
        resolved_overrides[role] = item
    return {
        'id': row['id'],
        'provider': row['provider'],
        'apiKey': decrypt_secret(stored_key),
        'apiKeyProtected': is_protected(stored_key),
        'apiBase': row['api_base'],
        'model': row['model'],
        'temperature': row['temperature'],
        'maxTokens': row['max_tokens'],
        'roleOverrides': resolved_overrides,
        'isActive': bool(row['is_active']),
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
    }


def get_active_llm_config() -> dict[str, Any] | None:
    from app.dao.llm_configs import get_active_config
    try:
        return get_active_config()
    except sqlite3.OperationalError as exc:
        # Uvicorn imports the provider before FastAPI lifespan runs init_db().
        # A fresh configured database has no runtime override yet; startup will
        # create the schema immediately afterward.
        if "no such table" in str(exc).lower() and "llm_configs" in str(exc).lower():
            return None
        raise


def default_style_config(preset: str) -> dict[str, Any]:
    presets = {
        'concise': {
            'name': 'Concise',
            'avgSentenceLength': 15,
            'dialogueRatio': 0.3,
            'descriptionDensity': 0.4,
            'paragraphLength': 'short',
            'tone': 'clean',
        },
        'poetic': {
            'name': 'Poetic',
            'avgSentenceLength': 25,
            'dialogueRatio': 0.25,
            'descriptionDensity': 0.7,
            'paragraphLength': 'medium',
            'tone': 'lyrical',
        },
        'humorous': {
            'name': 'Humorous',
            'avgSentenceLength': 18,
            'dialogueRatio': 0.5,
            'descriptionDensity': 0.3,
            'paragraphLength': 'short',
            'tone': 'playful',
        },
        'intense': {
            'name': 'Intense',
            'avgSentenceLength': 12,
            'dialogueRatio': 0.35,
            'descriptionDensity': 0.5,
            'paragraphLength': 'short',
            'tone': 'urgent',
        },
        'tender': {
            'name': 'Tender',
            'avgSentenceLength': 20,
            'dialogueRatio': 0.4,
            'descriptionDensity': 0.55,
            'paragraphLength': 'medium',
            'tone': 'gentle',
        },
        'epic': {
            'name': 'Epic',
            'avgSentenceLength': 28,
            'dialogueRatio': 0.2,
            'descriptionDensity': 0.65,
            'paragraphLength': 'long',
            'tone': 'grand',
        },
        'lightNovel': {
            'name': 'Light Novel',
            'avgSentenceLength': 16,
            'dialogueRatio': 0.55,
            'descriptionDensity': 0.35,
            'paragraphLength': 'short',
            'tone': 'bright',
        },
        'western': {
            'name': 'Western Fantasy',
            'avgSentenceLength': 22,
            'dialogueRatio': 0.3,
            'descriptionDensity': 0.6,
            'paragraphLength': 'medium',
            'tone': 'classical',
        },
        'emotional_resonance': {
            'name': 'Emotional Resonance',
            'avgSentenceLength': 21,
            'dialogueRatio': 0.42,
            'descriptionDensity': 0.5,
            'paragraphLength': 'medium',
            'tone': 'intimate',
        },
    }
    preset_data = presets.get(preset, presets['epic'])
    return {'id': gen_id(), **preset_data, 'customPrompt': ''}


def repair_persisted_text() -> None:
    with get_conn() as conn:
        _repair_table(
            conn,
            table='projects',
            key_column='id',
            text_columns=('name', 'subtitle', 'premise'),
            json_columns=('style_config', 'outline', 'tags', 'characters', 'locations', 'world_rules', 'import_analysis', 'inspiration_fragments'),
        )
        _repair_table(
            conn,
            table='chapters',
            key_column='id',
            text_columns=('title', 'content', 'summary', 'notes'),
            json_columns=(),
        )
        _repair_table(
            conn,
            table='novel_jobs',
            key_column='id',
            text_columns=(
                'message',
                'current_chapter_title',
                'last_completed_chapter_title',
                'llm_provider',
                'llm_model',
                'last_generation_source',
                'error',
            ),
            json_columns=(),
        )
        _repair_table(
            conn,
            table='novel_facts',
            key_column='id',
            text_columns=('content',),
            json_columns=(),
        )
        _repair_table(
            conn,
            table='foreshadows',
            key_column='id',
            text_columns=('name', 'description', 'resolution'),
            json_columns=('events_json',),
        )
        _repair_table(
            conn,
            table='volumes',
            key_column='id',
            text_columns=('title', 'summary'),
            json_columns=(),
        )
        _normalize_unusable_labels(conn)


def _repair_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    text_columns: tuple[str, ...],
    json_columns: tuple[str, ...],
) -> None:
    columns = ', '.join((key_column, *text_columns, *json_columns))
    rows = conn.execute(f'SELECT {columns} FROM {table}').fetchall()

    for row in rows:
        updates: dict[str, Any] = {}

        for column in text_columns:
            original = row[column]
            repaired = repair_text(original)
            if repaired != original:
                updates[column] = repaired

        for column in json_columns:
            original = row[column]
            repaired = repair_json_blob(original)
            if repaired != original:
                updates[column] = repaired

        if not updates:
            continue

        set_clause = ', '.join(f'{column} = ?' for column in updates)
        params = [updates[column] for column in updates]
        params.append(row[key_column])
        conn.execute(
            f'UPDATE {table} SET {set_clause} WHERE {key_column} = ?',
            tuple(params),
        )


def _sanitize_text(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = value.strip()
    if not cleaned:
        return fallback
    return fallback if _looks_unusable(cleaned) else cleaned


def _sanitize_label(value: Any, fallback: str) -> str:
    cleaned = _sanitize_text(value, fallback)
    return fallback if not cleaned else cleaned


def _looks_unusable(value: str) -> bool:
    if not value:
        return True
    if value.count('?') >= max(3, len(value) // 2):
        return True
    if all(char in '?？·. 0123456789章节第卷' for char in value) and value.count('?') >= 1:
        return True
    return False


def _normalize_unusable_labels(conn: sqlite3.Connection) -> None:
    project_rows = conn.execute('SELECT id, name FROM projects').fetchall()
    for row in project_rows:
        cleaned = _sanitize_label(repair_text(row['name']), '未命名项目')
        if cleaned != row['name']:
            conn.execute('UPDATE projects SET name = ? WHERE id = ?', (cleaned, row['id']))

    chapter_rows = conn.execute('SELECT id, title, chapter_order FROM chapters').fetchall()
    for row in chapter_rows:
        fallback = f"第{row['chapter_order'] or 1}章"
        cleaned = _sanitize_label(repair_text(row['title']), fallback)
        if cleaned != row['title']:
            conn.execute('UPDATE chapters SET title = ? WHERE id = ?', (cleaned, row['id']))
