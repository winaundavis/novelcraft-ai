import asyncio
import json
from pathlib import Path

from app.services.experiment_001_service import prepare_experiment_001


def _price():
    return {"currency": "CNY", "price_source": "test", "price_checked_at": "2026-08-24T00:00:00Z", "price_schedule": "peak", "cache_hit_input_per_m": 0.1, "cache_miss_input_per_m": 3.0, "output_per_m": 9.0}


def test_execution_uses_frozen_order_and_audits_six_calls(tmp_path):
    prepared = prepare_experiment_001(tmp_path, provider="DeepSeek", model="deepseek-v4-flash", price_config=_price())
    seen = []

    async def fake_call(request):
        seen.append((request["chapter_id"], request["variant"]))
        return {"text": f"第{len(seen)}段中文正文。", "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

    from app.services.experiment_001_execution_service import execute_experiment_001

    result = asyncio.run(execute_experiment_001(Path(prepared["artifacts"]["manifest"]).parent, call_model=fake_call))
    assert result["status"] == "COMPLETED_PENDING_HUMAN_REVIEW"
    assert seen == [("e001-c1", "baseline"), ("e001-c1", "candidate"), ("e001-c2", "baseline"), ("e001-c2", "candidate"), ("e001-c3", "baseline"), ("e001-c3", "candidate")]
    assert result["actual_calls"] == 6
    assert all(row["actual_total_tokens"] == 30 for row in result["calls"])
    assert Path(result["artifacts"]["results"]).is_file()


def test_execution_rejects_non_chinese_prose(tmp_path):
    prepared = prepare_experiment_001(tmp_path, provider="DeepSeek", model="deepseek-v4-flash", price_config=_price())

    async def fake_call(request):
        return {"text": "English-only response", "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

    from app.services.experiment_001_execution_service import execute_experiment_001

    artifact_root = Path(prepared["artifacts"]["manifest"]).parent
    result = asyncio.run(execute_experiment_001(artifact_root, call_model=fake_call))

    assert result["status"] == "INCOMPLETE"
    assert result["calls"][0]["status"] == "failed"
    assert "Chinese" in result["calls"][0]["error"]
    assert not list((artifact_root / "execution" / "outputs").glob("*.txt"))


def test_execution_rejects_latin_characters_in_chinese_prose(tmp_path):
    prepared = prepare_experiment_001(tmp_path, provider="DeepSeek", model="deepseek-v4-flash", price_config=_price())

    async def fake_call(request):
        return {"text": "林乔走进 Lin Qiao 的雨巷。", "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

    from app.services.experiment_001_execution_service import execute_experiment_001

    artifact_root = Path(prepared["artifacts"]["manifest"]).parent
    result = asyncio.run(execute_experiment_001(artifact_root, call_model=fake_call))

    assert result["status"] == "INCOMPLETE"
    assert result["calls"][0]["status"] == "failed"
    assert "Latin" in result["calls"][0]["error"]
