import re
import sys
import markdown
from pathlib import Path
from xhtml2pdf import pisa

BASE_DIR = Path(r"c:\Users\USER\OneDrive\Desktop\coding\nexucon\Tarsus-sitesupervise-Integration-\docs")

def convert_md_to_pdf(md_file_path, pdf_file_path):
    with open(md_file_path, "r", encoding="utf-8") as md_file:
        text = md_file.read()

    # Remove YAML frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]
            
    # Remove old <style> blocks
    text = re.sub(r"<style>.*?</style>", "", text, flags=re.DOTALL)
    
    # Remove lines 20-25 manually which have the old Title and Date
    lines = text.strip().split('\n')
    filtered_lines = []
    skip = True
    for line in lines:
        if line.startswith("## 1. Executive Summary") or line.startswith("## Executive Summary") or line.startswith("1. Executive Summary"):
            skip = False
        if not skip:
            filtered_lines.append(line)
            
    # Re-assemble text
    text = "\n".join(filtered_lines)

    html_body = markdown.markdown(text, extensions=['tables', 'fenced_code'])
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <title>NEXUCON Backend Architecture</title>
    <style>
        @page {{
            size: a4 portrait;
            margin: 1.5cm;
            margin-top: 2cm;
            margin-bottom: 2.5cm;
            @frame header {{
                -pdf-frame-content: header_content;
                top: 1cm;
                margin-left: 1.5cm;
                margin-right: 1.5cm;
                height: 1cm;
            }}
            @frame footer {{
                -pdf-frame-content: footer_content;
                bottom: 1cm;
                margin-left: 1.5cm;
                margin-right: 1.5cm;
                height: 1cm;
            }}
        }}
        body {{
            font-family: Helvetica, Arial, sans-serif;
            font-size: 10pt;
            color: #334155;
            line-height: 1.4;
        }}
        h1 {{
            color: #1E293B;
            font-size: 20pt;
            font-weight: bold;
            margin-bottom: 2pt;
        }}
        h2 {{
            color: #1E293B;
            font-size: 14pt;
            font-weight: bold;
            margin-top: 14pt;
            margin-bottom: 4pt;
            border-bottom: 1px solid #0F766E;
            padding-bottom: 4pt;
        }}
        h3 {{
            color: #0F766E;
            font-size: 11pt;
            font-weight: bold;
            margin-top: 10pt;
            margin-bottom: 4pt;
        }}
        p {{
            margin-bottom: 8pt;
        }}
        ul, ol {{
            margin-bottom: 8pt;
        }}
        li {{
            font-size: 9.5pt;
            margin-bottom: 4pt;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 12pt;
        }}
        th {{
            background-color: #1E293B;
            color: #FFFFFF;
            font-size: 9pt;
            font-weight: bold;
            padding: 6pt;
            text-align: left;
            word-wrap: break-word;
        }}
        td {{
            font-size: 8.5pt;
            color: #334155;
            padding: 6pt;
            border: 0.5px solid #E2E8F0;
            word-wrap: break-word;
        }}
        tr:nth-child(even) td {{
            background-color: #F8FAFC;
        }}
        code {{
            background-color: #F8FAFC;
            color: #0F172A;
            border: 0.5px solid #E2E8F0;
            padding: 2pt 4pt;
            font-size: 9pt;
            font-family: "Courier New", Courier, monospace;
            word-wrap: break-word;
        }}
        pre {{
            background-color: #F8FAFC;
            border: 0.5px solid #E2E8F0;
            padding: 8pt;
            margin-bottom: 8pt;
        }}
        pre code {{
            border: none;
            padding: 0;
        }}
    </style>
    </head>
    <body>
        <div id="header_content">
            <div style="font-size: 8pt; color: #64748B; text-align: center; font-weight: bold;">
                Confidential and Proprietary to Nexucon
            </div>
        </div>
        
        <div id="footer_content">
            <table style="width: 100%; border-top: 0.5px solid #E2E8F0; padding-top: 12pt; margin-bottom: 0;">
                <tr>
                    <td style="border: none; padding: 0; padding-top: 5pt; font-size: 9pt; color: #64748B;">Copyright &copy; 2026 NEXUCON.NET All rights reserved</td>
                    <td style="border: none; padding: 0; padding-top: 5pt; font-size: 9pt; color: #64748B; text-align: right;">Page <pdf:pagenumber/></td>
                </tr>
            </table>
        </div>

        <div>
            <h1 style="color: #1E293B; font-size: 20pt; font-weight: bold; margin-bottom: 2pt;">NEXUCON</h1>
            <div style="color: #0F766E; font-size: 15pt; font-weight: bold; margin-bottom: 14pt;">Backend Architecture & Technical Documentation</div>
            
            <table style="width: 100%; background-color: #F8FAFC; border: 0.5px solid #E2E8F0; padding: 8pt; margin-bottom: 20pt;">
                <tr>
                    <td style="border: none; color: #0F766E; font-weight: bold; font-size: 10pt; width: 15%;">Date:</td>
                    <td style="border: none; color: #334155; font-size: 10pt; width: 35%;">August 2026</td>
                    <td style="border: none; color: #0F766E; font-weight: bold; font-size: 10pt; width: 15%;">Version:</td>
                    <td style="border: none; color: #334155; font-size: 10pt; width: 35%;">1.0.0</td>
                </tr>
                <tr>
                    <td style="border: none; color: #0F766E; font-weight: bold; font-size: 10pt;">Project:</td>
                    <td colspan="3" style="border: none; color: #334155; font-size: 10pt;">Nexucon - Backend Technical Architecture</td>
                </tr>
            </table>
        </div>
        
        {html_body}
    </body>
    </html>
    """

    with open(pdf_file_path, "wb") as pdf_file:
        pisa.CreatePDF(html, dest=pdf_file)
    print(f"PDF successfully generated at {pdf_file_path}")

if __name__ == "__main__":
    md_path = BASE_DIR / "Backend_Architecture_Report.md"
    pdf_path = BASE_DIR / "NEXUCON_SiteSupervise_Backend_Technical_Documentation_v1.0.pdf"
    convert_md_to_pdf(md_path, pdf_path)
