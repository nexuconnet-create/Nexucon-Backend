import markdown
from xhtml2pdf import pisa
from pathlib import Path

BASE_DIR = Path(r"c:\Users\USER\OneDrive\Desktop\coding\nexucon\Tarsus-sitesupervise-Integration-\docs")

def convert_md_to_pdf(md_file_path, pdf_file_path):
    with open(md_file_path, "r", encoding="utf-8") as md_file:
        text = md_file.read()

    # Split frontmatter if it exists
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]

    html_body = markdown.markdown(text, extensions=['tables', 'fenced_code'])

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>NEXUCON Backend Docs</title>
        <style>
            @page {{
                size: a4 portrait;
                margin: 2cm;
                @top-center {{ content: "CONFIDENTIAL AND PROPRIETARY TO NEXUCON"; font-size: 9pt; color: #7f8c8d; }}
                @bottom-center {{ content: "Copyright © 2026 NEXUCON.NET All rights reserved - Page " counter(page); font-size: 9pt; color: #7f8c8d; }}
            }}
            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.4; }}
            h1 {{ color: #2c3e50; font-size: 24pt; border-bottom: 1px solid #3498db; padding-bottom: 5px; }}
            h2 {{ color: #2980b9; font-size: 18pt; margin-top: 20px; }}
            h3 {{ color: #16a085; font-size: 14pt; }}
            table {{ border: 1px solid #ddd; width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 6px; text-align: left; }}
            th {{ background-color: #f2f2f2; font-weight: bold; }}
            .center {{ text-align: center; }}
            .small {{ font-size: 9pt; color: #7f8c8d; }}
        </style>
    </head>
    <body>
        <div id="header_content" class="center small">
            CONFIDENTIAL AND PROPRIETARY TO NEXUCON
        </div>
        
        {html_body}
        
        <div id="footer_content" class="center small">
            Copyright © 2026 NEXUCON.NET All rights reserved - Page <pdf:pagenumber>
        </div>
    </body>
    </html>
    """

    with open(pdf_file_path, "wb") as pdf_file:
        pisa_status = pisa.CreatePDF(
            src=html,
            dest=pdf_file
        )

    if pisa_status.err:
        print("Error creating PDF:", pisa_status.err)
    else:
        print(f"PDF successfully generated at {pdf_file_path}")

if __name__ == "__main__":
    md_path = BASE_DIR / "Backend_Architecture_Report.md"
    pdf_path = BASE_DIR / "NEXUCON_SiteSupervise_Backend_Technical_Documentation_v1.0.pdf"
    convert_md_to_pdf(md_path, pdf_path)
