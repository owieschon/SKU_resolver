"""Catalog ingestion pathways — PDF text, Excel, and web/HTML into the decoder.

The row-extraction logic is pure (operates on lines / a matrix / html), so it
is tested with no file or network. The end of the file has a file-gated live
test against a real vendor PDF (skipped in CI / when the file or pypdf is
absent) — proof the whole pathway works on a catalog the system has never seen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from erp_harness import (
    decode_catalog,
    rows_from_catalog_lines,
    rows_from_html_tables,
    rows_from_worksheet,
)

# A few lines in the columnar shape `pdftotext -layout` produces for a real
# heavy-duty engine-parts catalog (own SKU, OEM cross-ref, description, app).
_PDF_LINES = [
    'WORLD AMERICAN #   REPLACES OEM #   DESCRIPTION            APPLICATION',
    'WA902-01-1002      142689           Accessory Drive Gear   Fits CUMMINS® N14',
    'WA902-01-1004      190397           Sleeve Wear            Fits CUMMINS® NT855',
    'WA902-02-1400      144714           Air Compressor Valve   Fits CUMMINS® NTC',
    'WA901-17-6601      4W5739           Connecting Rod Bearing Fits CATERPILLAR® 3300',
    'WA903-01-1021      8929310          Accessory Drive Gear   Fits DETROIT® 60 Series',
    '   World American®',                  # noise: no leading code
    '2',                                   # noise: page number
]


# --- PDF / text pathway ---------------------------------------------------------

def test_rows_from_catalog_lines_extracts_own_sku_and_description():
    rows = rows_from_catalog_lines(_PDF_LINES)
    assert len(rows) == 5                      # 5 product lines, noise dropped
    first = rows[0]
    assert first['sku'] == 'WA902-01-1002'     # catalog's OWN sku, not the OEM #
    assert first['description'] == 'Accessory Drive Gear'  # app text stripped
    # OEM cross-ref codes never become the SKU.
    assert all(r['sku'].startswith('WA') for r in rows)


def test_rows_capture_oem_and_fitment_and_section():
    lines = [
        'BEARINGS & BUSHINGS',
        'CUMMINS® DIRECT REPLACEMENT',
        'WORLD AMERICAN #   REPLACES OEM #   DESCRIPTION   APPLICATION',
        'WA902-17-6674      116391           Bushing       Fits CUMMINS® NTC',
    ]
    rows = rows_from_catalog_lines(lines)
    assert len(rows) == 1                       # the column-header line isn't a row
    r = rows[0]
    assert r['sku'] == 'WA902-17-6674'
    assert r['oem'] == '116391'                 # cross-ref captured (future displacement)
    assert 'CUMMINS' in r['fitment']            # fitment captured (future make/model)
    assert 'BEARINGS' in r['section'] and 'CUMMINS' in r['section']  # context captured


def test_extracted_rows_decode_into_a_family():
    rows = rows_from_catalog_lines(_PDF_LINES)
    report = decode_catalog(rows, sku_field='sku', description_field='description')
    wa = next(f for f in report.families if f.family_code == 'WA')
    assert wa.shape_mask == 'AN-N-N'           # WA902-01-1002 structure
    assert wa.member_count == 5


# --- Excel pathway --------------------------------------------------------------

def test_rows_from_worksheet_by_header_names():
    matrix = [
        ['Part Number', 'Description', 'List Price'],
        ['WA902-01-1002', 'Accessory Drive Gear', '120.00'],
        ['WA902-02-1400', 'Air Compressor Valve', '88.50'],
    ]
    rows = rows_from_worksheet(matrix)
    assert rows == [
        {'sku': 'WA902-01-1002', 'description': 'Accessory Drive Gear'},
        {'sku': 'WA902-02-1400', 'description': 'Air Compressor Valve'},
    ]


def test_rows_from_worksheet_falls_back_to_shape_when_headers_unhelpful():
    # No recognizable headers -> pick the most code-like column as SKU and the
    # longest-text column as description.
    matrix = [
        ['col_a', 'col_b'],
        ['Accessory Drive Gear', 'WA902-01-1002'],
        ['Air Compressor Valve', 'WA902-02-1400'],
    ]
    rows = rows_from_worksheet(matrix)
    assert rows[0]['sku'] == 'WA902-01-1002'
    assert rows[0]['description'] == 'Accessory Drive Gear'


def test_excel_source_roundtrip(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    from erp_harness import ExcelCatalogSource
    p = tmp_path / 'cat.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['SKU', 'Description'])
    ws.append(['WA902-01-1002', 'Accessory Drive Gear'])
    wb.save(p)
    rows = ExcelCatalogSource(p).rows()
    assert rows == [{'sku': 'WA902-01-1002', 'description': 'Accessory Drive Gear'}]


# --- Web / HTML pathway ---------------------------------------------------------

def test_http_html_catalog_source_with_injected_fetcher():
    from erp_harness import HttpHtmlCatalogSource
    html = ('<table><tr><th>Item #</th><th>Description</th></tr>'
            '<tr><td>WA902-01-1002</td><td>Accessory Drive Gear</td></tr></table>')
    captured = {}

    def fake_fetch(url):
        captured['url'] = url
        return html

    rows = HttpHtmlCatalogSource('https://vendor.example/catalog',
                                 fetcher=fake_fetch).rows()
    assert captured['url'] == 'https://vendor.example/catalog'
    assert rows == [{'sku': 'WA902-01-1002', 'description': 'Accessory Drive Gear'}]


def test_http_html_catalog_source_requires_a_fetcher():
    from erp_harness import HttpHtmlCatalogSource
    with pytest.raises(ValueError):
        HttpHtmlCatalogSource('https://x', fetcher=None)


def test_static_fetcher_builds_request_without_network():
    from erp_transport import static_fetcher
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['ua'] = req.headers.get('User-agent')

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'<table><tr><td>x</td></tr></table>'
        return _R()

    html = static_fetcher('https://vendor.example/p', urlopen=fake_urlopen)
    assert '<table>' in html
    assert captured['url'] == 'https://vendor.example/p' and captured['ua']


def test_rows_from_html_tables():
    html = """
    <html><body>
    <table>
      <tr><th>Item #</th><th>Description</th></tr>
      <tr><td>WA902-01-1002</td><td>Accessory Drive Gear</td></tr>
      <tr><td>WA902-02-1400</td><td>Air Compressor Valve</td></tr>
    </table>
    </body></html>
    """
    rows = rows_from_html_tables(html)
    assert {'sku': 'WA902-01-1002', 'description': 'Accessory Drive Gear'} in rows
    assert len(rows) == 2


# --- file-gated live test: a real vendor PDF ------------------------------------
# Point SKU_SAMPLE_PDF at any vendor parts-catalog PDF; defaults to a repo-local
# samples dir. Skipped when absent (e.g. in CI), so no machine path is baked in.
import os

_WA_PDF = Path(os.environ.get('SKU_SAMPLE_PDF',
                              Path(__file__).resolve().parent.parent
                              / 'data' / 'samples' / 'vendor_catalog.pdf'))


@pytest.mark.skipif(not _WA_PDF.exists(), reason='vendor PDF not present')
def test_live_real_pdf_decodes_to_wa_family():
    pytest.importorskip('pypdf')
    from erp_harness import PdfCatalogSource
    rows = PdfCatalogSource(_WA_PDF).rows()
    assert len(rows) > 300                      # a real, populated catalog
    report = decode_catalog(rows, sku_field='sku', description_field='description')
    wa = [f for f in report.families if f.family_code == 'WA']
    assert wa and wa[0].member_count > 200      # dominant family discovered
    assert report.structured_share > 0.4        # early result on an unseen catalog

    # Multi-field correlation: the engine-line segment auto-resolves from the
    # captured fitment/section evidence, lifting per-segment coverage sharply.
    enriched = decode_catalog(rows, sku_field='sku', description_field='description',
                              evidence_fields=['fitment', 'section', 'oem'])
    assert enriched.segment_coverage > report.segment_coverage + 0.2
    wa2 = next(f for f in enriched.families if f.family_code == 'WA')
    line_seg = next(r for r in wa2.segment_roles if r.role == 'classifier')
    brands = {t for _, t in line_seg.mapping}
    assert {'CUMMINS', 'CATERPILLAR', 'DETROIT'} <= brands
