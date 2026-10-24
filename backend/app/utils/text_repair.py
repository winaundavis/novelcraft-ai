import json
from typing import Any


MOJIBAKE_MARKERS = (
    "Ã",
    "Â",
    "â",
    "ä",
    "å",
    "æ",
    "ç",
    "è",
    "é",
    "ï",
    "ð",
    "œ",
    "š",
    "ž",
    "€",
    "™",
    "�",
)


def looks_mojibake(text: str) -> bool:
    if not text:
        return False
    if "\ufffd" in text:
        return True

    suspicious = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    if suspicious >= 2:
        return True

    latin_noise = sum(1 for char in text if 0x00C0 <= ord(char) <= 0x024F)
    chinese = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return latin_noise >= 3 and chinese == 0


def repair_text(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text

    best = text
    best_score = _score_text(text)
    candidate = text

    for _ in range(2):
        repaired = _decode_mojibake_once(candidate)
        if repaired is None or repaired == candidate:
            break
        candidate = repaired
        score = _score_text(repaired)
        if score > best_score:
            best = repaired
            best_score = score

    return best


def repair_data(value: Any) -> Any:
    if isinstance(value, str):
        return repair_text(value)
    if isinstance(value, list):
        return [repair_data(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_data(item) for key, item in value.items()}
    return value


def repair_json_blob(blob: str | None) -> str | None:
    if not blob:
        return blob

    candidates = [blob]
    repaired_blob = repair_text(blob)
    if repaired_blob != blob:
        candidates.append(repaired_blob)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        repaired = repair_data(parsed)
        serialized = json.dumps(repaired, ensure_ascii=False)
        if serialized != blob:
            return serialized
        return blob

    return repaired_blob if repaired_blob != blob else blob


def _decode_mojibake_once(text: str) -> str | None:
    if not looks_mojibake(text):
        return None

    for source_encoding in ("latin-1", "cp1252"):
        try:
            repaired = text.encode(source_encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired and repaired != text:
            return repaired
    return None


def _score_text(text: str) -> int:
    chinese = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    ascii_letters = sum(1 for char in text if char.isascii() and char.isalpha())
    suspicious = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    replacement = text.count("?")

    score = chinese * 4 + ascii_letters
    score -= suspicious * 6

    if replacement >= 6:
        score -= replacement * 2
    return score
