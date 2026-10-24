"""异模型评审脚本：用非 DeepSeek 模型（kimi/glm/qwen/openai）对章节做独立质量评审。

前置条件：
  1. 在"设置 → LLM 配置"（或 .env）填入对应 provider 的 API key：
     - kimi：KIMI_API_KEY（Moonshot）
     - glm：GLM_API_KEY（智谱）
     - qwen：QWEN_API_KEY（通义千问）
  2. 后端已启动。

用法：
  python scripts/cross_model_review.py --provider kimi --project <projectId> [--limit 5] [--chapter <order>]
  python scripts/cross_model_review.py --provider glm  --project <projectId> --limit 10

输出：
  test_artifacts/cross-model-review-<provider>-<ts>.json
  - 每章 D1-D10 维度分 + 总分 + 一句话总评
  - 与启发式基线（test_artifacts/style-real-baseline-v1.json）对比 diff
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from app.db import get_conn
from app.llm.provider import LLMConfigurationError, LLMProvider, LLMResponseError

SYSTEM_PROMPT = (
    "你是资深网文编辑，负责独立评审小说章节质量。请从以下维度打分（每项 1-5 分）：\n"
    "D1 开篇建立（人物/冲突/阅读动机）\n"
    "D2 章节有效变化\n"
    "D3 对话推进\n"
    "D4 角色辨识度\n"
    "D5 冲突升级\n"
    "D6 情绪三阶段\n"
    "D7 场景具体化\n"
    "D8 节奏变化\n"
    "D9 章节钩子\n"
    "D10 AI痕迹（高分=自然，低分=模板感）\n"
    "只输出 JSON：{\"dimensions\": {\"D1\": 1-5, ...}, \"total\": 0-10, \"comment\": \"一句话总评\"}"
)

PROVIDER_LABEL = {
    "kimi": "Moonshot Kimi",
    "glm": "智谱 GLM",
    "qwen": "通义千问 Qwen",
    "openai": "OpenAI",
    "claude": "Claude",
    "deepseek": "DeepSeek",
}


async def review_chapter(provider: str, chapter: dict) -> dict:
    content = chapter["content"] or ""
    if len(content) > 5000:
        content = content[:5000] + "…（截断）"

    # Moonshot kimi-k3 负载高，偶发 429 / 空响应；重试最多 3 次，间隔递增
    last_error = ""
    for attempt in range(1, 4):
        try:
            llm = LLMProvider(provider=provider)
            reply = await llm.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"请评审以下章节：\n{content}"},
                ],
                # kimi-k3 的 max_tokens=400 会被截断为空（finish=length），需 ≥2000 才能输出完整 JSON
                max_tokens=2000,
            )
            if not reply or not reply.strip():
                last_error = f"空响应（第{attempt}次）"
                await asyncio.sleep(attempt * 3)
                continue
            m = re.search(r"\{.*\}", reply, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                data["provider"] = provider
                return data
            last_error = f"无法解析: {reply[:100]}"
            await asyncio.sleep(attempt * 3)
        except (LLMConfigurationError, LLMResponseError) as e:
            last_error = f"{type(e).__name__}: {str(e)[:150]}"
            await asyncio.sleep(attempt * 3)
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {str(e)[:150]}"
            await asyncio.sleep(attempt * 3)
    return {"error": last_error, "provider": provider}


async def main():
    parser = argparse.ArgumentParser(description="异模型评审")
    parser.add_argument("--provider", default="kimi", choices=list(PROVIDER_LABEL.keys()))
    parser.add_argument("--project", required=True, help="项目 ID")
    parser.add_argument("--limit", type=int, default=5, help="评审章节数")
    parser.add_argument("--chapter", type=int, default=0, help="指定单章（chapter_order），0=从开头取")
    args = parser.parse_args()

    conn = get_conn()
    if args.chapter > 0:
        rows = conn.execute(
            "SELECT chapter_order, title, content FROM chapters WHERE project_id=? AND chapter_order=?",
            (args.project, args.chapter),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT chapter_order, title, content FROM chapters WHERE project_id=? ORDER BY chapter_order LIMIT ?",
            (args.project, args.limit),
        ).fetchall()
    conn.close()

    if not rows:
        print(f"项目 {args.project} 无章节")
        return

    print(f"=== 异模型评审（{PROVIDER_LABEL[args.provider]}，{len(rows)} 章） ===")
    results = []
    for row in rows:
        res = await review_chapter(args.provider, dict(row))
        res["chapter_order"] = row["chapter_order"]
        res["title"] = row["title"]
        results.append(res)
        if "error" in res:
            print(f"  第{row['chapter_order']}章《{row['title']}》: ERROR {res['error'][:60]}")
        else:
            print(f"  第{row['chapter_order']}章《{row['title']}》: 总分={res.get('total')} 评论={res.get('comment','')[:40]}")

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(
        os.path.dirname(__file__), '..', 'test_artifacts',
        f"cross-model-review-{args.provider}-{ts}.json",
    )
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"provider": args.provider, "project_id": args.project, "reviews": results}, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    asyncio.run(main())
