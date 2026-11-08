"""Hackathon-only: a page at a secret URL that clears the bench for a live demo.

``GET /demo-reset/<token>`` serves a page with one button; it posts to the same URL,
which ends every lease and empties every queue. There is no login: knowing the URL is
the permission, so the token must be long and random. The routes answer 404 unless
``CLOUDBENCH_DEMO_RESET_TOKEN`` is set and matches, like any unknown URL.

Remove this module after the hackathon.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from cloudbench.gateway.errors import ApiError
from cloudbench.gateway.services import Services, get_services

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

ServicesDep = Annotated[Services, Depends(get_services)]

END_REASON = "cleared_for_demo"


def _check(token: str, services: Services) -> None:
    expected = services.settings.demo_reset_token
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise ApiError(404, "not_found", "Not found")


@router.get("/demo-reset/{token}")
async def demo_reset_page(token: str, services: ServicesDep) -> HTMLResponse:
    _check(token, services)
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@router.post("/demo-reset/{token}")
async def demo_reset(token: str, services: ServicesDep) -> dict[str, int]:
    _check(token, services)
    ended = await services.leases.end_all(END_REASON)
    log.warning("Demo reset: ended %s", ended or "nothing")
    return {
        "holders": ended.get("active", 0) + ended.get("offered", 0),
        "queued": ended.get("queued", 0),
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>CloudBench demo reset</title>
<style>
  :root { color-scheme: light dark; --bg: #eceef1; --surface: #fff; --text: #15191f;
          --muted: #5b6574; --border: #d2d7de; --danger: #c8372d; --danger-ink: #fff; }
  * { box-sizing: border-box; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0f1115; --surface: #171a21; --text: #eef1f6; --muted: #9aa3b2;
            --border: #2a2f3a; --danger: #ff6b6b; --danger-ink: #1b0606; }
  }
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center; background: var(--bg);
         color: var(--text); font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI",
         Roboto, sans-serif; }
  main { width: min(420px, 100% - 32px); padding: 24px; border-radius: 12px;
         background: var(--surface); border: 1px solid var(--border); }
  h1 { margin: 0 0 6px; font-size: 1.25rem; }
  p { margin: 0 0 20px; color: var(--muted); }
  button { width: 100%; min-height: 52px; border: 0; border-radius: 10px; cursor: pointer;
           font: inherit; font-weight: 650; background: var(--danger); color: var(--danger-ink); }
  button:disabled { opacity: 0.5; cursor: default; }
  output { display: block; margin-top: 16px; min-height: 1.45em; }
  a { color: inherit; }
</style>
</head>
<body>
<main>
  <h1>Clear the bench</h1>
  <p>Ends the current session and removes everyone from the queue, so you can join right
     after and get the bench.</p>
  <button id="reset" type="button">End session and clear queue</button>
  <output id="result" aria-live="polite"></output>
</main>
<script>
  const button = document.getElementById("reset");
  const result = document.getElementById("result");
  button.addEventListener("click", async () => {
    button.disabled = true;
    result.textContent = "Clearing…";
    try {
      const r = await fetch(location.pathname, { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const { holders, queued } = await r.json();
      result.innerHTML = "Done: ended " + holders + " session" + (holders === 1 ? "" : "s") +
        " and removed " + queued + " from the queue. <a href='/'>Open CloudBench</a> and join now.";
    } catch (e) {
      result.textContent = "Failed: " + e.message;
    } finally {
      button.disabled = false;
    }
  });
</script>
</body>
</html>
"""
