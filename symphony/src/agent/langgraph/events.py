from __future__ import annotations

import json
import sys
from typing import Any


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, sort_keys=True))
    sys.stdout.flush()
