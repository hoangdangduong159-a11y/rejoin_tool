"""Customizable per-game rule engine for rejoin / account rotation / notifications."""
import re

VALID_ACTIONS = ("stop", "rotate", "rejoin", "notify")
ACTION_PRIORITY = {"stop": 0, "rotate": 1, "rejoin": 2, "notify": 3}

_OP_RE = re.compile(r"^(.+?)\s*(>=|<=|!=|==|~=|>|<)\s*(.+)$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smh]?)$", re.IGNORECASE)


def _coerce_value(raw):
    s = str(raw).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _parse_duration_seconds(val):
    if isinstance(val, (int, float)):
        return float(val) * 60.0
    m = _DURATION_RE.match(str(val).strip())
    if not m:
        raise ValueError(f"Invalid duration: {val!r}")
    num = float(m.group(1))
    unit = (m.group(2) or "m").lower()
    if unit == "s":
        return num
    if unit == "h":
        return num * 3600.0
    return num * 60.0


def _compare(actual, op, expected):
    if actual is None:
        return False
    if op == "~=":
        return bool(re.search(str(expected), str(actual), re.IGNORECASE))

    if isinstance(expected, bool) or isinstance(actual, bool):
        a_bool = actual if isinstance(actual, bool) else str(actual).lower() in ("true", "1", "yes")
        e_bool = expected if isinstance(expected, bool) else str(expected).lower() in ("true", "1", "yes")
        if op == "==":
            return a_bool == e_bool
        if op == "!=":
            return a_bool != e_bool
        return False

    try:
        a_num = float(actual)
        e_num = float(expected)
        if op == ">=":
            return a_num >= e_num
        if op == "<=":
            return a_num <= e_num
        if op == ">":
            return a_num > e_num
        if op == "<":
            return a_num < e_num
        if op == "==":
            return a_num == e_num
        if op == "!=":
            return a_num != e_num
    except (TypeError, ValueError):
        a_str = str(actual)
        e_str = str(expected)
        if op == "==":
            return a_str == e_str
        if op == "!=":
            return a_str != e_str
        if op == ">=":
            return a_str >= e_str
        if op == "<=":
            return a_str <= e_str
        if op == ">":
            return a_str > e_str
        if op == "<":
            return a_str < e_str
    return False


def parse_rule_shorthand(expr, default_action="rotate"):
    """Parse shorthand strings like 'status:done', 'stat:level>=100 -> rotate', 'kicked -> rejoin'."""
    text = str(expr).strip()
    action = default_action
    if "->" in text:
        cond_part, act_part = text.split("->", 1)
        text = cond_part.strip()
        action = act_part.strip().lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unsupported rule action {action!r} in {expr!r}")

    low = text.lower()
    if low == "kicked":
        return {"when": {"kicked": True}, "action": action, "reason": text, "id": f"sh:{expr}"}

    if ":" not in text:
        raise ValueError(f"Invalid rule shorthand {expr!r}")

    prefix, rest = text.split(":", 1)
    prefix = prefix.strip().lower()
    rest = rest.strip()

    if prefix == "status":
        when = {"status": rest}
    elif prefix == "account_state":
        when = {"account_state": rest}
    elif prefix == "kicked":
        when = {"kicked": _coerce_value(rest)}
    elif prefix == "elapsed_account":
        when = {"elapsed_account": rest}
    elif prefix == "elapsed_session":
        when = {"elapsed_session": rest}
    elif prefix == "rejoin_fails":
        m = _OP_RE.match("_" + rest)
        if m:
            when = {"rejoin_fails": (m.group(2), _coerce_value(m.group(3)))}
        else:
            when = {"rejoin_fails": (">=", int(_coerce_value(rest)))}
    elif prefix == "stat":
        m = _OP_RE.match(rest)
        if m:
            key, op, val = m.group(1).strip(), m.group(2), _coerce_value(m.group(3))
            when = {"stat": {key: (op, val)}}
        else:
            when = {"stat": {rest: ("==", True)}}
    else:
        raise ValueError(f"Unknown rule condition prefix {prefix!r} in {expr!r}")

    return {"when": when, "action": action, "reason": text, "id": f"sh:{expr}"}


def _normalize_stat_cond(val):
    if isinstance(val, (tuple, list)) and len(val) == 2:
        return val[0], val[1]
    if isinstance(val, str):
        m = _OP_RE.match("_" + val)
        if m:
            return m.group(2), _coerce_value(m.group(3))
    return "==", val


