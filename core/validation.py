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
    enrich_payload = {
        "only_verified_email": True,
        "data": {"linkedin_url": "https://www.linkedin.com/in/williamhgates"},
    }

    try:
        with httpx.Client(timeout=15) as client:
            r_search = client.post(
                PROSPEO_SEARCH_URL,
                headers={"X-KEY": api_key, "Content-Type": "application/json"},
                json=search_payload,
            )
            search_ok = r_search.status_code == 200 and not r_search.json().get("error")

            r_enrich = client.post(
                PROSPEO_ENRICH_URL,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json=enrich_payload,
            )
            enrich_ok = r_enrich.status_code == 200 and not r_enrich.json().get("error")

            if search_ok and enrich_ok:
                return True, "X-KEY (search) + X-API-KEY (enrich) working"
            if search_ok:
                return True, f"X-KEY working (search); enrich HTTP {r_enrich.status_code}"
            if enrich_ok:
                return True, f"X-API-KEY working (enrich); search HTTP {r_search.status_code}"
            return False, f"Auth failed: search={r_search.status_code}, enrich={r_enrich.status_code}"
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
