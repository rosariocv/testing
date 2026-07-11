import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

URL = "https://www.clinicaalemana.cl/aranceles/list/hospitalizacion"
OUT = Path("out")


def clean_output() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "responses").mkdir(parents=True, exist_ok=True)


def safe_name(url: str, index: int, suffix: str) -> str:
    parsed = urlparse(url)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", parsed.path.strip("/") or "root")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{index:04d}_{stem[:100]}_{digest}{suffix}"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def collect_dom(page):
    return await page.evaluate(
        """
        () => {
          const text = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
          const info = el => ({
            tag: el.tagName,
            text: text(el),
            id: el.id || null,
            class_name: typeof el.className === 'string' ? el.className : null,
            href: el.href || null,
            aria_label: el.getAttribute('aria-label'),
            role: el.getAttribute('role'),
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
            data: Object.fromEntries([...el.attributes]
              .filter(a => a.name.startsWith('data-'))
              .map(a => [a.name, a.value]))
          });

          const tables = [...document.querySelectorAll('table')].map((table, tableIndex) => ({
            table_index: tableIndex + 1,
            headers: [...table.querySelectorAll('thead th')].map(text),
            rows: [...table.querySelectorAll('tbody tr, tr')].map((tr, rowIndex) => ({
              row_index: rowIndex + 1,
              cells: [...tr.querySelectorAll('th,td')].map(text)
            })).filter(r => r.cells.some(Boolean))
          }));

          const controls = [...document.querySelectorAll('button,a,input,select,[role="button"]')]
            .map(info)
            .filter(x => x.text || x.aria_label || x.href || x.tag === 'INPUT' || x.tag === 'SELECT');

          const priceRe = /(?:\$\s*[0-9][0-9. ,]*|[0-9][0-9. ,]*\s*(?:CLP|pesos?))/i;
          const codeRe = /\b[0-9]{4,}(?:-[0-9A-Za-z]+)*\b/;
          const candidates = [...document.querySelectorAll('body *')].filter(el => {
            const t = text(el);
            if (!t || t.length > 800 || (!priceRe.test(t) && !codeRe.test(t))) return false;
            return ![...el.children].some(ch => {
              const c = text(ch);
              return c && (priceRe.test(c) || codeRe.test(c));
            });
          }).map(info);

          const globals = {};
          for (const key of ['__NEXT_DATA__', '__NUXT__', '__INITIAL_STATE__', '__APOLLO_STATE__']) {
            if (window[key] !== undefined) {
              try { globals[key] = JSON.parse(JSON.stringify(window[key])); }
              catch (e) { globals[key] = String(window[key]); }
            }
          }

          return {
            title: document.title,
            url: location.href,
            body_text: text(document.body),
            tables,
            controls,
            candidates,
            scripts: [...document.querySelectorAll('script[src]')].map(s => s.src),
            stylesheets: [...document.querySelectorAll('link[rel="stylesheet"]')].map(l => l.href),
            globals,
            html_language: document.documentElement.lang || null
          };
        }
        """
    )


async def visible_enabled(locator) -> bool:
    try:
        return await locator.count() > 0 and await locator.first.is_visible() and await locator.first.is_enabled()
    except Exception:
        return False


async def find_next_control(page):
    selectors = [
        'button[aria-label*="siguiente" i]',
        'a[aria-label*="siguiente" i]',
        'button[aria-label*="next" i]',
        'a[aria-label*="next" i]',
        'button[title*="siguiente" i]',
        'a[title*="siguiente" i]',
        '.pagination .next button',
        '.pagination .next a',
        'li.next button',
        'li.next a',
        '[class*="pagination"] [class*="next"]',
        '[class*="paginator"] [class*="next"]',
    ]
    for selector in selectors:
        loc = page.locator(selector)
        if await visible_enabled(loc):
            return loc.first, selector

    controls = page.locator('button,a,[role="button"]')
    count = await controls.count()
    for i in range(min(count, 400)):
        loc = controls.nth(i)
        try:
            if not await loc.is_visible() or not await loc.is_enabled():
                continue
            text = re.sub(r"\s+", " ", (await loc.inner_text()).strip()).lower()
            aria = ((await loc.get_attribute("aria-label")) or "").lower()
            title = ((await loc.get_attribute("title")) or "").lower()
            cls = ((await loc.get_attribute("class")) or "").lower()
            descriptor = " ".join([text, aria, title, cls])
            if re.search(r"\b(siguiente|next)\b", descriptor):
                return loc, f"text/attribute:{descriptor[:160]}"
            if text in {">", "›", "»", "→"} and re.search(r"pag|next|arrow", descriptor):
                return loc, f"symbol:{descriptor[:160]}"
        except Exception:
            continue
    return None, None


