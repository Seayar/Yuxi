"""电力规范 PDF 原文件的固定命名与下载地址。"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote


POWER_SPEC_PDF_BUCKET = "powerspec"
_SAFE_PDF_FILENAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,120}\.pdf$")


def power_spec_pdf_filename(standard_no: str) -> str:
    """把标准号转换为 powerspec bucket 中稳定的 PDF 文件名。"""

    normalized = unicodedata.normalize("NFKC", str(standard_no or "")).strip().upper()
    normalized = normalized.replace("—", "-").replace("–", "-").replace("－", "-").replace("／", "/")
    safe = re.sub(r"[^A-Z0-9._-]+", "_", normalized).strip("._-")
    safe = re.sub(r"_(?=\d)", "", safe)
    return f"{safe}.pdf" if safe else ""


def power_spec_pdf_download_url(standard_no: str) -> str | None:
    """返回标准号对应的同源鉴权 PDF 下载地址。"""

    filename = power_spec_pdf_filename(standard_no)
    return f"/api/knowledge/power-spec/pdfs/{quote(filename)}" if filename else None


def is_power_spec_pdf_filename(filename: str) -> bool:
    """校验下载路由中的 PDF 文件名，阻止路径注入。"""

    return bool(_SAFE_PDF_FILENAME_RE.fullmatch(str(filename or "")))
