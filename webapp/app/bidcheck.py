"""开标批次核对：读取记录目录、唱标明细与汇总，核对人数和最低报价。

业务口径：
- 输入记录目录首页 URL 与批次号，沿同站点分页遍历记录目录；
- 目录中每个标段提供企业唱标明细页和人数/最低报价汇总页；
- 按“标段号 + 统一社会信用代码”去重；
- 同一标段内同一统一社会信用代码出现不同报价时标记为冲突，
  冲突标段不核算最低报价，绝不任选其中一个报价；
- 只有目录、明细、汇总的批次修订号完全一致，且没有读取失败/上限截断时，
  才比较汇总人数、最低报价与明细去重结果；
- 结束前重新读取目录首页，若修订号或目录内容在核对期间变化，则无法核定；
- 任何无法核定的情形都保留问题链接，不输出“一致”结论。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .utils import same_origin

REQUEST_TIMEOUT = 15.0
USER_AGENT = "bid-opening-checker/1.0 (+local procurement audit tool)"

MAX_DIRECTORY_PAGES = 10
MAX_DETAIL_PAGES = 100
MAX_SUMMARY_PAGES = 100
MAX_SECTIONS = 200

_NEXT_TEXT_RE = re.compile(r"^(?:下一页|下页|后一页|更多页|next|>|»)$", re.IGNORECASE)

_LABEL_ALIASES: dict[str, str] = {
    "批次号": "batch_no",
    "开标批次": "batch_no",
    "批次编号": "batch_no",
    "招标批次号": "batch_no",
    "批次修订号": "revision",
    "批次修订": "revision",
    "目录修订号": "revision",
    "修订号": "revision",
    "批次版本号": "revision",
    "修订版本": "revision",
    "标段号": "section_no",
    "标段编号": "section_no",
    "包号": "section_no",
    "标段": "section_no",
    "标段名称": "section_name",
    "包名称": "section_name",
    "投标人数": "headcount",
    "唱标人数": "headcount",
    "投标人家数": "headcount",
    "投标人数量": "headcount",
    "投标单位数量": "headcount",
    "投标单位家数": "headcount",
    "供应商数量": "headcount",
    "供应商家数": "headcount",
    "家数": "headcount",
    "人数": "headcount",
    "最低唱标报价": "min_quote",
    "最低唱标价": "min_quote",
    "最低投标报价": "min_quote",
    "最低投标价": "min_quote",
    "最低报价": "min_quote",
    "最低价": "min_quote",
}
_REVISION_RE = re.compile(
    r"批次修订号(?:[：:]\s*|\s+)([0-9A-Za-z][0-9A-Za-z._\-/]{0,59})"
)
_BATCH_RE = re.compile(
    r"批次(?:号|编号)(?:[：:]\s*|\s+)([0-9A-Za-z][0-9A-Za-z._\-/]{0,39})"
)
_SECTION_NO_RE = re.compile(
    r"(?:标段号|标段编号|包号)\s*[：:]?\s*([0-9A-Za-z][0-9A-Za-z\-_/.（）()]{0,40})"
)
_SECTION_PREFIX_RE = re.compile(
    r"标段\s*[：:]\s*([0-9A-Za-z][0-9A-Za-z\-_/.（）()]{0,40})"
)
_SECTION_NAME_RE = re.compile(r"标段名称\s*[：:]?\s*([^\s<，,。；;]{1,60})")
_HEADCOUNT_RE = re.compile(
    r"(?:投标人数|唱标人数|投标人家数|投标人数量|投标单位家数|供应商数量|供应商家数|家数)"
    r"\s*[：:]?\s*([0-9]+)"
)
_MIN_QUOTE_RE = re.compile(
    r"最低(?:唱标|投标)?(?:报价|价)\s*[：:]?\s*([^\n\r<。；;]{1,80})"
)
_CODE_RE = re.compile(r"^[0-9A-Z]{18}$")
_MONEY_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class BatchStatus(str, Enum):
    CONSISTENT = "一致"
    DIFFERENCE = "存在差异"
    CONFLICT = "存在冲突"
    UNABLE = "无法核定"


@dataclass
class Problem:
    url: str
    kind: str
    reason: str


@dataclass
class RevisionRecord:
    url: str
    kind: str
    revision: str
    batch_no: str = ""


@dataclass
class DirectoryEntry:
    section_no: str
    section_key: str
    section_name: str = ""
    detail_urls: list[str] = field(default_factory=list)
    summary_url: str | None = None
    directory_urls: list[str] = field(default_factory=list)
    section_no_inferred: bool = False
    summary_url_ambiguous: bool = False


@dataclass
class BidRecord:
    section_no: str
    section_key: str
    section_name: str
    credit_code: str
    enterprise_name: str
    quote_text: str
    quote: Decimal | None
    url: str


@dataclass
class SummaryData:
    section_no: str
    section_key: str
    section_name: str
    headcount: int | None
    headcount_text: str
    min_quote_text: str
    min_quote: Decimal | None
    url: str
    revision: str


@dataclass
class QuoteOccurrence:
    quote_text: str
    quote: Decimal | None
    url: str
    enterprise_name: str = ""


@dataclass
class QuoteConflict:
    section_no: str
    section_name: str
    credit_code: str
    enterprise_names: list[str] = field(default_factory=list)
    occurrences: list[QuoteOccurrence] = field(default_factory=list)


@dataclass
class SectionResult:
    section_no: str
    section_name: str
    directory_urls: list[str] = field(default_factory=list)
    detail_urls: list[str] = field(default_factory=list)
    summary_url: str | None = None
    summary_count: int | None = None
    detail_count: int | None = None
    count_diff: int | None = None
    summary_min: Decimal | None = None
    detail_min: Decimal | None = None
    min_diff: Decimal | None = None
    conflicts: list[QuoteConflict] = field(default_factory=list)
    status: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class BatchReport:
    entry_url: str
    batch_no: str
    status: BatchStatus = BatchStatus.UNABLE
    verdict_note: str = ""
    directory_home: str = ""
    directory_revision: str = ""
    comparable: bool = False
    directory_pages: list[str] = field(default_factory=list)
    detail_pages: list[str] = field(default_factory=list)
    summary_pages: list[str] = field(default_factory=list)
    revisions: list[RevisionRecord] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    entries: list[DirectoryEntry] = field(default_factory=list)
    sections: list[SectionResult] = field(default_factory=list)
    conflicts: list[QuoteConflict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _normalize_url(base: str, href: str) -> str | None:
    if not href:
        return None
    url = urldefrag(urljoin(base, href))[0].strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def _strip_label(text: str) -> str:
    return re.sub(r"[\s:：*]+$", "", text or "").strip()


def _map_label(label_text: str) -> str | None:
    return _LABEL_ALIASES.get(_strip_label(label_text))


def _section_key(section_no: str) -> str:
    return re.sub(r"\s+", "", section_no or "").upper()


def _norm_code(code: str) -> str:
    return re.sub(r"\s+", "", code or "").upper()


def _parse_money(text: str) -> Decimal | None:
    """把“1,180,000.00 元 / 118.00万元”等文本规范为元。"""
    if not text:
        return None
    raw = text.replace("，", ",").replace(" ", "")
    multiplier = Decimal(1)
    if "亿元" in raw or "亿" in raw:
        multiplier = Decimal(100000000)
    elif "万元" in raw or "万" in raw:
        multiplier = Decimal(10000)
    clean = raw
    for token in ("人民币", "RMB", "rmb", "亿元", "万元", "亿", "万", "元", "￥", "¥", "$", ",", "，", " "):
        clean = clean.replace(token, "")
    # 允许“1180000.00（含税）”这类数字开头的附注；纯文字（如“详见附件”“SG-01”）不收
    if re.search(r"[A-Za-z一-鿿]", clean) and not re.match(r"^[（(]?[-+]?\d", clean):
        return None
    match = _MONEY_RE.search(clean)
    if not match:
        return None
    try:
        value = Decimal(match.group()) * multiplier
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_int(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"[0-9]+", text.replace(",", ""))
    return int(match.group()) if match else None


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _add_problem(report: BatchReport, url: str, kind: str, reason: str) -> None:
    key = (url, kind, reason)
    if key in {(p.url, p.kind, p.reason) for p in report.problems}:
        return
    report.problems.append(Problem(url=url, kind=kind, reason=reason))


def _http_error_text(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


# ---------------------------------------------------------------------------
# 页面解析
# ---------------------------------------------------------------------------


def _collect_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """从表格、dl、label、正文中收集规范标签 -> 值，保留第一个非空值。"""
    pairs: dict[str, str] = {}

    def put(label: str, value: str) -> None:
        key = _map_label(label)
        value = (value or "").strip()
        # 表头行会出现“标段号 | 标段名称”这类两列都是标签的情况，跳过
        if key and value and _map_label(value) is None and key not in pairs:
            pairs[key] = value

    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            put(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True))
        if len(cells) >= 4:
            put(cells[2].get_text(" ", strip=True), cells[3].get_text(" ", strip=True))

    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            put(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))

    for label in soup.find_all("label"):
        target_id = label.get("for")
        control = soup.find(attrs={"id": target_id}) if target_id else label.find_next(
            ["input", "textarea", "select"]
        )
        if control is not None:
            value = control.get("value") if control.name == "input" else control.get_text(" ", strip=True)
            if value:
                put(label.get_text(" ", strip=True), str(value))

    for element in soup.find_all(["p", "div", "li", "span"]):
        own_text = "".join(str(child) for child in element.children if not getattr(child, "name", None))
        own_text += element.string or ""
        for match in re.finditer(
            r"([一-龥A-Za-z]{2,12}?)\s*[：:]\s*([^\n\r<。；;]{1,80})",
            own_text,
        ):
            put(match.group(1), match.group(2))

    return pairs


def _extract_revision(pairs: dict[str, str], full_text: str) -> str:
    value = pairs.get("revision")
    if value:
        return value.strip()
    match = _REVISION_RE.search(full_text)
    return match.group(1).strip() if match else ""


def _extract_batch_no(pairs: dict[str, str], full_text: str) -> str:
    value = pairs.get("batch_no")
    if value:
        return value.strip()
    match = _BATCH_RE.search(full_text)
    return match.group(1).strip() if match else ""


def _anchor_kind(anchor, page_url: str) -> str | None:
    text = anchor.get_text(" ", strip=True)
    href = anchor.get("href", "")
    url = _normalize_url(page_url, href) or ""
    path = urlparse(url).path.lower()
    if "汇总" in text or "summary" in path:
        return "summary"
    if (
        "明细" in text
        or "唱标" in text
        or "报价" in text
        or "detail" in path
        or "bid" in path
    ):
        return "detail"
    return None


def _looks_like_section_no(text: str) -> bool:
    text = _strip_label(text)
    if not text or len(text) > 40:
        return False
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z\-_/.]{0,39}", text):
        return False
    return text.lower() not in {"detail", "summary", "index", "page", "next", "list"}


def _infer_section_no(row, detail_urls: list[str], summary_url: str | None) -> tuple[str, bool]:
    text = row.get_text(" ", strip=True)
    match = _SECTION_NO_RE.search(text) or _SECTION_PREFIX_RE.search(text)
    if match:
        return match.group(1).strip(), False

    for cell in row.find_all(["td", "th"]):
        if cell.find("a", href=True):
            continue
        candidate = cell.get_text(" ", strip=True)
        if _looks_like_section_no(candidate):
            return candidate, False

    for url in detail_urls or ([summary_url] if summary_url else []):
        if not url:
            continue
        stem = urlparse(url).path.rsplit("/", 1)[-1]
        stem = re.sub(r"\.(?:html?|shtml)$", "", stem, flags=re.IGNORECASE)
        if _looks_like_section_no(stem):
            return stem, True
    return "", False


def _infer_section_name(row, section_no: str) -> str:
    text = row.get_text(" ", strip=True)
    match = _SECTION_NAME_RE.search(text)
    if match:
        return match.group(1).strip()

    for cell in row.find_all(["td", "th"]):
        if cell.find("a", href=True):
            continue
        candidate = cell.get_text(" ", strip=True)
        if not candidate or candidate == section_no:
            continue
        if _map_label(candidate) is not None:
            continue
        if _looks_like_section_no(candidate):
            continue
        if candidate.lower() in {"唱标明细", "明细", "汇总", "人数与最低报价汇总"}:
            continue
        return candidate
    return ""


def _is_next_page_link(text: str, href: str) -> bool:
    if _NEXT_TEXT_RE.match(text.strip()):
        return True
    return bool(re.search(r"[?&](?:page|pageNum|p)=\d+", href, re.IGNORECASE))


def _detect_next_page(soup: BeautifulSoup, page_url: str) -> str | None:
    """在明细/汇总页中识别同站“下一页”链接。"""
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        href = anchor.get("href", "")
        url = _normalize_url(page_url, href)
        if not url or url == page_url:
            continue
        if _is_next_page_link(text, href):
            return url
    return None


@dataclass
class ParsedDirectory:
    batch_no: str
    revision: str
    entries: list[DirectoryEntry]
    next_page: str | None


def parse_directory_page(html: str, page_url: str) -> ParsedDirectory:
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(" ", strip=True)
    pairs = _collect_pairs(soup)
    revision = _extract_revision(pairs, full_text)
    batch_no = _extract_batch_no(pairs, full_text)

    entries: list[DirectoryEntry] = []
    next_page: str | None = None

    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        url = _normalize_url(page_url, anchor.get("href", ""))
        if not url or url == page_url:
            continue
        if _is_next_page_link(text, anchor.get("href", "")):
            next_page = next_page or url
            continue

    for row in soup.find_all("tr"):
        anchors = row.find_all("a", href=True)
        if not anchors:
            continue
        detail_urls: list[str] = []
        summary_urls: list[str] = []
        for anchor in anchors:
            url = _normalize_url(page_url, anchor.get("href", ""))
            if not url:
                continue
            kind = _anchor_kind(anchor, page_url)
            if kind == "summary":
                summary_urls.append(url)
            elif kind == "detail":
                detail_urls.append(url)
            elif (
                url != page_url
                and _is_next_page_link(anchor.get_text(" ", strip=True), anchor.get("href", ""))
            ):
                next_page = next_page or url

        if not detail_urls and not summary_urls:
            continue

        detail_urls = list(dict.fromkeys(detail_urls))
        summary_urls = list(dict.fromkeys(summary_urls))
        section_no, inferred = _infer_section_no(row, detail_urls, summary_urls[0] if summary_urls else None)
        section_name = _infer_section_name(row, section_no)
        entries.append(
            DirectoryEntry(
                section_no=section_no,
                section_key=_section_key(section_no),
                section_name=section_name,
                detail_urls=detail_urls,
                summary_url=summary_urls[0] if summary_urls else None,
                directory_urls=[page_url],
                section_no_inferred=inferred,
                summary_url_ambiguous=len(summary_urls) > 1,
            )
        )

    # 非表格的列表页兜底（先看 li，再看不含嵌套记录容器的 div，避免父容器重复收录）
    def add_container(item) -> None:
        anchors = item.find_all("a", href=True)
        if not anchors:
            return
        detail_urls, summary_urls = [], []
        for anchor in anchors:
            url = _normalize_url(page_url, anchor.get("href", ""))
            if not url:
                continue
            kind = _anchor_kind(anchor, page_url)
            if kind == "summary":
                summary_urls.append(url)
            elif kind == "detail":
                detail_urls.append(url)
        if not detail_urls and not summary_urls:
            return
        detail_urls = list(dict.fromkeys(detail_urls))
        summary_urls = list(dict.fromkeys(summary_urls))
        section_no, inferred = _infer_section_no(
            item, detail_urls, summary_urls[0] if summary_urls else None
        )
        entries.append(
            DirectoryEntry(
                section_no=section_no,
                section_key=_section_key(section_no),
                section_name=_infer_section_name(item, section_no),
                detail_urls=detail_urls,
                summary_url=summary_urls[0] if summary_urls else None,
                directory_urls=[page_url],
                section_no_inferred=inferred,
                summary_url_ambiguous=len(summary_urls) > 1,
            )
        )

    if not entries:
        for item in soup.find_all("li"):
            add_container(item)
    if not entries:
        for item in soup.find_all("div"):
            has_nested_records = any(
                _anchor_kind(anchor, page_url) in ("detail", "summary")
                for child in item.find_all(["li", "div"])
                for anchor in child.find_all("a", href=True)
            )
            if has_nested_records:
                continue
            add_container(item)

    # 下一页面链接也可能出现在表格行之外
    if next_page is None:
        for anchor in soup.find_all("a", href=True):
            if _anchor_kind(anchor, page_url) is not None:
                continue
            if _is_next_page_link(anchor.get_text(" ", strip=True), anchor.get("href", "")):
                candidate = _normalize_url(page_url, anchor.get("href", ""))
                if candidate and candidate != page_url:
                    next_page = candidate
                    break

    return ParsedDirectory(batch_no=batch_no, revision=revision, entries=entries, next_page=next_page)


@dataclass
class ParsedDetail:
    batch_no: str
    revision: str
    section_no: str
    section_name: str
    records: list[BidRecord]
    next_page: str | None = None


def _column_index(header_cells, keywords: tuple[str, ...]) -> int | None:
    for index, cell in enumerate(header_cells):
        text = cell.get_text(" ", strip=True)
        if any(keyword in text for keyword in keywords):
            return index
    return None


def _find_code_cell(cells) -> int | None:
    for index, cell in enumerate(cells):
        text = _norm_code(cell.get_text(" ", strip=True))
        if _CODE_RE.fullmatch(text) and any(ch.isdigit() for ch in text):
            return index
    return None


def _parse_bid_rows(soup: BeautifulSoup, url: str, page_section_no: str, page_section_name: str) -> list[BidRecord]:
    records: list[BidRecord] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_index = None
        code_idx = name_idx = quote_idx = section_idx = None
        for row_index, row in enumerate(rows):
            cells = row.find_all(["th", "td"])
            texts = [cell.get_text(" ", strip=True) for cell in cells]
            code_columns = [
                index
                for index, text in enumerate(texts)
                if "信用代码" in text or "统一社会信用" in text
            ]
            quote_columns = [
                index for index, text in enumerate(texts) if "报价" in text or "价格" in text
            ]
            # 信用代码与报价必须位于不同列，避免把“统计口径”说明行误当表头
            if code_columns and quote_columns and code_columns[0] != quote_columns[0]:
                header_index = row_index
                code_idx = code_columns[0]
                name_idx = _column_index(cells, ("企业名称", "投标人名称", "供应商名称", "单位名称", "投标企业"))
                quote_idx = _column_index(cells, ("报价", "价格", "唱标"))
                section_idx = _column_index(cells, ("标段号", "标段编号", "包号"))
                break

        data_rows = rows[header_index + 1 :] if header_index is not None else rows

        for row in data_rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            texts = [cell.get_text(" ", strip=True) for cell in cells]

            if header_index is not None and code_idx is not None:
                if code_idx >= len(cells):
                    continue
                code_text = texts[code_idx]
                if not code_text or _map_label(code_text) is not None:
                    continue
                name_text = texts[name_idx] if name_idx is not None and name_idx < len(cells) else ""
                quote_text = texts[quote_idx] if quote_idx is not None and quote_idx < len(cells) else ""
                row_section_no = (
                    texts[section_idx]
                    if section_idx is not None and section_idx < len(cells) and texts[section_idx]
                    else page_section_no
                )
            else:
                code_position = _find_code_cell(cells)
                if code_position is None:
                    continue
                code_text = texts[code_position]
                name_text = ""
                quote_text = ""
                for index, text in enumerate(texts):
                    if index == code_position or not text:
                        continue
                    if _parse_money(text) is not None and "报价" not in text and "价格" not in text:
                        quote_text = text
                    elif _parse_money(text) is not None and ("报价" in text or "价格" in text):
                        quote_text = text
                    elif not _parse_money(text) and not name_text:
                        name_text = text
                row_section_no = page_section_no

            credit_code = _norm_code(code_text)
            if not credit_code or _map_label(credit_code) is not None:
                continue
            if _CODE_RE.fullmatch(credit_code) is None:
                # 仍保留记录，由调用方在页面层面提示；这里只排除明显的表头文本
                if len(credit_code) < 8:
                    continue
            section_no = row_section_no or page_section_no
            records.append(
                BidRecord(
                    section_no=section_no,
                    section_key=_section_key(section_no),
                    section_name=page_section_name,
                    credit_code=credit_code,
                    enterprise_name=name_text.strip(),
                    quote_text=quote_text.strip(),
                    quote=_parse_money(quote_text),
                    url=url,
                )
            )

    return records


def parse_detail_page(html: str, url: str, fallback: DirectoryEntry) -> ParsedDetail:
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(" ", strip=True)
    pairs = _collect_pairs(soup)
    revision = _extract_revision(pairs, full_text)
    batch_no = _extract_batch_no(pairs, full_text)
    section_no = pairs.get("section_no") or fallback.section_no
    section_name = pairs.get("section_name") or fallback.section_name
    records = _parse_bid_rows(soup, url, section_no, section_name)
    return ParsedDetail(
        batch_no=batch_no,
        revision=revision,
        section_no=section_no,
        section_name=section_name,
        records=records,
        next_page=_detect_next_page(soup, url),
    )


@dataclass
class ParsedSummary:
    batch_no: str
    revision: str
    section_no: str
    section_name: str
    headcount: int | None
    headcount_text: str
    min_quote_text: str
    min_quote: Decimal | None
    next_page: str | None = None


def parse_summary_page(html: str, url: str, fallback: DirectoryEntry) -> ParsedSummary:
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(" ", strip=True)
    pairs = _collect_pairs(soup)
    revision = _extract_revision(pairs, full_text)
    batch_no = _extract_batch_no(pairs, full_text)
    section_no = pairs.get("section_no") or fallback.section_no
    section_name = pairs.get("section_name") or fallback.section_name

    headcount_text = pairs.get("headcount", "")
    min_quote_text = pairs.get("min_quote", "")
    if not headcount_text:
        match = _HEADCOUNT_RE.search(full_text)
        headcount_text = match.group(1) if match else ""
    if not min_quote_text:
        match = _MIN_QUOTE_RE.search(full_text)
        min_quote_text = match.group(1).strip() if match else ""

    return ParsedSummary(
        batch_no=batch_no,
        revision=revision,
        section_no=section_no,
        section_name=section_name,
        headcount=_parse_int(headcount_text),
        headcount_text=headcount_text,
        min_quote_text=min_quote_text,
        min_quote=_parse_money(min_quote_text),
        next_page=_detect_next_page(soup, url),
    )


# ---------------------------------------------------------------------------
# 核对编排
# ---------------------------------------------------------------------------


def _merge_entry(entries: dict[str, DirectoryEntry], entry: DirectoryEntry, report: BatchReport) -> None:
    key = entry.section_key or f"__unknown__:{entry.section_no}"
    existing = entries.get(key)
    if existing is None:
        entries[key] = entry
        return

    for url in entry.detail_urls:
        if url not in existing.detail_urls:
            existing.detail_urls.append(url)
    if entry.summary_url:
        if not existing.summary_url:
            existing.summary_url = entry.summary_url
        elif existing.summary_url != entry.summary_url:
            _add_problem(report, entry.directory_urls[-1], "目录", f"标段 {entry.section_no} 存在多个不同的汇总链接")
    if not existing.section_name and entry.section_name:
        existing.section_name = entry.section_name
    for url in entry.directory_urls:
        if url not in existing.directory_urls:
            existing.directory_urls.append(url)
    existing.section_no_inferred = existing.section_no_inferred and entry.section_no_inferred


def _check_page_identity(
    report: BatchReport,
    url: str,
    kind: str,
    parsed_batch_no: str,
    revision: str,
    home_revision: str,
    batch_no: str,
) -> None:
    report.revisions.append(
        RevisionRecord(url=url, kind=kind, revision=revision, batch_no=parsed_batch_no)
    )
    if not revision:
        _add_problem(report, url, kind, "页面缺少批次修订号，无法核对修订一致性")
    elif home_revision and revision != home_revision:
        _add_problem(
            report,
            url,
            kind,
            f"批次修订号与目录首页不一致（目录：{home_revision}，本页：{revision}）",
        )
    if parsed_batch_no and parsed_batch_no != batch_no:
        _add_problem(
            report,
            url,
            kind,
            f"页面批次号（{parsed_batch_no}）与输入批次号（{batch_no}）不一致",
        )


def _directory_signature(entries: list[DirectoryEntry]) -> tuple:
    return tuple(
        sorted(
            (
                entry.section_key,
                tuple(sorted(entry.detail_urls)),
                entry.summary_url or "",
            )
            for entry in entries
        )
    )


def _build_sections(
    entries: dict[str, DirectoryEntry],
    records: list[BidRecord],
    summaries: dict[str, SummaryData],
    comparable: bool,
) -> tuple[list[SectionResult], list[QuoteConflict]]:
    keys = set(entries) | {record.section_key for record in records} | set(summaries)
    sections: list[SectionResult] = []
    all_conflicts: list[QuoteConflict] = []

    for key in sorted(keys):
        entry = entries.get(key)
        section_records = [record for record in records if record.section_key == key]
        summary = summaries.get(key)

        if entry and entry.section_no:
            section_no = entry.section_no
        elif section_records:
            section_no = section_records[0].section_no
        elif summary and summary.section_no:
            section_no = summary.section_no
        else:
            section_no = key
        section_name = ""
        if entry and entry.section_name:
            section_name = entry.section_name
        elif section_records:
            section_name = section_records[0].section_name
        elif summary:
            section_name = summary.section_name

        grouped: dict[str, list[BidRecord]] = {}
        for record in section_records:
            grouped.setdefault(record.credit_code, []).append(record)

        section_conflicts: list[QuoteConflict] = []
        for code, group in grouped.items():
            quotes = {record.quote for record in group if record.quote is not None}
            if len(quotes) > 1:
                names = sorted({record.enterprise_name for record in group if record.enterprise_name})
                conflict = QuoteConflict(
                    section_no=section_no,
                    section_name=section_name,
                    credit_code=code,
                    enterprise_names=names,
                    occurrences=[
                        QuoteOccurrence(
                            quote_text=record.quote_text,
                            quote=record.quote,
                            url=record.url,
                            enterprise_name=record.enterprise_name,
                        )
                        for record in group
                    ],
                )
                section_conflicts.append(conflict)
                all_conflicts.append(conflict)

        detail_count = len(grouped)
        summary_count = summary.headcount if summary else None
        detail_min: Decimal | None = None
        if not section_conflicts and grouped:
            quotes = [group[0].quote for group in grouped.values() if group[0].quote is not None]
            detail_min = min(quotes) if quotes else None
        summary_min = summary.min_quote if summary else None

        count_diff: int | None = None
        min_diff: Decimal | None = None
        status = "无法核定"
        notes: list[str] = []

        if comparable:
            if detail_count is not None and summary_count is not None:
                count_diff = detail_count - summary_count
            if detail_min is not None and summary_min is not None:
                min_diff = detail_min - summary_min

            if section_conflicts:
                status = "报价冲突"
                notes.append("同一统一社会信用代码出现不同报价，不能任选其一，本标段不核算最低报价。")
            elif (
                (count_diff is not None and count_diff != 0)
                or (detail_min is None) != (summary_min is None)
                or (detail_min is not None and summary_min is not None and detail_min != summary_min)
            ):
                status = "存在差异"
            else:
                status = "一致"
        else:
            notes.append("存在页面读取失败、修订不一致或抓取上限等问题，未比较人数与最低报价。")

        if summary is None:
            notes.append("缺少汇总页，无法核对人数与最低报价。")
        elif summary.headcount is None:
            notes.append("汇总页未给出可解析的投标人数。")
        elif summary.min_quote is None:
            notes.append("汇总页未给出可解析的最低唱标报价。")

        if count_diff is not None and count_diff != 0:
            if count_diff > 0:
                notes.append(f"明细去重后比汇总多 {count_diff} 家，汇总可能漏记投标人。")
            else:
                notes.append(f"明细去重后比汇总少 {abs(count_diff)} 家，汇总可能多记或明细页缺失。")
        if min_diff is not None and min_diff != 0:
            if min_diff < 0:
                notes.append(f"明细最低报价比汇总低 {format_money(-min_diff)} 元，汇总可能漏记较低报价。")
            else:
                notes.append(f"明细最低报价比汇总高 {format_money(min_diff)} 元，汇总中的最低价在明细中不存在。")

        sections.append(
            SectionResult(
                section_no=section_no,
                section_name=section_name,
                directory_urls=list(entry.directory_urls) if entry else [],
                detail_urls=list(entry.detail_urls) if entry else [],
                summary_url=(
                    (entry.summary_url if entry and entry.summary_url else None)
                    or (summary.url if summary else None)
                ),
                summary_count=summary_count,
                detail_count=detail_count,
                count_diff=count_diff,
                summary_min=summary_min,
                detail_min=detail_min,
                min_diff=min_diff,
                conflicts=section_conflicts,
                status=status,
                notes=notes,
            )
        )

    return sections, all_conflicts


def _resolve_batch_directory(
    client: httpx.Client, entry_url: str, batch_no: str, report: BatchReport
) -> tuple[str, httpx.Response | None]:
    """若入口是批次选择页而非批次记录目录，按批次号找到唯一的批次目录链接。

    返回 (实际目录首页, 可复用的入口页响应)。入口页本身就是批次目录时复用响应，
    避免重复请求，也避免把一次核对算成两次目录读取。
    """
    try:
        response = client.get(entry_url)
        response.raise_for_status()
    except httpx.HTTPError:
        return entry_url, None
    final_url = str(response.url)
    if not same_origin(entry_url, final_url):
        return entry_url, None

    parsed = parse_directory_page(response.text, final_url)
    if parsed.batch_no or parsed.entries:
        return entry_url, response

    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[str] = []
    batch_pattern = re.compile(re.escape(batch_no) + r"(?![0-9A-Za-z])", re.IGNORECASE)
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        href = anchor.get("href", "")
        url = _normalize_url(final_url, href)
        if not url or not same_origin(entry_url, url):
            continue
        if _anchor_kind(anchor, final_url) in ("detail", "summary"):
            continue
        if _is_next_page_link(text, href):
            continue
        if batch_pattern.search(text + " " + url):
            candidates.append(url)

    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        _add_problem(
            report,
            final_url,
            "目录",
            f"目录首页存在多个与批次号 {batch_no} 匹配的链接，无法确定批次目录",
        )
    else:
        _add_problem(
            report,
            final_url,
            "目录",
            f"目录首页未找到与批次号 {batch_no} 匹配的批次目录链接",
        )
    return entry_url, response


def verify_batch(
    entry_url: str,
    batch_no: str,
    *,
    max_directory_pages: int = MAX_DIRECTORY_PAGES,
    max_detail_pages: int = MAX_DETAIL_PAGES,
    max_summary_pages: int = MAX_SUMMARY_PAGES,
    max_sections: int = MAX_SECTIONS,
) -> BatchReport:
    entry_url = urldefrag((entry_url or "").strip())[0]
    batch_no = (batch_no or "").strip()
    report = BatchReport(entry_url=entry_url, batch_no=batch_no)

    parsed_entry = urlparse(entry_url)
    if parsed_entry.scheme not in ("http", "https") or not parsed_entry.netloc:
        _add_problem(report, entry_url, "输入", "记录目录 URL 必须是 http(s) 开头的完整地址")
        report.verdict_note = "输入地址无效，未读取任何页面。"
        return report
    if not batch_no:
        _add_problem(report, entry_url, "输入", "请输入批次号")
        report.verdict_note = "缺少批次号，未读取任何页面。"
        return report

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        directory_home, cached_entry_response = _resolve_batch_directory(
            client, entry_url, batch_no, report
        )
        report.directory_home = directory_home
        entry_url = directory_home

        entries: dict[str, DirectoryEntry] = {}
        visited_directory: set[str] = set()
        queue = [entry_url]
        home_revision = ""
        home_batch_no = ""
        home_signature: tuple | None = None
        first_page = True

        while queue:
            page_url = queue.pop(0)
            if page_url in visited_directory:
                continue
            if len(visited_directory) >= max_directory_pages:
                _add_problem(
                    report,
                    page_url,
                    "目录",
                    f"达到目录页抓取上限（{max_directory_pages} 页），无法确认记录目录已完整遍历",
                )
                break
            if not same_origin(entry_url, page_url):
                _add_problem(report, page_url, "目录", "目录分页链接与入口不同站点，未跟随")
                continue

            visited_directory.add(page_url)
            report.directory_pages.append(page_url)
            try:
                if cached_entry_response is not None and page_url == entry_url:
                    response = cached_entry_response
                    cached_entry_response = None
                else:
                    response = client.get(page_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                _add_problem(report, page_url, "目录", f"目录页读取失败：{_http_error_text(exc)}")
                first_page = False
                continue

            final_url = str(response.url)
            if not same_origin(entry_url, final_url):
                _add_problem(report, final_url, "目录", "目录页重定向到不同站点，未读取页面内容")
                first_page = False
                continue

            parsed = parse_directory_page(response.text, final_url)

            if first_page:
                home_revision = parsed.revision
                home_batch_no = parsed.batch_no
                home_signature = _directory_signature(parsed.entries)
                report.directory_revision = home_revision
                if not home_batch_no:
                    _add_problem(report, final_url, "目录", "目录首页未找到批次号，无法确认与输入批次号对应")
                first_page = False

            _check_page_identity(
                report,
                final_url,
                "目录",
                parsed.batch_no,
                parsed.revision,
                home_revision,
                batch_no,
            )

            for entry in parsed.entries:
                if not entry.section_no:
                    _add_problem(report, final_url, "目录", "目录记录缺少标段号，无法按标段核对")
                    continue
                if entry.section_no_inferred:
                    _add_problem(
                        report,
                        final_url,
                        "目录",
                        f"标段 {entry.section_no} 的标段号只能从链接推断，目录未明确标注",
                    )
                if entry.summary_url_ambiguous:
                    _add_problem(report, final_url, "目录", f"标段 {entry.section_no} 存在多个汇总链接")
                _merge_entry(entries, entry, report)

            if parsed.next_page:
                if not same_origin(entry_url, parsed.next_page):
                    _add_problem(report, final_url, "目录", "下一页指向不同站点，已停止跟随")
                elif parsed.next_page in visited_directory:
                    _add_problem(report, final_url, "目录", "下一页指回已访问页面，分页异常")
                else:
                    queue.append(parsed.next_page)

        if not entries:
            _add_problem(report, entry_url, "目录", "记录目录中未找到任何标段明细/汇总记录")

        entry_list = sorted(entries.values(), key=lambda item: item.section_key)
        if len(entry_list) > max_sections:
            overflow_entry = entry_list[max_sections]
            overflow_url = overflow_entry.directory_urls[-1] if overflow_entry.directory_urls else entry_url
            _add_problem(
                report,
                overflow_url,
                "目录",
                f"标段数量超过抓取上限（{max_sections} 个），无法完整核对",
            )
            entry_list = entry_list[:max_sections]

        records: list[BidRecord] = []
        summaries: dict[str, SummaryData] = {}
        visited_details: set[str] = set()
        visited_summaries: set[str] = set()
        detail_limit_reported = False
        summary_limit_reported = False

        for entry in entry_list:
            if not entry.detail_urls:
                _add_problem(report, entry.directory_urls[-1] if entry.directory_urls else entry_url,
                             "目录", f"标段 {entry.section_no} 缺少唱标明细链接")
            if not entry.summary_url:
                _add_problem(report, entry.directory_urls[-1] if entry.directory_urls else entry_url,
                             "目录", f"标段 {entry.section_no} 缺少人数/最低报价汇总链接")

            detail_queue = list(entry.detail_urls)
            while detail_queue:
                detail_url = detail_queue.pop(0)
                if not same_origin(entry_url, detail_url):
                    _add_problem(report, detail_url, "明细", "明细链接与入口不同站点，未读取")
                    continue
                if detail_url in visited_details:
                    continue
                if len(visited_details) >= max_detail_pages:
                    if not detail_limit_reported:
                        _add_problem(
                            report,
                            detail_url,
                            "明细",
                            f"达到明细页抓取上限（{max_detail_pages} 页），无法完整核对",
                        )
                        detail_limit_reported = True
                    continue
                visited_details.add(detail_url)
                report.detail_pages.append(detail_url)
                try:
                    response = client.get(detail_url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    _add_problem(report, detail_url, "明细", f"明细页读取失败：{_http_error_text(exc)}")
                    continue
                final_url = str(response.url)
                if not same_origin(entry_url, final_url):
                    _add_problem(report, final_url, "明细", "明细页重定向到不同站点，未读取页面内容")
                    continue

                parsed_detail = parse_detail_page(response.text, final_url, entry)
                _check_page_identity(
                    report,
                    final_url,
                    "明细",
                    parsed_detail.batch_no,
                    parsed_detail.revision,
                    home_revision,
                    batch_no,
                )
                if parsed_detail.section_no and _section_key(parsed_detail.section_no) != entry.section_key:
                    _add_problem(
                        report,
                        final_url,
                        "明细",
                        f"明细页标段号（{parsed_detail.section_no}）与目录（{entry.section_no}）不一致",
                    )
                if not parsed_detail.records:
                    _add_problem(report, final_url, "明细", "明细页未解析到任何企业唱标记录")
                records.extend(parsed_detail.records)

                if parsed_detail.next_page:
                    next_url = parsed_detail.next_page
                    if not same_origin(entry_url, next_url):
                        _add_problem(report, final_url, "明细", "明细下一页指向不同站点，已停止跟随")
                    elif next_url in visited_details:
                        _add_problem(report, final_url, "明细", "明细下一页指回已访问页面，分页异常")
                    else:
                        detail_queue.append(next_url)

            if entry.summary_url:
                summary_url = entry.summary_url
                if not same_origin(entry_url, summary_url):
                    _add_problem(report, summary_url, "汇总", "汇总链接与入口不同站点，未读取")
                elif summary_url in visited_summaries:
                    pass
                elif len(visited_summaries) >= max_summary_pages:
                    if not summary_limit_reported:
                        _add_problem(
                            report,
                            summary_url,
                            "汇总",
                            f"达到汇总页抓取上限（{max_summary_pages} 页），无法完整核对",
                        )
                        summary_limit_reported = True
                else:
                    visited_summaries.add(summary_url)
                    report.summary_pages.append(summary_url)
                    try:
                        response = client.get(summary_url)
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        _add_problem(report, summary_url, "汇总", f"汇总页读取失败：{_http_error_text(exc)}")
                    else:
                        final_url = str(response.url)
                        if not same_origin(entry_url, final_url):
                            _add_problem(report, final_url, "汇总", "汇总页重定向到不同站点，未读取页面内容")
                        else:
                            parsed_summary = parse_summary_page(response.text, final_url, entry)
                            _check_page_identity(
                                report,
                                final_url,
                                "汇总",
                                parsed_summary.batch_no,
                                parsed_summary.revision,
                                home_revision,
                                batch_no,
                            )
                            if parsed_summary.section_no and _section_key(parsed_summary.section_no) != entry.section_key:
                                _add_problem(
                                    report,
                                    final_url,
                                    "汇总",
                                    f"汇总页标段号（{parsed_summary.section_no}）与目录（{entry.section_no}）不一致",
                                )
                            if parsed_summary.headcount is None:
                                _add_problem(report, final_url, "汇总", "汇总页未给出可解析的投标人数")
                            if parsed_summary.min_quote is None:
                                _add_problem(report, final_url, "汇总", "汇总页未给出可解析的最低唱标报价")
                            if parsed_summary.next_page:
                                _add_problem(
                                    report,
                                    final_url,
                                    "汇总",
                                    "汇总页存在下一页，未读取后续页面，无法确认汇总完整",
                                )
                            if entry.section_key in summaries and summaries[entry.section_key].url != final_url:
                                _add_problem(report, final_url, "汇总", "同一标段存在多个不同的汇总页")
                            else:
                                summaries[entry.section_key] = SummaryData(
                                    section_no=parsed_summary.section_no or entry.section_no,
                                    section_key=entry.section_key,
                                    section_name=parsed_summary.section_name or entry.section_name,
                                    headcount=parsed_summary.headcount,
                                    headcount_text=parsed_summary.headcount_text,
                                    min_quote_text=parsed_summary.min_quote_text,
                                    min_quote=parsed_summary.min_quote,
                                    url=final_url,
                                    revision=parsed_summary.revision,
                                )

        # 明细中出现的标段必须有汇总，且报价必须可解析，否则数据不完整
        record_keys = {record.section_key for record in records}
        for key in sorted(record_keys):
            if key not in summaries:
                sample = next(record for record in records if record.section_key == key)
                _add_problem(report, sample.url, "汇总", f"标段 {sample.section_no} 有唱标明细但缺少对应汇总页")
        missing_quote = [record for record in records if record.quote is None]
        for record in missing_quote:
            _add_problem(
                report,
                record.url,
                "明细",
                f"标段 {record.section_no} 企业 {record.credit_code} 的唱标报价无法解析（原文：{record.quote_text or '空'}）",
            )
        for record in records:
            if _CODE_RE.fullmatch(record.credit_code) is None:
                _add_problem(
                    report,
                    record.url,
                    "明细",
                    f"标段 {record.section_no} 的统一社会信用代码格式无法识别（原文：{record.credit_code or '空'}）",
                )

        # 结束前重读目录首页：修订号或目录内容在核对期间变化则无法核定
        try:
            recheck_response = client.get(entry_url)
            recheck_response.raise_for_status()
        except httpx.HTTPError as exc:
            _add_problem(report, entry_url, "目录复核", f"结束前重读目录首页失败：{_http_error_text(exc)}")
        else:
            final_url = str(recheck_response.url)
            recheck = parse_directory_page(recheck_response.text, final_url)
            report.revisions.append(
                RevisionRecord(
                    url=final_url,
                    kind="目录复核",
                    revision=recheck.revision,
                    batch_no=recheck.batch_no,
                )
            )
            if recheck.revision != home_revision:
                _add_problem(
                    report,
                    final_url,
                    "目录复核",
                    f"目录首页批次修订号在核对期间发生变化（开始：{home_revision or '未识别'}，结束：{recheck.revision or '未识别'}）",
                )
            if home_signature is not None and _directory_signature(recheck.entries) != home_signature:
                _add_problem(report, final_url, "目录复核", "目录首页的标段/明细/汇总链接在核对期间发生变化")
            if recheck.batch_no and recheck.batch_no != batch_no:
                _add_problem(report, final_url, "目录复核", f"目录首页批次号（{recheck.batch_no}）与输入批次号（{batch_no}）不一致")
            elif not recheck.batch_no:
                _add_problem(report, final_url, "目录复核", "复核时目录首页未找到批次号")

    report.entries = entry_list
    report.comparable = not report.problems
    processed_entries = {entry.section_key: entry for entry in entry_list}
    report.sections, report.conflicts = _build_sections(
        processed_entries, records, summaries, report.comparable
    )

    if report.problems:
        report.status = BatchStatus.UNABLE
        report.verdict_note = (
            f"发现 {len(report.problems)} 个问题（读取失败、修订不一致或抓取不完整），"
            "未形成核定结论；请按问题链接人工核查。"
        )
    elif report.conflicts:
        report.status = BatchStatus.CONFLICT
        difference_count = sum(1 for section in report.sections if section.status == "存在差异")
        extra = f"另有 {difference_count} 个标段存在人数或最低报价差异。" if difference_count else ""
        report.verdict_note = (
            f"批次修订号一致且抓取完整，但发现 {len(report.conflicts)} 个报价冲突；"
            "冲突标段不核算最低报价，不能判定为一致。" + extra
        )
    elif any(section.status == "存在差异" for section in report.sections):
        difference_count = sum(1 for section in report.sections if section.status == "存在差异")
        report.status = BatchStatus.DIFFERENCE
        report.verdict_note = (
            f"批次修订号一致且抓取完整，发现 {difference_count} 个标段的人数或最低报价与汇总不一致。"
        )
    else:
        report.status = BatchStatus.CONSISTENT
        report.verdict_note = "批次修订号一致且抓取完整，各标段人数与最低报价均与汇总一致。"

    return report
