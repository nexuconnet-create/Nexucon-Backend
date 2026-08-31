import markdown
from pathlib import Path

BASE_DIR = Path(r"c:\Users\USER\OneDrive\Desktop\coding\nexucon\Tarsus-sitesupervise-Integration-\docs")

def convert_md_to_html(md_file_path, html_file_path):
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
                margin: 2cm;
            }}
            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.4; }}
            h1 {{ color: #2c3e50; font-size: 24pt; border-bottom: 1px solid #3498db; padding-bottom: 5px; page-break-before: always; }}
            h1:first-child {{ page-break-before: avoid; }}
            h2 {{ color: #2980b9; font-size: 18pt; margin-top: 20px; }}
            h3 {{ color: #16a085; font-size: 14pt; }}
            table {{ border: 1px solid #ddd; width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 6px; text-align: left; }}
            th {{ background-color: #f2f2f2; font-weight: bold; }}
            .header {{ position: fixed; top: 0; width: 100%; text-align: center; font-size: 9pt; color: #7f8c8d; }}
            .footer {{ position: fixed; bottom: 0; width: 100%; text-align: center; font-size: 9pt; color: #7f8c8d; }}
            /* Add some padding so content doesn't overlap with fixed header/footer */
            body {{ padding-top: 30px; padding-bottom: 30px; }}
        </style>
    </head>
    <body>
        <div class="header">CONFIDENTIAL AND PROPRIETARY TO NEXUCON</div>
        <div class="footer">Copyright © 2026 NEXUCON.NET All rights reserved</div>
        {html_body}
    </body>
    </html>
    """

    with open(html_file_path, "w", encoding="utf-8") as html_file:
        html_file.write(html)
    print(f"HTML successfully generated at {html_file_path}")

if __name__ == "__main__":
    md_path = BASE_DIR / "Backend_Architecture_Report.md"
    html_path = BASE_DIR / "report.html"
    convert_md_to_html(md_path, html_path)
