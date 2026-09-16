"""Server-side HTML-to-PDF rendering, plus embedding an extra file (e.g. a
ZATCA XML) inside the resulting PDF as a genuine file attachment -- the
same idea as a Factur-X/ZUGFeRD hybrid invoice (opens as one ordinary PDF,
with the embedded file visible in a PDF reader's attachments/paperclip
panel), though ZATCA itself doesn't require it.

Framework- and DB-context-free by design, matching database/zatca/engine.py's
convention -- callers (Flask routes) own rendering the Jinja template to an
HTML string and reading any file to embed from disk.

Rendering uses a headless Chromium browser (Playwright) rather than a
native-library renderer (WeasyPrint) because this app's invoice templates
are bilingual EN/AR: correct Arabic text shaping/RTL needs a real text
engine, and Chromium gives that along with full flexbox/grid support and
byte-for-byte the same rendering the browser's own Print already produces,
with no separate native runtime (Pango/GTK) to install on the host.
"""
from playwright.sync_api import sync_playwright


def html_to_pdf(html, wait_ms=300):
    """Render a complete, self-contained HTML document string to PDF bytes.
    A fresh Playwright + browser instance is launched per call rather than
    kept as a shared/global instance -- simpler and free of any threading/
    process-safety pitfalls, at the cost of ~1-2s startup overhead per
    call, which is acceptable for a manually-triggered "Download PDF"
    action.

    The print templates' only external resource is a Font Awesome CDN
    stylesheet (purely cosmetic icons, no invoice data). Unlike a real
    user's browser, this server-side render can't assume reliable public
    internet access, and waiting on `networkidle` for that one external
    request made every download hang for a full 30s and then fail
    whenever that CDN was slow or unreachable from this host -- so that
    request is aborted outright, and `wait_until='load'` is used instead
    of `networkidle`, since `load` doesn't depend on the network ever
    going quiet. `wait_ms` gives the page a brief moment to finish
    painting after that."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route('**cdnjs.cloudflare.com/**', lambda route: route.abort())
            page.set_content(html, wait_until='load', timeout=15000)
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            return page.pdf(format='A4', print_background=True)
        finally:
            browser.close()


def embed_file_in_pdf(pdf_bytes, filename, content_bytes):
    """Return a copy of `pdf_bytes` with `content_bytes` embedded inside it
    as a named file attachment. Raises on failure -- callers should decide
    whether to log and fall back to serving the plain PDF, since a PDF
    without its attachment is still a usable result but silently doing so
    inside this framework-free module would hide real bugs from the
    routes that need to know about them."""
    import io
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    writer.append(reader)
    writer.add_attachment(filename, content_bytes)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
