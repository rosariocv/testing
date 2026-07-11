import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

URLS = {
    "examenes_por_especialidad": "https://www.clinicaalemana.cl/aranceles/list/examenes-por-especialidad",
    "imagenes_y_laboratorio": "https://www.clinicaalemana.cl/aranceles/list/imagenes-y-laboratorio",
}
OUT = Path("out")
OUT.mkdir(exist_ok=True)


def safe_name(url: str, i: int) -> str:
    parsed = urlparse(url)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", parsed.path.strip("/") or "root")
    return f"{i:04d}_{stem[:120]}"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="es-CL",
            timezone_id="America/Santiago",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        network = []
        response_tasks = []

        async def capture_response(response):
            idx = len(network) + 1
            headers = await response.all_headers()
            ct = headers.get("content-type", "")
            rec = {
                "index": idx,
                "url": response.url,
                "status": response.status,
                "content_type": ct,
                "request_method": response.request.method,
                "resource_type": response.request.resource_type,
            }
            network.append(rec)
            if any(x in ct.lower() for x in ["json", "javascript", "text/plain", "text/html"]):
                try:
                    body = await response.body()
                    if len(body) <= 8_000_000:
                        ext = ".json" if "json" in ct.lower() else ".js" if "javascript" in ct.lower() else ".txt"
                        name = safe_name(response.url, idx) + ext
                        (OUT / "responses").mkdir(exist_ok=True)
                        (OUT / "responses" / name).write_bytes(body)
                        rec["saved_as"] = f"responses/{name}"
                        rec["size_bytes"] = len(body)
                except Exception as exc:
                    rec["body_error"] = repr(exc)

        def on_response(response):
            response_tasks.append(asyncio.create_task(capture_response(response)))

        page.on("response", on_response)

        page_results = {}
        for key, url in URLS.items():
            result = {"url": url}
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                result["main_status"] = resp.status if resp else None
                try:
                    await page.wait_for_load_state("networkidle", timeout=90_000)
                except Exception as exc:
                    result["networkidle_error"] = repr(exc)
                await page.wait_for_timeout(15_000)
                result["title"] = await page.title()
                result["final_url"] = page.url
                result["body_text"] = await page.locator("body").inner_text(timeout=30_000)
                result["links"] = await page.locator("a").evaluate_all(
                    "els => els.map(e => ({text:(e.innerText||'').trim(), href:e.href})).filter(x=>x.text||x.href)"
                )
                result["buttons"] = await page.locator("button").evaluate_all(
                    "els => els.map(e => ({text:(e.innerText||'').trim(), aria:e.getAttribute('aria-label'), disabled:e.disabled}))"
                )
                result["scripts"] = await page.locator("script[src]").evaluate_all("els => els.map(e => e.src)")
                result["local_storage"] = await page.evaluate("Object.fromEntries(Object.entries(localStorage))")
                result["session_storage"] = await page.evaluate("Object.fromEntries(Object.entries(sessionStorage))")
                (OUT / f"{key}.html").write_text(await page.content(), encoding="utf-8")
                (OUT / f"{key}.txt").write_text(result["body_text"], encoding="utf-8")
                await page.screenshot(path=str(OUT / f"{key}.png"), full_page=True)
            except Exception as exc:
                result["error"] = repr(exc)
            page_results[key] = result

        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        (OUT / "page_results.json").write_text(json.dumps(page_results, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        await context.storage_state(path=str(OUT / "storage_state.json"))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
