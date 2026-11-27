"""Hardened document validation & text extraction for workspace attachments (PDF / Word / Excel / Text).

Security boundaries enforced at upload time:
  - Hard raw size cap (`MAX_ATTACHMENT_BYTES = 10 MiB`)
  - Magic-byte verification (never trusts client MIME header)
  - OOXML Zip Bomb protection (streaming real uncompressed byte counter, entry & ratio limits)
  - XXE / DTD rejection (`<!DOCTYPE` / `<!ENTITY` blocked + `defusedxml` loaded before `openpyxl`)
  - Parser circuit breakers (page / sheet / row / col / extracted-char caps)
  - Download MIME hardening (forces `application/octet-stream` for active web formats like HTML/SVG/XML/JS)
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import os
import posixpath
import re
from typing import List, Tuple
from urllib.parse import quote
import zipfile

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_FILES_PER_THREAD = 50
MAX_EXTRACTED_CHARS = 200_000

MAX_ZIP_ENTRIES = 2000
MAX_ZIP_RATIO = 100.0
MAX_ZIP_UNCOMPRESSED_BYTES = 32 * 1024 * 1024  # 32 MiB

MAX_PDF_PAGES = 100
MAX_XLSX_SHEETS = 20
MAX_XLSX_ROWS_PER_SHEET = 5000
MAX_XLSX_COLS_PER_ROW = 100

TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".log",
        ".sql",
        ".xml",
        ".html",
        ".htm",
        ".ini",
        ".toml",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".css",
        ".sh",
    }
)
OFFICE_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx"})
LEGACY_OFFICE_EXTENSIONS = frozenset({".doc", ".xls", ".ppt"})
SUPPORTED_UPLOAD_EXTENSIONS = TEXT_EXTENSIONS | OFFICE_EXTENSIONS

# Active browser-executable formats MUST always be served as `application/octet-stream`
_FORCE_OCTET_STREAM_EXTS = frozenset({".html", ".htm", ".svg", ".xml", ".xhtml", ".js", ".mjs", ".jsx"})

_DOWNLOAD_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".sql": "text/plain; charset=utf-8",
}

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\u4e00-\u9fff.()（） -]+")
_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class DocumentValidationError(ValueError):
    """Raised when an uploaded attachment fails validation, safety checks, or parsing."""

    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    ext: str
    content_type: str
    is_binary: bool
    extracted_text: str


def sanitize_upload_filename(raw_name: str) -> str:
    """Extract a clean single-segment filename safe for logical workspace paths."""
    base = posixpath.basename((raw_name or "").replace("\\", "/").strip())
    base = _FILENAME_SAFE_RE.sub("_", base).strip(". ")
    base = re.sub(r"\s+", " ", base)
    if not base or base in (".", ".."):
        raise DocumentValidationError("invalid_filename", "文件名无效，请重命名后重新上传")
    if len(base) > 120:
        stem, ext = os.path.splitext(base)
        base = stem[: max(1, 120 - len(ext))] + ext
    return base


def safe_download_content_type(logical_path: str, stored_content_type: str = "") -> str:
    """Return a hardened Content-Type for download responses (blocks Stored XSS)."""
    ext = os.path.splitext(logical_path.lower())[1]
    if ext in _FORCE_OCTET_STREAM_EXTS:
        return "application/octet-stream"
    if ext in _DOWNLOAD_CONTENT_TYPES:
        return _DOWNLOAD_CONTENT_TYPES[ext]
    if stored_content_type and stored_content_type.startswith("text/plain"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def build_content_disposition(logical_path: str) -> str:
    """RFC 5987 Content-Disposition header supporting UTF-8 / Chinese filenames safely."""
    filename = posixpath.basename(logical_path) or "download"
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "download"
    if "." not in ascii_fallback and "." in filename:
        ext = re.sub(r"[^A-Za-z0-9.]+", "", os.path.splitext(filename)[1])
        ascii_fallback = f"{ascii_fallback}{ext}"
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


def verify_safe_zip(raw: bytes, *, required_entry: str) -> None:
    """Validate OOXML archive against Zip Bombs, path anomalies, and XXE/DTD payloads."""
    if not raw.startswith(b"PK\x03\x04"):
        raise DocumentValidationError("invalid_magic", "文件头部不是有效的 Office OOXML 压缩包格式")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise DocumentValidationError("corrupt_file", "Office 文件已损坏或格式无效") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise DocumentValidationError("malicious_zip", "压缩包条目数量超过安全上限")
        names = {info.filename for info in infos}
        if required_entry not in names:
            raise DocumentValidationError(
                "invalid_magic", f"文件内容与后缀不匹配（缺少必要结构 {required_entry}）"
            )

        total_uncompressed = 0
        for info in infos:
            if info.is_dir():
                continue
            entry_uncompressed = 0
            xml_scan_buf = bytearray()
            is_xml = info.filename.lower().endswith((".xml", ".rels"))
            try:
                with zf.open(info, "r") as stream:
                    while True:
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        entry_uncompressed += len(chunk)
                        total_uncompressed += len(chunk)
                        if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                            raise DocumentValidationError(
                                "malicious_zip", "文件解压后体积超过安全上限（32 MiB），已拒绝处理"
                            )
                        if is_xml:
                            # Scan rolling window across chunk boundaries for <!DOCTYPE / <!ENTITY
                            xml_scan_buf.extend(chunk)
                            if _DTD_RE.search(xml_scan_buf):
                                raise DocumentValidationError(
                                    "malicious_xml", "检测到不安全的 XML DTD/实体声明，已拒绝解析"
                                )
                            if len(xml_scan_buf) > 32:
                                del xml_scan_buf[:-32]
            except DocumentValidationError:
                raise
            except Exception as exc:
                raise DocumentValidationError("corrupt_file", f"无法解压文档内容：{type(exc).__name__}") from exc

            if entry_uncompressed > 4096 and info.compress_size > 0:
                if (entry_uncompressed / info.compress_size) > MAX_ZIP_RATIO:
                    raise DocumentValidationError("malicious_zip", "文件压缩比异常过高，疑似压缩炸弹，已拒绝")


def _cap_extracted_text(text: str) -> str:
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text
    return (
        text[:MAX_EXTRACTED_CHARS]
        + f"\n\n[已截断：提取文本总长 {len(text)} 字符，超出单文件提取上限 {MAX_EXTRACTED_CHARS} 字符]"
    )


def _extract_pdf_text(raw: bytes) -> str:
    if not raw.startswith(b"%PDF-"):
        raise DocumentValidationError("invalid_magic", "文件头部不是有效的 PDF 格式（%PDF-）")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentValidationError(
            "missing_dependency",
            '当前环境未安装 PDF 解析依赖，请运行 pip install "agent-runtime[docs]"',
            status_code=501,
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise DocumentValidationError("encrypted_pdf", "暂不支持加密/带密码保护的 PDF 文件，请先解密后上传")
        total_pages = len(reader.pages)
        limit_pages = min(total_pages, MAX_PDF_PAGES)
        parts: List[str] = []
        for idx in range(limit_pages):
            page_text = (reader.pages[idx].extract_text() or "").strip()
            if page_text:
                parts.append(f"[Page {idx + 1}/{total_pages}]\n{page_text}")
        if total_pages > MAX_PDF_PAGES:
            parts.append(f"[已截断：共 {total_pages} 页，仅提取前 {MAX_PDF_PAGES} 页]")
    except DocumentValidationError:
        raise
    except Exception as exc:
        raise DocumentValidationError("corrupt_file", f"PDF 解析失败：{type(exc).__name__}") from exc

    if not parts:
        return "[该 PDF 未提取到可选中文本，可能为纯图片扫描件，当前纯文本模型暂不支持直接 OCR。]"
    return _cap_extracted_text("\n\n".join(parts))


def _extract_docx_text(raw: bytes) -> str:
    verify_safe_zip(raw, required_entry="word/document.xml")
    try:
        from docx import Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DocumentValidationError(
            "missing_dependency",
            '当前环境未安装 Word 解析依赖，请运行 pip install "agent-runtime[docs]"',
            status_code=501,
        ) from exc

    try:
        doc = Document(io.BytesIO(raw))
        blocks: List[str] = []
        body = doc.element.body
        for child in body.iterchildren():
            if isinstance(child, CT_P):
                text = Paragraph(child, doc).text.strip()
                if text:
                    blocks.append(text)
            elif isinstance(child, CT_Tbl):
                tbl = Table(child, doc)
                rows_md: List[str] = []
                for r_idx, row in enumerate(tbl.rows):
                    cells = [c.text.replace("\n", " ").replace("|", "\\|").strip() for c in row.cells]
                    if not any(cells):
                        continue
                    rows_md.append("| " + " | ".join(cells) + " |")
                    if r_idx == 0:
                        rows_md.append("| " + " | ".join("---" for _ in cells) + " |")
                if rows_md:
                    blocks.append("\n".join(rows_md))
    except DocumentValidationError:
        raise
    except Exception as exc:
        raise DocumentValidationError("corrupt_file", f"Word (.docx) 解析失败：{type(exc).__name__}") from exc

    if not blocks:
        return "[该 Word 文档未包含可提取文本内容。]"
    return _cap_extracted_text("\n\n".join(blocks))


def _extract_xlsx_text(raw: bytes) -> str:
    verify_safe_zip(raw, required_entry="xl/workbook.xml")
    try:
        # Ensure defusedxml is active so openpyxl guards XML parsing against XXE
        import defusedxml  # noqa: F401
        import openpyxl
    except ImportError as exc:
        raise DocumentValidationError(
            "missing_dependency",
            '当前环境未安装 Excel 解析依赖，请运行 pip install "agent-runtime[docs]"',
            status_code=501,
        ) from exc

    wb = None
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet_names = wb.sheetnames
        sections: List[str] = []
        for s_idx, name in enumerate(sheet_names[:MAX_XLSX_SHEETS]):
            ws = wb[name]
            lines: List[str] = []
            non_empty_rows = 0
            truncated_rows = False
            for row in ws.iter_rows(values_only=True):
                sliced = list(row[:MAX_XLSX_COLS_PER_ROW])
                while sliced and sliced[-1] is None:
                    sliced.pop()
                if not sliced or all(v is None or str(v).strip() == "" for v in sliced):
                    continue
                non_empty_rows += 1
                if non_empty_rows > MAX_XLSX_ROWS_PER_SHEET:
                    truncated_rows = True
                    break
                formatted = [
                    "" if v is None else str(v).replace("\t", " ").replace("\n", " ").strip()
                    for v in sliced
                ]
                lines.append("\t".join(formatted))
            header = f"[Sheet: {name} (rows={len(lines)})]"
            if truncated_rows:
                lines.append(f"...[已截断：仅提取前 {MAX_XLSX_ROWS_PER_SHEET} 非空行]")
            sections.append(header + ("\n" + "\n".join(lines) if lines else "\n（空工作表）"))
        if len(sheet_names) > MAX_XLSX_SHEETS:
            sections.append(f"[已截断：共 {len(sheet_names)} 个工作表，仅提取前 {MAX_XLSX_SHEETS} 个]")
    except DocumentValidationError:
        raise
    except Exception as exc:
        raise DocumentValidationError("corrupt_file", f"Excel (.xlsx) 解析失败：{type(exc).__name__}") from exc
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass

    if not sections:
        return "[该 Excel 工作簿未包含可提取数据。]"
    return _cap_extracted_text("\n\n".join(sections))


def parse_uploaded_document(raw_filename: str, raw: bytes) -> ParsedDocument:
    """Validate size, extension, magic bytes, and extract model-readable UTF-8 text."""
    if not raw:
        raise DocumentValidationError("empty_file", "上传文件不能为空")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise DocumentValidationError(
            "file_too_large",
            f"文件超过大小上限（最大支持 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB）",
            status_code=413,
        )

    filename = sanitize_upload_filename(raw_filename)
    ext = os.path.splitext(filename.lower())[1]

    if ext in LEGACY_OFFICE_EXTENSIONS:
        raise DocumentValidationError(
            "unsupported_format",
            f"暂不支持老旧二进制格式 {ext}，请另存为 .docx / .xlsx 后重新上传",
        )
    if ext not in SUPPORTED_UPLOAD_EXTENSIONS:
        raise DocumentValidationError(
            "unsupported_format",
            f"不支持的文件类型 {ext or '(无后缀)'}；支持文本/代码/表格类文件及 .pdf / .docx / .xlsx",
        )

    if ext == ".pdf":
        return ParsedDocument(
            filename=filename,
            ext=ext,
            content_type="application/pdf",
            is_binary=True,
            extracted_text=_extract_pdf_text(raw),
        )
    if ext == ".docx":
        return ParsedDocument(
            filename=filename,
            ext=ext,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            is_binary=True,
            extracted_text=_extract_docx_text(raw),
        )
    if ext == ".xlsx":
        return ParsedDocument(
            filename=filename,
            ext=ext,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            is_binary=True,
            extracted_text=_extract_xlsx_text(raw),
        )

    if b"\x00" in raw:
        raise DocumentValidationError("invalid_magic", "文本文件包含二进制空字节，请检查文件格式")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentValidationError("invalid_encoding", "文本文件须为 UTF-8 编码") from exc

    return ParsedDocument(
        filename=filename,
        ext=ext,
        content_type=safe_download_content_type(filename),
        is_binary=False,
        extracted_text=_cap_extracted_text(text),
    )
