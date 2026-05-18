"""Prompt-injection detection for untrusted user and document text.

This module is used as mandatory middleware around OCR/tool results and user
message intake. It is intentionally not exposed as an agent-callable tool:
compromised document text should not be able to decide whether it gets scanned.
"""

from __future__ import annotations

import re
from typing import Optional


INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?(previous\s+)?instruction", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous\s+)?context", re.IGNORECASE),
    re.compile(r"now\s+you\s+are", re.IGNORECASE),
    re.compile(r"system\s+override", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?above", re.IGNORECASE),
    re.compile(r"new\s+instruction", re.IGNORECASE),
    re.compile(r"you\s+must\s+(now\s+)?act\s+as", re.IGNORECASE),
    re.compile(r"<\s*system\s*>", re.IGNORECASE),
    re.compile(r"ADMIN\s*MODE", re.IGNORECASE),
    re.compile(r"update_draft_field|lookup_[a-z_]+|call\s+tool", re.IGNORECASE),
    re.compile(r"忽略.{0,12}(规则|指令|上下文|前面|以上)"),
    re.compile(r"绕过.{0,12}(政策|审批|规则|校验)"),
    re.compile(r"直接.{0,12}(提交|批准|审批|付款|打款)"),
    re.compile(r"(把|将).{0,16}金额.{0,8}(改|设|设置|填)"),
    re.compile(r"调用.{0,12}(工具|tool)"),
]


def scan_text(text: str) -> Optional[dict]:
    """Return an injection report if suspicious instruction patterns appear."""
    if not text:
        return None

    found: list[str] = []
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            found.append(pattern.pattern)

    if not found:
        return None

    return {"injection_detected": True, "patterns": found}
