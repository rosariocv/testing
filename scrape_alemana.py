import asyncio
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

URL = "https://www.clinicaalemana.cl/aranceles/list/hospitalizacion"
OUT = Path("out")


def safe_name(url: str, index: int, ext: str) -> str:
    parsed = urlparse(url)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (parsed.netloc + parsed.path).strip("/") or "root")
    return f"{index:04d}_{stem[:150]}{ext}"


async def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "responses").mkdir(parents=True)

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
        tasks = []

        async def capture_response(response):
            request = response.request
            headers = await response.all_headers()
            content_type = headers.get("content-type", "")
            record = {
                "index": len(network) + 1,
                "url": response.url,
                "status": response.status,
                "content_type": content_type,
                "resource_type": request.resource_type,
                "method": request.method,
                "post_data": request.post_data,
            }
            network.append(record)

            should_save = request.resource_type in {"xhr", "fetch", "document"}
            should_save = should_save or (
                request.resource_type == "script"
                and any(token in response.url.lower() for token in ["arancel", "main", "app", "bundle"])
            )
            if not should_save:
                return
            try:
                body = await response.body()
                if len(body) > 10_000_000:
                    record["body_skipped"] = f"{len(body)} bytes"
                    return
                ct = content_type.lower()
                if "json" in ct:
                    ext = ".json"
                elif "javascript" in ct or request.resource_type == "script":
                    ext = ".js"
                elif "html" in ct or request.resource_type == "document":
                    ext = ".html"
                else:
                    ext = ".txt"
                filename = safe_name(response.url, record["index"], ext)
                (OUT / "responses" / filename).write_bytes(body)
                record["saved_as"] = f"responses/{filename}"
                record["size_bytes"] = len(body)
            except Exception as exc:
                record["body_error"] = repr(exc)

        page.on("response", lambda response: tasks.append(asyncio.create_task(capture_response(response))))

        result = {"url": URL}
        try:
            response = await page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
            result["main_status"] = response.status if response else None
            try:
                await page.wait_for_load_state("networkidle", timeout=90_000)
            except Exception as exc:
                result["networkidle_error"] = repr(exc)
            await page.wait_for_timeout(20_000)

            result["title"] = await page.title()
            result["final_url"] = page.url
            result["body_text"] = await page.locator("body").inner_text(timeout=30_000)
            result["scripts"] = await page.locator("script[src]").evaluate_all("els => els.map(e => e.src)")
            result["links"] = await page.locator("a").evaluate_all(
                "els => els.map(e => ({text:(e.innerText||'').trim(), href:e.href, cls:e.className, aria:e.getAttribute('aria-label')}))"
            )
            result["buttons"] = await page.locator("button").evaluate_all(
                "els => els.map(e => ({text:(e.innerText||'').trim(), cls:e.className, aria:e.getAttribute('aria-label'), disabled:e.disabled, html:e.outerHTML.slice(0,1000)}))"
            )
            result["inputs"] = await page.locator("input,select").evaluate_all(
                "els => els.map(e => ({tag:e.tagName, type:e.type, name:e.name, value:e.value, placeholder:e.placeholder, cls:e.className}))"
            )
            result["tables"] = await page.locator("table").evaluate_all(
                "els => els.map(t => ({text:(t.innerText||'').trim(), html:t.outerHTML}))"
            )
            result["custom_elements"] = await page.locator("*").evaluate_all(
                "els => [...new Set(els.map(e=>e.tagName.toLowerCase()).filter(x=>x.includes('-')))].sort()"
            )
            result["candidate_nodes"] = await page.locator(
                "[class*='arancel'], [id*='arancel'], [class*='pagination'], [class*='page'], [aria-label*='página' i], [aria-label*='pagina' i]"
            ).evaluate_all(
                "els => els.map(e => ({tag:e.tagName, id:e.id, cls:e.className, text:(e.innerText||'').trim(), html:e.outerHTML.slice(0,4000)}))"
            )
            result["local_storage"] = await page.evaluate("Object.fromEntries(Object.entries(localStorage))")
            result["session_storage"] = await page.evaluate("Object.fromEntries(Object.entries(sessionStorage))")

            (OUT / "hospitalizacion.html").write_text(await page.content(), encoding="utf-8")
            (OUT / "hospitalizacion.txt").write_text(result["body_text"], encoding="utf-8")
        except Exception as exc:
            result["error"] = repr(exc)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        (OUT / "page_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
