"""Local NeuroForge conference-preparation application.

The service is deliberately loopback-oriented. Medical images stay under the
local NeuroForge data root; online literature requests contain only the query
typed by the operator.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from neuroforge.stress import (
    DEFAULT_CONFERENCE_PATH,
    ensure_stress_state,
    evaluate_commit,
    public_stress,
    refuse_clinical_action,
)
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

try:
    import pydicom
    from PIL import Image

    from radiology_workstation.app import _dicom_preview_data_url, _safe_dicom_metadata

    IMAGING_AVAILABLE = True
except ImportError:
    IMAGING_AVAILABLE = False

try:
    from medical_imaging_foundation import (
        PREPROCESSING_VERSION,
        SCHEMA_VERSION,
        PreprocessingConfig,
        discover_dicom_studies,
        load_volume,
        preprocess_volume,
    )

    VOLUMETRIC_AVAILABLE = True
except ImportError:
    VOLUMETRIC_AVAILABLE = False

try:
    from radiology_workstation import library as imaging_library

    LIBRARY_AVAILABLE = True
except ImportError:
    imaging_library = None  # type: ignore[assignment]
    LIBRARY_AVAILABLE = False


VAULT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
DATA_ROOT = Path(os.environ.get("NEUROFORGE_DATA_ROOT", VAULT_ROOT / "TFV" / "neuroforge"))
CASES_ROOT = DATA_ROOT / "cases"
EXPORT_ROOT = DATA_ROOT / "exports"
CATALOG_PATH = DATA_ROOT / "catalog.json"
TOPICS_PATH = DATA_ROOT / "saved_topics.json"
MAX_FILE_BYTES = int(os.environ.get("NEUROFORGE_MAX_FILE_BYTES", 1024 * 1024 * 1024))
MAX_VOLUME_VOXELS = int(os.environ.get("NEUROFORGE_MAX_VOLUME_VOXELS", 256 * 1024 * 1024))
ONLINE_TIMEOUT = int(os.environ.get("NEUROFORGE_ONLINE_TIMEOUT", 15))

DIRECT_IDENTIFIER_FIELDS = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientAddress",
    "AccessionNumber",
    "InstitutionName",
    "ReferringPhysicianName",
)
AGENT_HEADINGS = (
    "Case framing",
    "Limitations",
    "Teaching points",
    "Questions to expect",
    "Presenter next checks",
)


class CaseCreateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    clinical_question: str = Field(default="", max_length=2000)
    conference_id: str = Field(default="weekly-neuro-conference", max_length=100)
    allotted_minutes: int = Field(default=15, ge=5, le=90)


class CaseUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    clinical_question: str | None = Field(default=None, max_length=4000)
    narrative: str | None = Field(default=None, max_length=30_000)
    limitations: str | None = Field(default=None, max_length=10_000)
    takeaways: str | None = Field(default=None, max_length=10_000)
    status: str | None = Field(default=None, pattern="^(draft|rehearsal|ready|presented)$")
    allotted_minutes: int | None = Field(default=None, ge=5, le=90)
    active_asset_id: str | None = None
    presentation_order: list[str] | None = None


class KeyImageRequest(BaseModel):
    asset_id: str
    key_image: bool = True
    note: str = Field(default="", max_length=3000)


class LibraryAttachRequest(BaseModel):
    series_id: str = Field(min_length=8, max_length=128)
    timepoint_label: str = Field(default="TCIA library volume", max_length=120)
    preset: str = Field(default="", max_length=16)
    slice_index: int = Field(default=0, ge=0, le=4096)


class EvidenceSearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    mode: str = Field(default="local", pattern="^(local|online)$")
    limit: int = Field(default=8, ge=1, le=20)


class EvidenceDecisionRequest(BaseModel):
    evidence: dict[str, Any]
    decision: str = Field(pattern="^(doctor_approved|rejected)$")


class AgentRequest(BaseModel):
    action: str = Field(default="briefing", pattern="^(briefing|challenge|rehearsal|ask)$")
    question: str = Field(default="", max_length=4000)
    include_active_image: bool = False
    max_tokens: int = Field(default=1800, ge=600, le=2600)


class StressObservationRequest(BaseModel):
    finding: str = Field(min_length=3, max_length=400)
    location: str = Field(default="", max_length=120)


class StressCommitRequest(BaseModel):
    hypothesis: str = Field(min_length=3, max_length=400)
    conference_move: str = Field(min_length=3, max_length=800)
    confidence: int = Field(ge=0, le=100)


class PatientHandoffRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=160)
    clinical_question: str | None = Field(default=None, max_length=2000)
    active_note: str | None = Field(default=None, max_length=10000)
    allotted_minutes: int = Field(default=15, ge=5, le=90)


class SyncToChartRequest(BaseModel):
    patient_id: str | None = Field(default=None, max_length=120)


class SavedTopicRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    cadence: str = Field(default="weekly", pattern="^(manual|weekly)$")


class QuizGenerateRequest(BaseModel):
    audience: str = Field(default="clinical_medical_student", pattern="^(preclinical|clinical_medical_student|advanced_medical_student)$")
    topic: str = Field(default="neuroimaging interpretation", min_length=3, max_length=300)
    count: int = Field(default=5, ge=1, le=8)
    include_key_image: bool = True
    max_tokens: int = Field(default=2400, ge=800, le=3200)


class QuizQuestion(BaseModel):
    question_id: str = Field(default_factory=lambda: "QQ-" + uuid.uuid4().hex[:10])
    prompt: str = Field(min_length=8, max_length=4000)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=4, max_length=5000)
    teaching_point: str = Field(default="", max_length=3000)
    difficulty: str = Field(default="moderate", pattern="^(introductory|moderate|advanced)$")
    asset_id: str = ""
    status: str = Field(default="draft", pattern="^(draft|approved)$")


class QuizSetRequest(BaseModel):
    quiz_id: str = Field(default_factory=lambda: "QUIZ-" + uuid.uuid4().hex[:10])
    title: str = Field(default="Medical Imaging Quiz", min_length=3, max_length=200)
    audience: str = Field(default="clinical_medical_student", max_length=80)
    topic: str = Field(default="neuroimaging interpretation", max_length=300)
    instructions: str = Field(
        default="Select the single best answer for each question. Review the complete imaging study and clinical context when provided.",
        max_length=3000,
    )
    questions: list[QuizQuestion] = Field(min_length=1, max_length=12)
    generation_source: str = "doctor_authored"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8")
    pending.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _case_path(case_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", case_id):
        raise HTTPException(status_code=400, detail="invalid_case_id")
    return CASES_ROOT / case_id / "manifest.json"


def _load_case(case_id: str) -> dict[str, Any]:
    payload = _read_json(_case_path(case_id), None)
    if not payload:
        raise HTTPException(status_code=404, detail="case_not_found")
    return payload


def _save_case(payload: dict[str, Any], event: str = "case_updated") -> dict[str, Any]:
    payload["updated_at"] = _utc_now()
    payload.setdefault("audit", []).append(
        {"timestamp": payload["updated_at"], "event": event, "raw_medical_data_exported": False}
    )
    _write_json(_case_path(str(payload["case_id"])), payload)
    _rebuild_catalog()
    return payload


def _read_catalog() -> list[dict[str, Any]]:
    return _read_json(CATALOG_PATH, [])


def _rebuild_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest in sorted(CASES_ROOT.glob("*/manifest.json")):
        case = _read_json(manifest, {})
        if not case:
            continue
        rows.append(
            {
                "case_id": case.get("case_id"),
                "title": case.get("title"),
                "status": case.get("status", "draft"),
                "allotted_minutes": case.get("allotted_minutes", 15),
                "conference_id": case.get("conference_id", "weekly-neuro-conference"),
                "clinical_question": case.get("clinical_question", ""),
                "source_patient_id": case.get("source_patient_id"),
                "study_count": len(case.get("studies") or []),
                "key_image_count": sum(1 for asset in case.get("assets") or [] if asset.get("key_image")),
                "updated_at": case.get("updated_at"),
            }
        )
    rows.sort(key=lambda row: (row.get("status") != "ready", row.get("title") or ""))
    _write_json(CATALOG_PATH, rows)
    return rows


def _seed_demo_case() -> None:
    CASES_ROOT.mkdir(parents=True, exist_ok=True)
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    if any(CASES_ROOT.glob("*/manifest.json")):
        _rebuild_catalog()
        return

    case_id = "NF-2026-001"
    case_dir = CASES_ROOT / case_id
    preview_dir = case_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    demo_source = STATIC_ROOT / "demo-brain-flair.png"
    demo_preview = preview_dir / "synthetic-flair.png"
    shutil.copy2(demo_source, demo_preview)
    now = _utc_now()
    asset_id = "ASSET-synthetic-flair"
    case = {
        "schema": "neuroforge_case_v1",
        "case_id": case_id,
        "title": "Longitudinal white-matter imaging review",
        "conference_id": "weekly-neuro-conference",
        "status": "ready",
        "allotted_minutes": 18,
        "clinical_question": "How should the interval imaging pattern and its limitations be presented for conference discussion?",
        "narrative": "De-identified demonstration case with progressive neurologic symptoms and serial brain MRI. Review interval change, sequence concordance, and the limits of a selected-image preview before discussing differential considerations.",
        "limitations": "Synthetic demonstration image. Full clinical history, complete sequences, and source report are not included.",
        "takeaways": "Keep image observations separate from interpretation. State what changed, what is uncertain, and what additional context would resolve uncertainty.",
        "conference_path": dict(DEFAULT_CONFERENCE_PATH),
        "stress_test": {
            "schema": "neuroforge_stress_test_v1",
            "observations": [],
            "hypothesis": "",
            "conference_move": "",
            "confidence": None,
            "committed_at": None,
            "runs": [],
        },
        "deidentification": {"attested": True, "mode": "synthetic_demo", "checked_at": now},
        "studies": [
            {"study_id": "STUDY-baseline", "label": "Baseline", "date_label": "T-12 months", "asset_ids": [asset_id]},
            {"study_id": "STUDY-followup", "label": "Follow-up", "date_label": "Current", "asset_ids": [asset_id]},
        ],
        "assets": [
            {
                "asset_id": asset_id,
                "name": "Synthetic axial FLAIR",
                "kind": "image",
                "series_label": "FLAIR",
                "preview_path": "previews/synthetic-flair.png",
                "key_image": True,
                "note": "Selected teaching image; synthetic and not for diagnosis.",
                "metadata": {"modality": "MR", "body_part": "BRAIN", "synthetic": True},
            }
        ],
        "active_asset_id": asset_id,
        "presentation_order": ["question", "timeline", "key_images", "limitations", "evidence", "discussion", "takeaways"],
        "evidence": [
            {
                "evidence_id": "EV-demo-001",
                "title": "MRI lesion characterization: a structured approach",
                "citation": "Synthetic reviewed evidence card for interface testing.",
                "source": "local_demo",
                "source_id": "DEMO-001",
                "state": "local_reviewed",
                "supports_claim": "Use morphology, distribution, sequence behavior, and change over time.",
                "evidence_grade": "educational",
            }
        ],
        "agent_receipts": [],
        "quiz_sets": [],
        "audit": [{"timestamp": now, "event": "demo_case_created", "raw_medical_data_exported": False}],
        "created_at": now,
        "updated_at": now,
    }
    _write_json(case_dir / "manifest.json", case)
    _rebuild_catalog()


def _library_slice_url(asset: dict[str, Any], *, index: int | None = None, preset: str | None = None) -> str:
    series_id = str(asset.get("library_series_id") or "")
    if not series_id:
        return ""
    chosen_index = int(asset.get("slice_index") or 0) if index is None else index
    chosen_preset = str(preset if preset is not None else asset.get("preset") or "mri")
    return (
        f"/api/neuroforge/library/series/{urllib.parse.quote(series_id)}/slice"
        f"?index={chosen_index}&preset={urllib.parse.quote(chosen_preset)}"
    )


def _asset_url(case_id: str, asset: dict[str, Any]) -> str:
    if asset.get("kind") == "library_volume":
        return _library_slice_url(asset)
    rel = str(asset.get("preview_path") or "")
    return f"/api/neuroforge/assets/{case_id}/{urllib.parse.quote(rel)}" if rel else ""


def _public_case(case: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(case))
    for asset in result.get("assets") or []:
        asset["preview_url"] = _asset_url(str(result["case_id"]), asset)
        if asset.get("kind") == "library_volume":
            asset["volume_scrollable"] = True
            asset["use_lane"] = "clinical_academic_research"
        asset.pop("source_path", None)
    result["stress_test"] = public_stress(result)
    for study in result.get("studies") or []:
        if "volumetric_summary" in study:
            study["volumetric_summary"] = _sanitize_volumetric_summary(study["volumetric_summary"])
    return result


def _sanitize_volumetric_summary(summary: Any) -> list[dict[str, Any]]:
    """Keep the volumetric boundary limited to privacy-safe derived values."""
    if not isinstance(summary, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in summary:
        if not isinstance(item, dict):
            continue
        manifest = item.get("manifest")
        safe_manifest: dict[str, Any] = {}
        if isinstance(manifest, dict):
            safe_manifest = {
                "schema_version": str(manifest.get("schema_version", "")),
                "study_id": str(manifest.get("study_id", "")),
                "series": [],
            }
            for series in manifest.get("series") or []:
                if not isinstance(series, dict):
                    continue
                safe_manifest["series"].append(
                    {
                        "schema_version": str(series.get("schema_version", "")),
                        "study_id": str(series.get("study_id", "")),
                        "series_id": str(series.get("series_id", "")),
                        "modality": str(series.get("modality", "")),
                        "rows": int(series.get("rows", 0)),
                        "columns": int(series.get("columns", 0)),
                        "pixel_spacing": [float(value) for value in series.get("pixel_spacing", [])[:2]],
                        "slice_count": int(series.get("slice_count", 0)),
                        "slice_projections": [value for value in series.get("slice_projections", [])],
                        "instance_numbers": [value for value in series.get("instance_numbers", [])],
                    }
                )
        preprocessing = item.get("preprocessing")
        if not isinstance(preprocessing, dict):
            preprocessing = {}
        state = str(item.get("state", "local_only"))
        if state not in {"local_only", "local_only_skipped"}:
            state = "local_only_skipped"
        safe_item = {
                "manifest": safe_manifest,
                "modality": str(item.get("modality", "")),
                "view_shapes": {
                    str(key): [int(value) for value in shape]
                    for key, shape in (item.get("view_shapes") or {}).items()
                    if isinstance(key, str) and isinstance(shape, (list, tuple))
                },
                "foreground_fraction": float(item.get("foreground_fraction", 0.0)),
                "preprocessing": {
                    "schema_version": str(preprocessing.get("schema_version", "")),
                    "preprocessing_version": str(preprocessing.get("preprocessing_version", "")),
                },
                "state": state,
            }
        if state == "local_only_skipped" and item.get("skip_reason") in {"unsupported_modality", "voxel_limit_exceeded"}:
            safe_item["skip_reason"] = item["skip_reason"]
        sanitized.append(safe_item)
    return sanitized


def _library_catalog() -> dict[str, Any]:
    if not LIBRARY_AVAILABLE:
        return {"available": False, "studies": [], "series_count": 0}
    payload = imaging_library.list_studies()
    payload["available"] = True
    return payload


def _library_required() -> None:
    if not LIBRARY_AVAILABLE:
        raise HTTPException(status_code=503, detail="imaging_library_unavailable")


def _safe_asset_path(case_id: str, relative_path: str) -> Path:
    base = (CASES_ROOT / case_id).resolve()
    candidate = (base / relative_path).resolve()
    if base not in candidate.parents:
        raise HTTPException(status_code=400, detail="invalid_asset_path")
    return candidate


def _clean_query(query: str) -> str:
    compact = re.sub(r"\s+", " ", query).strip()
    if re.search(r"\b(patient|mrn|dob|address)\s*[:=]", compact, re.I):
        raise HTTPException(status_code=400, detail="query_may_contain_patient_identifier")
    return compact


def _local_search(query: str, limit: int) -> list[dict[str, Any]]:
    try:
        from vault_search import search_vault

        results = search_vault(query, top_k=limit)
    except Exception:
        results = []
    return [
        {
            "evidence_id": "EV-local-" + hashlib.sha1(str(item).encode()).hexdigest()[:12],
            "title": item.get("title") or item.get("source") or "Local literature result",
            "citation": " | ".join(str(v) for v in (item.get("journal"), item.get("year")) if v),
            "source": "local_vault",
            "source_id": item.get("pmid") or item.get("doi") or item.get("source") or "",
            "state": "local_reviewed",
            "snippet": item.get("snippet") or "",
            "evidence_grade": f"truth_score:{item.get('truth_score', 0)}",
            "retrieved_at": _utc_now(),
        }
        for item in results
    ]


def _pubmed_search(query: str, limit: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"db": "pubmed", "term": query, "retmode": "json", "retmax": limit, "sort": "relevance"}
    )
    with urllib.request.urlopen(
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{params}", timeout=ONLINE_TIMEOUT
    ) as response:
        search = json.load(response)
    ids = search.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    summary_params = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
    with urllib.request.urlopen(
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?{summary_params}", timeout=ONLINE_TIMEOUT
    ) as response:
        root = ET.fromstring(response.read())
    records: list[dict[str, Any]] = []
    for doc in root.findall(".//DocSum"):
        pmid = (doc.findtext("Id") or "").strip()
        fields = {item.attrib.get("Name", ""): "".join(item.itertext()).strip() for item in doc.findall("Item")}
        records.append(
            {
                "evidence_id": f"EV-pubmed-{pmid}",
                "title": fields.get("Title") or f"PubMed {pmid}",
                "citation": " | ".join(v for v in (fields.get("FullJournalName"), fields.get("PubDate")) if v),
                "source": "PubMed",
                "source_id": pmid,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "state": "online_unreviewed",
                "evidence_grade": "ungraded",
                "retrieved_at": _utc_now(),
            }
        )
    return records


def _agent_fallback(case: dict[str, Any], action: str) -> str:
    question = case.get("clinical_question") or "No conference question entered."
    limitation = case.get("limitations") or "Complete study and clinical context were not supplied."
    return (
        f"Case framing: {question}\n"
        f"Limitations: {limitation}\n"
        "Teaching points: Present interval change, sequence concordance, and uncertainty separately.\n"
        f"Questions to expect: What changed? Which sequences support it? What evidence would narrow the discussion? ({action})\n"
        "Presenter next checks: Verify the complete study, source report, key-image order, and every cited claim before conference."
    )


def _agent_response_is_safe(text: str) -> bool:
    lowered = text.lower()
    forbidden = ("hidden reasoning", "chain of thought", "input check", "process the json", "system prompt")
    return all(heading.lower() in lowered for heading in AGENT_HEADINGS) and not any(marker in lowered for marker in forbidden)


def _generate_pdf(case: dict[str, Any]) -> Path:
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    output = EXPORT_ROOT / f"neuroforge_{case['case_id']}_conference.pdf"
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "NFTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=colors.HexColor("#0b1720"), alignment=TA_LEFT
    )
    heading = ParagraphStyle(
        "NFHeading", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=colors.HexColor("#087f78"), spaceAfter=8
    )
    body = ParagraphStyle(
        "NFBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=10.5, leading=15, textColor=colors.HexColor("#22313a")
    )

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#d4dde1"))
        canvas.line(0.55 * inch, 0.46 * inch, 10.45 * inch, 0.46 * inch)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#64747d"))
        canvas.drawString(0.55 * inch, 0.28 * inch, "NeuroForge conference preparation - de-identified research workflow - clinician review required")
        canvas.drawRightString(10.45 * inch, 0.28 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(output), pagesize=landscape(letter), rightMargin=0.6 * inch, leftMargin=0.6 * inch, topMargin=0.55 * inch, bottomMargin=0.62 * inch
    )
    story: list[Any] = [
        Paragraph("NeuroForge", title_style),
        Spacer(1, 0.08 * inch),
        Paragraph(str(case.get("title") or "Conference case"), styles["Heading1"]),
        Spacer(1, 0.16 * inch),
        Paragraph("Clinical question", heading),
        Paragraph(str(case.get("clinical_question") or "Not entered."), body),
        Spacer(1, 0.18 * inch),
        Paragraph("Conference narrative", heading),
        Paragraph(str(case.get("narrative") or "Not entered.").replace("\n", "<br/>"), body),
    ]
    key_assets = [asset for asset in case.get("assets") or [] if asset.get("key_image") and asset.get("preview_path")]
    for asset in key_assets[:6]:
        story.append(PageBreak())
        story.append(Paragraph(str(asset.get("name") or "Key image"), title_style))
        story.append(Spacer(1, 0.12 * inch))
        image_path = _safe_asset_path(str(case["case_id"]), str(asset["preview_path"]))
        if image_path.exists():
            story.append(PdfImage(str(image_path), width=5.9 * inch, height=5.9 * inch, kind="proportional"))
        story.append(Spacer(1, 0.12 * inch))
        story.append(Paragraph(str(asset.get("note") or "Selected de-identified preview."), body))
    story.append(PageBreak())
    story.extend([Paragraph("Limitations", heading), Paragraph(str(case.get("limitations") or "None documented."), body), Spacer(1, 0.18 * inch)])
    story.extend([Paragraph("Reviewed evidence", heading)])
    approved = [item for item in case.get("evidence") or [] if item.get("state") in {"local_reviewed", "doctor_approved"}]
    for item in approved:
        story.append(Paragraph(f"<b>{item.get('title', 'Untitled')}</b><br/>{item.get('citation', '')}<br/>{item.get('supports_claim', '')}", body))
        story.append(Spacer(1, 0.1 * inch))
    story.extend([Spacer(1, 0.1 * inch), Paragraph("Takeaways", heading), Paragraph(str(case.get("takeaways") or "Not entered."), body)])
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output


def _fallback_quiz_questions(
    case: dict[str, Any],
    *,
    audience: str,
    topic: str,
    count: int,
    asset_id: str,
) -> list[dict[str, Any]]:
    limitation = str(case.get("limitations") or "The complete study and clinical context are not available.")
    templates = [
        {
            "prompt": "When reviewing the selected neuroimaging study, which approach best supports a defensible initial description?",
            "options": [
                "Describe sequence, location, morphology, and interval change before narrowing interpretation",
                "Name a single diagnosis before reviewing the remaining sequences",
                "Ignore study limitations if one key image is available",
                "Base the conclusion only on the most visually striking feature",
            ],
            "correct_index": 0,
            "explanation": "A structured observation-first approach reduces premature closure and keeps the image description distinct from interpretation.",
            "teaching_point": "Describe what is visible and how it changes before assigning diagnostic meaning.",
            "difficulty": "introductory",
        },
        {
            "prompt": "Which comparison is most important when presenting a longitudinal MRI case?",
            "options": [
                "Whether the image window settings look identical",
                "Change in lesion distribution, size, signal behavior, and associated mass effect across comparable sequences",
                "Whether each study used the same scanner manufacturer",
                "Only the date of the most recent examination",
            ],
            "correct_index": 1,
            "explanation": "Longitudinal interpretation depends on comparable sequence findings and meaningful interval change, not simply chronology or display settings.",
            "teaching_point": "Compare like sequences and state both true change and limits to comparability.",
            "difficulty": "moderate",
        },
        {
            "prompt": "Which limitation should be stated most clearly before using a selected key image for teaching?",
            "options": [
                "A key image proves the final diagnosis",
                "The complete study, other sequences, and clinical context may change interpretation",
                "Selected images are always more reliable than source images",
                "Limitations are unnecessary in an educational setting",
            ],
            "correct_index": 1,
            "explanation": limitation,
            "teaching_point": "A selected image is a teaching aid, not a substitute for the complete examination.",
            "difficulty": "introductory",
        },
        {
            "prompt": "What is the safest way to use an AI-generated imaging observation in a medical-student quiz?",
            "options": [
                "Treat it as ground truth because the model is local",
                "Publish it without an answer-key rationale",
                "Require physician review and reconcile it with the full study and approved teaching source",
                "Hide uncertainty so the question has one answer",
            ],
            "correct_index": 2,
            "explanation": "Local processing protects data boundaries but does not establish clinical validity. Physician review remains authoritative.",
            "teaching_point": "Local AI output still requires expert review, evidence, and transparent limitations.",
            "difficulty": "moderate",
        },
        {
            "prompt": "Which question stem best tests imaging reasoning rather than simple recall?",
            "options": [
                "What does MRI stand for?",
                "Which observation, sequence behavior, and interval pattern together best support the conclusion?",
                "Who invented computed tomography?",
                "What year was the first MRI scanner installed?",
            ],
            "correct_index": 1,
            "explanation": "A high-quality imaging question asks learners to integrate observations and context rather than recall an isolated definition.",
            "teaching_point": "Test image-based reasoning with evidence visible in the stem or selected image.",
            "difficulty": "moderate",
        },
        {
            "prompt": "Before finalizing this quiz, which quality check has the highest educational value?",
            "options": [
                "Ensure every distractor is longer than the correct answer",
                "Confirm the correct answer is supported, distractors are plausible, and the explanation teaches the intended objective",
                "Remove all limitations from the stem",
                "Use uncommon terminology to increase difficulty",
            ],
            "correct_index": 1,
            "explanation": "The answer, distractors, rationale, and learning objective must align. Difficulty should come from reasoning, not obscurity.",
            "teaching_point": "A good single-best-answer item is valid, focused, and educational even after the answer is revealed.",
            "difficulty": "moderate",
        },
        {
            "prompt": "Which information should remain outside an exported medical-student quiz?",
            "options": [
                "A de-identified selected preview",
                "A reviewed teaching explanation",
                "Direct patient identifiers and raw source paths",
                "The learning objective",
            ],
            "correct_index": 2,
            "explanation": "Educational exports should contain only approved de-identified material and should never expose direct identifiers or local source paths.",
            "teaching_point": "Educational utility does not override patient privacy.",
            "difficulty": "introductory",
        },
        {
            "prompt": "Which closing explanation best supports transfer of learning after an imaging question?",
            "options": [
                "The correct answer is B.",
                "This finding is obvious.",
                "Explain the discriminating imaging features, why the best answer fits, and why the alternatives do not",
                "Ask the learner to memorize the image",
            ],
            "correct_index": 2,
            "explanation": "A useful rationale makes the decision rule visible and addresses the strongest distractors.",
            "teaching_point": "The answer key should teach a reusable interpretation strategy.",
            "difficulty": "advanced",
        },
    ]
    questions: list[dict[str, Any]] = []
    for index, template in enumerate(templates[:count]):
        questions.append(
            {
                "question_id": "QQ-" + uuid.uuid4().hex[:10],
                **template,
                "prompt": template["prompt"] + (f" Focus area: {topic}." if index == 0 else ""),
                "asset_id": asset_id if asset_id and index < 3 else "",
                "status": "draft",
                "audience": audience,
            }
        )
    return questions


def _parse_quiz_questions(text: str, count: int, asset_id: str) -> list[dict[str, Any]]:
    candidate = text.strip()
    match = re.search(r"\[[\s\S]*\]", candidate)
    if not match:
        return []
    try:
        rows = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    validated: list[dict[str, Any]] = []
    for row in rows[:count]:
        if not isinstance(row, dict):
            return []
        options = row.get("options")
        if not isinstance(options, list) or len(options) != 4:
            return []
        try:
            item = QuizQuestion(
                prompt=str(row.get("prompt") or ""),
                options=[str(option) for option in options],
                correct_index=int(row.get("correct_index")),
                explanation=str(row.get("explanation") or ""),
                teaching_point=str(row.get("teaching_point") or ""),
                difficulty=str(row.get("difficulty") or "moderate").lower(),
                asset_id=asset_id if bool(row.get("use_key_image", True)) else "",
            )
        except Exception:
            return []
        validated.append(item.model_dump())
    return validated if len(validated) == count else []


def _generate_quiz_set(case: dict[str, Any], payload: QuizGenerateRequest) -> dict[str, Any]:
    key_assets = [asset for asset in case.get("assets") or [] if asset.get("key_image")]
    asset_id = str(key_assets[0].get("asset_id") or "") if payload.include_key_image and key_assets else ""
    image_context = [
        {
            "asset_id": asset.get("asset_id"),
            "name": asset.get("name"),
            "series_label": asset.get("series_label"),
            "metadata": asset.get("metadata") or {},
            "note": asset.get("note") or "",
        }
        for asset in key_assets[:3]
    ]
    prompt = (
        "Create a medical imaging quiz for medical students inside a local research workstation. "
        "Return JSON only: an array of exactly "
        f"{payload.count} objects with prompt, options (exactly four strings), correct_index (0-3), "
        "explanation, teaching_point, difficulty (introductory, moderate, or advanced), and use_key_image. "
        "Questions must be single-best-answer, educationally valid, non-trick, and answerable from the supplied "
        "case context or general imaging principles. Do not invent patient facts, diagnoses, citations, or hidden reasoning.\n\n"
        f"Audience: {payload.audience}\nTopic: {payload.topic}\n"
        f"Case question: {case.get('clinical_question')}\nNarrative: {case.get('narrative')}\n"
        f"Limitations: {case.get('limitations')}\nKey-image metadata: {json.dumps(image_context, ensure_ascii=True)}"
    )
    model_status = "fallback"
    generation_source = "safe_fallback"
    questions: list[dict[str, Any]] = []
    try:
        from forge_moe_registry import query_moe_adapter

        result = query_moe_adapter(prompt, max_tokens=payload.max_tokens, temperature=0.0, timeout=180)
        model_status = str(result.get("status") or "unknown")
        questions = _parse_quiz_questions(str(result.get("response") or ""), payload.count, asset_id)
        if questions:
            generation_source = "agent_validated"
    except Exception:
        model_status = "fallback"
    if not questions:
        questions = _fallback_quiz_questions(
            case,
            audience=payload.audience,
            topic=payload.topic,
            count=payload.count,
            asset_id=asset_id,
        )
    now = _utc_now()
    quiz = {
        "schema": "neuroforge_imaging_quiz_v1",
        "quiz_id": "QUIZ-" + uuid.uuid4().hex[:10],
        "title": f"{case.get('title') or 'Medical Imaging'} - Student Quiz",
        "audience": payload.audience,
        "topic": payload.topic,
        "instructions": "Select the single best answer for each question. Review the supplied image and stem carefully.",
        "questions": questions,
        "generation_source": generation_source,
        "model_status": model_status,
        "created_at": now,
        "updated_at": now,
        "requires_physician_review": True,
    }
    return quiz


def _next_numbering_id(numbering: Any, tag: str) -> int:
    values = []
    for element in numbering.findall(qn(tag)):
        raw = element.get(qn("w:abstractNumId" if tag == "w:abstractNum" else "w:numId"))
        if raw and raw.isdigit():
            values.append(int(raw))
    return max(values, default=0) + 1


def _add_word_numbering(document: Document, *, fmt: str, level_text: str) -> int:
    numbering = document.part.numbering_part.element
    abstract_id = _next_numbering_id(numbering, "w:abstractNum")
    num_id = _next_numbering_id(numbering, "w:num")
    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    multi = OxmlElement("w:multiLevelType")
    multi.set(qn("w:val"), "singleLevel")
    abstract.append(multi)
    level = OxmlElement("w:lvl")
    level.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:start")
    start.set(qn("w:val"), "1")
    level.append(start)
    num_fmt = OxmlElement("w:numFmt")
    num_fmt.set(qn("w:val"), fmt)
    level.append(num_fmt)
    text = OxmlElement("w:lvlText")
    text.set(qn("w:val"), level_text)
    level.append(text)
    suffix = OxmlElement("w:suff")
    suffix.set(qn("w:val"), "tab")
    level.append(suffix)
    ppr = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "num")
    tab.set(qn("w:pos"), "540")
    tabs.append(tab)
    ppr.append(tabs)
    indent = OxmlElement("w:ind")
    indent.set(qn("w:left"), "540")
    indent.set(qn("w:hanging"), "280")
    ppr.append(indent)
    level.append(ppr)
    abstract.append(level)
    numbering.append(abstract)
    num = OxmlElement("w:num")
    num.set(qn("w:numId"), str(num_id))
    abstract_ref = OxmlElement("w:abstractNumId")
    abstract_ref.set(qn("w:val"), str(abstract_id))
    num.append(abstract_ref)
    numbering.append(num)
    return num_id


def _apply_word_numbering(paragraph: Any, num_id: int) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    num_pr = OxmlElement("w:numPr")
    level = OxmlElement("w:ilvl")
    level.set(qn("w:val"), "0")
    number = OxmlElement("w:numId")
    number.set(qn("w:val"), str(num_id))
    num_pr.append(level)
    num_pr.append(number)
    ppr.append(num_pr)


def _set_docx_run_font(run: Any, *, size: float | None = None, bold: bool | None = None, color: str | None = None) -> None:
    run.font.name = "Calibri"
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Calibri")
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Calibri")
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def _configure_quiz_document(document: Document) -> None:
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.right_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25
    specs = {
        "Title": (24, "10212A", 0, 6),
        "Heading 1": (16, "0F7F77", 18, 10),
        "Heading 2": (13, "0F7F77", 14, 7),
        "Heading 3": (12, "294A59", 10, 5),
    }
    for style_name, (size, color, before, after) in specs.items():
        style = document.styles[style_name]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
        style._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(color)
        style.font.bold = style_name != "Title"
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        if style_name == "Title":
            style_ppr = style._element.get_or_add_pPr()
            border = style_ppr.find(qn("w:pBdr"))
            if border is not None:
                style_ppr.remove(border)
    if "Quiz Metadata" not in document.styles:
        meta = document.styles.add_style("Quiz Metadata", WD_STYLE_TYPE.PARAGRAPH)
        meta.base_style = normal
        meta.font.name = "Calibri"
        meta.font.size = Pt(10)
        meta.font.color.rgb = RGBColor.from_string("63747D")
        meta.paragraph_format.space_after = Pt(3)
    header = section.header.paragraphs[0]
    header.text = "NeuroForge Quiz Studio"
    _set_docx_run_font(header.runs[0], size=9, bold=True, color="63747D")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_run = footer.add_run("Medical imaging education | De-identified research workflow | ")
    _set_docx_run_font(footer_run, size=8, color="63747D")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)


def _build_quiz_docx(case: dict[str, Any], quiz: dict[str, Any]) -> Path:
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    output = EXPORT_ROOT / f"neuroforge_{case['case_id']}_{quiz['quiz_id']}_quiz.docx"
    document = Document()
    _configure_quiz_document(document)
    document.core_properties.title = str(quiz.get("title") or "Medical Imaging Quiz")
    document.core_properties.author = "NeuroForge"
    document.core_properties.subject = "De-identified medical imaging education"
    title = document.add_paragraph(style="Title")
    title.add_run(str(quiz.get("title") or "Medical Imaging Quiz"))
    subtitle = document.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(14)
    run = subtitle.add_run("Medical Imaging Quiz | Student Copy and Answer Key")
    _set_docx_run_font(run, size=13, bold=True, color="0F7F77")
    metadata = [
        ("Audience", str(quiz.get("audience") or "").replace("_", " ").title()),
        ("Topic", str(quiz.get("topic") or "")),
        ("Case", str(case.get("case_id") or "")),
        ("Prepared", datetime.now().strftime("%B %d, %Y")),
    ]
    for label, value in metadata:
        paragraph = document.add_paragraph(style="Quiz Metadata")
        label_run = paragraph.add_run(f"{label}: ")
        _set_docx_run_font(label_run, size=10, bold=True, color="294A59")
        value_run = paragraph.add_run(value)
        _set_docx_run_font(value_run, size=10, color="63747D")
    document.add_paragraph("Instructions", style="Heading 1")
    document.add_paragraph(str(quiz.get("instructions") or "Select the single best answer."))
    document.add_paragraph("Questions", style="Heading 1")

    assets = {str(asset.get("asset_id")): asset for asset in case.get("assets") or []}
    rendered_asset_ids: set[str] = set()
    for index, question in enumerate(quiz.get("questions") or [], start=1):
        heading = document.add_paragraph(style="Heading 2")
        heading.paragraph_format.keep_with_next = True
        heading.add_run(f"Question {index}")
        asset = assets.get(str(question.get("asset_id") or ""))
        asset_id = str(question.get("asset_id") or "")
        if asset and asset.get("preview_path") and asset_id not in rendered_asset_ids:
            rendered_asset_ids.add(asset_id)
            image_path = _safe_asset_path(str(case["case_id"]), str(asset["preview_path"]))
            if image_path.exists():
                image_paragraph = document.add_paragraph()
                image_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                image_paragraph.paragraph_format.keep_with_next = True
                image_paragraph.add_run().add_picture(str(image_path), width=Inches(3.7))
                caption = document.add_paragraph()
                caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                caption.paragraph_format.space_after = Pt(7)
                caption_run = caption.add_run("Selected de-identified teaching image")
                _set_docx_run_font(caption_run, size=9, color="63747D")
        stem = document.add_paragraph()
        stem.paragraph_format.keep_with_next = True
        stem_run = stem.add_run(str(question.get("prompt") or ""))
        _set_docx_run_font(stem_run, size=11, bold=True, color="10212A")
        alpha_num_id = _add_word_numbering(document, fmt="upperLetter", level_text="%1.")
        options = question.get("options") or []
        for option_index, option in enumerate(options):
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(4)
            paragraph.paragraph_format.line_spacing = 1.25
            paragraph.paragraph_format.keep_with_next = option_index < len(options) - 1
            _apply_word_numbering(paragraph, alpha_num_id)
            paragraph.add_run(str(option))

    document.add_page_break()
    document.add_paragraph("Answer Key", style="Title")
    note = document.add_paragraph()
    note_run = note.add_run("For instructor use. Confirm every item and rationale before distribution.")
    _set_docx_run_font(note_run, size=10, bold=True, color="A96813")
    for index, question in enumerate(quiz.get("questions") or [], start=1):
        heading = document.add_paragraph(f"Question {index}", style="Heading 2")
        heading.paragraph_format.keep_with_next = True
        correct_index = int(question.get("correct_index") or 0)
        options = question.get("options") or []
        answer = options[correct_index] if 0 <= correct_index < len(options) else "Not set"
        answer_paragraph = document.add_paragraph()
        answer_label = answer_paragraph.add_run(f"Correct answer: {chr(65 + correct_index)}. ")
        _set_docx_run_font(answer_label, bold=True, color="0F7F77")
        answer_paragraph.add_run(str(answer))
        document.add_paragraph(str(question.get("explanation") or ""))
        teaching = str(question.get("teaching_point") or "").strip()
        if teaching:
            teaching_paragraph = document.add_paragraph()
            label = teaching_paragraph.add_run("Teaching point: ")
            _set_docx_run_font(label, bold=True, color="294A59")
            teaching_paragraph.add_run(teaching)
    document.save(output)
    return output


@asynccontextmanager
async def lifespan(_: FastAPI):
    _seed_demo_case()
    yield


app = FastAPI(
    title="NeuroForge Medical Conference Organizer",
    version="0.1.0",
    description="Local de-identified neuro-imaging conference preparation workspace.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "surface": "neuroforge",
        "local_only": True,
        "case_count": len(_read_catalog()),
        "imaging_available": IMAGING_AVAILABLE,
        "library_available": LIBRARY_AVAILABLE,
        "use_lane": "clinical_academic_research",
    }


@app.get("/api/neuroforge/bootstrap")
def bootstrap() -> dict[str, Any]:
    catalog = _read_catalog() or _rebuild_catalog()
    active = _public_case(_load_case(str(catalog[0]["case_id"]))) if catalog else None
    return {
        "schema": "neuroforge_bootstrap_v1",
        "conference": {
            "conference_id": "weekly-neuro-conference",
            "title": "Weekly Neuro Conference",
            "scheduled_label": "Thursday 12:00 PM",
            "case_ids": [row["case_id"] for row in catalog],
        },
        "cases": catalog,
        "active_case": active,
        "saved_topics": _read_json(TOPICS_PATH, []),
        "online_policy": "operator_query_only_no_case_context",
        "use_lane": "clinical_academic_research",
        "library": _library_catalog(),
    }


@app.post("/api/neuroforge/cases")
def create_case(payload: CaseCreateRequest) -> dict[str, Any]:
    case_id = "NF-" + uuid.uuid4().hex[:10]
    now = _utc_now()
    case = {
        "schema": "neuroforge_case_v1",
        "case_id": case_id,
        "title": payload.title,
        "conference_id": payload.conference_id,
        "status": "draft",
        "allotted_minutes": payload.allotted_minutes,
        "clinical_question": payload.clinical_question,
        "narrative": "",
        "limitations": "",
        "takeaways": "",
        "deidentification": {"attested": False},
        "studies": [],
        "assets": [],
        "active_asset_id": None,
        "presentation_order": ["question", "timeline", "key_images", "limitations", "evidence", "discussion", "takeaways"],
        "evidence": [],
        "agent_receipts": [],
        "quiz_sets": [],
        "conference_path": dict(DEFAULT_CONFERENCE_PATH),
        "stress_test": {
            "schema": "neuroforge_stress_test_v1",
            "observations": [],
            "hypothesis": "",
            "conference_move": "",
            "confidence": None,
            "committed_at": None,
            "runs": [],
        },
        "audit": [{"timestamp": now, "event": "case_created", "raw_medical_data_exported": False}],
        "created_at": now,
        "updated_at": now,
    }
    _write_json(_case_path(case_id), case)
    _rebuild_catalog()
    return _public_case(case)


@app.get("/api/neuroforge/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    return _public_case(_load_case(case_id))


@app.put("/api/neuroforge/cases/{case_id}")
def update_case(case_id: str, payload: CaseUpdateRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    for key, value in payload.model_dump(exclude_none=True).items():
        case[key] = value
    return _public_case(_save_case(case))


@app.post("/api/neuroforge/cases/{case_id}/studies")
async def import_study(
    case_id: str,
    files: list[UploadFile] = File(...),
    timepoint_label: str = Form("Current"),
    deidentified_attestation: bool = Form(False),
) -> dict[str, Any]:
    if not deidentified_attestation:
        raise HTTPException(status_code=400, detail="deidentification_attestation_required")
    case = _load_case(case_id)
    case_dir = CASES_ROOT / case_id
    source_dir = case_dir / "studies" / uuid.uuid4().hex[:10]
    preview_dir = case_dir / "previews"
    source_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)
    study_id = "STUDY-" + uuid.uuid4().hex[:10]
    asset_ids: list[str] = []
    rejected: list[dict[str, str]] = []
    dicom_asset_ids: set[str] = set()
    dicom_files: list[tuple[Path, Path]] = []

    for upload in files:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(upload.filename or "study_file").name)[:180]
        target = source_dir / safe_name
        size = 0
        try:
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(status_code=413, detail=f"file_too_large:{safe_name}")
                    output.write(chunk)
            suffix = target.suffix.lower()
            asset_id = "ASSET-" + uuid.uuid4().hex[:12]
            metadata: dict[str, Any] = {}
            preview_path = ""
            kind = "dicom" if suffix == ".dcm" or "dicom" in (upload.content_type or "") else "image"
            if kind == "dicom":
                if not IMAGING_AVAILABLE:
                    raise ValueError("imaging_runtime_unavailable")
                dataset = pydicom.dcmread(str(target), stop_before_pixels=True, force=False)
                present = [field for field in DIRECT_IDENTIFIER_FIELDS if str(getattr(dataset, field, "") or "").strip()]
                if present:
                    raise ValueError("direct_identifiers_present:" + ",".join(present))
                metadata = _safe_dicom_metadata(dataset, 0)
                full_dataset = pydicom.dcmread(str(target), force=False)
                data_url, _, _ = _dicom_preview_data_url(full_dataset, 0)
                preview_name = f"{asset_id}.jpg"
                preview_path = f"previews/{preview_name}"
                (preview_dir / preview_name).write_bytes(__import__("base64").b64decode(data_url.split(",", 1)[1]))
            else:
                with Image.open(target) as image:
                    image.verify()
                with Image.open(target) as image:
                    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                    preview_name = f"{asset_id}.jpg"
                    preview_path = f"previews/{preview_name}"
                    image.convert("L").save(preview_dir / preview_name, format="JPEG", quality=90)
                    metadata = {"width": image.width, "height": image.height, "modality": "IMAGE"}
            asset = {
                "asset_id": asset_id,
                "name": safe_name,
                "kind": kind,
                "series_label": str(metadata.get("modality") or kind).upper(),
                "source_path": str(target.relative_to(case_dir)),
                "preview_path": preview_path,
                "size_bytes": size,
                "key_image": False,
                "note": "",
                "metadata": metadata,
            }
            case.setdefault("assets", []).append(asset)
            asset_ids.append(asset_id)
            if kind == "dicom":
                dicom_asset_ids.add(asset_id)
                dicom_files.append((target, preview_dir / preview_name))
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            target.unlink(missing_ok=True)
            rejected.append({"name": safe_name, "reason": str(exc)[:240]})

    volumetric_summary: list[dict[str, Any]] = []
    if dicom_asset_ids and VOLUMETRIC_AVAILABLE:
        try:
            discovered_studies = discover_dicom_studies(source_dir)
        except ValueError as exc:
            if "inconsistent geometry" not in str(exc).lower():
                rejected.append({"name": source_dir.name, "reason": "dicom_discovery_failed"})
            else:
                for target, preview in dicom_files:
                    target.unlink(missing_ok=True)
                    preview.unlink(missing_ok=True)
                case["assets"] = [asset for asset in case.get("assets", []) if asset.get("asset_id") not in dicom_asset_ids]
                asset_ids[:] = [asset_id for asset_id in asset_ids if asset_id not in dicom_asset_ids]
                rejected.append({"name": source_dir.name, "reason": "inconsistent_geometry"})
                discovered_studies = ()
        for discovered in discovered_studies:
            for series in discovered.series:
                modality = str(series.modality).upper()
                manifest = discovered.to_dict()
                item: dict[str, Any] = {
                    "manifest": manifest,
                    "modality": modality,
                    "view_shapes": {},
                    "foreground_fraction": 0.0,
                    "preprocessing": {"schema_version": SCHEMA_VERSION, "preprocessing_version": PREPROCESSING_VERSION},
                    "state": "local_only_skipped",
                }
                if modality not in {"MRI", "CT", "MR"}:
                    item["skip_reason"] = "unsupported_modality"
                else:
                    voxel_count = series.rows * series.columns * series.slice_count
                    if voxel_count > MAX_VOLUME_VOXELS:
                        item["skip_reason"] = "voxel_limit_exceeded"
                    else:
                        volume, _ = load_volume(series)
                        config = PreprocessingConfig("MRI" if modality in {"MR", "MRI"} else "CT")
                        processed = preprocess_volume(volume, config)
                        item["view_shapes"] = {name: list(view.shape) for name, view in processed.views.items()}
                        item["foreground_fraction"] = float(processed.foreground_mask.mean())
                        item["state"] = "local_only"
                volumetric_summary.append(_sanitize_volumetric_summary([item])[0])
    elif dicom_asset_ids:
        volumetric_summary = [
            {
                "manifest": {},
                "modality": "UNKNOWN",
                "view_shapes": {},
                "foreground_fraction": 0.0,
                "preprocessing": {"schema_version": "", "preprocessing_version": ""},
                "state": "local_only_skipped",
            }
        ]

    if asset_ids:
        case.setdefault("studies", []).append(
            {
                "study_id": study_id,
                "label": timepoint_label[:120],
                "date_label": timepoint_label[:120],
                "asset_ids": asset_ids,
                "volumetric_summary": _sanitize_volumetric_summary(volumetric_summary),
            }
        )
        case["active_asset_id"] = case.get("active_asset_id") or asset_ids[0]
        case["deidentification"] = {"attested": True, "checked_at": _utc_now(), "direct_identifier_gate": "passed"}
        _save_case(case, event="study_imported")
    return {"schema": "neuroforge_study_import_v1", "case": _public_case(case), "imported": len(asset_ids), "rejected": rejected}


@app.get("/api/neuroforge/library/studies")
def library_studies() -> dict[str, Any]:
    _library_required()
    return _library_catalog()


@app.get("/api/neuroforge/library/series/{series_id}/meta")
def library_series_meta(series_id: str) -> dict[str, Any]:
    _library_required()
    try:
        return imaging_library.series_meta(series_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown_series") from exc


@app.get("/api/neuroforge/library/series/{series_id}/slice")
def library_series_slice(
    series_id: str,
    index: int = Query(default=0, ge=0, le=4096),
    preset: str = Query(default=""),
):
    _library_required()
    try:
        png, meta = imaging_library.render_slice_png(series_id, index, preset=preset or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown_series") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Slice-Index": str(meta["index"]),
            "X-Slice-Count": str(meta["slice_count"]),
            "X-Window-Preset": str(meta["window"]["preset"]),
            "X-Use-Lane": "clinical_academic_research",
        },
    )


@app.post("/api/neuroforge/cases/{case_id}/library-series")
def attach_library_series(case_id: str, payload: LibraryAttachRequest) -> dict[str, Any]:
    _library_required()
    try:
        meta = imaging_library.series_meta(payload.series_id)
        png, _slice_meta = imaging_library.render_slice_png(
            payload.series_id,
            min(payload.slice_index, max(0, int(meta["slice_count"]) - 1)),
            preset=payload.preset or meta.get("default_preset"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown_series") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    case = _load_case(case_id)
    case_dir = CASES_ROOT / case_id
    preview_dir = case_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    asset_id = "ASSET-" + uuid.uuid4().hex[:12]
    preview_name = f"{asset_id}.png"
    (preview_dir / preview_name).write_bytes(png)
    preset = payload.preset or meta.get("default_preset") or "mri"
    slice_index = min(payload.slice_index, max(0, int(meta["slice_count"]) - 1))
    asset = {
        "asset_id": asset_id,
        "name": f"{meta['modality']} library series",
        "kind": "library_volume",
        "series_label": f"{meta['modality']} · {meta['slice_count']} slices",
        "library_series_id": meta["series_id"],
        "library_study_id": meta["study_id"],
        "slice_index": slice_index,
        "slice_count": int(meta["slice_count"]),
        "preset": preset,
        "preview_path": f"previews/{preview_name}",
        "key_image": False,
        "note": "TCIA public archive volume. Clinical academic research — not current care.",
        "metadata": {
            "modality": meta["modality"],
            "rows": meta["rows"],
            "columns": meta["columns"],
            "pixel_spacing_mm": meta["pixel_spacing_mm"],
            "data_origin": "public_research_archive",
            "use_lane": "clinical_academic_research",
            "attribution": meta.get("attribution") or imaging_library.ATTRIBUTION,
        },
    }
    case.setdefault("assets", []).append(asset)
    study_id = "STUDY-" + uuid.uuid4().hex[:10]
    case.setdefault("studies", []).append(
        {
            "study_id": study_id,
            "label": payload.timepoint_label[:120],
            "date_label": payload.timepoint_label[:120],
            "asset_ids": [asset_id],
            "library_series_id": meta["series_id"],
            "volumetric_summary": [
                {
                    "manifest": {
                        "schema_version": "1.0",
                        "study_id": meta["study_id"],
                        "series": [
                            {
                                "schema_version": "1.0",
                                "study_id": meta["study_id"],
                                "series_id": meta["series_id"],
                                "modality": meta["modality"],
                                "rows": meta["rows"],
                                "columns": meta["columns"],
                                "pixel_spacing": meta["pixel_spacing_mm"],
                                "slice_count": meta["slice_count"],
                                "slice_projections": [],
                                "instance_numbers": [],
                            }
                        ],
                    },
                    "modality": meta["modality"],
                    "view_shapes": {},
                    "foreground_fraction": 0.0,
                    "preprocessing": {"schema_version": "1.0", "preprocessing_version": "1.0"},
                    "state": "local_only",
                }
            ],
        }
    )
    case["active_asset_id"] = asset_id
    case["deidentification"] = {
        "attested": True,
        "mode": "public_research_archive",
        "checked_at": _utc_now(),
        "direct_identifier_gate": "library_pseudonymized",
    }
    saved = _public_case(_save_case(case, event="library_series_attached"))
    return {
        "schema": "neuroforge_library_attach_v1",
        "use_lane": "clinical_academic_research",
        "clinical_use_enabled": False,
        "case": saved,
        "asset_id": asset_id,
    }


@app.get("/api/neuroforge/assets/{case_id}/{relative_path:path}")
def asset(case_id: str, relative_path: str) -> FileResponse:
    path = _safe_asset_path(case_id, relative_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="asset_not_found")
    return FileResponse(path)


@app.post("/api/neuroforge/cases/{case_id}/key-image")
def key_image(case_id: str, payload: KeyImageRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    for asset_item in case.get("assets") or []:
        if asset_item.get("asset_id") == payload.asset_id:
            asset_item["key_image"] = payload.key_image
            asset_item["note"] = payload.note
            return _public_case(_save_case(case, event="key_image_updated"))
    raise HTTPException(status_code=404, detail="asset_not_found")


@app.post("/api/neuroforge/evidence/search")
def evidence_search(payload: EvidenceSearchRequest) -> dict[str, Any]:
    query = _clean_query(payload.query)
    try:
        results = _local_search(query, payload.limit) if payload.mode == "local" else _pubmed_search(query, payload.limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"evidence_search_failed:{type(exc).__name__}") from exc
    return {
        "schema": "neuroforge_evidence_search_v1",
        "query": query,
        "mode": payload.mode,
        "results": results,
        "raw_medical_data_exported": False,
        "transmitted_fields": ["operator_query"] if payload.mode == "online" else [],
    }


@app.post("/api/neuroforge/cases/{case_id}/evidence")
def decide_evidence(case_id: str, payload: EvidenceDecisionRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    evidence = json.loads(json.dumps(payload.evidence))
    evidence["state"] = payload.decision
    evidence["reviewed_at"] = _utc_now()
    evidence_id = evidence.get("evidence_id") or "EV-" + uuid.uuid4().hex[:12]
    evidence["evidence_id"] = evidence_id
    existing = [item for item in case.get("evidence") or [] if item.get("evidence_id") != evidence_id]
    case["evidence"] = existing + [evidence]
    return _public_case(_save_case(case, event=f"evidence_{payload.decision}"))


@app.post("/api/neuroforge/cases/{case_id}/stress/observations")
def add_stress_observation(case_id: str, payload: StressObservationRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    if refuse_clinical_action(payload.finding):
        raise HTTPException(status_code=400, detail="clinical_action_not_allowed")
    state = ensure_stress_state(case)
    observation = {
        "observation_id": "OBS-" + uuid.uuid4().hex[:10],
        "finding": payload.finding.strip(),
        "location": payload.location.strip(),
        "recorded_at": _utc_now(),
    }
    state["observations"].append(observation)
    saved = _public_case(_save_case(case, event="stress_observation_recorded"))
    return {"schema": "neuroforge_stress_observation_v1", "observation": observation, "case": saved}


@app.delete("/api/neuroforge/cases/{case_id}/stress/observations/{observation_id}")
def delete_stress_observation(case_id: str, observation_id: str) -> dict[str, Any]:
    case = _load_case(case_id)
    state = ensure_stress_state(case)
    before = len(state["observations"])
    state["observations"] = [
        item for item in state["observations"] if item.get("observation_id") != observation_id
    ]
    if len(state["observations"]) == before:
        raise HTTPException(status_code=404, detail="observation_not_found")
    return _public_case(_save_case(case, event="stress_observation_removed"))


@app.post("/api/neuroforge/cases/{case_id}/stress/commit")
def commit_stress_test(case_id: str, payload: StressCommitRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    if refuse_clinical_action(f"{payload.hypothesis} {payload.conference_move}"):
        raise HTTPException(status_code=400, detail="clinical_action_not_allowed")
    debrief = evaluate_commit(
        case,
        hypothesis=payload.hypothesis.strip(),
        conference_move=payload.conference_move.strip(),
        confidence=payload.confidence,
    )
    state = ensure_stress_state(case)
    state["hypothesis"] = payload.hypothesis.strip()
    state["conference_move"] = payload.conference_move.strip()
    state["confidence"] = payload.confidence
    state["committed_at"] = _utc_now()
    state["runs"].append(debrief)
    saved = _public_case(_save_case(case, event="stress_test_committed"))
    return {"schema": "neuroforge_stress_commit_v1", "debrief": debrief, "case": saved}


@app.post("/api/neuroforge/handoff/patient-chart")
def handoff_patient_chart(payload: PatientHandoffRequest) -> dict[str, Any]:
    from forge_neuroforge_bridge import push_patient_to_neuroforge
    bridge_res = push_patient_to_neuroforge(
        payload.patient_id,
        title=payload.title,
        clinical_question=payload.clinical_question,
        active_note=payload.active_note,
        allotted_minutes=payload.allotted_minutes,
    )
    case = _load_case(bridge_res["case_id"])
    return {"schema": "neuroforge_patient_handoff_v1", "bridge": bridge_res, "case": _public_case(case)}


@app.post("/api/neuroforge/cases/{case_id}/sync-to-chart")
def sync_case_to_patient_chart(case_id: str, payload: SyncToChartRequest) -> dict[str, Any]:
    from forge_neuroforge_bridge import sync_neuroforge_to_chart
    try:
        res = sync_neuroforge_to_chart(case_id, patient_id=payload.patient_id)
        return {"schema": "neuroforge_chart_sync_v1", **res}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="case_not_found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/neuroforge/cases/{case_id}/agent")
def agent_consult(case_id: str, payload: AgentRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    evidence = [item for item in case.get("evidence") or [] if item.get("state") in {"local_reviewed", "doctor_approved"}]
    stress = public_stress(case)
    observation_lines = "; ".join(
        str(item.get("finding") or "") for item in stress.get("observations") or []
    ) or "none recorded"
    prompt = (
        "You are a local NeuroForge conference-preparation agent. Return final visible text only using exactly these headings: "
        + ", ".join(AGENT_HEADINGS)
        + ". Do not expose hidden reasoning. Do not claim diagnostic authority. Do not invent citations. "
        "The doctor remains the final reviewer.\n\n"
        f"Action: {payload.action}\nQuestion: {payload.question}\n"
        f"Case title: {case.get('title')}\nClinical question: {case.get('clinical_question')}\n"
        f"Narrative: {case.get('narrative')}\nLimitations: {case.get('limitations')}\n"
        f"Doctor observations: {observation_lines}\n"
        f"Pinned working hypothesis: {stress.get('hypothesis') or 'not pinned'}\n"
        f"Conference move: {stress.get('conference_move') or 'not committed'}\n"
        f"Reviewed evidence: {json.dumps(evidence, ensure_ascii=True)[:12000]}"
    )
    model_result: dict[str, Any] = {}
    response = ""
    try:
        from forge_moe_registry import query_moe_adapter

        model_result = query_moe_adapter(prompt, max_tokens=payload.max_tokens, temperature=0.0, timeout=180)
        response = str(model_result.get("response") or "").strip()
    except Exception as exc:
        model_result = {"status": "fallback", "error": type(exc).__name__}
    validation = "validated"
    if not _agent_response_is_safe(response):
        validation = "safe_fallback"
        response = _agent_fallback(case, payload.action)
    receipt = {
        "receipt_id": "AR-" + uuid.uuid4().hex[:12],
        "timestamp": _utc_now(),
        "action": payload.action,
        "model_status": model_result.get("status", "unknown"),
        "validation": validation,
        "evidence_ids": [item.get("evidence_id") for item in evidence],
        "input_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "output_hash": hashlib.sha256(response.encode()).hexdigest(),
        "active_image_attached": bool(payload.include_active_image),
        "raw_medical_data_exported": False,
    }
    case.setdefault("agent_receipts", []).append(receipt)
    _save_case(case, event="agent_consult_completed")
    return {"schema": "neuroforge_agent_receipt_v1", "response": response, "receipt": receipt}


@app.get("/api/neuroforge/topics")
def topics() -> dict[str, Any]:
    return {"schema": "neuroforge_saved_topics_v1", "topics": _read_json(TOPICS_PATH, [])}


@app.post("/api/neuroforge/topics")
def save_topic(payload: SavedTopicRequest) -> dict[str, Any]:
    rows = _read_json(TOPICS_PATH, [])
    query = _clean_query(payload.query)
    row = {
        "topic_id": "TOPIC-" + uuid.uuid4().hex[:10],
        "query": query,
        "cadence": payload.cadence,
        "last_run_at": None,
        "next_run_policy": "operator_or_weekly_when_app_active",
        "enabled": True,
    }
    rows.append(row)
    _write_json(TOPICS_PATH, rows)
    return row


@app.post("/api/neuroforge/topics/{topic_id}/refresh")
def refresh_topic(topic_id: str) -> dict[str, Any]:
    rows = _read_json(TOPICS_PATH, [])
    topic = next((item for item in rows if item.get("topic_id") == topic_id), None)
    if not topic:
        raise HTTPException(status_code=404, detail="saved_topic_not_found")
    try:
        candidates = _pubmed_search(str(topic.get("query") or ""), 8)
    except Exception as exc:
        topic["last_error"] = type(exc).__name__
        topic["last_run_at"] = _utc_now()
        _write_json(TOPICS_PATH, rows)
        raise HTTPException(status_code=502, detail=f"topic_refresh_failed:{type(exc).__name__}") from exc
    topic["last_run_at"] = _utc_now()
    topic["last_error"] = ""
    topic["candidate_count"] = len(candidates)
    topic["candidates"] = candidates
    _write_json(TOPICS_PATH, rows)
    return {
        "schema": "neuroforge_saved_topic_refresh_v1",
        "topic": topic,
        "results": candidates,
        "raw_medical_data_exported": False,
        "transmitted_fields": ["saved_topic_query"],
    }


@app.post("/api/neuroforge/cases/{case_id}/quiz/generate")
def generate_quiz(case_id: str, payload: QuizGenerateRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    quiz = _generate_quiz_set(case, payload)
    case.setdefault("quiz_sets", []).append(quiz)
    _save_case(case, event="imaging_quiz_generated")
    return {
        "schema": "neuroforge_imaging_quiz_generation_v1",
        "quiz": quiz,
        "raw_medical_data_exported": False,
        "requires_physician_review": True,
    }


@app.put("/api/neuroforge/cases/{case_id}/quiz/{quiz_id}")
def update_quiz(case_id: str, quiz_id: str, payload: QuizSetRequest) -> dict[str, Any]:
    case = _load_case(case_id)
    quizzes = case.setdefault("quiz_sets", [])
    index = next((i for i, item in enumerate(quizzes) if item.get("quiz_id") == quiz_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="quiz_not_found")
    updated = payload.model_dump()
    updated["quiz_id"] = quiz_id
    updated["schema"] = "neuroforge_imaging_quiz_v1"
    updated["created_at"] = quizzes[index].get("created_at") or _utc_now()
    updated["updated_at"] = _utc_now()
    updated["requires_physician_review"] = not all(
        question.get("status") == "approved" for question in updated["questions"]
    )
    quizzes[index] = updated
    _save_case(case, event="imaging_quiz_updated")
    return {"schema": "neuroforge_imaging_quiz_update_v1", "quiz": updated}


@app.get("/api/neuroforge/cases/{case_id}/quiz/{quiz_id}/export.docx")
def export_quiz_docx(case_id: str, quiz_id: str) -> FileResponse:
    case = _load_case(case_id)
    quiz = next((item for item in case.get("quiz_sets") or [] if item.get("quiz_id") == quiz_id), None)
    if not quiz:
        raise HTTPException(status_code=404, detail="quiz_not_found")
    if not quiz.get("questions") or not all(
        question.get("status") == "approved" for question in quiz.get("questions") or []
    ):
        raise HTTPException(status_code=409, detail="approve_all_questions_before_export")
    output = _build_quiz_docx(case, quiz)
    case.setdefault("audit", []).append(
        {
            "timestamp": _utc_now(),
            "event": "imaging_quiz_docx_exported",
            "quiz_id": quiz_id,
            "raw_dicom_embedded": False,
            "raw_medical_data_exported": False,
        }
    )
    _write_json(_case_path(case_id), case)
    return FileResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=output.name,
    )


@app.get("/api/neuroforge/cases/{case_id}/presentation")
def presentation(case_id: str) -> dict[str, Any]:
    case = _public_case(_load_case(case_id))
    return {
        "schema": "neuroforge_presentation_v1",
        "case_id": case_id,
        "title": case.get("title"),
        "clinical_question": case.get("clinical_question"),
        "narrative": case.get("narrative"),
        "limitations": case.get("limitations"),
        "takeaways": case.get("takeaways"),
        "studies": case.get("studies"),
        "key_images": [asset_item for asset_item in case.get("assets") or [] if asset_item.get("key_image")],
        "evidence": [item for item in case.get("evidence") or [] if item.get("state") in {"local_reviewed", "doctor_approved"}],
        "presentation_order": case.get("presentation_order"),
        "research_only": True,
    }


@app.get("/api/neuroforge/cases/{case_id}/export.pdf")
def export_pdf(case_id: str) -> FileResponse:
    case = _load_case(case_id)
    output = _generate_pdf(case)
    case.setdefault("audit", []).append(
        {"timestamp": _utc_now(), "event": "presentation_pdf_exported", "raw_dicom_embedded": False, "raw_medical_data_exported": False}
    )
    _write_json(_case_path(case_id), case)
    return FileResponse(output, media_type="application/pdf", filename=output.name)
