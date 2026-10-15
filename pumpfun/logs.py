"""
Pulls the `Program data:` payloads a given program emitted out of a
transaction's log messages.

The runtime logs `Program <id> invoke [n]` / `Program <id> success|failed`
around every call, so a stack tells which program was running when each
`Program data:` line was written. Only payloads emitted while the target
program is on top of the stack are returned; this skips events from PumpSwap,
Pump Fees or a wrapper program that happen to share the transaction.
"""

import base64
import binascii
from typing import List

TRUNCATED_MARKER = "Log truncated"


def program_data(logs: List[str], program_id: str) -> List[bytes]:
    stack: List[str] = []
    payloads: List[bytes] = []
    for line in logs:
        if line.startswith("Program data: "):
            if stack and stack[-1] == program_id:
                try:
                    payloads.append(base64.b64decode(line[len("Program data: "):]))
                except binascii.Error:
                    pass
            continue
        parts = line.split(" ", 3)
        # "Program log: ...", "Program return: ..." etc. are not call frames.
        if len(parts) < 3 or parts[0] != "Program" or parts[1].endswith(":"):
            continue
        if parts[2] == "invoke":
            stack.append(parts[1])
        elif parts[2] == "success" or parts[2].startswith("failed"):
            if stack:
                stack.pop()
    return payloads


def is_truncated(logs: List[str]) -> bool:
    return any(TRUNCATED_MARKER in line for line in logs)
