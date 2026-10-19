"""Keep Chinese output in Traditional characters with Taiwan vocabulary, at zero token cost.

OpenCC's s2twp converts Simplified characters and Mainland vocabulary (软件/軟件→軟體,
網絡→網路, 信息→資訊, 視頻→影片…). It also over-converts a few forms that Taiwan media
commonly write differently; those are protected or restored (臺→台, 數據 stays 數據).
"""

from __future__ import annotations

from functools import lru_cache

# Applied after OpenCC: prefer the forms most Taiwan media actually use.
_AFTER = [("臺", "台")]
# Traditional terms OpenCC would rewrite that should stay as written
# (e.g. 項目 means "item" in Taiwan, but s2twp turns it into 專案 "project").
_PROTECT = ["數據中心", "數據集", "大數據", "數據", "項目", "公布", "宣布", "頒布", "發布", "分布"]


@lru_cache(maxsize=1)
def _converter():
    import opencc

    return opencc.OpenCC("s2twp")


# Simplified forms mapped before OpenCC (s2twp would turn 发布 into 釋出; news writing uses 發布).
_PRE = [("发布", "發布")]


def to_taiwan(text: str) -> str:
    if not text:
        return text
    for old, new in _PRE:
        text = text.replace(old, new)
    placeholders: dict[str, str] = {}
    protected = text
    for i, term in enumerate(_PROTECT):
        if term in protected:
            token = f"{i}"
            protected = protected.replace(term, token)
            placeholders[token] = term
    converted = _converter().convert(protected)
    for token, term in placeholders.items():
        converted = converted.replace(token, term)
    for old, new in _AFTER:
        converted = converted.replace(old, new)
    return converted


def changed_chars(before: str, after: str) -> int:
    """Rough count of characters the Taiwan guard had to fix (for the run report)."""
    if before == after:
        return 0
    return sum(1 for a, b in zip(before, after) if a != b) + abs(len(before) - len(after))
