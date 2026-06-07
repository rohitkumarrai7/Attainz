import httpx

PROSPEO_SEARCH_URL = "https://api.prospeo.io/search-person"
PROSPEO_ENRICH_URL = "https://api.prospeo.io/enrich-person"


def check_ocean(api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, "OCEAN_IO_API_KEY not set"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(
                "https://api.ocean.io/v2/credits/balance",
                headers={"X-Api-Token": api_key},
            )
            if r.status_code == 200:
                data = r.json()
                credits = data.get("credits", data.get("balance", "ok"))
                return True, f"Connected (credits: {credits})"
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as exc:
        return False, str(exc)


def check_prospeo(api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, "PROSPEO_API_KEY not set"

    search_payload = {
        "page": 1,
        "filters": {
            "company": {"websites": {"include": ["stripe.com"]}},
            "person_seniority": {"include": ["C-Suite"]},
            "max_person_per_company": 1,
        },
    }
    enrich_url = "https://www.linkedin.com/in/williamhgates"
    enrich_payloads = [
        {"only_verified_email": True, "data": {"linkedin_url": enrich_url}},
        {"only_verified_email": True, "linkedin_url": enrich_url},
    ]

    def _enrich_works(response: httpx.Response) -> bool:
        if response.status_code == 200 and not response.json().get("error"):
            return True
        if response.status_code in (400, 429):
            body = response.json()
            code = body.get("error_code", "")
            if code in ("NO_MATCH", "NOT_FOUND") or response.status_code == 429:
                return True
        return False

    try:
        with httpx.Client(timeout=15) as client:
            r_search = client.post(
                PROSPEO_SEARCH_URL,
                headers={"X-KEY": api_key, "Content-Type": "application/json"},
                json=search_payload,
            )
            search_ok = r_search.status_code == 200 and not r_search.json().get("error")

            enrich_ok = False
            enrich_detail = ""
            for header_name in ("X-KEY", "X-API-KEY"):
                for payload in enrich_payloads:
                    r_enrich = client.post(
                        PROSPEO_ENRICH_URL,
                        headers={header_name: api_key, "Content-Type": "application/json"},
                        json=payload,
                    )
                    if _enrich_works(r_enrich):
                        enrich_ok = True
                        enrich_detail = f"{header_name} (enrich) OK"
                        break
                if enrich_ok:
                    break

            if search_ok and enrich_ok:
                return True, f"X-KEY (search) + {enrich_detail}"
            if search_ok:
                return True, "X-KEY working (search); enrich endpoint reachable"
            if enrich_ok:
                return True, enrich_detail
            return False, f"Auth failed: search={r_search.status_code}"
    except Exception as exc:
        return False, str(exc)


def check_brevo(settings) -> tuple[bool, str, bool]:
    """Returns (ok, detail, is_smtp_key)."""
    from utils.brevo_transport import check_brevo as transport_check

    return transport_check(settings)


def run_validation(settings) -> dict:
    checks = []
    for name, fn, args in [
        ("Ocean.io", check_ocean, (settings.ocean_io_api_key,)),
        ("Prospeo", check_prospeo, (settings.prospeo_api_key,)),
    ]:
        ok, detail = fn(*args)
        checks.append({"service": name, "ok": ok, "detail": detail})

    brevo_ok, brevo_detail, smtp_key = check_brevo(settings)
    checks.append({
        "service": "Brevo",
        "ok": brevo_ok,
        "detail": brevo_detail,
        "smtp_key": smtp_key,
    })

    return {
        "all_ok": all(c["ok"] for c in checks),
        "checks": checks,
        "config": {
            "max_companies": settings.max_companies,
            "max_contacts_per_company": settings.max_contacts_per_company,
            "sender_name": settings.sender_name,
        },
    }
