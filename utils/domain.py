def normalize_domain(value: str) -> str:
    domain = value.strip().lower()
    domain = domain.removeprefix("https://").removeprefix("http://")
    domain = domain.removeprefix("www.")
    return domain.split("/")[0]
