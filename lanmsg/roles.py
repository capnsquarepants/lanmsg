"""Four exclusive roles; ownership is a protected property of an Admin."""

def can_change_role(actor, target, role):
    if target["owner"] or actor["id"] == target["id"]:
        return False
    if actor["owner"]:
        return True
    if actor["role"] == "admin":
        return target["role"] != "admin" and role != "admin"
    if actor["role"] == "moderator":
        return target["role"] in ("user", "banned") and role in ("user", "banned")
    return False


def identity(row):
    return {key: row[key] for key in ("id", "username", "display_name", "owner", "role")} | {
        "admin": int(row["role"] == "admin"), "moderator": int(row["role"] == "moderator")}
