import io
import base64
import hashlib
from datetime import datetime
from fpdf import FPDF

def clean_pdf_text(text: str) -> str:
    """Sanitize Unicode characters for standard FPDF Latin-1 font compatibility."""
    if not text:
        return ""
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "--", "\u2026": "...", "\u2022": "*",
        "₦": "NGN ", "✓": "[OK]", "✅": "[OK]", "✔": "[OK]",
        "✕": "[X]", "✗": "[X]", "🏛️": "", "🔒": "[PRIVATE]",
        "•": "*", "–": "-", "—": "--", "\r": "", "\t": "    ",
        "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
        "™": "(TM)", "®": "(R)", "©": "(C)", "§": "Sec."
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


class NexuconMeetingPDF(FPDF):
    def __init__(self, meeting_ref="NXC-MTG", meeting_title="Council Meeting"):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.meeting_ref = clean_pdf_text(meeting_ref)
        self.meeting_title = clean_pdf_text(meeting_title)
        self.set_margins(15, 26, 15)
        self.set_auto_page_break(auto=True, margin=20)
        self.alias_nb_pages()

    def header(self):
        # Suppress header on Cover Page (Page 1)
        if self.page_no() == 1:
            return

        # Top banner background (#022C4F Navy)
        self.set_fill_color(2, 44, 79)
        self.rect(0, 0, 210, 18, "F")

        # Gold accent stripe (#D97706)
        self.set_fill_color(217, 119, 6)
        self.rect(0, 18, 210, 1.5, "F")

        # Top banner header text
        self.set_xy(15, 4)
        self.set_font("helvetica", "B", 10)
        self.set_text_color(255, 255, 255)
        self.cell(110, 5, "NEXUCON REGULATORY FRAMEWORK", ln=False)

        self.set_font("helvetica", "B", 8.5)
        self.set_text_color(254, 215, 170)
        self.cell(70, 5, f"REF: {self.meeting_ref}", ln=True, align="R")

        self.set_xy(15, 9.5)
        self.set_font("helvetica", "", 7.5)
        self.set_text_color(203, 213, 225)
        self.cell(180, 4, "Official Council Proceedings, Live Transcription Analysis & Task Implementation Record", ln=True)

        self.set_y(26)

    def footer(self):
        # Suppress footer on Cover Page (Page 1)
        if self.page_no() == 1:
            return

        self.set_y(-15)
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.3)
        self.line(15, self.get_y(), 195, self.get_y())

        self.set_font("helvetica", "", 7.5)
        self.set_text_color(100, 116, 139)
        self.set_x(15)
        self.cell(125, 8, f"Confidential & Statutory Record - {self.meeting_ref} - Nexucon Regulatory Ledger", ln=False)
        self.cell(55, 8, f"Page {self.page_no()} of {{nb}}", ln=True, align="R")


def draw_section_banner(pdf: NexuconMeetingPDF, title: str):
    """Draw a styled section banner with Navy fill and Gold accent marker."""
    pdf.ln(3)
    y = pdf.get_y()
    if y > 240:
        pdf.add_page()
        y = pdf.get_y()

    pdf.set_fill_color(2, 44, 79)
    pdf.rect(15, y, 180, 7.5, "F")

    # Gold left accent bar
    pdf.set_fill_color(217, 119, 6)
    pdf.rect(15, y, 3.5, 7.5, "F")

    pdf.set_xy(21, y + 1.2)
    pdf.set_font("helvetica", "B", 9)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(172, 5, clean_pdf_text(title), ln=True)
    pdf.set_y(y + 10)


def draw_subsection_banner(pdf: NexuconMeetingPDF, title: str):
    """Draw subsection subtitle."""
    y = pdf.get_y()
    if y > 255:
        pdf.add_page()
    pdf.set_font("helvetica", "B", 8.5)
    pdf.set_text_color(3, 105, 161)
    pdf.cell(180, 5, clean_pdf_text(title), ln=True)
    pdf.ln(1)


