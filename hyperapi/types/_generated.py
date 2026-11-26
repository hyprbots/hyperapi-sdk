# GENERATED from openapi/hyperapi-public.yaml by scripts/generate_types.py.
# Do not edit by hand: change the spec and regenerate.

from __future__ import annotations

from builtins import list as list_aliased
from typing import Any, Literal, Optional, Union

from hyperapi.types._base import HyperAPIModel
from pydantic import ConfigDict, Field


class AsyncJobAccepted(HyperAPIModel):
    """
    Returned (HTTP 200) by every inference endpoint when the request sends `X-Async: true`.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    job_id: str
    status: Literal["pending"]
    poll_url: str
    """
    `/v1/jobs/{job_id}`
    """


class BatchCounts(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    total: int
    queued: int
    processing: int
    done: int
    failed: int


class BatchCreateRequest(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    endpoint: Literal[
        "/v1/parse",
        "/v1/classify",
        "/v1/split",
        "/v1/redact",
        "/v1/extract",
        "/v1/extract-omni",
    ]
    document_keys: list_aliased[str] = Field(..., min_length=1)
    """
    At most 1000 (413 above that).
    """
    options: Optional[dict[str, Any]] = None
    parse_mode: Optional[Literal["fast", "advanced"]] = None
    metadata: Optional[dict[str, Any]] = None
    """
    Opaque; echoed back.
    """
    webhook_url: Optional[str] = None
    estimated_pages_per_doc: int = Field(1, ge=1, le=10000)


class BatchCreateResponse(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    batch_id: str
    status: Literal[
        "queued",
        "processing",
        "completed",
        "completed_with_errors",
        "failed",
        "canceled",
    ]
    total_items: int


class BatchItemStatus(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    doc_index: int
    status: Literal["queued", "processing", "done", "failed"]
    error: Optional[str]
    """
    `canceled` for items stopped by a cancel.
    """
    result_url: Optional[str]
    """
    Presigned GET for the item's result file, signed when the response was built.
    """
    result_expired: bool
    filename: Optional[str]
    request_id: Optional[str]
    """
    `{batch_id}:{doc_index}`
    """
    pages_processed: Optional[int]
    tokens_used: Optional[int]


class BatchListEntry(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    batch_id: str
    status: Literal[
        "queued",
        "processing",
        "completed",
        "completed_with_errors",
        "failed",
        "canceled",
    ]
    endpoint: str
    total_items: int
    done_items: int
    failed_items: int
    created_at: str
    metadata: dict[str, Any]


class BatchListResponse(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    batches: list_aliased[BatchListEntry]
    limit: int
    offset: int


class BatchStatusResponse(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    batch_id: str
    status: Literal[
        "queued",
        "processing",
        "completed",
        "completed_with_errors",
        "failed",
        "canceled",
    ]
    endpoint: str
    counts: BatchCounts
    items_: list_aliased[BatchItemStatus] = Field(..., alias="items")
    created_at: str
    """
    ISO-8601 (Python isoformat).
    """
    metadata: dict[str, Any]


class Block(HyperAPIModel):
    """
    One document component. Null and empty-object values are left out; `heading`, `group` and `depth` place it in the document's structure.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    id: Optional[str] = None
    """
    `b<page>-<n>`
    """
    order: Optional[int] = None
    page: Optional[int] = None
    type: Optional[
        Literal[
            "abstract",
            "aside",
            "caption",
            "chart",
            "code",
            "contents",
            "figure",
            "footer",
            "footnote",
            "formula",
            "formula_number",
            "header",
            "heading",
            "list",
            "other",
            "page_number",
            "reference",
            "stamp",
            "table",
            "text",
            "title",
        ]
    ] = None
    """
    What kind of component a block/region is (the public replacement for the deprecated `label`).
    """
    bbox: Optional[list_aliased[float]] = None
    """
    [left, top, right, bottom] in the page's pixel space (`dimensions`).
    """
    html: Optional[str] = None
    furniture: Optional[bool] = None
    """
    Running head/foot.
    """
    empty: Optional[Literal[True]] = None
    """
    The markup holds nothing to read or see.
    """
    list: Optional[dict[str, Any]] = None
    continued_item: Optional[dict[str, Any]] = None
    style: Optional[dict[str, Any]] = None
    paint: Optional[dict[str, Any]] = None
    table_fallback: Optional[dict[str, Any]] = None
    heading: Optional[int] = Field(None, ge=1, le=6)
    group: Optional[str] = None
    """
    Innermost group id.
    """
    depth: Optional[int] = None


