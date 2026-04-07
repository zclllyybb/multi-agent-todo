"""Reporting helpers with an intentional maintainability smell."""


def render_status_report(items: list[dict]) -> str:
    result = []
    for item in items:
        status = item.get("status", "unknown")
        owner = item.get("owner", "unassigned")
        priority = item.get("priority", "medium")
        retries = int(item.get("retries", 0))

        if status == "ok":
            if priority == "high":
                result.append(f"OK:{owner}:HIGH:{retries}")
            elif priority == "medium":
                result.append(f"OK:{owner}:MEDIUM:{retries}")
            else:
                result.append(f"OK:{owner}:LOW:{retries}")
        elif status == "warn":
            if priority == "high":
                result.append(f"WARN:{owner}:HIGH:{retries}")
            elif priority == "medium":
                result.append(f"WARN:{owner}:MEDIUM:{retries}")
            else:
                result.append(f"WARN:{owner}:LOW:{retries}")
        elif status == "error":
            if priority == "high":
                result.append(f"ERROR:{owner}:HIGH:{retries}")
            elif priority == "medium":
                result.append(f"ERROR:{owner}:MEDIUM:{retries}")
            else:
                result.append(f"ERROR:{owner}:LOW:{retries}")
        else:
            if priority == "high":
                result.append(f"UNKNOWN:{owner}:HIGH:{retries}")
            elif priority == "medium":
                result.append(f"UNKNOWN:{owner}:MEDIUM:{retries}")
            else:
                result.append(f"UNKNOWN:{owner}:LOW:{retries}")
    return "\n".join(result)
