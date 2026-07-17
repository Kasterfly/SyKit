from sykit.utils import expose


@expose("ping")
def ping(session: dict):
    session["ping_count"] = session.get("ping_count", 0) + 1
    return {"pong": True, "count": session["ping_count"]}