class BulkGap(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    reason: Optional[str] = None


class BulkParseResultSummary(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    pages_processed: int
    model_version: Literal["advanced"]


class ClassifyResponse(HyperAPIModel):
    """
    Reply of /v1/classify (sync, or a completed classify job).
    """

    model_config = ConfigDict(
        extra="allow",
    )
    status: Literal["success"]
    request_id: str
    task: Literal["classify"]
    model_used: str
    cached: bool
    result: ClassifyResult
    ocr_text: str
    duration_ms: int
    metadata: ResponseMetadata
    dashboard_url: Optional[str] = None


class ClassifyResult(HyperAPIModel):
    """
    The classification result.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    document_type: Optional[str]
    initial_label: Optional[str]
    early_exit: bool
    """
    True when the document was rejected.
    """
    early_exit_reason: str
    domain: Literal["financial", "non_financial"]
    status: Optional[str]
    """
    Classifier outcome (e.g. `rejected`).
    """
    raw_label: Optional[str]
    other_hint: Optional[str]
    subject_hint: Optional[str]
    confidence: Optional[float]
    needs_review: Optional[bool]
    rejection: Optional[dict[str, Any]]
    warnings: Optional[list_aliased[ClassifyWarning]]
    details: Optional[dict[str, Any]] = None
    """
    Present only when requested via classifier_config.include_details.
    """


class ClassifyWarning(HyperAPIModel):
    """
    A non-fatal classifier configuration warning.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    code: Optional[str] = None
    field: Optional[str] = None
    message: Optional[str] = None


class ContentDescriptor(HyperAPIModel):
    """
    What kind of content this is.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    kind: Optional[str] = None
    """
    `sheets` | `blocks` | `pages` | `text`
    """
    text_source: Optional[Any] = None
    counts: Optional[dict[str, Any]] = None


class CrossPageTable(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    id: str
    """
    `x<n>`
    """
    table_ids: list_aliased[Optional[str]]
    pages: list_aliased[int]
    """
    1-based, as pages[].page_number.
    """
    continuation: Optional[str]
    """
    e.g. `merged` or `linked`.
    """
    html: Optional[str]
    """
    Stitched table html (with figure parsing of tables).
    """
    header_rows_dropped: Optional[Any] = None
    split_rows_joined: Optional[Any] = None
    aligned: Optional[Any] = None
    links: list_aliased[Link]


class DocumentAsset(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    part: Optional[str] = None
    """
    Part name inside the package.
    """
    url: Optional[str] = None
    """
    Presigned GET; absent if the upload or signing failed.
    """


class DocumentAssets(HyperAPIModel):
    """
    Container inventory for packaged formats (XPS); `images`, `fonts` and `attachments` carry a signed `url`.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    images: Optional[list_aliased[DocumentAsset]] = None
    fonts: Optional[list_aliased[DocumentAsset]] = None
    attachments: Optional[list_aliased[DocumentAsset]] = None


class ErrorBody(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    """
    Machine-readable reason, stable across wording changes (e.g. `page_quota_exceeded`, `insufficient_credits`, `DOCUMENT_ENCRYPTED`, `DOCUMENT_UNREADABLE`, `SYNC_PAGE_LIMIT`), else a status name such as BAD_REQUEST, UNAUTHORIZED or PAYMENT_REQUIRED.
    """
    message: Union[str, dict[str, Any]]
    """
    Human-readable text. Usually a string; the batch quota refusal (402 on POST /v1/batch) sends an object with `error`, `list_price_cents` and `list_price_micros`.
    """
    details: Optional[dict[str, Any]] = None
    """
    Optional numbers behind a refusal; a quota refusal carries `pages_used`, `pages_limit` and `pages_pending`.
    """
    data: Optional[dict[str, Any]] = None
    """
    Optional structured payload sent with some batch errors.
    """


class ErrorEnvelope(HyperAPIModel):
    """
    Body of every error reply from the API.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    error: ErrorBody


class ExtractEntitiesResult(HyperAPIModel):
    """
    Extracted fields: `entities`, `line_items` and `summary`. If part of an extraction fails, its fields are null.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    entities: Optional[dict[str, Any]]
    """
    Header/party fields; keys depend on the document.
    """
    line_items: Optional[list_aliased[dict[str, Any]]]
    summary: Optional[dict[str, Any]]
    """
    Document-level fields (totals, tax, currency, document_type …); keys depend on the document.
    """
    partial: Optional[Literal[True]] = None
    """
    Present only when one leg failed.
    """
    leg_errors: Optional[dict[str, str]] = None
    """
    Present only with `partial` — failed leg name to exception type name.
    """


class ExtractResponse(HyperAPIModel):
    """
    Sync response and async job body for /v1/extract and /v1/extract-omni.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    status: Literal["success"]
    request_id: str
    task: Literal["extract", "extract-omni"]
    model_used: str
    cached: bool
    result: Union[ExtractEntitiesResult, ExtractTemplateResult]
    ocr_text: str
    duration_ms: int
    metadata: ResponseMetadata
    dashboard_url: Optional[str] = None


class ExtractTemplateResult(HyperAPIModel):
    """
    A template was filled in: the caller's `schema`, or the organization's stored template.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    data: Any
    """
    The template, filled in (same shape as the template).
    """
    summary: dict[str, Any]


class FigureParseSummary(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    requested: Optional[Any] = None
    available: Optional[Any] = None
    reason: Optional[Any] = None
    blocks_total: Optional[int] = None
    blocks_parsed: Optional[int] = None
    blocks_skipped: Optional[int] = None
    gaps: Optional[list_aliased[dict[str, Any]]] = None


class Group(HyperAPIModel):
    """
    One node of the outline. Optional keys depend on `kind` and the grouping profile.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    id: str
    """
    `g<n>`
    """
    kind: str
    """
    section, preamble, page_header, page_footer, figure, table, list, formula,
    algorithm, note, aside, and mail-thread kinds (message, its envelope/body and field
    groups). Open-ended.

    """
    pages: list_aliased[Optional[int]]
    children: list_aliased[str]
    """
    Block ids and group ids, in order.
    """
    boxes: list_aliased[GroupBox]
    level: Optional[int] = None
    """
    Sections only; depth clamped to 1..6.
    """
    depth: Optional[int] = None
    """
    Sections only.
    """
    heading: Optional[str] = None
    """
    Sections only — the heading block id.
    """
    caption: Optional[str] = None
    """
    Caption block id (profile B).
    """
    table: Optional[str] = None
    continues: Optional[str] = None
    """
    Group id of the table this one continues.
    """
    repeats: Optional[str] = None
    """
    Page header/footer — id of the first copy.
    """
    overlaps: Optional[list_aliased[GroupOverlap]] = None
    thread: Optional[dict[str, Any]] = None
    """
    Root message of a mail chain.
    """


class GroupBox(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    page: Optional[int]
    bbox: list_aliased[float]
    """
    [left, top, right, bottom] in the page's pixel space (`dimensions`).
    """


class GroupOverlap(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    with_: str = Field(..., alias="with")
    """
    The overlapping sibling's id.
    """
    page: Optional[int]
    iou: float
    cover: float
    above: Optional[bool] = None
    basis: Optional[Literal["paint"]] = None


class JobEnvelope(HyperAPIModel):
    """
    GET /v1/jobs/{job_id}: the job's state, and its full reply once completed.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    job_id: str
    status: Literal["pending", "completed", "failed"]
    """
    A cancelled job reads `failed` with an `error` starting `cancelled:`. Only GET /v1/jobs/recent reports `cancelled`.
    """
    request_id: str
    created_at: float
    """
    Unix epoch seconds (float).
    """
    completed_at: Optional[float] = None
    """
    Unix epoch seconds; set when the job reaches completed or failed.
    """
    filename: Optional[str]
    endpoint: Optional[str]
    """
    e.g. `/v1/parse`
    """
    document_key: Optional[str]
    source: Optional[str]
    """
    Channel that created the job: `api` or `playground`.
    """
    is_test: bool
    """
    True for hk_test_* keys.
    """
    result: Optional[
        Union[
            ParseResponse,
            ExtractResponse,
            ClassifyResponse,
            SplitResponse,
            RedactResponse,
        ]
    ]
    """
    The operation's full reply when completed; null while pending and on failure. `result.task` names the operation.
    """
    error: Optional[str]
    """
    Human-readable failure reason (failed jobs).
    """
    error_code: Optional[str] = None
    """
    Machine-readable reason (e.g. `insufficient_credits`), present on failed jobs only; null when the failure carried none.
    """
    status_code: Optional[int]
    """
    HTTP status of the failure, when it had one.
    """
    retry_after_seconds: Optional[int]
    """
    Backoff hint on a retryable failure (e.g. 429).
    """
    segments_total: Optional[int] = None
    """
    Bulk parse only — segment progress while pending.
    """
    segments_done: Optional[int] = None
    """
    Bulk parse only.
    """
    segments_failed: Optional[int] = None
    """
    Bulk parse only.
    """


class LayoutRegion(HyperAPIModel):
    """
    A layout region on the page. Keys with null values are left out.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    type: Literal[
        "abstract",
        "aside",
        "caption",
        "chart",
        "code",
        "contents",
        "figure",
        "footer",
        "footnote",
        "formula",
        "formula_number",
        "header",
        "heading",
        "list",
        "other",
        "page_number",
        "reference",
        "stamp",
        "table",
        "text",
        "title",
    ]
    """
    What kind of component a block/region is (the public replacement for the deprecated `label`).
    """
    bbox: list_aliased[float]
    """
    [left, top, right, bottom] in the page's pixel space (`dimensions`).
    """
    order: Optional[int] = None
    score: Optional[float] = None
    source: Optional[Any] = None
    content: Optional[str] = None
    """
    Parsed figure content (chart table as markdown, image caption, table html).
    """
    parse_status: Optional[str] = None
    parse_type: Optional[str] = None
    parse_sub_type: Optional[str] = None
    parse_reason: Optional[str] = None
    asset: Optional[str] = None
    """
    Part name of the region's stored crop (joins assets.images[].part). Dropped when unservable.
    """
    table_id: Optional[str] = None
    """
    merge_tables: `t<n>`, document-wide.
    """
    continues_from: Optional[str] = None
    continues_to: Optional[str] = None
    paint: Optional[dict[str, Any]] = None
    """
    {index, asset} — paint order of the native picture shown.
    """


class Link(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    from_: Optional[str] = Field(None, alias="from")
    to: Optional[str] = None
    decision: Optional[str] = None


class PageDimensions(HyperAPIModel):
    """
    The raster every `bbox` on the page is measured in.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    width: float
    height: float


class PageGap(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    bbox: list_aliased[float]
    """
    [left, top, right, bottom] in the page's pixel space (`dimensions`).
    """
    height: float
    after: Optional[str]
    """
    Id of the block above.
    """
    before: Optional[str]
    """
    Id of the block below.
    """
    ratio: float
    """
    Height over the page's usual spacing.
    """


class PageSize(HyperAPIModel):
    """
    The page's own size as stated by the document.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    width: Optional[float] = None
    height: Optional[float] = None
    unit: Optional[str] = None
    """
    `pt` where the source stated a page box, `px` for a bare image.
    """


class ParsePage(HyperAPIModel):
    """
    One page of a parse result.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    page_number: int
    """
    1-based.
    """
    text: str
    text_source: Optional[Literal["native", "ocr"]] = None
    """
    Where the page's text came from. Present when the page has text boxes.
    """
    dimensions: Optional[PageDimensions] = None
    page_size: Optional[PageSize] = None
    background: Optional[str] = None
    """
    The page's own background colour (`#rrggbb`), when stated.
    """
    gaps: Optional[list_aliased[PageGap]] = None
    """
    Large empty vertical bands between consecutive blocks. Present only on pages that have one.
    """
    boxes: Optional[list_aliased[TextBox]] = None
    """
    Line boxes. Present when include_boxes is on (fast-mode default).
    """
    word_boxes: Optional[list_aliased[TextBox]] = None
    """
    Word boxes with measured typography. Present exactly when `boxes` is.
    """
    image_url: Optional[str] = None
    """
    Presigned page image. Present when include_image is on and the page image was stored; omitted if signing failed.
    """
    layout: Optional[list_aliased[LayoutRegion]] = None
    """
    Layout regions in reading order (fast mode with include_layout).
    """
    layout_text: Optional[str] = None
    """
    The page's text rebuilt around its layout regions.
    """
    structured: Optional[StructuredPage] = None


class ParseResponse(HyperAPIModel):
    """
    Reply of /v1/parse (sync, or a completed parse job). A bulk parse job (an oversized advanced document, on plans that include bulk parse) completes with a summary instead: no `cached`, `result` is a BulkParseResultSummary, and `result_url` points at the full result.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    status: str
    """
    `success` (bulk: `failed` when no segment succeeded).
    """
    request_id: str
    task: Literal["parse"]
    model_used: str
    cached: Optional[bool] = None
    """
    True when the OCR result came from cache (billed at $0). Absent on a bulk summary.
    """
    result: Union[ParseResult, BulkParseResultSummary]
    duration_ms: int
    metadata: ResponseMetadata
    dashboard_url: Optional[str] = None
    """
    Deep link to this request in the dashboard, when snapshots are on.
    """
    result_url: Optional[str] = None
    """
    Bulk parse jobs only — presigned GET for the full stitched result (re-signed on every poll).
    """


class ParseResult(HyperAPIModel):
    """
    The parsed document: text, pages and components.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    ocr: str
    """
    Full text, pages joined by blank lines.
    """
    pages: list_aliased[ParsePage]
    blocks: list_aliased[Block]
    """
    The document's components in reading order across all pages (empty when none were found).
    """
    groups: Optional[list_aliased[Group]] = None
    """
    Outline over `blocks`. Present only when non-empty.
    """
    html: Optional[str] = None
    """
    The document as html (data-type attributes). Present only when non-empty.
    """
    css: Optional[str] = None
    """
    One style block shared by the html. Present only when non-empty.
    """
    canvas: Optional[str] = None
    """
    The document as html with pagination removed. Present only when non-empty.
    """
    layout_text: Optional[str] = None
    """
    Layout-aware text of every page, joined by form feed. Present when any page has a layout.
    """
    content: Optional[ContentDescriptor] = None
    figure_parse: Optional[FigureParseSummary] = None
    cross_page_tables: Optional[list_aliased[CrossPageTable]] = None
    """
    Present whenever merge_tables was in effect (empty list = none found).
    """
    sheets: Optional[list_aliased[dict[str, Any]]] = None
    """
    Spreadsheet grid, xlsx only: one object per sheet ({name, index, rows, cols, cells[]}).
    """
    workbook: Optional[dict[str, Any]] = None
    """
    Workbook-level structure, spreadsheets only (passed through).
    """
    email: Optional[dict[str, Any]] = None
    """
    Normalised message, .eml/.msg only (passed through).
    """
    assets: Optional[DocumentAssets] = None


class RecentJob(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    job_id: Optional[str]
    request_id: Optional[str]
    status: Optional[Literal["pending", "completed", "failed", "cancelled"]]
    task: Optional[str]
    endpoint: Optional[str]
    pages: Optional[int]
    filename: Optional[str]
    document_key: Optional[str]
    source: Literal["api", "playground"]
    is_test: bool
    created_at: Optional[float]
    completed_at: Optional[float]
    error: Optional[str]
    """
    Null for cancelled jobs.
    """


class RecentJobsResponse(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    jobs: list_aliased[RecentJob]


class RedactPage(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    page_number: int
    image_url: Optional[str] = None
    """
    Omitted if signing failed.
    """


class RedactResponse(HyperAPIModel):
    """
    Reply of /v1/redact (sync, or a completed redact job). No `ocr_text`.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    status: Literal["success"]
    request_id: str
    task: Literal["redact"]
    model_used: str
    cached: bool
    result: RedactResult
    duration_ms: int
    metadata: ResponseMetadata
    dashboard_url: Optional[str] = None


class RedactResult(HyperAPIModel):
    """
    The redaction result, inline or as page image URLs (`output=urls`).
    """

    model_config = ConfigDict(
        extra="allow",
    )
    mode: Literal["redact", "deidentify"]
    original_text_block: str
    updated_text_block: str
    updated_dictionary: dict[str, Any]
    summary: dict[str, Any]
    images: Optional[list_aliased[str]] = None
    """
    Inline mode — masked pages as base64 images.
    """
    original_page_image_urls: Optional[dict[str, str]] = None
    """
    Inline mode — page number (string) to presigned URL of the original page.
    """
    output: Optional[Literal["urls"]] = None
    """
    output=urls only.
    """
    page_count: Optional[int] = None
    """
    output=urls only.
    """
    pages: Optional[list_aliased[RedactPage]] = None
    """
    output=urls only — masked pages as presigned links.
    """
    content_kind: Optional[str] = None
    """
    Pixel-less formats only.
    """
    redacted_text: Optional[str] = None
    """
    Pixel-less text formats only.
    """
    redacted_spans: Optional[list_aliased[Any]] = None
    replacements: Optional[list_aliased[Any]] = None
    sheets: Optional[list_aliased[dict[str, Any]]] = None
    """
    Pixel-less spreadsheet input only.
    """


class ResponseMetadata(HyperAPIModel):
    """
    `metadata` of every inference reply. Job replies carry fewer keys than sync replies (no `content_type`; no `mode` on async classify and split).
    """

    model_config = ConfigDict(
        extra="allow",
    )
    pages: int
    """
    Page count processed.
    """
    source: Optional[str] = None
    """
    How the document was supplied (`document_key`).
    """
    filename: Optional[str] = None
    file_size: Optional[int] = None
    content_type: Optional[str] = None
    """
    Sync responses only.
    """
    mode: Optional[str] = None
    """
    The `mode` query value (classify/split/extract: e.g. `default`; redact: `redact`|`deidentify`).
    """
    parse_mode: Optional[str] = None
    """
    Extract only.
    """
    document_type: Optional[str] = None
    """
    Extract only.
    """
    groups_profile: Optional[Literal["A", "B"]] = None
    """
    Parse only; present when a profile other than A was requested and an outline was built.
    """
    assets_expire_at: Optional[str] = None
    """
    UTC ISO-8601 time at which the earliest signed URL in this reply expires. Absent when the reply has no URL.
    """
    total_pages: Optional[int] = None
    """
    Bulk parse only.
    """
    bulk: Optional[bool] = None
    """
    Bulk parse only (true).
    """
    segments_total: Optional[int] = None
    """
    Bulk parse only.
    """
    segments_failed: Optional[int] = None
    """
    Bulk parse only.
    """
    gaps: Optional[list_aliased[BulkGap]] = None
    """
    Bulk parse only — page ranges that failed; empty when every segment succeeded.
    """


class Segmentation(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    splits: list_aliased[SplitSegment]


class SplitNotRequired(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    not_required: Literal[True]
    reason: str


class SplitResponse(HyperAPIModel):
    """
    Reply of /v1/split (sync, or a completed split job).
    """

    model_config = ConfigDict(
        extra="allow",
    )
    status: Literal["success"]
    request_id: str
    task: Literal["split"]
    model_used: str
    cached: bool
    result: Union[SplitResult, SplitNotRequired]
    ocr_text: str
    duration_ms: int
    metadata: ResponseMetadata
    dashboard_url: Optional[str] = None


class SplitResult(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    segmentation: Segmentation
    metadata: dict[str, Any]
    """
    Currently always empty.
    """


class SplitSegment(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    split_label: Optional[str]
    start_page: Optional[int]
    """
    1-based, inclusive.
    """
    end_page: Optional[Union[int, str]]
    """
    1-based, inclusive; a string only for an unfinished segment.
    """
    confidence: Optional[Union[int, float]]
    """
    0-10.
    """
    identifying_features: Optional[str]
    sub_documents: list_aliased[SubDocument]


class StructuredPage(HyperAPIModel):
    """
    Advanced mode's per-page layout. Figure images in `html`/`markdown` are signed URLs or are left out.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    html: str
    markdown: str
    regions: list_aliased[StructuredRegion]
    figure_gaps: Optional[list_aliased[Any]] = None
    """
    Present only when figure understanding completed partially.
    """
    figures_total: Optional[int] = None
    figures_described: Optional[int] = None


class StructuredRegion(HyperAPIModel):
    """
    An advanced-mode region.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    type: Optional[
        Literal[
            "abstract",
            "aside",
            "caption",
            "chart",
            "code",
            "contents",
            "figure",
            "footer",
            "footnote",
            "formula",
            "formula_number",
            "header",
            "heading",
            "list",
            "other",
            "page_number",
            "reference",
            "stamp",
            "table",
            "text",
            "title",
        ]
    ] = None
    """
    What kind of component a block/region is (the public replacement for the deprecated `label`).
    """
    bbox: Optional[list_aliased[float]] = None
    """
    [left, top, right, bottom] in the page's pixel space (`dimensions`).
    """


class SubDocument(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    page: Optional[int] = None
    document_class: Optional[str] = None
    fingerprint: Optional[Any] = None
    initiation_evidence: Optional[Any] = None


class TaskLimit(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    max_bytes: int
    max_pages: Optional[int]
    """
    null = no page rejection for this task.
    """
    urls_max_pages: Optional[int]
    """
    Redact only, and only while `output=urls` is accepted; null elsewhere.
    """


class TextBox(HyperAPIModel):
    """
    One line (`boxes`) or word (`word_boxes`). Beyond the three required keys, a box carries only the attributes the document stated or that were measured; their types vary.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    text: str
    bbox: list_aliased[float]
    """
    [left, top, right, bottom] in the page's pixel space (`dimensions`).
    """
    confidence: Optional[float]
    font: Optional[Any] = None
    """
    Font family.
    """
    font_face: Optional[Any] = None
    """
    The embedded face, e.g. a weight-qualified name.
    """
    size: Optional[Any] = None
    """
    Size, in the unit of the page's `page_size`.
    """
    color: Optional[Any] = None
    bold: Optional[Any] = None
    italic: Optional[Any] = None
    superscript: Optional[Any] = None
    font_source: Optional[Any] = None
    """
    `document` or `model`.
    """
    href: Optional[Any] = None
    """
    Link target covering the run.
    """
    underline: Optional[Any] = None
    strikethrough: Optional[Any] = None
    background: Optional[Any] = None
    authored_font: Optional[Any] = None
    authored_size: Optional[Any] = None
    authored_color: Optional[Any] = None
    authored_background: Optional[Any] = None
    authored_bold: Optional[Any] = None
    authored_italic: Optional[Any] = None
    authored_underline: Optional[Any] = None
    authored_strikethrough: Optional[Any] = None
    authored_superscript: Optional[Any] = None
    source: Optional[Any] = None
    """
    `native` when the document supplied the text verbatim.
    """
    kind: Optional[Any] = None
    """
    What a non-text box is (e.g. `picture`).
    """
    asset: Optional[Any] = None
    """
    Stored part a picture was rendered from.
    """
    px: Optional[Any] = None
    """
    A picture's own pixel size.
    """
    fill: Optional[Any] = None
    """
    A rect's fill colour.
    """
    font_asset: Optional[Any] = None
    """
    Embedded font file the run was set in (joins assets.fonts).
    """
    content_id: Optional[Any] = None


class UploadLimits(HyperAPIModel):
    """
    Absent capability blocks are serialized as null — null means "not offered", never "unlimited".
    """

    model_config = ConfigDict(
        extra="allow",
    )
    max_upload_bytes: int
    parse_fast: TaskLimit
    parse_advanced: TaskLimit
    bulk_parse: Optional[TaskLimit]
    extract_fast: TaskLimit
    extract_advanced: Optional[TaskLimit]
    classify: TaskLimit
    split: TaskLimit
    redact: TaskLimit


class UploadRequest(HyperAPIModel):
    model_config = ConfigDict(
        extra="allow",
    )
    filename: str
    content_type: str = "application/pdf"
    """
    Must be an allowed type (PDF, common images, office text types, XPS) or the call is a 415.
    """
    content_length: Optional[int]
    """
    Declared byte size. Missing or < 1 is a 400 (the model types it optional; the handler requires it).
    """


class UploadResponse(HyperAPIModel):
    """
    The upload URL is signed for server-side encryption and the declared content type: the PUT must send `x-amz-server-side-encryption: AES256` and the same `Content-Type`.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    document_key: str
    upload_url: str
    expires_in: int
    """
    Seconds the upload_url stays valid.
    """
    limits: Optional[UploadLimits]
