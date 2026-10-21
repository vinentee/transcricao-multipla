"""PDF rendering for completed transcript exports."""

from html import escape
from io import BytesIO

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def _clock(milliseconds):
    seconds = milliseconds // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


class TranscriptCanvas:
    """Draw a lightweight, repeated header and footer on every PDF page."""

    def __init__(self, filename):
        self.filename = filename

    def __call__(self, canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#DDDCE7"))
        canvas.line(document.leftMargin, A4[1] - 20 * mm, A4[0] - document.rightMargin, A4[1] - 20 * mm)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(colors.HexColor("#24233A"))
        canvas.drawString(document.leftMargin, A4[1] - 15 * mm, "Voz em Texto · Transcrição")
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#66657B"))
        canvas.drawRightString(A4[0] - document.rightMargin, A4[1] - 15 * mm, self.filename[:70])
        canvas.line(document.leftMargin, 15 * mm, A4[0] - document.rightMargin, 15 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(document.leftMargin, 10 * mm, "Documento gerado por Voz em Texto")
        canvas.drawRightString(A4[0] - document.rightMargin, 10 * mm, f"Página {document.page}")
        canvas.restoreState()


def build_transcript_pdf(job):
    """Return a PDF byte stream. The caller owns access control and response headers."""
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=28 * mm,
        bottomMargin=23 * mm,
        title=f"Transcrição - {job.original_filename}",
        author="Voz em Texto",
        pageCompression=0,
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TranscriptTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, leading=25, textColor=colors.HexColor("#24233A"), spaceAfter=6)
    metadata = ParagraphStyle("TranscriptMetadata", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=13, textColor=colors.HexColor("#66657B"), spaceAfter=14)
    speaker = ParagraphStyle("TranscriptSpeaker", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=colors.HexColor("#5045BE"), keepWithNext=True)
    transcript = ParagraphStyle("TranscriptBody", parent=styles["Normal"], fontName="Helvetica", fontSize=10.5, leading=15, textColor=colors.HexColor("#24233A"), spaceAfter=11)

    created_at = timezone.localtime(job.created_at).strftime("%d/%m/%Y às %H:%M")
    duration = _clock(job.duration_seconds * 1000) if job.duration_seconds else "não informada"
    story = [
        Paragraph("Transcrição do áudio", title),
        Paragraph(
            f"<b>Arquivo:</b> {escape(job.original_filename)}<br/>"
            f"<b>Enviado em:</b> {created_at}<br/>"
            f"<b>Duração:</b> {duration}",
            metadata,
        ),
    ]

    if job.needs_review:
        story.append(Paragraph("<b>REVISÃO PENDENTE:</b> Confira os trechos sinalizados com o áudio original.", metadata))
        for notice in job.review_notices:
            story.append(Paragraph(f"{notice['start']} - {notice['end']}: {escape(notice['label'])}", metadata))

    for segment in job.segments.select_related("speaker").all():
        speaker_name = segment.speaker.display_name if segment.speaker else "Falante não identificado"
        heading = Paragraph(f"{_clock(segment.start_ms)} - {_clock(segment.end_ms)} &nbsp; {escape(speaker_name)}", speaker)
        warning = Paragraph("Revisão pendente: " + escape("; ".join(segment.review_labels)), speaker) if segment.needs_review else None
        text = Paragraph(escape(segment.text).replace("\n", "<br/>"), transcript)
        # Keep the segment heading with its first line, while allowing a very
        # long transcript segment to continue naturally onto the next page.
        story.extend([heading, *([warning] if warning else []), Spacer(1, 2 * mm), text])

    document.build(story, onFirstPage=TranscriptCanvas(job.original_filename), onLaterPages=TranscriptCanvas(job.original_filename))
    return buffer.getvalue()