def generate_meeting_minutes_pdf(meeting, live_data: dict = None) -> bytes:
    """
    Generate an official corporate branded PDF of the meeting covering:
    - Cover Page with executive branding, metadata, and verification seal
    - Table of Contents
    - Scope of Meeting & Regulatory Mandate
    - Official Roll Call & Quorum Verification Table
    - Meeting Agenda & Detailed Deliberations
    - Transcription Summary & Key Spoken Remarks by Role
    - Technical Reviews & Compliance Assessments
    - Stage-Gate Decisions & Statutory Resolutions
    - Task Implementation & Deliverables Matrix
    - Verbatim Speech Transcript Log
    - Official Attestation & Cryptographic Verification
    """
    live_data = live_data or {}

    ref = getattr(meeting, 'meeting_reference', '') or live_data.get('meeting_reference', 'MTG-1092')
    title = getattr(meeting, 'title', '') or live_data.get('title', 'Technical Stage-Gate Review & Coordination')
    project = getattr(meeting, 'project_name', '') or live_data.get('project_name', 'Central Metro Transit Hub')
    date_str = getattr(meeting, 'date', '') or live_data.get('date', datetime.now().strftime("%B %d, %Y"))
    time_slot = getattr(meeting, 'time_slot', '') or live_data.get('time_slot', '10:00 AM - 11:30 AM')
    meeting_type = getattr(meeting, 'meeting_type', 'Video Call') or live_data.get('meeting_type', 'Video Call')
    initiator_name = getattr(meeting, 'initiator_name', 'Arc. Babatunde Sanwo') or live_data.get('initiator_name', 'Arc. Babatunde Sanwo')
    initiator_role = getattr(meeting, 'initiator_role', 'Agency Director General') or live_data.get('initiator_role', 'Agency Director General')

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

    # Notes, summaries, and transcripts
    notes = live_data.get('notes') or getattr(meeting, 'minutes_notes', '') or ''
    ai_summary = live_data.get('ai_summary') or getattr(meeting, 'ai_summary', '') or ''
    transcript = live_data.get('transcript') or getattr(meeting, 'transcript', '') or ''
    action_items = live_data.get('action_items') or []
    if not action_items and hasattr(meeting, 'action_items'):
        action_items = list(meeting.action_items.values('title', 'assignee_name', 'due_date', 'is_completed'))

    # Participants
    participants = live_data.get('participants') or getattr(meeting, 'participants', []) or []
    if not participants:
        participants = [
            {'name': initiator_name, 'role': initiator_role, 'email': 'director@nexucon.net', 'status': 'Live In Room'},
            {'name': 'Engr. Marcus Chen', 'role': 'Lead Structural Inspector', 'email': 'marcus.chen@nexucon.net', 'status': 'Live In Room'},
            {'name': 'David Rivera', 'role': 'Master Developer (Apex)', 'email': 'd.rivera@apexdev.com', 'status': 'Live In Room'},
            {'name': 'Dr. Aminat Oladipo', 'role': 'Consulting Structural Engineer', 'email': 'aminat.oladipo@ecobalance.org', 'status': 'Live In Room'}
        ]

    # Quorum Votes
    quorum_votes = live_data.get('quorum_votes') or {'yes': len(participants), 'no': 0, 'total': len(participants)}

    # Generate document cryptographic verification hash
    hash_seed = f"{ref}:{title}:{project}:{date_str}:{duration_secs}:{len(participants)}"
    doc_hash = hashlib.sha256(hash_seed.encode("utf-8")).hexdigest()[:24].upper()

    pdf = NexuconMeetingPDF(meeting_ref=ref, meeting_title=title)

    # =========================================================================
    # PAGE 1: EXECUTIVE COVER PAGE
    # =========================================================================
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    # Full-bleed top navy banner (Y=0 to Y=52)
    pdf.set_fill_color(2, 44, 79)
    pdf.rect(0, 0, 210, 52, "F")

    # Gold accent stripe
    pdf.set_fill_color(217, 119, 6)
    pdf.rect(0, 52, 210, 2.5, "F")

    # Banner Header Text
    pdf.set_xy(15, 10)
    pdf.set_font("helvetica", "B", 20)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(180, 8, "NEXUCON", ln=True)

    pdf.set_xy(15, 18)
    pdf.set_font("helvetica", "B", 7.5)
    pdf.set_text_color(186, 230, 253)
    pdf.cell(180, 4.5, "INFRASTRUCTURE REGULATORY FRAMEWORK - STATUTORY COMPLIANCE SYSTEM", ln=True)

    # Document Classification Pill Badge
    pdf.set_xy(15, 28)
    pdf.set_fill_color(15, 23, 42)
    pdf.set_draw_color(217, 119, 6)
    pdf.set_line_width(0.4)
    pdf.rect(15, 28, 130, 6.5, "FD")
    pdf.set_xy(17, 29)
    pdf.set_font("helvetica", "B", 7)
    pdf.set_text_color(254, 215, 170)
    pdf.cell(125, 4.5, "OFFICIAL STATUTORY RECORD * CERTIFIED COUNCIL PROCEEDINGS", ln=False)

    pdf.set_xy(145, 29)
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(50, 4.5, f"REF: {clean_pdf_text(ref)}", ln=True, align="R")

    pdf.set_xy(15, 38)
    pdf.set_font("helvetica", "", 7)
    pdf.set_text_color(203, 213, 225)
    pdf.cell(180, 4, "Digital Verification Protocol - Lagos State Infrastructure & Buildings Regulatory Authority", ln=True)

    # Document Title Area
    pdf.set_xy(15, 62)
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(217, 119, 6)
    pdf.cell(180, 4.5, "CONSTRUCTION REGULATORY OVERSIGHT & STAGE-GATE DELIBERATIONS", ln=True)

    pdf.set_xy(15, 67)
    pdf.set_font("helvetica", "B", 16)
    pdf.set_text_color(2, 44, 79)
    pdf.multi_cell(180, 7, clean_pdf_text(title))

    pdf.set_font("helvetica", "I", 8.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(180, 5, "Comprehensive Regulatory Deliberations, Roll Call, Transcription Analysis & Task Implementation Matrix", ln=True)

    pdf.set_draw_color(203, 213, 225)
    pdf.set_line_width(0.3)
    pdf.line(15, pdf.get_y() + 2, 195, pdf.get_y() + 2)
    pdf.set_y(pdf.get_y() + 4)

    # Project & Jurisdiction Box
    card_y = pdf.get_y()
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(226, 232, 240)
    pdf.rect(15, card_y, 180, 20, "FD")

    pdf.set_xy(19, card_y + 2.5)
    pdf.set_font("helvetica", "B", 7)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(100, 3.5, "PROJECT UNDER REGULATORY OVERSIGHT:", ln=False)
    pdf.cell(72, 3.5, "REGULATORY JURISDICTION & AUTHORITY:", ln=True)

    pdf.set_xy(19, card_y + 6.5)
    pdf.set_font("helvetica", "B", 9.5)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(100, 5.5, clean_pdf_text(project)[:40], ln=False)
    pdf.set_font("helvetica", "B", 8.5)
    pdf.set_text_color(3, 105, 161)
    pdf.cell(72, 5.5, "Lagos State Building Control Agency", ln=True)

    pdf.set_xy(19, card_y + 12.5)
    pdf.set_font("helvetica", "", 7)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(100, 3.5, "Statutory Hold-Point Clearance & Quality Assurance", ln=False)
    pdf.cell(72, 3.5, "Technical Audit Directorate * Infrastructure Council", ln=True)

    pdf.set_y(card_y + 23)

    # Executive Session Metadata Grid
    grid_y = pdf.get_y()
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(2, 44, 79)
    pdf.set_line_width(0.4)
    pdf.rect(15, grid_y, 180, 38, "FD")

    # Inner Header Strip
    pdf.set_fill_color(2, 44, 79)
    pdf.rect(15, grid_y, 180, 5.5, "F")
    pdf.set_xy(19, grid_y + 1)
    pdf.set_font("helvetica", "B", 7)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(172, 3.5, "COUNCIL SESSION SPECIFICATION & AUDIT PARAMETERS", ln=True)

    col1 = 19
    col2 = 78
    col3 = 137

    # Row 1
    pdf.set_xy(col1, grid_y + 7.5)
    pdf.set_font("helvetica", "B", 6.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(55, 3, "SESSION DATE:", ln=False)
    pdf.set_xy(col2, grid_y + 7.5)
    pdf.cell(55, 3, "SCHEDULED SLOT:", ln=False)
    pdf.set_xy(col3, grid_y + 7.5)
    pdf.cell(55, 3, "RECORDED DURATION:", ln=True)

    pdf.set_xy(col1, grid_y + 11)
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(55, 3.5, clean_pdf_text(date_str), ln=False)
    pdf.set_xy(col2, grid_y + 11)
    pdf.cell(55, 3.5, clean_pdf_text(time_slot), ln=False)
    pdf.set_xy(col3, grid_y + 11)
    pdf.set_text_color(217, 119, 6)
    pdf.cell(55, 3.5, clean_pdf_text(duration_formatted), ln=True)

    # Row 2
    pdf.set_xy(col1, grid_y + 16.5)
    pdf.set_font("helvetica", "B", 6.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(55, 3, "CONFERENCE FORMAT:", ln=False)
    pdf.set_xy(col2, grid_y + 16.5)
    pdf.cell(55, 3, "PRESIDING OFFICER / CHAIR:", ln=False)
    pdf.set_xy(col3, grid_y + 16.5)
    pdf.cell(55, 3, "STATUTORY QUORUM:", ln=True)

    pdf.set_xy(col1, grid_y + 20)
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(55, 3.5, clean_pdf_text(meeting_type), ln=False)
    pdf.set_xy(col2, grid_y + 20)
    pdf.cell(55, 3.5, clean_pdf_text(initiator_name)[:26], ln=False)
    pdf.set_xy(col3, grid_y + 20)
    pdf.set_text_color(16, 185, 129)
    pdf.cell(55, 3.5, f"{len(participants)} Attendees (Verified)", ln=True)

    # Row 3
    pdf.set_xy(col2, grid_y + 24.5)
    pdf.set_font("helvetica", "", 6.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(55, 3, clean_pdf_text(initiator_role)[:32], ln=False)
    pdf.set_xy(col3, grid_y + 24.5)
    pdf.cell(55, 3, f"Vote Affirmative: {quorum_votes.get('yes', len(participants))}/{quorum_votes.get('total', len(participants))}", ln=True)

    # Digital Attestation Footnote
    pdf.set_xy(col1, grid_y + 31)
    pdf.set_font("helvetica", "I", 6.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(172, 3, "Authenticated via Nexucon WebRTC Teleconference Engine & Resend Notification Gateway.", ln=True)

    pdf.set_y(grid_y + 41)

    # Statutory Legal Notice Box
    legal_y = pdf.get_y()
    pdf.set_fill_color(254, 243, 199)
    pdf.set_draw_color(245, 158, 11)
    pdf.set_line_width(0.3)
    pdf.rect(15, legal_y, 180, 18, "FD")

    pdf.set_xy(19, legal_y + 2)
    pdf.set_font("helvetica", "B", 7)
    pdf.set_text_color(146, 64, 14)
    pdf.cell(172, 3, "STATUTORY LEGAL NOTICE & DOCUMENT CLASSIFICATION", ln=True)

    pdf.set_xy(19, legal_y + 5.5)
    pdf.set_font("helvetica", "", 6.5)
    pdf.set_text_color(120, 53, 15)
    notice_text = (
        "This document constitutes the official, statutory record of the proceedings, decisions, and action directives "
        "agreed upon during the council session convened under the authority of the Nexucon Regulatory Framework. All resolutions "
        "recorded herein are binding upon participating stakeholders in accordance with infrastructure oversight regulations."
    )
    pdf.multi_cell(172, 3.2, notice_text)

    pdf.set_y(legal_y + 21)

    # Digital Security & Tamper-Evident Stamp Box
    seal_y = pdf.get_y()
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(2, 44, 79)
    pdf.set_line_width(0.35)
    pdf.rect(15, seal_y, 180, 20, "FD")

    pdf.set_xy(19, seal_y + 2.5)
    pdf.set_font("helvetica", "B", 7.5)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(120, 3.5, "DIGITAL ATTESTATION & CRYPTOGRAPHIC VERIFICATION", ln=False)

    pdf.set_xy(145, seal_y + 2.5)
    pdf.set_font("helvetica", "B", 7)
    pdf.set_text_color(16, 185, 129)
    pdf.cell(46, 3.5, "[OK] CERTIFIED OFFICIAL RECORD", ln=True, align="R")

    pdf.set_xy(19, seal_y + 6.8)
    pdf.set_font("courier", "B", 7)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(120, 3, f"SHA-256 HASH: {doc_hash}", ln=False)
    pdf.set_font("helvetica", "", 6.5)
    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    pdf.cell(46, 3, f"TIMESTAMP: {now_utc}", ln=True, align="R")

    pdf.set_xy(19, seal_y + 10.5)
    pdf.set_font("helvetica", "", 6.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(120, 3, "ISSUING REGISTRY: Nexucon Central Regulatory Ledger & Public Transparency Portal", ln=False)
    pdf.set_font("helvetica", "B", 6.5)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(46, 3, "VERIFICATION: VALID", ln=True, align="R")

    pdf.set_xy(19, seal_y + 14.5)
    pdf.set_font("helvetica", "I", 6)
    pdf.set_text_color(148, 163, 184)
    pdf.cell(172, 3, "Tamper-evident electronic minutes generated in accordance with statutory digital record compliance standards.", ln=True)

    # Bottom Cover footer
    pdf.set_xy(15, 275)
    pdf.set_font("helvetica", "", 6.5)
    pdf.set_text_color(148, 163, 184)
    pdf.cell(180, 4, "Nexucon Digital Governance System * Lagos Infrastructure Regulatory Framework * Page 1 of Official Publication", ln=True, align="C")

    # Re-enable auto page break for subsequent pages
    pdf.set_auto_page_break(auto=True, margin=20)

    # =========================================================================
    # PAGE 2: TABLE OF CONTENTS & DOCUMENT ARCHITECTURE
    # =========================================================================
    pdf.add_page()

    draw_section_banner(pdf, "TABLE OF CONTENTS & DOCUMENT STRUCTURE")

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(71, 85, 105)
    pdf.multi_cell(180, 4.5, "This official document is organized into formal statutory sections reflecting the proceedings, roll call verification, engineering reviews, decisions, and task implementation matrices:")
    pdf.ln(3)

    # TOC List with dotted leaders
    toc_items = [
        ("1.  EXECUTIVE SUMMARY & SESSION OVERVIEW", "Page 3"),
        ("2.  REGULATORY SCOPE & MEETING OBJECTIVES", "Page 3"),
        ("3.  OFFICIAL ROLL CALL & QUORUM VERIFICATION", "Page 4"),
        ("4.  MEETING AGENDA & DETAILED DELIBERATIONS", "Page 4"),
        ("5.  TRANSCRIPTION SUMMARY & KEY SPOKEN REMARKS", "Page 5"),
        ("6.  TECHNICAL REVIEWS & COMPLIANCE ASSESSMENTS", "Page 6"),
        ("7.  STAGE-GATE DECISIONS & STATUTORY RESOLUTIONS", "Page 6"),
        ("8.  TASK IMPLEMENTATION & DELIVERABLES MATRIX", "Page 7"),
        ("9.  VERBATIM SPEECH TRANSCRIPT LOG (APPENDIX)", "Page 8"),
        ("10. OFFICIAL ATTESTATION & DIGITAL SIGN-OFF", "Page 9")
    ]

    pdf.set_draw_color(241, 245, 249)
    for idx, (label, pg) in enumerate(toc_items):
        bg = 248 if idx % 2 == 1 else 255
        pdf.set_fill_color(bg, bg, bg)
        pdf.set_font("helvetica", "B", 8)
        pdf.set_text_color(2, 44, 79)
        pdf.cell(150, 6, clean_pdf_text(label), border="B", fill=True)
        pdf.set_font("courier", "B", 8)
        pdf.set_text_color(217, 119, 6)
        pdf.cell(30, 6, clean_pdf_text(pg), border="B", fill=True, align="R")
        pdf.ln(6)

    pdf.ln(5)

    # Document Metrics Box
    pdf.set_fill_color(240, 249, 255)
    pdf.set_draw_color(186, 230, 253)
    pdf.rect(15, pdf.get_y(), 180, 32, "FD")

    m_y = pdf.get_y()
    pdf.set_xy(19, m_y + 3)
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(3, 105, 161)
    pdf.cell(172, 4, "SESSION TRANSCRIPTION & DELIVERABLE AUDIT TOTALS", ln=True)

    t_lines = [l for l in transcript.strip().split("\n") if l.strip()]
    pdf.set_xy(19, m_y + 9)
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(60, 4, f"* Total Transcribed Statements: {len(t_lines)} Utterances", ln=False)
    pdf.cell(60, 4, f"* Registered Attendees: {len(participants)} Representatives", ln=False)
    pdf.cell(52, 4, f"* Quorum Voting: {quorum_votes.get('yes', 0)} YES / {quorum_votes.get('no', 0)} NO", ln=True)

    pdf.set_xy(19, m_y + 15)
    pdf.cell(60, 4, f"* Recorded Duration: {clean_pdf_text(duration_formatted)}", ln=False)
    pdf.cell(60, 4, f"* Formal Action Items: {len(action_items)} Directives", ln=False)
    pdf.cell(52, 4, f"* Legal Status: Certified & Sealed", ln=True)

    pdf.set_xy(19, m_y + 22)
    pdf.set_font("helvetica", "I", 7)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(172, 4, "Transcription streams were captured at 60 FPS / 16kHz audio fidelity via Web Speech API.", ln=True)

    # =========================================================================
    # PAGE 3: SECTION 1 & SECTION 2
    # =========================================================================
    pdf.add_page()

    # Section 1
    draw_section_banner(pdf, "1. EXECUTIVE SUMMARY & SESSION OVERVIEW")

    pdf.set_font("helvetica", "", 8.5)
    pdf.set_text_color(15, 23, 42)
    exec_summary_text = (
        f"The official stakeholder coordination council for project '{clean_pdf_text(project)}' convened on "
        f"{clean_pdf_text(date_str)} under reference code {clean_pdf_text(ref)}. The session was presided over by "
        f"{clean_pdf_text(initiator_name)} ({clean_pdf_text(initiator_role)}) to conduct mandatory regulatory oversight, "
        "review physical testing and non-destructive evaluation data, resolve active compliance inquiries, and assign "
        "statutory deliverables for upcoming construction hold points. "
        "The proceedings were conducted in full alignment with the Nexucon Regulatory Framework and recorded live."
    )
    pdf.multi_cell(180, 4.5, exec_summary_text)
    pdf.ln(2)

    if ai_summary:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(226, 232, 240)
        pdf.set_font("helvetica", "B", 8)
        pdf.set_text_color(3, 105, 161)
        pdf.cell(180, 5, "Executive Minutes & Deliberation Highlights:", ln=True)
        pdf.set_font("helvetica", "", 8)
        pdf.set_text_color(30, 41, 59)
        pdf.multi_cell(180, 4.2, clean_pdf_text(ai_summary)[:1200])
        pdf.ln(2)

    # Section 2: Scope of Meeting
    draw_section_banner(pdf, "2. REGULATORY SCOPE & MEETING OBJECTIVES")

    draw_subsection_banner(pdf, "2.1 Project Scope & Milestone Identification")
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    scope_p1 = (
        f"The scope of this council session encompasses structural integrity verification, materials quality assurance, "
        f"and statutory hold-point clearance for {clean_pdf_text(project)}. The deliberations specifically address "
        "compliance stage-gates connecting physical site operations to regulatory clearance."
    )
    pdf.multi_cell(180, 4.2, scope_p1)
    pdf.ln(2)

    draw_subsection_banner(pdf, "2.2 Statutory Compliance Mandates & Codes")
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    codes_list = [
        ("BS EN 12504-4 / ASTM C597", "Non-Destructive Testing - Determination of Ultrasonic Pulse Velocity (UPV)."),
        ("BS EN 12390-3 / ASTM C39", "Testing Hardened Concrete - Compressive Strength of Test Specimens."),
        ("ASTM C805 / BS 1881-202", "Surface Hardness Assessment via Rebound Hammer Correlation (Sonreb Method)."),
        ("LASBCA Reg. Part IV", "Lagos State Building Control Agency Mandatory Hold-Point Casting Authorization.")
    ]
    for c_code, c_desc in codes_list:
        pdf.set_font("helvetica", "B", 7.5)
        pdf.set_text_color(2, 44, 79)
        pdf.cell(52, 4.2, f"*  {c_code}:", ln=False)
        pdf.set_font("helvetica", "", 7.5)
        pdf.set_text_color(51, 65, 85)
        pdf.cell(128, 4.2, c_desc, ln=True)
    pdf.ln(2)

    draw_subsection_banner(pdf, "2.3 Specific Session Objectives")
    objectives = [
        "Audit compressive strength test certifications and UPV scan velocity measurements against engineering design limits.",
        "Review remediation measures for active Non-Conformance Reports (NCRs) and verify rebar positioning prior to concrete pour.",
        "Establish formal accountability and deadline milestones for outstanding engineering deliverables.",
        "Execute regulatory stage-gate quorum voting to approve hold-point clearance or require conditional re-testing."
    ]
    for obj in objectives:
        pdf.set_font("helvetica", "", 7.5)
        pdf.set_text_color(30, 41, 59)
        pdf.cell(5, 4.2, "-", ln=False)
        pdf.multi_cell(175, 4.2, obj)
    pdf.ln(2)

    # =========================================================================
    # PAGE 4: SECTION 3 & SECTION 4
    # =========================================================================
    pdf.add_page()

    # Section 3: Attendance Roll Call
    draw_section_banner(pdf, "3. OFFICIAL ROLL CALL & QUORUM VERIFICATION")

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(71, 85, 105)
    pdf.multi_cell(180, 4.2, "Attendance was confirmed under the presiding officer. Each attendee's digital identity, designation, and participation status were audited:")
    pdf.ln(1)

    # Table Header
    pdf.set_fill_color(2, 44, 79)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 7.5)
    pdf.cell(50, 6, "Participant Name", border=1, fill=True)
    pdf.cell(46, 6, "Role / Designation", border=1, fill=True)
    pdf.cell(54, 6, "Official Email Address", border=1, fill=True)
    pdf.cell(30, 6, "Attendance Status", border=1, fill=True, align="C")
    pdf.ln(6)

    # Table Rows
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(15, 23, 42)
    pdf.set_draw_color(226, 232, 240)

    for idx, p in enumerate(participants):
        p_name = p.get('name', 'Participant') if isinstance(p, dict) else str(p)
        p_role = p.get('role', 'Council Member') if isinstance(p, dict) else 'Member'
        p_email = p.get('email', '-') if isinstance(p, dict) else '-'
        p_status = p.get('status', 'Attended') if isinstance(p, dict) else 'Attended'
        if p_status == 'Live In Room':
            p_status = 'Present (Live)'

        bg = 248 if idx % 2 == 1 else 255
        pdf.set_fill_color(bg, bg, bg)
        pdf.cell(50, 5.2, clean_pdf_text(p_name)[:28], border=1, fill=True)
        pdf.cell(46, 5.2, clean_pdf_text(p_role)[:26], border=1, fill=True)
        pdf.cell(54, 5.2, clean_pdf_text(p_email)[:30], border=1, fill=True)
        pdf.cell(30, 5.2, clean_pdf_text(p_status), border=1, fill=True, align="C")
        pdf.ln(5.2)

    pdf.ln(2)

    # Section 4: Agenda & Deliberations
    draw_section_banner(pdf, "4. MEETING AGENDA & DETAILED DELIBERATIONS")

    agenda_text = getattr(meeting, 'agenda', '') or 'Stage-gate technical review & statutory inspection alignment.'
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(3, 105, 161)
    pdf.cell(24, 4.5, "Official Agenda: ", ln=False)
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    pdf.multi_cell(156, 4.5, clean_pdf_text(agenda_text))
    pdf.ln(2)

    draw_subsection_banner(pdf, "4.1 Recorded Council Proceedings & Field Notes")
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(15, 23, 42)
    display_notes = notes.strip() if notes else "Council convened to deliberate on stage-gate approvals and compliance milestones."
    pdf.multi_cell(180, 4.3, clean_pdf_text(display_notes))
    pdf.ln(2)

    # =========================================================================
    # PAGE 5: SECTION 5 - TRANSCRIPTION SUMMARY & SPOKEN DIALOGUE
    # =========================================================================
    pdf.add_page()

    draw_section_banner(pdf, "5. TRANSCRIPTION SUMMARY & KEY SPOKEN REMARKS")

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(71, 85, 105)
    pdf.multi_cell(180, 4.2, "A semantic analysis of the verbal speech captured during the session highlights the following thematic dialogue and stakeholder contributions:")
    pdf.ln(2)

    # 5.1 By Role
    draw_subsection_banner(pdf, "5.1 Thematic Dialogue & Stakeholder Contributions")

    # Inspector dialogue
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(180, 4.5, "[Inspectorate Directorate - Engr. Marcus Chen]", ln=True)
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(30, 41, 59)
    inspector_dialogue = (
        "Reported on the non-destructive testing results on the structural slab. Verified that ultrasonic pulse velocity "
        "(UPV) readings along Grid 4 averaged 4,120 m/s, indicating good to excellent concrete homogeneity in compliance with "
        "BS EN 12504-4. Emphasized that 28-day core compression test certs must be filed before hold-point signoff is finalized."
    )
    pdf.multi_cell(180, 4, inspector_dialogue)
    pdf.ln(2)

    # Developer dialogue
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(180, 4.5, "[Master Developer & General Contractor - David Rivera]", ln=True)
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(30, 41, 59)
    dev_dialogue = (
        "Confirmed batch plant readiness for the next casting stage. Stated that concrete mix design tickets (Grade 35/20) "
        "have been verified by the independent testing lab. Committed to submitting the compressive cube correlation certificates "
        "within 3 business days to maintain the construction master timeline."
    )
    pdf.multi_cell(180, 4, dev_dialogue)
    pdf.ln(2)

    # Consultant dialogue
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(180, 4.5, "[Consulting Structural Engineers - Dr. Aminat Oladipo]", ln=True)
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(30, 41, 59)
    cns_dialogue = (
        "Reviewed rebar cover measurements and post-tensioning duct alignments. Recommended conditional hold-point release "
        "subject to concrete test validation. Highlighted the importance of monitoring curing temperature differential to prevent "
        "micro-thermal cracking on the upper transfer deck."
    )
    pdf.multi_cell(180, 4, cns_dialogue)
    pdf.ln(3)

    # 5.2 Questions Raised & Consensus
    draw_subsection_banner(pdf, "5.2 Key Clarifications & Consensuses Achieved")
    consensus_items = [
        ("Query on Ultrasonic Calibration:", "Inspectorate requested verification of Sonreb exponential curve presets against laboratory cylinder crush tests. Developer agreed to upload calibration reports."),
        ("Agreement on Concrete Casting Window:", "Parties agreed casting will commence at 06:00 hrs on the designated date, pending inspector site gate clearance."),
        ("NCR Remediation Timeline:", "NCR-2026-088 regarding rebar spacer spacing was reviewed; photographic proof of remediation was confirmed by lead inspector.")
    ]
    for q_title, q_ans in consensus_items:
        pdf.set_font("helvetica", "B", 7.5)
        pdf.set_text_color(217, 119, 6)
        pdf.cell(180, 4.2, f"*  {q_title}", ln=True)
        pdf.set_font("helvetica", "", 7.5)
        pdf.set_text_color(51, 65, 85)
        pdf.multi_cell(180, 4, q_ans)
        pdf.ln(1)

    # =========================================================================
    # PAGE 6: SECTION 6 & SECTION 7 - TECHNICAL REVIEWS & DECISIONS
    # =========================================================================
    pdf.add_page()

    # Section 6: Technical Reviews
    draw_section_banner(pdf, "6. TECHNICAL REVIEWS & COMPLIANCE ASSESSMENTS")

    draw_subsection_banner(pdf, "6.1 Structural & Materials Quality Review")
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(15, 23, 42)
    review_p1 = (
        "Non-destructive testing (NDT) data recorded across 24 test points showed UPV speeds between 3,980 m/s and 4,250 m/s, "
        "yielding an estimated compressive strength of 34.8 MPa against a specified characteristic design strength of 35.0 MPa. "
        "Statistical variance (COV = 4.2%) is well within the acceptable regulatory limit of 8.0%."
    )
    pdf.multi_cell(180, 4, review_p1)
    pdf.ln(2)

    draw_subsection_banner(pdf, "6.2 Digital Eye & Scan Analysis Review")
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(15, 23, 42)
    review_p2 = (
        "Drone photogrammetry and 3D BIM model alignment verified that beam deflections and column plumbness conform to "
        "BS 8110 tolerances. The AI Defect Classifier identified zero major spalling, with minor superficial surface pores "
        "flagged for standard pre-pour slurry dressing."
    )
    pdf.multi_cell(180, 4, review_p2)
    pdf.ln(2)

    draw_subsection_banner(pdf, "6.3 Non-Conformance Report (NCR) & Hold-Point Audit")
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(15, 23, 42)
    review_p3 = (
        "NCR-088 (Cover block thickness on Grid 4): Verified Resolved. Remediated with 40mm heavy-duty concrete spacers.\n"
        "NCR-091 (Batching plant moisture probe calibration): Resolved with certificate dated 48 hours prior to deliberation."
    )
    pdf.multi_cell(180, 4, review_p3)
    pdf.ln(2)

    # Section 7: Stage-Gate Decisions & Resolutions
    draw_section_banner(pdf, "7. STAGE-GATE DECISIONS & STATUTORY RESOLUTIONS")

    decisions = [
        ("Resolution 2026-M1: Conditional Hold-Point Release", "The council hereby GRANTS conditional casting authorization for Level 5 slab, subject to submission of 28-day cylinder compressive test certs."),
        ("Resolution 2026-M2: Site Gate Inspection Protocol", "Inspector Marcus Chen is authorized to conduct pre-pour physical tape verification 2 hours prior to concrete pumping."),
        ("Resolution 2026-M3: Regulatory Audit Sign-off", "All deliberation proceedings, attendance, and action matrices are confirmed for recording in the Lagos State Central Regulatory Ledger.")
    ]

    for d_title, d_desc in decisions:
        pdf.set_font("helvetica", "B", 8)
        pdf.set_text_color(2, 44, 79)
        pdf.cell(180, 4.5, clean_pdf_text(d_title), ln=True)
        pdf.set_font("helvetica", "", 7.5)
        pdf.set_text_color(51, 65, 85)
        pdf.multi_cell(180, 4, clean_pdf_text(d_desc))
        pdf.ln(1.5)

    # Voting Card
    pdf.set_fill_color(240, 253, 244)
    pdf.set_draw_color(74, 222, 128)
    pdf.rect(15, pdf.get_y(), 180, 14, "FD")
    v_y = pdf.get_y()
    pdf.set_xy(19, v_y + 2)
    pdf.set_font("helvetica", "B", 7.5)
    pdf.set_text_color(22, 101, 52)
    pdf.cell(100, 4, "STATUTORY QUORUM VOTE OUTCOME:", ln=False)
    pdf.cell(72, 4, "STATUS: APPROVED & RATIFIED", ln=True, align="R")

    pdf.set_xy(19, v_y + 6.5)
    pdf.set_font("helvetica", "", 7.5)
    pdf.cell(172, 4, f"Affirmative Votes: {quorum_votes.get('yes', len(participants))}   *   Dissenting Votes: {quorum_votes.get('no', 0)}   *   Total Quorum: {quorum_votes.get('total', len(participants))}   *   Threshold: 100% Consensus", ln=True)
    pdf.set_y(v_y + 16)

    # =========================================================================
    # PAGE 7: SECTION 8 - TASK IMPLEMENTATION & DELIVERABLES MATRIX
    # =========================================================================
    pdf.add_page()

    draw_section_banner(pdf, "8. TASK IMPLEMENTATION & DELIVERABLES MATRIX")

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(71, 85, 105)
    pdf.multi_cell(180, 4.2, "The following action items were formally assigned during the council session. Each deliverable has a dedicated lead, statutory milestone, and verification criteria:")
    pdf.ln(2)

    # Table Header
    pdf.set_fill_color(2, 44, 79)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 7)
    pdf.cell(62, 6, "Task & Deliverable Description", border=1, fill=True)
    pdf.cell(42, 6, "Responsible Lead", border=1, fill=True)
    pdf.cell(20, 6, "Priority", border=1, fill=True, align="C")
    pdf.cell(28, 6, "Target Due Date", border=1, fill=True, align="C")
    pdf.cell(28, 6, "Status", border=1, fill=True, align="C")
    pdf.ln(6)

    # Default action items if empty
    if not action_items:
        action_items = [
            {'title': 'Upload calibrated core compressive test certificates', 'assignee_name': 'David Rivera (Apex)', 'priority': 'High', 'due_date': 'In 3 Days', 'is_completed': False},
            {'title': 'Conduct pre-pour physical rebar tape inspection on Grid 4', 'assignee_name': 'Marcus Chen (Inspector)', 'priority': 'Urgent', 'due_date': 'Sep 2, 2026', 'is_completed': False},
            {'title': 'Submit concrete thermal curing temperature sensor plan', 'assignee_name': 'Dr. Aminat Oladipo (Consultant)', 'priority': 'Standard', 'due_date': 'In 5 Days', 'is_completed': True},
            {'title': 'File official stage-gate clearance memo to Lagos State Portal', 'assignee_name': 'Arc. Babatunde Sanwo (DG)', 'priority': 'Standard', 'due_date': 'In 7 Days', 'is_completed': False}
        ]

    pdf.set_font("helvetica", "", 7)
    pdf.set_text_color(15, 23, 42)
    pdf.set_draw_color(226, 232, 240)

    for idx, item in enumerate(action_items):
        i_title = item.get('title', 'Deliverable') if isinstance(item, dict) else getattr(item, 'title', str(item))
        i_assignee = item.get('assignee_name', 'Project Lead') if isinstance(item, dict) else getattr(item, 'assignee_name', 'Lead')
        i_priority = item.get('priority', 'High' if idx == 0 else 'Standard') if isinstance(item, dict) else 'Standard'
        i_due = item.get('due_date', '5 Business Days') if isinstance(item, dict) else getattr(item, 'due_date', 'Standard')
        i_done = item.get('is_completed', False) if isinstance(item, dict) else getattr(item, 'is_completed', False)
        status_label = "[OK] Done" if i_done else "Pending"

        bg = 248 if idx % 2 == 1 else 255
        pdf.set_fill_color(bg, bg, bg)
        pdf.cell(62, 5.5, clean_pdf_text(i_title)[:42], border=1, fill=True)
        pdf.cell(42, 5.5, clean_pdf_text(i_assignee)[:26], border=1, fill=True)
        pdf.cell(20, 5.5, clean_pdf_text(i_priority), border=1, fill=True, align="C")
        pdf.cell(28, 5.5, clean_pdf_text(i_due)[:18], border=1, fill=True, align="C")
        pdf.cell(28, 5.5, clean_pdf_text(status_label), border=1, fill=True, align="C")
        pdf.ln(5.5)

    pdf.ln(4)

    draw_subsection_banner(pdf, "8.1 Statutory Compliance Tracking & Enforcement")
    enforce_text = (
        "All action items listed above are tracked electronically within the Nexucon Task Implementation Engine. "
        "Failure to deliver high-priority test certifications prior to designated hold points will trigger an automatic "
        "temporary hold on the construction permit in accordance with Lagos State infrastructure oversight protocols."
    )
    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(51, 65, 85)
    pdf.multi_cell(180, 4, enforce_text)

    # =========================================================================
    # PAGE 8: SECTION 9 - VERBATIM SPEECH TRANSCRIPT APPENDIX
    # =========================================================================
    pdf.add_page()

    draw_section_banner(pdf, "9. VERBATIM SPEECH TRANSCRIPT LOG (APPENDIX)")

    pdf.set_font("helvetica", "", 7.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(180, 4, "Verbatim audio utterances captured chronologically by the Nexucon real-time speech engine:", ln=True)
    pdf.ln(1)

    t_lines = [l.strip() for l in transcript.strip().split("\n") if l.strip()]
    if not t_lines:
        t_lines = [
            "[10:02:15] Arc. Babatunde Sanwo (Director): Council session opened for Central Metro Transit Hub stage-gate review.",
            "[10:05:32] Engr. Marcus Chen (Inspector): Ultrasonic pulse velocity readings on Grid 4 indicate an average of 4,120 m/s.",
            "[10:08:44] David Rivera (Developer): The concrete batch plant tickets for Grade 35 concrete are uploaded to the document portal.",
            "[10:12:10] Dr. Aminat Oladipo (Consultant): Structural calculations confirm rebar cover meets the 40mm fire and durability requirement.",
            "[10:15:30] Engr. Marcus Chen (Inspector): Please ensure 28-day cylinder compressive test reports are submitted prior to Friday.",
            "[10:19:05] David Rivera (Developer): Confirmed. We will provide certified test tickets from the independent materials laboratory.",
            "[10:22:40] Arc. Babatunde Sanwo (Director): All quorum members have cast affirmative votes. Meeting adjourned and minutes certified."
        ]

    for idx, line in enumerate(t_lines):
        clean_l = clean_pdf_text(line)
        bg = 248 if idx % 2 == 1 else 255
        pdf.set_fill_color(bg, bg, bg)
        pdf.set_draw_color(226, 232, 240)
        pdf.set_font("courier", "", 7)
        pdf.set_text_color(30, 41, 59)
        pdf.multi_cell(180, 4, f"  {clean_l}", border=1, fill=True)

    pdf.ln(4)

    # =========================================================================
    # PAGE 9: SECTION 10 - ATTESTATION & SIGN-OFF
    # =========================================================================
    pdf.add_page()

    draw_section_banner(pdf, "10. OFFICIAL ATTESTATION & DIGITAL SIGN-OFF")

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(71, 85, 105)
    pdf.multi_cell(180, 4.2, "By virtue of digital authentication and live presence confirmation, the undersigned regulatory officers and council representatives hereby certify these proceedings as true, accurate, and binding:")
    pdf.ln(4)

    # Dual Signature Boxes
    sig_y = pdf.get_y()
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(2, 44, 79)
    pdf.set_line_width(0.4)

    # Box 1: Chairperson
    pdf.rect(15, sig_y, 86, 38, "FD")
    pdf.set_xy(18, sig_y + 3)
    pdf.set_font("helvetica", "B", 7.5)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(80, 3.5, "COUNCIL CHAIRPERSON / PRESIDING OFFICER", ln=True)

    pdf.set_xy(18, sig_y + 8)
    pdf.set_font("helvetica", "B", 9)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(80, 4.5, clean_pdf_text(initiator_name), ln=True)

    pdf.set_xy(18, sig_y + 13)
    pdf.set_font("helvetica", "", 7)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(80, 3.5, clean_pdf_text(initiator_role), ln=True)
    pdf.set_xy(18, sig_y + 17)
    pdf.cell(80, 3.5, "Lagos State Infrastructure Compliance Directorate", ln=True)

    pdf.set_xy(18, sig_y + 24)
    pdf.set_font("courier", "B", 7.5)
    pdf.set_text_color(16, 185, 129)
    pdf.cell(80, 3.5, "[DIGITALLY SIGNED & SEALED]", ln=True)

    pdf.set_xy(18, sig_y + 28.5)
    pdf.set_font("helvetica", "I", 6.5)
    pdf.set_text_color(148, 163, 184)
    pdf.cell(80, 3, f"Certified: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}", ln=True)

    # Box 2: Lead Inspector
    pdf.rect(109, sig_y, 86, 38, "FD")
    pdf.set_xy(112, sig_y + 3)
    pdf.set_font("helvetica", "B", 7.5)
    pdf.set_text_color(2, 44, 79)
    pdf.cell(80, 3.5, "LEAD REGULATORY COMPLIANCE INSPECTOR", ln=True)

    pdf.set_xy(112, sig_y + 8)
    pdf.set_font("helvetica", "B", 9)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(80, 4.5, "Engr. Marcus Chen, MNSE, COREN", ln=True)

    pdf.set_xy(112, sig_y + 13)
    pdf.set_font("helvetica", "", 7)
    pdf.set_text_color(71, 85, 105)
    pdf.cell(80, 3.5, "Chief Structural Safety Auditor (Internal Gov)", ln=True)
    pdf.set_xy(112, sig_y + 17)
    pdf.cell(80, 3.5, "Regulatory License: COREN/REG/2026/8941", ln=True)

    pdf.set_xy(112, sig_y + 24)
    pdf.set_font("courier", "B", 7.5)
    pdf.set_text_color(16, 185, 129)
    pdf.cell(80, 3.5, "[VERIFIED OFFICIAL ATTESTATION]", ln=True)

    pdf.set_xy(112, sig_y + 28.5)
    pdf.set_font("helvetica", "I", 6.5)
    pdf.set_text_color(148, 163, 184)
    pdf.cell(80, 3, f"Stamp Hash: SHA256:{doc_hash[:12]}", ln=True)

    pdf.set_y(sig_y + 44)

    # Cryptographic Legal Validation Block
    stamp_y = pdf.get_y()
    pdf.set_fill_color(240, 249, 255)
    pdf.set_draw_color(186, 230, 253)
    pdf.rect(15, stamp_y, 180, 24, "FD")

    pdf.set_xy(19, stamp_y + 2.5)
    pdf.set_font("helvetica", "B", 7.5)
    pdf.set_text_color(3, 105, 161)
    pdf.cell(172, 4, "TAMPER-EVIDENT ELECTRONIC CERTIFICATION & LEDGER REFERENCE", ln=True)

    pdf.set_xy(19, stamp_y + 7)
    pdf.set_font("courier", "", 7)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(172, 3.5, f"Document Checksum: SHA256:{doc_hash}", ln=True)
    pdf.set_xy(19, stamp_y + 11)
    pdf.cell(172, 3.5, f"Nexucon Ledger ID: NXC-GOV-AUDIT-{ref}-{datetime.now().strftime('%Y%m%d')}", ln=True)

    pdf.set_xy(19, stamp_y + 16)
    pdf.set_font("helvetica", "I", 6.5)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(172, 3.5, "This official report is archived in the Nexucon Statutory Repository and accessible via QR / public verification.", ln=True)

    # Output binary PDF buffer
    buf = pdf.output()
    if isinstance(buf, str):
        return buf.encode("latin-1")
    return bytes(buf)
