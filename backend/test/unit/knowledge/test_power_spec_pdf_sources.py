import pytest

from yuxi.knowledge.power_spec.pdf_sources import (
    is_power_spec_pdf_filename,
    power_spec_pdf_download_url,
    power_spec_pdf_filename,
)


@pytest.mark.parametrize(
    ("standard_no", "filename"),
    [
        ("DL/T 408-2023", "DL_T408-2023.pdf"),
        ("DL／T 584—2017", "DL_T584-2017.pdf"),
        ("GB/T 11022-2020", "GB_T11022-2020.pdf"),
        ("GB∕T 6451-2023", "GB_T6451-2023.pdf"),
    ],
)
def test_power_spec_pdf_filename_is_stable(standard_no, filename):
    assert power_spec_pdf_filename(standard_no) == filename
    assert power_spec_pdf_download_url(standard_no) == f"/api/knowledge/power-spec/pdfs/{filename}"


def test_power_spec_pdf_filename_rejects_empty_standard_no():
    assert power_spec_pdf_filename("  ") == ""
    assert power_spec_pdf_download_url("") is None


def test_power_spec_pdf_filename_validation_rejects_path_traversal():
    assert is_power_spec_pdf_filename("DL_T408-2023.pdf")
    assert not is_power_spec_pdf_filename("../DL_T408-2023.pdf")
    assert not is_power_spec_pdf_filename("DL_T408-2023.pdf/extra")
