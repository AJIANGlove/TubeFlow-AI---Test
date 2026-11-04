"""Read-only /capabilities probe for every configured H3 node.

Run inside the TubeFlow web container (has tubeflow_test on sys.path and
network access to the Kinestack control network):

    docker exec -i tubeflowai-test-web-1 python - < probe_capabilities.py

Prints a JSON object keyed by node_id. Tokens are read from Settings and used
only in the Authorization header; they are never printed.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from tubeflow_test.config import Settings


def main() -> None:
    settings = Settings()
    result = {}
    for pool in settings.h3_node_pools:
        for node_id, url, token, capacity in zip(
            pool.node_ids, pool.node_urls, pool.node_tokens, pool.node_capacities
        ):
            record = {
                "_probe": {
                    "node_id": node_id,
                    "profile": pool.profile,
                    "base_url": url,
                    "capacity": capacity,
                    "http_status": None,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "probe_error": None,
                },
                "response": None,
            }
            try:
                request = urllib.request.Request(
                    url.rstrip("/") + "/capabilities",
                    headers={"Authorization": f"Bearer {token}"},
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    record["_probe"]["http_status"] = response.status
                    body = response.read().decode("utf-8", "replace")
                    try:
                        record["response"] = json.loads(body)
                    except Exception:
                        record["response_raw"] = body[:4000]
            except urllib.error.HTTPError as exc:
                record["_probe"]["http_status"] = exc.code
                body = exc.read().decode("utf-8", "replace")
                try:
                    record["response"] = json.loads(body)
                except Exception:
                    record["response_raw"] = body[:4000]
                record["_probe"]["probe_error"] = f"HTTPError: {exc.code}"
            except Exception as exc:
                record["_probe"]["probe_error"] = f"{type(exc).__name__}: {exc}"
            result[node_id] = record
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
