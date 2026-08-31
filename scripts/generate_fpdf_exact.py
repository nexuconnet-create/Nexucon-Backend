import markdown
from pathlib import Path
from fpdf import FPDF
from fpdf.enums import XPos, YPos

BASE_DIR = Path(r"c:\Users\USER\OneDrive\Desktop\coding\nexucon\Tarsus-sitesupervise-Integration-\docs")

class PDF(FPDF):
    def header(self):
        # Header is only added if page_no > 1
        if self.page_no() > 1:
            self.set_font("helvetica", "B", 10)
            self.set_text_color(0, 0, 0)
            self.cell(0, 10, "Confidential and Proprietary to Nexucon", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
            self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", "", 10)
        self.set_text_color(0, 0, 0)
        self.cell(0, 10, f"Copyright (c) 2026 NEXUCON.NET All rights reserved Page {self.page_no()}", new_x=XPos.RIGHT, new_y=YPos.TOP, align="C")

def create_title_page(pdf):
    pdf.add_page()
    pdf.set_y(50)
    pdf.set_font("helvetica", "B", 24)
    pdf.cell(0, 15, "NEXUCON", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    
    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 15, "Backend Technical Architecture & API Documentation", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    
    pdf.ln(20)
    pdf.set_font("helvetica", "", 12)
    pdf.cell(0, 10, "Version: 1.0.0", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.cell(0, 10, "Date: August 2026", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    
    pdf.ln(30)
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 10, "Prepared for: NEXUCON.NET & SITESUPERVISE.TECH", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    
    pdf.ln(50)
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 10, "CONFIDENTIALITY NOTICE:", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 8, "This document contains proprietary information and is strictly confidential.", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.cell(0, 8, "Unauthorized distribution, copying, or disclosure is strictly prohibited under the NDA.", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")

def convert_md_to_pdf(md_file_path, pdf_file_path):
    with open(md_file_path, "r", encoding="utf-8") as md_file:
        text = md_file.read()

    # Split frontmatter if it exists
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
            
    # Remove <style> blocks
    import re
    text = re.sub(r"<style>.*?</style>", "", text, flags=re.DOTALL)
            
    # Convert MD to HTML for fpdf2
    html_body = markdown.markdown(text, extensions=['tables', 'fenced_code'])
    
    # Sanitize html for fpdf2
    html_body = html_body.replace("–", "-").replace("—", "-").replace("“", '"').replace("”", '"').replace("’", "'").replace("`", "")
    html_body = html_body.encode("latin-1", "ignore").decode("latin-1")
    
    # Add minimal style for write_html
    html_body = f"""
    <font face="helvetica" size="10">
    {html_body}
    </font>
    """
    
    pdf = PDF()
    
    # Title Page
    create_title_page(pdf)
    
    # Content Pages
    pdf.add_page()
    pdf.set_font("helvetica", size=10)
    
    try:
        pdf.write_html(html_body)
    except Exception as e:
        print("HTML rendering error:", e)
        # fallback
        pdf.add_page()
        pdf.multi_cell(0, 5, text.encode("latin-1", "replace").decode("latin-1"))

    pdf.output(str(pdf_file_path))
    print(f"PDF successfully generated at {pdf_file_path}")

if __name__ == "__main__":
    md_path = BASE_DIR / "Backend_Architecture_Report.md"
    pdf_path = BASE_DIR / "NEXUCON_SiteSupervise_Backend_Technical_Documentation_v1.0.pdf"
    convert_md_to_pdf(md_path, pdf_path)
