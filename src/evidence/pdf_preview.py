"""Render a single manuscript page as PNG without a browser PDF plugin."""

from contextlib import closing
from io import BytesIO
from threading import RLock

_PDFIUM_LOCK = RLock()  # PDFium must not be called concurrently from Streamlit threads.


def render_pdf_page(pdf_bytes, page_number, max_dimension=1600):
    import pypdfium2 as pdfium

    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError("Choose a PDF page numbered from 1.")
    if not 256 <= max_dimension <= 2400:
        raise ValueError("PDF preview size must be between 256 and 2400 pixels.")
    with _PDFIUM_LOCK, closing(pdfium.PdfDocument(pdf_bytes)) as document:
        page_count = len(document)
        if page_number > page_count:
            raise ValueError("The selected page is outside this PDF.")
        with closing(document[page_number - 1]) as page:
            longest = max(page.get_size())
            if longest <= 0:
                raise ValueError("This PDF page has invalid dimensions.")
            with closing(page.render(scale=max_dimension / longest)) as bitmap:
                with closing(bitmap.to_pil()) as image:
                    output = BytesIO()
                    image.save(output, format="PNG")
                    return output.getvalue(), page_count
