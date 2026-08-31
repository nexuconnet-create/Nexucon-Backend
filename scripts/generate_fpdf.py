import markdown
from pathlib import Path
from fpdf import FPDF

BASE_DIR = Path(r"c:\Users\USER\OneDrive\Desktop\coding\nexucon\Tarsus-sitesupervise-Integration-\docs")

class PDF(FPDF):
    def header(self):
        self.set_font("helvetica", "B", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, "CONFIDENTIAL AND PROPRIETARY TO NEXUCON", 0, 0, "C")
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Copyright (c) 2026 NEXUCON.NET All rights reserved - Page {self.page_no()}", 0, 0, "C")

def convert_md_to_pdf(md_file_path, pdf_file_path):
    with open(md_file_path, "r", encoding="utf-8") as md_file:
        text = md_file.read()

    # Split frontmatter if it exists
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
            
    # fpdf2's write_html does not fully support advanced markdown tables or complex CSS.
    # However, it renders basic html well.
    html_body = markdown.markdown(text, extensions=['tables', 'fenced_code'])
    
    # We need to strip out things fpdf2 might choke on.
    # fpdf2 doesn't like generic markdown tables with <thead>, it prefers basic <tr><td>.
    
    # Sanitize html for fpdf2 default font (helvetica doesn't support full unicode)
    html_body = html_body.replace("–", "-").replace("—", "-").replace("“", '"').replace("”", '"').replace("’", "'")
    html_body = html_body.encode("latin-1", "ignore").decode("latin-1")
    
    pdf = PDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=11)
    
    try:
        pdf.write_html(html_body)
    except Exception as e:
        print("HTML rendering error:", e)
        # fallback to plain text if HTML fails
        pdf.add_page()
        pdf.set_font("helvetica", size=10)
        pdf.multi_cell(0, 5, text.encode("latin-1", "replace").decode("latin-1"))

    pdf.output(str(pdf_file_path))
    print(f"PDF successfully generated at {pdf_file_path}")

if __name__ == "__main__":
    md_path = BASE_DIR / "Backend_Architecture_Report.md"
    pdf_path = BASE_DIR / "NEXUCON_SiteSupervise_Backend_Technical_Documentation_v1.0.pdf"
    convert_md_to_pdf(md_path, pdf_path)