def _eval_when(when, ctx):
    if not isinstance(when, dict) or not when:
        return False

    hb = ctx.get("hb") or {}
    stats = hb.get("stats") if isinstance(hb.get("stats"), dict) else {}

    if "all" in when:
        return all(_eval_when(sub, ctx) for sub in when["all"])
    if "any" in when:
        return any(_eval_when(sub, ctx) for sub in when["any"])

    for key, expected in when.items():
        if key == "status":
            if str(hb.get("status", "")).lower() != str(expected).lower():
                return False
        elif key == "account_state":
            if str(ctx.get("account_state", "")).lower() != str(expected).lower():
                return False
        elif key == "kicked":
            is_kicked = bool(hb.get("kicked")) or str(hb.get("status", "")).lower() == "kicked"
            if isinstance(expected, bool):
                if is_kicked != expected:
                    return False
            else:
                msg = str(hb.get("kickMsg", ""))
                if not is_kicked or str(expected).lower() not in msg.lower():
                    return False
        elif key == "elapsed_account":
            if float(ctx.get("elapsed_account_sec", 0.0)) < _parse_duration_seconds(expected):
                return False
        elif key == "elapsed_session":
            if float(ctx.get("elapsed_session_sec", 0.0)) < _parse_duration_seconds(expected):
                return False
        elif key == "rejoin_fails":
            op, num = _normalize_stat_cond(expected)
            if not _compare(ctx.get("consecutive_fails", 0), op, num):
                return False
        elif key == "stat":
            if not isinstance(expected, dict):
                return False
            for s_key, s_cond in expected.items():
                op, val = _normalize_stat_cond(s_cond)
                if not _compare(stats.get(s_key), op, val):
                    return False
        else:
            return False
    return True


def compile_rules(profile):
    """Compile profile['rotate_on'], profile['rules'], and max_rejoins_before_rotate."""
    compiled = []

    for item in profile.get("rotate_on") or []:
        if isinstance(item, str) and item.strip():
            compiled.append(parse_rule_shorthand(item, default_action="rotate"))

    for idx, item in enumerate(profile.get("rules") or []):
        if isinstance(item, str) and item.strip():
            compiled.append(parse_rule_shorthand(item, default_action="rotate"))
        elif isinstance(item, dict) and "when" in item:
            act = str(item.get("action", "rotate")).lower()
            if act not in VALID_ACTIONS:
                raise ValueError(f"Unsupported rule action {act!r}")
            compiled.append({
                "when": item["when"],
                "action": act,
                "reason": item.get("reason") or f"rule_{idx}",
                "id": item.get("id") or f"rule_{idx}_{act}",
            })

    max_fails = int(profile.get("max_rejoins_before_rotate") or 0)
    if max_fails > 0:
        compiled.append({
            "when": {"rejoin_fails": (">=", max_fails)},
            "action": "rotate",
            "reason": f"max_rejoins>={max_fails}",
            "id": f"__max_rejoins_{max_fails}",
        })

    # Built-in fallback so RejoinTool.requestRejoin() works in every profile
    has_rejoin_rule = any(
        r["when"] == {"status": "rejoin"} for r in compiled
    )
    if not has_rejoin_rule:
        compiled.append({
            "when": {"status": "rejoin"},
            "action": "rejoin",
            "reason": "lua_request_rejoin",
            "id": "__builtin_status_rejoin",
        })

    return compiled


def evaluate_rules(rules, ctx):
    """Evaluate compiled rules. Returns (action, reason) or (None, None)."""
    notified = ctx.setdefault("notified_rules", set())
    hb = ctx.get("hb") or {}
    hb_reason = str(hb.get("reason") or "").strip()

    matched = []
    for idx, rule in enumerate(rules):
        if _eval_when(rule["when"], ctx):
            act = rule["action"]
            rid = rule.get("id") or f"{idx}:{rule.get('reason')}"
            if act == "notify":
                if rid in notified:
                    continue
                notified.add(rid)
            reason = rule.get("reason") or act
            if hb_reason and rule["when"].get("status") in ("done", "rejoin"):
                reason = f"{reason}:{hb_reason}"
            elif hb.get("kickMsg") and "kicked" in rule["when"]:
                reason = f"{reason}:{str(hb['kickMsg'])[:80]}"
            matched.append((ACTION_PRIORITY.get(act, 99), idx, act, reason))

    if not matched:
        return None, None
    matched.sort(key=lambda x: (x[0], x[1]))
    _, _, act, reason = matched[0]
    return act, reason
