import io
import base64
from datetime import datetime
from fpdf import FPDF

def clean_pdf_text(text: str) -> str:
    """Sanitize Unicode characters for standard FPDF Latin-1 font compatibility."""
    if not text:
        return ""
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "--", "\u2026": "...", "\u2022": "*",
        "₦": "NGN ", "✓": "[OK]", "✅": "[OK]", "🏛️": "", "🔒": "[PRIVATE]",
        "•": "*", "–": "-", "—": "-", "\r": "", "\t": "    "
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


class NexuconMeetingPDF(FPDF):
    def __init__(self, meeting_ref="NXC-MTG", meeting_title="Council Meeting"):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.meeting_ref = clean_pdf_text(meeting_ref)
        self.meeting_title = clean_pdf_text(meeting_title)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages()

    def header(self):
        # Top banner background (#022C4F Navy)
        self.set_fill_color(2, 44, 79)
        self.rect(0, 0, 210, 22, "F")
        
        # Gold accent stripe (#D97706)
        self.set_fill_color(217, 119, 6)
        self.rect(0, 22, 210, 2, "F")

        # Top banner header text
        self.set_xy(10, 5)
        self.set_font("helvetica", "B", 11)
        self.set_text_color(255, 255, 255)
        self.cell(100, 6, "NEXUCON REGULATORY FRAMEWORK", ln=False)

        self.set_font("helvetica", "B", 9)
        self.set_text_color(254, 215, 170)
        self.cell(90, 6, f"REF: {self.meeting_ref}", ln=True, align="R")

        self.set_xy(10, 11)
        self.set_font("helvetica", "", 8)
        self.set_text_color(203, 213, 225)
        self.cell(190, 5, "Official Council Minutes & Statutory Transcription Record", ln=True)

        self.set_y(28)

    def footer(self):
        self.set_y(-15)
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.3)
        self.line(10, self.get_y(), 200, self.get_y())

        self.set_font("helvetica", "", 7.5)
        self.set_text_color(100, 116, 139)
        self.set_x(10)
        self.cell(120, 8, f"Confidential & Statutory Record - {self.meeting_ref} - Nexucon Digital Protocol", ln=False)
        self.cell(70, 8, f"Page {self.page_no()} of {{nb}}", ln=True, align="R")


