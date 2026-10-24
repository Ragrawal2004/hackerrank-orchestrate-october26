"""Email -> user_id resolution and per-user identifier sets (SPEC §3). Exact matching only."""


class UnknownIdentity(LookupError):
    pass


class AmbiguousIdentity(LookupError):
    pass


def normalize_email(email):
    return email.strip().lower()


def resolve_email_detailed(conn, email):
    """Return (user_id, 'current'|'historical'). Current snapshot first, then audit history."""
    e = normalize_email(email)
    cur = {r[0] for r in conn.execute(
        "select user_id from identity where kind='email' and source='snapshot' and value=?", (e,))}
    if cur:
        if len(cur) > 1:
            raise AmbiguousIdentity(f"{e} is the current email of {sorted(cur)}")
        return next(iter(cur)), "current"
    hist = {r[0] for r in conn.execute(
        "select user_id from identity where kind='email' and value=?", (e,))}
    if not hist:
        raise UnknownIdentity(e)
    if len(hist) > 1:
        raise AmbiguousIdentity(f"{e} maps to {sorted(hist)}")
    return next(iter(hist)), "historical"


def resolve_email(conn, email):
    return resolve_email_detailed(conn, email)[0]


def identifiers(conn, user_id, as_of=None):
    """All identifiers of a user as {kind: set(values)}.

    as_of (ISO timestamp) drops rows whose valid_from is later than it.
    """
    sql = "select kind, value from identity where user_id=?"
    args = [user_id]
    if as_of is not None:
        sql += " and (valid_from is null or valid_from <= ?)"
        args.append(as_of)
    out = {k: set() for k in ("user_id", "name", "email", "phone", "email_hash")}
    for kind, value in conn.execute(sql, args):
        out[kind].add(value)
    return out