async def main() -> None:
    clean_output()
    network = []
    console_messages = []
    request_failures = []
    response_tasks = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="es-CL",
            timezone_id="America/Santiago",
            viewport={"width": 1440, "height": 1400},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def capture_response(response):
            request = response.request
            headers = await response.all_headers()
            content_type = headers.get("content-type", "")
            rec = {
                "index": len(network) + 1,
                "url": response.url,
                "status": response.status,
                "content_type": content_type,
                "method": request.method,
                "resource_type": request.resource_type,
                "post_data": request.post_data,
            }
            network.append(rec)
            should_save = (
                request.resource_type in {"xhr", "fetch", "document"}
                or "json" in content_type.lower()
                or "text/plain" in content_type.lower()
            )
            if should_save:
                try:
                    body = await response.body()
                    if len(body) <= 10_000_000:
                        suffix = ".json" if "json" in content_type.lower() else ".txt"
                        name = safe_name(response.url, rec["index"], suffix)
                        (OUT / "responses" / name).write_bytes(body)
                        rec["saved_as"] = f"responses/{name}"
                        rec["size_bytes"] = len(body)
                    else:
                        rec["body_skipped_size_bytes"] = len(body)
                except Exception as exc:
                    rec["body_error"] = repr(exc)

        def on_response(response):
            response_tasks.append(asyncio.create_task(capture_response(response)))

        page.on("response", on_response)
        page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text}))
        page.on(
            "requestfailed",
            lambda req: request_failures.append(
                {"url": req.url, "method": req.method, "resource_type": req.resource_type, "failure": req.failure}
            ),
        )

        result = {"requested_url": URL}
        try:
            response = await page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
            result["main_status"] = response.status if response else None
            try:
                await page.wait_for_load_state("networkidle", timeout=90_000)
            except Exception as exc:
                result["networkidle_error"] = repr(exc)
            await page.wait_for_timeout(12_000)

            # Dismiss common consent banners when present.
            for pattern in [r"aceptar", r"acepto", r"permitir", r"entendido"]:
                try:
                    button = page.get_by_role("button", name=re.compile(pattern, re.I))
                    if await visible_enabled(button):
                        await button.first.click(timeout=5_000)
                        await page.wait_for_timeout(1_000)
                        break
                except Exception:
                    pass

            pages = []
            seen_fingerprints = set()
            for page_number in range(1, 101):
                await page.wait_for_timeout(1_500)
                dom = await collect_dom(page)
                fingerprint = hashlib.sha1(dom["body_text"].encode("utf-8")).hexdigest()
                if fingerprint in seen_fingerprints:
                    result["pagination_stop"] = f"repeated fingerprint at page {page_number}"
                    break
                seen_fingerprints.add(fingerprint)
                dom["page_number"] = page_number
                dom["fingerprint"] = fingerprint
                pages.append(dom)
                (OUT / f"page_{page_number:03d}.html").write_text(await page.content(), encoding="utf-8")
                (OUT / f"page_{page_number:03d}.txt").write_text(dom["body_text"], encoding="utf-8")

                next_control, next_descriptor = await find_next_control(page)
                if next_control is None:
                    result["pagination_stop"] = f"no enabled next control after page {page_number}"
                    break
                result.setdefault("next_controls", []).append(
                    {"page_number": page_number, "descriptor": next_descriptor}
                )
                before_url = page.url
                before_fingerprint = fingerprint
                try:
                    await next_control.click(timeout=15_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=30_000)
                    except Exception:
                        pass
                    changed = False
                    for _ in range(20):
                        await page.wait_for_timeout(500)
                        current_text = await page.locator("body").inner_text()
                        current_fp = hashlib.sha1(current_text.encode("utf-8")).hexdigest()
                        if current_fp != before_fingerprint or page.url != before_url:
                            changed = True
                            break
                    if not changed:
                        result["pagination_stop"] = f"next control produced no content change after page {page_number}"
                        break
                except Exception as exc:
                    result["pagination_stop"] = f"next click failed after page {page_number}: {exc!r}"
                    break

            result["final_url"] = page.url
            result["title"] = await page.title()
            result["pages_captured"] = len(pages)
            result["page_fingerprints"] = [p["fingerprint"] for p in pages]
            write_json(OUT / "pages.json", pages)
            await page.screenshot(path=str(OUT / "hospitalizacion.png"), full_page=True)
            (OUT / "final.html").write_text(await page.content(), encoding="utf-8")
        except Exception as exc:
            result["error"] = repr(exc)

        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        write_json(OUT / "result.json", result)
        write_json(OUT / "network.json", network)
        write_json(OUT / "console.json", console_messages)
        write_json(OUT / "request_failures.json", request_failures)
        await context.storage_state(path=str(OUT / "storage_state.json"))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