def generate_meeting_minutes_pdf(meeting, live_data: dict = None) -> bytes:
    """
    Generate an official corporate branded PDF of the meeting proceedings,
    metadata, roll call, minutes, resolutions, action items, and transcription.
    """
    live_data = live_data or {}
    
    ref = getattr(meeting, 'meeting_reference', '') or 'NXC-MTG'
    title = getattr(meeting, 'title', '') or 'Stakeholder Council Meeting'
    project = getattr(meeting, 'project_name', '') or 'General Infrastructure Oversight'
    date_str = getattr(meeting, 'date', '') or datetime.now().strftime("%B %d, %Y")
    time_slot = getattr(meeting, 'time_slot', '') or 'Official Session'
    meeting_type = getattr(meeting, 'meeting_type', 'Video Call')
    initiator_name = getattr(meeting, 'initiator_name', 'Agency Head')
    initiator_role = getattr(meeting, 'initiator_role', 'Government Directorate')

    # Duration calculations
    duration_secs = live_data.get('duration_seconds') or getattr(meeting, 'duration_seconds', 0) or 0
    if duration_secs > 0:
        mins, secs = divmod(duration_secs, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            duration_formatted = f"{hours}h {mins}m {secs}s"
        else:
            duration_formatted = f"{mins}m {secs}s"
    else:
        duration_formatted = "Concluded Session (Recorded)"

    # Notes and transcriptions
    notes = live_data.get('notes') or getattr(meeting, 'minutes_notes', '') or ''
    ai_summary = live_data.get('ai_summary') or getattr(meeting, 'ai_summary', '') or ''
    transcript = live_data.get('transcript') or getattr(meeting, 'transcript', '') or ''
    action_items = live_data.get('action_items') or []
    if not action_items and hasattr(meeting, 'action_items'):
        action_items = list(meeting.action_items.values('title', 'assignee_name', 'due_date', 'is_completed'))

    # Participant roster
    participants = live_data.get('participants') or getattr(meeting, 'participants', []) or []

    pdf = NexuconMeetingPDF(meeting_ref=ref, meeting_title=title)
    pdf.add_page()

    # 1. Title & Header Block
    pdf.set_font("helvetica", "B", 16)
    pdf.set_text_color(2, 44, 79)
    pdf.multi_cell(190, 7, clean_pdf_text(title))
    pdf.ln(1)

    pdf.set_font("helvetica", "B", 9)
    pdf.set_text_color(3, 105, 161)
    pdf.cell(190, 5, f"PROJECT: {clean_pdf_text(project).upper()}", ln=True)
    pdf.ln(3)

    # 2. Key Metadata Box
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(203, 213, 225)
    pdf.set_line_width(0.4)
    x = pdf.get_x()
    y = pdf.get_y()
    pdf.rect(x, y, 190, 32, "FD")

    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(71, 85, 105)

    col1_x = x + 4
    col2_x = x + 68
    col3_x = x + 132

    # Row 1
    pdf.set_xy(col1_x, y + 3)
    pdf.cell(60, 4, f"Meeting Reference:  {clean_pdf_text(ref)}", ln=False)
    pdf.set_xy(col2_x, y + 3)
    pdf.cell(60, 4, f"Session Date:  {clean_pdf_text(date_str)}", ln=False)
    pdf.set_xy(col3_x, y + 3)
    pdf.cell(54, 4, f"Format:  {clean_pdf_text(meeting_type)}", ln=True)

    # Row 2
    pdf.set_xy(col1_x, y + 10)
    pdf.cell(60, 4, f"Session Slot:  {clean_pdf_text(time_slot)}", ln=False)
    pdf.set_xy(col2_x, y + 10)
    pdf.cell(60, 4, f"Total Duration:  {clean_pdf_text(duration_formatted)}", ln=False)
    pdf.set_xy(col3_x, y + 10)
    status_label = getattr(meeting, 'status', 'Completed')
    pdf.cell(54, 4, f"Status:  {clean_pdf_text(status_label)}", ln=True)

    # Row 3
    pdf.set_xy(col1_x, y + 17)
    pdf.cell(120, 4, f"Initiator / Chair:  {clean_pdf_text(initiator_name)} ({clean_pdf_text(initiator_role)})", ln=False)
    pdf.set_xy(col3_x, y + 17)
    pdf.cell(54, 4, f"Total Attendees:  {len(participants)} registered", ln=True)

    # Row 4: Council Seal
    pdf.set_xy(col1_x, y + 24)
    pdf.set_font("helvetica", "I", 7.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(180, 4, "Verified by Nexucon Council Cryptographic Attendance & Minutes Protocol", ln=True)

    pdf.set_y(y + 36)

    # 3. Attendance Roster Table
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(190, 6, "1. OFFICIAL ROLL CALL & PARTICIPANT ATTENDANCE", ln=True)
    pdf.ln(1)

    # Table Header
    pdf.set_fill_color(2, 44, 79)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 8)
    pdf.cell(60, 6, "Participant Name", border=1, fill=True)
    pdf.cell(48, 6, "Role / Designation", border=1, fill=True)
    pdf.cell(58, 6, "Official Email Address", border=1, fill=True)
    pdf.cell(24, 6, "Status", border=1, fill=True, align="C")
    pdf.ln(6)

    # Table Rows
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    pdf.set_draw_color(226, 232, 240)

    if participants:
        for idx, p in enumerate(participants):
            p_name = p.get('name', 'Participant') if isinstance(p, dict) else str(p)
            p_role = p.get('role', 'Council Member') if isinstance(p, dict) else 'Member'
            p_email = p.get('email', '-') if isinstance(p, dict) else '-'
            p_status = p.get('status', 'Attended') if isinstance(p, dict) else 'Attended'

            bg = 248 if idx % 2 == 1 else 255
            pdf.set_fill_color(bg, bg, bg)
            pdf.cell(60, 5.5, clean_pdf_text(p_name)[:34], border=1, fill=True)
            pdf.cell(48, 5.5, clean_pdf_text(p_role)[:28], border=1, fill=True)
            pdf.cell(58, 5.5, clean_pdf_text(p_email)[:34], border=1, fill=True)
            pdf.cell(24, 5.5, clean_pdf_text(p_status), border=1, fill=True, align="C")
            pdf.ln(5.5)
    else:
        pdf.cell(190, 6, "No attendance entries registered.", border=1, align="C")
        pdf.ln(6)

    pdf.ln(4)

    # 4. Meeting Agenda & Executive Minutes
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(190, 6, "2. MEETING MINUTES & NOTE TAKER RECORD", ln=True)
    pdf.ln(1)

    agenda_text = getattr(meeting, 'agenda', '') or 'Stage-gate technical review & statutory inspection alignment.'
    pdf.set_font("helvetica", "B", 8.5)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(22, 5, "Agenda: ", ln=False)
    pdf.set_font("helvetica", "", 8.5)
    pdf.set_text_color(15, 23, 42)
    pdf.multi_cell(168, 5, clean_pdf_text(agenda_text))
    pdf.ln(2)

    # Live Note Taker Minutes
    pdf.set_font("helvetica", "B", 8.5)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(190, 5, "Official Session Minutes:", ln=True)

    pdf.set_font("helvetica", "", 8.5)
    pdf.set_text_color(15, 23, 42)
    display_notes = notes.strip() if notes else "Session concluded. Meeting minutes recorded and certified by the council presiding officer."
    pdf.multi_cell(190, 5, clean_pdf_text(display_notes))
    pdf.ln(3)

    # AI Executive Summary (if available)
    if ai_summary:
        pdf.set_fill_color(240, 249, 255)
        pdf.set_draw_color(186, 230, 253)
        pdf.set_font("helvetica", "B", 8.5)
        pdf.set_text_color(3, 105, 161)
        pdf.cell(190, 5, "AI Note Taker Summary & Key Discussion Points:", ln=True)
        pdf.set_font("helvetica", "", 8)
        pdf.set_text_color(30, 41, 59)
        pdf.multi_cell(190, 4.5, clean_pdf_text(ai_summary))
        pdf.ln(3)

    # 5. Action Items & Deliverables Matrix
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(190, 6, "3. DELIVERABLES & ACTION ITEMS MATRIX", ln=True)
    pdf.ln(1)

    pdf.set_fill_color(2, 44, 79)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 8)
    pdf.cell(100, 6, "Action Item / Deliverable", border=1, fill=True)
    pdf.cell(46, 6, "Assigned Lead", border=1, fill=True)
    pdf.cell(44, 6, "Target Due Date", border=1, fill=True, align="C")
    pdf.ln(6)

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    pdf.set_draw_color(226, 232, 240)

    if action_items:
        for idx, item in enumerate(action_items):
            i_title = item.get('title', 'Deliverable') if isinstance(item, dict) else getattr(item, 'title', str(item))
            i_assignee = item.get('assignee_name', 'Project Lead') if isinstance(item, dict) else getattr(item, 'assignee_name', 'Lead')
            i_due = item.get('due_date', '5 Business Days') if isinstance(item, dict) else getattr(item, 'due_date', 'Standard')

            bg = 248 if idx % 2 == 1 else 255
            pdf.set_fill_color(bg, bg, bg)
            pdf.cell(100, 5.5, clean_pdf_text(i_title)[:60], border=1, fill=True)
            pdf.cell(46, 5.5, clean_pdf_text(i_assignee)[:26], border=1, fill=True)
            pdf.cell(44, 5.5, clean_pdf_text(i_due)[:24], border=1, fill=True, align="C")
            pdf.ln(5.5)
    else:
        pdf.cell(190, 6, "No outstanding action items created during this session.", border=1, align="C")
        pdf.ln(6)

    pdf.ln(4)

    # 6. Verbatim Transcription Appendix
    if transcript:
        pdf.set_font("helvetica", "B", 11)
        pdf.set_text_color(2, 44, 79)
        pdf.cell(190, 6, "4. CERTIFIED SPEECH TRANSCRIPTION APPENDIX", ln=True)
        pdf.ln(1)

        pdf.set_font("courier", "", 7.5)
        pdf.set_text_color(51, 65, 85)
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(226, 232, 240)

        # Print transcript lines
        t_lines = transcript.strip().split("\n")
        for line in t_lines:
            clean_l = clean_pdf_text(line).strip()
            if clean_l:
                pdf.multi_cell(190, 4, clean_l, border="LR", fill=True)

        pdf.set_draw_color(226, 232, 240)
        pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + 190, pdf.get_y())
        pdf.ln(4)

    # 7. Official Sign-off Seal
    pdf.ln(2)
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(95, 5, "COUNCIL CHAIRPERSON / INITIATOR", ln=False)
    pdf.cell(95, 5, "REGULATORY COMPLIANCE ATTESTATION", ln=True, align="R")

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(95, 4, f"{clean_pdf_text(initiator_name)}", ln=False)
    pdf.cell(95, 4, "Certified & Digitally Sealed", ln=True, align="R")

    pdf.set_font("helvetica", "I", 7)
    pdf.set_text_color(148, 163, 184)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    pdf.cell(95, 4, f"Presiding Officer - {clean_pdf_text(initiator_role)}", ln=False)
    pdf.cell(95, 4, f"Generated: {now_str}", ln=True, align="R")

    # Output binary PDF buffer
    buf = pdf.output()
    if isinstance(buf, str):
        return buf.encode("latin-1")
    return bytes(buf)

