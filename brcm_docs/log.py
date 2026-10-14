from __future__ import annotations

import sys
import time


def log(*a) -> None:
    print(time.strftime("%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    if sec < 3600:
        return f"{sec // 60}분"
    if sec < 86400:
        return f"{sec // 3600}시간 {sec % 3600 // 60}분"
    return f"{sec // 86400}일 {sec % 86400 // 3600}시간"
