import httpx

from utils.config import Settings


def create_http_client(settings: Settings | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        follow_redirects=True,
    )
