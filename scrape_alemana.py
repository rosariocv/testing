import asyncio
import json
import re
import shutil
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://www.clinicaalemana.cl/aranceles/list/hospitalizacion"
OUT = Path("out")


def write_json(name: str, value) -> None:
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_clp(text: str):
    if not text:
        return None
    match = re.search(r"\$\s*([0-9][0-9.\s]*)", text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


async def extract_page_structure(page, page_number: int):
    return await page.evaluate(
        """
        (pageNumber) => {
          const norm = value => (value || '').replace(/\s+/g, ' ').trim();
          const unique = elements => [...new Set(elements)];
          const candidates = unique([
            ...document.querySelectorAll('[role="row"]'),
            ...document.querySelectorAll('[class*="_row_"]')
          ]);

          const rows = [];
          for (const row of candidates) {
            let cells = [...row.querySelectorAll(':scope > [role="columnheader"], :scope > [role="cell"], :scope > [class*="_cell_"]')];
            if (cells.length < 2) {
              cells = [...row.querySelectorAll('[role="columnheader"], [role="cell"], [class*="_cell_"]')];
            }
            cells = unique(cells).filter(cell => !cells.some(other => other !== cell && other.contains(cell)));
            const cellData = cells.map(cell => ({
              text: norm(cell.innerText || cell.textContent),
              role: cell.getAttribute('role'),
              class_name: typeof cell.className === 'string' ? cell.className : null,
              html: cell.outerHTML.slice(0, 5000),
              links: [...cell.querySelectorAll('a')].map(a => ({
                text: norm(a.innerText || a.textContent),
                aria_label: a.getAttribute('aria-label'),
                href: a.href || null
              }))
            }));
            if (cellData.length >= 2 && cellData.some(c => c.text)) {
              rows.push({
                page_number: pageNumber,
                row_text: norm(row.innerText || row.textContent),
                row_role: row.getAttribute('role'),
                row_class: typeof row.className === 'string' ? row.className : null,
                cells: cellData
              });
            }
          }

          const pagination = [...document.querySelectorAll('button,a,[role="button"]')]
            .map(el => ({
              text: norm(el.innerText || el.textContent),
              class_name: typeof el.className === 'string' ? el.className : null,
              aria_label: el.getAttribute('aria-label'),
              title: el.getAttribute('title'),
              disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
              parent_class: el.parentElement && typeof el.parentElement.className === 'string' ? el.parentElement.className : null
            }))
            .filter(x => /^\d+$/.test(x.text) || /siguiente|next|anterior|previous/i.test(`${x.text} ${x.aria_label || ''} ${x.title || ''}`));

          return {
            page_number: pageNumber,
            url: location.href,
            title: document.title,
            body_text: norm(document.body.innerText || document.body.textContent),
            rows,
            pagination,
            grids: [...document.querySelectorAll('[role="grid"],[role="table"],table')].map(el => ({
              role: el.getAttribute('role'),
              tag: el.tagName,
              class_name: typeof el.className === 'string' ? el.className : null,
              text: norm(el.innerText || el.textContent).slice(0, 3000)
            }))
          };
        }
        """,
        page_number,
    )


async def numeric_page_buttons(page):
    return await page.evaluate(
        """
        () => [...document.querySelectorAll('button')]
          .map(el => ({
            text: (el.innerText || el.textContent || '').trim(),
            class_name: typeof el.className === 'string' ? el.className : '',
            parent_class: el.parentElement && typeof el.parentElement.className === 'string' ? el.parentElement.className : '',
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
          }))
          .filter(x => /^\d+$/.test(x.text) && (/page/i.test(x.class_name) || /pag/i.test(x.parent_class)))
        """
    )


async def click_numeric_page(page, number: int):
    return await page.evaluate(
        """
        (number) => {
          const buttons = [...document.querySelectorAll('button')];
          const target = buttons.find(el => {
            const text = (el.innerText || el.textContent || '').trim();
            const cls = typeof el.className === 'string' ? el.className : '';
            const parentCls = el.parentElement && typeof el.parentElement.className === 'string' ? el.parentElement.className : '';
            return text === String(number) && (/page/i.test(cls) || /pag/i.test(parentCls));
          });
          if (!target) return false;
          target.click();
          return true;
        }
        """,
        number,
    )


async def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    network = []
    console = []
    failures = []
    response_tasks = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            locale="es-CL",
            timezone_id="America/Santiago",
            viewport={"width": 1440, "height": 1400},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def capture_response(response):
            request = response.request
            headers = await response.all_headers()
            rec = {
                "url": response.url,
                "status": response.status,
                "content_type": headers.get("content-type", ""),
                "method": request.method,
                "resource_type": request.resource_type,
                "post_data": request.post_data,
            }
            network.append(rec)
            if request.resource_type in {"xhr", "fetch"} or "json" in rec["content_type"].lower():
                try:
                    body = await response.body()
                    if len(body) <= 5_000_000:
                        rec["body_text"] = body.decode("utf-8", errors="replace")
                        rec["size_bytes"] = len(body)
                except Exception as exc:
                    rec["body_error"] = repr(exc)

        page.on("response", lambda response: response_tasks.append(asyncio.create_task(capture_response(response))))
        page.on("console", lambda msg: console.append({"type": msg.type, "text": msg.text}))
        page.on("requestfailed", lambda req: failures.append({"url": req.url, "failure": req.failure}))

        summary = {"requested_url": URL}
        try:
            response = await page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
            summary["main_status"] = response.status if response else None
            try:
                await page.wait_for_load_state("networkidle", timeout=90_000)
            except Exception as exc:
                summary["networkidle_error"] = repr(exc)
            await page.wait_for_timeout(8_000)

            # Click consent only when an actual consent button is visible.
            for pattern in [r"^aceptar$", r"^acepto$", r"^entendido$"]:
                try:
                    button = page.get_by_role("button", name=re.compile(pattern, re.I))
                    if await button.count() and await button.first.is_visible():
                        await button.first.click(timeout=5_000)
                        await page.wait_for_timeout(1_000)
                        break
                except Exception:
                    pass

            buttons = await numeric_page_buttons(page)
            page_numbers = sorted({int(x["text"]) for x in buttons}) or [1]
            summary["numeric_page_buttons"] = buttons
            summary["page_numbers"] = page_numbers

            pages = []
            previous_body = None
            for number in page_numbers:
                if number != page_numbers[0]:
                    clicked = await click_numeric_page(page, number)
                    if not clicked:
                        summary.setdefault("click_errors", []).append(f"Could not find numeric page button {number}")
                        continue
                    for _ in range(30):
                        await page.wait_for_timeout(400)
                        current_body = await page.locator("body").inner_text()
                        if current_body != previous_body:
                            break
                structure = await extract_page_structure(page, number)
                previous_body = await page.locator("body").inner_text()
                pages.append(structure)
                (OUT / f"page_{number:03d}.html").write_text(await page.content(), encoding="utf-8")
                (OUT / f"page_{number:03d}.txt").write_text(structure["body_text"], encoding="utf-8")

            raw_rows = [row for page_data in pages for row in page_data["rows"]]
            records = []
            for row in raw_rows:
                cells = [cell["text"] for cell in row["cells"]]
                if len(cells) < 6:
                    continue
                if any("Prestación" in value for value in cells[:2]):
                    continue
                if not any("$" in value for value in cells):
                    continue
                # Keep the six published columns in their displayed order.
                cells = cells[:6]
                records.append(
                    {
                        "page_number": row["page_number"],
                        "prestacion": cells[0],
                        "codigo_interno": cells[1],
                        "codigo_fonasa": cells[2],
                        "valor_paciente_particular_texto": cells[3],
                        "valor_paciente_particular_clp": parse_clp(cells[3]),
                        "valor_paciente_fonasa_texto": cells[4],
                        "valor_paciente_fonasa_clp": parse_clp(cells[4]),
                        "valor_paciente_isapre_texto": cells[5],
                        "valor_paciente_isapre_clp": parse_clp(cells[5]),
                        "fila_texto": row["row_text"],
                        "source_url": URL,
                    }
                )

            # De-duplicate exact published records while preserving page order.
            unique_records = []
            seen = set()
            for record in records:
                key = tuple(record.get(k) for k in [
                    "prestacion", "codigo_interno", "codigo_fonasa",
                    "valor_paciente_particular_texto", "valor_paciente_fonasa_texto",
                    "valor_paciente_isapre_texto"
                ])
                if key not in seen:
                    seen.add(key)
                    unique_records.append(record)

            summary.update(
                {
                    "title": await page.title(),
                    "final_url": page.url,
                    "pages_captured": len(pages),
                    "raw_role_rows": len(raw_rows),
                    "records_parsed": len(unique_records),
                }
            )
            write_json("pages_structured.json", pages)
            write_json("records.json", unique_records)
            write_json("raw_rows.json", raw_rows)
            await page.screenshot(path=str(OUT / "hospitalizacion_last_page.png"), full_page=True)
        except Exception as exc:
            summary["error"] = repr(exc)

        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        write_json("result.json", summary)
        write_json("network.json", network)
        write_json("console.json", console)
        write_json("request_failures.json", failures)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
