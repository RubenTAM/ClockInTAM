from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _money(value: float) -> str:
    return f"${float(value or 0):,.2f}"


def render_payroll_receipt(receipt: dict) -> bytes:
    """Create a printable one-time representation from CONTPAQi data."""
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=letter,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Recibo periodo {receipt['periodNumber']}",
        author="TecnoAll · Tiempo",
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("<b>TECNOALL · RECIBO DE NÓMINA</b>", styles["Title"]),
        Paragraph(
            "Representación generada bajo demanda con información de CONTPAQi Nóminas.",
            styles["Normal"],
        ),
        Spacer(1, 7 * mm),
    ]
    summary = [
        ["Trabajador", receipt["employeeName"]],
        ["No. empleado", receipt["employeeCode"]],
        ["Periodo", f"{receipt['periodNumber']} · {receipt['periodType']}"],
        ["Fechas", f"{receipt['periodStart']} al {receipt['periodEnd']}"],
        ["Fecha de pago", receipt["paymentDate"]],
        ["UUID", receipt["uuid"]],
    ]
    summary_table = Table(summary, colWidths=[38 * mm, 136 * mm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF4F8")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#164B6B")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#C7D8E0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([summary_table, Spacer(1, 7 * mm)])

    lines = [["Tipo", "Concepto", "Importe"]]
    labels = {
        "perception": "Percepción",
        "deduction": "Deducción",
        "withholding": "Retención",
        "other_payment": "Otro pago",
    }
    for item in receipt.get("items", []):
        lines.append([
            labels.get(item["category"], item["category"]),
            f"{item['conceptNumber']} · {item['conceptName']}",
            _money(item["amount"]),
        ])
    detail = Table(lines, colWidths=[32 * mm, 112 * mm, 30 * mm], repeatRows=1)
    detail.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#164B6B")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#D4E0E5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFB")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([detail, Spacer(1, 7 * mm)])
    totals = Table([
        ["Percepciones y otros pagos", _money(receipt["grossPay"])],
        ["Deducciones", _money(receipt["deductions"])],
        ["Retenciones", _money(receipt["withholdings"])],
        ["Neto", _money(receipt["netPay"])],
    ], colWidths=[55 * mm, 35 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), "Helvetica"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EAF4F8")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#C7D8E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(totals)
    document.build(story)
    return output.getvalue()
